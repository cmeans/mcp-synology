"""Tests for modules/system/utilization.py — get_resource_usage."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import respx
from mcp.server.fastmcp.exceptions import ToolError

from mcp_synology.core.state import ApiInfoEntry
from mcp_synology.modules.system.utilization import get_resource_usage
from tests.conftest import BASE_URL

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient


def _install_utilization_api(client: DsmClient) -> None:
    client._api_cache["SYNO.Core.System.Utilization"] = ApiInfoEntry(
        path="entry.cgi", min_version=1, max_version=3
    )


class TestGetResourceUsage:
    @respx.mock
    async def test_success_full_payload(self, mock_client: DsmClient) -> None:
        """Happy path: CPU + memory + network + disk all populated."""
        _install_utilization_api(mock_client)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "cpu": {
                        "user_load": 12,
                        "system_load": 5,
                        "other_load": 2,
                        "1min_load": 150,
                        "5min_load": 120,
                        "15min_load": 100,
                    },
                    "memory": {
                        "memory_size": 8388608,
                        "real_usage": 35,
                        "avail_real": 5452595,
                        "swap_usage": 2,
                        "total_swap": 2097152,
                    },
                    "network": [
                        {"device": "total", "rx": 1024000, "tx": 512000},
                        {"device": "eth0", "rx": 512000, "tx": 256000},
                    ],
                    "disk": {
                        "disk": [
                            {
                                "device": "sda",
                                "utilization": 15,
                                "read_byte": 1024,
                                "write_byte": 2048,
                            }
                        ]
                    },
                },
            }
        )

        result = await get_resource_usage(mock_client)
        assert "Resource Usage" in result
        assert "eth0" in result
        assert "sda" in result

    @respx.mock
    async def test_cpu_other_load_fallback_format(self, mock_client: DsmClient) -> None:
        """Alternate CPU format: no 15min_load, no system/user_load, just other_load.

        Covers the ``elif "other_load" in cpu:`` branch — some DSM versions
        report CPU purely as ``other_load`` (e.g., single-percent roll-up)
        rather than the system/user split or the load-average block.
        """
        _install_utilization_api(mock_client)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "cpu": {"other_load": 42},
                    "memory": {"real_usage": 10},
                    "disk": {"disk": []},
                },
            }
        )
        result = await get_resource_usage(mock_client)
        assert "CPU usage" in result
        assert "42%" in result

    @respx.mock
    async def test_memory_cached_and_swap_detail(self, mock_client: DsmClient) -> None:
        """Memory payload with cached bytes and swap-in activity.

        Covers the ``if cached:`` and ``if swap_used:`` branches in
        ``_format_memory``. Both are conditional additions to the memory
        output that only trigger when the NAS reports non-zero values.
        """
        _install_utilization_api(mock_client)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "cpu": {"user_load": 5, "system_load": 2, "other_load": 0},
                    "memory": {
                        "memory_size": 8388608,  # 8 GB in KB
                        "real_usage": 40,
                        "avail_real": 5242880,  # ~5 GB
                        "cached": 1048576,  # 1 GB cached
                        "si_disk": 25,  # active swap-in
                    },
                    "disk": {"disk": []},
                },
            }
        )
        result = await get_resource_usage(mock_client)
        assert "1024 MB cached" in result
        assert "Swap in" in result
        assert "25 pages/s" in result

    @respx.mock
    async def test_disk_unexpected_type_falls_through_to_empty_list(
        self, mock_client: DsmClient
    ) -> None:
        """Disk as neither dict nor list → treated as empty, no disk rows.

        Covers the ``else: disk_list = []`` fallback in get_resource_usage.
        Should not raise — just render without any disk entries. Uses a
        CPU/memory payload so ``pairs`` is non-empty and we don't trip
        the ``unavailable`` branch.
        """
        _install_utilization_api(mock_client)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "cpu": {"user_load": 5, "system_load": 2},
                    "memory": {"real_usage": 30},
                    # Neither dict with 'disk' key nor a bare list — e.g.,
                    # DSM returned a string sentinel or null-like value.
                    "disk": "unavailable",
                },
            }
        )
        result = await get_resource_usage(mock_client)
        assert "Resource Usage" in result
        assert "CPU usage" in result
        # No disk entries rendered
        assert "Disk (" not in result

    @respx.mock
    async def test_success_disk_as_list(self, mock_client: DsmClient) -> None:
        """DSM sometimes returns disk as a bare list instead of {'disk': [...]}."""
        _install_utilization_api(mock_client)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "cpu": {"user_load": 10, "system_load": 5, "other_load": 0},
                    "memory": {"memory_size": 8388608, "real_usage": 30},
                    "disk": [
                        {
                            "device": "sdb",
                            "utilization": 25,
                            "read_byte": 0,
                            "write_byte": 0,
                        }
                    ],
                },
            }
        )
        result = await get_resource_usage(mock_client)
        assert "sdb" in result

    async def test_api_not_in_cache_returns_api_not_found(self, mock_client: DsmClient) -> None:
        """If SYNO.Core.System.Utilization isn't cached, report api_not_found."""
        # Deliberately do NOT install the API
        with pytest.raises(ToolError) as exc_info:
            await get_resource_usage(mock_client)
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "api_not_found"
        assert body["error"]["retryable"] is False

    @respx.mock
    async def test_dsm_105_maps_to_permission_denied(self, mock_client: DsmClient) -> None:
        """DSM code 105 on this API means the account isn't admin — not a session issue."""
        _install_utilization_api(mock_client)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 105}}
        )

        with pytest.raises(ToolError) as exc_info:
            await get_resource_usage(mock_client)
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "permission_denied"
        assert body["error"]["retryable"] is False
        assert "admin" in body["error"]["suggestion"].lower()

    @respx.mock
    async def test_other_dsm_error_propagates_as_synology_error(
        self, mock_client: DsmClient
    ) -> None:
        """Non-105 DSM errors fall through to synology_error_response."""
        _install_utilization_api(mock_client)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 100}}
        )

        with pytest.raises(ToolError) as exc_info:
            await get_resource_usage(mock_client)
        body = json.loads(str(exc_info.value))
        # Code 100 is unknown/generic — routes to dsm_error
        assert body["error"]["code"] == "dsm_error"

    @respx.mock
    async def test_empty_payload_returns_unavailable_retryable(
        self, mock_client: DsmClient
    ) -> None:
        """API returns success but no metrics → unavailable/retryable=True.

        This is the finding-2 fix: previously this call site emitted
        retryable=False, contradicting info.py and the PR body table.
        """
        _install_utilization_api(mock_client)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(json={"success": True, "data": {}})

        with pytest.raises(ToolError) as exc_info:
            await get_resource_usage(mock_client)
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "unavailable"
        assert body["error"]["retryable"] is True
