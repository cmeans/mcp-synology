"""Shared helpers for Download Station tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp_synology.core.errors import ErrorCode, SynologyError
from mcp_synology.core.formatting import (
    SCHEDULE_PLAN_LENGTH,
    error_response,
    format_size,
    weekly_plan_grid,
)
from mcp_synology.modules.filestation.paths import check_parts

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


def format_task_status(status: object, error_detail: object = None) -> str:
    """Render a DSM task status.

    Known status strings pass through. Anything else (an unrecognized string,
    or an int from an API shape this module doesn't consume) renders as
    ``unknown(<raw>)`` so the original value is preserved in diagnostic
    output; None renders as plain ``unknown``. ``error_detail`` is DSM's
    ``status_extra.error_detail`` (e.g. ``broken_link``), appended when set.
    """
    if status is None:
        text = "unknown"
    elif isinstance(status, str) and status in TASK_STATUSES:
        text = status
    else:
        text = f"unknown({status})"
    return f"{text} ({error_detail})" if error_detail else text


def task_error_detail(task: dict[str, Any]) -> object:
    """DSM's reason for an ``error`` task (``status_extra.error_detail``), or None."""
    extra = task.get("status_extra")
    return extra.get("error_detail") if isinstance(extra, dict) else None


# Every tool argument that names a NAS folder. Each goes through `normalize_destination`
# (#510); a new one goes here, and tests/modules/downloadstation/test_paths.py fails until
# it does.
DESTINATION_PARAMS: dict[str, str] = {
    "create_download": "destination",
    "edit_download": "destination",
    "set_download_config": "default_destination",
}

# DSM's answer, per call, to a destination with a space anywhere in it (a padded name, or
# `a b`), even one that exists: `Task.edit` 100 and `Info.setserverconfig` 401, on DSM 7.1,
# 7.2.2, 7.3.2 and 7.4.1 (vdsm, #510). JSON-quoting the value makes it refuse every
# destination. `Task.create` takes the same destinations.
SPACE_ERRORS = {"edit": 100, "config": 401}
SPACE_ADVICE = {
    100: "Delete the task and create it again with create_download's destination, which "
    "takes spaces, or choose a folder without a space in its path.",
    401: "Pass the folder as destination on each create_download, which takes spaces, or "
    "choose a folder without a space in its path.",
}
_DESTINATION_ADVICE = "Write the path out in full from the shared folder, e.g. 'video/Torrents'."


def normalize_destination(
    destination: str,
    *,
    operation: str,
    param: str = "destination",
    space_error: int | None = None,
) -> str:
    """Make a destination share-relative, as Download Station requires.

    File Station shows paths as ``/share/...``; DS rejects that form (DSM 403
    "Destination doesn't exist" on create, error 100 on edit), so the ``/`` at either end is
    dropped. Otherwise the path is taken exactly, by File Station's part rules (#510):
    `check_parts` drops only whitespace before a leading ``/`` and refuses a NUL (DSM cuts
    the path there: a task created or edited into ``x/foo<NUL>y`` went to ``x/foo``), an
    empty part (``//``) and a ``.`` or ``..`` part, padded too (#449). DS reaches a folder
    whose name starts or ends with whitespace exactly (``x/foo `` and ``x/ foo`` are
    folders beside ``x/foo``; vdsm DSM 7.1, 7.2.2, 7.3.2 and 7.4.1), so stripping sent a
    task to another folder. ``space_error`` is the DSM error code of a call that can't take
    a destination with a space in it (`SPACE_ERRORS`): such a destination is refused up
    front, since DSM's own answer reads as a missing folder. ``param`` names the caller's
    argument in errors.
    """
    relative = check_parts(destination, param, _DESTINATION_ADVICE)[1:]
    if not relative:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"{operation} failed: {param} {destination!r} names no shared folder.",
            retryable=False,
            param=param,
            value=destination,
            suggestion="Pass a share-relative path such as 'video/Torrents'.",
        )
    if space_error is not None and " " in relative:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"{operation} failed: {param} {destination!r} has a space in it, which this "
            f"Download Station call can't take: DSM answers error {space_error} even when the "
            "folder exists.",
            retryable=False,
            param=param,
            value=destination,
            suggestion=SPACE_ADVICE[space_error],
        )
    return relative


def task_size_unknown(total: int, downloaded: int, status: object) -> bool:
    """Whether a task's size of 0 means DSM doesn't know the size yet (#345).

    DSM reports 0 while the size is unknown, e.g. an HTTP download whose server sends
    no size (seen on DSM 7.1.1 at 70.1 MiB downloaded). Only a finished (or seeding)
    task with nothing downloaded is taken to be a real 0-byte download.
    """
    if total > 0:
        return False
    return downloaded > 0 or status not in STATUS_GROUPS["finished"]


def format_task_size(total: int, downloaded: int, status: object) -> str:
    """Render a task's size, or ``unknown`` when DSM's 0 means it isn't known (#345)."""
    return "unknown" if task_size_unknown(total, downloaded, status) else format_size(total)


def format_transfer_progress(downloaded: int, total: int, *, size_unknown: bool = False) -> str:
    """Render ``<downloaded> / <total> (<percent>%)``.

    When ``total == 0`` percent is shown as an em dash. When DSM reports
    ``downloaded > total`` (occasionally happens with seed-after-finish),
    percent is clamped to 100. With ``size_unknown`` (see ``task_size_unknown``)
    it renders ``<downloaded> downloaded (size unknown)`` instead.
    """
    down_str = format_size(downloaded)
    if size_unknown:
        return f"{down_str} downloaded (size unknown)"
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


# Weekly plan format, verified from DSM's own SYNO.ux.ScheduleTable widget
# (#123): day-major Sun..Sat, 24 chars per day, one char per hour.
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

    '0' = no download, '1' = full speed, '2' = limited (alt speed). A malformed
    stored plan is rendered rather than rejected (see ``core.formatting``),
    because other clients can store anything and a read must never fail on it.
    """
    return weekly_plan_grid(
        plan,
        _CELL_GLYPHS,
        "Legend: # = full speed   ~ = limited (alt speed)   . = no download",
    )


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
