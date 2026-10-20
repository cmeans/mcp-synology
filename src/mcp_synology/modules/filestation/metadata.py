"""File Station metadata tools: get_file_info, get_dir_size."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, NoReturn

import httpx

from mcp_synology.core.errors import ErrorCode, SynologyError, error_from_code
from mcp_synology.core.formatting import (
    error_response,
    format_key_value,
    format_size,
    format_status,
    format_table,
    format_timestamp,
    synology_error_response,
)
from mcp_synology.modules.filestation.helpers import (
    find_missing_paths,
    missing_paths_suggestion,
    normalize_path,
    task_request,
    unanswered_status_response,
    validate_additional,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)


async def _stop_dirsize_task(client: DsmClient, taskid: str) -> None:
    """Stop a DirSize background task. Logs warnings on failure."""
    try:
        await task_request(client, "SYNO.FileStation.DirSize", "stop", taskid)
    except SynologyError as e:
        if e.code == 599:
            # DSM drops a DirSize task once its finished status has been read.
            logger.debug("DirSize task %s already gone at stop", taskid)
            return
        logger.warning("DirSize task cleanup failed for %s: %s", taskid, e)
    except Exception:
        logger.warning("DirSize task cleanup failed for %s", taskid, exc_info=True)


async def get_file_info(
    client: DsmClient,
    *,
    paths: list[str],
    additional: list[str] | None = None,
) -> str:
    """Get detailed metadata for specific files or folders.

    Closes #68 (the `get_file_info` half). DSM 7.x's
    `SYNO.FileStation.List getinfo` does not honor the documented
    comma-joined multi-path format on v2 — verified against vdsm DSM
    7.2.2 in CI: a request with `path=/a,/b` returns a single synthetic
    record whose `path` field IS the literal `/a,/b` string. v3 has the
    same problem (verified by the round-1 v2-pin attempt that didn't
    fix the bug). The user's documented workaround on #68 was to loop
    one path at a time, and that's what this function now does
    internally: one DSM call per input path, results aggregated into
    the response shape callers already expect (single-info card for one
    path, table for multiple). Trade-off is N round-trips for N paths,
    which is fine for the typical small-N usage and trivially correct.
    """
    validate_additional(additional, tool_name="Get file info")
    if additional is None:
        additional = ["real_path", "size", "owner", "time", "perm"]

    if not paths:
        error_response(
            ErrorCode.NOT_FOUND,
            "Get file info failed: No paths provided.",
            retryable=False,
            param="paths",
            value=paths,
            suggestion="Pass at least one path.",
        )

    normalized = [normalize_path(p) for p in paths]

    # Pin to v2 for parallelism with Delete / CopyMove / Search. v3 doesn't
    # buy us anything for getinfo and adds risk of further multi-path quirks.
    getinfo_version = min(2, client.negotiate_version("SYNO.FileStation.List", max_version=2))
    additional_param = '["' + '","'.join(additional) + '"]'

    files: list[dict[str, Any]] = []
    missing: list[tuple[str, int]] = []
    for p in normalized:
        try:
            data = await client.request(
                "SYNO.FileStation.List",
                "getinfo",
                version=getinfo_version,
                params={
                    "path": p,
                    "additional": additional_param,
                },
            )
        except SynologyError as e:
            synology_error_response("Get file info", e)
        for entry in data.get("files", []):
            # A missing path comes back as a per-file error, not a failed request:
            # {"code": 408, "path": ...} on DSM 7.1 and 7.2.2 (#161).
            if "code" in entry:
                missing.append((str(entry.get("path") or p), int(entry["code"])))
            else:
                files.append(entry)

    if missing and not files:
        codes = {code for _, code in missing}
        mapped = error_from_code(codes.pop() if len(codes) == 1 else 408, "SYNO.FileStation.List")
        error_response(
            mapped.error_code,
            "Get file info failed: "
            + "; ".join(f"{path} (DSM error {code})" for path, code in missing),
            retryable=False,
            param="paths",
            value=paths,
            suggestion=mapped.suggestion or "Check the paths with list_files or search_files.",
        )

    notes = "".join(
        "\n" + format_status(f"Not found: {path} (DSM error {code})", success=False)
        for path, code in missing
    )

    if len(files) == 1:
        return _format_single_info(files[0]) + notes

    if not files:
        error_response(
            ErrorCode.NOT_FOUND,
            "Get file info failed: No file information returned.",
            retryable=False,
            param="paths",
            value=paths,
            suggestion="Check that the paths exist.",
        )

    headers = ["Name", "Path", "Type", "Size", "Modified"]
    rows: list[list[str]] = []
    for f in files:
        add_info = f.get("additional", {})
        name = f.get("name", "")
        path = f.get("path", "")
        ftype = "Directory" if f.get("isdir") else "File"
        size = "—" if f.get("isdir") or "size" not in add_info else format_size(add_info["size"])
        mtime = add_info.get("time", {}).get("mtime", 0)
        modified = format_timestamp(mtime) if mtime else "—"
        rows.append([name, path, ftype, size, modified])

    return format_table(headers=headers, rows=rows, title=f"File Info ({len(files)} items)") + notes


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
    # DSM's "type" is the extension type ("ISO"; "" for a folder) and
    # "mount_point_type" is "" unless the path is a mount point.
    if add_info.get("type"):
        pairs.append(("File type", str(add_info["type"])))
    if add_info.get("mount_point_type"):
        pairs.append(("Mount point", str(add_info["mount_point_type"])))

    # Only when DSM returned it: an `additional` without "size" isn't a 0-byte file.
    if not is_dir and "size" in add_info:
        pairs.append(("Size", format_size(add_info["size"])))

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


def _refuse_path(normalized: str, path: str, code: int, missing: list[tuple[str, int]]) -> None:
    """Refuse get_dir_size for a path DSM reports as missing (408) or unreadable (407)."""
    if code == 408:
        error_response(
            ErrorCode.NOT_FOUND,
            f"Get directory size failed: {normalized} doesn't exist on the NAS.",
            retryable=False,
            param="path",
            value=path,
            suggestion=missing_paths_suggestion(missing),
        )
    if code == 407:  # no permission for it
        error_response(
            ErrorCode.PERMISSION_DENIED,
            f"Get directory size failed: no permission to read {normalized} on the NAS.",
            retryable=False,
            param="path",
            value=path,
            suggestion="Check the DSM user's permissions for that shared folder.",
        )


def _status_errors_response(normalized: str, path: str, errors: list[Any]) -> NoReturn:
    """A finished DirSize status that carries errors: the entry for the path, else the first."""
    entries = [e for e in errors if isinstance(e, dict) and isinstance(e.get("code"), int)]
    entry = next((e for e in entries if e.get("path") == normalized), None)
    if entry is None and entries:
        entry = entries[0]
    code = int(entry["code"]) if entry else 0
    _refuse_path(normalized, path, code, [(normalized, code)])
    synology_error_response("Get directory size", error_from_code(code, "SYNO.FileStation.DirSize"))


async def get_dir_size(
    client: DsmClient,
    *,
    path: str,
    timeout: float = 120.0,
) -> str:
    """Calculate the total size of a directory."""
    normalized = normalize_path(path)

    # DSM's DirSize task on a path that doesn't exist finishes at once with 0 bytes, 0
    # files and 0 folders (seska, DSM 7.1.1), which reads as "empty". Check the path
    # first, as delete_files does. Best-effort: if getinfo gets a DSM error or an
    # unreadable reply, or reports another code, DirSize runs; a finished status that
    # carries errors is then mapped the same way, never read as 0 B. A network error
    # ends the call, as it would at the DirSize start.
    try:
        missing = await find_missing_paths(client, [normalized])
    except SynologyError as e:
        logger.debug("Get directory size: path check failed: %s", e)
        missing = []
    if missing:
        _refuse_path(normalized, path, missing[0][1], missing)

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
    # Wall-clock deadline: a slow status request counts too (#237).
    deadline = time.monotonic() + timeout
    interval = 0.5
    poll_error: SynologyError | None = None
    unanswered: httpx.TransportError | None = None
    result: str | None = None
    status_errors: list[Any] = []

    try:
        while time.monotonic() < deadline:
            try:
                # 599 here used to be read as "finished too fast to measure";
                # it was the unquoted task ID matching no task (#121).
                status = await task_request(client, "SYNO.FileStation.DirSize", "status", taskid)
            except SynologyError as e:
                poll_error = e
                break
            except httpx.TransportError as e:
                unanswered = e
                break

            if status.get("finished", False):
                # A path the user can't read finishes with errors and no totals
                # (vdsm 7.1: {"errors": [{"code": 407, "path": ...}], "finished": true}).
                raw_errors = status.get("errors") or []
                status_errors = raw_errors if isinstance(raw_errors, list) else [raw_errors]
                if status_errors:
                    break
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
    finally:
        await _stop_dirsize_task(client, taskid)

    if poll_error:
        synology_error_response("Get directory size", poll_error)
    if status_errors:
        _status_errors_response(normalized, path, status_errors)
    if unanswered:
        unanswered_status_response(
            "Get directory size",
            normalized,
            unanswered,
            suggestion=(
                "Try a subdirectory, or give DSM longer to answer with connection.timeout "
                "in the config."
            ),
        )
    if result:
        return result

    error_response(
        ErrorCode.TIMEOUT,
        f"Get directory size failed: timed out after {timeout}s.",
        retryable=True,
        param="timeout",
        value=timeout,
        suggestion="The directory may be very large. Try a subdirectory.",
    )
