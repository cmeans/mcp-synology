"""Synology Photos API error codes (``SYNO.Foto.*`` and ``SYNO.FotoTeam.*``).

Photos uses its own numbering (117, 160, 602, 620, 801, …), different from File
Station's and Download Station's 400-series. Every code here was seen on DSM 7.1
(Photos 1.3.4) or 7.2.2 (Photos 1.9.1) except 901, which comes from the 1.3.4 web
client; see ``docs/specs/photos-module-spec.md`` (Errors, Prerequisites).

To add a new code: add an entry below; its ``ErrorCode`` must have a section in
docs/error-codes.md (tests/core/test_help_urls.py enforces that).
"""

from __future__ import annotations

from mcp_synology.core.errors import ErrorCode, SynologyError


class PhotosError(SynologyError):
    """A Synology Photos API error; ``error_code`` depends on the DSM code."""

    def __init__(
        self,
        message: str,
        *,
        code: int,
        suggestion: str | None,
        error_code: ErrorCode,
    ) -> None:
        super().__init__(message, code=code, suggestion=suggestion)
        self.error_code = error_code


_ADMIN_PHOTOS_PRIVILEGE = (
    "A DSM administrator grants it in Control Panel > Application Privileges > Synology Photos."
)

PHOTOS_ERROR_CODES: dict[int, tuple[ErrorCode, str, str]] = {
    101: (
        ErrorCode.INVALID_PARAMETER,
        "Photos rejected a parameter",
        "Check the values passed; Photos can't change some fields (e.g. a photo's location).",
    ),
    103: (
        ErrorCode.NOT_SUPPORTED,
        "This Synology Photos version doesn't have that action",
        "It needs a newer Synology Photos; check Package Center for an update.",
    ),
    117: (
        ErrorCode.NOT_FOUND,
        "Photos found no such item, folder or task",
        "List again to get current ids; a finished task disappears once cleared.",
    ),
    160: (
        ErrorCode.PERMISSION_DENIED,
        "This account can't use Synology Photos",
        _ADMIN_PHOTOS_PRIVILEGE,
    ),
    602: (
        ErrorCode.UNAVAILABLE,
        "This account has no Photos personal space: the user home service is off",
        "A DSM administrator enables it in Control Panel > User & Group > Advanced > "
        "User Home. Shared Space (space=shared) doesn't need it.",
    ),
    620: (
        ErrorCode.INVALID_PARAMETER,
        "Synology Photos doesn't support this file type",
        "Upload photos and videos only; other files can go through File Station's "
        "upload_file (needs File Station write permission) or DSM.",
    ),
    621: (
        ErrorCode.NOT_FOUND,
        "The destination folder doesn't exist in Photos",
        "List folders with list_photo_collections type=folder, or create it first.",
    ),
    641: (
        ErrorCode.ALREADY_EXISTS,
        "A folder with that name already exists there",
        "Pick another name, or use the existing folder.",
    ),
    642: (
        ErrorCode.NOT_FOUND,
        "No such folder in Photos",
        "List folders with list_photo_collections type=folder.",
    ),
    801: (
        ErrorCode.PERMISSION_DENIED,
        "Shared Space is off, or this account has no access to it",
        "An administrator enables Shared Space in Synology Photos > Settings > Shared "
        "Space and grants this account access there.",
    ),
    803: (
        ErrorCode.PERMISSION_DENIED,
        "No permission for this Shared Space folder",
        "A Shared Space manager grants folder permissions in Synology Photos.",
    ),
    901: (
        ErrorCode.UNAVAILABLE,
        "People (face recognition) is turned off in Synology Photos",
        "Turn it on in Synology Photos > Settings > Personal (or Shared Space) > People.",
    ),
    950: (
        ErrorCode.INVALID_PARAMETER,
        "The smart album condition names a tag, person or folder that no longer exists",
        "Pass tags by name so they're looked up again; tag ids can change after "
        "Photos re-indexes a file.",
    ),
    953: (
        ErrorCode.INVALID_PARAMETER,
        "Photos can't use this smart album condition",
        "Give at least one condition besides the owner (folders, people, tags, places, "
        "dates, type or ratings).",
    ),
}
