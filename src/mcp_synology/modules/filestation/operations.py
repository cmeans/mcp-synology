"""File Station operations: create_folder, rename, copy, move, delete, restore."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, NoReturn

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.display import escaped_repr, shown, shown_beside
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
    path_list,
    rewrapped_error,
    sentence,
    synology_error_response,
    then,
)
from mcp_synology.modules.filestation.batch import (
    BatchOutcome,
    Budget,
    Item,
    ItemOutcome,
    batch_reply,
    bounded_summary,
    no_scope,
    run_batch,
)
from mcp_synology.modules.filestation.helpers import (
    RESTORE_UNDER_FILE_ADVICE,
    UNDER_FILE_ADVICE,
    _is_dot,
    describe_transport_error,
    diagnose_under_file,
    dsm_str,
    ensure_recycle_status,
    files_above,
    find_missing_paths,
    missing_paths_suggestion,
    missing_text,
    name_key,
    no_answer,
    normalize_path,
    path_exists,
    paths_isdir,
    probe_recycle_status,
    under_file_text,
    unreadable_paths_error,
)
from mcp_synology.modules.filestation.paths import (
    Plan,
    ResolvedPath,
    bin_index,
    bin_item_origin,
    plan_deletes,
    plan_landings,
    refuse_bad_name,
    resolve_bin_items,
)
from mcp_synology.modules.filestation.tasks import (
    SKIP_CAUTION,
    Handle,
    HandleKind,
    MonotonicProgress,
    ProgressCallback,
    RunningTask,
    TaskRecords,
    find_running,
    handoff_reply,
    pending_landing,
    running_landing,
    running_task,
    skipped_line,
    tasks_for_landing_check,
    tracked_landings,
    where_it_went,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager, AbstractContextManager

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
    for p, given in zip(normalized, paths, strict=True):
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
            # DSM's 1100 "Insufficient filesystem permissions" for a folder under a file (vdsm
            # 7.1, #702), which points at permissions.
            if e.code == 1100:
                blocker = await diagnose_under_file(client, p)
                if blocker:
                    error_response(
                        ErrorCode.NOT_FOUND,
                        f"Create folder failed: {shown(p)} can't be created: {shown(blocker)} "
                        "is a file.",
                        retryable=False,
                        param="paths",
                        value=given,  # the caller's own text (QA r1 on #714)
                        suggestion=UNDER_FILE_ADVICE,
                    )
            synology_error_response("Create folder", e)
        folders.extend(data.get("folders", []))

    lines = [format_status(f"Created {len(folders):,} folder(s):")]
    for f in folders:
        path = f.get("path", "")
        # A reply naming what a call acted on quotes what wouldn't show (#519, #688).
        lines.append(f"  \U0001f4c1 {shown(path)}")

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
    # getinfo's 401 entry, no answer either way: a path under a file (vdsm 7.1, #702).
    blocker = await diagnose_under_file(client, path) if source is None else None
    if blocker:
        error_response(
            ErrorCode.NOT_FOUND,
            f"Rename failed (DSM error 1200): {under_file_text(path, blocker)}.",
            retryable=False,
            param="path",
            value=original,
            suggestion=missing_paths_suggestion([(path, 408)]),
        )
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
        # Quoted when padding or a hidden character wouldn't show (#519, #688).
        return format_status(f"Renamed:\n  {shown(normalized)} \u2192 {shown(new_path)}")

    return format_status(f"Renamed {shown(normalized)} to {shown(new_name)}")


async def copy_files(
    client: DsmClient,
    *,
    paths: list[ResolvedPath],
    dest_folder: ResolvedPath,
    overwrite: bool = False,
    timeout: float = 120.0,
    records: TaskRecords | None = None,
    on_progress: ProgressCallback | None = None,
    paths_arg: Sequence[str] | None = None,
) -> str:
    """Copy files or folders to a destination. ``paths_arg``: the caller's own text for
    ``paths``, which the landing checks' refusals quote (#684)."""
    text, _ = await _copy_move(
        client,
        tool="copy_files",
        given=paths_arg if paths_arg is not None else paths,
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
    paths_arg: Sequence[str] | None = None,
) -> str:
    """Move files or folders to a new location (a share root, a whole bin or a user's home
    folder is refused by `resolve_paths`, #479, #510). ``paths_arg``: as `copy_files`'."""
    text, _ = await _copy_move(
        client,
        tool="move_files",
        given=paths_arg if paths_arg is not None else paths,
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
        # One a line when there are two or more (#693).
        text = path_list(f"Already {past} before this failure", [named(p) for p in done])
    if skipped:
        # A skip's advice warns here too: retrying one named like what landed replaces it (#580).
        ran = [*(done or []), *(landed or []), *([failing] if failing else [])]
        text = then(
            text, skipped_line(operation != "Copy", skipped, ran, dest_of=dest_of, dest=dest)
        )
    return text


async def _refuse_missing_sources(
    client: DsmClient, operation: str, checked: list[str], original: Sequence[str]
) -> None:
    """Refuse the whole call before anything moves if any source is missing (#162 policy)."""
    try:
        missing = await find_missing_paths(client, checked)
        under = await files_above(client, missing)  # a path under a file (#702)
    except SynologyError as e:
        synology_error_response(f"{operation} files", e)
    if missing:
        # Each path in words, and the code DSM gave (#620: a 407 was called "not found").
        err_code, suggestion = unreadable_paths_error(missing, under)
        error_response(
            err_code,
            # One a line when two or more (#693).
            then(
                path_list(
                    f"{operation} files refused",
                    [missing_text(p, c, under.get(p)) for p, c in missing],
                ),
                f"Nothing was {_PAST_TENSE.get(operation, 'processed')}.",
            ),
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
    anything_done: bool,
) -> None:
    """Refuse if a running task would land in ``dest`` under one of ``remaining``'s names, and
    either replaces (#587). Checked before each path: a task can start during an earlier path's
    wait. Earlier paths may be done, or a restore's earlier items: then the batch's failure
    formatter says what was done and what wasn't started (#482); else it says nothing was.
    Names are quoted, since a task's path may be anyone's text, and only the path that clashes
    is named, since a task can hold thousands."""
    found = running_landing(
        tasks, sources=remaining, dest=dest, overwrite=overwrite, remove_src=remove_src
    )
    if found is None:
        return
    source, clash, task = found
    outcome = "" if anything_done else f" Nothing was {_PAST_TENSE.get(operation, 'changed')}."
    same = clash.rsplit("/", 1)[-1] == source.rsplit("/", 1)[-1]
    names = (
        "the same name"
        if same
        else "a name that matches ignoring case, which some file systems treat as one,"
    )
    keep = "" if not overwrite else " Once it's done, overwrite=false keeps what's there."
    if task.task.get("local"):
        # Another call through this server, started or about to start: it counts until that
        # call returns, though DSM may show its task finished (QA round 2 on #597). The record
        # was read during this call's check, so that call may have returned since.
        what = (
            f"another {task.handle.kind} call through this server is landing in "
            f"{escaped_repr(dest)} (with {escaped_repr(clash)}, started or about to start when "
            "this call checked; it may have returned since)"
        )
        advice = (
            "Once that call has returned, check what's there (list_files) before "
            f"running this again, or use another destination.{keep}"
        )
    else:
        what = (
            f"a running {task.handle.kind} of {escaped_repr(clash)} is landing in "
            f"{escaped_repr(dest)}"
        )
        advice = (
            f"Check on it with file_task and the handle `{task.handle}`, and when it's done, "
            "check what's there (list_files) before running this again, or use another "
            f"destination.{keep}"
        )
    error_response(
        ErrorCode.INVALID_PARAMETER,
        f"{operation} files refused: {what}, with {names} as {escaped_repr(source)}, so one "
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
    copy/move", whose suggestion points at permissions. getinfo tells the cases apart: 408 is
    `not_found`, 407 `permission_denied`, and a 401 for a folder under a file `not_found` naming
    the file (#702). Any other DSM answer leaves it to the task, as before. No answer, or an
    unreadable one, stops a call with nothing started: starting then would be with the
    duplicate-task guard down too, since it reads the same stalled DSM. With ``anything_done``
    (a restore's later destination), it goes on, so finished items aren't lost.
    """
    try:
        found = await find_missing_paths(client, [dest])
        # A folder under a file: getinfo's 401, where DSM's task answers 1002 (vdsm 7.1, #702).
        # In the same try, so no answer stops the call as the getinfo above does.
        under = await files_above(client, found)
    except Exception as e:
        # No answer, a proxy's error status or a reply that isn't DSM's (a `SynologyError` too)
        # ends a call with nothing started (QA r1 on #714).
        if not anything_done and not isinstance(e, SynologyError):
            raise  # a transport error: the server's own structured error
        if not anything_done and no_answer(e):
            # Named as the source check's is (pre-QA r3 on #714).
            synology_error_response(f"{operation} files", e)
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
    # After earlier items finished, the batch's failure formatter says what was done (#482).
    nothing = "" if anything_done else f" Nothing was {_PAST_TENSE.get(operation, 'processed')}."
    if 408 in codes:
        error_response(
            ErrorCode.NOT_FOUND,
            f"{operation} files refused: the destination folder {shown(dest)} doesn't exist."
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
    blocker = next(iter(under.values()), None)  # one path checked, however DSM spells it
    if blocker:
        error_response(
            ErrorCode.NOT_FOUND,
            f"{operation} files refused: the destination folder "
            f"{under_file_text(dest, blocker)}.{nothing}",
            retryable=False,
            param="dest_folder",
            value=dest,
            suggestion=RESTORE_UNDER_FILE_ADVICE if operation == "Restore" else UNDER_FILE_ADVICE,
        )
    if 407 in codes:
        error_response(
            ErrorCode.PERMISSION_DENIED,
            f"{operation} files refused: no permission to use the destination folder {shown(dest)}."
            f"{nothing}",
            retryable=False,
            param="dest_folder",
            value=dest,
            suggestion=(
                "Pick a folder this DSM account can write to, or have its permissions "
                "changed in DSM."
            ),
        )


def _upper_first(text: str) -> str:
    """``text`` with its first character upper-cased and the rest as written."""
    return text[:1].upper() + text[1:]


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
        f"Part of it may already be in {shown(str(dest))}{gone}: DSM doesn't say how much was "
        f"{past} before it stopped, so check there before trying again."
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
            then(
                then(
                    f"{operation} files failed: {_code_text(err_code)} on path: "
                    f"{shown(str(err_path))}.",
                    _partial_note(status, [(err_path, err_code)], operation, path, dest),
                ),
                _already_done(
                    operation,
                    done,
                    skipped_so_far,
                    dest_of=dest_of,
                    dest=dest,
                    failing=path,
                    landed=landed,
                ),
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
            then(
                then(
                    # One a line when two or more failed (#693).
                    path_list(
                        f"{operation} files failed",
                        [f"{shown(p)} ({_code_text(code)})" for p, code in failed],
                    ),
                    _partial_note(status, failed, operation, path, dest),
                ),
                _already_done(
                    operation,
                    done,
                    skipped_so_far,
                    dest_of=dest_of,
                    dest=dest,
                    failing=path,
                    landed=landed,
                ),
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
    *,
    probe: bool = True,
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
        # Out of time (`bounded_summary`), the kinds aren't read: the hedged wording, as for a
        # lookup DSM didn't answer.
        kinds = await paths_isdir(client, skipped_paths) if probe else {}
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
            f"{verb} {len(ok):,} of {len(normalized):,} item(s){in_full} to {shown(dest)}/; "
            f"{', '.join(parts)} (that name is already at the destination, overwrite=false):",
            success=False,
        )
    ]

    def entry(p: str, folder: bool = False) -> str:
        # A skipped folder's leftovers are its own children; a file stays in its parent.
        src_dir = p if folder else "/".join(p.split("/")[:-1]) or ""
        where = f" (skipped items are still in {shown(src_dir)}/)"
        if p in unknown:
            where = " (skipped items are still at this path or under it)"
        return f"  {shown(p)}" + (where if remove_src else "")

    if ok:
        lines.append(f"{verb}:")
        lines.extend(f"  {shown(p.split('/')[-1])}" for p in ok)
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
            f"  {escaped_repr(p)} (this call's {escaped_repr(first)} went first)"
            for p, first in own.items()
        )
    if not own:
        lines.append(f"Pass overwrite=true to replace them, {SKIP_CAUTION}")
    elif len(own) < len(skipped_paths):
        lines.append(f"For the others, pass overwrite=true to replace them, {SKIP_CAUTION}")
    return lines


def _with_replaced_note(
    error: ToolError,
    operation: str,
    done: list[str],
    replaced: dict[str, str],
    went: dict[str, str],
    dest: str,
) -> ToolError:
    """``error`` with what the items already done met at the destination (#620), for a call
    that failed after them; unchanged when nothing is to be said."""

    def of(kind: str) -> list[str]:
        # A name holding ", " is quoted by shown(), so a list can't read it as two (QA round 1
        # on #623, #685).
        return [where_it_went(q, went, dest) for q in done if replaced.get(q) == kind]

    files, folders = of(_REPLACED_FILE), of(_MERGED)
    other, unchecked = of(_FILE_THERE) + of(_FOLDER_THERE), of(_UNCHECKED)
    if not (files or folders or other or unchecked):
        return error
    try:
        body = json.loads(str(error))
        message = body["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return error
    past = _PAST_TENSE.get(operation, "processed")
    # One list per kind, each one a line when two or more (#693), after the list of what was
    # done, which they're part of.
    added = ""
    for heading, names in (
        ("Of those, these replaced what was already there", files),
        (
            "Of those, these merged into the folder(s) already there, replacing files with the "
            "same names",
            folders,
        ),
        (
            "Of those, these met an item of the other kind (a file or a folder) under the same "
            "name, and what DSM did with it isn't known",
            other,
        ),
        ("Of those, these couldn't be checked for what was already there", unchecked),
    ):
        if names:
            added = then(added, path_list(heading, names))
    if files or folders:
        added = then(added, "A file replaced this way may not be in the recycle bin.")
    # Right after the list of what was done, which this call wrote (_already_done), so a
    # name holding "Not started: " can't move it (pre-QA r6 on #623); otherwise at the end.
    anchor = path_list(
        f"Already {past} before this failure", [where_it_went(q, went, dest) for q in done]
    )
    at = message.rfind(anchor)
    if at >= 0:
        end = at + len(anchor)
        body["error"]["message"] = then(message[:end], added) + message[end:]
    else:
        body["error"]["message"] = then(sentence(message), added)
    return rewrapped_error(body)


# What a source's name met at the destination (#620): a file it replaced (`file`), a folder it
# merged into (`folder`), something of the other kind (`file-there`, `folder-there`: what DSM
# did with it isn't captured), or a lookup that failed (`unchecked`).
_REPLACED_FILE, _MERGED, _FILE_THERE, _FOLDER_THERE, _UNCHECKED = (
    "file",
    "folder",
    "file-there",
    "folder-there",
    "unchecked",
)


async def _replaced(
    lookups: Mapping[str, asyncio.Future[dict[str, bool]]], targets: dict[str, str]
) -> dict[str, str]:
    """What each source's name met at the destination (#620), by source; a name with nothing
    there is left out."""
    found: dict[str, str] = {}
    for q, future in lookups.items():
        try:
            kinds = await future
        except Exception as e:  # noqa: BLE001 - never fail the copy over a reply note
            logger.debug("Couldn't check the destination for %s: %s", targets[q], e)
            found[q] = _UNCHECKED
            continue
        there = kinds.get(targets[q])
        if there is None:
            continue
        source_is_dir = kinds.get(q)
        if there:
            found[q] = _MERGED if source_is_dir is True else _FOLDER_THERE
        else:
            found[q] = _REPLACED_FILE if source_is_dir is False else _FILE_THERE
    return found


def _refuse_no_paths(operation: str, paths: Sequence[str]) -> None:
    """Refuse a call with no paths."""
    if not paths:
        error_response(
            ErrorCode.NOT_FOUND,
            f"{operation} files failed: No paths provided.",
            retryable=False,
            param="paths",
            value=paths,
            suggestion="Pass at least one path.",
        )


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
    records: TaskRecords | None = None,
    on_progress: ProgressCallback | None = None,
    fresh: Sequence[str] = (),
    share_name: str = "",
    plan: Plan | None = None,
    tool: str = "",
    given: Sequence[str] = (),
) -> tuple[str, bool]:
    """Shared implementation for copy and move operations, and a restore to one place.

    Closes #84 (the `move_files` half — `copy_files` and the multi-path
    `restore_from_recycle_bin` flow share this code path). DSM 7.x's
    `SYNO.FileStation.CopyMove start` does not honor the documented
    comma-joined multi-path format on v2 — a request with
    `path=/a,/b` is treated as a single literal path and silently no-ops
    while the task reports success. Same root-cause family as #68
    (delete + getinfo); fix matches that pattern: one DSM CopyMove task
    per input path, run by `batch.run_batch` (#512).

    Returns ``(text, handed_off)``. When a path's task outlives ``timeout`` it is
    handed off (#245): the text is the handoff reply, and the remaining paths are listed as not
    started. ``fresh``: folders this call re-created, so nothing in them was already there.
    ``plan``: a restore's, which checked every item's landing already; or, with none, this
    checks them before any DSM request, as ``tool`` with the caller's own text ``given`` (which a
    refusal quotes): one or the other, never both (#684).
    """
    _refuse_no_paths(operation, paths)
    dest = normalize_path(dest_folder)
    sources = [normalize_path(p) for p in paths]
    if plan is None:
        assert tool and given, "_copy_move: a plan, or the tool and the caller's text"
        plan = plan_landings(tool, given, sources, [dest] * len(sources), overwrite=overwrite)
    else:
        assert not tool and not given, "_copy_move: a plan, or the tool and its text, not both"
    normalized = _collapse_overlaps(sources, drop_nested=remove_src)
    items = [Item(p, dest) for p in normalized]
    adapter = CopyMoveAdapter(
        client,
        items,
        operation=operation,
        overwrite=overwrite,
        remove_src=remove_src,
        check_sources=check_sources,
        paths_arg=paths,
        fresh=fresh,
        share_name=share_name,
    )
    outcome = await run_batch(
        client,
        adapter,
        plan,
        items,
        budget=Budget(timeout),
        records=records,
        on_progress=on_progress,
    )
    return await batch_reply(adapter, outcome), outcome.last is not None


class CopyMoveAdapter:
    """`run_batch`'s adapter for copy, move and restore (#512): one per call.

    A restore to several places (``multi``) has one group per item, each checked and summarized
    as its own call was before the runner: the destination check, the overwrite lookups, the
    summary. What the call did so far (``done``, ``skipped``, ``went``, ``seen``) spans groups.
    """

    api = "SYNO.FileStation.CopyMove"

    def __init__(
        self,
        client: DsmClient,
        items: Sequence[Item],
        *,
        operation: str,
        overwrite: bool,
        remove_src: bool,
        check_sources: bool,
        paths_arg: Sequence[str],
        fresh: Sequence[str] = (),
        multi: bool = False,
        share_name: str = "",
    ) -> None:
        self.client = client
        self.items = list(items)
        self.operation = operation
        self.kind: HandleKind = _KIND[operation]
        self.overwrite = overwrite
        self.remove_src = remove_src
        self.check_sources = check_sources
        self.paths_arg = paths_arg
        self.fresh = fresh
        self.multi = multi
        self.share_name = share_name
        self._version = 2
        self._track: Callable[[RunningTask], None] | None = None
        # The call's state, across groups.
        self.done: list[str] = []
        self.skipped: list[str] = []
        self.went: dict[str, str] = {}
        # What each source's name met at the destination (#620), kept across a restore's groups
        # so a later failure names what earlier ones replaced.
        self.seen: dict[str, str] = {}
        self.texts: list[str] = []
        # The current group's.
        self.group: list[str] = []
        self.dest = ""
        self.total = 0
        self.skipped_paths: list[str] = []
        self.landed_here: list[str] = []
        self.replaced: dict[str, str] = {}

    @property
    def version(self) -> int:
        return self._version

    @contextmanager
    def batch_scope(self) -> Iterator[None]:
        # One record of what this call lands and starts, until it returns (#597); inside a
        # restore's own, that one.
        with tracked_landings(self.client) as track:
            self._track = track
            yield

    def track(self, task: RunningTask) -> None:
        assert self._track is not None
        self._track(task)

    async def begin_group(self, items: Sequence[Item]) -> None:
        client, operation = self.client, self.operation
        normalized = [x.source for x in items]
        dest = items[0].dest
        assert dest is not None
        # A restore's later destination goes on after a failed check, so finished items
        # aren't lost (#588).
        anything_done = self.multi and bool(self.done or self.skipped)
        self.group, self.dest = normalized, dest
        self.total, self.skipped_paths, self.landed_here, self.replaced = 0, [], [], {}

        # Pin to version 2 — v3 uses JSON request format with different
        # parameter encoding that our path-as-form-param shape doesn't support.
        self._version = min(2, client.negotiate_version("SYNO.FileStation.CopyMove", max_version=2))

        # With overwrite, which sources' names are already at the destination, so the reply can
        # say what was replaced or merged into (#620, 0.11.0rc3 live test: an overwrite read like
        # a plain copy). It runs alongside the source and destination checks, never after them:
        # run after, it delayed this call's landing records and let a parallel call start a task
        # this one replaced (#597; pre-QA on #623). Not a target that is the source itself, nor
        # one in or at a folder this call re-created (`fresh`), nor one an earlier item of the
        # call landed: those weren't there before. A lookup that fails is listed as unchecked.
        def made_here(path: str) -> bool:
            """``path`` is, or is inside, a folder this call made (``fresh`` lists the parents it
            made too, and only those: QA round 1 on #623)."""
            return any(f == path or path.startswith(f + "/") for f in self.fresh)

        targets: dict[str, str] = {}
        if self.overwrite:
            targets = {
                q: t
                for q in normalized
                if (t := f"{dest}/{q.rsplit('/', 1)[-1]}") != q and not made_here(t)
            }
        # One request per target, started before the checks below and awaited after this call's
        # landing records and before its first start, so the records aren't delayed (#597;
        # pre-QA on #623 measured a parallel call slipping in when they were) and the lookups
        # can't see this call's own landing. At most 4 at once: an overwrite of hundreds of items
        # mustn't burst requests at DSM ahead of the checks that share the connection pool
        # (pre-QA r2).
        gate = asyncio.Semaphore(4)

        async def look(source: str, target: str) -> dict[str, bool]:
            # The source's kind too: a file onto a same-named folder isn't a merge (QA round 1).
            async with gate:
                return await paths_isdir(client, [target, source])

        lookups = {q: asyncio.ensure_future(look(q, t)) for q, t in targets.items()}
        try:
            if self.check_sources:
                await _refuse_missing_sources(client, operation, normalized, self.paths_arg)
            await _refuse_missing_destination(client, operation, dest, anything_done=anything_done)
            # What this call will land, before it starts anything, so a parallel call sees all of
            # it (QA round 2 on #597).
            for q in normalized:
                self.track(
                    pending_landing(
                        self.kind, q, dest, overwrite=self.overwrite, remove_src=self.remove_src
                    )
                )
            # What was there, read before the first start: answered after it, a lookup would see
            # this call's own landing (pre-QA r2 on #623). The records are published by now, so
            # waiting here doesn't open #597's window.
            self.replaced = await _replaced(lookups, targets)
        finally:
            for future in lookups.values():
                # Stops one still waiting; on one that failed, cancel() also marks its exception
                # as seen, so asyncio never logs it as never retrieved.
                future.cancel()
        self.seen.update(self.replaced)

    def start_guard(self, item: Item) -> AbstractAsyncContextManager[Any]:
        # Held from before the check until the start returns, and not across the wait: a
        # parallel call for this destination then finds this task in the list (#587). One lock
        # at a time, so calls can't deadlock.
        return self.client.destination_lock(self.dest)

    async def before_start(self, item: Item, read_timeout: float) -> dict[str, Any] | None:
        p = item.source
        at = self.group.index(p)
        # A copy of the same source to another destination is a different operation; a
        # move (or restore) of a source that is already moving anywhere is not.
        match: dict[str, Any] = {"remove_src": self.remove_src}
        if not self.remove_src:
            match["dest_folder_path"] = self.dest
        listed, tasks = await tasks_for_landing_check(self.client, read_timeout=read_timeout)
        _refuse_landing_on_a_running_task(
            tasks,
            self.operation,
            self.group[at:],
            self.dest,
            overwrite=self.overwrite,
            remove_src=self.remove_src,
            anything_done=bool(self.done or self.skipped),
        )
        # DSM's list only: a parallel call's finished task is nothing to hand back.
        return find_running(listed, api=self.api, path=p, params=match)

    def start_params(self, item: Item) -> dict[str, str]:
        return {
            "path": item.source,
            "dest_folder_path": self.dest,
            "overwrite": str(self.overwrite).lower(),
            "remove_src": str(self.remove_src).lower(),
        }

    def started(self, item: Item, taskid: str) -> None:
        self.track(
            RunningTask(
                Handle(self.kind, taskid),
                [item.source],
                {
                    "dest_folder_path": self.dest,
                    "overwrite": self.overwrite,
                    "remove_src": self.remove_src,
                },
                {"local": True},
            )
        )

    def label(self, item: Item) -> str:
        return f"{_PRESENT[self.operation]} {item.source}"

    def read_finished(self, item: Item, status: dict[str, Any]) -> tuple[int, bool]:
        # What the call did before is the failure formatter's to add (#482).
        return _copy_move_result(status, item.source, self.operation, dest=self.dest)

    def lost_check(self, item: Item) -> None:
        return None

    def item_done(self, item: Item, result: Any) -> None:
        size, skipped = result
        p = item.source
        self.total += size
        self.went[p] = self.dest
        if skipped:
            self.skipped_paths.append(p)
            self.skipped.append(p)
        else:
            self.landed_here.append(p)
            self.done.append(p)

    def _task_dest(self, outcome: ItemOutcome) -> tuple[str, bool]:
        """Where the handed-off task lands, and with what overwrite: a task already running
        may go elsewhere."""
        task_dest, task_overwrite = self.dest, self.overwrite
        if outcome.running is not None:
            started = outcome.running.get("params") or {}
            if isinstance(started.get("dest_folder_path"), str):
                task_dest = started["dest_folder_path"]
            if isinstance(started.get("overwrite"), bool):
                task_overwrite = started["overwrite"]
        return task_dest, task_overwrite

    def _placed(self, outcome: ItemOutcome, task_dest: str) -> list[str]:
        # A task already running to another folder lands nothing here.
        p = outcome.item.source
        return [*self.done, p] if task_dest == self.dest else list(self.done)

    def record_extras(self, outcome: ItemOutcome) -> dict[str, Any]:
        task_dest, task_overwrite = self._task_dest(outcome)
        return {
            "paths": [outcome.item.source],
            "dest": task_dest,
            "overwrite": task_overwrite,
            # Only what landed, and only for the destination this call asked for.
            "earlier": self.landed_here if task_dest == self.dest else [],
            "skipped_earlier": list(self.skipped),
            "done_earlier": list(self.done),
            "landed_earlier": self._placed(outcome, task_dest),
            "dest_of": dict(self.went),
        }

    async def end_group(self, items: Sequence[Item], wait: float) -> None:
        # ``items``: the group's items that ran, all of them unless the call's wait ran out
        # partway (#496), so a summary never names one that wasn't started.
        ran = [x.source for x in items]
        cut_short = len(ran) < len(self.group)
        self.group = [p for p in self.group if p in ran]
        group = list(self.group)
        # A restore's earlier items at this destination; for any other call, none.
        earlier = [
            q
            for q in [*self.done, *self.skipped]
            if q not in group and self.went.get(q) == self.dest
        ]

        def make(probe: bool) -> Awaitable[str]:
            return _copy_move_summary(
                self.client,
                group,
                self.dest,
                self.remove_src,
                self.total,
                self.skipped_paths,
                # Cut short by the call's wait: only the named sources went (pre-QA r2).
                only_named_sources_removed=self.multi or cut_short,
                earlier=earlier,
                replaced=self.replaced,
                probe=probe,
            )

        text = await bounded_summary(make, wait, f"{self.operation} to {self.dest}")
        if self.operation == "Restore" and self.share_name:
            # Rephrase from "Moved" to "Restored"
            text = _as_restore(text, self.share_name)
        self.texts.append(text)

    async def summary(self, batch: BatchOutcome) -> str:
        results = list(self.texts)
        if not self.multi:
            return results[0]
        partial = [r for r in results if r.startswith("[!]")]
        if partial:
            results.insert(
                0,
                format_status(
                    f"Restored {len(results) - len(partial):,} of {len(results):,} item(s) "
                    f"from /{self.share_name} recycle bin; {len(partial):,} with skips (that name "
                    "is already at the destination, overwrite=false; details below):",
                    success=False,
                ),
            )
        return "\n".join(results)

    def handoff_text(self, outcome: ItemOutcome, batch: BatchOutcome) -> str:
        assert outcome.handoff is not None
        p = outcome.item.source
        task_dest, _ = self._task_dest(outcome)
        placed = self._placed(outcome, task_dest)
        reply = handoff_reply(
            verb=f"Still {_PRESENT[self.operation].lower()}",
            path=p,
            handle=outcome.handoff.handle,
            status=outcome.handoff.status,
            stall=outcome.handoff.stall,
            dest=task_dest,
            done=self.done,
            skipped=self.skipped,
            landed=placed,
            dest_of=self.went,
            not_started=[x.source for x in batch.not_started()],
        )
        if outcome.running is not None:
            other = task_dest if task_dest != self.dest else None
            reply = _already_running(self.operation, p, other) + "\n" + reply
        # A skipped folder lands its other items, and a hand-off can't tell it from a file.
        ran = {name_key(path) for path in [*placed, *self.skipped]}
        alike = [q for q in self.group[self.group.index(p) + 1 :] if name_key(q) in ran]
        if alike:
            # #580: running them with overwrite=true would replace what it placed.
            # One a line when two or more (#693).
            named = path_list(
                "Named like one this call already ran", [escaped_repr(q) for q in alike]
            )
            reply += "\n" + then(
                named,
                "Run them with overwrite=false or send them to another folder: with "
                "overwrite=true they could replace it.",
            )
        if not self.multi:
            return reply
        results = list(self.texts)
        earlier_skips = sum(x.startswith("[!]") for x in results)
        if earlier_skips:
            # The first line is what gets read, as it is for a finished restore.
            results.insert(
                0,
                format_status(
                    f"Restored {len(results) - earlier_skips:,} of "
                    f"{len(self.items):,} item(s) from /{self.share_name} recycle bin "
                    f"so far; {earlier_skips:,} with skips (that name is already at "
                    "the destination, "
                    "overwrite=false; details below):",
                    success=False,
                ),
            )
        results.append(reply)
        return "\n".join(results)

    def cause(self, outcome: ItemOutcome) -> NoReturn:
        operation, p = self.operation, outcome.item.source
        e = outcome.error
        if outcome.state == "may_have_started":
            assert isinstance(e, httpx.TransportError)
            # The start got no answer: it may have started, so it isn't retryable even when
            # nothing else ran (#452; the maintainer's Q4 on #512; #588).
            error_response(
                ErrorCode.TIMEOUT
                if isinstance(e, httpx.TimeoutException)
                else ErrorCode.UNAVAILABLE,
                f"{operation} files failed: DSM didn't answer for {shown(p)} "
                f"({describe_transport_error(e)}); its {operation.lower()} may have started.",
                retryable=False,
                param="paths",
                value=p,
                # Where it may have left something, not its source: a copy doesn't change it
                # (pre-QA r4 on #687).
                # Only the first letter: str.capitalize() lower-cases the rest, paths included
                # (QA round 2 on #687).
                suggestion=f"{_upper_first(self.check_clause(outcome.item))}, or file_task with "
                "no handle, before running it again.",
            )
        if isinstance(e, ToolError):
            raise e
        assert isinstance(e, SynologyError)
        synology_error_response(f"{operation} files", e)

    def progress_text(self, outcome: ItemOutcome) -> str:
        return _already_done(
            self.operation,
            self.done,
            self.skipped,
            dest_of=self.went,
            dest=self.dest,
            failing=outcome.item.source,
        )

    def check_clause(self, item: Item) -> str:
        # A copy's source doesn't change: what it may have left is at the destination (pre-QA
        # r3 on #687); a move's or restore's may be in either.
        dest = shown(item.dest or self.dest)
        if self.remove_src:
            # A name holding " and " is quoted, so it can't read as two (pre-QA r1 on #693).
            return (
                f"check {shown_beside(item.source, ' and ')} and "
                f"{shown_beside(item.dest or self.dest, ' and ')} with list_files"
            )
        return f"check {dest} with list_files"

    def retry_advice(self) -> str:
        past = _PAST_TENSE.get(self.operation, "processed")
        if self.remove_src:
            return f"the paths already {past} (they're no longer where they were)"
        return f"the paths already {past}"

    def amend(self, error: ToolError) -> ToolError:
        # Which done ones replaced something (pre-QA r4 on #623).
        return _with_replaced_note(
            error, self.operation, self.done, self.seen, self.went, self.dest
        )


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
    replaced: dict[str, str] | None = None,
    probe: bool = True,
) -> str:
    """The result text of a finished copy or move (a restore rewords it, ``_as_restore``).
    ``replaced``: sources whose name was already at the destination (with overwrite), and
    whether that was a folder (#620). ``probe``: False when the call's wait is over, so a
    skipped source's kind isn't read (`bounded_summary`)."""
    verb = "Copied" if not remove_src else "Moved"
    ok = [p for p in normalized if p not in skipped_paths]

    def name(p: str) -> str:
        return p.split("/")[-1]

    if not skipped_paths:
        lines = [format_status(f"{verb} {len(normalized):,} item(s) to {shown(dest)}/:")]
        lines.extend(f"  {shown(name(p))}" for p in normalized)
    else:
        lines = await _skip_report(
            client, verb, dest, normalized, ok, skipped_paths, remove_src, earlier, probe=probe
        )

    met = {p: kind for p, kind in (replaced or {}).items() if p in ok}

    def listed(heading: str, kinds: tuple[str, ...]) -> bool:
        # One name a line, as the copied block lists them: a name holding ", " can't be read
        # as two (QA round 1 on #623); shown() makes padding visible.
        names = [shown(name(p)) for p, kind in met.items() if kind in kinds]
        if names:
            lines.append(f"\n{heading}")
            lines.extend(f"  {n}" for n in names)
        return bool(names)

    replaced_any = listed("Replaced what was already there:", (_REPLACED_FILE,))
    merged_any = listed(
        "Merged into the folder(s) already there, replacing files with the same names:",
        (_MERGED,),
    )
    listed(
        "Already there as the other kind (a file for a folder, or a folder for a file); what "
        "DSM did with it isn't known:",
        (_FILE_THERE, _FOLDER_THERE),
    )
    listed("Couldn't check whether anything was already there for:", (_UNCHECKED,))
    if replaced_any or merged_any:
        # As the tools' descriptions warn: vdsm's /home kept no copy (pre-QA r3 on #623).
        lines.append("A file replaced this way may not be in the recycle bin.")

    if total_processed > 0:
        lines.append(f"\nTotal size: {format_size(total_processed)}")

    # With a skip, or from a restore to several places or file_task's final result, whose other
    # sources may still be in that folder, it names what went instead (#585).
    if remove_src and not skipped_paths and not only_named_sources_removed:
        src_dirs = sorted({"/".join(p.split("/")[:-1]) for p in normalized})
        lines.extend(f"\nSource files have been removed from {shown(d)}/." for d in src_dirs)
    elif remove_src:
        by_dir: dict[str, list[str]] = {}
        for p in ok:
            by_dir.setdefault("/".join(p.split("/")[:-1]), []).append(name(p))
        # One a line when two or more (#693).
        lines.extend(
            "\n" + path_list(f"Moved sources removed from {shown(d)}/", [shown(n) for n in names])
            for d, names in sorted(by_dir.items())
        )

    return "\n".join(lines)


class DeleteAdapter:
    """`run_batch`'s adapter for delete_files (#512): one per call, one group."""

    api = "SYNO.FileStation.Delete"
    kind: HandleKind = "delete"

    def __init__(
        self,
        client: DsmClient,
        normalized: list[str],
        *,
        paths_arg: Sequence[str],
        recursive: bool,
        recycle_bin_status: dict[str, bool] | None,
    ) -> None:
        self.client = client
        self.normalized = normalized
        self.paths_arg = paths_arg
        self.recursive = recursive
        self.recycle_bin_status = recycle_bin_status
        self._version = 2
        self.deleted: list[str] = []

    @property
    def version(self) -> int:
        return self._version

    def batch_scope(self) -> AbstractContextManager[Any]:
        return no_scope()

    async def begin_group(self, items: Sequence[Item]) -> None:
        client = self.client
        # Refuse the whole call if any path is missing (#162): DSM "deletes" a missing path
        # without an error, and a partial delete of a mistyped list is worse than none.
        try:
            missing = await find_missing_paths(client, self.normalized)
            under = await files_above(client, missing)  # a path under a file (#702)
        except SynologyError as e:
            synology_error_response("Delete files", e)
        if missing:
            err_code, suggestion = unreadable_paths_error(missing, under)
            error_response(
                err_code,
                # One a line when two or more (#693).
                then(
                    path_list(
                        "Delete files refused",
                        [missing_text(p, c, under.get(p)) for p, c in missing],
                    ),
                    "Nothing was deleted.",
                ),
                retryable=False,
                param="paths",
                value=self.paths_arg,
                suggestion=suggestion,
            )
        # Pin to v2 — v3 uses JSON request format with different parameter encoding.
        self._version = min(2, client.negotiate_version("SYNO.FileStation.Delete", max_version=2))

    def start_guard(self, item: Item) -> AbstractAsyncContextManager[Any]:
        return contextlib.nullcontext()

    async def before_start(self, item: Item, read_timeout: float) -> dict[str, Any] | None:
        # Never start a second delete of a path that already has one running (#245).
        return await running_task(
            self.client, api=self.api, path=item.source, read_timeout=read_timeout
        )

    def start_params(self, item: Item) -> dict[str, str]:
        return {
            "path": item.source,
            "recursive": str(self.recursive).lower(),
            "accurate_progress": "true",
        }

    def started(self, item: Item, taskid: str) -> None:
        return None

    def label(self, item: Item) -> str:
        return f"Deleting {item.source}"

    def read_finished(self, item: Item, status: dict[str, Any]) -> None:
        _delete_result(status, item.source, recursive=self.recursive)

    def lost_check(self, item: Item) -> Awaitable[bool | None]:
        # DSM can't say how it ended, but the path can: gone is what was asked for (#521).
        # Bounded: a stalled DSM mustn't hold the reply and its handle (#245).
        return asyncio.wait_for(path_exists(self.client, item.source), _LOST_DELETE_CHECK)

    def item_done(self, item: Item, result: Any) -> None:
        self.deleted.append(item.source)

    def record_extras(self, outcome: ItemOutcome) -> dict[str, Any]:
        p = outcome.item.source
        # No DSM request between the handoff and the reply: a recycle-bin probe to a
        # DSM stalled by this very delete would lose the handle (#245). Keep only a
        # cached answer; the final result probes lazily (_delete_summary).
        recycle: dict[str, bool] = {}
        parts = p.split("/")
        share_name = parts[1] if len(parts) > 1 else ""
        in_bin = bin_index(parts[1:]) is not None
        status = self.recycle_bin_status
        if status is not None and not in_bin and share_name in status:
            recycle[share_name] = status[share_name]
        return {"paths": [p], "recursive": self.recursive, "recycle": recycle}

    async def end_group(self, items: Sequence[Item], wait: float) -> None:
        return None

    async def summary(self, batch: BatchOutcome) -> str:
        # Only what ran: the call's wait may have run out before the rest (#496).
        ran = [o.item.source for o in batch.items if o.state == "done"]

        def make(probe: bool) -> Awaitable[str]:
            return _delete_summary(self.client, ran, self.recycle_bin_status, probe=probe)

        wait = batch.budget.for_check() if batch.budget is not None else None
        return await bounded_summary(make, wait, "Delete")

    def handoff_text(self, outcome: ItemOutcome, batch: BatchOutcome) -> str:
        assert outcome.handoff is not None
        p = outcome.item.source
        reply = handoff_reply(
            verb="Still deleting",
            path=p,
            handle=outcome.handoff.handle,
            status=outcome.handoff.status,
            stall=outcome.handoff.stall,
            done=self.deleted,
            not_started=[x.source for x in batch.not_started()],
        )
        return reply if outcome.running is None else _already_running("Delete", p) + "\n" + reply

    def cause(self, outcome: ItemOutcome) -> NoReturn:
        p = outcome.item.source
        e = outcome.error
        if outcome.state == "may_have_started":
            assert isinstance(e, httpx.TransportError)
            # The start got no answer (a status with none hands the item off in poll_task): it
            # may have started, so it isn't retryable even when nothing else ran (#452; the
            # maintainer's Q4 on #512; QA round 1 on #480).
            error_response(
                ErrorCode.TIMEOUT
                if isinstance(e, httpx.TimeoutException)
                else ErrorCode.UNAVAILABLE,
                f"Delete files failed: DSM didn't answer for {shown(p)} "
                f"({describe_transport_error(e)}); its delete may have started.",
                retryable=False,
                param="paths",
                value=p,
                # Named, as copy's names where to look (pre-QA r5 on #687).
                suggestion=f"{_upper_first(self.check_clause(outcome.item))}, or file_task with "
                "no handle, before deleting again.",
            )
        if isinstance(e, ToolError):
            raise e
        assert isinstance(e, SynologyError)
        synology_error_response("Delete files", e)

    def progress_text(self, outcome: ItemOutcome) -> str:
        return _already_done("Delete", self.deleted)

    def check_clause(self, item: Item) -> str:
        return f"check {shown(item.source)} with list_files"

    def retry_advice(self) -> str:
        # The same call again is refused (the deleted paths are missing): only the items not
        # deleted can be retried (QA round 2 on #480).
        return "the paths already deleted (the same call would be refused)"

    def amend(self, error: ToolError) -> ToolError:
        return error


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
            f"Delete files failed: DSM error code {err_code} on path: {shown(str(err_path))}.",
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
            # One a line when two or more failed (#693).
            path_list(
                "Delete files failed", [f"{shown(p)} (DSM error {code})" for p, code in failed]
            ),
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
    _refuse_no_paths("Delete", paths)
    # A delete lands nowhere: its plan only says which sources the batch may run (#684).
    sources = [normalize_path(p) for p in paths]
    plan = plan_deletes(sources)
    normalized = _collapse_overlaps(sources)
    adapter = DeleteAdapter(
        client,
        normalized,
        paths_arg=paths,
        recursive=recursive,
        recycle_bin_status=recycle_bin_status,
    )
    items = [Item(p, None) for p in normalized]
    outcome = await run_batch(
        client,
        adapter,
        plan,
        items,
        budget=Budget(timeout),
        records=records,
        on_progress=on_progress,
    )
    return await batch_reply(adapter, outcome)


async def _delete_summary(
    client: DsmClient,
    normalized: list[str],
    recycle_bin_status: dict[str, bool] | None,
    *,
    probe: bool = True,
) -> str:
    """The result text of a finished delete_files call. ``probe``: False when the call's wait
    is over (`bounded_summary`): a share whose bin setting isn't known yet is reported as one
    DSM didn't answer for, never guessed."""
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
    shares_late: set[str] = set()  # not checked: the call's wait was over (`bounded_summary`)
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
        elif not probe:
            homes_answered = False
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
            if not probe and share_name not in recycle_bin_status:
                shares_late.add(share_name)
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
        shares_with_recycle
        or shares_unknown
        or shares_late
        or shares_unclear
        or kept_bins
        or unknown_bins
    )
    verb = "Deleted" if has_recycle else "Permanently deleted"

    lines = [format_status(f"{verb} {len(normalized):,} item(s):")]
    # A list: padding and characters that draw nothing are shown, as errors show them (#519).
    lines.extend(f"  {shown(p)}" for p in normalized)

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
            lines.extend(f"  {shown(kept + p[len(home_bin) :])}" for p in bin_paths)
            continue
        if share in unknown_bins:
            lines.append(
                f"\nCouldn't check whether /homes has its recycle bin enabled: if it is, these "
                f"went from {user}'s home recycle bin into {kept}, and list_recycle_bin "
                "share=homes shows whether they can be recovered:"
            )
            lines.extend(f"  {shown(p)}" for p in bin_paths)
            continue
        # Name the bin items only when the call also deleted others, so it's clear
        # which ones are gone for good.
        if outside_bin:
            lines.append(
                f"\n\u26a0 Deleted from the recycle bin on /{share} \u2014 permanent, "
                "these cannot be recovered:"
            )
            lines.extend(f"  {shown(p)}" for p in bin_paths)
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
    # Cut short by this server, not DSM: the call's wait was over (pre-QA r4 on #687).
    lines.extend(
        f"\nDidn't check whether /{share} has its recycle bin enabled (the call's wait was over); "
        "list_recycle_bin shows whether these files can be recovered."
        for share in sorted(shares_late)
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
    # Normalized once, so the plan's landings and the items' are the same text (pre-QA r2 on
    # #684: `/video/Restored/` planned one landing and ran another).
    # An empty one is no destination (each item goes back where it was), not the root (pre-QA
    # r3 on #694: normalize_path("") is "/").
    dest_folder = ResolvedPath(normalize_path(dest_folder)) if dest_folder else None
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
    # Before any DSM request, as the handler ran them (#684): the refusals' order is kept.
    plan = plan_landings(
        "restore_from_recycle_bin",
        paths,
        full_paths,
        restore_destinations,
        overwrite=overwrite,
        dest_given=dest_folder is not None,
    )

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
                anything_done=False,
            )
        recreated, made = (
            ([], [])
            if dest_folder
            else await _recreate_missing_folders(client, restore_destinations)
        )
        # One a line when two or more (#693).
        note = ""
        if len(recreated) == 1:
            note = f"\nRe-created {shown(recreated[0])} (no longer there) to restore into."
        elif recreated:
            note = "\n" + path_list(
                "Re-created to restore into (they were no longer there)",
                [shown(p) for p in recreated],
            )

        if dest_folder is None and len(set(restore_destinations)) > 1:
            # Multiple different destinations: one batch, an item and a group per destination's
            # item, each checked and summarized as its own (#512).
            items = [
                Item(normalize_path(fp), normalize_path(dest), group=i)
                for i, (fp, dest) in enumerate(zip(full_paths, restore_destinations, strict=False))
            ]
            adapter = CopyMoveAdapter(
                client,
                items,
                operation="Restore",
                overwrite=overwrite,
                remove_src=True,
                check_sources=False,
                paths_arg=paths,
                fresh=made,
                multi=True,
                share_name=share_name,
            )
            outcome = await run_batch(
                client,
                adapter,
                plan,
                items,
                budget=Budget(timeout),
                records=records,
                on_progress=on_progress,
            )
            return await batch_reply(adapter, outcome, note)

        # Use the first destination (or provided dest_folder)
        actual_dest = dest_folder or restore_destinations[0]
        result, _ = await _copy_move(
            client,
            plan=plan,
            paths=full_paths,
            dest_folder=actual_dest,
            overwrite=overwrite,
            remove_src=True,
            operation="Restore",
            timeout=timeout,
            check_sources=False,
            records=records,
            on_progress=on_progress,
            fresh=made,
            share_name=share_name,
        )
        return result + note


async def _recreate_missing_folders(
    client: DsmClient, folders: list[str]
) -> tuple[list[str], list[str]]:
    """Create the inferred restore destinations that no longer exist; return them, and every
    folder the call made, the missing parents ``force_parent`` created along the way too.

    A file restored out of a folder that was deleted with it has no original folder to
    go back to, and DSM refuses that move with 1002 "Destination error" (DSM 7.1 and
    7.3.2, #333). The spec always said a missing parent is re-created; this does it,
    deepest first, since ``force_parent`` creates the folders above it. The parents are
    looked up with the destinations, so a parent that was already there isn't counted as
    made (QA round 1 on #623: its overwrite note was dropped).
    """
    wanted = sorted(set(folders))
    parents = sorted(
        {
            "/".join(path.split("/")[:i])
            for path in wanted
            for i in range(3, path.count("/") + 1)  # below the share: /<share>/a, ...
        }
        - set(wanted)
    )
    try:
        found = await find_missing_paths(client, wanted + parents)
    except SynologyError as e:
        synology_error_response("Restore files", e)
    # A destination this account can't use is refused before anything moves: found only at its
    # item's turn, it came after earlier items were restored (#482).
    denied = [path for path, code in found if code == 407 and path in wanted]
    if denied:
        error_response(
            ErrorCode.PERMISSION_DENIED,
            # One a line when two or more (#693).
            f"Restore files refused: no permission to use the original folder {shown(denied[0])}. "
            "Nothing was restored."
            if len(denied) == 1
            else then(
                path_list(
                    "Restore files refused: no permission to use the original folders",
                    [shown(p) for p in denied],
                ),
                "Nothing was restored.",
            ),
            retryable=False,
            param="dest_folder",
            value=denied[0],
            suggestion=(
                "Restore them to a folder this DSM account can write to (dest_folder), or have "
                "the folder's permissions changed in DSM."
            ),
        )
    # An original folder under a file is refused before anything moves too: found only at its
    # item's turn, it came after earlier items were restored (QA r1 on #714).
    try:
        under = await files_above(client, found)
    except SynologyError as e:
        synology_error_response("Restore files", e)
    blocked = [path for path in wanted if path in under]
    if blocked:
        texts = [under_file_text(path, under[path]) for path in blocked]
        error_response(
            ErrorCode.NOT_FOUND,
            # One a line when two or more (#693).
            f"Restore files refused: the original folder {texts[0]}. Nothing was restored."
            if len(texts) == 1
            else then(
                path_list("Restore files refused: original folders under a file", texts),
                "Nothing was restored.",
            ),
            retryable=False,
            param="dest_folder",  # as the 407 refusal above: where it would restore to
            value=blocked[0],
            suggestion=RESTORE_UNDER_FILE_ADVICE,
        )
    gone = {path for path, code in found if code == 408}
    missing = [path for path in wanted if path in gone]
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
    made = set(created) | {
        parent
        for parent in parents
        if parent in gone and any(path.startswith(parent + "/") for path in created)
    }
    return sorted(created), sorted(made)
