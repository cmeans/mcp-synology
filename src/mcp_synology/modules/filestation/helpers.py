"""File Station helpers: path normalization, size parsing, async polling."""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NoReturn

import httpx

from mcp_synology.core.client import DsmClient
from mcp_synology.core.display import shown
from mcp_synology.core.errors import (
    AuthenticationError,
    DsmHttpError,
    DsmReplyError,
    ErrorCode,
    SynologyError,
    error_from_code,
)
from mcp_synology.core.formatting import (
    ESCAPED_NAME_ADVICE,
    error_response,
    holds_escape,
)
from mcp_synology.core.redaction import redact_urls

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)


def never_sent(exc: Exception) -> bool:
    """The request never reached DSM: its connection couldn't be opened (as Photos'
    ``photo_task`` says). A pool timeout belongs here too, but that's #442."""
    return isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)


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


def describe_transport_error(exc: httpx.TransportError) -> str:
    """``ReadTimeout``, or ``ConnectError: [Errno 111] ...``; never empty (#237).

    httpx's timeouts stringify to ``''``. Any URL in the text is redacted.
    """
    text = redact_urls(str(exc))
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def unanswered_status_response(
    operation: str,
    path: str,
    exc: httpx.TransportError,
    *,
    task_note: str = "",
    already: str = "",
    suggestion: str | None = None,
) -> NoReturn:
    """Raise the structured error for a background task whose status DSM stopped answering.

    DSM can stall for longer than the connection timeout while it deletes or copies
    very large files (#237); httpx's error used to escape with an empty message.
    """
    timed_out = isinstance(exc, httpx.TimeoutException)
    what = "didn't answer" if timed_out else "lost the connection during"
    if never_sent(exc):
        # The connection was never opened, so the request never reached DSM (as Photos says),
        # and a longer timeout or a smaller folder wouldn't help.
        what = "couldn't be reached for"
        suggestion = (
            "Check that the NAS is on and reachable from this machine, then try again; "
            "list the path to see where the task got to."
        )
    note = f" {task_note}" if task_note else ""
    error_response(
        ErrorCode.TIMEOUT if timed_out else ErrorCode.UNAVAILABLE,
        f"{operation} failed: DSM {what} a status request on path: {shown(path)}"
        f" ({describe_transport_error(exc)}).{note}{already}",
        retryable=True,
        param="path",
        value=path,
        suggestion=suggestion
        or (
            "List the path to see where it got to before retrying. DSM can stall on very "
            "large files; a larger connection timeout (connection.timeout in the config) "
            "gives it longer to answer, or use DSM's own File Station for this one."
        ),
    )


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


# The `additional` fields get_file_info accepts: exactly the ones its output shows,
# so nothing requested is silently dropped (#149). It's the only tool with the
# parameter; the listing tools fetch fixed fields. `volume_status` is left out: only
# list_share returns it. Documented in `docs/specs/filestation-module-spec.md`.
_VALID_ADDITIONAL_FIELDS: frozenset[str] = frozenset(
    {
        "real_path",
        "size",
        "owner",
        "time",
        "perm",
        "type",
        "mount_point_type",
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
    "PIB": 1024**5,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
    "PB": 1024**5,
}

# Pattern for parsing human-readable sizes like "1.5GiB", "500 MB"
_SIZE_PATTERN = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(B|[KMGTP]i?B)\s*$",
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


def strip_padding(path: str) -> str:
    """``path`` without padding: whitespace before a leading ``/``, or the whole of a path
    that is only whitespace (#488). Anything else is part of the path, kept exactly."""
    if not path.strip():
        return ""
    unpadded = path.lstrip()
    return unpadded if unpadded.startswith("/") else path


def name_key(path: str) -> str:
    """A path's last part as a same-name check compares it: ignoring case, the widest way, as a
    refusal costs less than a file lost on a file system that folds case (#495)."""
    return path.rsplit("/", 1)[-1].casefold()


def normalize_path(path: str) -> str:
    """Normalize a file path for the DSM API.

    - Drop whitespace before a leading ``/`` (padding around the path, not part of it)
    - Prepend `/` if missing
    - Strip trailing `/` (unless root)

    Nothing else is stripped (#488): DSM keeps whitespace in a name exactly (``foo ``,
    `` foo``, ``foo<TAB>``, ``foo<NBSP>`` and ``" "`` are names beside ``foo``; getinfo,
    List, Rename and Delete act on exactly the named one; vdsm DSM 7.1, 7.2.2, 7.3.2 and
    7.4.1, admin and not). Stripping sent ``/video/x/foo `` as ``/video/x/foo``, a different
    item, and ``/video/x/ `` as its parent. A path of whitespace only is empty.
    """
    path = strip_padding(path)
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        # A path of slashes only is the root, not an empty string (pre-QA on #510).
        path = path.rstrip("/") or "/"
    return path


def validate_share_path(path: str, known_shares: set[str]) -> str | None:
    """Validate that the first path component is a known shared folder.

    Returns an error message if invalid, or None if valid.
    """
    normalized = normalize_path(path)
    parts = normalized.split("/")
    # parts[0] is empty string (before leading /), parts[1] is the share name
    if len(parts) < 2 or not parts[1]:
        return f"Invalid path {path!r}: must start with a shared folder name."

    share = parts[1]
    # Strip #recycle if present — user might be browsing recycle bin
    if share == "#recycle":
        return f"Invalid path {path!r}: must start with a shared folder name, not #recycle."

    # DSM takes an ordinary share in any case: /TESTSHARE/... works for share "testshare"
    # (DSM 7.1 and 7.2.2; QA R1 on #173). The user home service doesn't: /Home is 408 (#353).
    if share.casefold() not in {s.casefold() for s in known_shares}:
        available = ", ".join(sorted(known_shares)) if known_shares else "(none)"
        # A path passed back with the quotes a reply showed it in (`shown`): say so, as
        # advice only, so a share really named that way isn't refused (pre-QA r2 on #692).
        quotes = (
            " If the path came from a reply in quotes, pass it without them."
            if share in ("'", '"')
            else ""
        )
        return (
            f"Unknown shared folder {share!r}. "
            f"Available shares: {available}. "
            f"Use list_shares to see all shared folders.{quotes}"
        )
    return None


def parse_human_size(value: str | int) -> int:
    """Parse a human-readable size string into bytes.

    Accepts:
        - Integers (treated as bytes): 1048576
        - Strings: "500MiB", "2GiB", "1.5TiB" (case-insensitive), up to PiB. KB/MB/GB/TB/PB
          are accepted too and mean the same binary units.

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
    """Check if a filename matches a wildcard pattern (case-insensitive).

    Only ``*`` and ``?`` are wildcards; everything else, ``[...]`` and commas included,
    matches itself, and the pattern matches the whole name. DSM's own patterns don't read
    ``[...]`` as a class either, but differ otherwise: a comma separates alternatives, and
    a pattern without ``*`` or ``?`` matches anywhere (``dsm_pattern_matcher``). With
    fnmatch, ``*[1080p]*`` was a character class that left out every name containing 1,
    0, 8 or p (#396), so ``[`` is escaped as ``[[]``. fnmatch itself stays: its translation
    doesn't backtrack, while a regex with one ``.*`` per ``*`` does, badly, on long names.
    """
    return fnmatch.fnmatchcase(filename.lower(), pattern.lower().replace("[", "[[]"))


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
# Probe: `SYNO.FileStation.List` on `/{share}/#recycle/` with `limit=1`: one entry at
# most (DSM's `limit=0` means all of them, #353).
#   - Success                     -> recycle bin enabled (True), cached
#   - DSM 408 (no `#recycle`)     -> ask the share's own setting (`SYNO.Core.Share list`,
#       `additional=["recyclebin"]`; for /home, the user home service's,
#       `SYNO.Core.User.Home get`), which only an administrator can read: its answer is
#       cached. For anyone else (DSM 105) the answer is False, NOT cached (#409): DSM
#       creates `#recycle` when the share is created and again at the first delete into
#       the bin, so a missing folder means the bin is off, or on with nothing deleted into
#       it yet (a new account's home), or that someone deleted `#recycle` itself. Checked
#       again on each use, a delete into a bin that's on shows up as soon as DSM re-creates
#       the folder.
#   - Anything else (105 permission denied, another DSM error, no answer) -> unknown
#       (None), NOT cached (#255): the next call probes again. `ensure_recycle_status`
#       then answers True so `delete_files` stays optimistic about recoverability;
#       `list_shares` shows "couldn't check". A WARNING names the cause.
# Shares whose bin couldn't be checked were warned about once per server run; later
# misses log at DEBUG, since an uncached share is checked again on every use (#255).
_warned_recycle: set[str] = set()


def _warn_once(share_name: str, message: str, *args: object) -> None:
    level = logging.DEBUG if share_name in _warned_recycle else logging.WARNING
    _warned_recycle.add(share_name)
    logger.log(level, message, *args)


async def _share_recycle_setting(client: DsmClient, share_name: str) -> bool | None:
    """The share's own recycle-bin setting, or None when this account can't read it.

    Only an administrator can read it (105 for anyone else, seska DSM 7.1.1). The user
    home folder ``/home`` takes the user home service's setting (``SYNO.Core.User.Home``,
    Control Panel > User > Advanced): the ``homes`` share's own flag can disagree with it
    (vdsm: ``homes`` off while every user's home bin is on).
    """
    try:
        if share_name == "home":
            data = await client.request("SYNO.Core.User.Home", "get")
            setting = data.get("enable_recycle_bin")
            return setting if isinstance(setting, bool) else None
        data = await client.request(
            "SYNO.Core.Share",
            "list",
            params={"shareType": "all", "additional": json.dumps(["recyclebin"])},
        )
    except SynologyError as e:
        logger.debug("Can't read /%s's recycle-bin setting: %s", share_name, e.code or e)
        return None
    for share in data.get("shares", []):
        if share.get("name") == share_name and isinstance(share.get("enable_recycle_bin"), bool):
            return bool(share["enable_recycle_bin"])
    return None


async def probe_recycle_status(client: DsmClient, share_name: str) -> tuple[bool | None, bool]:
    """Probe `/{share_name}/#recycle`: (True on, False off or no bin folder, None can't
    tell; definite).

    ``definite`` says whether the answer may be cached. A missing ``#recycle`` that
    the share's own setting can't explain is ``(False, False)``: no bin folder yet.
    """
    try:
        await client.request(
            "SYNO.FileStation.List",
            "list",
            params={"folder_path": f"/{share_name}/#recycle", "limit": 1},
        )
    except SynologyError as e:
        if e.code == 408 and not isinstance(e, AuthenticationError):
            setting = await _share_recycle_setting(client, share_name)
            if setting is not None:
                logger.debug(
                    "Probed recycle bin on /%s: #recycle missing; the share's setting is %s",
                    share_name,
                    "on" if setting else "off",
                )
                return setting, True
            logger.debug("Probed recycle bin on /%s: no #recycle folder yet", share_name)
            return False, False
        if e.code == 105:
            _warn_once(
                share_name,
                "Recycle-bin probe on /%s returned DSM 105 (permission denied); can't tell "
                "whether its recycle bin is on. Grant the MCP service account read access "
                "on /%s/#recycle for accurate delete-files messaging.",
                share_name,
                share_name,
            )
        else:
            _warn_once(
                share_name,
                "Recycle-bin probe on /%s failed (%s); can't tell whether its recycle bin "
                "is on. It will be checked again on next use.",
                share_name,
                f"DSM error {e.code}" if e.code is not None else type(e).__name__,
            )
        return None, False
    logger.debug("Probed recycle bin on /%s: enabled", share_name)
    return True, True


async def ensure_recycle_status(
    client: DsmClient,
    share_name: str,
    recycle_status: dict[str, bool],
) -> bool:
    """Return cached recycle-bin status for `share_name`, probing when it isn't cached.

    Caches a definite answer in `recycle_status` in place so later calls skip the
    probe. A missing ``#recycle`` that isn't definite answers False uncached (#409),
    and a probe that can't tell answers True uncached (the optimistic default that
    keeps delete-files messaging hopeful, #255).
    """
    if share_name in recycle_status:
        return recycle_status[share_name]
    status, definite = await probe_recycle_status(client, share_name)
    if status is None:
        return True
    if definite:
        recycle_status[share_name] = status
    return status


def correct_recycle_status_from_observation(
    share_name: str,
    observed_enabled: bool,
    recycle_status: dict[str, bool],
) -> None:
    """Update the cache when a tool observes DSM behavior that contradicts it.

    Seeing ``/share/#recycle`` list proves the bin is on: cache True. Not finding it
    (408 from `list_recycle_bin`) proves nothing (#409), so it only drops a cached
    True, and the next call checks again.
    """
    cached = recycle_status.get(share_name)
    if cached == observed_enabled:
        return
    if cached is not None:
        logger.info(
            "Self-correcting recycle-bin cache on /%s: cached=%s, observed=%s",
            share_name,
            cached,
            observed_enabled,
        )
    if observed_enabled:
        recycle_status[share_name] = True
    else:
        recycle_status.pop(share_name, None)


_WILDCARD_CHARS = ("*", "?")


async def find_missing_paths(
    client: DsmClient, paths: list[str], *, as_sent: bool = False
) -> list[tuple[str, int]]:
    """Return ``(path, dsm_code)`` for each path that ``getinfo`` reports as missing.

    DSM answers a missing path with a per-file ``{"code": 408, "path": ...}`` entry rather
    than a failed request (DSM 7.1 and 7.2.2; #161, #162). An answer with no entry for a
    path proves nothing either way, so that path isn't reported. A failed request raises
    ``SynologyError``, or a transport error with no answer, for the caller to map.
    ``as_sent``: each path as it was sent, not as DSM's entry spells it, for a caller that
    matches the result against its own paths (pre-QA r4 on #714).
    """
    version = min(2, client.negotiate_version("SYNO.FileStation.List", max_version=2))
    missing: list[tuple[str, int]] = []
    for path in paths:
        data = await client.request(
            "SYNO.FileStation.List", "getinfo", version=version, params={"path": path}
        )
        for entry in data.get("files", []):
            if "code" in entry:
                shown_as = path if as_sent else str(entry.get("path") or path)
                missing.append((shown_as, int(entry["code"])))
    return missing


async def path_exists(client: DsmClient, path: str) -> bool | None:
    """One ``getinfo``: True when DSM shows the path, False when it answers 408 for it.

    None when the answer shows neither (no entry, or another code such as 407). A failed
    request raises for the caller to handle.
    """
    version = min(2, client.negotiate_version("SYNO.FileStation.List", max_version=2))
    data = await client.request(
        "SYNO.FileStation.List", "getinfo", version=version, params={"path": path}
    )
    for entry in data.get("files", []):
        if entry.get("code") == 408:
            return False
        if isinstance(entry.get("isdir"), bool):
            return True
    return None


# What a NAS that doesn't answer raises from a GET request (#517, #282): it ends the call.
# httpx's own transport error (timeouts included), or a proxy's error status or a page that
# isn't DSM's (`DsmHttpError`, `DsmReplyError`). `DsmTimeoutError` and `DsmUnavailableError`
# come only from the POST helpers, which these checks don't use.
_NO_ANSWER = (httpx.TransportError, DsmHttpError, DsmReplyError)


def no_answer(exc: BaseException) -> bool:
    """Whether ``exc`` is how a NAS that doesn't answer shows (`_NO_ANSWER`), for a caller that
    catches more (QA r1 on #714: `DsmHttpError` and `DsmReplyError` are `SynologyError`s)."""
    return isinstance(exc, _NO_ANSWER)


def _files(data: Any) -> list[dict[str, Any]]:
    """``getinfo``'s ``files`` entries that are objects; nothing for a malformed reply, which
    then reads as "can't tell" (pre-QA r5 on #517)."""
    files = data.get("files") if isinstance(data, dict) else None
    return [f for f in files if isinstance(f, dict)] if isinstance(files, list) else []


@dataclass(frozen=True)
class FolderKind:
    """What `folder_kind` found: ``kind`` is ``"folder"``, ``"file"`` or ``"missing"``;
    ``under_file`` is the file that makes a missing path impossible, for a path under a file."""

    kind: str
    under_file: str | None = None


async def folder_kind(client: DsmClient, path: str) -> FolderKind | None:
    """One ``getinfo``: a folder, a file or a missing path (DSM's 408 entry), or None when the
    answer shows none of these (another code, such as 407, or no entry) or DSM answers the
    request with an error code: a caller then lets DSM's own answer stand (#517).

    A NAS that doesn't answer ends the call, since the request after the check would wait as
    long again, past a client's 60 s (pre-QA r3): a transport error or timeout raises, as in
    `get_dir_size`'s pre-check, and so does a proxy's error status or a reply that isn't DSM's
    (`DsmHttpError`, `DsmReplyError`), which is how a proxy reports the same stall (#282)."""
    version = min(2, client.negotiate_version("SYNO.FileStation.List", max_version=2))
    try:
        data = await client.request(
            "SYNO.FileStation.List", "getinfo", version=version, params={"path": path}
        )
    except (*_NO_ANSWER, AuthenticationError):
        # A refused re-login is the login's own answer, not "can't tell" (#448; pre-QA r1 on
        # #705).
        raise
    except SynologyError as e:
        logger.debug("Couldn't check %s: %s: %s", path, type(e).__name__, e)
        return None
    for entry in _files(data):
        if entry.get("code") == 408:
            return FolderKind("missing")
        if entry.get("code") == 401:
            # DSM's code for a path under a file (`…/file/child`, getinfo and List alike, vdsm
            # 7.1 to 7.4.1): a file among its parents means it can't exist.
            blocker = await _file_above(client, path, version)
            return FolderKind("missing", blocker) if blocker else None
        if isinstance(entry.get("isdir"), bool):
            return FolderKind("folder" if entry["isdir"] else "file")
    return None


async def _file_above(
    client: DsmClient, path: str, version: int, seen: dict[str, dict[str, Any]] | None = None
) -> str | None:
    """The file among ``path``'s parents, when one is; None when none is found or DSM answers
    a parent with an error code (#517). A NAS that doesn't answer raises, as in `folder_kind`.

    The parents read folders down to the file, then 401 below it (vdsm 7.1: `f0.txt` a file,
    `f0.txt/x` and `f0.txt/x/y` 401), so the file is found by halving the levels: about
    log2(depth) requests, not one per level, which a deep path would make outlast a client's
    60 s (pre-QA r2). ``seen``: the entries of parents already read in this call, shared across
    paths, so paths in the same folders don't read them again (pre-QA r3 on #714: 50 paths
    with 401 for another reason cost 200 requests)."""
    parts = path.rstrip("/").split("/")
    # Parent lengths below the share (never a file) to the nearest parent (pre-QA r5).
    low, high = 3, len(parts) - 1
    while low <= high:
        end = (low + high) // 2
        parent = "/".join(parts[:end])
        entry: dict[str, Any] | None = None if seen is None else seen.get(parent)
        if entry is None:
            try:
                data = await client.request(
                    "SYNO.FileStation.List", "getinfo", version=version, params={"path": parent}
                )
            except (*_NO_ANSWER, AuthenticationError):
                raise
            except SynologyError as e:
                logger.debug("Couldn't check %s: %s: %s", parent, type(e).__name__, e)
                return None
            entry = next(iter(_files(data)), {})
            if seen is not None:
                seen[parent] = entry
        if entry.get("isdir") is False:
            return parent
        if entry.get("isdir") is True:
            low = end + 1  # a folder: the file, if any, is below it
        elif entry.get("code") == 401:
            high = end - 1  # under the file: it's above
        else:
            return None  # another code (a 408, a 407) or no answer: can't tell
    return None


async def paths_isdir(client: DsmClient, paths: list[str]) -> dict[str, bool]:
    """Return ``{path: isdir}`` for each path whose type ``getinfo`` reports.

    A path DSM gives no ``isdir`` for (missing, or an unexpected shape) is left out. A
    failed request raises ``SynologyError``, or a transport error with no answer, for the caller
    to handle.
    """
    version = min(2, client.negotiate_version("SYNO.FileStation.List", max_version=2))
    kinds: dict[str, bool] = {}
    for path in paths:
        data = await client.request(
            "SYNO.FileStation.List", "getinfo", version=version, params={"path": path}
        )
        for entry in data.get("files", []):
            if isinstance(entry.get("isdir"), bool):
                kinds[path] = entry["isdir"]
    return kinds


_NO_PERMISSION = frozenset({105, 407})
_CHECK_ACCESS = "check it and the account's access to it in DSM."
_OTHER_CODE = "DSM couldn't report the path: check it and the account's access to it in DSM."


async def files_above(client: DsmClient, missing: Sequence[tuple[str, int]]) -> dict[str, str]:
    """``{path: file}`` for each path ``getinfo`` answered 401 for that lies under a file (#702).

    DSM answers a path under a file (`…/f.txt/x`) with a 401 entry (vdsm 7.1 to 7.4.1), which
    `missing_text` alone would call "can't be read". `_file_above` finds the file; a path whose
    parents don't show one is left out, so its 401 stands. A NAS that doesn't answer raises, as
    in `folder_kind`."""
    version = min(2, client.negotiate_version("SYNO.FileStation.List", max_version=2))
    found: dict[str, str] = {}
    seen: dict[str, dict[str, Any]] = {}  # parents read, shared by the paths' searches
    for path, code in missing:
        if code != 401 or path in found:
            continue
        # A file already found above another path needs no search (pre-QA r1 on #702: 40 paths
        # under one file cost 160 requests). `seen` alone covers paths at one depth, not ones
        # at different depths, whose halving reads other parents (pre-QA r5 on #714).
        known = next((f for f in found.values() if path.startswith(f + "/")), None)
        blocker = known or await _file_above(client, path, version, seen)
        if blocker:
            found[path] = blocker
    return found


async def diagnose_under_file(client: DsmClient, path: str) -> str | None:
    """The file among ``path``'s parents, or None (#702), after DSM refused a request in a way
    that doesn't say why, which is how it answers a path under a file (`…/f.txt/x`, vdsm 7.1):
    `create_folder`'s 1100, `upload_file`'s 401, `rename`'s 1200. Only a diagnosis of that
    answer: whatever the check hits gives None and DSM's answer stands (a DSM error logged at
    DEBUG, no answer at WARNING). A check *before* a request (a getinfo 401 entry, as in
    `get_dir_size`, `download_file` and the copy or move destination) uses `files_above`
    instead, which lets no answer end the call (pre-QA r2 on #702)."""
    version = min(2, client.negotiate_version("SYNO.FileStation.List", max_version=2))
    try:
        return await _file_above(client, path, version)
    except Exception:
        logger.warning("Couldn't check whether %s is under a file", path, exc_info=True)
        return None


# The advice for a destination or new folder under a file (#702): nothing can be made there.
UNDER_FILE_ADVICE = (
    "A file can't hold files or folders: pick a folder that isn't below one (list_files shows "
    "what's there)."
)
# A restore's (QA r1 on #714): its folder may be the original one, which the caller never gave.
RESTORE_UNDER_FILE_ADVICE = (
    "A file can't hold files or folders: restore to a folder that isn't below one "
    "(dest_folder; list_files shows what's there)."
)


def under_file_text(path: str, file: str) -> str:
    """A path under a file, in words (#702), as `list_files` and `search_files` write it
    (#517)."""
    return f"{shown(path)} doesn't exist on the NAS: {shown(file)} is a file"


def missing_text(path: str, code: int, under_file: str | None) -> str:
    """A path ``getinfo`` couldn't report, in words (#620: "(DSM error 408)" alone left the
    model to translate DSM's code; a 407 was called "not found"), keeping the code.
    ``under_file``: the file above it (`files_above`), which means it doesn't exist (#702);
    required, so no caller forgets to look."""
    if under_file is not None:
        # The code beside "doesn't exist", as for a 408, so it isn't read as the file's (QA r1
        # on #714).
        return f"{shown(path)}: doesn't exist (DSM error {code}): {shown(under_file)} is a file"
    why = (
        "no permission"
        if code in _NO_PERMISSION
        else {408: "doesn't exist"}.get(code, "can't be read")
    )
    return f"{shown(path)}: {why} (DSM error {code})"


def unreadable_paths_error(
    missing: list[tuple[str, int]], under: Mapping[str, str]
) -> tuple[ErrorCode, str]:
    """The error code and suggestion for paths ``getinfo`` answered with a code (#620): every
    path refused for permission is ``permission_denied``, every missing one ``not_found``;
    a mix gets each kind's own advice (not the shared "retry with paths that exist", which a
    path refused for permission can't follow: QA round 3 on #606). A per-path code other
    than 408, 105 or 407 gets one fixed message under ``filestation_error``: the generic
    mapping of such a code (a session code's "re-authentication will be attempted", 599's
    "background task") doesn't fit a path's entry (QA round 1 on #623). A path in ``under``
    (`files_above`: under a file) is missing, whatever its code (#702)."""
    missing = [(path, 408 if path in under else code) for path, code in missing]
    codes = [code for _, code in missing]
    perm = error_from_code(407, "SYNO.FileStation.List").suggestion or ""
    if codes and all(code in _NO_PERMISSION for code in codes):
        return ErrorCode.PERMISSION_DENIED, perm
    if all(code == 408 for code in codes):
        return ErrorCode.NOT_FOUND, missing_paths_suggestion(missing)
    gone = [m for m in missing if m[1] == 408]
    parts = []
    if gone:
        parts.append("Use list_files or search_files to find the correct path.")
        wildcard = _wildcard_hint(gone)
        if wildcard:
            parts.append(wildcard)
        if any(holds_escape(path) for path, _ in gone):
            parts.append(ESCAPED_NAME_ADVICE)
    if any(code in _NO_PERMISSION for code in codes):
        parts.append(f"For one with no permission: {perm}")
    if any(code != 408 and code not in _NO_PERMISSION for code in codes):
        parts.append(
            _OTHER_CODE
            if len(parts) == 0
            else f"For one DSM gave another code for: {_CHECK_ACCESS}"
        )
    if gone:
        return ErrorCode.NOT_FOUND, " ".join(parts)
    if any(code in _NO_PERMISSION for code in codes):
        return ErrorCode.PERMISSION_DENIED, " ".join(parts)
    return ErrorCode.FILESTATION_ERROR, " ".join(parts)


def _wildcard_hint(missing: list[tuple[str, int]]) -> str:
    """The advice for a missing path that holds a wildcard character, or ""."""
    if any(ch in path for path, _ in missing for ch in _WILDCARD_CHARS):
        return (
            "Paths are literal, not wildcard patterns: use search_files (or list_files with a "
            "pattern) to find the files, then pass their full paths."
        )
    return ""


def missing_paths_suggestion(missing: list[tuple[str, int]]) -> str:
    """Suggestion for a call refused because paths don't exist (adds a wildcard hint, and one for
    a name copied from a reply that showed it escaped)."""
    hint = _wildcard_hint(missing)
    wildcard = f" {hint}" if hint else ""
    if missing and all(holds_escape(path) for path, _ in missing):
        # Listing again would show the same escape: lead with what ends the loop (pre-QA r2).
        return ESCAPED_NAME_ADVICE + wildcard
    suggestion = (
        "Check the paths with list_files or search_files, then retry with paths that exist."
        + wildcard
    )
    if any(holds_escape(path) for path, _ in missing):
        # A listing shows the same escape again, so "check with list_files" alone loops (#604).
        suggestion += f" {ESCAPED_NAME_ADVICE}"
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


def _with_listed_share_name(path: str, known_shares: set[str]) -> str:
    """Return ``path`` with its share named as ``list_share`` names it (#353).

    DSM takes an ordinary share in any case (``/TESTSHARE`` for ``testshare``), but not the
    user home service: ``/Home`` and ``/HOME`` get 408 where ``/home`` lists (vdsm DSM 7.1
    and 7.4.1; seska's DSM 7.1.1, admin and not). So the listed spelling is always sent.
    """
    parts = normalize_path(path).split("/")
    if len(parts) < 2 or not parts[1] or parts[1] in known_shares:
        return path
    listed = next((s for s in known_shares if s.casefold() == parts[1].casefold()), None)
    if listed is None:
        return path
    return "/".join(["", listed, *parts[2:]])


def _is_dot(part: str) -> bool:
    """``.`` or ``..``, padded with any whitespace: whether DSM trims padding beside a dot
    is unprobed, so every padded form is refused (#449, #510)."""
    return part.strip() in (".", "..")


def refuse_dot_parts(
    path: str,
    param: str,
    suggestion: str = "Write the path out in full from the shared folder, e.g. /video/Movies.",
) -> None:
    """Raise ``invalid_parameter`` for a NAS path with a ``.`` or ``..`` part (#449).

    DSM refuses them itself (File Station answers 418 "Illegal name or path"; CreateFolder
    1100, on DSM 7.1 through 7.4.1), but only after the share check, which reads just
    the first part, so ``/video/../photo`` passed as a ``video`` path and anything keyed
    on the share (the recycle-bin note after a delete, a hidden ``#recycle``) used the
    wrong one.
    """
    if any(_is_dot(part) for part in normalize_path(path).split("/") if part):
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"Invalid path {path!r}: '.' and '..' parts aren't allowed.",
            retryable=False,
            param=param,
            value=path,
            suggestion=suggestion,
        )


async def ensure_known_shares(
    client: DsmClient,
    paths: list[str],
    known_shares: set[str],
    params: str | list[str],
    suggestion: str | None = None,
) -> list[str]:
    """Raise ``not_found`` for a path whose first component isn't a shared folder (#160),
    and ``invalid_parameter`` for one with a ``.`` or ``..`` part (#449).

    ``params`` is the tool argument each path came from (one name for all, or one per
    path), so an error names the argument the caller passed. ``suggestion`` replaces both
    errors' suggestions (a path from a setting is fixed in the config, not the call).

    Returns ``paths`` in order, each with its share named as DSM lists it (#353): DSM
    takes an ordinary share in any case, but the home service only as ``home``.

    ``known_shares`` is a cache owned by the caller (cleared on re-auth). It's filled on
    first use and reloaded once when a path names a share it doesn't hold, so a share
    created since is still found. For an account that isn't an admin, the list is the
    shares it can see, which is what it should be told about. If DSM won't list shares,
    the check is skipped, ``paths`` come back unchanged and DSM's own error stands.
    """
    if not paths:
        return []
    names = [params] * len(paths) if isinstance(params, str) else params
    for path, param in zip(paths, names, strict=True):
        if suggestion:
            refuse_dot_parts(path, param, suggestion)
        else:
            refuse_dot_parts(path, param)

    def _shares() -> set[str]:
        return {
            normalize_path(p).split("/")[1].casefold() for p in paths if len(normalize_path(p)) > 1
        }

    known = {s.casefold() for s in known_shares}
    if (not known_shares or not _shares() <= known) and not await _refresh_known_shares(
        client, known_shares
    ):
        return list(paths)
    for path, param in zip(paths, names, strict=True):
        problem = validate_share_path(path, known_shares)
        if problem:
            error_response(
                ErrorCode.NOT_FOUND,
                problem,
                retryable=False,
                param=param,
                value=path,
                suggestion=suggestion or "Call list_shares to see the shared folders on this NAS.",
            )
    return [_with_listed_share_name(path, known_shares) for path in paths]
