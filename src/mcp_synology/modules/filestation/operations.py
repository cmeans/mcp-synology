"""File Station operations: create_folder, rename, copy, move, delete, restore."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import anyio
import httpx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import (
    FILESTATION_ERROR_CODES,
    ErrorCode,
    SynologyError,
    error_from_code,
)
from mcp_synology.core.formatting import (
    ESCAPED_NAME_ADVICE,
    error_response,
    format_size,
    format_status,
    holds_escape,
    synology_error_response,
)
from mcp_synology.modules.filestation.helpers import (
    _escaped_repr,
    _is_dot,
    describe_transport_error,
    dsm_str,
    ensure_recycle_status,
    find_missing_paths,
    missing_paths_suggestion,
    missing_text,
    name_key,
    normalize_path,
    path_exists,
    paths_isdir,
    probe_recycle_status,
    shown,
    unreadable_paths_error,
)
from mcp_synology.modules.filestation.paths import (
    ResolvedPath,
    bin_index,
    bin_item_origin,
    refuse_bad_name,
    resolve_bin_items,
)
from mcp_synology.modules.filestation.tasks import (
    SKIP_CAUTION,
    Handle,
    HandleKind,
    Handoff,
    MonotonicProgress,
    ProgressCallback,
    RunningTask,
    TaskRecord,
    TaskRecords,
    _stop_background_task,
    find_running,
    handoff_reply,
    pending_landing,
    poll_task,
    running_landing,
    running_task,
    skipped_line,
    tasks_for_landing_check,
    tracked_landings,
    where_it_went,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)

# How long a lost delete waits on its path check (#521), so a stalled DSM can't hold the reply.
_LOST_DELETE_CHECK = 5.0


async def create_folder(
    client: DsmClient,
    *,
    paths: list[ResolvedPath],
    force_parent: bool = True,
) -> str:
    """Create one or more new folders (``paths`` checked by `resolve_paths`, #510).

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
    # refuses one before sending it since #479, so this is a defensive guard). `rename`
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
            f"Rename failed (DSM error 1200): {shown(path)} doesn't exist on the NAS.",
            retryable=False,
            param="path",
            value=original,
            suggestion=missing_paths_suggestion([(path, 408)]),
        )
    if taken:
        error_response(
            ErrorCode.ALREADY_EXISTS,
            f"Rename failed (DSM error 1200): {shown(target)} already exists. Rename doesn't "
            "replace an existing file or folder.",
            retryable=False,
            param="new_name",
            value=new_name,
            suggestion="Pick another name, or move or delete the existing one first.",
        )


async def rename(
    client: DsmClient,
    *,
    path: ResolvedPath,
    new_name: str,
) -> str:
    """Rename a file or folder (``path`` checked by `resolve_paths`, #510)."""
    normalized = normalize_path(path)

    # new_name is just a name: not a path, nor `.` or `..`, as #449 refuses in paths
    relocates = "/" in new_name or _is_dot(new_name)
    refuse_bad_name(
        "Rename",
        new_name,
        "new_name",
        "Use move_files to relocate files to a different directory."
        if relocates
        else "Pass the new name only, e.g. Severance.",
        parent=normalized.rsplit("/", 1)[0] or "/",
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
    paths: list[ResolvedPath],
    dest_folder: ResolvedPath,
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
    paths: list[ResolvedPath],
    dest_folder: ResolvedPath,
    overwrite: bool = False,
    timeout: float = 120.0,
    records: TaskRecords | None = None,
    on_progress: ProgressCallback | None = None,
) -> str:
    """Move files or folders to a new location (a share root, a whole bin or a user's home
    folder is refused by `resolve_paths`, #479, #510)."""
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
    to = f" (to {shown(dest)}/)" if dest else ""
    return (
        f"A {operation.lower()} of {shown(path)}{to} was already running on the NAS, so no second "
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
    # As messages show it: quoted when padded (`shown`, #489).
    pattern = r"(?<![^\s:(])" + re.escape(shown(path)) + r"(?=$|\.$|\.\s|\s\()"
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
        message = _unstop(message) + f". Failed on: {shown(failed)}."
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
        # "retry", and a check first, since the failed path may be gone already,
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
                f"refused), and check {shown(failed)} with list_files first."
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
        message = _unstop(message) + f". Not started: {', '.join(map(shown, rest))}."
    body["error"]["message"] = message
    return ToolError(json.dumps(body))


def _already_done(
    operation: str,
    done: list[str] | None,
    skipped: list[str] | None = None,
    *,
    dest_of: dict[str, str] | None = None,
    dest: str | None = None,
    failing: str | None = None,
    landed: list[str] | None = None,
) -> str:
    """Name the items a multi-path operation finished before a later one failed, and the ones DSM
    skipped, which aren't done: a move's is still at its source (#585). One that went elsewhere
    than ``dest`` says so (``dest_of``). ``failing``: the source that failed, part of which may
    have landed. ``landed``: what else landed, for a task whose record knows more."""

    def named(path: str) -> str:
        return where_it_went(path, dest_of, dest)

    text = ""
    if done:
        past = _PAST_TENSE.get(operation, "processed")
        text = f". Already {past} before this failure: " + ", ".join(map(named, done))
    if skipped:
        # A skip's advice warns here too: retrying one named like what landed replaces it (#580).
        ran = [*(done or []), *(landed or []), *([failing] if failing else [])]
        line = skipped_line(operation != "Copy", skipped, ran, dest_of=dest_of, dest=dest)
        text += ". " + line.removesuffix(".")
    return text


async def _refuse_missing_sources(
    client: DsmClient, operation: str, checked: list[str], original: Sequence[str]
) -> None:
    """Refuse the whole call before anything moves if any source is missing (#162 policy)."""
    try:
        missing = await find_missing_paths(client, checked)
    except SynologyError as e:
        synology_error_response(f"{operation} files", e)
    if missing:
        # Each path in words, and the code DSM gave (#620: a 407 was called "not found").
        err_code, suggestion = unreadable_paths_error(missing)
        error_response(
            err_code,
            f"{operation} files refused: "
            + "; ".join(missing_text(p, c) for p, c in missing)
            + f". Nothing was {_PAST_TENSE.get(operation, 'processed')}.",
            retryable=False,
            param="paths",
            value=original,
            suggestion=suggestion,
        )


def _refuse_landing_on_a_running_task(
    tasks: list[RunningTask],
    operation: str,
    remaining: list[str],
    dest: str,
    *,
    overwrite: bool,
    remove_src: bool,
    done: list[str],
    skipped: list[str],
    later: list[str] | None,
    dest_of: dict[str, str] | None = None,
) -> None:
    """Refuse if a running task would land in ``dest`` under one of ``remaining``'s names, and
    either replaces (#587). Checked before each path: a task can start during an earlier path's
    wait. Earlier paths may be done, or a restore's earlier items, so it says what was done and
    what wasn't started. Names are quoted, since a task's path may be anyone's text, and only the
    path that clashes is named, since a task can hold thousands."""
    found = running_landing(
        tasks, sources=remaining, dest=dest, overwrite=overwrite, remove_src=remove_src
    )
    if found is None:
        return
    source, clash, task = found
    rest = [p for p in remaining if p != source] + list(later or [])
    outcome = f" Nothing was {_PAST_TENSE.get(operation, 'changed')}."
    if done or skipped:
        outcome = (
            _already_done(operation, done, skipped, dest_of=dest_of, dest=dest).removeprefix(".")
            + "."
        )
        if rest:
            outcome += f" Not started: {', '.join(map(_escaped_repr, rest))}."
    same = clash.rsplit("/", 1)[-1] == source.rsplit("/", 1)[-1]
    names = (
        "the same name"
        if same
        else "a name that matches ignoring case, which some file systems treat as one,"
    )
    leave_out = " Leave out what's already done." if done else ""
    keep = "" if not overwrite else " Once it's done, overwrite=false keeps what's there."
    if task.task.get("local"):
        # Another call through this server, started or about to start: it counts until that
        # call returns, though DSM may show its task finished (QA round 2 on #597). The record
        # was read during this call's check, so that call may have returned since.
        what = (
            f"another {task.handle.kind} call through this server is landing in "
            f"{_escaped_repr(dest)} (with {_escaped_repr(clash)}, started or about to start when "
            "this call checked; it may have returned since)"
        )
        advice = (
            "Once that call has returned, check what's there (list_files) before "
            f"running this again, or use another destination.{keep}{leave_out}"
        )
    else:
        what = (
            f"a running {task.handle.kind} of {_escaped_repr(clash)} is landing in "
            f"{_escaped_repr(dest)}"
        )
        advice = (
            f"Check on it with file_task and the handle `{task.handle}`, and when it's done, "
            "check what's there (list_files) before running this again, or use another "
            f"destination.{keep}{leave_out}"
        )
    error_response(
        ErrorCode.INVALID_PARAMETER,
        f"{operation} files refused: {what}, with {names} as {_escaped_repr(source)}, so one "
        f"could replace the other (for folders, items of the same name inside them).{outcome}",
        retryable=False,
        param="paths",
        value=source,
        suggestion=advice,
    )


async def _refuse_missing_destination(
    client: DsmClient, operation: str, dest: str, *, anything_done: bool = False
) -> None:
    """Refuse before anything starts if the destination folder is missing or refused (#344).

    DSM answers a copy or move into a missing folder with 1002, "Destination error during
    copy/move", whose suggestion points at permissions. getinfo tells the cases apart: 408
    is `not_found`, 407 `permission_denied`. Any other DSM answer leaves it to the task, as
    before. No answer, or an unreadable one, stops a call with nothing started: starting then
    would be with the duplicate-task guard down too, since it reads the same stalled DSM. With
    ``anything_done`` (a restore's later destination), it goes on, so finished items aren't lost.
    """
    try:
        found = await find_missing_paths(client, [dest])
    except Exception as e:
        if not anything_done and not isinstance(e, SynologyError):
            raise
        # A restore to several places runs this after earlier items finished: no reply it can use
        # mustn't lose them (#588). At WARNING, as DSM's 1002 that follows a missing folder points
        # at permissions.
        logger.debug("The destination check's error", exc_info=True)
        logger.warning(
            "%s: destination check for %s failed (%s, DSM code %s); starting anyway",
            operation,
            dest,
            type(e).__name__,
            getattr(e, "code", None),
        )
        return
    codes = {code for _, code in found}
    nothing = f"Nothing was {_PAST_TENSE.get(operation, 'processed')}."
    if 408 in codes:
        error_response(
            ErrorCode.NOT_FOUND,
            f"{operation} files refused: the destination folder {shown(dest)} doesn't exist. "
            f"{nothing}",
            retryable=False,
            param="dest_folder",
            value=dest,
            # A destination copied from an escaped listing names nothing, and create_folder
            # would make a look-alike with a literal backslash, so that advice isn't given.
            suggestion=(
                ESCAPED_NAME_ADVICE
                if holds_escape(dest)
                else "Create it first with create_folder, or check the path with list_files, "
                "then retry."
            ),
        )
    if 407 in codes:
        error_response(
            ErrorCode.PERMISSION_DENIED,
            f"{operation} files refused: no permission to use the destination folder {shown(dest)}."
            f" {nothing}",
            retryable=False,
            param="dest_folder",
            value=dest,
            suggestion=(
                "Pick a folder this DSM account can write to, or have its permissions "
                "changed in DSM."
            ),
        )


async def _start_copy_move(
    client: DsmClient,
    path: str,
    *,
    dest: str,
    overwrite: bool,
    remove_src: bool,
    operation: str,
    copymove_version: int,
) -> str:
    """Start DSM's task for one path; its taskid. Raises ToolError on a DSM error."""
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
    return str(start_data.get("taskid", ""))


async def _finish_copy_move(
    client: DsmClient,
    path: str,
    taskid: str,
    *,
    dest: str,
    operation: str,
    copymove_version: int,
    timeout: float,
    done: list[str] | None = None,
    skipped: list[str] | None = None,
    dest_of: dict[str, str] | None = None,
    on_progress: ProgressCallback | None = None,
) -> tuple[int, bool] | Handoff:
    """Wait for a started copy or move of a single path, via DSM's async-task pattern.

    Returns the `processed_size` byte count from the completed task (and whether DSM
    skipped items that already existed at the destination, #171) so the
    caller can sum sizes across all per-path tasks, or a Handoff when the task outlived
    ``timeout``, DSM stopped answering its status requests (#245), or DSM kept answering that it
    has no such task (#521). Raises ToolError on
    failure (via error_response/synology_error_response). A task that DSM reports an
    error for (other than a 599 on a status read, #521), or whose call is canceled, is stopped,
    so orphaned tasks can't accumulate on the NAS.
    """
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
    if result.outcome in ("handoff", "stalled", "lost"):
        # Left running on the NAS, not stopped (#245): the caller gets a handle. Lost too: it
        # may still be running, and the reply says what's known (#521).
        stall = result.error if result.outcome != "handoff" else None
        return Handoff(Handle(_KIND[operation], taskid), result.status, stall)
    return _copy_move_result(
        result.status, path, operation, done, dest=dest, skipped_so_far=skipped, dest_of=dest_of
    )


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
        f". Part of it may already be in {shown(str(dest))}{gone}: DSM doesn't say how much was "
        f"{past} before it stopped, so check there before trying again"
    )


def _copy_move_result(
    status: dict[str, Any],
    path: str,
    operation: str,
    done: list[str] | None = None,
    *,
    dest: str | None = None,
    skipped_so_far: list[str] | None = None,
    dest_of: dict[str, str] | None = None,
    landed: list[str] | None = None,
) -> tuple[int, bool]:
    """``(processed bytes, skipped)`` from a finished CopyMove status; ToolError on failure."""
    if "error" in status:
        err = status["error"]
        err_code = err.get("code", 0) if isinstance(err, dict) else err
        err_path = status.get("path") or path
        mapped = error_from_code(err_code, "SYNO.FileStation.CopyMove")
        error_response(
            mapped.error_code,
            f"{operation} files failed: {_code_text(err_code)} on path: {shown(str(err_path))}"
            + _partial_note(status, [(err_path, err_code)], operation, path, dest)
            + _already_done(
                operation,
                done,
                skipped_so_far,
                dest_of=dest_of,
                dest=dest,
                failing=path,
                landed=landed,
            ),
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
            + "; ".join(f"{shown(p)} ({_code_text(code)})" for p, code in failed)
            + _partial_note(status, failed, operation, path, dest)
            + _already_done(
                operation,
                done,
                skipped_so_far,
                dest_of=dest_of,
                dest=dest,
                failing=path,
                landed=landed,
            ),
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
    # overwrite=true. Same on DSM 7.1, 7.2.2, 7.3.2 and 7.4.1 (#171; vdsm).
    skip = status.get("skipstatus")
    skip_value = (skip.get("status") or "unseen") if isinstance(skip, dict) else skip
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
    earlier: Sequence[str] = (),
) -> list[str]:
    """Lines for a copy/move/restore result where DSM skipped something (#171, #189).

    DSM's ``skipstatus`` says only that something was skipped for a source: all of a file,
    or some of a folder's children (the rest still went through). So each skipped source's
    type is looked up: a file was skipped, a folder had items skipped. A source whose type
    can't be read keeps the hedged "in whole or in part" wording. ``earlier``: other sources of the
    same call at this destination, landed or skipped, when a hand-off or a restore to several
    places split it up.
    """
    try:
        kinds = await paths_isdir(client, skipped_paths)
    except Exception as e:
        # The copy already happened; report it with the hedged wording, never as an error,
        # whatever the lookup met: right after a large task, DSM may not answer at all (#588).
        logger.warning(
            "Couldn't read the type of skipped sources %s: %s (DSM code %s)",
            skipped_paths,
            type(e).__name__,
            getattr(e, "code", None),
        )
        logger.debug("The skip lookup's error", exc_info=True)
        kinds = {}
    files = [p for p in skipped_paths if kinds.get(p) is False]
    folders = [p for p in skipped_paths if kinds.get(p) is True]
    unknown = [p for p in skipped_paths if p not in kinds]
    done = verb.lower()

    # What this call itself put at the destination, which `overwrite=true` would replace (#580).
    # Compared as `refuse_same_names` does; a skipped folder's other items landed too.
    landed: dict[str, str] = {}
    own: dict[str, str] = {}
    for p in earlier:
        landed.setdefault(name_key(p), p)
    for p in normalized:
        name = name_key(p)
        if p in skipped_paths and name in landed:
            own[p] = landed[name]
        if p not in skipped_paths or kinds.get(p) is not False:
            landed.setdefault(name, p)

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
            f"{', '.join(parts)} (that name is already at the destination, overwrite=false):",
            success=False,
        )
    ]

    def entry(p: str, folder: bool = False) -> str:
        # A skipped folder's leftovers are its own children; a file stays in its parent.
        src_dir = p if folder else "/".join(p.split("/")[:-1]) or ""
        where = f" (skipped items are still in {src_dir}/)"
        if p in unknown:
            where = " (skipped items are still at this path or under it)"
        return f"  {p}" + (where if remove_src else "")

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
    if own:
        lines.append(
            "Don't retry these with overwrite=true unless the user asked to replace that item: "
            "what's there may "
            "be what this call just put there, and it would be replaced, possibly with no copy in "
            "the recycle bin. Send them to another folder, or rename them first:"
        )
        lines.extend(
            f"  {_escaped_repr(p)} (this call's {_escaped_repr(first)} went first)"
            for p, first in own.items()
        )
    if not own:
        lines.append(f"Pass overwrite=true to replace them, {SKIP_CAUTION}")
    elif len(own) < len(skipped_paths):
        lines.append(f"For the others, pass overwrite=true to replace them, {SKIP_CAUTION}")
    return lines


async def _copy_move(
    client: DsmClient,
    *,
    paths: Sequence[str],
    dest_folder: str,
    overwrite: bool,
    remove_src: bool,
    operation: str,
    timeout: float = 120.0,
    check_sources: bool = True,
    already_done: list[str] | None = None,
    already_skipped: list[str] | None = None,
    dest_of: dict[str, str] | None = None,
    later: list[str] | None = None,
    records: TaskRecords | None = None,
    on_progress: ProgressCallback | None = None,
    fresh: Sequence[str] = (),
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

    # With overwrite, which sources' names are already at the destination, so the reply can
    # say what was replaced or merged into (#620, 0.11.0rc3 live test: an overwrite read like
    # a plain copy). It runs alongside the source and destination checks, never after them:
    # run after, it delayed this call's landing records and let a parallel call start a task
    # this one replaced (#597; pre-QA on #623). Not a target that is the source itself, nor one
    # in or at a folder this call re-created (`fresh`), nor one an earlier item of the call
    # landed: those weren't there before. A lookup that fails leaves the note unsaid.
    def made_here(path: str) -> bool:
        """``path`` is, or is inside, a folder this call re-created (its parents too)."""
        return any(f == path or f.startswith(path + "/") or path.startswith(f + "/") for f in fresh)

    targets: dict[str, str] = {}
    if overwrite:
        targets = {
            q: t
            for q in normalized
            if (t := f"{dest}/{q.rsplit('/', 1)[-1]}") != q and not made_here(t)
        }
    # One request per target, started before the checks below and awaited after this call's
    # landing records and before its first start, so the records aren't delayed (#597; pre-QA
    # on #623 measured a parallel call slipping in when they were) and the lookups can't see
    # this call's own landing. At most 4 at once: an overwrite of hundreds of items mustn't
    # burst requests at DSM ahead of the checks that share the connection pool (pre-QA r2).
    gate = asyncio.Semaphore(4)

    async def look(target: str) -> dict[str, bool]:
        async with gate:
            return await paths_isdir(client, [target])

    lookups = {q: asyncio.ensure_future(look(t)) for q, t in targets.items()}
    try:
        if check_sources:
            await _refuse_missing_sources(client, operation, normalized, paths)
        await _refuse_missing_destination(
            client, operation, dest, anything_done=bool(already_done or already_skipped)
        )
        return await _copy_move_tasks(
            client,
            normalized=normalized,
            paths=paths,
            dest=dest,
            overwrite=overwrite,
            remove_src=remove_src,
            operation=operation,
            timeout=timeout,
            copymove_version=copymove_version,
            already_done=already_done,
            already_skipped=already_skipped,
            dest_of=dest_of,
            later=later,
            records=records,
            on_progress=on_progress,
            lookups=lookups,
            targets=targets,
        )
    finally:
        for future in lookups.values():
            # Stops one still waiting; on one that failed, cancel() also marks its exception
            # as seen, so asyncio never logs it as never retrieved.
            future.cancel()


async def _replaced(
    lookups: Mapping[str, asyncio.Future[dict[str, bool]]], targets: dict[str, str]
) -> dict[str, bool]:
    """Which sources' names were already at the destination, and whether as a folder (#620)."""
    found: dict[str, bool] = {}
    for q, future in lookups.items():
        try:
            kinds = await future
        except Exception as e:  # noqa: BLE001 - never fail the copy over a reply note
            logger.debug("Couldn't check the destination for %s: %s", targets[q], e)
            continue
        if targets[q] in kinds:
            found[q] = kinds[targets[q]]
    return found


async def _copy_move_tasks(
    client: DsmClient,
    *,
    normalized: list[str],
    paths: Sequence[str],
    dest: str,
    overwrite: bool,
    remove_src: bool,
    operation: str,
    timeout: float,
    copymove_version: int,
    already_done: list[str] | None,
    already_skipped: list[str] | None,
    dest_of: dict[str, str] | None,
    later: list[str] | None,
    records: TaskRecords | None,
    on_progress: ProgressCallback | None,
    lookups: Mapping[str, asyncio.Future[dict[str, bool]]],
    targets: dict[str, str],
) -> tuple[str, bool]:
    """``_copy_move``'s tasks, once its checks passed: start each source, wait, hand off."""
    total_processed = 0
    done = list(already_done or [])
    skipped_so_far = already_skipped if already_skipped is not None else []
    went: dict[str, str] = dest_of if dest_of is not None else {}
    skipped_paths: list[str] = []
    landed_here: list[str] = []
    with tracked_landings(client) as track:
        # What this call will land, before it starts anything, so a parallel call sees all of
        # it (QA round 2 on #597).
        for q in normalized:
            track(
                pending_landing(
                    _KIND[operation], q, dest, overwrite=overwrite, remove_src=remove_src
                )
            )
        # What was there, read before the first start: answered after it, a lookup would see
        # this call's own landing (pre-QA r2 on #623). The records are published by now, so
        # waiting here doesn't open #597's window.
        replaced = await _replaced(lookups, targets)
        for i, p in enumerate(normalized):
            # A copy of the same source to another destination is a different operation; a
            # move (or restore) of a source that is already moving anywhere is not.
            match: dict[str, Any] = {"remove_src": remove_src}
            if not remove_src:
                match["dest_folder_path"] = dest
            outcome: tuple[int, bool] | Handoff
            task_dest, task_overwrite = dest, overwrite
            try:
                # Held from before the check until the start returns, and not across the wait: a
                # parallel call for this destination then finds this task in the list (#587). One
                # lock at a time, so calls can't deadlock.
                async with client.destination_lock(dest):
                    listed, tasks = await tasks_for_landing_check(client)
                    _refuse_landing_on_a_running_task(
                        tasks,
                        operation,
                        normalized[i:],
                        dest,
                        overwrite=overwrite,
                        remove_src=remove_src,
                        done=done,
                        skipped=skipped_so_far,
                        later=later,
                        dest_of=went,
                    )
                    # DSM's list only: a parallel call's finished task is nothing to hand back.
                    running = find_running(
                        listed, api="SYNO.FileStation.CopyMove", path=p, params=match
                    )
                    taskid = (
                        None
                        if running is not None
                        else await _start_copy_move(
                            client,
                            p,
                            dest=dest,
                            overwrite=overwrite,
                            remove_src=remove_src,
                            operation=operation,
                            copymove_version=copymove_version,
                        )
                    )
                    if taskid is not None:
                        track(
                            RunningTask(
                                Handle(_KIND[operation], taskid),
                                [p],
                                {
                                    "dest_folder_path": dest,
                                    "overwrite": overwrite,
                                    "remove_src": remove_src,
                                },
                                {"local": True},
                            )
                        )
                if running is not None:
                    started = running.get("params") or {}
                    if isinstance(started.get("dest_folder_path"), str):
                        task_dest = started["dest_folder_path"]
                    if isinstance(started.get("overwrite"), bool):
                        task_overwrite = started["overwrite"]
                    # Never start a second task on a path that already has one (#245).
                    outcome = Handoff(Handle(_KIND[operation], str(running["taskid"])), running)
                else:
                    assert taskid is not None
                    outcome = await _finish_copy_move(
                        client,
                        p,
                        taskid,
                        dest=dest,
                        operation=operation,
                        copymove_version=copymove_version,
                        timeout=timeout,
                        done=done,
                        skipped=skipped_so_far,
                        dest_of=went,
                        on_progress=on_progress,
                    )
            except httpx.TransportError as e:
                rest = normalized[i + 1 :] + list(later or [])
                if not done and not skipped_so_far and not rest:
                    raise  # nothing to add: the server's usual timeout or connection error
                # The start got no answer (a status with none hands the item off in
                # poll_task): it may have started. Say so, with what was done and what
                # wasn't started, as delete_files does (#588).
                not_started = f". Not started: {', '.join(map(shown, rest))}" if rest else ""
                error_response(
                    ErrorCode.TIMEOUT
                    if isinstance(e, httpx.TimeoutException)
                    else ErrorCode.UNAVAILABLE,
                    f"{operation} files failed: DSM didn't answer for {shown(p)} "
                    f"({describe_transport_error(e)}); its {operation.lower()} may have started"
                    + _already_done(
                        operation, done, skipped_so_far, dest_of=went, dest=dest, failing=p
                    )
                    + f"{not_started}.",
                    retryable=False,
                    param="paths",
                    value=p,
                    suggestion="Check with list_files, or file_task with no handle, before "
                    "running it again" + (", and leave out what's already done." if done else "."),
                )
            if isinstance(outcome, Handoff):
                # A task already running to another folder lands nothing here.
                placed = [*done, p] if task_dest == dest else list(done)
                if records is not None:
                    records.add(
                        TaskRecord(
                            handle=outcome.handle,
                            paths=[p],
                            dest=task_dest,
                            overwrite=task_overwrite,
                            # Only what landed, and only for the destination this call asked for.
                            earlier=landed_here if task_dest == dest else [],
                            skipped_earlier=list(skipped_so_far),
                            done_earlier=list(done),
                            landed_earlier=placed,
                            dest_of=dict(went),
                        )
                    )
                reply = handoff_reply(
                    verb=f"Still {_PRESENT[operation].lower()}",
                    path=p,
                    handle=outcome.handle,
                    status=outcome.status,
                    stall=outcome.stall,
                    dest=task_dest,
                    done=done,
                    skipped=skipped_so_far,
                    landed=placed,
                    dest_of=went,
                    not_started=normalized[i + 1 :] + list(later or []),
                )
                if running is not None:
                    other = task_dest if task_dest != dest else None
                    reply = _already_running(operation, p, other) + "\n" + reply
                # A skipped folder lands its other items, and a hand-off can't tell it from a file.
                ran = {name_key(path) for path in [*placed, *skipped_so_far]}
                alike = [q for q in normalized[i + 1 :] if name_key(q) in ran]
                if alike:
                    # #580: "Run them again" with overwrite=true would replace what it placed.
                    named = ", ".join(map(_escaped_repr, alike))
                    reply += (
                        f"\nNamed like one this call already ran: {named}. "
                        "Run them with overwrite=false or send them to another folder: with "
                        "overwrite=true they could replace it."
                    )
                return reply, True
            size, skipped = outcome
            total_processed += size
            went[p] = dest
            if skipped:
                skipped_paths.append(p)
                skipped_so_far.append(p)
            else:
                landed_here.append(p)
                done.append(p)

        text = await _copy_move_summary(
            client,
            normalized,
            dest,
            remove_src,
            total_processed,
            skipped_paths,
            only_named_sources_removed=already_skipped is not None,
            # A restore's earlier items at this destination; for any other call, none.
            earlier=[
                q for q in [*done, *skipped_so_far] if q not in normalized and went.get(q) == dest
            ],
            replaced=replaced,
        )
        return text, False


async def _copy_move_summary(
    client: DsmClient,
    normalized: list[str],
    dest: str,
    remove_src: bool,
    total_processed: int,
    skipped_paths: list[str],
    *,
    only_named_sources_removed: bool = False,
    earlier: Sequence[str] = (),
    replaced: dict[str, bool] | None = None,
) -> str:
    """The result text of a finished copy or move (a restore rewords it, ``_as_restore``).
    ``replaced``: sources whose name was already at the destination (with overwrite), and
    whether that was a folder (#620)."""
    verb = "Copied" if not remove_src else "Moved"
    ok = [p for p in normalized if p not in skipped_paths]

    def name(p: str) -> str:
        return p.split("/")[-1]

    if not skipped_paths:
        lines = [format_status(f"{verb} {len(normalized):,} item(s) to {dest}/:")]
        lines.extend(f"  {name(p)}" for p in normalized)
    else:
        lines = await _skip_report(
            client, verb, dest, normalized, ok, skipped_paths, remove_src, earlier
        )

    gone_over = {p: isdir for p, isdir in (replaced or {}).items() if p in ok}
    over_files = [name(p) for p, isdir in gone_over.items() if not isdir]
    over_folders = [name(p) for p, isdir in gone_over.items() if isdir]
    if over_files:
        lines.append(f"\nReplaced what was already there: {', '.join(over_files)}")
    if over_folders:
        lines.append(
            "\nMerged into the folder(s) already there, replacing files with the same names: "
            + ", ".join(over_folders)
        )
    if over_files or over_folders:
        # As the tools' descriptions warn: vdsm's /home kept no copy (pre-QA r3 on #623).
        lines.append("A file replaced this way may not be in the recycle bin.")

    if total_processed > 0:
        lines.append(f"\nTotal size: {format_size(total_processed)}")

    # With a skip, or from a restore to several places or file_task's final result, whose other
    # sources may still be in that folder, it names what went instead (#585).
    if remove_src and not skipped_paths and not only_named_sources_removed:
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
    returns None when the delete finished, or DSM kept answering that it has no such task and
    the path is gone (#521); or a Handoff when the task outlived ``timeout``, DSM stopped
    answering its status requests (#245), or it has no such task and the path isn't known to be
    gone. A task that DSM
    reports an error for, or whose call is canceled, is stopped; a 599 on a status read isn't
    such an error (#521).
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
    if result.outcome == "lost":
        # DSM can't say how it ended, but the path can: gone is what was asked for (#521).
        # Bounded: a stalled DSM mustn't hold the reply and its handle (#245).
        try:
            exists = await asyncio.wait_for(path_exists(client, path), _LOST_DELETE_CHECK)
        except asyncio.CancelledError:
            # A canceled call stops its task, as `poll_task` does: this one may be running.
            with anyio.CancelScope(shield=True):
                await _stop_background_task(
                    client, "SYNO.FileStation.Delete", dsm_str(taskid), delete_version, logger
                )
            raise
        except Exception as e:
            # Any answer that doesn't say it's gone leaves the outcome unknown.
            logger.warning("Couldn't check %s after a lost delete: %s", path, type(e).__name__)
            exists = None
        if exists is False:
            return None
    if result.outcome in ("handoff", "stalled", "lost"):
        # Left running on the NAS, not stopped (#245): the caller gets a handle.
        stall = result.error if result.outcome != "handoff" else None
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
            + "; ".join(f"{shown(p)} (DSM error {code})" for p, code in failed)
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
            f"deleted for {shown(path)}.",
            retryable=False,
            param="paths",
            value=path,
            suggestion=missing_paths_suggestion([(path, 408)]),
        )


async def delete_files(
    client: DsmClient,
    *,
    paths: list[ResolvedPath],
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
        err_code, suggestion = unreadable_paths_error(missing)
        error_response(
            err_code,
            "Delete files refused: "
            + "; ".join(missing_text(p, c) for p, c in missing)
            + ". Nothing was deleted.",
            retryable=False,
            param="paths",
            value=paths,
            suggestion=suggestion,
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
                        f"Delete files failed: DSM didn't answer for {shown(p)} "
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
            in_bin = bin_index(parts[1:]) is not None
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
    # A path inside a share's bin (/<share>/#recycle/…) is gone for good whatever the share's
    # setting: DSM keeps no second copy (#220). A user's home bin reached through /homes
    # (/homes/<user>/#recycle/…) is, to DSM, a folder inside the homes share, so a delete there
    # goes into the homes share's own bin when that's on (/homes/#recycle/<user>/#recycle/…:
    # seska DSM 7.1.1, found in the 0.11.0rc2 live test, #610) and is permanent only when it's
    # off (vdsm, all four versions; Share set answered 403 for turning it on, on DSM 7.1 and
    # 7.4.1). Keyed by the bin's folder below the root: "video", or "homes/alice".
    in_bin: dict[str, list[str]] = {}
    outside_bin: list[str] = []
    for p in normalized:
        parts = p.split("/")[1:]
        at = bin_index(parts)
        if at is not None:
            in_bin.setdefault("/".join(parts[:at]), []).append(p)
        else:
            outside_bin.append(p)

    # Determine recycle bin status per share. Probes lazily on first observation
    # via `ensure_recycle_status` so the dict stays empty until we actually need
    # the answer; pre-#37 the dict was never populated and every share was
    # incorrectly reported as recycle-on. The dict is shared across all tool
    # handlers (closure-captured in modules/filestation/__init__.py) and cleared
    # on session re-auth (see `_register_recycle_status_invalidator` in __init__).
    shares_with_recycle: set[str] = set()
    shares_without_recycle: set[str] = set()
    shares_unknown: set[str] = set()  # DSM didn't answer
    shares_unclear: set[str] = set()  # answered, but not whether the bin is on

    # A home bin through /homes follows the homes share's own bin (#610), checked once per
    # call, here, for the home bins and for any other /homes path the call deleted (pre-QA
    # r2 on #613). Unlike ensure_recycle_status's hopeful default (#255), a probe that can't
    # tell is "couldn't check", never "can still be recovered".
    home_keys = {key for key in in_bin if "/" in key}
    homes_bin: bool | None = None
    homes_answered = True
    if home_keys and recycle_bin_status is not None:
        if "homes" in recycle_bin_status:
            homes_bin = recycle_bin_status["homes"]
        else:
            try:
                homes_bin, definite = await probe_recycle_status(client, "homes")
            except httpx.TransportError as e:
                logger.debug("Recycle-bin probe on /homes unanswered: %r", e)
                homes_answered = False
            else:
                if not definite:
                    # A tentative answer (no #recycle, and /homes's setting unreadable). After
                    # the delete that most likely means off (#409), but it isn't proven, and
                    # this check never guesses (QA round 1 on #613).
                    homes_bin = None
                elif homes_bin is not None:
                    recycle_bin_status["homes"] = homes_bin
    kept_bins = home_keys if homes_bin else set()
    unknown_bins = home_keys if homes_bin is None else set()
    if recycle_bin_status is None:
        # Defensive: caller passed nothing. Skip probing; preserve prior
        # default-true messaging for the touched shares, except /homes beside a home bin,
        # which takes the same strict answer as the home bin (pre-QA r3 on #613).
        for p in outside_bin:
            parts = p.split("/")
            share_name = parts[1] if len(parts) > 1 else ""
            if share_name == "homes" and home_keys:
                shares_unclear.add(share_name)
            else:
                shares_with_recycle.add(share_name)
    else:
        seen: set[str] = set()
        for p in outside_bin:
            parts = p.split("/")
            share_name = parts[1] if len(parts) > 1 else ""
            if share_name in seen:
                continue
            seen.add(share_name)
            if share_name == "homes" and home_keys:
                # Already checked above, strictly: the same answer for the whole reply.
                if homes_bin is None:
                    (shares_unknown if not homes_answered else shares_unclear).add(share_name)
                elif homes_bin:
                    shares_with_recycle.add(share_name)
                else:
                    shares_without_recycle.add(share_name)
                continue
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

    # Shares where this call deleted from a bin for good (a home bin kept in /homes's isn't).
    bin_shares = {key.split("/")[0] for key in in_bin if key not in kept_bins | unknown_bins}
    has_recycle = bool(
        shares_with_recycle or shares_unknown or shares_unclear or kept_bins or unknown_bins
    )
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
        if share in kept_bins or share in unknown_bins:
            user = share.split("/", 1)[1]
            kept = f"/homes/#recycle/{user}/#recycle"
        if share in kept_bins:
            lines.append(
                f"\nRecycle bin is enabled on /homes, so these went from {user}'s home recycle "
                f"bin into /homes's own ({kept}). They can still be recovered: list_recycle_bin "
                "share=homes with a pattern (their names) finds them, and "
                "restore_from_recycle_bin share=homes brings them back with a dest_folder "
                "outside the bin. Empty folders aren't kept in the bin. To remove them for "
                "good, delete the ones still there (that listing shows which) by those paths, "
                "after the user agrees:"
            )
            home_bin = f"/homes/{user}/#recycle"
            lines.extend(f"  {kept}{p[len(home_bin) :]}" for p in bin_paths)
            continue
        if share in unknown_bins:
            lines.append(
                f"\nCouldn't check whether /homes has its recycle bin enabled: if it is, these "
                f"went from {user}'s home recycle bin into {kept}, and list_recycle_bin "
                "share=homes shows whether they can be recovered:"
            )
            lines.extend(f"  {p}" for p in bin_paths)
            continue
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
    lines.extend(
        f"\nCouldn't check whether /{share} has its recycle bin enabled; "
        "list_recycle_bin shows whether these files can be recovered."
        for share in sorted(shares_unclear)
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
    share: ResolvedPath,
    paths: list[str],
    dest_folder: ResolvedPath | None = None,
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

    # Each item as the full path of an item in the bin (#479, #488, #510).
    full_paths: list[str] = list(resolve_bin_items(share_name, paths))
    restore_destinations: list[str] = []
    for item in full_paths:
        # Infer original path by stripping #recycle
        if dest_folder:
            restore_destinations.append(dest_folder)
        else:
            # /video/#recycle/Shows/Ep1.mkv -> /video/Shows
            restore_destinations.append(bin_item_origin(share_name, item))

    await _refuse_missing_sources(client, "Restore", full_paths, paths)
    destinations = [dest_folder or d for d in restore_destinations]
    # One record of what this restore will land and starts, across its items (#597): each
    # item's destination is recorded before anything starts, and checked before any folder is
    # re-created, so a clash visible now refuses the whole restore with nothing changed.
    with tracked_landings(client) as track:
        # Recorded as they'll land: an item inside another one going to the same place lands
        # inside it, not beside it (QA round 3 on #597). The list read here is the one check
        # made before any folder is re-created; each item is checked again as it starts.
        by_dest: dict[str, list[str]] = {}
        for fp, dest in zip(full_paths, destinations, strict=False):
            by_dest.setdefault(dest, []).append(fp)
        landing = [
            (fp, dest) for dest, items in by_dest.items() for fp in _collapse_overlaps(items)
        ]
        for fp, dest in landing:
            track(pending_landing("restore", fp, dest, overwrite=overwrite, remove_src=True))
        _, tasks = await tasks_for_landing_check(client)
        # Checked as recorded: a nested item lands inside its parent, not in `dest`.
        for fp, dest in landing:
            _refuse_landing_on_a_running_task(
                tasks,
                "Restore",
                [fp],
                dest,
                overwrite=overwrite,
                remove_src=True,
                done=[],
                skipped=[],
                later=None,
            )
        recreated = (
            [] if dest_folder else await _recreate_missing_folders(client, restore_destinations)
        )
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
            restored_skipped: list[str] = []
            restored_to: dict[str, str] = {}
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
                    already_skipped=restored_skipped,
                    dest_of=restored_to,
                    later=full_paths[i + 1 :],
                    records=records,
                    on_progress=on_progress,
                    fresh=recreated,
                )
                if handed_off:
                    earlier_skips = sum(x.startswith("[!]") for x in results)
                    if earlier_skips:
                        # The first line is what gets read, as it is for a finished restore.
                        results.insert(
                            0,
                            format_status(
                                f"Restored {len(results) - earlier_skips:,} of "
                                f"{len(full_paths):,} item(s) from /{share_name} recycle bin "
                                f"so far; {earlier_skips:,} with skips (that name is already at "
                                "the destination, "
                                "overwrite=false; details below):",
                                success=False,
                            ),
                        )
                    results.append(r)
                    return "\n".join(results) + note
                results.append(_as_restore(r, share_name))
                if fp not in restored_skipped:
                    restored.append(fp)
            partial = [r for r in results if r.startswith("[!]")]
            if partial:
                results.insert(
                    0,
                    format_status(
                        f"Restored {len(results) - len(partial):,} of {len(results):,} item(s) "
                        f"from /{share_name} recycle bin; {len(partial):,} with skips (that name "
                        "is already at the destination, overwrite=false; details below):",
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
            fresh=recreated,
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
