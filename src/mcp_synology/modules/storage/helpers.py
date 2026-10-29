"""Storage module helpers: DSM calls, ID resolution, shared formatting.

Every API here is administrator-only and JSON-format (docs/specs/storage-module-spec.md):
lists go JSON-encoded, free text through ``dsm_str()``, and a 105 is the account, never
the session.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mcp_synology.core.errors import DsmHttpError, DsmReplyError, ErrorCode, SynologyError
from mcp_synology.core.formatting import (
    error_response,
    format_size,
    format_timestamp,
    synology_error_response,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)

STORAGE_API = "SYNO.Storage.CGI.Storage"

ADMIN_SUGGESTION = (
    "Storage Manager's APIs are for DSM administrators only. Configure this connection "
    "with an administrator account to use the storage tools."
)

# 121: the NAS doesn't have this feature (deduplication on a DS1618+, for example).
NOT_SUPPORTED = 121
# 103: the method doesn't exist on this DSM (get_latest_online_drive_db_info on 7.1).
METHOD_MISSING = 103

# UI-only fields: left out of list tools, shown raw by the get_* tools (spec, "What's
# shown").
UI_FIELDS = frozenset(
    {"i18nNamingInfo", "can_do", "vspace_can_do", "suggestions", "action", "hide_info"}
)


def is_ui_field(key: str) -> bool:
    return key in UI_FIELDS or key.startswith("show_") or "_btn" in key


def json_list(values: list[str]) -> str:
    """A list as DSM's JSON-format APIs need it: one JSON string, not repeated params."""
    return json.dumps(values)


def refuse_non_admin(operation: str) -> None:
    error_response(
        ErrorCode.PERMISSION_DENIED,
        f"{operation} failed: Storage Manager needs a DSM administrator account.",
        retryable=False,
        suggestion=ADMIN_SUGGESTION,
    )


async def call(
    client: DsmClient,
    operation: str,
    api: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    version: int = 1,
) -> Any:
    """A call the tool can't do without: any DSM error fails the tool.

    Every call is pinned to v1, the version the captures verified, unless the caller
    names another (the client would otherwise send the NAS's highest).

    105 is ``permission_denied`` naming the administrator requirement; 121 is
    ``not_supported``.
    """
    if api not in client._api_cache:
        error_response(
            ErrorCode.API_NOT_FOUND,
            f"{operation} failed: {api} isn't available on this NAS.",
            retryable=False,
            suggestion=(
                "Storage Manager may not be installed or running on this DSM."
                if api.startswith("SYNO.Storage.")
                else f"{api} isn't offered by this DSM."
            ),
        )
    try:
        return await client.request(api, method, version=version, params=params)
    except SynologyError as e:
        if e.code == 105:
            refuse_non_admin(operation)
        if e.code == NOT_SUPPORTED:
            error_response(
                ErrorCode.NOT_SUPPORTED,
                f"{operation} failed: this NAS doesn't support it (DSM error 121).",
                retryable=False,
            )
        synology_error_response(operation, e)


@dataclass
class Sections:
    """Results of optional calls, with a note for each one that gave nothing."""

    operation: str
    notes: list[str] = field(default_factory=list)

    async def get(
        self,
        client: DsmClient,
        label: str,
        api: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        version: int = 1,
        refusals: frozenset[int] = frozenset({105}),
    ) -> Any:
        """The reply's data, or None (with a note) when the section can't be read.

        A refusal (105; ``USB list`` also answers 404 to a non-admin) still fails the
        whole tool: the account isn't an administrator.
        """
        if api not in client._api_cache:
            self.notes.append(f"{label}: not available on this NAS")
            return None
        try:
            return await client.request(api, method, version=version, params=params)
        except SynologyError as e:
            if e.code in refusals:
                refuse_non_admin(self.operation)
            if e.code == NOT_SUPPORTED:
                self.notes.append(f"{label}: not available on this NAS")
            elif e.code == METHOD_MISSING:
                self.notes.append(f"{label}: not available on this DSM version")
            else:
                logger.debug("%s %s/%s failed: %s", self.operation, api, method, e)
                self.notes.append(f"{label}: couldn't be read ({failure(e)})")
            return None


def failure(e: SynologyError) -> str:
    """'DSM error 117', or what came back instead of a DSM reply (a proxy's 502)."""
    if e.code is not None:
        return f"DSM error {e.code}"
    if isinstance(e, DsmHttpError):
        return f"HTTP {e.status}"
    if isinstance(e, DsmReplyError):
        return "no DSM reply"
    return type(e).__name__


def as_dict(value: object) -> dict[str, Any]:
    """A dict DSM sent, or {} for anything else."""
    return value if isinstance(value, dict) else {}


def dicts(value: object) -> list[dict[str, Any]]:
    """The dict items of a list DSM sent (anything else, or a non-list, is skipped)."""
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


async def load_info(client: DsmClient, operation: str) -> dict[str, Any]:
    """Storage Manager's ``load_info``: disks, pools, volumes, caches, env."""
    data = await call(client, operation, STORAGE_API, "load_info")
    return data if isinstance(data, dict) else {}


def items(info: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """A list from ``load_info``; ``ssdCaches`` and ``sharedCaches`` are absent without a
    cache, not empty."""
    return dicts(info.get(key))


def check_range(operation: str, param: str, value: int, low: int, high: int | None) -> None:
    """Refuse an out-of-range paging value before any DSM call, as Photos does."""
    if value < low or (high is not None and value > high):
        accepted = f"{low}–{high:,}" if high is not None else f"{low} or more"
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"{operation} failed: {param} must be {accepted}.",
            retryable=False,
            param=param,
            value=value,
        )


def not_found(
    operation: str,
    kind: str,
    param: str,
    value: str,
    valid: list[str],
    labels: list[str] | None = None,
) -> None:
    """``valid`` holds values the tool takes back; ``labels`` (if given) shows them in the
    message with more context, such as a disk's bay name."""
    shown = ", ".join(labels or valid) if valid else "none"
    error_response(
        ErrorCode.NOT_FOUND,
        f"{operation} failed: no {kind} {value!r} on this NAS. Valid: {shown}.",
        retryable=False,
        param=param,
        value=value,
        valid=valid,
        suggestion="Use one of the valid values, or list them again.",
    )


# --- formatting --------------------------------------------------------------------


def size(value: object) -> str:
    """DSM's byte counts come as strings ("41968612671488"); a missing one is "—"."""
    if value is None or value == "":
        return "—"
    try:
        return format_size(int(str(value)))
    except (TypeError, ValueError):
        return str(value)


def to_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def percent(used: object, total: object) -> str:
    u, t = to_int(used), to_int(total)
    if u is None or not t:
        return "—"
    return f"{u / t * 100:.1f}%"


def pct(value: object) -> str:
    """A percentage DSM sends ('83'), or — when it's missing."""
    return "—" if value is None or value == "" else f"{value}%"


def usage(sizes: dict[str, Any]) -> str:
    """'35.4 TiB of 36.6 TiB used (96.5%)'."""
    total, used = sizes.get("total"), sizes.get("used")
    if total is None:
        return "—"
    if used is None:
        return size(total)
    return f"{size(used)} of {size(total)} used ({percent(used, total)})"


def status_text(obj: dict[str, Any]) -> str:
    """An object's status, with what DSM says about it ('attention, fs_almost_full')."""
    status = str(obj.get("summary_status") or obj.get("status") or "unknown")
    detail = (
        (obj.get("space_status") or {}).get("detail")
        if isinstance(obj.get("space_status"), dict)
        else None
    )
    return f"{status}, {detail}" if detail and detail != status else status


def raid_name(device_type: object) -> str:
    """'raid_5' → 'RAID 5', 'shr_without_1' → 'SHR', 'basic' → 'Basic'."""
    text = str(device_type or "")
    if text.startswith("raid_"):
        return "RAID " + text[5:].upper()
    if text.startswith("shr"):
        return "SHR-2" if "2" in text else "SHR"
    return {"basic": "Basic", "jbod": "JBOD"}.get(text, text or "—")


def pool_label(pool: dict[str, Any]) -> str:
    return f"Pool {pool.get('num_id', '?')} ({pool.get('id', '?')})"


def volume_label(volume: dict[str, Any]) -> str:
    return f"Volume {volume.get('num_id', '?')} ({volume.get('vol_path') or volume.get('id')})"


def disk_label(disk: dict[str, Any]) -> str:
    return f"{disk.get('longName') or disk.get('name') or 'Disk'} ({disk.get('id', '?')})"


def shared_cache_of(info: dict[str, Any], cache: dict[str, Any]) -> dict[str, Any] | None:
    """The shared SSD cache a volume's cache (``ssdCaches``) is carved from, if any."""
    path = str(cache.get("SSDPath") or "")
    for shared in items(info, "sharedCaches"):
        if path.startswith(str(shared.get("space_path")) + "/"):
            return shared
    return None


def cache_title(info: dict[str, Any], cache: dict[str, Any]) -> str:
    """One name for one cache everywhere: 'Cache 1 (shared_cache_1, volume cache …)'."""
    shared = shared_cache_of(info, cache)
    if shared:
        return (
            f"Cache {shared.get('num_id', '?')} ({shared.get('id')}, volume cache "
            f"{cache.get('id')})"
        )
    return f"Cache {cache.get('num_id', '?')} ({cache.get('id')})"


def yes_no(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value in (0, 1, "0", "1"):
        return "yes" if str(value) == "1" else "no"
    return str(value)


def value_text(value: object) -> str:
    """Any other field's value, shown as DSM sent it."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(", ", ": "))
    if value == "" or value is None:
        return "—"
    return str(value)


# Fields DSM sends as epoch seconds (0 = never or not scheduled).
EPOCH_FIELDS = frozenset({
    "last_done_time", "next_schedule_time", "next_start_time", "start_time",
    "completed_time", "db_last_update_time", "delay_end_time", "schedule_disable_time",
    "last_dry_run_time", "last_manual_dedupe_time", "next_run_time", "last_run_time",
})  # fmt: skip


def when(epoch: object) -> str:
    """An epoch DSM sends with no local-time counterpart, in UTC (see EPOCH_NOTE)."""
    if isinstance(epoch, (int, float)) and not isinstance(epoch, bool) and epoch > 0:
        return f"about {format_timestamp(epoch)}"
    return "—"


def field_text(key: str, value: object) -> str:
    """A field's value, with epoch fields as times, also one level down (``dedup_info``)."""
    if key in EPOCH_FIELDS:
        return when(value)
    if isinstance(value, dict) and EPOCH_FIELDS.intersection(value):
        value = {k: when(v) if k in EPOCH_FIELDS else v for k, v in value.items()}
    return value_text(value)


def other_fields(
    obj: dict[str, Any], known: set[str], *, keep_ui: bool = False, prefix: str = ""
) -> list[tuple[str, str]]:
    """Every field not already shown, under its own name, so nothing DSM sends is lost."""
    return [
        (f"{prefix}{key}", field_text(key, value))
        for key, value in obj.items()
        if key not in known and (keep_ui or not is_ui_field(key))
    ]


def epoch_note(*texts: str) -> str:
    """EPOCH_NOTE, when any of ``texts`` shows an "about" time."""
    return "\n\n" + EPOCH_NOTE if any("about " in t for t in texts) else ""


def notes_block(notes: list[str]) -> str:
    return "\n".join(["", "Not shown:", *(f"  - {n}" for n in notes)]) if notes else ""


# Spec open item 7: DSM's epoch times looked an hour off on a NAS on daylight saving time.
EPOCH_NOTE = (
    'Times marked "about" come from DSM as UTC instants, which can be an hour off while '
    "the NAS is on daylight saving time."
)
