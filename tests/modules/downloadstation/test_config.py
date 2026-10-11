"""Tests for modules/downloadstation/config.py.

Real DSM payloads + api/method/version-keyed routes (see ``dsm.py``, #123).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx
from mcp.server.fastmcp.exceptions import ToolError

from mcp_synology.modules.downloadstation.config import (
    get_download_config,
    get_schedule,
    set_download_config,
    set_schedule,
)
from tests.modules.downloadstation.dsm import captured, err, get_route, ok

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

INFO = "SYNO.DownloadStation.Info"
SCHED_V1 = "SYNO.DownloadStation.Schedule"
SCHED_DS2 = "SYNO.DownloadStation2.Settings.Scheduler"


def _envelope(exc: ToolError) -> dict[str, Any]:
    return json.loads(str(exc))["error"]  # type: ignore[no-any-return]


def _sent(route: respx.Route) -> dict[str, str]:
    return dict(route.calls.last.request.url.params)


def _manager(is_manager: bool = True) -> None:
    key = "info_getinfo_admin" if is_manager else "info_getinfo_user"
    get_route(INFO, "getinfo", version=1).respond(json=captured(key))


def _ds2_get(plan: str | None = None, **fields: Any) -> respx.Route:
    body = captured("ds2_scheduler_get_admin")
    if plan is not None:
        body["data"]["schedule"] = plan
    body["data"].update(fields)
    return get_route(SCHED_DS2, "get", version=1).respond(json=body)


class TestGetSchedule:
    @respx.mock
    async def test_plan_comes_from_ds2_and_flags_from_v1(self, mock_client: DsmClient) -> None:
        """Regression for #123 bug 2: v1 getconfig has no schedule_plan at all."""
        get_route(SCHED_V1, "getconfig", version=1).respond(json=captured("schedule_getconfig"))
        _ds2_get(plan="2" * 24 + "0" * 144)
        result = await get_schedule(mock_client)
        sun = next(ln for ln in result.splitlines() if ln.startswith("Sun"))
        assert sun.count("~") == 24
        assert "Enabled" in result
        assert "eMule schedule enabled" in result
        assert "Legend" in result

    @respx.mock
    async def test_alt_rates_are_shown(self, mock_client: DsmClient) -> None:
        get_route(SCHED_V1, "getconfig", version=1).respond(json=captured("schedule_getconfig"))
        _ds2_get(download_rate=512, upload_rate=0)
        result = await get_schedule(mock_client)
        assert "Limited download rate:   512 KB/s" in result
        assert "Limited upload rate:     unlimited" in result

    @respx.mock
    async def test_malformed_stored_plan_renders_with_note(self, mock_client: DsmClient) -> None:
        """DS2 stores unvalidated plans; reading one must never error (#123 QA 1)."""
        get_route(SCHED_V1, "getconfig", version=1).respond(json=captured("schedule_getconfig"))
        _ds2_get(plan="9" * 100)
        result = await get_schedule(mock_client)
        assert "stored plan is malformed (100 chars" in result

    @respx.mock
    async def test_non_manager_gets_flags_and_plan_unavailable_note(
        self, mock_client: DsmClient
    ) -> None:
        """#123 bug 11: DS2 Scheduler.get is 105 for non-admins; v1 works."""
        get_route(SCHED_V1, "getconfig", version=1).respond(json=captured("schedule_getconfig"))
        get_route(SCHED_DS2, "get", version=1).respond(json=captured("ds2_scheduler_get_user"))
        result = await get_schedule(mock_client)
        assert "Enabled" in result
        assert "weekly plan not available" in result
        assert "Sun" not in result

    @respx.mock
    async def test_ds2_absent_gets_flags_and_plan_unavailable_note(
        self, mock_client: DsmClient
    ) -> None:
        del mock_client.api_cache[SCHED_DS2]
        get_route(SCHED_V1, "getconfig", version=1).respond(json=captured("schedule_getconfig"))
        result = await get_schedule(mock_client)
        assert "weekly plan not available" in result

    @respx.mock
    async def test_other_ds2_error_propagates(self, mock_client: DsmClient) -> None:
        get_route(SCHED_V1, "getconfig", version=1).respond(json=captured("schedule_getconfig"))
        get_route(SCHED_DS2, "get", version=1).respond(json=err(100))
        with pytest.raises(ToolError, match="100"):
            await get_schedule(mock_client)

    @respx.mock
    async def test_v1_error_propagates(self, mock_client: DsmClient) -> None:
        get_route(SCHED_V1, "getconfig", version=1).respond(json=err(105))
        with pytest.raises(ToolError) as exc:
            await get_schedule(mock_client)
        assert _envelope(exc.value)["code"] == "permission_denied"


class TestSetSchedule:
    """Routing per #123 item 4: enabled/emule_enabled → v1 (works for any
    DS-enabled user); plan and rates → DS2 (admin only), written FIRST.
    """

    @respx.mock
    async def test_plan_goes_to_ds2_json_encoded(self, mock_client: DsmClient) -> None:
        """Regression for #123 bug 3: v1 setconfig silently ignores schedule_plan."""
        plan = "012" * 56
        ds2 = get_route(SCHED_DS2, "set", version=1).respond(json=ok())
        v1 = get_route(SCHED_V1, "setconfig", version=1).respond(json=ok())
        await set_schedule(mock_client, schedule_plan=plan)
        assert json.loads(_sent(ds2)["schedule"]) == plan
        assert not v1.called

    @respx.mock
    async def test_enabled_goes_to_v1_only(self, mock_client: DsmClient) -> None:
        ds2 = get_route(SCHED_DS2, "set").respond(json=ok())
        v1 = get_route(SCHED_V1, "setconfig", version=1).respond(json=ok())
        await set_schedule(mock_client, enabled=True, emule_enabled=False)
        assert _sent(v1)["enabled"] == "true"
        assert _sent(v1)["emule_enabled"] == "false"
        assert not ds2.called

    @respx.mock
    async def test_rates_go_to_ds2(self, mock_client: DsmClient) -> None:
        ds2 = get_route(SCHED_DS2, "set", version=1).respond(json=ok())
        await set_schedule(mock_client, download_rate=512, upload_rate=0)
        assert _sent(ds2)["download_rate"] == "512"
        assert _sent(ds2)["upload_rate"] == "0"

    @respx.mock
    async def test_both_apis_ds2_written_first(self, mock_client: DsmClient) -> None:
        order: list[str] = []

        def _record(name: str) -> Any:
            def _side_effect(_request: httpx.Request) -> httpx.Response:
                order.append(name)
                return httpx.Response(200, json=ok())

            return _side_effect

        get_route(SCHED_DS2, "set", version=1).mock(side_effect=_record("ds2"))
        get_route(SCHED_V1, "setconfig", version=1).mock(side_effect=_record("v1"))
        result = await set_schedule(mock_client, enabled=True, schedule_plan="1" * 168)
        assert order == ["ds2", "v1"]
        assert "enabled" in result and "schedule_plan" in result

    @respx.mock
    async def test_ds2_refusal_means_nothing_applied(self, mock_client: DsmClient) -> None:
        get_route(SCHED_DS2, "set", version=1).respond(json=captured("ds2_scheduler_get_user"))
        v1 = get_route(SCHED_V1, "setconfig", version=1).respond(json=ok())
        with pytest.raises(ToolError) as exc:
            await set_schedule(mock_client, enabled=True, schedule_plan="1" * 168)
        envelope = _envelope(exc.value)
        assert envelope["code"] == "permission_denied"
        assert "nothing was applied" in envelope["message"]
        assert not v1.called

    @respx.mock
    async def test_v1_failure_after_ds2_reports_partial_apply(self, mock_client: DsmClient) -> None:
        get_route(SCHED_DS2, "set", version=1).respond(json=ok())
        get_route(SCHED_V1, "setconfig", version=1).respond(json=err(100))
        with pytest.raises(ToolError) as exc:
            await set_schedule(mock_client, enabled=True, schedule_plan="1" * 168)
        message = _envelope(exc.value)["message"]
        assert "Applied: schedule_plan" in message
        assert "Not applied: enabled" in message

    @respx.mock
    async def test_v1_only_failure_is_surfaced_without_partial_note(
        self, mock_client: DsmClient
    ) -> None:
        get_route(SCHED_V1, "setconfig", version=1).respond(json=err(105))
        with pytest.raises(ToolError) as exc:
            await set_schedule(mock_client, enabled=False)
        message = _envelope(exc.value)["message"]
        assert "partially" not in message
        assert "Permission denied" in message

    @respx.mock
    async def test_ds2_fields_without_ds2_api_are_refused_before_any_write(
        self, mock_client: DsmClient
    ) -> None:
        del mock_client.api_cache[SCHED_DS2]
        v1 = get_route(SCHED_V1, "setconfig", version=1).respond(json=ok())
        with pytest.raises(ToolError, match="weekly plan"):
            await set_schedule(mock_client, enabled=True, schedule_plan="1" * 168)
        assert not v1.called

    @respx.mock
    async def test_captured_error_120_is_explained(self, mock_client: DsmClient) -> None:
        get_route(SCHED_DS2, "set", version=1).respond(json=captured("ds2_scheduler_set_raw_err"))
        with pytest.raises(ToolError, match="JSON"):
            await set_schedule(mock_client, schedule_plan="1" * 168)

    @pytest.mark.parametrize(
        ("plan", "fragment"),
        [("1" * 167, "168"), ("3" + "1" * 167, "'3'"), ("9" * 168, "'9'")],
    )
    async def test_invalid_plan_rejected_before_any_request(
        self, mock_client: DsmClient, plan: str, fragment: str
    ) -> None:
        with pytest.raises(ToolError) as exc:
            await set_schedule(mock_client, schedule_plan=plan)
        assert fragment in _envelope(exc.value)["message"]

    async def test_negative_rate_rejected(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError, match="download_rate"):
            await set_schedule(mock_client, download_rate=-1)

    async def test_no_fields_supplied_raises(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError, match="no fields"):
            await set_schedule(mock_client)


class TestGetDownloadConfig:
    @respx.mock
    async def test_manager_sees_nas_wide_config(self, mock_client: DsmClient) -> None:
        _manager()
        get_route(INFO, "getconfig", version=1).respond(json=captured("info_getconfig_admin"))
        result = await get_download_config(mock_client)
        assert "Download Station configuration" in result
        assert "your user view" not in result
        assert "writable" in result
        assert "BT max upload:        20 KB/s" in result

    @respx.mock
    async def test_non_manager_view_is_labeled(self, mock_client: DsmClient) -> None:
        """#123 bug 10: non-managers get a sanitized per-user view."""
        _manager(is_manager=False)
        get_route(INFO, "getconfig", version=1).respond(json=captured("info_getconfig_user"))
        result = await get_download_config(mock_client)
        assert "your user view — not the NAS-wide settings" in result
        assert "None" not in result  # null destination renders as a dash

    @respx.mock
    async def test_missing_bool_renders_em_dash(self, mock_client: DsmClient) -> None:
        _manager()
        body = captured("info_getconfig_admin")
        del body["data"]["emule_enabled"]
        get_route(INFO, "getconfig", version=1).respond(json=body)
        result = await get_download_config(mock_client)
        assert "eMule enabled:        —" in result

    @respx.mock
    async def test_dsm_error_propagates(self, mock_client: DsmClient) -> None:
        _manager()
        get_route(INFO, "getconfig", version=1).respond(json=err(105))
        with pytest.raises(ToolError) as exc:
            await get_download_config(mock_client)
        assert _envelope(exc.value)["code"] == "permission_denied"


class TestSetDownloadConfig:
    @respx.mock
    async def test_uses_setserverconfig_with_only_supplied_fields(
        self, mock_client: DsmClient
    ) -> None:
        """Regression for #123 bug 4: Info.setconfig does not exist (103)."""
        _manager()
        route = get_route(INFO, "setserverconfig", version=1).respond(json=ok())
        await set_download_config(mock_client, bt_max_download=123, default_destination="writable")
        sent = _sent(route)
        assert sent["bt_max_download"] == "123"
        assert sent["default_destination"] == "writable"
        assert "bt_max_upload" not in sent

    @respx.mock
    @pytest.mark.parametrize(
        ("field", "value"),
        [("bt_max_upload", 5), ("emule_max_download", 10), ("emule_max_upload", 20)],
    )
    async def test_each_rate_field_is_sent(
        self, mock_client: DsmClient, field: str, value: int
    ) -> None:
        _manager()
        route = get_route(INFO, "setserverconfig", version=1).respond(json=ok())
        await set_download_config(mock_client, **{field: value})
        assert _sent(route)[field] == str(value)

    @respx.mock
    async def test_non_manager_is_refused_before_writing(self, mock_client: DsmClient) -> None:
        """#123 bug 12: DSM answers success to a non-manager and ignores the write."""
        _manager(is_manager=False)
        route = get_route(INFO, "setserverconfig", version=1).respond(
            json=captured("info_setserverconfig_user")
        )
        with pytest.raises(ToolError) as exc:
            await set_download_config(mock_client, bt_max_download=777)
        envelope = _envelope(exc.value)
        assert envelope["code"] == "permission_denied"
        assert "silently ignores" in envelope["message"]
        assert not route.called

    async def test_no_fields_supplied_raises(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError, match="no fields"):
            await set_download_config(mock_client)

    @respx.mock
    async def test_dsm_error_propagates(self, mock_client: DsmClient) -> None:
        _manager()
        get_route(INFO, "setserverconfig", version=1).respond(json=err(101))
        with pytest.raises(ToolError, match="101"):
            await set_download_config(mock_client, bt_max_download=1)
