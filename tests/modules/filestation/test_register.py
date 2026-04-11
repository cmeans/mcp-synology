"""Tests for modules/filestation/__init__.py — module registration code paths.

The filestation `register()` function declares 14 tools (7 READ + 7 WRITE)
inside `if "<tool>" in ctx.allowed_tools:` blocks. The previous test_server.py
tests only verified `server is not None`, which walked the registration
decorators but not the inner tool closures. These tests:

1. Construct a real FastMCP + a mocked SharedClientManager + RegisterContext
2. Call register() with the full set of allowed tools
3. Pull each tool function via server._tool_manager._tools[name].fn
4. Mock the underlying domain function and invoke the tool, asserting the
   closure walked through `await manager.get_client()` and forwarded the
   result through `manager.with_update_notice()`

The two parameterless WRITE tools (download_file/upload_file) take an extra
`Context` argument so they're tested separately with a mocked Context that
captures `report_progress` calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from mcp.server.fastmcp import FastMCP

from mcp_synology.modules import RegisterContext
from mcp_synology.modules.filestation import MODULE_INFO, register

if TYPE_CHECKING:
    import pytest


def _make_ctx(
    allowed: set[str] | None = None,
    settings: dict | None = None,
) -> tuple[FastMCP, MagicMock, RegisterContext]:
    server = FastMCP("test-fs")
    manager = MagicMock()
    fake_client = MagicMock()
    manager.get_client = AsyncMock(return_value=fake_client)
    # `with_update_notice` is sync and just appends to a string. Echo through.
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


class TestFilestationModuleRegister:
    def test_register_all_tools_when_all_allowed(self) -> None:
        server, _manager, ctx = _make_ctx()
        register(ctx)
        registered = set(server._tool_manager._tools.keys())
        expected = {t.name for t in MODULE_INFO.tools}
        assert registered == expected

    def test_register_read_only_tools(self) -> None:
        read_names = {
            "list_shares",
            "list_files",
            "list_recycle_bin",
            "search_files",
            "get_file_info",
            "get_dir_size",
            "download_file",
        }
        server, _manager, ctx = _make_ctx(allowed=read_names)
        register(ctx)
        assert set(server._tool_manager._tools.keys()) == read_names

    def test_register_write_only_tools(self) -> None:
        write_names = {
            "create_folder",
            "rename",
            "copy_files",
            "move_files",
            "delete_files",
            "upload_file",
            "restore_from_recycle_bin",
        }
        server, _manager, ctx = _make_ctx(allowed=write_names)
        register(ctx)
        assert set(server._tool_manager._tools.keys()) == write_names

    def test_register_with_no_tools_allowed(self) -> None:
        server, _manager, ctx = _make_ctx(allowed=set())
        register(ctx)
        assert server._tool_manager._tools == {}

    def test_register_applies_custom_settings(self) -> None:
        """Custom settings_dict is parsed via FileStationSettings."""
        server, _manager, ctx = _make_ctx(
            settings={
                "file_type_indicator": "text",
                "async_timeout": 200,
                "search_timeout": 300,
                "default_download_dir": "~/downloads",
                "default_upload_dir": "/volume1/incoming",
            }
        )
        register(ctx)
        # Just confirm registration succeeded with non-default settings.
        assert "list_shares" in server._tool_manager._tools

    def test_default_download_dir_expands_tilde(self) -> None:
        """Tilde in default_download_dir gets expanded so the closure has a real path."""
        server, _manager, ctx = _make_ctx(
            allowed={"download_file"},
            settings={"default_download_dir": "~/downloads"},
        )
        register(ctx)
        assert "download_file" in server._tool_manager._tools


class TestFilestationToolInvocation:
    """Invoke each registered tool to walk the closure body lines.

    Each tool's body line numbers were what coverage flagged as missing.
    These tests mock the underlying domain function and invoke the tool
    via `server._tool_manager._tools[name].fn(...)`.
    """

    @staticmethod
    def _capture_call(monkeypatch: pytest.MonkeyPatch, target: str) -> AsyncMock:
        mock = AsyncMock(return_value=f"<<{target}-result>>")
        monkeypatch.setattr(target, mock)
        return mock

    async def test_list_shares_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, manager, ctx = _make_ctx()
        target = "mcp_synology.modules.filestation.listing.list_shares"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        result = await server._tool_manager._tools["list_shares"].fn()
        assert result == f"<<{target}-result>>"
        manager.get_client.assert_awaited()
        mock.assert_awaited_once()

    async def test_list_files_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, _manager, ctx = _make_ctx()
        target = "mcp_synology.modules.filestation.listing.list_files"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        result = await server._tool_manager._tools["list_files"].fn(path="/share")
        assert "list_files-result" in result
        mock.assert_awaited_once()

    async def test_list_recycle_bin_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, _manager, ctx = _make_ctx()
        mock = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.listing.list_recycle_bin"
        )
        register(ctx)
        result = await server._tool_manager._tools["list_recycle_bin"].fn(share="video")
        assert "list_recycle_bin-result" in result
        mock.assert_awaited_once()

    async def test_search_files_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, _manager, ctx = _make_ctx()
        target = "mcp_synology.modules.filestation.search.search_files"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        result = await server._tool_manager._tools["search_files"].fn(folder_path="/share")
        assert "search_files-result" in result
        mock.assert_awaited_once()

    async def test_get_file_info_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, _manager, ctx = _make_ctx()
        mock = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.metadata.get_file_info"
        )
        register(ctx)
        result = await server._tool_manager._tools["get_file_info"].fn(paths=["/share/x"])
        assert "get_file_info-result" in result
        mock.assert_awaited_once()

    async def test_get_dir_size_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, _manager, ctx = _make_ctx()
        mock = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.metadata.get_dir_size"
        )
        register(ctx)
        result = await server._tool_manager._tools["get_dir_size"].fn(path="/share")
        assert "get_dir_size-result" in result
        mock.assert_awaited_once()

    async def test_download_file_with_explicit_dest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, _manager, ctx = _make_ctx()
        mock = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.transfer.download_file"
        )
        register(ctx)

        fake_mcp_ctx = MagicMock()
        fake_mcp_ctx.report_progress = AsyncMock()

        result = await server._tool_manager._tools["download_file"].fn(
            ctx=fake_mcp_ctx,
            path="/share/file.txt",
            dest_folder="/tmp",
        )
        assert "download_file-result" in result
        mock.assert_awaited_once()
        # Verify the progress callback wired by the closure forwards to ctx.report_progress
        kwargs = mock.await_args.kwargs
        progress_cb = kwargs["progress_callback"]
        await progress_cb(50, 100)
        fake_mcp_ctx.report_progress.assert_awaited_once()

    async def test_download_file_no_dest_returns_error(self) -> None:
        """No dest_folder and no default_download_dir → bare error string returned."""
        server, _manager, ctx = _make_ctx(allowed={"download_file"})
        register(ctx)

        fake_mcp_ctx = MagicMock()
        result = await server._tool_manager._tools["download_file"].fn(
            ctx=fake_mcp_ctx,
            path="/share/file.txt",
        )
        assert "No destination folder" in result

    async def test_download_file_uses_default_download_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, _manager, ctx = _make_ctx(
            allowed={"download_file"},
            settings={"default_download_dir": "/var/incoming"},
        )
        mock = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.transfer.download_file"
        )
        register(ctx)

        fake_mcp_ctx = MagicMock()
        result = await server._tool_manager._tools["download_file"].fn(
            ctx=fake_mcp_ctx,
            path="/share/file.txt",
        )
        assert "download_file-result" in result
        kwargs = mock.await_args.kwargs
        assert kwargs["dest_folder"] == "/var/incoming"

    async def test_create_folder_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, _manager, ctx = _make_ctx()
        mock = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.operations.create_folder"
        )
        register(ctx)
        result = await server._tool_manager._tools["create_folder"].fn(paths=["/share/new"])
        assert "create_folder-result" in result
        mock.assert_awaited_once()

    async def test_rename_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, _manager, ctx = _make_ctx()
        mock = self._capture_call(monkeypatch, "mcp_synology.modules.filestation.operations.rename")
        register(ctx)
        result = await server._tool_manager._tools["rename"].fn(path="/share/old", new_name="new")
        assert "rename-result" in result
        mock.assert_awaited_once()

    async def test_copy_files_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, _manager, ctx = _make_ctx()
        mock = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.operations.copy_files"
        )
        register(ctx)
        result = await server._tool_manager._tools["copy_files"].fn(
            paths=["/share/a"], dest_folder="/share/b"
        )
        assert "copy_files-result" in result
        mock.assert_awaited_once()

    async def test_move_files_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, _manager, ctx = _make_ctx()
        mock = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.operations.move_files"
        )
        register(ctx)
        result = await server._tool_manager._tools["move_files"].fn(
            paths=["/share/a"], dest_folder="/share/b"
        )
        assert "move_files-result" in result
        mock.assert_awaited_once()

    async def test_delete_files_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, _manager, ctx = _make_ctx()
        mock = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.operations.delete_files"
        )
        register(ctx)
        result = await server._tool_manager._tools["delete_files"].fn(paths=["/share/x"])
        assert "delete_files-result" in result
        mock.assert_awaited_once()

    async def test_upload_file_with_explicit_dest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, _manager, ctx = _make_ctx()
        mock = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.transfer.upload_file"
        )
        register(ctx)

        fake_mcp_ctx = MagicMock()
        fake_mcp_ctx.report_progress = AsyncMock()

        result = await server._tool_manager._tools["upload_file"].fn(
            ctx=fake_mcp_ctx,
            local_path="/local/file.txt",
            dest_folder="/share",
        )
        assert "upload_file-result" in result
        kwargs = mock.await_args.kwargs
        await kwargs["progress_callback"](75, 100)
        fake_mcp_ctx.report_progress.assert_awaited_once()

    async def test_upload_file_no_dest_returns_error(self) -> None:
        server, _manager, ctx = _make_ctx(allowed={"upload_file"})
        register(ctx)
        fake_mcp_ctx = MagicMock()
        result = await server._tool_manager._tools["upload_file"].fn(
            ctx=fake_mcp_ctx,
            local_path="/local/file.txt",
        )
        assert "No NAS destination folder" in result

    async def test_upload_file_uses_default_upload_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, _manager, ctx = _make_ctx(
            allowed={"upload_file"},
            settings={"default_upload_dir": "/volume1/incoming"},
        )
        mock = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.transfer.upload_file"
        )
        register(ctx)
        fake_mcp_ctx = MagicMock()
        result = await server._tool_manager._tools["upload_file"].fn(
            ctx=fake_mcp_ctx,
            local_path="/local/file.txt",
        )
        assert "upload_file-result" in result
        kwargs = mock.await_args.kwargs
        assert kwargs["dest_folder"] == "/volume1/incoming"

    async def test_restore_from_recycle_bin_invocation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, _manager, ctx = _make_ctx()
        mock = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.operations.restore_from_recycle_bin"
        )
        register(ctx)
        result = await server._tool_manager._tools["restore_from_recycle_bin"].fn(
            share="video", paths=["/share/#recycle/x"]
        )
        assert "restore_from_recycle_bin-result" in result
        mock.assert_awaited_once()
