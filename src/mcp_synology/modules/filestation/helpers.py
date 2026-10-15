"""File Station helpers: path normalization, size parsing, async polling."""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from mcp_synology.core.client import DsmClient
from mcp_synology.core.errors import ErrorCode, SynologyError
from mcp_synology.core.formatting import error_response

logger = logging.getLogger(__name__)


def dsm_str(value: str) -> str:
    """JSON-quote a free-text parameter value for a JSON-format File Station API.

    DSM parses these values as JSON first (#121), so an unquoted value that is
    valid JSON is read as that type: a folder named ``2024`` or ``true`` gets
    error 400, a ``list`` pattern of ``2024`` crashes the request (HTTP 502),
    and a Search/DirSize task ID like ``17918443788CE649B9`` matches no task.
    Synology's API guide quotes every such value. Paths need no quoting: they
    start with ``/`` and so are never valid JSON.
    """
    return json.dumps(value, ensure_ascii=False)


# A quoted task ID that DSM rejects outright. Reported once on DSM 7.2.2
# (esausilva/synology-api-sdk#1), never seen on vdsm 7.1/7.2.2 or a real
# DS1618+, so it's a fallback, not the default.
_QUOTED_TASKID_REJECTED = frozenset({599, 600})


async def task_request(
    client: DsmClient,
    api: str,
    method: str,
    taskid: str,
    *,
    version: int | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call a Search/DirSize task method with ``taskid`` JSON-quoted (#121).

    An unquoted ID whose first character after the 10-digit timestamp is a
    digit is misread, so ``list`` answers DSM's unknown-task reply, ``status``
    fails with 599, and ``stop``/``clean`` silently clean nothing. If DSM
    rejects the quoted form (599/600), retry once unquoted.
    """
    base = dict(params or {})
    try:
        return await client.request(
            api, method, version=version, params={**base, "taskid": dsm_str(taskid)}
        )
    except SynologyError as e:
        if e.code not in _QUOTED_TASKID_REJECTED:
            raise
        logger.warning(
            "%s/%s rejected quoted taskid %s (DSM %s); retrying unquoted",
            api,
            method,
            taskid,
            e.code,
        )
        return await client.request(api, method, version=version, params={**base, "taskid": taskid})


# Union of `additional` field values DSM 7.x accepts across SYNO.FileStation.List
# and SYNO.FileStation.Search. Tighter per-API whitelisting isn't worth the
# maintenance cost — DSM silently ignores values that don't apply to a given
# endpoint (e.g. `mount_point_type` on a non-share path simply doesn't appear
# in the response), so the union acts as a typo-and-injection guard without
# blocking valid use cases. Documented in `docs/specs/filestation-module-spec.md`.
_VALID_ADDITIONAL_FIELDS: frozenset[str] = frozenset(
    {
        "real_path",
        "size",
        "owner",
        "time",
        "perm",
        "type",
        "mount_point_type",
        "volume_status",
    }
)


def validate_additional(values: list[str] | None, *, tool_name: str) -> None:
    """Reject unknown `additional` field names before they hit DSM.

    DSM accepts unknown values silently (the field just doesn't appear in the
    response), which makes typos invisible to callers. Validating up-front
    surfaces the typo as a clear ToolError naming the bad value and listing
    the supported set.
    """
    if not values:
        return
    unknown = [v for v in values if v not in _VALID_ADDITIONAL_FIELDS]
    if unknown:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"{tool_name} failed: unknown 'additional' field(s): {sorted(set(unknown))!r}.",
            retryable=False,
            param="additional",
            value=values,
            suggestion=("Supported fields: " + ", ".join(sorted(_VALID_ADDITIONAL_FIELDS)) + "."),
        )


# Size unit multipliers, all binary (1 KiB = 1024 bytes). Output is labeled KiB/MiB/...;
# input also takes KB/MB/... with the same binary meaning, since that's what users type.
_SIZE_UNITS: dict[str, int] = {
    "B": 1,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
}

# Pattern for parsing human-readable sizes like "1.5GiB", "500 MB"
_SIZE_PATTERN = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(B|[KMGT]i?B)\s*$",
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

    # DSM share names aren't case-sensitive: /TESTSHARE/... works for share "testshare"
    # (DSM 7.1 and 7.2.2; QA R1 on #173).
    if share.casefold() not in {s.casefold() for s in known_shares}:
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
        - Strings: "500MiB", "2GiB", "1.5TiB" (case-insensitive). KB/MB/GB/TB are
          accepted too and mean the same binary units.

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
            f"Use a number (bytes) or a human-readable size like '500MiB', '2GiB', '1.5TiB'."
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


# Closes #37. Lazy per-share recycle-bin probe + observation-based self-correction.
#
# `recycle_status` is a closure-captured `dict[str, bool]` shared across every
# tool handler in the filestation module (see modules/filestation/__init__.py
# where it's created and threaded through the `recycle_bin_status=` kwarg).
# Before this helper landed, the dict was created empty and never populated:
# every share looked like recycle-on by default, so `delete_files` always told
# the user their files were recoverable from `#recycle` even when the share
# had recycle disabled and the data was actually gone.
#
# Strategy: probe lazily per share on first observation, cache the result for
# the life of the session, and let the auth manager invalidate the cache on
# session re-auth (since admin could toggle `#recycle` between sessions).
#
# Probe: `SYNO.FileStation.List` on `/{share}/#recycle/` with `limit=0`. Cheap;
# only fetches the directory entry, not its contents.
#   - Success                     -> recycle bin enabled  (True)
#   - DSM 408 (path not found)    -> recycle bin disabled (False)
#   - DSM 105 (permission denied) -> unknown; default True + WARN so the
#       message stays optimistic about recoverability and the operator sees a
#       diagnostic in the log
#   - Other errors                -> unknown; same optimistic-True + WARN
async def ensure_recycle_status(
    client: DsmClient,
    share_name: str,
    recycle_status: dict[str, bool],
) -> bool:
    """Return cached recycle-bin status for `share_name`, probing on first call.

    Caches the result in `recycle_status` in-place so subsequent calls hit the
    dict and skip the probe. Probe failures fall back to True (the optimistic
    default that preserves prior messaging behavior) and emit a WARNING.
    """
    if share_name in recycle_status:
        return recycle_status[share_name]

    try:
        await client.request(
            "SYNO.FileStation.List",
            "list",
            params={"folder_path": f"/{share_name}/#recycle", "limit": 0},
        )
        recycle_status[share_name] = True
        logger.debug("Probed recycle bin on /%s: enabled", share_name)
    except SynologyError as e:
        if e.code == 408:
            recycle_status[share_name] = False
            logger.debug("Probed recycle bin on /%s: disabled (#recycle missing)", share_name)
        elif e.code == 105:
            # Permission denied — the bot user lacks read access on
            # `/{share}/#recycle`. Operator-actionable: grant the MCP
            # service account read on the recycle subfolder. We can't
            # tell whether the bin is on or off from here; default to
            # enabled so messaging stays optimistic.
            recycle_status[share_name] = True
            logger.warning(
                "Recycle-bin probe on /%s returned DSM 105 (permission denied); "
                "assuming enabled. Grant the MCP service account read access on "
                "/%s/#recycle for accurate delete-files messaging.",
                share_name,
                share_name,
            )
        else:
            recycle_status[share_name] = True
            logger.warning(
                "Recycle-bin probe on /%s returned DSM error %s; assuming enabled. "
                "Delete-files messaging may be incorrect for this share until re-auth.",
                share_name,
                e.code,
            )

    return recycle_status[share_name]


def correct_recycle_status_from_observation(
    share_name: str,
    observed_enabled: bool,
    recycle_status: dict[str, bool],
) -> None:
    """Update the cache when a tool observes DSM behavior that contradicts it.

    For example, `list_recycle_bin` may catch a DSM 408 ``not_found`` on
    `/share/#recycle` when the cache says the share has recycle enabled —
    which means the cache is stale (admin disabled it mid-session). Flip the
    bit, log it, and let subsequent calls see the corrected state without
    waiting for re-auth invalidation.
    """
    cached = recycle_status.get(share_name)
    if cached is None or cached == observed_enabled:
        # Nothing to correct; let `ensure_recycle_status` populate normally.
        recycle_status.setdefault(share_name, observed_enabled)
        return
    logger.info(
        "Self-correcting recycle-bin cache on /%s: cached=%s, observed=%s",
        share_name,
        cached,
        observed_enabled,
    )
    recycle_status[share_name] = observed_enabled


_WILDCARD_CHARS = ("*", "?")


async def find_missing_paths(client: DsmClient, paths: list[str]) -> list[tuple[str, int]]:
    """Return ``(path, dsm_code)`` for each path that ``getinfo`` reports as missing.

    DSM answers a missing path with a per-file ``{"code": 408, "path": ...}`` entry rather
    than a failed request (DSM 7.1 and 7.2.2; #161, #162). An answer with no entry for a
    path proves nothing either way, so that path isn't reported. A failed request raises
    ``SynologyError`` for the caller to map.
    """
    version = min(2, client.negotiate_version("SYNO.FileStation.List", max_version=2))
    missing: list[tuple[str, int]] = []
    for path in paths:
        data = await client.request(
            "SYNO.FileStation.List", "getinfo", version=version, params={"path": path}
        )
        for entry in data.get("files", []):
            if "code" in entry:
                missing.append((str(entry.get("path") or path), int(entry["code"])))
    return missing


def missing_paths_suggestion(missing: list[tuple[str, int]]) -> str:
    """Suggestion for a call refused because paths don't exist (adds a wildcard hint)."""
    suggestion = (
        "Check the paths with list_files or search_files, then retry with paths that exist."
    )
    if any(ch in path for path, _ in missing for ch in _WILDCARD_CHARS):
        suggestion += (
            " Paths are literal, not wildcard patterns: use search_files (or list_files with a "
            "pattern) to find the files, then pass their full paths."
        )
    return suggestion


async def _refresh_known_shares(client: DsmClient, known_shares: set[str]) -> bool:
    """Reload ``known_shares`` from ``list_share``; return False if DSM won't say."""
    try:
        data = await client.request("SYNO.FileStation.List", "list_share", params={})
    except SynologyError as e:
        logger.warning("Couldn't list shared folders to validate paths (DSM %s): %s", e.code, e)
        return False
    known_shares.clear()
    known_shares.update(str(s["name"]) for s in data.get("shares", []) if s.get("name"))
    logger.debug("Known shares: %s", sorted(known_shares))
    return True


async def ensure_known_shares(client: DsmClient, paths: list[str], known_shares: set[str]) -> None:
    """Raise ``not_found`` for a path whose first component isn't a shared folder (#160).

    ``known_shares`` is a cache owned by the caller (cleared on re-auth). It's filled on
    first use and reloaded once when a path names a share it doesn't hold, so a share
    created since is still found. For an account that isn't an admin, the list is the
    shares it can see, which is what it should be told about. If DSM won't list shares,
    the check is skipped and DSM's own error stands.
    """
    if not paths:
        return

    def _shares() -> set[str]:
        return {
            normalize_path(p).split("/")[1].casefold() for p in paths if len(normalize_path(p)) > 1
        }

    known = {s.casefold() for s in known_shares}
    if (not known_shares or not _shares() <= known) and not await _refresh_known_shares(
        client, known_shares
    ):
        return
    for path in paths:
        problem = validate_share_path(path, known_shares)
        if problem:
            error_response(
                ErrorCode.NOT_FOUND,
                problem,
                retryable=False,
                param="path",
                value=path,
                suggestion="Call list_shares to see the shared folders on this NAS.",
            )
