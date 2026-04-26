"""File Station helpers: path normalization, size parsing, async polling."""

from __future__ import annotations

import fnmatch
import logging
import re
from datetime import UTC, datetime

from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)

# Size unit multipliers (binary: 1 KB = 1024 bytes)
_SIZE_UNITS: dict[str, int] = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
}

# Pattern for parsing human-readable sizes like "1.5GB", "500 MB"
_SIZE_PATTERN = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)\s*$",
    re.IGNORECASE,
)

# Video file extensions for icon display
_VIDEO_EXTENSIONS = frozenset(
    {
        "mkv",
        "mp4",
        "avi",
        "mov",
        "wmv",
        "flv",
        "webm",
        "m4v",
        "mpg",
        "mpeg",
        "ts",
    }
)


def normalize_path(path: str) -> str:
    """Normalize a file path for the DSM API.

    - Prepend `/` if missing
    - Strip trailing `/` (unless root)
    """
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def validate_share_path(path: str, known_shares: set[str]) -> str | None:
    """Validate that the first path component is a known shared folder.

    Returns an error message if invalid, or None if valid.
    """
    normalized = normalize_path(path)
    parts = normalized.split("/")
    # parts[0] is empty string (before leading /), parts[1] is the share name
    if len(parts) < 2 or not parts[1]:
        return f"Invalid path '{path}': must start with a shared folder name."

    share = parts[1]
    # Strip #recycle if present — user might be browsing recycle bin
    if share == "#recycle":
        return f"Invalid path '{path}': must start with a shared folder name, not #recycle."

    if share not in known_shares:
        available = ", ".join(sorted(known_shares)) if known_shares else "(none)"
        return (
            f"Unknown shared folder '{share}'. "
            f"Available shares: {available}. "
            f"Use list_shares to see all shared folders."
        )
    return None


def parse_human_size(value: str | int) -> int:
    """Parse a human-readable size string into bytes.

    Accepts:
        - Integers (treated as bytes): 1048576
        - Strings: "500MB", "2GB", "1.5TB" (case-insensitive, binary units)

    Raises ValueError for invalid input.
    """
    if isinstance(value, int):
        return value

    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())

    match = _SIZE_PATTERN.match(str(value))
    if not match:
        msg = (
            f"Invalid size '{value}'. "
            f"Use a number (bytes) or a human-readable size like '500MB', '2GB', '1.5TB'."
        )
        raise ValueError(msg)

    number = float(match.group(1))
    unit = match.group(2).upper()
    return int(number * _SIZE_UNITS[unit])


def parse_mtime(value: str) -> int:
    """Parse an mtime filter into Unix epoch seconds.

    Accepts ``YYYY-MM-DD``, ISO 8601 datetime (with or without
    timezone offset), or a numeric epoch-seconds string. Naive
    datetimes are treated as UTC for stable cross-host behavior.

    Raises ValueError for unrecognized input.
    """
    s = value.strip()
    if s.lstrip("-").isdigit():
        return int(s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        msg = (
            f"Invalid mtime value {value!r}. "
            "Use ISO 8601 (e.g. '2026-04-01T12:00:00+00:00'), "
            "a calendar date ('YYYY-MM-DD'), or Unix epoch seconds."
        )
        raise ValueError(msg) from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def file_type_icon(is_dir: bool, filename: str = "", style: str = "emoji") -> str:
    """Get a file type indicator icon.

    Args:
        is_dir: Whether this is a directory.
        filename: The filename (used to detect video files).
        style: "emoji" or "text".
    """
    if is_dir:
        return "\U0001f4c1" if style == "emoji" else "[DIR]"

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in _VIDEO_EXTENSIONS:
        return "\U0001f3ac" if style == "emoji" else "[VIDEO]"

    return "\U0001f4c4" if style == "emoji" else "[FILE]"


def escape_multi_path(paths: list[str]) -> str:
    """Escape and comma-join paths for DSM multi-path parameters.

    Delegates to DsmClient.escape_path_param.
    """
    return DsmClient.escape_path_param(paths)


def matches_pattern(filename: str, pattern: str) -> bool:
    """Check if a filename matches a glob pattern (case-insensitive)."""
    return fnmatch.fnmatch(filename.lower(), pattern.lower())
