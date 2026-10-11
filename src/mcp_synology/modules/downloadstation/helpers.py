"""Shared helpers for Download Station tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp_synology.core.errors import SynologyError
from mcp_synology.core.formatting import format_size

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

# DSM Download Station task statuses. The v1, v2 and v3 Task APIs all return
# these as strings (official DS Web API guide, Appendix A; verified live on
# DSM 7.2.2 / DS 4.1-5012 in #123). Only the separate SYNO.DownloadStation2.Task
# API returns integers, and nothing in this module reads status from it.
TASK_STATUSES: frozenset[str] = frozenset(
    {
        "waiting",
        "downloading",
        "paused",
        "finishing",
        "finished",
        "hash_checking",
        "seeding",
        "filehosting_waiting",
        "extracting",
        "error",
    }
)

# Operator-facing groupings used by list_downloads(status_filter=...).
# "downloading" is interpreted as "in-flight or waiting for a slot, but not
# paused / finished / error" — so transient pre-active states (waiting,
# filehosting_waiting) are bucketed there alongside actively-transferring
# states.
STATUS_GROUPS: dict[str, set[str]] = {
    "downloading": {
        "waiting",
        "downloading",
        "finishing",
        "hash_checking",
        "filehosting_waiting",
        "extracting",
    },
    "finished": {"finished", "seeding"},
    "paused": {"paused"},
    "error": {"error"},
}


def format_task_status(status: object) -> str:
    """Render a DSM task status.

    Known status strings pass through. Anything else (an unrecognized string,
    or an int from an API shape this module doesn't consume) renders as
    ``unknown(<raw>)`` so the original value is preserved in diagnostic
    output; None renders as plain ``unknown``.
    """
    if status is None:
        return "unknown"
    if isinstance(status, str) and status in TASK_STATUSES:
        return status
    return f"unknown({status})"


def format_transfer_progress(downloaded: int, total: int) -> str:
    """Render ``<downloaded> / <total> (<percent>%)``.

    When ``total == 0`` percent is shown as an em dash. When DSM reports
    ``downloaded > total`` (occasionally happens with seed-after-finish),
    percent is clamped to 100.
    """
    down_str = format_size(downloaded)
    total_str = format_size(total)
    if total <= 0:
        return f"{down_str} / {total_str} (—)"
    pct = min(100, int(downloaded * 100 / total))
    return f"{down_str} / {total_str} ({pct}%)"


def format_speed(bytes_per_sec: int) -> str:
    """Render a B/s rate; non-positive values display as an em dash."""
    if bytes_per_sec <= 0:
        return "—"
    return f"{format_size(bytes_per_sec)}/s"


def format_eta(downloaded: int, total: int, speed: int) -> str:
    """Render an estimated time-to-completion string.

    Returns an em dash when the ETA is undefined (zero/negative speed, or
    downloaded already at-or-past total — including DSM's occasional
    seed-after-finish overshoot). Otherwise renders one of:

    - ``Ns`` for under one minute
    - ``Nm`` for under one hour
    - ``NhMm`` for under one day
    - ``NdMh`` for one day or more
    """
    if speed <= 0 or total <= downloaded:
        return "—"
    remaining = total - downloaded
    seconds = remaining // speed
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


_DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# Weekly plan format, verified from DSM's own SYNO.ux.ScheduleTable widget
# (#123): day-major Sun..Sat, 24 chars per day, one char per hour.
SCHEDULE_PLAN_LENGTH = 7 * 24
_VALID_PLAN_CHARS = frozenset("012")

_CELL_GLYPHS: dict[str, str] = {
    "0": ".",  # no download
    "1": "#",  # full speed
    "2": "~",  # limited (alt speed)
}


def schedule_plan_problem(plan: str) -> str | None:
    """Describe why ``plan`` is not a valid weekly plan, or None if it is.

    DS2 ``Settings.Scheduler.set`` stores whatever it is given (no server-side
    validation), so this client-side check is the only guard on writes.
    """
    if len(plan) != SCHEDULE_PLAN_LENGTH:
        return (
            f"schedule_plan must be {SCHEDULE_PLAN_LENGTH} chars "
            f"(7 days × 24 hours), got {len(plan)}"
        )
    bad = sorted(set(plan) - _VALID_PLAN_CHARS)
    if bad:
        shown = ", ".join(repr(ch) for ch in bad)
        return (
            f"schedule_plan may only contain '0' (no download), '1' (full speed) "
            f"or '2' (limited); found {shown}"
        )
    return None


def format_schedule_grid(plan: str) -> str:
    """Render a Download Station weekly plan as a 7 × 24 text grid.

    ``plan`` is day-major Sun..Sat, 24 chars per day: '0' = no download,
    '1' = full speed, '2' = limited (alt speed). A malformed stored plan is
    rendered rather than rejected — missing or unrecognized cells show as
    '?' and a note names the problem — because other clients can store
    anything and a read must never fail on it.
    """
    hour_header = "     " + " ".join(f"{h:02d}" for h in range(24))
    lines = [hour_header]
    for day_idx, name in enumerate(_DAY_NAMES):
        day_slice = plan[day_idx * 24 : (day_idx + 1) * 24].ljust(24, "?")
        cells = " ".join(_CELL_GLYPHS.get(ch, "?") for ch in day_slice)
        lines.append(f"{name}  {cells}")

    lines.append("")
    lines.append("Legend: # = full speed   ~ = limited (alt speed)   . = no download")
    if len(plan) != SCHEDULE_PLAN_LENGTH:
        lines.append(
            f"Note: stored plan is malformed ({len(plan)} chars, expected "
            f"{SCHEDULE_PLAN_LENGTH}); '?' marks unreadable hours."
        )
    elif set(plan) - _VALID_PLAN_CHARS:
        lines.append(
            "Note: stored plan is malformed (contains values other than 0/1/2); "
            "'?' marks unreadable hours."
        )
    return "\n".join(lines)


async def ds_is_manager(client: DsmClient) -> bool | None:
    """Whether the session's user is a Download Station manager.

    From ``SYNO.DownloadStation.Info.getinfo``'s ``is_manager``. Non-managers
    get a sanitized per-user view of the global config, only see their own
    tasks, and have global config writes silently ignored (#123). Returns
    None when it can't be determined (API absent, call failed), so callers
    fall back to their pre-#123 behavior rather than failing.
    """
    if "SYNO.DownloadStation.Info" not in client.api_cache:
        return None
    try:
        data = await client.request("SYNO.DownloadStation.Info", "getinfo", version=1)
    except SynologyError:
        return None
    value = data.get("is_manager")
    return value if isinstance(value, bool) else None
