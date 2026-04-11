"""Tests for modules/system/__init__.py — module registration code paths.

The system module's `register()` function declares two tools (get_system_info,
get_resource_usage) inside `if "<tool>" in ctx.allowed_tools:` blocks. Static
import in `tests/modules/test_module_system.py` covers the module-level
constants but not the registration body. These tests exercise both branches
plus the empty-allowed case, then invoke the registered tool functions to
walk through the inner closures (`await manager.get_client()` etc.) so the
underlying domain functions get called as well.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from mcp.server.fastmcp import FastMCP

from mcp_synology.modules import RegisterContext
from mcp_synology.modules.system import MODULE_INFO, register

if TYPE_CHECKING:
    import pytest


def _make_ctx(allowed: set[str] | None = None) -> tuple[FastMCP, MagicMock, RegisterContext]:
    """Create a real FastMCP server + mock manager + RegisterContext for tests."""
    server = FastMCP("test-system")
    manager = MagicMock()
    fake_client = MagicMock()
    manager.get_client = AsyncMock(return_value=fake_client)

    if allowed is None:
        allowed = {t.name for t in MODULE_INFO.tools}

    ctx = RegisterContext(
        server=server,
        manager=manager,
        allowed_tools=allowed,
        settings_dict={},
        display_name="test-nas",
    )
    return server, manager, ctx


class TestSystemModuleRegister:
    def test_register_with_all_tools_allowed(self) -> None:
        server, _manager, ctx = _make_ctx()
        register(ctx)
        registered = set(server._tool_manager._tools.keys())
        assert registered == {"get_system_info", "get_resource_usage"}

    def test_register_with_only_get_system_info_allowed(self) -> None:
        server, _manager, ctx = _make_ctx(allowed={"get_system_info"})
        register(ctx)
        assert set(server._tool_manager._tools.keys()) == {"get_system_info"}

    def test_register_with_only_get_resource_usage_allowed(self) -> None:
        server, _manager, ctx = _make_ctx(allowed={"get_resource_usage"})
        register(ctx)
        assert set(server._tool_manager._tools.keys()) == {"get_resource_usage"}

    def test_register_with_no_tools_allowed(self) -> None:
        server, _manager, ctx = _make_ctx(allowed=set())
        register(ctx)
        assert server._tool_manager._tools == {}

    def test_registered_tools_have_descriptions_from_module_info(self) -> None:
        server, _manager, ctx = _make_ctx()
        register(ctx)
        for tool_info in MODULE_INFO.tools:
            registered = server._tool_manager._tools[tool_info.name]
            assert registered.description == tool_info.description

    async def test_get_system_info_invocation_calls_underlying(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invoking the registered tool walks the closure: get_client → get_system_info."""
        server, manager, ctx = _make_ctx()

        sentinel = "sentinel-system-info-output"
        mock_get = AsyncMock(return_value=sentinel)
        monkeypatch.setattr("mcp_synology.modules.system.info.get_system_info", mock_get)

        register(ctx)

        tool_fn = server._tool_manager._tools["get_system_info"].fn
        result = await tool_fn()

        manager.get_client.assert_awaited_once()
        mock_get.assert_awaited_once()
        assert result == sentinel

    async def test_get_resource_usage_invocation_calls_underlying(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, manager, ctx = _make_ctx()

        sentinel = "sentinel-resource-usage-output"
        mock_get = AsyncMock(return_value=sentinel)
        monkeypatch.setattr("mcp_synology.modules.system.utilization.get_resource_usage", mock_get)

        register(ctx)

        tool_fn = server._tool_manager._tools["get_resource_usage"].fn
        result = await tool_fn()

        manager.get_client.assert_awaited_once()
        mock_get.assert_awaited_once()
        assert result == sentinel
