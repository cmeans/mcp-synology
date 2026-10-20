"""File Station transfer operations: upload_file, download_file."""

from __future__ import annotations

import errno
import logging
import os
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, NoReturn

from mcp_synology.core.errors import (
    DownloadHttpError,
    ErrorCode,
    SynologyError,
    SynologyFileExistsError,
)
from mcp_synology.core.formatting import (
    error_response,
    format_size,
    format_status,
    synology_error_response,
)
from mcp_synology.core.local_paths import refuse_outside_roots
from mcp_synology.modules.filestation.helpers import normalize_path

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient, ProgressCallback

logger = logging.getLogger(__name__)

# Files larger than this get a timeout warning in the output.
_LARGE_FILE_THRESHOLD = 1024 * 1024 * 1024  # 1 GiB

# Separators rejected in a local file name on every OS, not just the host's:
# a name with a backslash is a path on Windows even when this runs on POSIX.
_NAME_SEPARATORS = frozenset({"/", "\\"} | {s for s in (os.sep, os.altsep) if s})


def _local_name_problem(name: str) -> str | None:
    """Return why ``name`` can't be used as a file name inside a local folder.

    ``download_file`` joins the name onto ``dest_folder``. A name with a path
    separator or a ``..`` component would write outside that folder, and an
    absolute name would replace the folder entirely (``Path("/a") / "/b"`` is
    ``/b``). Returns ``None`` for a plain file name.
    """
    if not name or name in (".", ".."):
        return "it is empty or a relative directory reference"
    if any(sep in name for sep in _NAME_SEPARATORS):
        return "it contains a path separator"
    if "\x00" in name:
        return "it contains a NUL character"
    # On Windows a drive-qualified name replaces the folder (C:\\dl / "D:x" is "D:x");
    # refused on every OS, like a backslash.
    if PureWindowsPath(name).drive:
        return "it names a drive"
    return None


def _expand_home(operation: str, param: str, value: str) -> Path:
    """``~`` and ``~user`` expanded; an unknown user is ``invalid_parameter``, not a crash.

    Reached with an unknown user only by unconfined (direct) callers: with allowed
    folders, ``refuse_outside_roots`` refuses it first like any path outside (#260).
    """
    try:
        return Path(value).expanduser()
    except RuntimeError as e:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"{operation} refused: {value!r} can't be expanded: {e}",
            retryable=False,
            param=param,
            value=value,
            suggestion="Pass an absolute local path, or one starting with ~/.",
        )


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
    allowed_roots: list[Path] | None = None,
) -> str:
    """Upload a local file to a NAS folder."""
    refuse_outside_roots(
        "Upload",
        "local_path",
        local_path,
        Path(local_path),
        allowed_roots,
        module="filestation",
        hide_unknown_users=True,
    )
    local = _expand_home("Upload", "local_path", local_path)
    if not local.is_file():
        error_response(
            ErrorCode.NOT_FOUND,
            f"Upload failed: Local file not found: {local_path}",
            retryable=False,
            param="local_path",
            value=local_path,
            suggestion="Check the file path and try again.",
        )

    file_size = local.stat().st_size
    dest = normalize_path(dest_folder)
    effective_name = filename or local.name

    # Report initial progress
    if progress_callback:
        await progress_callback(0, file_size)

    def already_exists() -> NoReturn:
        error_response(
            ErrorCode.ALREADY_EXISTS,
            f"Upload failed: File '{effective_name}' already exists in {dest}.",
            retryable=False,
            param="filename",
            value=effective_name,
            suggestion="Use overwrite=true to replace the existing file.",
        )

    try:
        data = await client.upload(
            dest,
            local,
            effective_name,
            overwrite=overwrite,
            create_parents=create_parents,
            timeout=timeout,
        )
    except SynologyFileExistsError:
        already_exists()
    except SynologyError as e:
        synology_error_response("Upload", e)
    except OSError as e:
        error_response(
            ErrorCode.FILESYSTEM_ERROR,
            f"Upload failed: Failed to read local file '{local_path}': {e}",
            retryable=False,
            param="local_path",
            value=local_path,
            suggestion="Check file permissions and that the file is not locked.",
        )

    if data.get("blSkip"):
        # DSM skipped the file and kept the existing one (it does this for overwrite=false,
        # which upload() no longer sends). Never report it as uploaded (#195).
        already_exists()

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
    allowed_roots: list[Path] | None = None,
) -> str:
    """Download a NAS file to a local directory."""
    # Before the existence check, so a refusal doesn't reveal what exists outside.
    refuse_outside_roots(
        "Download",
        "dest_folder",
        dest_folder,
        Path(dest_folder),
        allowed_roots,
        module="filestation",
        hide_unknown_users=True,
    )
    local_dir = _expand_home("Download", "dest_folder", dest_folder)
    if not local_dir.is_dir():
        error_response(
            ErrorCode.NOT_FOUND,
            f"Download failed: Local directory not found: {dest_folder}",
            retryable=False,
            param="dest_folder",
            value=dest_folder,
            suggestion="Check the directory path and try again.",
        )

    nas_path = normalize_path(path)
    effective_name = filename or PurePosixPath(nas_path).name
    problem = _local_name_problem(effective_name)
    if problem:
        param, value = ("filename", filename) if filename else ("path", path)
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"Download failed: Can't save as {effective_name!r}: {problem}.",
            retryable=False,
            param=param,
            value=value,
            suggestion=(
                "Pass a plain file name (no folders) as filename; "
                "choose the local folder with dest_folder."
            ),
        )
    dest_file = local_dir / effective_name
    if dest_file.parent != local_dir:
        # Backstop for any name shape the rules above miss: never write outside dest_folder.
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"Download failed: Can't save as {effective_name!r}: it would land outside "
            f"{local_dir}.",
            retryable=False,
            param="filename" if filename else "path",
            value=filename if filename else path,
            suggestion="Pass a plain file name (no folders) as filename.",
        )

    if dest_file.is_dir() and not dest_file.is_symlink():
        # Neither mode can replace a folder; refuse before downloading (QA R1 on #178).
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"Download failed: {dest_file} is a folder, so the file can't be saved there.",
            retryable=False,
            param="filename" if filename else "path",
            value=filename if filename else path,
            suggestion="Pass a different filename, or choose another dest_folder.",
        )

    # lexists, not exists: a dangling symlink is still a name in the way (QA R1 on #178).
    if os.path.lexists(dest_file) and not overwrite:
        error_response(
            ErrorCode.ALREADY_EXISTS,
            f"Download failed: Local file already exists: {dest_file}",
            retryable=False,
            param="filename",
            value=dest_file.name,
            suggestion="Use overwrite=true to replace the existing file.",
        )

    # Pre-flight disk space check using NAS file metadata.
    # Best-effort: if the API call fails, skip the check and let the
    # download proceed (client.download() also checks Content-Length).
    # It also tells a missing file apart: DSM answers the download of one with an HTTP
    # 502 and its own error page (DSM 7.1 and 7.2.2, #292), the same status a proxy
    # gives for a server error (#324).
    nas_file_size: int | None = None
    exists: bool | None = None  # None: the check didn't say
    entry: dict[str, Any] = {}
    try:
        info = await client.request(
            "SYNO.FileStation.List",
            "getinfo",
            params={"path": nas_path, "additional": '["size"]'},
        )
        files = info.get("files", [])
        if files and isinstance(files[0], dict):
            entry = files[0]
    except Exception as e:  # noqa: BLE001
        logger.debug("Pre-flight getinfo failed: %s", e)

    if entry.get("code") == 408:  # getinfo's entry for a missing path (#161)
        exists = False
    elif entry.get("code") == 407:  # no permission for it (vdsm 7.1 and 7.2.2)
        error_response(
            ErrorCode.PERMISSION_DENIED,
            f"Download failed: no permission to read {nas_path} on the NAS.",
            retryable=False,
            param="path",
            value=nas_path,
            suggestion="Check the DSM user's permissions for that shared folder.",
        )
    elif entry.get("isdir"):
        # DSM answers a folder's download with the same bare 502 (vdsm).
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"Download failed: {nas_path} is a folder; download_file fetches one file.",
            retryable=False,
            param="path",
            value=nas_path,
            suggestion="Download the files in it one at a time (list them with list_files).",
        )
    elif entry and "code" not in entry:
        exists = True
        nas_file_size = entry.get("additional", {}).get("size", 0)

    if exists is False:
        error_response(
            ErrorCode.NOT_FOUND,
            f"Download failed: {nas_path} doesn't exist on the NAS.",
            retryable=False,
            param="path",
            value=nas_path,
            suggestion="Check the path with list_files or search_files.",
        )

    if nas_file_size:
        free_space = shutil.disk_usage(local_dir).free
        if nas_file_size > free_space:
            error_response(
                ErrorCode.DISK_FULL,
                f"Download failed: Insufficient local disk space: "
                f"file is {format_size(nas_file_size)} "
                f"but only {format_size(free_space)} free on {local_dir}.",
                retryable=True,
                suggestion="Free space on the local disk or choose a different destination.",
            )

    try:
        bytes_written = await client.download(
            nas_path,
            dest_file,
            timeout=timeout,
            progress_callback=progress_callback,
            overwrite=overwrite,
        )
    except FileExistsError:
        # The local file appeared while the download streamed; overwrite=False holds.
        error_response(
            ErrorCode.ALREADY_EXISTS,
            f"Download failed: Local file already exists: {dest_file} (it was created "
            "while the download was in progress)",
            retryable=False,
            param="filename",
            value=dest_file.name,
            suggestion="Use overwrite=true to replace the existing file.",
        )
    except SynologyError as e:
        # client.download() streams to a temp file and removes it on failure,
        # so there is nothing to clean up here. Never unlink dest_file: with
        # overwrite=True it is the user's pre-existing file.
        if exists and isinstance(e, DownloadHttpError) and e.status >= 500:
            # The file is there, so this 5xx is a server or proxy error, not DSM's
            # answer for a missing file (#324).
            error_response(
                ErrorCode.UNAVAILABLE,
                f"Download failed: DSM, or a proxy in front of it, answered HTTP {e.status} "
                f"for {nas_path}, which exists.",
                retryable=True,
                suggestion="Try again shortly; if it keeps happening, run 'mcp-synology check'.",
            )
        synology_error_response("Download", e)
    except OSError as e:
        # Filesystem rejected the write. Possibilities:
        #   - ENOSPC: disk full
        #   - EACCES/EPERM: permission denied
        #   - EINVAL/ENAMETOOLONG: illegal chars, path too long
        # Disk-full is handled specifically here so smart clients get a
        # retryable ``disk_full`` code that matches the pre-flight branch
        # earlier in this function. Using ``errno`` rather than substring
        # matching on the error message — the message is locale-dependent
        # and varies across OS versions, ``errno`` does not.
        if e.errno == errno.ENOSPC:
            error_response(
                ErrorCode.DISK_FULL,
                f"Download failed: No space left on local disk writing {dest_file}: "
                f"{e.strerror or e}",
                retryable=True,
                suggestion="Free space on the local disk or choose a different destination.",
            )
        # Name the file the caller asked for: str(e) can carry the internal temp path.
        error_response(
            ErrorCode.FILESYSTEM_ERROR,
            f"Download failed: Failed to write local file {dest_file}: {e.strerror or e}",
            retryable=False,
            param="dest_folder",
            value=dest_folder,
            suggestion=(
                "The filename may contain characters not allowed on this OS. "
                "Use the filename parameter to specify a compatible name."
            ),
        )
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
