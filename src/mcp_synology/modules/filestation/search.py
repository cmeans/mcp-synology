"""File Station search tool: search_files."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from mcp_synology.core.errors import SynologyError
from mcp_synology.core.formatting import format_error, format_size, format_table, format_timestamp
from mcp_synology.modules.filestation.helpers import (
    file_type_icon,
    matches_pattern,
    normalize_path,
    parse_human_size,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient


async def _cleanup_search_task(
    client: DsmClient, taskid: str, version: int, logger: logging.Logger
) -> None:
    """Stop and clean a search task. Logs warnings on failure instead of silently suppressing."""
    for method in ("stop", "clean"):
        try:
            await client.request(
                "SYNO.FileStation.Search",
                method,
                version=version,
                params={"taskid": taskid},
            )
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
    additional: list[str] | None = None,
    file_type_indicator: str = "emoji",
    timeout: float = 120.0,
    poll_interval: float = 1.0,
) -> str:
    """Search for files by name, type, size, or modification date."""
    if additional is None:
        additional = ["size", "time"]

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
            start_params["pattern"] = search_pattern
    if extension:
        start_params["extension"] = extension
    # Always pass filetype — DSM defaults to "file" if omitted, which
    # excludes directories from results.
    start_params["filetype"] = filetype
    if size_from is not None:
        start_params["size_from"] = str(parse_human_size(size_from))
    if size_to is not None:
        start_params["size_to"] = str(parse_human_size(size_to))
    if mtime_from:
        start_params["mtime_from"] = mtime_from
    if mtime_to:
        start_params["mtime_to"] = mtime_to

    try:
        start_data = await client.request(
            "SYNO.FileStation.Search",
            "start",
            version=search_version,
            params=start_params,
        )
    except SynologyError as e:
        return format_error("Search files", str(e), e.suggestion)

    taskid = start_data.get("taskid", "")

    # Poll for results. DSM search is async — start kicks it off, list checks
    # progress. We must poll promptly: if we wait too long, the search may
    # finish and discard results before we read them. We also don't trust
    # finished=True on the very first poll with 0 results, as it can be a
    # false positive on non-indexed shares.
    start_time = time.monotonic()
    all_files: list[dict[str, Any]] = []
    finished = False
    poll_count = 0
    poll_error: str | None = None

    try:
        while (time.monotonic() - start_time) < timeout:
            try:
                list_data = await client.request(
                    "SYNO.FileStation.Search",
                    "list",
                    version=search_version,
                    params={
                        "taskid": taskid,
                        "additional": '["' + '","'.join(additional) + '"]',
                        "limit": str(limit),
                        "offset": "0",
                    },
                )
            except SynologyError as e:
                poll_error = format_error("Search files", str(e), e.suggestion)
                break

            poll_count += 1
            all_files = list_data.get("files", [])
            finished = list_data.get("finished", False)

            if finished and (all_files or poll_count >= 3):
                # Trust finished=True if we have results, or after enough polls
                # to confirm the search genuinely found nothing.
                break

            await asyncio.sleep(poll_interval)
    finally:
        # Always clean up the task to avoid orphaned processes on the NAS.
        # Orphaned search tasks consume CPU indefinitely.
        await _cleanup_search_task(client, taskid, search_version, logger)

    if poll_error:
        return poll_error

    # Apply client-side exclude_pattern
    excluded_count = 0
    if exclude_pattern:
        original_count = len(all_files)
        all_files = [
            f for f in all_files if not matches_pattern(f.get("name", ""), exclude_pattern)
        ]
        excluded_count = original_count - len(all_files)

    if not all_files:
        title_parts = [f"Search results in {normalized}"]
        if pattern:
            title_parts.append(f"(pattern: {pattern})")
        title = " ".join(title_parts)
        msg = f"{title}\n\n0 results found."
        if not finished:
            msg += " (search timed out — try narrowing the scope)"
        else:
            msg += " Try broadening the pattern or checking the folder path."
        return msg

    # Build results table
    title_parts = [f"Search results in {normalized}"]
    if pattern:
        title_parts.append(f"(pattern: {pattern}")
        if exclude_pattern:
            title_parts[-1] += f", excluding: {exclude_pattern}"
        title_parts[-1] += ")"

    title = " ".join(title_parts)

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
        size = "—" if is_dir else format_size(add_info.get("size", 0))
        mtime = add_info.get("time", {}).get("mtime", 0)
        modified = format_timestamp(mtime) if mtime else "—"

        rows.append([icon, name, parent, size, modified])

    result = format_table(headers=headers, rows=rows, title=title)
    total = list_data.get("total", len(all_files))
    result += f"\n\n{len(all_files)} results found"
    if total > limit:
        result += f" (showing {limit} of {total} — increase limit to see more)"
    if excluded_count > 0:
        result += f" ({excluded_count} excluded by filter)"
    result += "."
    if not finished:
        result += " (search timed out — partial results shown)"

    return result
