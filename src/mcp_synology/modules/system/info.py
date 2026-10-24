"""System info tool: get_system_info."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
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


# Every field the four sources send gets a line (maintainer, 2026-10-23: "We should be
# showing all information that's being provided. Even Serial #."). These are the ones
# shown under their own label; anything else DSM sends is listed under its field name.
_DSM_INFO_KEYS = {
    "codepage", "model", "ram", "serial", "temperature", "temperature_warn", "time",
    "uptime", "version", "version_string",
}  # fmt: skip
_CORE_KEYS = {
    "cpu_clock_speed", "cpu_cores", "cpu_family", "cpu_series", "cpu_vendor", "enabled_ntp",
    "external_pci_slot_info", "firmware_date", "firmware_ver", "model", "ntp_server",
    "ram_size", "rp1", "rp2", "sata_dev", "serial", "support_esata", "support_rp", "sys_temp",
    "sys_tempwarn", "systempwarn", "temperature_warning", "time", "time_zone",
    "time_zone_desc", "up_time", "usb_dev",
}  # fmt: skip
_NTP_KEYS = {
    "date", "enable_ntp", "hour", "minute", "now", "second", "server", "timestamp",
    "timezone",
}  # fmt: skip
_STATUS_KEYS = {"ext_nic_is_incompatible", "is_system_crashed", "upgrade_ready"}

_ADMIN_ONLY = (
    "CPU, NTP server, firmware date, update-ready, crash and network-card flags, "
    "redundant power, eSATA, and attached USB, SATA and PCIe devices"
)
# DSM's answers to an account that isn't an administrator: 1006 from Core.System, 105
# from Region.NTP and System.Status (vdsm DSM 7.1, 7.2.2, 7.3.2 and 7.4.1).
_REFUSED = {105, 1006}
_PCI_KEYS = {"Occupied", "Recognized", "cardName", "slot"}


async def _fetch(
    client: DsmClient, api: str, method: str, version: int | None = None
) -> tuple[dict[str, Any], bool]:
    """One source's reply ({} when it has none), and whether DSM refused this account.

    Only a refusal (105, 1006) means "needs an administrator": a proxy error or a
    missing API is something else, and the reply mustn't send the user to an admin.
    """
    if api not in client._api_cache:
        return {}, False
    try:
        data = await client.request(api, method, version=version)
    except SynologyError as e:
        logger.debug("%s/%s failed (may require admin): %s", api, method, e)
        return {}, e.code in _REFUSED
    return (data if isinstance(data, dict) else {}), False


def _yes_no(value: Any) -> str:
    return "yes" if value in (True, "yes", 1, "1", "true") else "no"


def _uptime_from_hms(text: str) -> int | None:
    """Core.System's up_time is "hours:minutes:seconds" ("3244:15:30")."""
    try:
        hours, minutes, seconds = (int(x) for x in str(text).split(":"))
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _utc_offset(ntp: dict[str, Any]) -> str | None:
    """The NAS's current UTC offset: its local clock fields against the exact timestamp.

    time_zone_desc names the zone's standard offset ("(GMT-06:00) Central Time"), not
    today's: seska answered -06:00 while on daylight time (-05:00). Region.NTP's
    ``timestamp`` is an exact instant and ``date``/``hour``/``minute``/``second`` the same
    instant on the NAS's clock, so their difference is the offset in force now.
    """
    try:
        local = datetime.strptime(
            f"{ntp['date']} {int(ntp['hour'])}:{int(ntp['minute'])}:{int(ntp['second'])}",
            "%Y/%m/%d %H:%M:%S",
        )
        utc = datetime.fromtimestamp(int(ntp["timestamp"]), tz=UTC).replace(tzinfo=None)
    except (KeyError, TypeError, ValueError):
        return None
    minutes = round((local - utc).total_seconds() / 900) * 15
    sign = "-" if minutes < 0 else "+"
    hours, mins = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{mins:02d}"


def _devices(items: Any) -> str:
    """A USB/SATA device list: each entry's fields, or "none"."""
    if not items:
        return "none"
    if not isinstance(items, list):
        return str(items)
    shown = []
    for item in items:
        if isinstance(item, dict):
            shown.append(", ".join(f"{k}: {v}" for k, v in item.items()))
        else:
            shown.append(str(item))
    return "; ".join(shown)


def _pci_slots(items: Any) -> str:
    if not isinstance(items, list) or not items:
        return "none"
    shown = []
    for slot in items:
        if not isinstance(slot, dict):
            shown.append(str(slot))
            continue
        occupied = _yes_no(slot.get("Occupied")) == "yes"
        card = str(slot.get("cardName") or "").strip()
        card = card if card not in ("", "-") else "unknown card"  # DSM sends "-" for none
        state = card if occupied else "empty"
        if occupied and _yes_no(slot.get("Recognized")) == "no":
            state += " (not recognized)"
        extra = ", ".join(f"{k}: {v}" for k, v in slot.items() if k not in _PCI_KEYS)
        if extra:
            state += f" ({extra})"
        shown.append(f"slot {slot.get('slot', '?')}: {state}")
    return "; ".join(shown)


async def get_system_info(client: DsmClient) -> str:
    """Get everything DSM reports about the NAS.

    SYNO.DSM.Info works for every account. SYNO.Core.System, SYNO.Core.Region.NTP and
    SYNO.Core.System.Status are admin-only (1006 or 105 for any other account, on DSM
    7.1, 7.2.2, 7.3.2 and 7.4.1): for an administrator they add the CPU, time zone, NTP,
    firmware date, update status and devices; for anyone else the reply says plainly
    that those need an administrator.
    """
    dsm, _ = await _fetch(client, "SYNO.DSM.Info", "getinfo")
    core, core_refused = await _fetch(client, "SYNO.Core.System", "info", version=1)
    # Only an admin gets Core.System; don't spend two more refused requests otherwise.
    ntp: dict[str, Any] = {}
    status: dict[str, Any] = {}
    if core:
        ntp, _ = await _fetch(client, "SYNO.Core.Region.NTP", "get", version=1)
        status, _ = await _fetch(client, "SYNO.Core.System.Status", "get", version=1)

    if not dsm and not core:
        error_response(
            ErrorCode.UNAVAILABLE,
            "System info failed: No system information available.",
            retryable=True,
            suggestion="Check that the user has permission to query system info.",
        )

    pairs: list[tuple[str, str]] = []

    model = dsm.get("model") or core.get("model", "")
    if model:
        pairs.append(("Model", str(model)))

    serial = dsm.get("serial") or core.get("serial", "")
    if serial:
        pairs.append(("Serial number", str(serial)))

    firmware = dsm.get("version_string") or core.get("firmware_ver", "")
    build = str(dsm.get("version") or "")
    if firmware or build:
        text = str(firmware or f"build {build}")
        if build and build not in text:
            text += f" (build {build})"
        if core.get("firmware_date"):
            text += f", firmware date {core['firmware_date']}"
        pairs.append(("DSM version", text))

    # VirtualDSM puts the whole name in cpu_vendor and leaves family and series empty;
    # a real NAS splits it ("INTEL", "Atom", "C3538").
    cpu_parts = [str(core.get(k) or "").strip() for k in ("cpu_vendor", "cpu_family", "cpu_series")]
    cpu_name = " ".join(p for p in cpu_parts if p)
    if cpu_name:
        details = []
        if core.get("cpu_cores"):
            details.append(f"{core['cpu_cores']} cores")
        if core.get("cpu_clock_speed"):
            details.append(f"{core['cpu_clock_speed']} MHz")
        pairs.append(("CPU", cpu_name + (f" ({' @ '.join(details)})" if details else "")))

    # Explicit None-checks: 0 is a valid reading, not a missing one.
    ram = dsm["ram"] if dsm.get("ram") is not None else core.get("ram_size")
    if ram:
        pairs.append(("RAM", f"{int(ram):,} MiB"))

    temp = dsm["temperature"] if dsm.get("temperature") is not None else core.get("sys_temp")
    temp_warn = any(
        bool(src.get(k))
        for src, k in (
            (dsm, "temperature_warn"),
            (core, "sys_tempwarn"),
            (core, "systempwarn"),
            (core, "temperature_warning"),
        )
    )
    if temp is not None:
        pairs.append(("Temperature", f"{temp}°C" + (" ⚠ WARNING" if temp_warn else "")))

    uptime = dsm.get("uptime")
    if uptime is None and core.get("up_time"):
        uptime = _uptime_from_hms(core["up_time"])
    if uptime:
        pairs.append(("Uptime", _format_uptime(int(uptime))))

    # DSM sends the NAS's local time with no zone (seska, DSM 7.1.1: "Wed Oct 21
    # 15:47:18 2026" at 20:47 UTC, NAS in CDT), so the label says whose.
    sys_time = dsm.get("time") or core.get("time", "")
    if sys_time:
        pairs.append(("System time", f"{sys_time} (NAS local time)"))

    if core.get("time_zone") or core.get("time_zone_desc") or ntp.get("timezone"):
        desc = re.sub(r"^\(GMT[+-]\d{2}:\d{2}\)\s*", "", str(core.get("time_zone_desc") or ""))
        name = str(core.get("time_zone") or ntp.get("timezone") or "")
        zone = desc or name
        if name and desc and name not in desc:
            zone += f" ({name})"
        offset = _utc_offset(ntp)
        pairs.append(("Time zone", zone + (f", currently {offset}" if offset else "")))
    elif core_refused:
        pairs.append(("Time zone", "not available to this account (needs a DSM administrator)"))
    elif not core:
        pairs.append(("Time zone", "unavailable: SYNO.Core.System didn't answer"))

    if core or ntp:
        ntp_on = core.get("enabled_ntp", ntp.get("enable_ntp") == "ntp")
        server = core.get("ntp_server") or ntp.get("server")
        pairs.append(("NTP", str(server) if ntp_on and server else "off"))

    if dsm.get("codepage"):
        pairs.append(("Code page", str(dsm["codepage"])))

    # These three are named after DSM's own fields and never seen true, so the label
    # names the field rather than claiming a meaning DSM doesn't document.
    for key, label in (
        ("upgrade_ready", "Update ready"),
        ("is_system_crashed", "Crash flag"),
        ("ext_nic_is_incompatible", "Incompatible-NIC flag"),
    ):
        if key in status:
            pairs.append((f"{label} ({key})", _yes_no(status[key])))

    if "support_rp" in core or "rp1" in core or "rp2" in core:
        # Shown as DSM sends them: seen only on VirtualDSM (support_rp true, rp1 1, rp2 1,
        # with no power supplies at all), and the DS1618+ sends none of the three, so what
        # they mean is unverified. The reply mustn't read as "redundant power, both fine".
        raw = [
            f"{k} {_yes_no(core[k]) if k == 'support_rp' else core[k]}"
            for k in ("support_rp", "rp1", "rp2")
            if k in core
        ]
        pairs.append(
            ("Redundant power", f"DSM's raw values {', '.join(raw)} (meaning not documented)")
        )
    if "support_esata" in core:
        pairs.append(("eSATA port", _yes_no(core["support_esata"])))
    if "usb_dev" in core:
        pairs.append(("USB devices", _devices(core["usb_dev"])))
    if "sata_dev" in core:
        pairs.append(("SATA devices", _devices(core["sata_dev"])))
    if "external_pci_slot_info" in core:
        pairs.append(("PCIe slots", _pci_slots(core["external_pci_slot_info"])))

    # Whatever else DSM sends, under its own field name: nothing it reports is hidden.
    for source, known in (
        (dsm, _DSM_INFO_KEYS),
        (core, _CORE_KEYS),
        (ntp, _NTP_KEYS),
        (status, _STATUS_KEYS),
    ):
        for key in sorted(set(source) - known):
            pairs.append((key, str(source[key])))

    if core_refused:
        pairs.append(("Needs a DSM administrator", _ADMIN_ONLY))
    elif not core:
        pairs.append(("Unavailable", f"{_ADMIN_ONLY}: SYNO.Core.System didn't answer"))

    return format_key_value(pairs, title="System Info")
