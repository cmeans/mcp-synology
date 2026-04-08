"""System info tool: get_system_info."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mcp_synology.core.errors import SynologyError
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
            "unavailable",
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
    ram = dsm.get("ram") or core.get("ram_size", 0)
    if ram:
        pairs.append(("RAM", f"{ram} MB"))

    # Temperature
    temp = dsm.get("temperature") or core.get("sys_temp", 0)
    temp_warn = dsm.get("temperature_warn", False) or core.get("temperature_warn", False)
    if temp:
        temp_str = f"{temp}°C"
        if temp_warn:
            temp_str += " ⚠ WARNING"
        pairs.append(("Temperature", temp_str))

    # Uptime
    uptime = dsm.get("uptime") or core.get("up_time", 0)
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
            "unavailable",
            "System info failed: No system information returned.",
            retryable=True,
        )

    return format_key_value(pairs, title="System Info")
