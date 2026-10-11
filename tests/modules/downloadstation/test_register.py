"""Tests for modules/downloadstation/__init__.py — module registration code paths.

Mirrors the structure of tests/modules/filestation/test_register.py. As Phase 1
adds each tool, this file gains a test asserting the tool is registered when
allowed and absent when filtered out.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from mcp.server.fastmcp import FastMCP

from mcp_synology.modules import RegisterContext
from mcp_synology.modules.downloadstation import MODULE_INFO, register


def _make_ctx(
    allowed: set[str] | None = None,
    settings: dict | None = None,
) -> tuple[FastMCP, MagicMock, RegisterContext]:
    server = FastMCP("test-ds")
    manager = MagicMock()
    fake_client = MagicMock()
    manager.get_client = AsyncMock(return_value=fake_client)
    manager.with_update_notice = MagicMock(side_effect=lambda s: s)

    if allowed is None:
        allowed = {t.name for t in MODULE_INFO.tools}

    ctx = RegisterContext(
        server=server,
        manager=manager,
        allowed_tools=allowed,
        settings_dict=settings or {},
        display_name="test-nas",
    )
    return server, manager, ctx


class TestDownloadstationModuleRegister:
    def test_module_info_declares_required_dsm_task_api(self) -> None:
        api_names = {req.api_name for req in MODULE_INFO.required_apis}
        assert "SYNO.DownloadStation.Task" in api_names
        required = [r for r in MODULE_INFO.required_apis if not r.optional]
        assert len(required) == 1
        assert required[0].api_name == "SYNO.DownloadStation.Task"

    def test_module_info_tools_present(self) -> None:
        tool_names = {t.name for t in MODULE_INFO.tools}
        # Phase 1 + Phase 2 — Phase 3 (BT search + RSS) lands separately.
        assert tool_names == {
            # Phase 1 READ
            "list_downloads",
            "get_download_info",
            "get_download_stats",
            "get_download_config",
            "get_schedule",
            # Phase 2 WRITE
            "create_download",
            "delete_download",
            "pause_download",
            "resume_download",
            "edit_download",
            "set_download_config",
            "set_schedule",
        }

    def test_module_info_phase1_tools_are_read_phase2_tools_are_write(self) -> None:
        from mcp_synology.modules import PermissionTier

        read_names = {
            "list_downloads",
            "get_download_info",
            "get_download_stats",
            "get_download_config",
            "get_schedule",
        }
        write_names = {
            "create_download",
            "delete_download",
            "pause_download",
            "resume_download",
            "edit_download",
            "set_download_config",
            "set_schedule",
        }
        for tool in MODULE_INFO.tools:
            if tool.name in read_names:
                assert tool.permission_tier == PermissionTier.READ, f"{tool.name} should be READ"
            elif tool.name in write_names:
                assert tool.permission_tier == PermissionTier.WRITE, f"{tool.name} should be WRITE"
            else:
                raise AssertionError(f"unexpected tool {tool.name}")

    def test_register_no_tools_when_none_allowed(self) -> None:
        server, _manager, ctx = _make_ctx(allowed=set())
        register(ctx)
        assert server._tool_manager._tools == {}

    def test_register_all_tools_when_all_allowed(self) -> None:
        server, _manager, ctx = _make_ctx()
        register(ctx)
        registered = set(server._tool_manager._tools.keys())
        expected = {t.name for t in MODULE_INFO.tools}
        assert registered == expected


class TestDownloadstationToolInvocation:
    """Invoke each registered tool to walk the closure body lines.

    Mirrors tests/modules/filestation/test_register.py::TestFilestationToolInvocation.
    The downloadstation register() body has one closure per tool; without this
    coverage the closures sit at the bottom of __init__.py untested even after
    integration tests confirm registration succeeds.
    """

    @staticmethod
    def _capture_call(monkeypatch, target: str) -> AsyncMock:
        mock = AsyncMock(return_value=f"<<{target}-result>>")
        monkeypatch.setattr(target, mock)
        return mock

    async def test_list_downloads_invocation(self, monkeypatch) -> None:
        server, manager, ctx = _make_ctx()
        target = "mcp_synology.modules.downloadstation.tasks.list_downloads"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        result = await server._tool_manager._tools["list_downloads"].fn()
        assert result == f"<<{target}-result>>"
        manager.get_client.assert_awaited()
        mock.assert_awaited_once()

    async def test_get_download_info_invocation(self, monkeypatch) -> None:
        server, _manager, ctx = _make_ctx()
        target = "mcp_synology.modules.downloadstation.tasks.get_download_info"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        result = await server._tool_manager._tools["get_download_info"].fn(task_id="dbid_001")
        assert result == f"<<{target}-result>>"
        mock.assert_awaited_once()

    async def test_get_download_stats_invocation(self, monkeypatch) -> None:
        server, _manager, ctx = _make_ctx()
        target = "mcp_synology.modules.downloadstation.stats.get_download_stats"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        result = await server._tool_manager._tools["get_download_stats"].fn()
        assert result == f"<<{target}-result>>"
        mock.assert_awaited_once()

    async def test_get_download_config_invocation(self, monkeypatch) -> None:
        server, _manager, ctx = _make_ctx()
        target = "mcp_synology.modules.downloadstation.config.get_download_config"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        result = await server._tool_manager._tools["get_download_config"].fn()
        assert result == f"<<{target}-result>>"
        mock.assert_awaited_once()

    async def test_get_schedule_invocation(self, monkeypatch) -> None:
        server, _manager, ctx = _make_ctx()
        target = "mcp_synology.modules.downloadstation.config.get_schedule"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        result = await server._tool_manager._tools["get_schedule"].fn()
        assert result == f"<<{target}-result>>"
        mock.assert_awaited_once()


class TestDownloadstationPhase2ToolInvocation:
    """Invoke each registered WRITE tool closure to walk the body lines.

    Mirrors TestDownloadstationToolInvocation for Phase 1. Without these the
    seven new closures in __init__.py sit untested at module-coverage time,
    even though their downstream handlers have direct tests.
    """

    @staticmethod
    def _capture_call(monkeypatch, target: str) -> AsyncMock:
        mock = AsyncMock(return_value=f"<<{target}-result>>")
        monkeypatch.setattr(target, mock)
        return mock

    async def test_create_download_invocation(self, monkeypatch) -> None:
        server, _manager, ctx = _make_ctx()
        target = "mcp_synology.modules.downloadstation.tasks.create_download"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        result = await server._tool_manager._tools["create_download"].fn(uri="magnet:?...")
        assert result == f"<<{target}-result>>"
        mock.assert_awaited_once()

    async def test_delete_download_invocation(self, monkeypatch) -> None:
        server, _manager, ctx = _make_ctx()
        target = "mcp_synology.modules.downloadstation.tasks.delete_download"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        result = await server._tool_manager._tools["delete_download"].fn(
            task_ids=["dbid_001"], force_complete=True
        )
        assert result == f"<<{target}-result>>"
        assert mock.await_args.kwargs == {"task_ids": ["dbid_001"], "force_complete": True}

    def test_delete_download_has_no_delete_data_param(self) -> None:
        """#123 bug 7: DS never deletes completed files; delete_data was a false promise."""
        server, _manager, ctx = _make_ctx()
        register(ctx)
        params = server._tool_manager._tools["delete_download"].parameters["properties"]
        assert "delete_data" not in params
        assert params["force_complete"]["default"] is False

    async def test_pause_download_invocation(self, monkeypatch) -> None:
        server, _manager, ctx = _make_ctx()
        target = "mcp_synology.modules.downloadstation.tasks.pause_download"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        result = await server._tool_manager._tools["pause_download"].fn(task_ids=["dbid_001"])
        assert result == f"<<{target}-result>>"
        mock.assert_awaited_once()

    async def test_resume_download_invocation(self, monkeypatch) -> None:
        server, _manager, ctx = _make_ctx()
        target = "mcp_synology.modules.downloadstation.tasks.resume_download"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        result = await server._tool_manager._tools["resume_download"].fn(task_ids=["dbid_001"])
        assert result == f"<<{target}-result>>"
        mock.assert_awaited_once()

    async def test_edit_download_invocation(self, monkeypatch) -> None:
        server, _manager, ctx = _make_ctx()
        target = "mcp_synology.modules.downloadstation.tasks.edit_download"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        result = await server._tool_manager._tools["edit_download"].fn(
            task_ids=["dbid_001"], destination="downloads"
        )
        assert result == f"<<{target}-result>>"
        mock.assert_awaited_once()

    async def test_set_download_config_invocation(self, monkeypatch) -> None:
        server, _manager, ctx = _make_ctx()
        target = "mcp_synology.modules.downloadstation.config.set_download_config"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        result = await server._tool_manager._tools["set_download_config"].fn(bt_max_download=100)
        assert result == f"<<{target}-result>>"
        mock.assert_awaited_once()

    async def test_set_schedule_invocation(self, monkeypatch) -> None:
        server, _manager, ctx = _make_ctx()
        target = "mcp_synology.modules.downloadstation.config.set_schedule"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        result = await server._tool_manager._tools["set_schedule"].fn(
            enabled=True, download_rate=512, upload_rate=0
        )
        assert result == f"<<{target}-result>>"
        kwargs = mock.await_args.kwargs
        assert kwargs["download_rate"] == 512
        assert kwargs["upload_rate"] == 0


class TestModuleInfoDescriptions:
    """#123 item 12: every tool whose user-facing behavior changed says so."""

    @staticmethod
    def _desc(name: str) -> str:
        return next(t.description for t in MODULE_INFO.tools if t.name == name)

    def test_ds2_scheduler_is_an_optional_requirement(self) -> None:
        reqs = {r.api_name: r for r in MODULE_INFO.required_apis}
        assert reqs["SYNO.DownloadStation2.Settings.Scheduler"].optional

    def test_list_downloads_mentions_own_tasks_for_non_managers(self) -> None:
        assert "only their own tasks" in self._desc("list_downloads")

    def test_get_download_config_no_longer_claims_plan_summary(self) -> None:
        desc = self._desc("get_download_config")
        assert "plan summary" not in desc
        assert "per-user view" in desc

    def test_set_download_config_mentions_manager_requirement(self) -> None:
        assert "manager" in self._desc("set_download_config")

    def test_schedule_tools_describe_verified_plan_format(self) -> None:
        for name in ("get_schedule", "set_schedule"):
            desc = self._desc(name)
            assert "full speed" in desc and "limited" in desc, name
        set_desc = self._desc("set_schedule")
        assert "Sun" in set_desc and "download_rate" in set_desc

    def test_create_download_mentions_returned_ids(self) -> None:
        assert "task IDs" in self._desc("create_download")

    def test_edit_download_mentions_version_requirement(self) -> None:
        assert "newer Download Station" in self._desc("edit_download")

    def test_delete_download_describes_real_file_semantics(self) -> None:
        desc = self._desc("delete_download")
        assert "finished tasks are always kept" in desc
        assert "force_complete" in desc
        assert "delete_data" not in desc
