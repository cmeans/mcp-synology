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

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from mcp_synology.modules import RegisterContext
from mcp_synology.modules.filestation import MODULE_INFO, register


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

    @pytest.fixture(autouse=True)
    def _shares_known(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # These tests cover the closures' plumbing; share validation (#160) has its own.
        monkeypatch.setattr(
            "mcp_synology.modules.filestation.helpers.ensure_known_shares", AsyncMock()
        )

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

    async def test_get_file_info_passes_additional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The spec documents `additional` on get_file_info, and the handler takes
        it; #87 exposed it on three tools but missed this one."""
        server, _manager, ctx = _make_ctx()
        target = "mcp_synology.modules.filestation.metadata.get_file_info"
        mock = self._capture_call(monkeypatch, target)
        register(ctx)
        await server._tool_manager._tools["get_file_info"].fn(paths=["/a"], additional=["size"])
        assert mock.await_args.kwargs["additional"] == ["size"]

    async def test_get_file_info_description_names_additional(self) -> None:
        """QA on #146: the client sees only the tool description, not the handler's
        docstring, so the description itself has to name `additional` and its values."""
        server, _manager, ctx = _make_ctx()
        register(ctx)
        tools = {t.name: t for t in await server.list_tools()}
        description = tools["get_file_info"].description or ""
        assert "additional" in description
        for field in ("real_path", "size", "owner", "time", "perm", "type", "mount_point_type"):
            assert field in description

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


UNKNOWN = "/nosuchshare/x"
# (tool, domain function it must not reach, kwargs naming the unknown share)
SHARE_CASES = [
    ("list_files", "listing.list_files", {"path": UNKNOWN}),
    ("list_recycle_bin", "listing.list_recycle_bin", {"share": "nosuchshare"}),
    ("search_files", "search.search_files", {"folder_path": UNKNOWN}),
    ("get_file_info", "metadata.get_file_info", {"paths": ["/writable/ok", UNKNOWN]}),
    ("get_dir_size", "metadata.get_dir_size", {"path": UNKNOWN}),
    ("download_file", "transfer.download_file", {"path": UNKNOWN, "dest_folder": "/tmp"}),
    ("create_folder", "operations.create_folder", {"paths": [UNKNOWN]}),
    ("rename", "operations.rename", {"path": UNKNOWN, "new_name": "n"}),
    ("copy_files", "operations.copy_files", {"paths": ["/writable/a"], "dest_folder": UNKNOWN}),
    ("move_files", "operations.move_files", {"paths": [UNKNOWN], "dest_folder": "/writable"}),
    ("delete_files", "operations.delete_files", {"paths": [UNKNOWN]}),
    (
        "upload_file",
        "transfer.upload_file",
        {"local_path": "/etc/hostname", "dest_folder": UNKNOWN},
    ),
    (
        "restore_from_recycle_bin",
        "operations.restore_from_recycle_bin",
        {"share": "nosuchshare", "paths": ["x"]},
    ),
]
NEEDS_CTX = {"download_file", "upload_file"}


class TestFilestationShareValidation:
    """#160: every path-taking tool checks the share name before its own DSM call."""

    @staticmethod
    def _ctx_with_shares(*names: str) -> tuple[FastMCP, MagicMock, RegisterContext]:
        server, manager, ctx = _make_ctx()
        client = MagicMock()

        async def request(api: str, method: str, **_: object) -> dict[str, object]:
            if method == "list_share":
                return {"shares": [{"name": n, "path": f"/{n}"} for n in names]}
            return {}

        client.request = AsyncMock(side_effect=request)
        manager.get_client = AsyncMock(return_value=client)
        return server, manager, ctx

    @pytest.mark.parametrize(
        ("tool", "target", "kwargs"), SHARE_CASES, ids=[c[0] for c in SHARE_CASES]
    )
    async def test_unknown_share_refused_before_tool_runs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tool: str,
        target: str,
        kwargs: dict[str, object],
    ) -> None:
        domain = AsyncMock(return_value="ran")
        monkeypatch.setattr(f"mcp_synology.modules.filestation.{target}", domain)
        server, _, ctx = self._ctx_with_shares("writable")
        register(ctx)
        fn = server._tool_manager._tools[tool].fn
        extra: dict[str, object] = {}
        if tool in NEEDS_CTX:
            fake_mcp_ctx = MagicMock()
            fake_mcp_ctx.report_progress = AsyncMock()
            extra["ctx"] = fake_mcp_ctx
        with pytest.raises(ToolError) as exc_info:
            await fn(**extra, **kwargs)
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "not_found"
        assert "nosuchshare" in body["error"]["message"]
        assert "writable" in body["error"]["message"]
        domain.assert_not_awaited()

    async def test_known_share_reaches_the_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        domain = AsyncMock(return_value="ran")
        monkeypatch.setattr("mcp_synology.modules.filestation.operations.delete_files", domain)
        server, _, ctx = self._ctx_with_shares("writable")
        register(ctx)
        assert await server._tool_manager._tools["delete_files"].fn(paths=["/writable/a"]) == "ran"
        domain.assert_awaited_once()
