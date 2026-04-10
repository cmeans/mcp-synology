"""Tests for modules/system/info.py — get_system_info."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx
from mcp.server.fastmcp.exceptions import ToolError

from mcp_synology.core.state import ApiInfoEntry
from mcp_synology.modules.system.info import get_system_info
from tests.conftest import BASE_URL

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient


def _install_system_apis(client: DsmClient) -> None:
    """Add SYNO.DSM.Info and SYNO.Core.System to the client's API cache."""
    client._api_cache["SYNO.DSM.Info"] = ApiInfoEntry(
        path="entry.cgi", min_version=1, max_version=3
    )
    client._api_cache["SYNO.Core.System"] = ApiInfoEntry(
        path="entry.cgi", min_version=1, max_version=3
    )


def _mock_response(request: httpx.Request, responses: dict[str, dict[str, Any]]) -> httpx.Response:
    """Return a canned response keyed on the ``api`` query param."""
    api = dict(request.url.params).get("api", "")
    payload = responses.get(api, {"success": False, "error": {"code": 100}})
    return httpx.Response(200, json=payload)


class TestGetSystemInfo:
    @respx.mock
    async def test_success_with_both_sources(self, mock_client: DsmClient) -> None:
        """Happy path: DSM.Info + Core.System both return data, output merges them."""
        _install_system_apis(mock_client)
        responses = {
            "SYNO.DSM.Info": {
                "success": True,
                "data": {
                    "model": "DS920+",
                    "version_string": "DSM 7.2.1-69057",
                    "ram": 8192,
                    "temperature": 42,
                    "uptime": 123456,
                    "time": "2026-04-10 12:00:00",
                },
            },
            "SYNO.Core.System": {
                "success": True,
                "data": {
                    "cpu_series": "Intel Celeron J4125",
                    "cpu_cores": "4",
                    "cpu_clock_speed": 2000,
                    "enabled_ntp": True,
                    "ntp_server": "pool.ntp.org",
                },
            },
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=lambda req: _mock_response(req, responses)
        )

        result = await get_system_info(mock_client)

        assert "DS920+" in result
        assert "DSM 7.2.1-69057" in result
        assert "Intel Celeron J4125" in result
        assert "4 cores" in result
        assert "2000 MHz" in result
        assert "8192 MB" in result
        assert "42°C" in result
        assert "pool.ntp.org" in result

    @respx.mock
    async def test_success_with_temperature_warning(self, mock_client: DsmClient) -> None:
        """Temperature warning flag should render the warning marker."""
        _install_system_apis(mock_client)
        responses = {
            "SYNO.DSM.Info": {
                "success": True,
                "data": {
                    "model": "DS920+",
                    "version_string": "DSM 7.2",
                    "temperature": 65,
                    "temperature_warn": True,
                },
            },
            "SYNO.Core.System": {"success": True, "data": {}},
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=lambda req: _mock_response(req, responses)
        )

        result = await get_system_info(mock_client)

        assert "65°C" in result
        assert "WARNING" in result

    @respx.mock
    async def test_success_dsm_only_no_core(self, mock_client: DsmClient) -> None:
        """Core.System missing from API cache should still produce output from DSM.Info."""
        mock_client._api_cache["SYNO.DSM.Info"] = ApiInfoEntry(
            path="entry.cgi", min_version=1, max_version=3
        )
        # Intentionally do not install SYNO.Core.System — _fetch_core_system_info
        # must short-circuit and return {} when the API is absent from the cache.
        responses = {
            "SYNO.DSM.Info": {
                "success": True,
                "data": {"model": "DS220j", "version_string": "DSM 7.0"},
            },
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=lambda req: _mock_response(req, responses)
        )

        result = await get_system_info(mock_client)
        assert "DS220j" in result
        assert "DSM 7.0" in result

    @respx.mock
    async def test_both_sources_fail_returns_unavailable(self, mock_client: DsmClient) -> None:
        """If both API calls raise, the tool should emit a retryable unavailable error."""
        _install_system_apis(mock_client)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 105}}
        )

        with pytest.raises(ToolError) as exc_info:
            await get_system_info(mock_client)
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "unavailable"
        assert body["error"]["retryable"] is True

    @respx.mock
    async def test_both_sources_return_empty_data_returns_unavailable(
        self, mock_client: DsmClient
    ) -> None:
        """API calls succeed but populate no fields → unavailable, retryable.

        Hits the first ``error_response`` branch (``not dsm and not core``)
        because empty dicts are falsy.
        """
        _install_system_apis(mock_client)
        responses = {
            "SYNO.DSM.Info": {"success": True, "data": {}},
            "SYNO.Core.System": {"success": True, "data": {}},
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=lambda req: _mock_response(req, responses)
        )

        with pytest.raises(ToolError) as exc_info:
            await get_system_info(mock_client)
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "unavailable"
        assert body["error"]["retryable"] is True

    @respx.mock
    async def test_unrecognized_fields_produce_no_pairs(self, mock_client: DsmClient) -> None:
        """Sources return non-empty dicts but with fields we don't extract.

        This is the SECOND ``if not pairs: error_response(...)`` branch —
        distinct from the empty-dict case above. The dicts are truthy so
        the first branch is skipped, but since none of the expected
        fields (model, version_string, ram, cpu_series, etc.) are
        populated, ``pairs`` stays empty and the late-branch
        ``unavailable`` fires at the bottom of the function.
        """
        _install_system_apis(mock_client)
        responses = {
            "SYNO.DSM.Info": {
                "success": True,
                "data": {"some_unknown_field": "value", "another_unknown": 42},
            },
            "SYNO.Core.System": {
                "success": True,
                "data": {"yet_another_field": "also unknown"},
            },
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=lambda req: _mock_response(req, responses)
        )

        with pytest.raises(ToolError) as exc_info:
            await get_system_info(mock_client)
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "unavailable"
        assert body["error"]["retryable"] is True
        assert "No system information returned" in body["error"]["message"]

    @respx.mock
    async def test_uptime_formatting(self, mock_client: DsmClient) -> None:
        """Uptime seconds should be formatted as days/hours/minutes."""
        _install_system_apis(mock_client)
        # 2 days, 3 hours, 15 minutes = 2*86400 + 3*3600 + 15*60 = 184500
        responses = {
            "SYNO.DSM.Info": {
                "success": True,
                "data": {"model": "DS920+", "uptime": 184500},
            },
            "SYNO.Core.System": {"success": True, "data": {}},
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=lambda req: _mock_response(req, responses)
        )

        result = await get_system_info(mock_client)
        assert "2d" in result
        assert "3h" in result
        assert "15m" in result

    @respx.mock
    async def test_uptime_under_one_minute(self, mock_client: DsmClient) -> None:
        """Uptime of less than 60 seconds renders as '< 1m'."""
        _install_system_apis(mock_client)
        responses = {
            "SYNO.DSM.Info": {
                "success": True,
                "data": {"model": "DS920+", "uptime": 30},
            },
            "SYNO.Core.System": {"success": True, "data": {}},
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=lambda req: _mock_response(req, responses)
        )

        result = await get_system_info(mock_client)
        assert "< 1m" in result
