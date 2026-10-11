"""Download Station config tools: global config and the weekly schedule.

API surface verified live on DSM 7.2.2 / DS 4.1-5012 (#123):

- Global config: ``SYNO.DownloadStation.Info`` v1 ``getconfig`` /
  ``setserverconfig`` (there is no ``setconfig``). Non-managers get a
  sanitized per-user view from ``getconfig``, and DSM answers success to
  their ``setserverconfig`` while ignoring it.
- Schedule flags: ``SYNO.DownloadStation.Schedule`` v1 ``getconfig`` /
  ``setconfig`` carry only ``enabled`` / ``emule_enabled`` and work for any
  DS-enabled user.
- Weekly plan + limited-speed rates: ``SYNO.DownloadStation2.Settings.Scheduler``
  v1 ``get`` / ``set`` (admin only; JSON-format API, so string values are
  JSON-encoded; no server-side validation). Its ``enable_schedule`` is the
  same flag as v1 ``enabled``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from mcp_synology.core.errors import ErrorCode, SynologyError, SynologyPermissionError
from mcp_synology.core.formatting import (
    error_response,
    format_key_value,
    synology_error_response,
)
from mcp_synology.modules.downloadstation.helpers import ds_is_manager

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

_SCHEDULER_DS2 = "SYNO.DownloadStation2.Settings.Scheduler"


def _kbps_str(kbps: int) -> str:
    """Format a KB/s rate; 0 means unlimited per DSM convention."""
    if kbps <= 0:
        return "unlimited"
    return f"{kbps} KB/s"


def _bool_str(value: bool | None) -> str:
    if value is None:
        return "—"
    return "yes" if value else "no"


async def get_download_config(client: DsmClient) -> str:
    """Get Download Station global configuration.

    For a non-manager, DSM returns a sanitized per-user view (null default
    destination, zeroed rates), so the output is labeled as such rather than
    presented as the NAS-wide settings.
    """
    is_manager = await ds_is_manager(client)
    try:
        data = await client.request(
            "SYNO.DownloadStation.Info",
            "getconfig",
            version=1,
        )
    except SynologyError as e:
        synology_error_response("Get download config", e)

    pairs: list[tuple[str, str]] = [
        ("Default destination", str(data.get("default_destination") or "—")),
        ("BT max download", _kbps_str(int(data.get("bt_max_download", 0)))),
        ("BT max upload", _kbps_str(int(data.get("bt_max_upload", 0)))),
        ("HTTP max download", _kbps_str(int(data.get("http_max_download", 0)))),
        ("FTP max download", _kbps_str(int(data.get("ftp_max_download", 0)))),
        ("NZB max download", _kbps_str(int(data.get("nzb_max_download", 0)))),
        ("eMule enabled", _bool_str(data.get("emule_enabled"))),
        ("eMule max download", _kbps_str(int(data.get("emule_max_download", 0)))),
        ("eMule max upload", _kbps_str(int(data.get("emule_max_upload", 0)))),
        ("Auto-unzip enabled", _bool_str(data.get("unzip_service_enabled"))),
    ]

    title = "Download Station configuration"
    if is_manager is False:
        title += " (your user view — not the NAS-wide settings)"
    return format_key_value(pairs, title=title)


async def get_schedule(client: DsmClient) -> str:
    """Get the weekly DS schedule: enable flags, limited-speed rates, and a 7×24 grid.

    Flags come from v1 ``Schedule.getconfig``; the plan and rates from DS2
    ``Settings.Scheduler.get``. When DS2 is absent or refuses (105 for
    non-admins), the flags are still shown with a note that the plan isn't
    available.
    """
    from mcp_synology.modules.downloadstation.helpers import format_schedule_grid

    try:
        flags = await client.request(
            "SYNO.DownloadStation.Schedule",
            "getconfig",
            version=1,
        )
    except SynologyError as e:
        synology_error_response("Get download schedule", e)

    pairs: list[tuple[str, str]] = [
        ("Enabled", _bool_str(flags.get("enabled"))),
        ("eMule schedule enabled", _bool_str(flags.get("emule_enabled"))),
    ]

    plan_unavailable: str | None = None
    plan: dict[str, object] = {}
    if _SCHEDULER_DS2 not in client.api_cache:
        plan_unavailable = f"this NAS does not expose {_SCHEDULER_DS2}"
    else:
        try:
            plan = await client.request(_SCHEDULER_DS2, "get", version=1)
        except SynologyPermissionError:
            plan_unavailable = "requires a Download Station manager/admin account"
        except SynologyError as e:
            synology_error_response("Get download schedule", e)

    if plan_unavailable is not None:
        flags_block = format_key_value(pairs, title="Download Station schedule")
        return f"{flags_block}\n\nNote: weekly plan not available ({plan_unavailable})."

    pairs.append(("Limited download rate", _kbps_str(_as_int(plan.get("download_rate")))))
    pairs.append(("Limited upload rate", _kbps_str(_as_int(plan.get("upload_rate")))))
    flags_block = format_key_value(pairs, title="Download Station schedule")

    stored = plan.get("schedule", "")
    grid = format_schedule_grid(stored if isinstance(stored, str) else "")
    return f"{flags_block}\n\n{grid}"


def _as_int(value: object) -> int:
    return value if isinstance(value, int) else 0


async def set_download_config(
    client: DsmClient,
    *,
    bt_max_download: int | None = None,
    bt_max_upload: int | None = None,
    emule_max_download: int | None = None,
    emule_max_upload: int | None = None,
    default_destination: str | None = None,
) -> str:
    """Set DS global configuration. Only supplied fields are updated.

    Rates are KB/s; pass 0 to mean unlimited. Refused for non-managers:
    DSM answers success to their ``setserverconfig`` but ignores it.
    """
    fields: dict[str, str] = {}
    if bt_max_download is not None:
        fields["bt_max_download"] = str(bt_max_download)
    if bt_max_upload is not None:
        fields["bt_max_upload"] = str(bt_max_upload)
    if emule_max_download is not None:
        fields["emule_max_download"] = str(emule_max_download)
    if emule_max_upload is not None:
        fields["emule_max_upload"] = str(emule_max_upload)
    if default_destination is not None:
        fields["default_destination"] = default_destination

    if not fields:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            "Set download config failed: no fields supplied (nothing to change).",
            retryable=False,
            valid=[
                "bt_max_download",
                "bt_max_upload",
                "emule_max_download",
                "emule_max_upload",
                "default_destination",
            ],
        )

    if await ds_is_manager(client) is False:
        error_response(
            ErrorCode.PERMISSION_DENIED,
            "Set download config failed: requires a Download Station manager/admin "
            "account. DSM accepts this change from non-managers but silently ignores it, "
            "so nothing was sent.",
            retryable=False,
            suggestion="Use an account that is a Download Station manager (e.g. an admin).",
        )

    try:
        await client.request(
            "SYNO.DownloadStation.Info",
            "setserverconfig",
            version=1,
            params=fields,
        )
    except SynologyError as e:
        synology_error_response("Set download config", e)

    pairs: list[tuple[str, str]] = list(fields.items())
    return format_key_value(pairs, title=f"Set download config — {len(fields)} field(s) updated")


async def set_schedule(
    client: DsmClient,
    *,
    enabled: bool | None = None,
    emule_enabled: bool | None = None,
    schedule_plan: str | None = None,
    download_rate: int | None = None,
    upload_rate: int | None = None,
) -> str:
    """Set the DS schedule. Only supplied fields are updated.

    ``enabled`` / ``emule_enabled`` go to v1 ``Schedule.setconfig`` (works
    for any DS-enabled user; DSM lets them toggle the NAS-wide flag).
    ``schedule_plan`` (168 chars, day-major Sun..Sat, '0' no download /
    '1' full speed / '2' limited) and the limited-speed ``download_rate`` /
    ``upload_rate`` (KB/s, 0 = unlimited) go to DS2 ``Settings.Scheduler.set``
    (admin only). DS2 does no validation, so everything is validated here
    first. The DS2 write goes first: a refusal there means nothing was
    applied; if the later v1 write fails, the error names what was applied.
    """
    from mcp_synology.modules.downloadstation.helpers import schedule_plan_problem

    if schedule_plan is not None:
        problem = schedule_plan_problem(schedule_plan)
        if problem is not None:
            error_response(
                ErrorCode.INVALID_PARAMETER,
                f"Set schedule failed: {problem}.",
                retryable=False,
                param="schedule_plan",
                value=schedule_plan,
            )
    for name, rate in (("download_rate", download_rate), ("upload_rate", upload_rate)):
        if rate is not None and rate < 0:
            error_response(
                ErrorCode.INVALID_PARAMETER,
                f"Set schedule failed: {name} must be >= 0 KB/s (0 = unlimited), got {rate}.",
                retryable=False,
                param=name,
                value=rate,
            )

    ds2_fields: dict[str, str] = {}
    if schedule_plan is not None:
        ds2_fields["schedule"] = json.dumps(schedule_plan)
    if download_rate is not None:
        ds2_fields["download_rate"] = str(download_rate)
    if upload_rate is not None:
        ds2_fields["upload_rate"] = str(upload_rate)

    v1_fields: dict[str, str] = {}
    if enabled is not None:
        v1_fields["enabled"] = str(enabled).lower()
    if emule_enabled is not None:
        v1_fields["emule_enabled"] = str(emule_enabled).lower()

    if not ds2_fields and not v1_fields:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            "Set schedule failed: no fields supplied (nothing to change).",
            retryable=False,
            valid=["enabled", "emule_enabled", "schedule_plan", "download_rate", "upload_rate"],
        )

    # Tool-facing names of what each write applies, for partial-failure reports.
    ds2_names = [
        name
        for name, value in (
            ("schedule_plan", schedule_plan),
            ("download_rate", download_rate),
            ("upload_rate", upload_rate),
        )
        if value is not None
    ]
    v1_names = list(v1_fields)

    if ds2_fields:
        if _SCHEDULER_DS2 not in client.api_cache:
            error_response(
                ErrorCode.API_NOT_FOUND,
                f"Set schedule failed: the weekly plan and limited-speed rates need "
                f"{_SCHEDULER_DS2}, which this NAS does not expose; nothing was applied.",
                retryable=False,
            )
        try:
            await client.request(_SCHEDULER_DS2, "set", version=1, params=ds2_fields)
        except SynologyError as e:
            synology_error_response("Set download schedule (nothing was applied)", e)

    if v1_fields:
        try:
            await client.request(
                "SYNO.DownloadStation.Schedule",
                "setconfig",
                version=1,
                params=v1_fields,
            )
        except SynologyError as e:
            if ds2_names:
                synology_error_response(
                    f"Set download schedule partially failed. Applied: {', '.join(ds2_names)}. "
                    f"Not applied: {', '.join(v1_names)}",
                    e,
                )
            synology_error_response("Set download schedule", e)

    # Don't echo the full 168-char plan back; just note it was set.
    pairs: list[tuple[str, str]] = [(k, v) for k, v in v1_fields.items()]
    if schedule_plan is not None:
        pairs.append(("schedule_plan", f"<set to {len(schedule_plan)}-char plan>"))
    if download_rate is not None:
        pairs.append(("download_rate", _kbps_str(download_rate)))
    if upload_rate is not None:
        pairs.append(("upload_rate", _kbps_str(upload_rate)))
    return format_key_value(pairs, title=f"Set download schedule — {len(pairs)} field(s) updated")
