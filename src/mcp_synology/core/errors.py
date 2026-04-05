"""Typed exception hierarchy and error code mapping."""

from __future__ import annotations


class SynologyError(Exception):
    """Base exception for all Synology DSM errors."""

    def __init__(self, message: str, code: int | None = None, suggestion: str | None = None):
        self.code = code
        self.suggestion = suggestion
        super().__init__(message)


class AuthenticationError(SynologyError):
    """Authentication failed (bad credentials, 2FA required, etc.)."""


class SessionExpiredError(SynologyError):
    """Session expired or invalidated (codes 106, 107, 119)."""


class SynologyPermissionError(SynologyError):
    """Permission denied (code 105). NOT a session issue — never re-auth on this."""


class ApiNotFoundError(SynologyError):
    """Requested API does not exist on this NAS."""


class FileStationError(SynologyError):
    """Base exception for File Station API errors."""


class PathNotFoundError(FileStationError):
    """Path not found (code 408)."""


class SynologyFileExistsError(FileStationError):
    """File already exists at destination (code 414)."""


class DiskFullError(FileStationError):
    """No space left on device (code 416)."""


class IllegalNameError(FileStationError):
    """Invalid file or folder name (codes 418, 419)."""


# Common DSM error codes (100-series) shared across all APIs.
COMMON_ERROR_CODES: dict[int, tuple[str, str]] = {
    100: ("Unknown error", "Check NAS logs for details."),
    101: ("Invalid parameter", "Check the parameter values in your request."),
    102: ("API does not exist", "The requested API is not available on this NAS."),
    103: (
        "Method does not exist",
        "The requested method is not supported by this API version.",
    ),
    104: ("Not allowed version", "The API version is not supported by this NAS."),
    105: (
        "Permission denied",
        "The MCP service account may not have access. Check DSM user permissions.",
    ),
    106: ("Session timeout", "Session has expired. Re-authentication will be attempted."),
    107: (
        "Duplicate login",
        "Another session displaced this one. Consider using a dedicated DSM service account.",
    ),
    119: ("Invalid session", "Session ID is invalid. Re-authentication will be attempted."),
}

# Auth API error codes (400-series for SYNO.API.Auth).
AUTH_ERROR_CODES: dict[int, tuple[str, str]] = {
    400: ("No such account or incorrect password", "Check username and password."),
    401: ("Disabled account", "This account is disabled in DSM. Contact your NAS administrator."),
    402: (
        "Permission denied",
        "This account does not have permission to use this service. "
        "Check DSM > Control Panel > User > Applications.",
    ),
    403: ("2FA required", "Run 'mcp-synology setup' to complete 2FA bootstrap."),
    404: (
        "2FA code failed",
        "The OTP code was incorrect or expired. Try again with a fresh code.",
    ),
    406: (
        "OTP enforcement required",
        "Admin requires 2FA. Enable 2FA in DSM > Personal > Security.",
    ),
    407: (
        "Max login attempts exceeded",
        "Account is temporarily locked. Wait a few minutes and try again.",
    ),
    408: (
        "IP blocked",
        "Too many failed attempts. Check DSM > Control Panel > Security > Auto Block.",
    ),
    409: (
        "SID required",
        "Session ID is required for this operation.",
    ),
    410: (
        "Token expired",
        "The login token has expired. Re-authenticate.",
    ),
}

# File Station error codes (400-series).
FILESTATION_ERROR_CODES: dict[int, tuple[str, str]] = {
    400: ("Invalid parameter", "Check path format and parameter values."),
    401: ("Unknown error", "An unknown file operation error occurred. Retry or check NAS logs."),
    402: ("System too busy", "The NAS is too busy. Wait and retry."),
    408: ("No such file or directory", "Use list_files or search_files to find the correct path."),
    414: ("File already exists", "Use overwrite=true to replace existing files."),
    415: ("Disk quota exceeded", "Free space or contact NAS administrator."),
    416: ("No space left on device", "Free space on the NAS."),
    418: (
        "Illegal name or path",
        'Avoid characters: / \\ : * ? " < > |',
    ),
    419: (
        "Illegal file name",
        'Avoid characters: / \\ : * ? " < > |',
    ),
    421: ("Device or resource busy", "The file is in use by another process. Wait and retry."),
    599: ("No such task", "Background task not found (may have already completed)."),
    600: (
        "Search folder access denied",
        "The user may not have permission to search this folder. "
        "Try searching a specific share instead of the root path.",
    ),
    900: ("Unexpected server error", "Check DSM logs for details."),
    1000: (
        "Failed to copy files",
        "Check source paths exist and user has read permission.",
    ),
    1001: (
        "Failed to move files",
        "Check source paths exist and user has read/write permission.",
    ),
    1002: (
        "Destination error during copy/move",
        "Check destination folder exists, user has write permission, "
        "and file ownership allows the operation. "
        "Files owned by other users (e.g., service accounts) may require admin access.",
    ),
    1100: (
        "Insufficient filesystem permissions",
        "The DSM user does not have write access to this shared folder. "
        "Check DSM > Control Panel > Shared Folder > Permissions.",
    ),
    1101: (
        "Unsupported target filesystem",
        "The destination filesystem does not support this operation.",
    ),
    1800: ("Disk quota exceeded during upload", "Free space or contact NAS administrator."),
    1801: ("No permission to upload", "Check DSM shared folder write permissions."),
    1802: ("Upload error", "Check the destination path and disk space."),
    1803: ("Upload timeout", "Try a smaller file or increase the timeout."),
}


def error_from_code(code: int, api_name: str = "") -> SynologyError:
    """Create a typed exception from a DSM error code.

    Error codes are context-specific: the same code means different things
    for Auth vs FileStation APIs. This function checks API-specific codes
    first, then falls back to common codes.
    """
    # Auth API codes (400-series for SYNO.API.Auth)
    if "Auth" in api_name and code in AUTH_ERROR_CODES:
        message, suggestion = AUTH_ERROR_CODES[code]
        if code in (400, 401, 403, 404, 406):
            return AuthenticationError(message, code=code, suggestion=suggestion)
        return SynologyError(message, code=code, suggestion=suggestion)

    # File Station codes (400-series for SYNO.FileStation.*)
    if "FileStation" in api_name and code in FILESTATION_ERROR_CODES:
        message, suggestion = FILESTATION_ERROR_CODES[code]
        if code == 408:
            return PathNotFoundError(message, code=code, suggestion=suggestion)
        if code == 414:
            return SynologyFileExistsError(message, code=code, suggestion=suggestion)
        if code == 416:
            return DiskFullError(message, code=code, suggestion=suggestion)
        if code in (418, 419):
            return IllegalNameError(message, code=code, suggestion=suggestion)
        return FileStationError(message, code=code, suggestion=suggestion)

    # Common codes (100-series, shared across all APIs)
    if code in COMMON_ERROR_CODES:
        message, suggestion = COMMON_ERROR_CODES[code]
        if code == 105:
            return SynologyPermissionError(message, code=code, suggestion=suggestion)
        if code in (106, 107, 119):
            return SessionExpiredError(message, code=code, suggestion=suggestion)
        if code == 102:
            return ApiNotFoundError(message, code=code, suggestion=suggestion)
        return SynologyError(message, code=code, suggestion=suggestion)

    return SynologyError(f"Unknown error (code {code})", code=code)
