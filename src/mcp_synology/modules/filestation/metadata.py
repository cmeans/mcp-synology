"""File Station metadata tools: get_file_info, get_dir_size."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from mcp_synology.core.errors import ErrorCode, SynologyError
from mcp_synology.core.formatting import (
    error_response,
    format_key_value,
    format_size,
    format_table,
    format_timestamp,
    synology_error_response,
)
from mcp_synology.modules.filestation.helpers import (
    escape_multi_path,
    normalize_path,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)


async def _stop_dirsize_task(client: DsmClient, taskid: str) -> None:
    """Stop a DirSize background task. Logs warnings on failure."""
    try:
        await client.request(
            "SYNO.FileStation.DirSize",
            "stop",
            params={"taskid": taskid},
        )
    except SynologyError as e:
        logger.warning("DirSize task cleanup failed for %s: %s", taskid, e)
    except Exception:
        logger.warning("DirSize task cleanup failed for %s", taskid, exc_info=True)


async def get_file_info(
    client: DsmClient,
    *,
    paths: list[str],
    additional: list[str] | None = None,
) -> str:
    """Get detailed metadata for specific files or folders."""
    if additional is None:
        additional = ["real_path", "size", "owner", "time", "perm"]

    normalized = [normalize_path(p) for p in paths]
    path_param = escape_multi_path(normalized)

    try:
        data = await client.request(
            "SYNO.FileStation.List",
            "getinfo",
            params={
                "path": path_param,
                "additional": '["' + '","'.join(additional) + '"]',
            },
        )
    except SynologyError as e:
        synology_error_response("Get file info", e)

    files = data.get("files", [])

    if len(files) == 1:
        return _format_single_info(files[0])

    # Multiple files: table format
    if not files:
        error_response(
            ErrorCode.NOT_FOUND,
            "Get file info failed: No file information returned.",
            retryable=False,
            suggestion="Check that the paths exist.",
        )

    headers = ["Name", "Path", "Type", "Size", "Modified"]
    rows: list[list[str]] = []
    for f in files:
        add_info = f.get("additional", {})
        name = f.get("name", "")
        path = f.get("path", "")
        ftype = "Directory" if f.get("isdir") else "File"
        size = format_size(add_info.get("size", 0)) if not f.get("isdir") else "—"
        mtime = add_info.get("time", {}).get("mtime", 0)
        modified = format_timestamp(mtime) if mtime else "—"
        rows.append([name, path, ftype, size, modified])

    return format_table(headers=headers, rows=rows, title=f"File Info ({len(files)} items)")


def _format_single_info(file_data: dict[str, Any]) -> str:
    """Format detailed info for a single file."""
    add_info = file_data.get("additional", {})
    name = file_data.get("name", "")
    path = file_data.get("path", "")
    is_dir = file_data.get("isdir", False)

    pairs: list[tuple[str, str]] = [
        ("Name", name),
        ("Path", path),
    ]

    real_path = add_info.get("real_path", "")
    if real_path:
        pairs.append(("Real path", real_path))

    pairs.append(("Type", "Directory" if is_dir else "File"))

    if not is_dir:
        pairs.append(("Size", format_size(add_info.get("size", 0))))

    owner_info = add_info.get("owner", {})
    user = owner_info.get("user", "")
    group = owner_info.get("group", "")
    if user:
        owner_str = f"{user} ({group})" if group else user
        pairs.append(("Owner", owner_str))

    time_info = add_info.get("time", {})
    if time_info.get("mtime"):
        pairs.append(("Modified", format_timestamp(time_info["mtime"])))
    if time_info.get("crtime"):
        pairs.append(("Created", format_timestamp(time_info["crtime"])))
    if time_info.get("atime"):
        pairs.append(("Accessed", format_timestamp(time_info["atime"])))

    perm = add_info.get("perm", {})
    if perm.get("posix"):
        pairs.append(("Permissions", str(perm["posix"])))

    return format_key_value(pairs, title=f"File Info: {path}")


async def get_dir_size(
    client: DsmClient,
    *,
    path: str,
    timeout: float = 120.0,
) -> str:
    """Calculate the total size of a directory."""
    normalized = normalize_path(path)

    try:
        start_data = await client.request(
            "SYNO.FileStation.DirSize",
            "start",
            params={"path": normalized},
        )
    except SynologyError as e:
        synology_error_response("Get directory size", e)

    taskid = start_data.get("taskid", "")

    # Poll for completion. Use try/finally to ensure task is always stopped,
    # preventing orphaned processes that consume CPU on the NAS.
    elapsed = 0.0
    interval = 0.5
    poll_error: SynologyError | None = None
    result: str | None = None

    try:
        while elapsed < timeout:
            try:
                status = await client.request(
                    "SYNO.FileStation.DirSize",
                    "status",
                    params={"taskid": taskid},
                )
            except SynologyError as e:
                poll_error = e
                break

            if status.get("finished", False):
                total_size = status.get("total_size", 0)
                num_file = status.get("num_file", 0)
                num_dir = status.get("num_dir", 0)

                result = format_key_value(
                    [
                        ("Total size", format_size(total_size)),
                        ("Files", str(num_file)),
                        ("Directories", str(num_dir)),
                    ],
                    title=f"Directory Size: {normalized}",
                )
                break

            await asyncio.sleep(interval)
            elapsed += interval
    finally:
        await _stop_dirsize_task(client, taskid)

    if poll_error:
        synology_error_response("Get directory size", poll_error)
    if result:
        return result

    error_response(
        ErrorCode.TIMEOUT,
        f"Get directory size failed: timed out after {timeout}s.",
        retryable=True,
        suggestion="The directory may be very large. Try a subdirectory.",
    )
