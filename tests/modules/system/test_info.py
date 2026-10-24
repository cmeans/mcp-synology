"""Tests for modules/system/info.py — get_system_info."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

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
        assert "8,192 MiB" in result
        assert "42°C" in result
        # The whole row, not a substring (CodeQL read `"<host>" in s` as a URL check).
        assert ["NTP:", "pool.ntp.org"] in [line.split() for line in result.splitlines()]
        # DSM sends the NAS's local time with no zone, so the label says whose.
        assert "System time:  2026-04-10 12:00:00 (NAS local time)" in result

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


# --- Captured replies (fixtures/README.md) -----------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures"
_SOURCES = (
    "SYNO.DSM.Info",
    "SYNO.Core.System",
    "SYNO.Core.Region.NTP",
    "SYNO.Core.System.Status",
)


def _serve_capture(client: DsmClient, name: str) -> list[str]:
    """Answer every system API from a captured reply; returns the APIs called."""
    for api in _SOURCES:
        client._api_cache[api] = ApiInfoEntry(path="entry.cgi", min_version=1, max_version=3)
    capture = json.loads((_FIXTURES / name).read_text())
    called: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        api = dict(request.url.params).get("api", "")
        called.append(api)
        data = capture[api]
        if "error" in data and len(data) == 1:
            return httpx.Response(200, json={"success": False, "error": {"code": data["error"]}})
        return httpx.Response(200, json={"success": True, "data": data})

    respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=respond)
    return called


def _rows(result: str) -> dict[str, str]:
    rows = {}
    for line in result.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and value.startswith("  "):
            rows[key] = value.strip()
    return rows


_ZONES = {
    "7.1": "Central Time (US & Canada), currently UTC-05:00",
    "7.2.2": "Greenwich Mean Time (GMT), currently UTC+00:00",
    "7.3.2": "Bombay, Calcutta, Madras, New Delhi, Colombo, currently UTC+05:30",
    "7.4.1": "Amsterdam, Berlin, Rome, Stockholm, Vienna, currently UTC+02:00",
}


class TestEverythingDsmSends:
    """Every field DSM sends is shown (maintainer, 2026-10-23), serial number included."""

    @pytest.mark.parametrize("version", sorted(_ZONES))
    @respx.mock
    async def test_admin_sees_every_source(self, mock_client: DsmClient, version: str) -> None:
        _serve_capture(mock_client, f"{version}-admin.json")
        rows = _rows(await get_system_info(mock_client))
        assert rows["Model"] == "VirtualDSM"
        assert rows["Serial number"] == "0000000000000"
        # VirtualDSM puts the whole CPU name in cpu_vendor; family and series are empty.
        assert rows["CPU"] == "AMD Ryzen 9 7940HS (2 cores @ 3992 MHz)"
        # The zone and the offset in force now, from Region.NTP's exact timestamp: each
        # vdsm image runs in its own zone (tests/vdsm/config.py:DSM_TIMEZONES).
        assert rows["Time zone"] == _ZONES[version]
        assert rows["NTP"] == "time.google.com"
        assert rows["Code page"] == "enu"
        assert rows["Update ready (upgrade_ready)"] == "no"
        assert rows["Crash flag (is_system_crashed)"] == "no"
        assert rows["Redundant power"] == (
            "DSM's raw values support_rp yes, rp1 1, rp2 1 (meaning not documented)"
        )
        assert rows["eSATA port"] == "no"
        assert rows["USB devices"] == "none"
        assert rows["SATA devices"] == "none"
        assert rows["PCIe slots"] == "none"
        assert ", firmware date " in rows["DSM version"]
        assert "Needs a DSM administrator" not in rows

    @pytest.mark.parametrize("version", sorted(_ZONES))
    @respx.mock
    async def test_other_accounts_are_told_plainly(
        self, mock_client: DsmClient, version: str
    ) -> None:
        called = _serve_capture(mock_client, f"{version}-user.json")
        rows = _rows(await get_system_info(mock_client))
        assert rows["Serial number"] == "0000000000000"
        assert rows["DSM version"].startswith(f"DSM {version}")
        assert rows["Code page"] == "enu"
        assert rows["Time zone"] == "not available to this account (needs a DSM administrator)"
        assert rows["Needs a DSM administrator"].startswith("CPU, NTP server, firmware date")
        assert "update-ready" in rows["Needs a DSM administrator"]
        assert "CPU" not in rows
        # Core.System's refusal means the other two admin-only APIs aren't tried.
        assert "SYNO.Core.Region.NTP" not in called
        assert "SYNO.Core.System.Status" not in called

    @respx.mock
    async def test_a_real_nas_splits_the_cpu_name(self, mock_client: DsmClient) -> None:
        _serve_capture(mock_client, "ds1618-7.1.1-admin.json")
        rows = _rows(await get_system_info(mock_client))
        assert rows["CPU"] == "INTEL Atom C3538 (4 cores @ 2100 MHz)"
        assert rows["DSM version"] == "DSM 7.1.1-42962 Update 9, firmware date 2025/07/29"
        # time_zone_desc says GMT-06:00 (standard time); the NAS was on daylight time.
        assert rows["Time zone"] == "Central Time (US & Canada), currently UTC-05:00"
        assert rows["PCIe slots"] == "slot 1: empty"
        assert rows["eSATA port"] == "yes"
        assert "Redundant power" not in rows  # a single-PSU DS1618+ sends no rp fields
        assert rows["Temperature"] == "50°C"

    @respx.mock
    async def test_a_field_we_dont_know_is_shown_under_its_name(
        self, mock_client: DsmClient
    ) -> None:
        _install_system_apis(mock_client)
        responses = {
            "SYNO.DSM.Info": {"success": True, "data": {"model": "DS920+", "new_field": "x1"}},
            "SYNO.Core.System": {"success": True, "data": {"other_new": 7}},
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=lambda req: _mock_response(req, responses)
        )
        rows = _rows(await get_system_info(mock_client))
        assert rows["new_field"] == "x1"
        assert rows["other_new"] == "7"


class TestHelpers:
    @pytest.mark.parametrize(
        ("text", "seconds"),
        [("0:0:24", 24), ("3244:15:30", 11679330), ("1:2", None), ("a:b:c", None), ("", None)],
    )
    def test_uptime_from_hms(self, text: str, seconds: int | None) -> None:
        from mcp_synology.modules.system.info import _uptime_from_hms

        assert _uptime_from_hms(text) == seconds

    @pytest.mark.parametrize(
        ("local", "offset"),
        [
            ("2026/10/24 00:30:00", "UTC-03:30"),  # Newfoundland, across midnight
            ("2026/10/24 09:45:00", "UTC+05:45"),  # Nepal
            ("2026/10/24 18:00:00", "UTC+14:00"),  # Kiribati
            ("2026/10/23 23:30:00", "UTC-04:30"),  # previous day on the NAS
        ],
    )
    def test_utc_offset_any_zone_and_date(self, local: str, offset: str) -> None:
        from mcp_synology.modules.system.info import _utc_offset

        date, clock = local.split()
        hour, minute, second = clock.split(":")
        # 2026-10-24 04:00:00 UTC
        ntp = {"date": date, "hour": hour, "minute": minute, "second": second}
        assert _utc_offset({**ntp, "timestamp": 1792814400}) == offset

    def test_utc_offset_without_the_fields_is_none(self) -> None:
        from mcp_synology.modules.system.info import _utc_offset

        assert _utc_offset({}) is None
        assert (
            _utc_offset({"date": "x", "hour": 1, "minute": 2, "second": 3, "timestamp": 0}) is None
        )

    def test_devices_and_slots(self) -> None:
        from mcp_synology.modules.system.info import _devices, _pci_slots

        assert _devices([]) == "none"
        assert _devices([{"vendor": "SanDisk", "product": "USB Disk"}, "sdq"]) == (
            "vendor: SanDisk, product: USB Disk; sdq"
        )
        assert _devices("odd") == "odd"
        slots = [
            {"Occupied": "no", "Recognized": "no", "cardName": "-", "slot": "1"},
            {"Occupied": "yes", "Recognized": "yes", "cardName": "E10G18-T1", "slot": "2"},
            {"Occupied": "yes", "Recognized": "no", "cardName": "-", "slot": "3", "lanes": 4},
            "odd",
        ]
        assert _pci_slots(slots) == (
            "slot 1: empty; slot 2: E10G18-T1; "
            "slot 3: unknown card (not recognized) (lanes: 4); odd"
        )
        assert _pci_slots([]) == "none"


class TestWhenCoreSystemFailsOtherwise:
    @respx.mock
    async def test_a_server_error_isnt_called_a_permission_problem(
        self, mock_client: DsmClient
    ) -> None:
        """Only DSM's refusal (105, 1006) means "needs an administrator"."""
        _install_system_apis(mock_client)

        def respond(request: httpx.Request) -> httpx.Response:
            if dict(request.url.params).get("api") == "SYNO.Core.System":
                return httpx.Response(200, json={"success": False, "error": {"code": 102}})
            data = {"model": "DS920+", "version_string": "DSM 7.2-64570", "version": "64561"}
            return httpx.Response(200, json={"success": True, "data": data})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=respond)
        rows = _rows(await get_system_info(mock_client))
        assert rows["Time zone"] == "unavailable: SYNO.Core.System didn't answer"
        assert rows["Unavailable"].endswith(": SYNO.Core.System didn't answer")
        assert "Needs a DSM administrator" not in rows
        # The build is added when the version string doesn't already carry it.
        assert rows["DSM version"] == "DSM 7.2-64570 (build 64561)"

    @respx.mock
    async def test_uptime_from_core_system(self, mock_client: DsmClient) -> None:
        _install_system_apis(mock_client)
        responses = {
            "SYNO.DSM.Info": {"success": True, "data": {"model": "DS920+"}},
            "SYNO.Core.System": {"success": True, "data": {"up_time": "26:00:00"}},
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=lambda req: _mock_response(req, responses)
        )
        assert _rows(await get_system_info(mock_client))["Uptime"] == "1d 2h"
