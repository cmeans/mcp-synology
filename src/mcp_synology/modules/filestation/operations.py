"""File Station operations: create_folder, rename, copy, move, delete, restore."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from mcp_synology.core.errors import ErrorCode, SynologyError, error_from_code
from mcp_synology.core.formatting import (
    error_response,
    format_size,
    format_status,
    synology_error_response,
)
from mcp_synology.modules.filestation.helpers import (
    dsm_str,
    ensure_recycle_status,
    find_missing_paths,
    missing_paths_suggestion,
    normalize_path,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)


async def _stop_background_task(
    client: DsmClient,
    api: str,
    taskid: str,
    version: int,
    task_logger: logging.Logger,
) -> None:
    """Stop a background task (CopyMove, Delete). Logs warnings on failure."""
    try:
        await client.request(api, "stop", version=version, params={"taskid": taskid})
    except SynologyError as e:
        task_logger.warning("%s task cleanup failed for %s: %s", api, taskid, e)
    except Exception:
        task_logger.warning("%s task cleanup failed for %s", api, taskid, exc_info=True)


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

    lines = [format_status(f"Created {len(folders)} folder(s):")]
    for f in folders:
        path = f.get("path", "")
        lines.append(f"  \U0001f4c1 {path}")

    return "\n".join(lines)


async def rename(
    client: DsmClient,
    *,
    path: str,
    new_name: str,
) -> str:
    """Rename a file or folder."""
    normalized = normalize_path(path)

    # Validate new_name is just a name, not a path
    if "/" in new_name:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            "Rename failed: new_name should be just a filename, not a path.",
            retryable=False,
            param="new_name",
            value=new_name,
            suggestion="Use move_files to relocate files to a different directory.",
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
) -> str:
    """Copy files or folders to a destination."""
    return await _copy_move(
        client,
        paths=paths,
        dest_folder=dest_folder,
        overwrite=overwrite,
        remove_src=False,
        operation="Copy",
        timeout=timeout,
    )


async def move_files(
    client: DsmClient,
    *,
    paths: list[str],
    dest_folder: str,
    overwrite: bool = False,
    timeout: float = 120.0,
) -> str:
    """Move files or folders to a new location."""
    return await _copy_move(
        client,
        paths=paths,
        dest_folder=dest_folder,
        overwrite=overwrite,
        remove_src=True,
        operation="Move",
        timeout=timeout,
    )


_PAST_TENSE = {"Copy": "copied", "Move": "moved", "Restore": "restored", "Delete": "deleted"}


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
) -> tuple[int, bool]:
    """Copy or move a single path via DSM async-task pattern.

    Returns the `processed_size` byte count from the completed task (and whether DSM
    skipped items that already existed at the destination, #171) so the
    caller can sum sizes across all per-path tasks. Raises ToolError on
    failure (via error_response/synology_error_response). Always stops the
    background task on exit so orphaned tasks can't accumulate on the NAS.
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

    elapsed = 0.0
    interval = 0.5
    status: dict[str, Any] = {}
    poll_error: SynologyError | None = None
    timed_out = False

    try:
        while elapsed < timeout:
            try:
                status = await client.request(
                    "SYNO.FileStation.CopyMove",
                    "status",
                    version=copymove_version,
                    params={"taskid": taskid},
                )
            except SynologyError as e:
                poll_error = e
                break

            logger.debug("%s status (%s): %s", operation, path, status)

            if status.get("finished", False):
                break

            await asyncio.sleep(interval)
            elapsed += interval
        else:
            timed_out = True
    finally:
        await _stop_background_task(
            client,
            "SYNO.FileStation.CopyMove",
            taskid,
            copymove_version,
            logger,
        )

    if poll_error:
        synology_error_response(f"{operation} files", poll_error)
    if timed_out:
        error_response(
            ErrorCode.TIMEOUT,
            f"{operation} files failed: timed out after {timeout}s on path: {path}",
            retryable=True,
            param="timeout",
            value=timeout,
            suggestion="The operation may still be running on the NAS.",
        )

    if "error" in status:
        err = status["error"]
        err_code = err.get("code", 0) if isinstance(err, dict) else err
        err_path = status.get("path", path)
        mapped = error_from_code(err_code, "SYNO.FileStation.CopyMove")
        error_response(
            mapped.error_code,
            f"{operation} files failed: DSM error code {err_code} on path: {err_path}"
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
            + "; ".join(f"{p} (DSM error {code})" for p, code in failed)
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
) -> str:
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
    dest = normalize_path(dest_folder)

    # Pin to version 2 — v3 uses JSON request format with different
    # parameter encoding that our path-as-form-param shape doesn't support.
    copymove_version = min(2, client.negotiate_version("SYNO.FileStation.CopyMove", max_version=2))

    if check_sources:
        await _refuse_missing_sources(client, operation, normalized, paths)

    total_processed = 0
    done = list(already_done or [])
    skipped_paths: list[str] = []
    for p in normalized:
        size, skipped = await _copy_move_one_path(
            client,
            p,
            dest=dest,
            overwrite=overwrite,
            remove_src=remove_src,
            operation=operation,
            copymove_version=copymove_version,
            timeout=timeout,
            done=done,
        )
        total_processed += size
        if skipped:
            skipped_paths.append(p)
        done.append(p)

    verb = "Copied" if not remove_src else "Moved"
    ok = [p for p in normalized if p not in skipped_paths]

    def name(p: str) -> str:
        return p.split("/")[-1]

    if not skipped_paths:
        lines = [format_status(f"{verb} {len(normalized)} item(s) to {dest}/:")]
        lines.extend(f"  {name(p)}" for p in normalized)
    else:
        # DSM only says that it skipped something for a source (a whole file, or some of
        # a folder's children), not what, so each such source is listed separately (#171).
        lines = [
            format_status(
                f"{verb} {len(ok)} of {len(normalized)} item(s) to {dest}/; "
                f"{len(skipped_paths)} skipped in whole or in part (already at the "
                "destination, overwrite=false):",
                success=False,
            )
        ]
        if ok:
            lines.append(f"{verb}:")
            lines.extend(f"  {name(p)}" for p in ok)
        lines.append("Skipped in whole or in part:")
        for p in skipped_paths:
            src_dir = "/".join(p.split("/")[:-1]) or "/"
            lines.append(
                f"  {p}" + (f" (skipped items are still in {src_dir}/)" if remove_src else "")
            )
        lines.append("Pass overwrite=true to replace them.")

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
    done: list[str] | None = None,
) -> None:
    """Delete a single path via DSM async-task pattern.

    Raises ToolError (via error_response/synology_error_response) on failure;
    returns None on success. Always stops the background task on exit.
    """
    try:
        start_data = await client.request(
            "SYNO.FileStation.Delete",
            "start",
            version=delete_version,
            params={
                "path": path,
                "recursive": str(recursive).lower(),
            },
        )
    except SynologyError as e:
        synology_error_response("Delete files", e)

    taskid = start_data.get("taskid", "")

    elapsed = 0.0
    interval = 0.5
    status: dict[str, Any] = {}
    poll_error: SynologyError | None = None
    timed_out = False

    try:
        while elapsed < timeout:
            try:
                status = await client.request(
                    "SYNO.FileStation.Delete",
                    "status",
                    version=delete_version,
                    params={"taskid": taskid},
                )
            except SynologyError as e:
                poll_error = e
                break

            logger.debug("Delete status (%s): %s", path, status)

            if status.get("finished", False):
                break

            await asyncio.sleep(interval)
            elapsed += interval
        else:
            timed_out = True
    finally:
        # DSM releases a finished Delete task itself: `stop` then always answers 401
        # (DSM 7.1 and 7.2.2), which logged a cleanup warning on every delete. Stop
        # only a task that didn't finish (timeout, polling error, cancellation).
        if not status.get("finished", False):
            await _stop_background_task(
                client,
                "SYNO.FileStation.Delete",
                taskid,
                delete_version,
                logger,
            )

    if poll_error:
        synology_error_response("Delete files", poll_error)
    if timed_out:
        error_response(
            ErrorCode.TIMEOUT,
            f"Delete files failed: timed out after {timeout}s on path: {path}",
            retryable=True,
            param="timeout",
            value=timeout,
            suggestion="The operation may still be running on the NAS.",
        )

    if "error" in status:
        err = status["error"]
        err_code = err.get("code", 0) if isinstance(err, dict) else err
        err_path = status.get("path", path)
        mapped = error_from_code(err_code, "SYNO.FileStation.Delete")
        error_response(
            mapped.error_code,
            f"Delete files failed: DSM error code {err_code} on path: {err_path}",
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
            + _already_done("Delete", done),
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
            f"Delete files failed: {path} was not found when the delete ran, so nothing "
            "was deleted for it" + (_already_done("Delete", done) or "."),
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
    for p in normalized:
        await _delete_one_path(
            client,
            p,
            recursive=recursive,
            delete_version=delete_version,
            timeout=timeout,
            done=deleted,
        )
        deleted.append(p)

    # Determine recycle bin status per share. Probes lazily on first observation
    # via `ensure_recycle_status` so the dict stays empty until we actually need
    # the answer; pre-#37 the dict was never populated and every share was
    # incorrectly reported as recycle-on. The dict is shared across all tool
    # handlers (closure-captured in modules/filestation/__init__.py) and cleared
    # on session re-auth (see `_register_recycle_status_invalidator` in __init__).
    shares_with_recycle: set[str] = set()
    shares_without_recycle: set[str] = set()
    if recycle_bin_status is None:
        # Defensive: caller passed nothing. Skip probing; preserve prior
        # default-true messaging for the touched shares.
        for p in normalized:
            parts = p.split("/")
            share_name = parts[1] if len(parts) > 1 else ""
            shares_with_recycle.add(share_name)
    else:
        seen: set[str] = set()
        for p in normalized:
            parts = p.split("/")
            share_name = parts[1] if len(parts) > 1 else ""
            if share_name in seen:
                continue
            seen.add(share_name)
            enabled = await ensure_recycle_status(client, share_name, recycle_bin_status)
            if enabled:
                shares_with_recycle.add(share_name)
            else:
                shares_without_recycle.add(share_name)

    has_recycle = bool(shares_with_recycle)
    verb = "Deleted" if has_recycle else "Permanently deleted"

    lines = [format_status(f"{verb} {len(normalized)} item(s):")]
    lines.extend(f"  {p}" for p in normalized)

    lines.extend(
        f"\nRecycle bin is enabled on /{share} \u2014 "
        f"files can be recovered with list_recycle_bin and restore_from_recycle_bin."
        for share in sorted(shares_with_recycle)
    )
    lines.extend(
        f"\n\u26a0 Recycle bin is NOT enabled on /{share} \u2014 these files cannot be recovered."
        for share in sorted(shares_without_recycle)
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
    )


async def restore_from_recycle_bin(
    client: DsmClient,
    *,
    share: str,
    paths: list[str],
    dest_folder: str | None = None,
    overwrite: bool = False,
    timeout: float = 120.0,
) -> str:
    """Restore files from a shared folder's recycle bin."""
    share_name = share.strip("/").split("/")[0]

    # Normalize recycle bin paths
    full_paths: list[str] = []
    restore_destinations: list[str] = []

    for p in paths:
        p = p.strip()
        # Ensure the path is within #recycle
        if f"/{share_name}/#recycle" in p:
            full_paths.append(normalize_path(p))
        elif p.startswith("#recycle"):
            full_paths.append(normalize_path(f"/{share_name}/{p}"))
        else:
            full_paths.append(normalize_path(f"/{share_name}/#recycle/{p}"))

        # Infer original path by stripping #recycle
        if dest_folder:
            restore_destinations.append(dest_folder)
        else:
            # /video/#recycle/Shows/Ep1.mkv -> /video/Shows/
            original = full_paths[-1].replace(f"/{share_name}/#recycle", f"/{share_name}")
            restore_destinations.append("/".join(original.split("/")[:-1]) or f"/{share_name}")

    await _refuse_missing_sources(client, "Restore", full_paths, paths)

    # Use the first destination (or provided dest_folder)
    actual_dest = dest_folder or restore_destinations[0]
    if dest_folder is None and len(set(restore_destinations)) > 1:
        # Multiple different destinations — move each one separately
        results: list[str] = []
        restored: list[str] = []
        for fp, dest in zip(full_paths, restore_destinations, strict=False):
            r = await _copy_move(
                client,
                paths=[fp],
                dest_folder=dest,
                overwrite=overwrite,
                remove_src=True,
                operation="Restore",
                timeout=timeout,
                check_sources=False,
                already_done=restored,
            )
            results.append(_as_restore(r, share_name))
            restored.append(fp)
        partial = [r for r in results if r.startswith("[!]")]
        if partial:
            results.insert(
                0,
                format_status(
                    f"Restored {len(results) - len(partial)} of {len(results)} item(s) from "
                    f"/{share_name} recycle bin; {len(partial)} skipped in whole or in part "
                    "(already at the destination, overwrite=false):",
                    success=False,
                ),
            )
        return "\n".join(results)

    result = await _copy_move(
        client,
        paths=full_paths,
        dest_folder=actual_dest,
        overwrite=overwrite,
        remove_src=True,
        operation="Restore",
        timeout=timeout,
        check_sources=False,
    )

    # Rephrase from "Moved" to "Restored"
    return _as_restore(result, share_name)
