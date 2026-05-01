"""File Station listing tools: list_shares, list_files, list_recycle_bin."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp.exceptions import ToolError

from mcp_synology.core.errors import SynologyError
from mcp_synology.core.formatting import (
    format_size,
    format_table,
    format_timestamp,
    synology_error_response,
)
from mcp_synology.modules.filestation.helpers import (
    correct_recycle_status_from_observation,
    ensure_recycle_status,
    file_type_icon,
    normalize_path,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

# Map common sort field names to DSM API field names
_SORT_ALIASES: dict[str, str] = {
    "modified": "mtime",
    "date": "mtime",
    "time": "mtime",
    "created": "crtime",
    "accessed": "atime",
    "owner": "user",
}


def _resolve_sort_by(sort_by: str) -> str:
    return _SORT_ALIASES.get(sort_by, sort_by)


async def list_shares(
    client: DsmClient,
    *,
    additional: list[str] | None = None,
    sort_by: str = "name",
    sort_direction: str = "asc",
    recycle_bin_status: dict[str, bool] | None = None,
    hostname: str = "NAS",
    file_type_indicator: str = "emoji",
) -> str:
    """List all shared folders on the NAS."""
    if additional is None:
        additional = ["real_path", "size", "owner", "perm"]

    sort_by = _resolve_sort_by(sort_by)

    try:
        data = await client.request(
            "SYNO.FileStation.List",
            "list_share",
            params={
                "additional": '["' + '","'.join(additional) + '"]',
                "sort_by": sort_by,
                "sort_direction": sort_direction,
            },
        )
    except SynologyError as e:
        synology_error_response("List shares", e)

    shares = data.get("shares", [])
    if not shares:
        return format_table(
            headers=["Name", "Path"],
            rows=[],
            title=f"Shared Folders on {hostname}",
        )

    headers = ["Name", "Path", "Size", "Owner"]
    if recycle_bin_status:
        headers.append("Recycle Bin")

    rows: list[list[str]] = []
    for share in shares:
        name = share.get("name", "")
        path = share.get("path", f"/{name}")
        add_info = share.get("additional", {})
        size = format_size(add_info.get("size", {}).get("total_size", 0))
        owner = add_info.get("owner", {}).get("user", "—")
        row = [name, path, size, owner]
        if recycle_bin_status:
            row.append("enabled" if recycle_bin_status.get(name, False) else "disabled")
        rows.append(row)

    total = data.get("total", len(shares))
    result = format_table(headers=headers, rows=rows, title=f"Shared Folders on {hostname}")
    result += f"\n\n{total} shared folders found."
    result += (
        "\n\nPaths shown above are the root for all file operations."
        '\nExample: to list files in the video share, use list_files(path="/video")'
    )
    return result


async def list_files(
    client: DsmClient,
    *,
    path: str,
    additional: list[str] | None = None,
    pattern: str | None = None,
    filetype: str = "all",
    sort_by: str = "name",
    sort_direction: str = "asc",
    offset: int = 0,
    limit: int = 200,
    hide_recycle: bool = True,
    file_type_indicator: str = "emoji",
) -> str:
    """List files and folders within a directory path."""
    if additional is None:
        additional = ["size", "time"]

    normalized = normalize_path(path)

    sort_by = _resolve_sort_by(sort_by)

    params: dict[str, Any] = {
        "folder_path": normalized,
        "additional": '["' + '","'.join(additional) + '"]',
        "filetype": filetype,
        "sort_by": sort_by,
        "sort_direction": sort_direction,
        "offset": str(offset),
        "limit": str(limit),
    }
    if pattern:
        params["pattern"] = pattern

    try:
        data = await client.request("SYNO.FileStation.List", "list", params=params)
    except SynologyError as e:
        synology_error_response("List files", e)

    files = data.get("files", [])
    total = data.get("total", len(files))

    # Optionally filter out #recycle (disabled by default)
    if hide_recycle:
        files = [f for f in files if f.get("name") != "#recycle"]

    if not files:
        return format_table(
            headers=["Type", "Name"],
            rows=[],
            title=f"Contents of {normalized} (0 items)",
        )

    headers = ["Type", "Name", "Size", "Modified"]
    rows: list[list[str]] = []
    for f in files:
        is_dir = f.get("isdir", False)
        name = f.get("name", "")
        display_name = name + "/" if is_dir else name
        icon = file_type_icon(is_dir, name, style=file_type_indicator)

        add_info = f.get("additional", {})
        size = "—" if is_dir else format_size(add_info.get("size", 0))

        time_info = add_info.get("time", {})
        mtime = time_info.get("mtime", 0)
        modified = format_timestamp(mtime) if mtime else "—"

        rows.append([icon, display_name, size, modified])

    shown = len(files)
    title = f"Contents of {normalized} ({total} items)"
    result = format_table(headers=headers, rows=rows, title=title)

    next_offset = offset + shown
    if next_offset < total:
        result += f"\n\nShowing {shown} of {total:,} items. Use offset={next_offset} to see more."
    elif shown > 0:
        result += f"\n\n{shown} of {total} items."

    return result


async def list_recycle_bin(
    client: DsmClient,
    *,
    share: str,
    pattern: str | None = None,
    sort_by: str = "mtime",
    sort_direction: str = "desc",
    limit: int = 100,
    file_type_indicator: str = "emoji",
    recycle_bin_status: dict[str, bool] | None = None,
) -> str:
    """List contents of a shared folder's recycle bin."""
    # Normalize share name
    share_name = share.strip("/").split("/")[0]

    # Probe (or hit cache) for recycle-bin status before issuing the actual list
    # call. Pre-#37 the cache was always empty, so this fast-path early-return
    # never fired and every share fell into the list-and-catch-408 path below.
    if recycle_bin_status is not None:
        enabled = await ensure_recycle_status(client, share_name, recycle_bin_status)
        if not enabled:
            return (
                f"Recycle bin is not enabled on /{share_name}. Deleted files cannot be recovered."
            )

    recycle_path = f"/{share_name}/#recycle"

    try:
        result = await list_files(
            client,
            path=recycle_path,
            pattern=pattern,
            sort_by=sort_by,
            sort_direction=sort_direction,
            limit=limit,
            hide_recycle=False,
            file_type_indicator=file_type_indicator,
        )
    except ToolError as e:
        # list_files converts SynologyError to ToolError with a JSON envelope.
        # DSM error 408 = "No such file or directory" — the #recycle folder
        # doesn't exist when the recycle bin is disabled on this share.
        try:
            body = json.loads(str(e))
            if body.get("error", {}).get("code") == "not_found":
                logger.debug("Recycle bin path %s not found", recycle_path)
                # Self-correct: cache may have said enabled (or been empty);
                # the live DSM call just contradicted it.
                if recycle_bin_status is not None:
                    correct_recycle_status_from_observation(
                        share_name, observed_enabled=False, recycle_status=recycle_bin_status
                    )
                return (
                    f"Recycle bin is not enabled on /{share_name}. "
                    "Deleted files cannot be recovered."
                )
        except (json.JSONDecodeError, TypeError):
            pass
        raise

    # Successful list confirms recycle is enabled — self-correct if cache disagreed.
    if recycle_bin_status is not None:
        correct_recycle_status_from_observation(
            share_name, observed_enabled=True, recycle_status=recycle_bin_status
        )
    return result
