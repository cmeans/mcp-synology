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
    ensure_recycle_status,
    escape_multi_path,
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
    """Create one or more new folders."""
    normalized = [normalize_path(p) for p in paths]

    # Build folder_path and name params from the paths
    folder_paths: list[str] = []
    names: list[str] = []
    for p in normalized:
        parent = "/".join(p.split("/")[:-1]) or "/"
        name = p.split("/")[-1]
        folder_paths.append(parent)
        names.append(name)

    try:
        data = await client.request(
            "SYNO.FileStation.CreateFolder",
            "create",
            params={
                "folder_path": escape_multi_path(folder_paths),
                "name": escape_multi_path(names),
                "force_parent": str(force_parent).lower(),
            },
        )
    except SynologyError as e:
        synology_error_response("Create folder", e)

    folders = data.get("folders", [])
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
                "name": new_name,
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


async def _copy_move(
    client: DsmClient,
    *,
    paths: list[str],
    dest_folder: str,
    overwrite: bool,
    remove_src: bool,
    operation: str,
    timeout: float = 120.0,
) -> str:
    """Shared implementation for copy and move operations."""
    normalized = [normalize_path(p) for p in paths]
    dest = normalize_path(dest_folder)
    path_param = escape_multi_path(normalized)

    # Pin to version 2 — v3 uses JSON request format with different
    # parameter encoding that our comma-separated path format doesn't support.
    copymove_version = min(2, client.negotiate_version("SYNO.FileStation.CopyMove", max_version=2))

    try:
        start_data = await client.request(
            "SYNO.FileStation.CopyMove",
            "start",
            version=copymove_version,
            params={
                "path": path_param,
                "dest_folder_path": dest,
                "overwrite": str(overwrite).lower(),
                "remove_src": str(remove_src).lower(),
            },
        )
    except SynologyError as e:
        synology_error_response(f"{operation} files", e)

    taskid = start_data.get("taskid", "")

    # Poll for completion. Use try/finally to ensure task is always stopped,
    # preventing orphaned processes that consume CPU on the NAS.
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

            logger.debug("%s status: %s", operation, status)

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
            f"{operation} files failed: timed out after {timeout}s.",
            retryable=True,
            param="timeout",
            value=timeout,
            suggestion="The operation may still be running on the NAS.",
        )

    # Check for errors in the completed task
    if "error" in status:
        err = status["error"]
        err_code = err.get("code", 0) if isinstance(err, dict) else err
        err_path = status.get("path", "")
        # Route err_code through error_from_code so callers see a specific
        # envelope (e.g. not_found, disk_full) matching the synchronous error
        # paths in this module. Unknown codes fall back to DSM_ERROR.
        mapped = error_from_code(err_code, "SYNO.FileStation.CopyMove")
        error_response(
            mapped.error_code,
            f"{operation} files failed: DSM error code {err_code} on path: {err_path}",
            retryable=mapped.retryable,
            suggestion=(
                mapped.suggestion
                or "Check that source paths exist and you have permission to access them."
            ),
        )

    # Build response
    processed_size = status.get("processed_size", 0)
    verb = "Copied" if not remove_src else "Moved"
    lines = [format_status(f"{verb} {len(normalized)} item(s) to {dest}/:")]
    lines.extend(f"  {p.split('/')[-1]}" for p in normalized)

    if processed_size > 0:
        lines.append(f"\nTotal size: {format_size(processed_size)}")

    if remove_src:
        src_dirs = sorted({"/".join(p.split("/")[:-1]) for p in normalized})
        lines.extend(f"\nSource files have been removed from {d}/." for d in src_dirs)

    return "\n".join(lines)


async def _delete_one_path(
    client: DsmClient,
    path: str,
    *,
    recursive: bool,
    delete_version: int,
    timeout: float,
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

    normalized = [normalize_path(p) for p in paths]

    # Pin to v2 — v3 uses JSON request format with different parameter encoding.
    delete_version = min(2, client.negotiate_version("SYNO.FileStation.Delete", max_version=2))

    for p in normalized:
        await _delete_one_path(
            client,
            p,
            recursive=recursive,
            delete_version=delete_version,
            timeout=timeout,
        )

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

    # Use the first destination (or provided dest_folder)
    actual_dest = dest_folder or restore_destinations[0]
    if dest_folder is None and len(set(restore_destinations)) > 1:
        # Multiple different destinations — move each one separately
        results: list[str] = []
        for fp, dest in zip(full_paths, restore_destinations, strict=False):
            r = await _copy_move(
                client,
                paths=[fp],
                dest_folder=dest,
                overwrite=overwrite,
                remove_src=True,
                operation="Restore",
                timeout=timeout,
            )
            results.append(r)
        return "\n".join(results)

    result = await _copy_move(
        client,
        paths=full_paths,
        dest_folder=actual_dest,
        overwrite=overwrite,
        remove_src=True,
        operation="Restore",
        timeout=timeout,
    )

    # Rephrase from "Moved" to "Restored"
    return result.replace("[+] Moved", f"[+] Restored from /{share_name} recycle bin:")
