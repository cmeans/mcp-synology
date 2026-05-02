"""System info tool: get_system_info."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mcp_synology.core.errors import ErrorCode, SynologyError
from mcp_synology.core.formatting import error_response, format_key_value

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)


def _format_uptime(seconds: int) -> str:
    """Format uptime seconds as human-readable string."""
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "< 1m"


async def _fetch_dsm_info(client: DsmClient) -> dict[str, Any]:
    """Fetch basic system info via SYNO.DSM.Info (works for all users)."""
    try:
        return await client.request("SYNO.DSM.Info", "getinfo")
    except SynologyError as e:
        logger.debug("SYNO.DSM.Info failed: %s", e)
        return {}


async def _fetch_core_system_info(client: DsmClient) -> dict[str, Any]:
    """Fetch extended system info via SYNO.Core.System (requires admin)."""
    if "SYNO.Core.System" not in client._api_cache:
        return {}
    try:
        return await client.request("SYNO.Core.System", "info", version=1)
    except SynologyError as e:
        logger.debug("SYNO.Core.System/info failed (may require admin): %s", e)
        return {}


async def get_system_info(client: DsmClient) -> str:
    """Get NAS hardware and software information.

    Uses SYNO.DSM.Info (works for all users) as the primary source,
    supplemented by SYNO.Core.System (admin only) for additional details
    like CPU specs and NTP configuration.
    """
    dsm = await _fetch_dsm_info(client)
    core = await _fetch_core_system_info(client)

    if not dsm and not core:
        error_response(
            ErrorCode.UNAVAILABLE,
            "System info failed: No system information available.",
            retryable=True,
            suggestion="Check that the user has permission to query system info.",
        )

    pairs: list[tuple[str, str]] = []

    # Model (from either source)
    model = dsm.get("model") or core.get("model", "")
    if model:
        pairs.append(("Model", model))

    # Firmware / DSM version
    firmware = dsm.get("version_string") or core.get("firmware_ver", "")
    if firmware:
        pairs.append(("Firmware", firmware))

    # CPU info (Core.System only)
    cpu_series = core.get("cpu_series", "")
    cpu_cores = core.get("cpu_cores", "")
    cpu_clock = core.get("cpu_clock_speed", 0)
    if cpu_series:
        cpu_str = cpu_series
        if cpu_cores:
            cpu_str += f" ({cpu_cores} cores)"
        if cpu_clock:
            cpu_str += f" @ {cpu_clock} MHz"
        pairs.append(("CPU", cpu_str))

    # RAM
    # Use explicit None-check so a (theoretical) 0 value isn't misread as missing.
    ram = dsm["ram"] if dsm.get("ram") is not None else core.get("ram_size")
    if ram:
        pairs.append(("RAM", f"{ram} MB"))

    # Temperature — 0°C is technically a valid reading (cold-room install),
    # so distinguish missing from zero with explicit None-checks.
    temp = dsm["temperature"] if dsm.get("temperature") is not None else core.get("sys_temp")
    temp_warn_dsm = dsm.get("temperature_warn")
    temp_warn = temp_warn_dsm if temp_warn_dsm is not None else core.get("temperature_warn", False)
    if temp is not None:
        temp_str = f"{temp}°C"
        if temp_warn:
            temp_str += " ⚠ WARNING"
        pairs.append(("Temperature", temp_str))

    # Uptime
    uptime = dsm["uptime"] if dsm.get("uptime") is not None else core.get("up_time")
    if uptime:
        pairs.append(("Uptime", _format_uptime(uptime)))

    # System time
    sys_time = dsm.get("time") or core.get("time", "")
    if sys_time:
        pairs.append(("System time", sys_time))

    # NTP (Core.System only)
    ntp_enabled = core.get("enabled_ntp", False)
    ntp_server = core.get("ntp_server", "")
    if ntp_enabled and ntp_server:
        pairs.append(("NTP", ntp_server))

    if not pairs:
        error_response(
            ErrorCode.UNAVAILABLE,
            "System info failed: No system information returned.",
            retryable=True,
        )

    return format_key_value(pairs, title="System Info")
