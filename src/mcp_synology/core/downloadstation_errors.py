"""Download Station API error codes (400-series, DS-specific semantics).

DS overloads the same numeric range that FILESTATION_ERROR_CODES uses, with
different meanings (e.g., DS 400 = file upload failed; FS 400 = invalid
parameter). Keeping the maps in separate files makes the overlap visible.

To add a new code: add an entry below AND ensure docs/error-codes.md has a
matching section (the tests/core/test_help_urls.py test enforces parity).
"""

from __future__ import annotations

from mcp_synology.core.errors import ErrorCode, SynologyError


class DownloadStationError(SynologyError):
    """Base exception for Download Station API errors."""

    error_code = ErrorCode.DSM_ERROR


DOWNLOADSTATION_ERROR_CODES: dict[int, tuple[str, str]] = {
    400: (
        "File upload failed",
        "Failed to upload .torrent or .nzb file. Check the file is readable "
        "and within DSM's upload size limit.",
    ),
    401: (
        "Max number of tasks reached",
        "DSM has hit its max-concurrent-tasks limit. Pause or delete finished "
        "tasks first, then retry.",
    ),
    402: (
        "Destination denied",
        "Destination share rejected the task. Check the user has write "
        "permission on the destination share.",
    ),
    403: (
        "Destination doesn't exist",
        "Pass the destination share-relative ('video/Torrents', not "
        "'/volume1/video/Torrents'). If the folder is new: Download Station doesn't create "
        "missing folders, so create it first with File Station's create_folder (its path "
        "form, '/video/TV/New show'; needs File Station write permission) or, without that, "
        "ask the user to create it in DSM, then retry.",
    ),
    404: (
        "Invalid task id",
        "Task id not found: no such task, or it belongs to another user (an account "
        "that isn't a Download Station manager sees only its own tasks). Run "
        "list_downloads to get the current set of ids.",
    ),
    405: (
        "Invalid task action",
        "Action is not valid for this task's current state (e.g., resuming an "
        "active or finished task, or pausing a paused or finished task). Check "
        "its status with list_downloads.",
    ),
    406: (
        "No default destination",
        "DS has no default destination configured. Set one via set_download_config "
        "or in DSM > Download Station > Settings > General.",
    ),
    407: (
        "Set destination failed",
        "Setting the task destination failed. The path may be invalid or the "
        "user may lack write permission.",
    ),
    408: (
        "File doesn't exist",
        "A referenced file (typically a .torrent or .nzb path on the NAS) does not exist.",
    ),
}
