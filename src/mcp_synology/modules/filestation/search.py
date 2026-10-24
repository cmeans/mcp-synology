"""File Station search tool: search_files."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from mcp_synology.core.errors import ErrorCode, SynologyError
from mcp_synology.core.formatting import (
    error_response,
    format_size,
    format_table,
    format_timestamp,
    synology_error_response,
)
from mcp_synology.modules.filestation.helpers import (
    dsm_str,
    file_type_icon,
    matches_pattern,
    normalize_path,
    parse_human_size,
    parse_mtime,
    task_request,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient


async def _cleanup_search_task(
    client: DsmClient, taskid: str, version: int, logger: logging.Logger
) -> None:
    """Stop and clean a search task. Logs warnings on failure instead of silently suppressing."""
    for method in ("stop", "clean"):
        try:
            await task_request(client, "SYNO.FileStation.Search", method, taskid, version=version)
        except SynologyError as e:
            logger.warning("Search task cleanup (%s) failed for %s: %s", method, taskid, e)
        except Exception:
            logger.warning("Search task cleanup (%s) failed for %s", method, taskid, exc_info=True)


async def search_files(
    client: DsmClient,
    *,
    folder_path: str,
    pattern: str | None = None,
    extension: str | None = None,
    filetype: str = "all",
    size_from: str | int | None = None,
    size_to: str | int | None = None,
    mtime_from: str | None = None,
    mtime_to: str | None = None,
    exclude_pattern: str | None = None,
    recursive: bool = True,
    limit: int = 500,
    file_type_indicator: str = "emoji",
    timeout: float = 120.0,
    poll_interval: float = 1.0,
) -> str:
    """Search for files by name, type, size, or modification date."""
    logger = logging.getLogger(__name__)

    normalized = normalize_path(folder_path)

    # Pin to version 2 — v3 uses JSON request format with different parameter encoding
    # that causes silent failures (same issue as CopyMove/Delete).
    search_version = min(2, client.negotiate_version("SYNO.FileStation.Search", max_version=2))
    logger.debug("Using Search API v%d", search_version)

    # Start search
    start_params: dict[str, Any] = {
        "folder_path": normalized,
        "recursive": str(recursive).lower(),
    }
    if pattern:
        if pattern.startswith("*.") and "." not in pattern[2:]:
            # Pure extension pattern like "*.mkv" — use DSM's extension filter
            if not extension:
                extension = pattern[2:]
        else:
            # DSM Search uses glob matching on filenames. Wrap with wildcards
            # so a keyword like "Bambu" matches "Bambu Studio" (i.e., *Bambu*).
            search_pattern = pattern
            if "*" not in pattern and "?" not in pattern:
                search_pattern = f"*{pattern}*"
            start_params["pattern"] = dsm_str(search_pattern)
    if extension:
        start_params["extension"] = dsm_str(extension)
    # Always pass filetype — DSM defaults to "file" if omitted, which
    # excludes directories from results.
    start_params["filetype"] = filetype
    if size_from is not None:
        start_params["size_from"] = str(parse_human_size(size_from))
    if size_to is not None:
        start_params["size_to"] = str(parse_human_size(size_to))
    if mtime_from:
        start_params["mtime_from"] = str(parse_mtime(mtime_from))
    if mtime_to:
        start_params["mtime_to"] = str(parse_mtime(mtime_to))
    # With filetype=all and nothing to match on, DSM refuses the search as "Search folder
    # access denied" (DSM 7.2.2 and 7.3.2; filetype=file or dir alone works). "*" asks
    # for everything, which is what such a call means (#306).
    if filetype == "all" and not any(
        k in start_params
        for k in ("pattern", "extension", "size_from", "size_to", "mtime_from", "mtime_to")
    ):
        start_params["pattern"] = dsm_str("*")

    try:
        start_data = await client.request(
            "SYNO.FileStation.Search",
            "start",
            version=search_version,
            params=start_params,
        )
    except SynologyError as e:
        synology_error_response("Search files", e)

    taskid = start_data.get("taskid", "")

    # Poll for results. DSM search is async: start kicks it off, list checks
    # progress. A real list reply carries `total` and `files`; a bare
    # {"finished": true} is DSM's reply for a task it doesn't know (#121), so
    # it's a lost task, never "0 results".
    start_time = time.monotonic()
    all_files: list[dict[str, Any]] = []
    finished = False
    lost_task = False
    poll_error: SynologyError | None = None

    try:
        while (time.monotonic() - start_time) < timeout:
            try:
                list_data = await task_request(
                    client,
                    "SYNO.FileStation.Search",
                    "list",
                    taskid,
                    version=search_version,
                    params={
                        "additional": '["size","time"]',  # what the table shows (#149)
                        "limit": str(limit),
                        "offset": "0",
                    },
                )
            except SynologyError as e:
                poll_error = e
                break

            if "total" not in list_data and "files" not in list_data:
                lost_task = True
                break
            all_files = list_data.get("files", [])
            finished = list_data.get("finished", False)
            if finished:
                break

            await asyncio.sleep(poll_interval)
    finally:
        # Always clean up the task to avoid orphaned processes on the NAS.
        # Orphaned search tasks consume CPU indefinitely.
        await _cleanup_search_task(client, taskid, search_version, logger)

    if poll_error:
        synology_error_response("Search files", poll_error)
    if lost_task:
        error_response(
            ErrorCode.UNAVAILABLE,
            f"Search files failed: DSM lost the search task for {normalized} "
            "and returned no results, so this is not a 0-result answer.",
            retryable=True,
            suggestion="Retry the search, or browse the likely folder with list_files.",
        )

    # Apply client-side exclude_pattern
    excluded_count = 0
    if exclude_pattern:
        original_count = len(all_files)
        all_files = [
            f for f in all_files if not matches_pattern(f.get("name", ""), exclude_pattern)
        ]
        excluded_count = original_count - len(all_files)

    # The heading names the caller's own filters, never the "*" sent for none (#306).
    filters = [f"pattern: {pattern}"] if pattern else []
    if exclude_pattern:
        filters.append(f"excluding: {exclude_pattern}")
    title = f"Search results in {normalized}"
    if filters:
        title += f" ({', '.join(filters)})"

    if not all_files:
        msg = f"{title}\n\n0 results found."
        if not finished:
            msg += " (search timed out — try narrowing the scope)"
        else:
            msg += " Try broadening the filters or checking the folder path."
        return msg

    headers = ["Type", "Name", "Path", "Size", "Modified"]
    rows: list[list[str]] = []
    for f in all_files:
        is_dir = f.get("isdir", False)
        name = f.get("name", "")
        icon = file_type_icon(is_dir, name, style=file_type_indicator)
        fpath = f.get("path", "")
        # Show parent directory
        parent = "/".join(fpath.split("/")[:-1]) + "/" if "/" in fpath else ""

        add_info = f.get("additional", {})
        size = "—" if is_dir or "size" not in add_info else format_size(add_info["size"])
        mtime = add_info.get("time", {}).get("mtime", 0)
        modified = format_timestamp(mtime) if mtime else "—"

        rows.append([icon, name, parent, size, modified])

    result = format_table(headers=headers, rows=rows, title=title)
    total = list_data.get("total", len(all_files))
    result += f"\n\n{len(all_files):,} results found"
    if total > limit:
        result += f" (showing {limit:,} of {total:,} — increase limit to see more)"
    if excluded_count > 0:
        result += f" ({excluded_count:,} excluded by filter)"
    result += "."
    if not finished:
        result += " (search timed out — partial results shown)"

    return result
