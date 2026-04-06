"""File Station transfer operations: upload_file, download_file."""

from __future__ import annotations

import contextlib
import logging
import shutil
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from mcp_synology.core.errors import SynologyError, SynologyFileExistsError
from mcp_synology.core.formatting import format_error, format_size, format_status
from mcp_synology.modules.filestation.helpers import normalize_path

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient, ProgressCallback

logger = logging.getLogger(__name__)

# Files larger than this get a timeout warning in the output.
_LARGE_FILE_THRESHOLD = 1024 * 1024 * 1024  # 1 GB


async def upload_file(
    client: DsmClient,
    *,
    local_path: str,
    dest_folder: str,
    filename: str | None = None,
    overwrite: bool = False,
    create_parents: bool = True,
    timeout: float = 300.0,
    progress_callback: ProgressCallback | None = None,
) -> str:
    """Upload a local file to a NAS folder."""
    local = Path(local_path)
    if not local.is_file():
        return format_error(
            "Upload",
            f"Local file not found: {local_path}",
            "Check the file path and try again.",
        )

    file_size = local.stat().st_size
    dest = normalize_path(dest_folder)
    effective_name = filename or local.name

    # Report initial progress
    if progress_callback:
        await progress_callback(0, file_size)

    try:
        await client.upload(
            dest,
            local,
            effective_name,
            overwrite=overwrite,
            create_parents=create_parents,
            timeout=timeout,
        )
    except SynologyFileExistsError:
        return format_error(
            "Upload",
            f"File '{effective_name}' already exists in {dest}.",
            "Use overwrite=true to replace the existing file.",
        )
    except SynologyError as e:
        return format_error("Upload", str(e), e.suggestion)
    except OSError as e:
        return format_error(
            "Upload",
            f"Failed to read local file '{local_path}': {e}",
            "Check file permissions and that the file is not locked.",
        )

    # Report completion
    if progress_callback:
        await progress_callback(file_size, file_size)

    result = format_status(f"Uploaded {effective_name} ({format_size(file_size)}) to {dest}/")

    if file_size >= _LARGE_FILE_THRESHOLD:
        result += (
            f"\n    Note: Large file ({format_size(file_size)}). "
            "If future uploads of this size time out, "
            "increase upload_timeout in module settings."
        )

    return result


async def download_file(
    client: DsmClient,
    *,
    path: str,
    dest_folder: str,
    filename: str | None = None,
    overwrite: bool = False,
    timeout: float = 300.0,
    progress_callback: ProgressCallback | None = None,
) -> str:
    """Download a NAS file to a local directory."""
    local_dir = Path(dest_folder)
    if not local_dir.is_dir():
        return format_error(
            "Download",
            f"Local directory not found: {dest_folder}",
            "Check the directory path and try again.",
        )

    nas_path = normalize_path(path)
    effective_name = filename or PurePosixPath(nas_path).name
    dest_file = local_dir / effective_name

    if dest_file.exists() and not overwrite:
        return format_error(
            "Download",
            f"Local file already exists: {dest_file}",
            "Use overwrite=true to replace the existing file.",
        )

    # Pre-flight disk space check using NAS file metadata.
    # Best-effort: if the API call fails, skip the check and let the
    # download proceed (client.download() also checks Content-Length).
    nas_file_size: int | None = None
    try:
        info = await client.request(
            "SYNO.FileStation.List",
            "getinfo",
            params={"path": nas_path, "additional": '["size"]'},
        )
        files = info.get("files", [])
        if files:
            nas_file_size = files[0].get("additional", {}).get("size", 0)
    except Exception as e:  # noqa: BLE001
        logger.debug("Pre-flight getinfo failed: %s", e)

    if nas_file_size:
        free_space = shutil.disk_usage(local_dir).free
        if nas_file_size > free_space:
            return format_error(
                "Download",
                f"Insufficient local disk space: file is {format_size(nas_file_size)} "
                f"but only {format_size(free_space)} free on {local_dir}.",
                "Free space on the local disk or choose a different destination.",
            )

    try:
        bytes_written = await client.download(
            nas_path,
            dest_file,
            timeout=timeout,
            progress_callback=progress_callback,
        )
    except SynologyError as e:
        # Clean up partial file on failure
        if dest_file.exists():
            try:
                dest_file.unlink()
                logger.debug("Cleaned up partial download: %s", dest_file)
            except OSError:
                logger.warning("Failed to clean up partial download: %s", dest_file)
        return format_error("Download", str(e), e.suggestion)
    except OSError as e:
        # Filesystem rejected the write — illegal characters in filename on this OS,
        # permission denied, disk full, path too long, etc.
        if dest_file.exists():
            with contextlib.suppress(OSError):
                dest_file.unlink()
        error_str = str(e)
        if "disk space" in error_str.lower() or "space" in error_str.lower():
            suggestion = "Free space on the local disk or choose a different destination."
        else:
            suggestion = (
                "The filename may contain characters not allowed on this OS. "
                "Use the filename parameter to specify a compatible name."
            )
        return format_error("Download", f"Failed to write local file: {e}", suggestion)
    except Exception:
        # Clean up partial file on unexpected failure
        if dest_file.exists():
            with contextlib.suppress(OSError):
                dest_file.unlink()
        raise

    result = format_status(
        f"Downloaded {effective_name} ({format_size(bytes_written)}) to {dest_file}"
    )

    if bytes_written >= _LARGE_FILE_THRESHOLD:
        result += (
            f"\n    Note: Large file ({format_size(bytes_written)}). "
            "If future downloads of this size time out, "
            "increase download_timeout in module settings."
        )

    return result
