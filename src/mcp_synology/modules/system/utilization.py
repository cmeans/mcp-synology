"""System utilization tool: get_resource_usage."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mcp_synology.core.errors import SynologyError
from mcp_synology.core.formatting import (
    error_response,
    format_key_value,
    format_size,
    synology_error_response,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)


def _format_rate(bytes_per_sec: float) -> str:
    """Format a transfer rate as human-readable string."""
    return f"{format_size(int(bytes_per_sec))}/s"


def _format_cpu(cpu: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract CPU utilization pairs."""
    pairs: list[tuple[str, str]] = []
    total = cpu.get("system_load", 0) + cpu.get("user_load", 0)
    # Some DSM versions provide 15min_load, 5min_load, 1min_load
    # Others provide system_load + user_load + other_load
    if "15min_load" in cpu:
        pairs.append(
            (
                "CPU load avg",
                f"1m={cpu.get('1min_load', 0)}  "
                f"5m={cpu.get('5min_load', 0)}  "
                f"15m={cpu.get('15min_load', 0)}",
            )
        )
    if total > 0:
        pairs.append(
            (
                "CPU usage",
                f"{total}% (system={cpu.get('system_load', 0)}%, user={cpu.get('user_load', 0)}%)",
            )
        )
    elif "other_load" in cpu:
        other = cpu.get("other_load", 0)
        pairs.append(("CPU usage", f"{other}%"))
    return pairs


def _format_memory(mem: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract memory utilization pairs."""
    pairs: list[tuple[str, str]] = []
    usage_pct = mem.get("real_usage", 0)
    total = mem.get("memory_size", 0)  # in KB
    avail = mem.get("avail_real", 0)  # in KB
    cached = mem.get("cached", 0)  # in KB
    swap_used = mem.get("si_disk", 0)

    if usage_pct:
        pairs.append(("Memory usage", f"{usage_pct}%"))
    if total:
        total_mb = total // 1024
        avail_mb = avail // 1024 if avail else 0
        detail = f"{total_mb} MB total"
        if avail_mb:
            detail += f", {avail_mb} MB available"
        if cached:
            detail += f", {cached // 1024} MB cached"
        pairs.append(("Memory detail", detail))
    if swap_used:
        pairs.append(("Swap in", f"{swap_used} pages/s"))
    return pairs


def _format_network(net: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Extract network utilization pairs."""
    pairs: list[tuple[str, str]] = []
    for iface in net:
        device = iface.get("device", "unknown")
        rx = iface.get("rx", 0)  # bytes/sec
        tx = iface.get("tx", 0)  # bytes/sec
        if rx or tx:
            pairs.append((f"Network ({device})", f"↓ {_format_rate(rx)}  ↑ {_format_rate(tx)}"))
    return pairs


def _format_disk(disks: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Extract disk I/O utilization pairs."""
    pairs: list[tuple[str, str]] = []
    for disk in disks:
        device = disk.get("device", "unknown")
        util = disk.get("utilization", 0)
        read_rate = disk.get("read_byte", 0)
        write_rate = disk.get("write_byte", 0)
        if util or read_rate or write_rate:
            detail = f"{util}% busy"
            if read_rate or write_rate:
                detail += f"  R={_format_rate(read_rate)}  W={_format_rate(write_rate)}"
            pairs.append((f"Disk ({device})", detail))
    return pairs


async def get_resource_usage(client: DsmClient) -> str:
    """Get live system resource utilization."""
    if "SYNO.Core.System.Utilization" not in client._api_cache:
        error_response(
            "api_not_found",
            "Resource usage failed: SYNO.Core.System.Utilization API not available.",
            retryable=False,
            suggestion="This API may require admin privileges or may not be available "
            "on this DSM version.",
        )

    try:
        data = await client.request("SYNO.Core.System.Utilization", "get")
    except SynologyError as e:
        if e.code == 105:
            error_response(
                "permission_denied",
                "Resource usage failed: Permission denied — admin account required.",
                retryable=False,
                suggestion="The SYNO.Core.System.Utilization API requires an admin DSM account. "
                "Configure an admin connection or check DSM user permissions.",
            )
        synology_error_response("Resource usage", e)

    pairs: list[tuple[str, str]] = []

    # CPU
    cpu = data.get("cpu", {})
    if cpu:
        pairs.extend(_format_cpu(cpu))

    # Memory
    mem = data.get("memory", {})
    if mem:
        pairs.extend(_format_memory(mem))

    # Network
    net = data.get("network", [])
    if net:
        pairs.extend(_format_network(net))

    # Disk
    disks = data.get("disk", {})
    if isinstance(disks, dict):
        disk_list = disks.get("disk", [])
    elif isinstance(disks, list):
        disk_list = disks
    else:
        disk_list = []
    if disk_list:
        pairs.extend(_format_disk(disk_list))

    if not pairs:
        error_response(
            "unavailable",
            "Resource usage failed: No utilization data returned.",
            retryable=False,
            suggestion="The API returned successfully but no metrics were populated.",
        )

    return format_key_value(pairs, title="Resource Usage")
