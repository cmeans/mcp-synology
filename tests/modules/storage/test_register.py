"""Storage module registration, and each tool called through the server."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from mcp.server.mcpserver import MCPServer

from mcp_synology.modules import PermissionTier, RegisterContext
from mcp_synology.modules.storage import MODULE_INFO, register
from tests.modules.storage.dsm import REALNAS, Replay, make_client

TOOLS = {
    "get_storage_overview", "list_storage_pools", "list_volumes", "get_volume", "list_disks",
    "get_disk", "list_ssd_caches", "get_storage_schedules", "get_storage_settings",
    "list_external_storage", "list_snapshots",
}  # fmt: skip


def _server(allowed: set[str], client: object = None) -> MCPServer:
    server = MCPServer("test-storage")
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=client or MagicMock())
    register(RegisterContext(server, manager, allowed, {}, "test-nas"))
    return server


def test_module_info() -> None:
    assert {t.name for t in MODULE_INFO.tools} == TOOLS
    assert all(t.permission_tier is PermissionTier.READ for t in MODULE_INFO.tools)
    required = [r.api_name for r in MODULE_INFO.required_apis if not r.optional]
    assert "SYNO.Storage.CGI.Storage" in required
    assert all("administrator" in t.description for t in MODULE_INFO.tools)


async def test_registers_every_allowed_tool_read_only() -> None:
    tools = await _server(TOOLS).list_tools()
    assert {t.name for t in tools} == TOOLS
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)


async def test_registers_only_allowed_tools() -> None:
    tools = await _server({"list_disks", "get_disk"}).list_tools()
    assert {t.name for t in tools} == {"list_disks", "get_disk"}


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("get_storage_overview", {}, "Storage Overview"),
        ("list_storage_pools", {}, "Pool 1 (reuse_1)"),
        ("list_volumes", {}, "Volume 1 (/volume1)"),
        ("get_volume", {"volume": "/volume1"}, "Space by category"),
        ("list_disks", {}, "Drive 1 (sda)"),
        ("get_disk", {"disk": "sda", "history_limit": 20}, "S.M.A.R.T. attributes"),
        ("list_ssd_caches", {}, "Cache 1 (shared_cache_1, volume cache alloc_cache_1_1)"),
        ("get_storage_schedules", {}, "Data scrubbing"),
        ("get_storage_settings", {}, "RAID resync speed"),
        ("list_external_storage", {}, "USB Disk 1"),
        ("list_snapshots", {"share": "share04"}, "Snapshots of share04"),
    ],
)
async def test_tool_through_the_server(name: str, args: dict[str, object], expected: str) -> None:
    replay = Replay(REALNAS)
    with respx.mock:
        client = await make_client(replay)
        async with client:
            result = await _server(TOOLS, client).call_tool(name, args)
    assert expected in str(result)
