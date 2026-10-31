"""File Station operations: create_folder, rename, copy, move, delete, restore."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import (
    FILESTATION_ERROR_CODES,
    ErrorCode,
    SynologyError,
    error_from_code,
)
from mcp_synology.core.formatting import (
    error_response,
    format_size,
    format_status,
    synology_error_response,
)
from mcp_synology.modules.filestation.helpers import (
    describe_transport_error,
    dsm_str,
    ensure_recycle_status,
    find_missing_paths,
    missing_paths_suggestion,
    normalize_path,
    path_exists,
    paths_isdir,
    refuse_bad_name,
    refuse_dot_parts,
)
from mcp_synology.modules.filestation.tasks import (
    Handle,
    HandleKind,
    Handoff,
    MonotonicProgress,
    ProgressCallback,
    TaskRecord,
    TaskRecords,
    handoff_reply,
    poll_task,
    running_task,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)


async def create_folder(
    client: DsmClient,
    *,
    paths: list[str],
    force_parent: bool = True,
) -> str:
    """Create one or more new folders.

    Closes #95. DSM 7.x's `SYNO.FileStation.CreateFolder create` does not
    honor the documented comma-joined multi-path format — a request with
    `name=a,b` and `folder_path=/X,/X` is treated as a single literal name
    and creates one mangled folder while the tool reports
    `Created 1 folder(s)`. Same root-cause family as #68 (delete + getinfo)
    and #84 (move/copy); fix matches that pattern: one DSM CreateFolder
    call per input path. Per-path serial means N round-trips for N paths,
    which is fine for typical small-N usage and trivially correct.
    """
    normalized = [normalize_path(p) for p in paths]

    folders: list[dict[str, Any]] = []
    for p in normalized:
        parent = "/".join(p.split("/")[:-1]) or "/"
        name = p.split("/")[-1]
        try:
            data = await client.request(
                "SYNO.FileStation.CreateFolder",
                "create",
                params={
                    "folder_path": parent,
                    "name": dsm_str(name),
                    "force_parent": str(force_parent).lower(),
                },
            )
        except SynologyError as e:
            synology_error_response("Create folder", e)
        folders.extend(data.get("folders", []))

    lines = [format_status(f"Created {len(folders):,} folder(s):")]
    for f in folders:
        path = f.get("path", "")
        lines.append(f"  \U0001f4c1 {path}")

    return "\n".join(lines)


async def _explain_rename_1200(client: DsmClient, path: str, original: str, new_name: str) -> None:
    """Say which of DSM's two causes a rename's 1200 was, when getinfo can tell (#188).

    DSM answers 1200 both for a source that doesn't exist and for a name already taken in
    that folder (vdsm DSM 7.1, 7.2.2, 7.3.2 and 7.4.1). A source getinfo reports missing
    (408) is `not_found`; when getinfo shows both the source and the new path exist,
    `already_exists` (rename never replaces, #183). Anything else, including a failed
    check, returns, and the caller reports the 1200 with its table message.
    """
    parent, _, name = path.rpartition("/")
    target = f"{parent}/{new_name}"
    # The new path can't stand for a taken name when it's the source itself (its own
    # name), or when the source is a share root (no parent folder to look in; `rename`
    # refuses one before sending it since #479, so this only guards other callers). `rename`
    # refuses "." and ".." before getting here (#449).
    can_be_taken = bool(parent) and new_name != name
    try:
        source = await path_exists(client, path)
        taken = source is True and can_be_taken and await path_exists(client, target) is True
    except Exception:
        # Only a diagnostic: whatever it hits, DSM's own 1200 is still the answer.
        logger.warning("Couldn't check why DSM refused to rename %s (1200)", path, exc_info=True)
        return
    if source is False:
        error_response(
            ErrorCode.NOT_FOUND,
            f"Rename failed (DSM error 1200): {path} doesn't exist on the NAS.",
            retryable=False,
            param="path",
            value=original,
            suggestion=missing_paths_suggestion([(path, 408)]),
        )
    if taken:
        error_response(
            ErrorCode.ALREADY_EXISTS,
            f"Rename failed (DSM error 1200): {target} already exists. Rename doesn't "
            "replace an existing file or folder.",
            retryable=False,
            param="new_name",
            value=new_name,
            suggestion="Pick another name, or move or delete the existing one first.",
        )


async def rename(
    client: DsmClient,
    *,
    path: str,
    new_name: str,
) -> str:
    """Rename a file or folder."""
    normalized = normalize_path(path)
    _refuse_share_root_source("Rename", path, "path")

    # new_name is just a name: not a path, nor `.` or `..`, as #449 refuses in paths
    relocates = "/" in new_name or new_name.strip(" ") in (".", "..")
    refuse_bad_name(
        "Rename",
        new_name,
        "new_name",
        "Use move_files to relocate files to a different directory."
        if relocates
        else "Pass the new name only, e.g. Severance.",
    )

    try:
        data = await client.request(
            "SYNO.FileStation.Rename",
            "rename",
            params={
                "path": normalized,
                "name": dsm_str(new_name),
            },
        )
    except SynologyError as e:
        if e.code == 1200:
            await _explain_rename_1200(client, normalized, path, new_name)
        synology_error_response("Rename", e)

    files = data.get("files", [])
    if files:
        new_path = files[0].get("path", "")
        old_dir = "/".join(normalized.split("/")[:-1])
        old_name = normalized.split("/")[-1]
        return format_status(f"Renamed:\n  {old_dir}/{old_name} \u2192 {new_path}")

    return format_status(f"Renamed {normalized} to {new_name}")


async def copy_files(
    client: DsmClient,
    *,
    paths: list[str],
    dest_folder: str,
    overwrite: bool = False,
    timeout: float = 120.0,
    records: TaskRecords | None = None,
    on_progress: ProgressCallback | None = None,
) -> str:
    """Copy files or folders to a destination."""
    text, _ = await _copy_move(
        client,
        paths=paths,
        dest_folder=dest_folder,
        overwrite=overwrite,
        remove_src=False,
        operation="Copy",
        timeout=timeout,
        records=records,
        on_progress=on_progress,
    )
    return text


async def move_files(
    client: DsmClient,
    *,
    paths: list[str],
    dest_folder: str,
    overwrite: bool = False,
    timeout: float = 120.0,
    records: TaskRecords | None = None,
    on_progress: ProgressCallback | None = None,
) -> str:
    """Move files or folders to a new location."""
    for p in paths:
        _refuse_share_root_source("Move", p, "paths")
    text, _ = await _copy_move(
        client,
        paths=paths,
        dest_folder=dest_folder,
        overwrite=overwrite,
        remove_src=True,
        operation="Move",
        timeout=timeout,
        records=records,
        on_progress=on_progress,
    )
    return text


_PAST_TENSE = {"Copy": "copied", "Move": "moved", "Restore": "restored", "Delete": "deleted"}
_PRESENT = {"Copy": "Copying", "Move": "Moving", "Restore": "Restoring", "Delete": "Deleting"}
_KIND: dict[str, HandleKind] = {
    "Copy": "copy",
    "Move": "move",
    "Restore": "restore",
    "Delete": "delete",
}


def _already_running(operation: str, path: str, dest: str | None = None) -> str:
    """Lead line when a path already had a running task, so none was started (#245).

    ``dest`` names the running task's destination when it isn't the one asked for.
    """
    to = f" (to {dest}/)" if dest else ""
    return (
        f"A {operation.lower()} of {path}{to} was already running on the NAS, so no second "
        "one was started."
    )


def _collapse_overlaps(paths: list[str], *, drop_nested: bool = True) -> list[str]:
    """Drop repeated paths and paths inside another listed path, keeping order (QA R1 on
    #173). Each path runs as its own DSM task, so a child of an earlier path, or the same
    path twice, would run after the first task had already removed it.

    Copy passes ``drop_nested=False`` (QA R2 on #173): copying ``/a`` and ``/a/b`` is two
    copies, ``dest/a`` and ``dest/b``, and the child still exists after the parent's copy.
    """
    out: list[str] = []
    for p in paths:
        if p in out:
            continue
        if drop_nested and any(p != q and p.startswith(q.rstrip("/") + "/") for q in paths):
            continue
        out.append(p)
    return out


def refuse_bad_parts(paths: list[str]) -> None:
    """Refuse a ``.``, ``..``, empty or whitespace-only part, or a NUL, in a delete path.

    ``/video/.``, ``/video/x/..`` and ``//video`` (when DSM won't list shares) name the
    share itself (#451). Checked on the paths as given, before the share check rewrites
    them: ``normalize_path`` strips the whole path, so ``/video/x/ `` would be sent as
    ``/video/x``, deleting the folder (QA round 2 on #480; the same stripping in other tools
    is #488). A NUL is refused rather than sent: whether DSM cuts the path there (leaving
    ``/video/``) is unprobed, and no file name holds one (pre-QA on #480; #472). A plain
    ``.`` or ``..`` part gets #470's message, as in every other tool; the rest are the cases
    only a delete refuses.
    """
    for path in paths:
        refuse_dot_parts(path, "paths")
        parts = normalize_path(path).split("/")[1:]
        body = path.rstrip("/")
        blank_last = not body.rsplit("/", 1)[-1].strip()
        if blank_last or "\x00" in path or any(part.strip() in ("", ".", "..") for part in parts):
            error_response(
                ErrorCode.INVALID_PARAMETER,
                f"Delete files refused: '.', '..', empty and whitespace-only parts, and NUL "
                f"characters, aren't allowed ({path!r}). Nothing was deleted.",
                retryable=False,
                param="paths",
                value=path,
                suggestion="Write the path out in full from the shared folder, e.g. "
                "/video/Movies. An item whose name is only whitespace can't be reached "
                "this way: delete it in DSM's File Station.",
            )


def refuse_share_roots(paths: list[str]) -> None:
    """Refuse a path that names a whole shared folder (#451) or a share's whole recycle bin
    (#479), and any `refuse_bad_parts` path.

    On DSM 7.2.2, 7.3.2 and 7.4.1, File Station's Delete on ``/<share>`` deletes the shared
    folder itself, for any account with write access, and the recycle bin, inside the
    share, can't bring it back; DSM 7.1 refuses it. Emptying a share safely is #484. The
    tool calls this after the share check, so a one-part path that isn't a share (a typo)
    gets "unknown shared folder", not this (pre-QA on #480).

    Delete on ``/<share>/#recycle`` removes the whole bin folder, every user's deleted items
    with it, for an administrator and for a non-admin with write access alike (seska, DSM
    7.1.1; #479); DSM re-creates an empty bin at the share's next delete. A user's home bin,
    ``/homes/<user>/#recycle`` to an administrator, is refused too (`_bin_index`).
    """
    refuse_bad_parts(paths)
    for path in paths:
        parts = normalize_path(path).split("/")[1:]
        at = _bin_index(parts)
        if at is not None and len(parts) == at + 1:
            whose, lister = _whole_bin_words(parts)
            lost = (
                "everything in the bin"
                if at == 2
                else "everything in the bin, other users' deleted items included"
            )
            error_response(
                ErrorCode.INVALID_PARAMETER,
                f"Delete files refused: {path!r} is {whose}. Deleting it would permanently "
                f"remove {lost}. Nothing was deleted.",
                retryable=False,
                param="paths",
                value=path,
                suggestion=f"To delete items from the bin for good, list them with {lister} "
                f"and delete each by its own path (/{'/'.join(parts)}/<item>), after the "
                "user agrees; to empty the whole bin, use DSM's Control Panel > Shared "
                "Folder > Action > Empty Recycle Bin.",
            )
        if len(parts) == 1:
            error_response(
                ErrorCode.INVALID_PARAMETER,
                f"Delete files refused: {path!r} is a whole shared folder. delete_files "
                "doesn't delete or empty a shared folder: on DSM 7.2.2 to 7.4.1 (tested), "
                "and likely later, that would delete the shared folder itself, beyond the "
                "recycle bin's reach. "
                "Nothing was deleted.",
                retryable=False,
                param="paths",
                value=path,
                suggestion="To empty it, list it with list_files and delete its items by "
                "their own paths (leaving out its #recycle), after the user agrees; to delete "
                "the shared folder itself, use DSM's Control Panel > Shared Folder.",
            )


def _bin_index(parts: list[str]) -> int | None:
    """The index of the ``#recycle`` part when ``parts`` (a normalized path's parts, the
    share first) lead into a recycle bin, else None.

    A share's bin is ``/<share>/#recycle``. The user home service gives each user a bin too,
    which the user sees as ``/home/#recycle`` and an administrator as
    ``/homes/<user>/#recycle`` (pre-QA on #479; vdsm, all four versions).
    """
    if len(parts) > 1 and parts[1] == "#recycle":
        return 1
    if len(parts) > 2 and parts[0].casefold() == "homes" and parts[2] == "#recycle":
        return 2
    return None


def _whole_bin_words(parts: list[str]) -> tuple[str, str]:
    """What a whole bin is called in a refusal, and the tool that lists its items."""
    if _bin_index(parts) == 2:
        return (
            f"the whole recycle bin of {parts[1]}'s home folder ({parts[1]}'s /home/#recycle)",
            "list_files",
        )
    return "the shared folder's whole recycle bin", "list_recycle_bin"


def _refuse_share_root_source(operation: str, path: str, param: str) -> None:
    """Refuse a whole shared folder, or a share's whole recycle bin, as what a move moves or
    a rename renames (#479).

    On DSM 7.1, 7.2.2, 7.3.2 and 7.4.1 (vdsm), CopyMove with ``remove_src`` on ``/<share>``
    took the shared folder itself out of DSM's share list for the non-admin test user, who
    can write there, and the reply read as a skip (admin wasn't probed). Rename on
    ``/<share>`` made it vanish from the list for an administrator, under its new name too;
    a non-admin got 1200. Copying a shared folder only copies its contents into a new
    folder, so ``copy_files`` takes one. Moving or renaming ``/<share>/#recycle`` (or a user's
    ``/homes/<user>/#recycle``) would take the deleted items out of the bin, which restore refuses for the same reason
    (unprobed: refused on what the request names). Empty parts are dropped first:
    ``//video`` gets past the share check when DSM won't list shares, and Rename has no
    missing-path check (pre-QA). Called after the share check, as `refuse_share_roots` is.
    """
    parts = [part for part in normalize_path(path).split("/") if part]
    past = "moved" if operation == "Move" else "renamed"
    head = f"{operation} {'files ' if operation == 'Move' else ''}refused: {path!r} is"
    at = _bin_index(parts)
    if at is not None and len(parts) == at + 1:
        whose, _lister = _whole_bin_words(parts)
        acting = "Moving" if operation == "Move" else "Renaming"
        if at == 2:
            items = "that user's deleted items"
            suggestion = (
                f"To bring items back, {parts[1]} can use restore_from_recycle_bin with "
                f"share='home'; to move some elsewhere, move each by its own path "
                f"(/{'/'.join(parts)}/<item>)."
            )
        else:
            items = "every user's deleted items"
            suggestion = (
                "To bring items back, use restore_from_recycle_bin with the items "
                "list_recycle_bin shows; to move some elsewhere, restore them with dest_folder."
            )
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"{head} {whose}. {acting} it would take {items} out of the bin. Nothing was {past}.",
            retryable=False,
            param=param,
            value=path,
            suggestion=suggestion,
        )
    if len(parts) != 1:
        return
    if operation == "Move":
        detail = (
            "Moving one would remove the shared folder itself from DSM, beyond the recycle "
            "bin's reach (tested on DSM 7.1 to 7.4.1, as a non-admin with write access)."
        )
        suggestion = (
            "To move what's in it, list it with list_files and move its items by their own "
            "paths (leaving out its #recycle); to keep it and copy it, use copy_files; to "
            "move or rename the shared folder itself, use DSM's Control Panel > Shared Folder."
        )
    else:
        detail = (
            "Renaming one through File Station makes it disappear from DSM's shared folders, "
            "under its new name too (tested on DSM 7.1 to 7.4.1)."
        )
        suggestion = "Rename a shared folder in DSM's Control Panel > Shared Folder."
    error_response(
        ErrorCode.INVALID_PARAMETER,
        f"{head} a whole shared folder. {detail} Nothing was {past}.",
        retryable=False,
        param=param,
        value=path,
        suggestion=suggestion,
    )


def _unstop(message: str) -> str:
    """``message`` without its final period, and only that one: a path can end in dots."""
    return message[:-1] if message.endswith(".") else message


# Advice that already says to check with list_files (a start with no answer, a path not
# found when the delete ran),
# not advice that only mentions it (408's "Use list_files or search_files to find …").
_CHECKS_WITH_LIST_FILES = re.compile(r"\bcheck\b[^.]*\bwith list_files\b", re.IGNORECASE)


def _names_path(message: str, path: str) -> bool:
    """Whether ``message`` names ``path`` itself: after the start, a space, ":" or "(",
    and followed by the end, a sentence's period, or " (" (as in
    ``/video/b (DSM error 900)``); not a path ending in it (``/video/b/x/video/b``)."""
    pattern = r"(?<![^\s:(])" + re.escape(path) + r"(?=$|\.$|\.\s|\s\()"
    return re.search(pattern, message) is not None


def _with_progress_so_far(
    error: ToolError, failed: str, done: list[str], rest: list[str]
) -> ToolError:
    """A delete's error, saying which path failed, what was already deleted and what
    wasn't started, so the user can tell what's left after a failure partway (QA round 1
    on #480)."""
    if not done and not rest:
        return error
    try:
        body = json.loads(str(error))
        message = body["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return error
    # A DSM error on a start or a status request names no path (QA round 5 on #480); one
    # that names a path inside the item, or a sibling starting with the same characters
    # (/video/a.txt, /video/a b), doesn't name the item either.
    if not _names_path(message, failed):
        message = _unstop(message) + f". Failed on: {failed}."
    body["error"]["param"] = "paths"
    body["error"]["value"] = failed
    # The delete loop is the one place that adds these: _delete_result doesn't (pre-QA on
    # #480; a check for the words in the message missed a path containing them).
    if done:
        message = _unstop(message) + _already_done("Delete", done) + "."
        # The same call again is refused (the deleted paths are missing): only the items not
        # deleted can be retried (QA round 2 on #480).
        body["error"]["retryable"] = False
        # Added to the cause's own advice (free space, permissions, check before deleting
        # again), never in place of it (QA round 3 on #480).
        # A note that reads after any advice, or alone (QA round 4): no "Then", no second
        # "retry", and a check first, since the failed path may be gone already (a 599),
        # unless the advice already says to check with list_files (QA round 5).
        original = str(body["error"].get("suggestion") or "").strip()
        if _CHECKS_WITH_LIST_FILES.search(original):
            retry = (
                "When retrying, leave out the paths already deleted (the same call would be "
                "refused)."
            )
        else:
            retry = (
                "When retrying, leave out the paths already deleted (the same call would be "
                f"refused), and check {failed} with list_files first."
            )
        if not original:
            body["error"]["suggestion"] = retry
        elif original[-1] in ".!?)":
            body["error"]["suggestion"] = f"{original} {retry}"
        else:
            # Advice that ends in a list (418's characters to avoid) goes last, so no
            # punctuation is read as part of it.
            body["error"]["suggestion"] = f"{retry} {original}"
    if rest:
        message = _unstop(message) + f". Not started: {', '.join(rest)}."
    body["error"]["message"] = message
    return ToolError(json.dumps(body))


def _already_done(operation: str, done: list[str] | None) -> str:
    """Name the items a multi-path operation finished before a later one failed."""
    if not done:
        return ""
    return f". Already {_PAST_TENSE.get(operation, 'processed')} before this failure: " + ", ".join(
        done
    )


async def _refuse_missing_sources(
    client: DsmClient, operation: str, checked: list[str], original: list[str]
) -> None:
    """Refuse the whole call before anything moves if any source is missing (#162 policy)."""
    try:
        missing = await find_missing_paths(client, checked)
    except SynologyError as e:
        synology_error_response(f"{operation} files", e)
    if missing:
        error_response(
            ErrorCode.NOT_FOUND,
            f"{operation} files refused: not found: "
            + "; ".join(f"{p} (DSM error {code})" for p, code in missing)
            + f". Nothing was {_PAST_TENSE.get(operation, 'processed')}.",
            retryable=False,
            param="paths",
            value=original,
            suggestion=missing_paths_suggestion(missing),
        )


async def _refuse_missing_destination(client: DsmClient, operation: str, dest: str) -> None:
    """Refuse before anything starts if the destination folder is missing or refused (#344).

    DSM answers a copy or move into a missing folder with 1002, "Destination error during
    copy/move", whose suggestion points at permissions. getinfo tells the cases apart: 408
    is `not_found`, 407 `permission_denied`. Any other answer, or no answer, leaves it to
    the task, as before.
    """
    try:
        found = await find_missing_paths(client, [dest])
    except SynologyError as e:
        logger.debug(
            "%s: destination check for %s failed (%s); starting anyway", operation, dest, e
        )
        return
    codes = {code for _, code in found}
    nothing = f"Nothing was {_PAST_TENSE.get(operation, 'processed')}."
    if 408 in codes:
        error_response(
            ErrorCode.NOT_FOUND,
            f"{operation} files refused: the destination folder {dest} doesn't exist. {nothing}",
            retryable=False,
            param="dest_folder",
            value=dest,
            suggestion=(
                "Create it first with create_folder, or check the path with list_files, then retry."
            ),
        )
    if 407 in codes:
        error_response(
            ErrorCode.PERMISSION_DENIED,
            f"{operation} files refused: no permission to use the destination folder {dest}."
            f" {nothing}",
            retryable=False,
            param="dest_folder",
            value=dest,
            suggestion=(
                "Pick a folder this DSM account can write to, or have its permissions "
                "changed in DSM."
            ),
        )


async def _copy_move_one_path(
    client: DsmClient,
    path: str,
    *,
    dest: str,
    overwrite: bool,
    remove_src: bool,
    operation: str,
    copymove_version: int,
    timeout: float,
    done: list[str] | None = None,
    on_progress: ProgressCallback | None = None,
) -> tuple[int, bool] | Handoff:
    """Copy or move a single path via DSM async-task pattern.

    Returns the `processed_size` byte count from the completed task (and whether DSM
    skipped items that already existed at the destination, #171) so the
    caller can sum sizes across all per-path tasks, or a Handoff when the task outlived
    ``timeout`` or DSM stopped answering its status requests (#245). Raises ToolError on
    failure (via error_response/synology_error_response). A task that DSM reports an
    error for, or whose call is canceled, is stopped, so orphaned tasks can't
    accumulate on the NAS.
    """
    try:
        start_data = await client.request(
            "SYNO.FileStation.CopyMove",
            "start",
            version=copymove_version,
            params={
                "path": path,
                "dest_folder_path": dest,
                "overwrite": str(overwrite).lower(),
                "remove_src": str(remove_src).lower(),
            },
        )
    except SynologyError as e:
        synology_error_response(f"{operation} files", e)

    taskid = start_data.get("taskid", "")

    result = await poll_task(
        client,
        api="SYNO.FileStation.CopyMove",
        version=copymove_version,
        taskid=taskid,
        wait=timeout,
        label=f"{_PRESENT[operation]} {path}",
        on_progress=on_progress,
    )

    if result.outcome == "poll_error":
        assert isinstance(result.error, SynologyError)
        synology_error_response(f"{operation} files", result.error)
    if result.outcome in ("handoff", "stalled"):
        # Left running on the NAS, not stopped (#245): the caller gets a handle.
        stall = result.error if result.outcome == "stalled" else None
        return Handoff(Handle(_KIND[operation], taskid), result.status, stall)
    return _copy_move_result(result.status, path, operation, done, dest=dest)


def _code_text(code: object) -> str:
    """``DSM error 415: Disk quota exceeded``, or just the code when it isn't known.

    Takes whatever a status holds: a code that isn't a number is shown as it is.
    """
    try:
        known = FILESTATION_ERROR_CODES.get(int(code))  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):  # None, "x", inf
        known = None
    return f"DSM error {code}: {known[0]}" if known else f"DSM error {code}"


def _partial_note(
    status: dict[str, Any],
    failed: Sequence[tuple[str, object]],
    operation: str,
    source: str | None,
    dest: str | None,
) -> str:
    """Say where a copy, move or restore that failed part-way may have left part of it.

    DSM doesn't report how much it wrote before failing: a copy stopped by a quota left
    18 GiB at the destination while its status said `processed_size: 0` (seska, DSM 7.1.1).
    Left out only when the source itself was gone (408 on that path), since nothing can
    have been written then; any other failure, a 408 deeper in a folder included, may
    follow partial writes.
    """
    dest = status.get("dest_folder_path") or dest
    if not dest or (source and all(c == 408 and p == source for p, c in failed)):
        return ""
    past = _PAST_TENSE.get(operation, "processed")
    gone = (
        ", and some of the source items may already be gone"
        if operation in ("Move", "Restore")
        else ""
    )
    return (
        f". Part of it may already be in {dest}{gone}: DSM doesn't say how much was "
        f"{past} before it stopped, so check there before trying again"
    )


def _copy_move_result(
    status: dict[str, Any],
    path: str,
    operation: str,
    done: list[str] | None = None,
    *,
    dest: str | None = None,
) -> tuple[int, bool]:
    """``(processed bytes, skipped)`` from a finished CopyMove status; ToolError on failure."""
    if "error" in status:
        err = status["error"]
        err_code = err.get("code", 0) if isinstance(err, dict) else err
        err_path = status.get("path") or path
        mapped = error_from_code(err_code, "SYNO.FileStation.CopyMove")
        error_response(
            mapped.error_code,
            f"{operation} files failed: {_code_text(err_code)} on path: {err_path}"
            + _partial_note(status, [(err_path, err_code)], operation, path, dest)
            + _already_done(operation, done),
            retryable=mapped.retryable,
            suggestion=(
                mapped.suggestion
                or "Check that source paths exist and you have permission to access them."
            ),
        )

    errors = status.get("errors")
    if isinstance(errors, list) and errors:
        # A missing source finishes without an `error` key: DSM lists it under `errors`
        # instead ({"code": 408, "path": ...}; DSM 7.1 and 7.2.2). Same class as #162.
        failed = [
            (str(e.get("path") or path), int(e.get("code", 0)))
            for e in errors
            if isinstance(e, dict)
        ]
        mapped = error_from_code(failed[0][1] if failed else 0, "SYNO.FileStation.CopyMove")
        error_response(
            mapped.error_code,
            f"{operation} files failed: "
            + "; ".join(f"{p} ({_code_text(code)})" for p, code in failed)
            + _partial_note(status, failed, operation, path, dest)
            + _already_done(operation, done),
            retryable=mapped.retryable,
            param="paths",
            value=path,
            suggestion=(
                mapped.suggestion
                or "Check that source paths exist and you have permission to access them."
            ),
        )

    size = status.get("processed_size", 0)
    # With overwrite=false DSM skips items that already exist at the destination and
    # says so only here: {"status": "none"} = nothing skipped, {"status": "all"} = at
    # least one item skipped (a whole file, or some of a folder's children); null with
    # overwrite=true. Same on DSM 7.1 and 7.2.2 (#171).
    skip = status.get("skipstatus")
    skip_value = skip.get("status") if isinstance(skip, dict) else skip
    if skip is not None:
        logger.debug("CopyMove skipstatus for %s: %r", path, skip)
    # Anything but "none"/absent counts as skipped: the safe direction for an unseen shape.
    skipped = skip_value not in (None, "", "none")
    return (int(size) if isinstance(size, int | float) else 0), skipped


async def _skip_report(
    client: DsmClient,
    verb: str,
    dest: str,
    normalized: list[str],
    ok: list[str],
    skipped_paths: list[str],
    remove_src: bool,
) -> list[str]:
    """Lines for a copy/move/restore result where DSM skipped something (#171, #189).

    DSM's ``skipstatus`` says only that something was skipped for a source: all of a file,
    or some of a folder's children (the rest still went through). So each skipped source's
    type is looked up: a file was skipped, a folder had items skipped. A source whose type
    can't be read keeps the hedged "in whole or in part" wording.
    """
    try:
        kinds = await paths_isdir(client, skipped_paths)
    except SynologyError as e:
        # The copy already happened; report it with the hedged wording, never as an error.
        logger.warning("Couldn't read the type of skipped sources (DSM %s): %s", e.code, e)
        kinds = {}
    files = [p for p in skipped_paths if kinds.get(p) is False]
    folders = [p for p in skipped_paths if kinds.get(p) is True]
    unknown = [p for p in skipped_paths if p not in kinds]
    done = verb.lower()

    parts = []
    if files:
        parts.append(f"{len(files):,} skipped")
    if folders:
        parts.append(f"{len(folders):,} folder{'s' if len(folders) > 1 else ''} with items skipped")
    if unknown:
        parts.append(f"{len(unknown):,} skipped in whole or in part")
    # "In full" only when a folder may have been partly done: "0 of 1" alone would claim
    # nothing went through (QA R1 on #175).
    in_full = " in full" if folders or unknown else ""
    lines = [
        format_status(
            f"{verb} {len(ok):,} of {len(normalized):,} item(s){in_full} to {dest}/; "
            f"{', '.join(parts)} (already at the destination, overwrite=false):",
            success=False,
        )
    ]

    def entry(p: str, folder: bool = False) -> str:
        # A skipped folder's leftovers are its own children; a file stays in its parent.
        src_dir = p if folder else "/".join(p.split("/")[:-1]) or ""
        return f"  {p}" + (f" (skipped items are still in {src_dir}/)" if remove_src else "")

    if ok:
        lines.append(f"{verb}:")
        lines.extend(f"  {p.split('/')[-1]}" for p in ok)
    if files:
        lines.append("Skipped:")
        lines.extend(entry(p) for p in files)
    if folders:
        # DSM doesn't say whether anything in the folder was left to copy, so don't claim it was.
        lines.append(
            f"Folders with items skipped (anything not already at the destination was {done}):"
        )
        lines.extend(entry(p, folder=True) for p in folders)
    if unknown:
        lines.append("Skipped in whole or in part:")
        lines.extend(entry(p) for p in unknown)
        lines.append(
            f"For a skipped folder, the parts not already at the destination were still {done}."
        )
    lines.append("Pass overwrite=true to replace them.")
    return lines


async def _copy_move(
    client: DsmClient,
    *,
    paths: list[str],
    dest_folder: str,
    overwrite: bool,
    remove_src: bool,
    operation: str,
    timeout: float = 120.0,
    check_sources: bool = True,
    already_done: list[str] | None = None,
    later: list[str] | None = None,
    records: TaskRecords | None = None,
    on_progress: ProgressCallback | None = None,
) -> tuple[str, bool]:
    """Shared implementation for copy and move operations.

    Closes #84 (the `move_files` half — `copy_files` and the multi-path
    `restore_from_recycle_bin` flow share this code path). DSM 7.x's
    `SYNO.FileStation.CopyMove start` does not honor the documented
    comma-joined multi-path format on v2 — a request with
    `path=/a,/b` is treated as a single literal path and silently no-ops
    while the task reports success. Same root-cause family as #68
    (delete + getinfo); fix matches that pattern: one DSM CopyMove task
    per input path. Per-path serial means N round-trips for N paths,
    which is fine for typical small-N usage and trivially correct.

    `escape_multi_path` is no longer called here — the per-path loop
    sends each path raw, sidestepping the comma-join entirely.

    Returns ``(text, handed_off)``. When a path's task outlives ``timeout`` it is
    handed off (#245): the text is the handoff reply, and the remaining paths (plus
    ``later``, the caller's own not-yet-started items) are listed as not started.
    """
    if not paths:
        error_response(
            ErrorCode.NOT_FOUND,
            f"{operation} files failed: No paths provided.",
            retryable=False,
            param="paths",
            value=paths,
            suggestion="Pass at least one path.",
        )

    normalized = _collapse_overlaps([normalize_path(p) for p in paths], drop_nested=remove_src)
    # One progress token per tool call: keep its values increasing across paths (#245).
    on_progress = MonotonicProgress.wrap(on_progress)
    dest = normalize_path(dest_folder)

    # Pin to version 2 — v3 uses JSON request format with different
    # parameter encoding that our path-as-form-param shape doesn't support.
    copymove_version = min(2, client.negotiate_version("SYNO.FileStation.CopyMove", max_version=2))

    if check_sources:
        await _refuse_missing_sources(client, operation, normalized, paths)
    await _refuse_missing_destination(client, operation, dest)

    total_processed = 0
    done = list(already_done or [])
    skipped_paths: list[str] = []
    for i, p in enumerate(normalized):
        # A copy of the same source to another destination is a different operation; a
        # move (or restore) of a source that is already moving anywhere is not.
        match: dict[str, Any] = {"remove_src": remove_src}
        if not remove_src:
            match["dest_folder_path"] = dest
        running = await running_task(client, api="SYNO.FileStation.CopyMove", path=p, params=match)
        outcome: tuple[int, bool] | Handoff
        task_dest, task_overwrite = dest, overwrite
        if running is not None:
            started = running.get("params") or {}
            if isinstance(started.get("dest_folder_path"), str):
                task_dest = started["dest_folder_path"]
            if isinstance(started.get("overwrite"), bool):
                task_overwrite = started["overwrite"]
            # Never start a second task on a path that already has one (#245).
            outcome = Handoff(Handle(_KIND[operation], str(running["taskid"])), running)
        else:
            outcome = await _copy_move_one_path(
                client,
                p,
                dest=dest,
                overwrite=overwrite,
                remove_src=remove_src,
                operation=operation,
                copymove_version=copymove_version,
                timeout=timeout,
                done=done,
                on_progress=on_progress,
            )
        if isinstance(outcome, Handoff):
            if records is not None:
                records.add(
                    TaskRecord(
                        handle=outcome.handle, paths=[p], dest=task_dest, overwrite=task_overwrite
                    )
                )
            reply = handoff_reply(
                verb=f"Still {_PRESENT[operation].lower()}",
                path=p,
                handle=outcome.handle,
                status=outcome.status,
                stall=outcome.stall,
                done=done,
                not_started=normalized[i + 1 :] + list(later or []),
            )
            if running is not None:
                other = task_dest if task_dest != dest else None
                reply = _already_running(operation, p, other) + "\n" + reply
            return reply, True
        size, skipped = outcome
        total_processed += size
        if skipped:
            skipped_paths.append(p)
        done.append(p)

    text = await _copy_move_summary(
        client, normalized, dest, remove_src, total_processed, skipped_paths
    )
    return text, False


async def _copy_move_summary(
    client: DsmClient,
    normalized: list[str],
    dest: str,
    remove_src: bool,
    total_processed: int,
    skipped_paths: list[str],
) -> str:
    """The result text of a finished copy or move (a restore rewords it, ``_as_restore``)."""
    verb = "Copied" if not remove_src else "Moved"
    ok = [p for p in normalized if p not in skipped_paths]

    def name(p: str) -> str:
        return p.split("/")[-1]

    if not skipped_paths:
        lines = [format_status(f"{verb} {len(normalized):,} item(s) to {dest}/:")]
        lines.extend(f"  {name(p)}" for p in normalized)
    else:
        lines = await _skip_report(client, verb, dest, normalized, ok, skipped_paths, remove_src)

    if total_processed > 0:
        lines.append(f"\nTotal size: {format_size(total_processed)}")

    if remove_src and not skipped_paths:
        src_dirs = sorted({"/".join(p.split("/")[:-1]) for p in normalized})
        lines.extend(f"\nSource files have been removed from {d}/." for d in src_dirs)
    elif remove_src:
        by_dir: dict[str, list[str]] = {}
        for p in ok:
            by_dir.setdefault("/".join(p.split("/")[:-1]), []).append(name(p))
        lines.extend(
            f"\nMoved sources removed from {d}/: {', '.join(names)}"
            for d, names in sorted(by_dir.items())
        )

    return "\n".join(lines)


async def _delete_one_path(
    client: DsmClient,
    path: str,
    *,
    recursive: bool,
    delete_version: int,
    timeout: float,
    on_progress: ProgressCallback | None = None,
) -> Handoff | None:
    """Delete a single path via DSM async-task pattern.

    Raises ToolError (via error_response/synology_error_response) on failure;
    returns None when the delete finished, or a Handoff when the task outlived
    ``timeout`` or DSM stopped answering its status requests (#245). A task that DSM
    reports an error for, or whose call is canceled, is stopped.
    """
    try:
        start_data = await client.request(
            "SYNO.FileStation.Delete",
            "start",
            version=delete_version,
            params={
                "path": path,
                "recursive": str(recursive).lower(),
                "accurate_progress": "true",
            },
        )
    except SynologyError as e:
        synology_error_response("Delete files", e)

    taskid = start_data.get("taskid", "")

    result = await poll_task(
        client,
        api="SYNO.FileStation.Delete",
        version=delete_version,
        taskid=taskid,
        wait=timeout,
        label=f"Deleting {path}",
        on_progress=on_progress,
    )
    status = result.status

    if result.outcome == "poll_error":
        assert isinstance(result.error, SynologyError)
        synology_error_response("Delete files", result.error)
    if result.outcome in ("handoff", "stalled"):
        # Left running on the NAS, not stopped (#245): the caller gets a handle.
        stall = result.error if result.outcome == "stalled" else None
        return Handoff(Handle("delete", taskid), status, stall)

    _delete_result(status, path, recursive=recursive)
    return None


def _delete_result(status: dict[str, Any], path: str, *, recursive: bool) -> None:
    """Raise ToolError if a finished Delete status reports a failure; else return."""
    if "error" in status:
        err = status["error"]
        err_code = err.get("code", 0) if isinstance(err, dict) else err
        # An empty path (as in DSM's nothing-found status) names nothing: use ours (pre-QA).
        err_path = status.get("path") or path
        mapped = error_from_code(err_code, "SYNO.FileStation.Delete")
        error_response(
            mapped.error_code,
            f"Delete files failed: DSM error code {err_code} on path: {err_path}.",
            retryable=mapped.retryable,
            suggestion=(
                mapped.suggestion
                or "Check that paths exist and you have permission to delete them."
            ),
        )

    errors = status.get("errors")
    if isinstance(errors, list) and errors:
        # Like CopyMove, a Delete task can finish without an `error` key and list per-item
        # failures under `errors` instead, e.g. 900 for a non-recursive delete of a
        # non-empty folder, which stays in place (QA R1 on #173).
        failed = [
            (str(e.get("path") or path), int(e.get("code", 0)))
            for e in errors
            if isinstance(e, dict)
        ]
        mapped = error_from_code(failed[0][1] if failed else 0, "SYNO.FileStation.Delete")
        suggestion = mapped.suggestion or "Check that paths exist and you have permission."
        if not recursive:
            suggestion = (
                "A folder that isn't empty can't be deleted with recursive=false; "
                "pass recursive=true to delete it with its contents. " + suggestion
            )
        error_response(
            mapped.error_code,
            "Delete files failed: "
            + "; ".join(f"{p} (DSM error {code})" for p, code in failed)
            + ".",
            retryable=mapped.retryable,
            param="paths",
            value=path,
            suggestion=suggestion,
        )

    if status.get("total") == -1:
        # DSM's "nothing found" signature: finished, total -1, empty path (#162). The
        # pre-check saw the path, so it disappeared before the task ran.
        error_response(
            ErrorCode.NOT_FOUND,
            # The path ends the sentence, so a partial failure's "Failed on" sees it named
            # (QA round 6 on #480).
            "Delete files failed: the path wasn't found when the delete ran, so nothing was "
            f"deleted for {path}.",
            retryable=False,
            param="paths",
            value=path,
            suggestion=missing_paths_suggestion([(path, 408)]),
        )


async def delete_files(
    client: DsmClient,
    *,
    paths: list[str],
    recursive: bool = True,
    recycle_bin_status: dict[str, bool] | None = None,
    timeout: float = 120.0,
    records: TaskRecords | None = None,
    on_progress: ProgressCallback | None = None,
) -> str:
    """Delete files or folders.

    Closes #68 (the `delete_files` half). DSM 7.x's
    `SYNO.FileStation.Delete start` does not honor the documented
    comma-joined multi-path format on v2 — the user reported that
    `delete_files(paths=[a, b, ..., l])` returns success but no paths
    are actually deleted, while single-path calls work. The verified
    parallel symptom on `get_file_info` (vdsm CI proves a v2-style
    `path=/a,/b` returns one synthetic record with the literal
    comma-joined string as its `path`) makes the same root cause near
    certain on Delete. Fix matches the user's documented workaround:
    one DSM Delete task per input path. Per-path serial means N
    round-trips for N paths, which is fine for typical small-N usage
    and trivially correct.
    """
    if not paths:
        error_response(
            ErrorCode.NOT_FOUND,
            "Delete files failed: No paths provided.",
            retryable=False,
            param="paths",
            value=paths,
            suggestion="Pass at least one path.",
        )

    # Here too, not only in the tool: a caller that skips the tool mustn't delete a shared
    # folder (pre-QA on #480).
    refuse_share_roots(paths)
    normalized = _collapse_overlaps([normalize_path(p) for p in paths])
    # One progress token per tool call: keep its values increasing across paths (#245).
    on_progress = MonotonicProgress.wrap(on_progress)

    # Refuse the whole call if any path is missing (#162): DSM "deletes" a missing path
    # without an error, and a partial delete of a mistyped list is worse than none.
    try:
        missing = await find_missing_paths(client, normalized)
    except SynologyError as e:
        synology_error_response("Delete files", e)
    if missing:
        error_response(
            ErrorCode.NOT_FOUND,
            "Delete files refused: not found: "
            + "; ".join(f"{p} (DSM error {code})" for p, code in missing)
            + ". Nothing was deleted.",
            retryable=False,
            param="paths",
            value=paths,
            suggestion=missing_paths_suggestion(missing),
        )

    # Pin to v2 — v3 uses JSON request format with different parameter encoding.
    delete_version = min(2, client.negotiate_version("SYNO.FileStation.Delete", max_version=2))

    deleted: list[str] = []
    for i, p in enumerate(normalized):
        running = await running_task(client, api="SYNO.FileStation.Delete", path=p)
        handoff: Handoff | None
        if running is not None:
            # Never start a second delete of a path that already has one running (#245).
            handoff = Handoff(Handle("delete", str(running["taskid"])), running)
        else:
            try:
                handoff = await _delete_one_path(
                    client,
                    p,
                    recursive=recursive,
                    delete_version=delete_version,
                    timeout=timeout,
                    on_progress=on_progress,
                )
            except ToolError as e:
                raise _with_progress_so_far(e, p, deleted, normalized[i + 1 :]) from None
            except httpx.TransportError as e:
                if not deleted and not normalized[i + 1 :]:
                    raise  # nothing to add: the server's usual timeout or connection error
                # The start got no answer (a status with none hands the item off in
                # poll_task): it may have started. Say so, with what was done and what
                # wasn't started (QA round 1 on #480).
                try:
                    error_response(
                        ErrorCode.TIMEOUT
                        if isinstance(e, httpx.TimeoutException)
                        else ErrorCode.UNAVAILABLE,
                        f"Delete files failed: DSM didn't answer for {p} "
                        f"({describe_transport_error(e)}); its delete may have started.",
                        retryable=False,
                        suggestion="Check with list_files, or file_task with no handle, "
                        "before deleting again.",
                    )
                except ToolError as err:
                    raise _with_progress_so_far(err, p, deleted, normalized[i + 1 :]) from None
        if handoff is not None:
            handle = handoff.handle
            # No DSM request between the handoff and the reply: a recycle-bin probe to a
            # DSM stalled by this very delete would lose the handle (#245). Keep only a
            # cached answer; the final result probes lazily (_delete_summary).
            recycle: dict[str, bool] = {}
            parts = p.split("/")
            share_name = parts[1] if len(parts) > 1 else ""
            in_bin = _bin_index(parts[1:]) is not None
            if recycle_bin_status is not None and not in_bin and share_name in recycle_bin_status:
                recycle[share_name] = recycle_bin_status[share_name]
            if records is not None:
                records.add(
                    TaskRecord(handle=handle, paths=[p], recursive=recursive, recycle=recycle)
                )
            reply = handoff_reply(
                verb="Still deleting",
                path=p,
                handle=handle,
                status=handoff.status,
                stall=handoff.stall,
                done=deleted,
                not_started=normalized[i + 1 :],
            )
            return reply if running is None else _already_running("Delete", p) + "\n" + reply
        deleted.append(p)

    return await _delete_summary(client, normalized, recycle_bin_status)


async def _delete_summary(
    client: DsmClient,
    normalized: list[str],
    recycle_bin_status: dict[str, bool] | None,
) -> str:
    """The result text of a finished delete_files call."""
    # A path inside a bin (/<share>/#recycle/…, or a user's /homes/<user>/#recycle/…) is
    # gone for good whatever the share's setting: DSM keeps no second copy (#220). Keyed by
    # the bin's folder below the root: "video", or "homes/alice".
    in_bin: dict[str, list[str]] = {}
    outside_bin: list[str] = []
    for p in normalized:
        parts = p.split("/")[1:]
        at = _bin_index(parts)
        if at is not None:
            in_bin.setdefault("/".join(parts[:at]), []).append(p)
        else:
            outside_bin.append(p)
    bin_shares = {key.split("/")[0] for key in in_bin}

    # Determine recycle bin status per share. Probes lazily on first observation
    # via `ensure_recycle_status` so the dict stays empty until we actually need
    # the answer; pre-#37 the dict was never populated and every share was
    # incorrectly reported as recycle-on. The dict is shared across all tool
    # handlers (closure-captured in modules/filestation/__init__.py) and cleared
    # on session re-auth (see `_register_recycle_status_invalidator` in __init__).
    shares_with_recycle: set[str] = set()
    shares_without_recycle: set[str] = set()
    shares_unknown: set[str] = set()
    if recycle_bin_status is None:
        # Defensive: caller passed nothing. Skip probing; preserve prior
        # default-true messaging for the touched shares.
        for p in outside_bin:
            parts = p.split("/")
            share_name = parts[1] if len(parts) > 1 else ""
            shares_with_recycle.add(share_name)
    else:
        seen: set[str] = set()
        for p in outside_bin:
            parts = p.split("/")
            share_name = parts[1] if len(parts) > 1 else ""
            if share_name in seen:
                continue
            seen.add(share_name)
            try:
                enabled = await ensure_recycle_status(client, share_name, recycle_bin_status)
            except httpx.TransportError as e:
                # DSM didn't answer (e.g. right after finishing a huge delete): the
                # delete is done, so report it without the recycle-bin line (#245).
                logger.debug("Recycle-bin probe on /%s unanswered: %r", share_name, e)
                shares_unknown.add(share_name)
                continue
            if enabled:
                shares_with_recycle.add(share_name)
            else:
                shares_without_recycle.add(share_name)

    has_recycle = bool(shares_with_recycle or shares_unknown)
    verb = "Deleted" if has_recycle else "Permanently deleted"

    lines = [format_status(f"{verb} {len(normalized):,} item(s):")]
    lines.extend(f"  {p}" for p in normalized)

    # DSM keeps no empty folder in the bin, not even one deleted on its own (seska DSM
    # 7.1.1; vdsm, all four versions, for one inside a deleted folder), so the note doesn't
    # promise one back (#479).
    lines.extend(
        f"\nRecycle bin is enabled on /{share} \u2014 "
        f"files can be recovered with list_recycle_bin and restore_from_recycle_bin"
        + (
            ", except the items deleted from the recycle bin itself."
            if share in bin_shares
            else "."
        )
        + " Empty folders aren't kept in the bin."
        for share in sorted(shares_with_recycle)
    )
    for share, bin_paths in sorted(in_bin.items()):
        # Name the bin items only when the call also deleted others, so it's clear
        # which ones are gone for good.
        if outside_bin:
            lines.append(
                f"\n\u26a0 Deleted from the recycle bin on /{share} \u2014 permanent, "
                "these cannot be recovered:"
            )
            lines.extend(f"  {p}" for p in bin_paths)
        else:
            lines.append(
                f"\n\u26a0 Deleted from the recycle bin on /{share} \u2014 permanent, "
                "these cannot be recovered."
            )
    lines.extend(
        f"\n\u26a0 Recycle bin is NOT enabled on /{share} \u2014 these files cannot be recovered."
        for share in sorted(shares_without_recycle)
    )
    lines.extend(
        f"\nCouldn't check whether /{share} has its recycle bin enabled (DSM didn't answer); "
        "list_recycle_bin shows whether these files can be recovered."
        for share in sorted(shares_unknown)
    )

    return "\n".join(lines)


def _as_restore(text: str, share_name: str) -> str:
    """Word a move result from the recycle bin as a restore."""
    prefix = f"Restored from /{share_name} recycle bin:"
    return (
        text.replace("[+] Moved", f"[+] {prefix}")
        .replace("[!] Moved", f"[!] {prefix}")
        .replace("\nMoved:\n", "\nRestored:\n")
        .replace("\nMoved sources removed from", "\nRestored items removed from")
        .replace("were still moved.", "were still restored.")
        .replace("at the destination was moved):", "at the destination was restored):")
    )


async def restore_from_recycle_bin(
    client: DsmClient,
    *,
    share: str,
    paths: list[str],
    dest_folder: str | None = None,
    overwrite: bool = False,
    timeout: float = 120.0,
    records: TaskRecords | None = None,
    on_progress: ProgressCallback | None = None,
) -> str:
    """Restore files from a shared folder's recycle bin.

    A restore that outlives ``timeout`` is handed off (#245); with several
    destinations, the items after the handed-off one are listed as not started.
    """
    share_name = share.strip("/").split("/")[0]
    # One progress token per tool call, shared by every destination's task (#245).
    on_progress = MonotonicProgress.wrap(on_progress)

    # Normalize recycle bin paths
    full_paths: list[str] = []
    restore_destinations: list[str] = []

    def in_bin(rel: str) -> bool:
        # `#recycle` itself or a path inside it; not `#recycled/…`, a folder beside it.
        return rel == "#recycle" or rel.startswith("#recycle/")

    bin_root = f"/{share_name}/#recycle"
    for item in paths:
        p = item.strip()
        # Ensure the path is within #recycle. A full path may spell the share in another case;
        # it's re-rooted on the share as given, which the handler has set to the listed
        # spelling (#353).
        head, _, rest = p.lstrip("/").partition("/")
        if head.casefold() == share_name.casefold() and in_bin(rest):
            full_paths.append(normalize_path(f"/{share_name}/{rest}"))
        elif in_bin(p):
            full_paths.append(normalize_path(f"/{share_name}/{p}"))
        else:
            full_paths.append(normalize_path(f"/{share_name}/#recycle/{p.lstrip('/')}"))
        if any(part and not part.strip() for part in full_paths[-1].split("/")):
            # `_copy_move` normalizes the path again, which strips a whitespace-only last
            # part: `#recycle/ /` would be checked as `/video/#recycle/ ` and sent as the
            # bin itself, `Shows/ /` as the folder `Shows` (pre-QA; the same stripping is
            # #488). Refused, as delete_files refuses one.
            error_response(
                ErrorCode.INVALID_PARAMETER,
                f"Restore refused: whitespace-only parts aren't allowed ({item!r}). Nothing "
                "was restored.",
                retryable=False,
                param="paths",
                value=item,
                suggestion="Give each item as list_recycle_bin shows it, e.g. Shows/Ep1.mkv. "
                "An item whose name is only whitespace can't be reached this way: restore it "
                "in DSM's File Station.",
            )
        if full_paths[-1] == bin_root:
            # An empty item ("", "/", "#recycle") names the bin itself: restoring it would
            # move the whole bin out (#479).
            error_response(
                ErrorCode.INVALID_PARAMETER,
                f"Restore refused: {item!r} names the whole recycle bin of /{share_name}, "
                "not an item in it. Nothing was restored.",
                retryable=False,
                param="paths",
                value=item,
                suggestion="Give each item as list_recycle_bin shows it, e.g. Shows/Ep1.mkv.",
            )

        # Infer original path by stripping #recycle
        if dest_folder:
            restore_destinations.append(dest_folder)
        else:
            # /video/#recycle/Shows/Ep1.mkv -> /video/Shows/
            original = full_paths[-1].replace(f"/{share_name}/#recycle", f"/{share_name}")
            restore_destinations.append("/".join(original.split("/")[:-1]) or f"/{share_name}")

    await _refuse_missing_sources(client, "Restore", full_paths, paths)
    recreated = [] if dest_folder else await _recreate_missing_folders(client, restore_destinations)
    note = (
        f"\nRe-created {', '.join(recreated)} (no longer there) to restore into."
        if recreated
        else ""
    )

    # Use the first destination (or provided dest_folder)
    actual_dest = dest_folder or restore_destinations[0]
    if dest_folder is None and len(set(restore_destinations)) > 1:
        # Multiple different destinations — move each one separately
        results: list[str] = []
        restored: list[str] = []
        for i, (fp, dest) in enumerate(zip(full_paths, restore_destinations, strict=False)):
            r, handed_off = await _copy_move(
                client,
                paths=[fp],
                dest_folder=dest,
                overwrite=overwrite,
                remove_src=True,
                operation="Restore",
                timeout=timeout,
                check_sources=False,
                already_done=restored,
                later=full_paths[i + 1 :],
                records=records,
                on_progress=on_progress,
            )
            if handed_off:
                results.append(r)
                return "\n".join(results) + note
            results.append(_as_restore(r, share_name))
            restored.append(fp)
        partial = [r for r in results if r.startswith("[!]")]
        if partial:
            results.insert(
                0,
                format_status(
                    f"Restored {len(results) - len(partial):,} of {len(results):,} item(s) from "
                    f"/{share_name} recycle bin; {len(partial):,} with skips "
                    "(already at the destination, overwrite=false; details below):",
                    success=False,
                ),
            )
        return "\n".join(results) + note

    result, handed_off = await _copy_move(
        client,
        paths=full_paths,
        dest_folder=actual_dest,
        overwrite=overwrite,
        remove_src=True,
        operation="Restore",
        timeout=timeout,
        check_sources=False,
        records=records,
        on_progress=on_progress,
    )
    if handed_off:
        return result + note

    # Rephrase from "Moved" to "Restored"
    return _as_restore(result, share_name) + note


async def _recreate_missing_folders(client: DsmClient, folders: list[str]) -> list[str]:
    """Create the inferred restore destinations that no longer exist; return them.

    A file restored out of a folder that was deleted with it has no original folder to
    go back to, and DSM refuses that move with 1002 "Destination error" (DSM 7.1 and
    7.3.2, #333). The spec always said a missing parent is re-created; this does it,
    deepest first, since ``force_parent`` creates the folders above it.
    """
    try:
        missing = [
            path
            for path, code in await find_missing_paths(client, sorted(set(folders)))
            if code == 408
        ]
    except SynologyError as e:
        synology_error_response("Restore files", e)
    created: list[str] = []
    for path in sorted(missing, key=lambda p: p.count("/"), reverse=True):
        if any(done.startswith(path + "/") for done in created):
            continue  # made already, as a parent of a deeper one
        parent, _, name = path.rpartition("/")
        try:
            await client.request(
                "SYNO.FileStation.CreateFolder",
                "create",
                params={
                    "folder_path": parent or "/",
                    "name": dsm_str(name),
                    "force_parent": "true",
                },
            )
        except SynologyError as e:
            synology_error_response("Restore files", e)
        created.append(path)
    return sorted(created)
