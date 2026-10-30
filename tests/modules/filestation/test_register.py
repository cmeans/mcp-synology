"""Tests for modules/filestation/__init__.py — module registration code paths.

The filestation `register()` function declares 15 tools (7 READ + 8 WRITE)
inside `if "<tool>" in ctx.allowed_tools:` blocks. The previous test_server.py
tests only verified `server is not None`, which walked the registration
decorators but not the inner tool closures. These tests:

1. Construct a real MCPServer + a mocked SharedClientManager + RegisterContext
2. Call register() with the full set of allowed tools
3. Pull each tool function via server._tool_manager._tools[name].fn
4. Mock the underlying domain function and invoke the tool, asserting the
   closure walked through `await manager.get_client()` and forwarded the
   result through the module's `_reply()`

The tools that report progress (download_file, upload_file, and since #245
copy_files, move_files, delete_files and restore_from_recycle_bin) take an extra
`Context` argument, passed here as a mocked Context that captures
`report_progress` calls.
"""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from mcp_synology.modules import RegisterContext
from mcp_synology.modules.filestation import MODULE_INFO, FileStationSettings, register

if TYPE_CHECKING:
    from pathlib import Path

    from mcp_synology.core.client import DsmClient


def _make_ctx(
    allowed: set[str] | None = None,
    settings: dict | None = None,
    shared_paths: list[str] | None = None,
) -> tuple[MCPServer, MagicMock, RegisterContext]:
    server = MCPServer("test-fs")
    manager = MagicMock()
    fake_client = MagicMock()
    manager.get_client = AsyncMock(return_value=fake_client)

    if allowed is None:
        allowed = {t.name for t in MODULE_INFO.tools}

    ctx = RegisterContext(
        server=server,
        manager=manager,
        allowed_tools=allowed,
        settings_dict=settings or {},
        display_name="test-nas",
        allowed_local_paths=shared_paths,
    )
    return server, manager, ctx


def _fake_mcp_ctx() -> MagicMock:
    fake = MagicMock()
    fake.report_progress = AsyncMock()
    return fake


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

    def test_download_file_is_not_annotated_read_only(self) -> None:
        """download_file writes (and with overwrite=True replaces) local files.

        It stays in the READ tier, since it only reads the NAS, but it must not
        claim readOnlyHint: clients may auto-approve tools that do.
        """
        server, _manager, ctx = _make_ctx(allowed={"download_file"})
        register(ctx)
        annos = server._tool_manager._tools["download_file"].annotations
        assert annos is not None
        assert annos.read_only_hint is False
        assert annos.destructive_hint is True

    def test_overwrite_capable_tools_are_destructive(self) -> None:
        """#183: with overwrite=true these replace NAS files, and annotations are static,
        so they can't depend on overwrite. Clients may auto-approve non-destructive tools."""
        server, _manager, ctx = _make_ctx()
        register(ctx)
        tools = server._tool_manager._tools
        for name in ("copy_files", "upload_file", "restore_from_recycle_bin"):
            annos = tools[name].annotations
            assert annos is not None, name
            assert annos.read_only_hint is False, name
            assert annos.destructive_hint is True, name

    def test_register_write_only_tools(self) -> None:
        write_names = {
            "create_folder",
            "rename",
            "copy_files",
            "move_files",
            "delete_files",
            "upload_file",
            "restore_from_recycle_bin",
            "file_task",
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
            "mcp_synology.modules.filestation.helpers.ensure_known_shares",
            AsyncMock(side_effect=lambda _client, paths, _cache: list(paths)),
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

    async def test_listing_tools_take_no_additional(self) -> None:
        """#149: list_shares, list_files and search_files accepted `additional` fields
        their tables never show. The parameter is gone; get_file_info keeps it."""
        server, _manager, ctx = _make_ctx()
        register(ctx)
        tools = {t.name: t for t in await server.list_tools()}
        for name in ("list_shares", "list_files", "search_files"):
            assert "additional" not in tools[name].input_schema["properties"], name
            assert "additional" not in (tools[name].description or ""), name
        assert "additional" in tools["get_file_info"].input_schema["properties"]

    async def test_get_file_info_description_names_additional(self) -> None:
        """QA on #146: the client sees only what's served, not the handler's docstring,
        so `additional`'s schema description has to name its values (#247 moved them
        there from the tool description)."""
        server, _manager, ctx = _make_ctx()
        register(ctx)
        tools = {t.name: t for t in await server.list_tools()}
        description = tools["get_file_info"].input_schema["properties"]["additional"]["description"]
        for field in ("real_path", "size", "owner", "time", "perm", "type", "mount_point_type"):
            assert field in description

    async def test_list_recycle_bin_says_its_search_is_capped(self) -> None:
        """#375: "the whole bin is searched" let a model say a file isn't in the bin after
        a search that stopped at the walk's caps. The description and `pattern` name them,
        from the constants, so the two can't drift apart."""
        from mcp_synology.modules.filestation.listing import (
            _WALK_MAX_ENTRIES,
            _WALK_MAX_FOLDERS,
        )

        server, _manager, ctx = _make_ctx()
        register(ctx)
        tool = {t.name: t for t in await server.list_tools()}["list_recycle_bin"]
        pattern = tool.input_schema["properties"]["pattern"]["description"]
        for text in (tool.description or "", pattern):
            assert f"{_WALK_MAX_FOLDERS} folders and {_WALK_MAX_ENTRIES:,} items" in text, text
            assert "whole bin" not in text

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

    async def test_download_file_no_dest_raises_structured_error(self) -> None:
        """No dest_folder and no default_download_dir → a structured error, not text (#194)."""
        server, manager, ctx = _make_ctx(allowed={"download_file"})
        register(ctx)

        fake_mcp_ctx = MagicMock()
        with pytest.raises(ToolError) as exc:
            await server._tool_manager._tools["download_file"].fn(
                ctx=fake_mcp_ctx,
                path="/share/file.txt",
            )
        err = json.loads(str(exc.value))["error"]
        assert err["code"] == "invalid_parameter"
        assert err["param"] == "dest_folder"
        assert err["retryable"] is False
        assert "No destination folder" in err["message"]
        assert "default_download_dir" in err["suggestion"]
        manager.get_client.assert_not_called()

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

    async def test_transfers_get_the_configured_allowed_roots(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """allowed_local_paths reaches both transfer tools, resolved, plus
        default_download_dir, which is always allowed."""
        server, _manager, ctx = _make_ctx(
            allowed={"download_file", "upload_file"},
            settings={
                "allowed_local_paths": [str(tmp_path / "a")],
                "default_download_dir": str(tmp_path / "dl"),
            },
        )
        down = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.transfer.download_file"
        )
        up = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.transfer.upload_file"
        )
        register(ctx)
        tools = server._tool_manager._tools
        fake_mcp_ctx = MagicMock()
        fake_mcp_ctx.report_progress = AsyncMock()

        await tools["download_file"].fn(ctx=fake_mcp_ctx, path="/share/f.txt")
        await tools["upload_file"].fn(
            ctx=fake_mcp_ctx, local_path=str(tmp_path / "a" / "f"), dest_folder="/share"
        )

        expected = [(tmp_path / "a").resolve(), (tmp_path / "dl").resolve()]
        assert down.await_args.kwargs["allowed_roots"] == expected
        assert up.await_args.kwargs["allowed_roots"] == expected

    async def test_transfers_default_to_the_downloads_folder(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "mcp_synology.core.local_paths.default_downloads_dir",
            lambda: tmp_path / "Downloads",
        )
        server, _manager, ctx = _make_ctx(allowed={"download_file"})
        down = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.transfer.download_file"
        )
        register(ctx)
        fake_mcp_ctx = MagicMock()
        await server._tool_manager._tools["download_file"].fn(
            ctx=fake_mcp_ctx, path="/share/f.txt", dest_folder=str(tmp_path)
        )
        assert down.await_args.kwargs["allowed_roots"] == [(tmp_path / "Downloads").resolve()]

    async def test_transfers_use_the_shared_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """#257: the top-level list replaces the Downloads default and adds to the module's."""
        monkeypatch.setattr(
            "mcp_synology.core.local_paths.default_downloads_dir",
            lambda: tmp_path / "Downloads",
        )
        for settings, expected in (
            ({}, [(tmp_path / "shared").resolve()]),
            (
                {"allowed_local_paths": [str(tmp_path / "own")]},
                [(tmp_path / "shared").resolve(), (tmp_path / "own").resolve()],
            ),
        ):
            server, _manager, ctx = _make_ctx(
                allowed={"download_file"},
                settings=settings,
                shared_paths=[str(tmp_path / "shared")],
            )
            down = self._capture_call(
                monkeypatch, "mcp_synology.modules.filestation.transfer.download_file"
            )
            register(ctx)
            await server._tool_manager._tools["download_file"].fn(
                ctx=MagicMock(), path="/share/f.txt", dest_folder=str(tmp_path)
            )
            assert down.await_args.kwargs["allowed_roots"] == expected

    def test_empty_allowed_local_paths_is_rejected(self) -> None:
        """An empty list would refuse every transfer; that's a config mistake, not a policy."""
        with pytest.raises(ValidationError):
            FileStationSettings(allowed_local_paths=[])

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
            ctx=_fake_mcp_ctx(), paths=["/share/a"], dest_folder="/share/b"
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
            ctx=_fake_mcp_ctx(), paths=["/share/a"], dest_folder="/share/b"
        )
        assert "move_files-result" in result
        mock.assert_awaited_once()

    async def test_delete_files_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, _manager, ctx = _make_ctx()
        mock = self._capture_call(
            monkeypatch, "mcp_synology.modules.filestation.operations.delete_files"
        )
        register(ctx)
        result = await server._tool_manager._tools["delete_files"].fn(
            ctx=_fake_mcp_ctx(), paths=["/share/x"]
        )
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

    async def test_upload_file_no_dest_raises_structured_error(self) -> None:
        server, manager, ctx = _make_ctx(allowed={"upload_file"})
        register(ctx)
        fake_mcp_ctx = MagicMock()
        with pytest.raises(ToolError) as exc:
            await server._tool_manager._tools["upload_file"].fn(
                ctx=fake_mcp_ctx,
                local_path="/local/file.txt",
            )
        err = json.loads(str(exc.value))["error"]
        assert err["code"] == "invalid_parameter"
        assert err["param"] == "dest_folder"
        assert err["retryable"] is False
        assert "No NAS destination folder" in err["message"]
        assert "default_upload_dir" in err["suggestion"]
        manager.get_client.assert_not_called()

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
            ctx=_fake_mcp_ctx(), share="video", paths=["/share/#recycle/x"]
        )
        assert "restore_from_recycle_bin-result" in result
        mock.assert_awaited_once()


# (tool, domain function, tool kwargs, the kwargs the domain function must get)
LISTED_CASES = [
    (
        "copy_files",
        "operations.copy_files",
        {"paths": ["/Video/a", "/Video/b"], "dest_folder": "/Video/c"},
        {"paths": ["/video/a", "/video/b"], "dest_folder": "/video/c"},
    ),
    (
        "move_files",
        "operations.move_files",
        {"paths": ["/Video/a"], "dest_folder": "/Home/c"},
        {"paths": ["/video/a"], "dest_folder": "/home/c"},
    ),
    (
        "restore_from_recycle_bin",
        "operations.restore_from_recycle_bin",
        {"share": "Home", "paths": ["x"], "dest_folder": "/Video/c"},
        {"share": "/home", "paths": ["x"], "dest_folder": "/video/c"},
    ),
    (
        "restore_from_recycle_bin",
        "operations.restore_from_recycle_bin",
        {"share": "Home", "paths": ["x"]},
        {"share": "/home", "paths": ["x"], "dest_folder": None},
    ),
    (
        "upload_file",
        "transfer.upload_file",
        {"local_path": "/tmp/f", "dest_folder": "/Home/up"},
        {"dest_folder": "/home/up"},
    ),
    ("list_recycle_bin", "listing.list_recycle_bin", {"share": "Home"}, {"share": "/home"}),
    ("delete_files", "operations.delete_files", {"paths": ["/Home/x"]}, {"paths": ["/home/x"]}),
    ("list_files", "listing.list_files", {"path": "/Home"}, {"path": "/home"}),
    ("search_files", "search.search_files", {"folder_path": "/Home"}, {"folder_path": "/home"}),
    (
        "get_file_info",
        "metadata.get_file_info",
        {"paths": ["/Video/a", "/Home/b"]},
        {"paths": ["/video/a", "/home/b"]},
    ),
    ("get_dir_size", "metadata.get_dir_size", {"path": "/Home/d"}, {"path": "/home/d"}),
    (
        "download_file",
        "transfer.download_file",
        {"path": "/Home/f", "dest_folder": "/tmp"},
        {"path": "/home/f"},
    ),
    (
        "create_folder",
        "operations.create_folder",
        {"paths": ["/Home/n", "/Video/m"]},
        {"paths": ["/home/n", "/video/m"]},
    ),
    ("rename", "operations.rename", {"path": "/Home/x", "new_name": "y"}, {"path": "/home/x"}),
]


@pytest.mark.parametrize(("tool", "target", "kwargs", "expected"), LISTED_CASES)
async def test_tools_send_the_share_name_as_listed(
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    target: str,
    kwargs: dict[str, object],
    expected: dict[str, object],
) -> None:
    """#353: every handler hands on the paths ensure_known_shares returns, in order."""
    from mcp_synology.modules.filestation.helpers import _with_listed_share_name

    async def listed(_client: object, paths: list[str], _cache: set[str]) -> list[str]:
        return [_with_listed_share_name(p, {"video", "home"}) for p in paths]

    monkeypatch.setattr("mcp_synology.modules.filestation.helpers.ensure_known_shares", listed)
    mock = AsyncMock(return_value="ok")
    monkeypatch.setattr(f"mcp_synology.modules.filestation.{target}", mock)
    server, _manager, ctx = _make_ctx(shared_paths=["/tmp"])
    register(ctx)
    fn = server._tool_manager._tools[tool].fn
    params = inspect.signature(fn).parameters
    await fn(**({"ctx": _fake_mcp_ctx()} if "ctx" in params else {}), **kwargs)
    got = mock.await_args.kwargs
    assert {k: got[k] for k in expected} == expected


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
NEEDS_CTX = {
    "download_file",
    "upload_file",
    "copy_files",
    "move_files",
    "delete_files",
    "restore_from_recycle_bin",
}


class TestFilestationShareValidation:
    """#160: every path-taking tool checks the share name before its own DSM call."""

    @staticmethod
    def _ctx_with_shares(*names: str) -> tuple[MCPServer, MagicMock, RegisterContext]:
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
        fn = server._tool_manager._tools["delete_files"].fn
        assert await fn(ctx=_fake_mcp_ctx(), paths=["/writable/a"]) == "ran"
        domain.assert_awaited_once()


WAIT_TOOLS = {
    "delete_files": ("operations.delete_files", {"paths": ["/s/x"]}),
    "copy_files": ("operations.copy_files", {"paths": ["/s/x"], "dest_folder": "/s/d"}),
    "move_files": ("operations.move_files", {"paths": ["/s/x"], "dest_folder": "/s/d"}),
    "restore_from_recycle_bin": (
        "operations.restore_from_recycle_bin",
        {"share": "s", "paths": ["x"]},
    ),
}


class TestFileTaskAndHandoff:
    """#245: file_task registration, the capped handoff wait, progress, and discovery."""

    @pytest.fixture(autouse=True)
    def _shares_known(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "mcp_synology.modules.filestation.helpers.ensure_known_shares",
            AsyncMock(side_effect=lambda _client, paths, _cache: list(paths)),
        )

    def test_file_task_registered_write_only(self) -> None:
        server, _manager, ctx = _make_ctx()
        register(ctx)
        tool = server._tool_manager._tools["file_task"]
        info = next(t for t in MODULE_INFO.tools if t.name == "file_task")
        assert info.permission_tier.name == "WRITE"
        annos = tool.annotations
        assert annos is not None
        assert annos.read_only_hint is False
        # Worst case (#183): action="stop" can leave a move half done (QA R1 on #251).
        assert annos.destructive_hint is True
        assert annos.idempotent_hint is not True

        read_names = {t.name for t in MODULE_INFO.tools if t.permission_tier.name == "READ"}
        server, _manager, ctx = _make_ctx(allowed=read_names)
        register(ctx)
        assert "file_task" not in server._tool_manager._tools

    async def test_file_task_schema_has_parameter_descriptions(self) -> None:
        server, _manager, ctx = _make_ctx(allowed={"file_task"})
        register(ctx)
        (tool,) = await server.list_tools()
        props = tool.input_schema["properties"]
        assert set(props) == {"handle", "action"}
        assert "Omit it to list running operations" in props["handle"]["description"]
        assert props["action"]["enum"] == ["status", "stop"]
        assert "stop" in props["action"]["description"]

    async def test_file_task_invocation_shares_records(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp_synology.modules.filestation.tasks import TaskRecords

        mock = AsyncMock(return_value="<<file_task>>")
        monkeypatch.setattr("mcp_synology.modules.filestation.tasks.file_task", mock)
        server, _manager, ctx = _make_ctx()
        register(ctx)
        fn = server._tool_manager._tools["file_task"].fn
        assert await fn(handle="delete:X", action="stop") == "<<file_task>>"
        await fn()
        first, second = mock.await_args_list
        assert first.kwargs["handle"] == "delete:X"
        assert first.kwargs["action"] == "stop"
        assert second.kwargs["handle"] is None
        assert second.kwargs["action"] == "status"
        assert isinstance(first.kwargs["records"], TaskRecords)
        assert first.kwargs["records"] is second.kwargs["records"]

    async def test_operations_get_the_same_records(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mocks = {}
        for name, (target, _) in WAIT_TOOLS.items():
            mocks[name] = AsyncMock(return_value="ok")
            monkeypatch.setattr(f"mcp_synology.modules.filestation.{target}", mocks[name])
        file_task_mock = AsyncMock(return_value="ok")
        monkeypatch.setattr("mcp_synology.modules.filestation.tasks.file_task", file_task_mock)
        server, _manager, ctx = _make_ctx()
        register(ctx)
        for name, (_, kwargs) in WAIT_TOOLS.items():
            await server._tool_manager._tools[name].fn(ctx=_fake_mcp_ctx(), **kwargs)
        await server._tool_manager._tools["file_task"].fn()
        records = file_task_mock.await_args.kwargs["records"]
        for name, mock in mocks.items():
            assert mock.await_args.kwargs["records"] is records, name

    @pytest.mark.parametrize(
        ("settings", "expected"),
        [
            ({}, {name: 30.0 for name in WAIT_TOOLS}),
            (
                {"delete_timeout": 3600, "copy_move_timeout": 3600},
                {name: 600.0 for name in WAIT_TOOLS},
            ),
            (
                {"delete_timeout": 45, "copy_move_timeout": 90},
                {
                    "delete_files": 45.0,
                    "copy_files": 90.0,
                    "move_files": 90.0,
                    "restore_from_recycle_bin": 90.0,
                },
            ),
            ({"async_timeout": 300}, {name: 30.0 for name in WAIT_TOOLS}),
        ],
        ids=["unset", "capped", "per-operation", "async-timeout-ignored"],
    )
    async def test_handoff_wait_capped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: dict[str, int],
        expected: dict[str, float],
    ) -> None:
        mocks = {}
        for name, (target, _) in WAIT_TOOLS.items():
            mocks[name] = AsyncMock(return_value="ok")
            monkeypatch.setattr(f"mcp_synology.modules.filestation.{target}", mocks[name])
        search = AsyncMock(return_value="ok")
        monkeypatch.setattr("mcp_synology.modules.filestation.search.search_files", search)
        server, _manager, ctx = _make_ctx(settings=settings)
        register(ctx)
        for name, (_, kwargs) in WAIT_TOOLS.items():
            await server._tool_manager._tools[name].fn(ctx=_fake_mcp_ctx(), **kwargs)
            assert mocks[name].await_args.kwargs["timeout"] == expected[name], name
        await server._tool_manager._tools["search_files"].fn(folder_path="/s")
        assert search.await_args.kwargs["timeout"] == float(settings.get("async_timeout", 120))

    @pytest.mark.parametrize("name", sorted(WAIT_TOOLS))
    async def test_handler_reports_progress(
        self, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        target, kwargs = WAIT_TOOLS[name]
        mock = AsyncMock(return_value="ok")
        monkeypatch.setattr(f"mcp_synology.modules.filestation.{target}", mock)
        server, _manager, ctx = _make_ctx()
        register(ctx)
        fake = _fake_mcp_ctx()
        await server._tool_manager._tools[name].fn(ctx=fake, **kwargs)
        on_progress = mock.await_args.kwargs["on_progress"]
        await on_progress(5.0, 10.0, "Deleting /s/x: 5 of 10 items")
        await on_progress(0.0, None, "Deleting /s/x: counting items")
        assert [c.args for c in fake.report_progress.await_args_list] == [
            (5.0, 10.0, "Deleting /s/x: 5 of 10 items"),
            (0.0, None, "Deleting /s/x: counting items"),
        ]


RUNNING_DELETE = {
    "api": "SYNO.FileStation.Delete",
    "crtime": 1760000000,
    "finished": False,
    "params": {"accurate_progress": True, "path": ["/s/big"], "recursive": True},
    "processed_num": 8,
    "progress": 0.02,
    "taskid": "FileStation_D1",
    "total": 342,
}
RUNNING_DELETE_2 = {
    **RUNNING_DELETE,
    "params": {"accurate_progress": True, "path": ["/s/two"], "recursive": True},
    "taskid": "FileStation_D2",
}


class TestDiscoveryAtFirstConnection:
    """#245: the first File Station reply of a server process tells the model about
    operations still running on the NAS; discovery never breaks a tool call."""

    @staticmethod
    def _wire(manager: MagicMock, client: object, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        manager.get_client = AsyncMock(return_value=client)
        shares = AsyncMock(return_value="SHARES")
        monkeypatch.setattr("mcp_synology.modules.filestation.listing.list_shares", shares)
        return shares

    @respx.mock
    async def test_discovery_note_on_first_reply_only(
        self, monkeypatch: pytest.MonkeyPatch, mock_client: DsmClient
    ) -> None:
        from tests.conftest import BASE_URL

        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            return_value=httpx.Response(
                200, json={"success": True, "data": {"tasks": [RUNNING_DELETE], "total": 1}}
            )
        )
        server, manager, ctx = _make_ctx()
        self._wire(manager, mock_client, monkeypatch)
        register(ctx)
        fn = server._tool_manager._tools["list_shares"].fn
        first = await fn()
        assert first.startswith("SHARES\n\n---\nNote:")
        assert first.endswith("Use file_task to check on them or stop them.")
        assert "deleting /s/big (8 of 342 items, 2%), handle `delete:FileStation_D1`" in first
        assert await fn() == "SHARES"
        assert route.call_count == 1
        params = dict(route.calls.last.request.url.params)
        assert (params["api"], params["method"]) == ("SYNO.FileStation.BackgroundTask", "list")

    @respx.mock
    @pytest.mark.parametrize(
        ("tasks", "kwargs", "first"),
        [
            # Only the task the reply is about: no note, it would repeat that task.
            ([RUNNING_DELETE], {"handle": "delete:FileStation_D1"}, "STATUS"),
            # Others too (QA round 3): they're still named, minus the one asked about.
            (
                [RUNNING_DELETE, RUNNING_DELETE_2],
                {"handle": " `delete:FileStation_D1`. "},
                "STATUS\n\n---\nNote: 1 operation(s) still running on the NAS:\n"
                "  deleting /s/two (8 of 342 items, 2%), handle `delete:FileStation_D2`\n"
                "Use file_task to check on them or stop them.",
            ),
            # The list: it shows them all already.
            ([RUNNING_DELETE, RUNNING_DELETE_2], {}, "STATUS"),
        ],
        ids=["only-that-task", "other-tasks", "list"],
    )
    async def test_file_task_reply_and_the_discovery_note(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_client: DsmClient,
        tasks: list[dict[str, object]],
        kwargs: dict[str, str],
        first: str,
    ) -> None:
        """file_task first: the note leaves out the task the reply is about; either way
        it's used up, so later replies don't carry it."""
        from tests.conftest import BASE_URL

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            return_value=httpx.Response(
                200, json={"success": True, "data": {"tasks": tasks, "total": len(tasks)}}
            )
        )
        monkeypatch.setattr(
            "mcp_synology.modules.filestation.tasks.file_task", AsyncMock(return_value="STATUS")
        )
        server, manager, ctx = _make_ctx()
        self._wire(manager, mock_client, monkeypatch)
        register(ctx)
        assert await server._tool_manager._tools["file_task"].fn(**kwargs) == first
        assert await server._tool_manager._tools["list_shares"].fn() == "SHARES"

    @respx.mock
    async def test_discovery_seeds_file_task_records(
        self, monkeypatch: pytest.MonkeyPatch, mock_client: DsmClient
    ) -> None:
        from tests.conftest import BASE_URL

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            return_value=httpx.Response(
                200, json={"success": True, "data": {"tasks": [RUNNING_DELETE], "total": 1}}
            )
        )
        file_task_mock = AsyncMock(return_value="ok")
        monkeypatch.setattr("mcp_synology.modules.filestation.tasks.file_task", file_task_mock)
        server, manager, ctx = _make_ctx()
        manager.get_client = AsyncMock(return_value=mock_client)
        register(ctx)
        await server._tool_manager._tools["file_task"].fn()
        from mcp_synology.modules.filestation.tasks import Handle

        records = file_task_mock.await_args.kwargs["records"]
        record = records.get(Handle("delete", "FileStation_D1"))
        assert record is not None
        assert record.paths == ["/s/big"]

    @respx.mock
    async def test_discovery_failure_does_not_break_tool(
        self, monkeypatch: pytest.MonkeyPatch, mock_client: DsmClient
    ) -> None:
        from tests.conftest import BASE_URL

        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            return_value=httpx.Response(500, text="oops")
        )
        server, manager, ctx = _make_ctx()
        self._wire(manager, mock_client, monkeypatch)
        register(ctx)
        fn = server._tool_manager._tools["list_shares"].fn
        assert await fn() == "SHARES"
        assert await fn() == "SHARES"
        assert route.call_count == 1

    async def test_discovery_unexpected_error_does_not_break_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        boom = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr("mcp_synology.modules.filestation.tasks.discover_running_tasks", boom)
        server, manager, ctx = _make_ctx()
        self._wire(manager, MagicMock(), monkeypatch)
        register(ctx)
        fn = server._tool_manager._tools["list_shares"].fn
        assert await fn() == "SHARES"
        assert await fn() == "SHARES"
        boom.assert_awaited_once()

    async def test_no_discovery_without_file_task(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A read-only connection can't follow up on a task, so it isn't told about one."""
        discover = AsyncMock(return_value="Note: something")
        monkeypatch.setattr(
            "mcp_synology.modules.filestation.tasks.discover_running_tasks", discover
        )
        server, manager, ctx = _make_ctx(allowed={"list_shares"})
        self._wire(manager, MagicMock(), monkeypatch)
        register(ctx)
        assert await server._tool_manager._tools["list_shares"].fn() == "SHARES"
        discover.assert_not_awaited()

    async def test_discovery_waits_for_a_successful_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed login doesn't use up the one discovery."""
        discover = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "mcp_synology.modules.filestation.tasks.discover_running_tasks", discover
        )
        server, manager, ctx = _make_ctx()
        self._wire(manager, MagicMock(), monkeypatch)
        manager.get_client = AsyncMock(side_effect=[RuntimeError("login failed"), MagicMock()])
        register(ctx)
        fn = server._tool_manager._tools["list_shares"].fn
        with pytest.raises(RuntimeError):
            await fn()
        discover.assert_not_awaited()
        assert await fn() == "SHARES"
        discover.assert_awaited_once()


class TestShareRootDelete:
    """#451: a shared folder itself means its contents, deleted only after a preview the
    user confirms: through elicitation, or with a one-time token."""

    PREVIEW = "This would delete everything in these shared folders"

    @staticmethod
    def _setup(
        monkeypatch: pytest.MonkeyPatch,
        contents: list[str] | None = None,
        missing: list[tuple[str, int]] | None = None,
    ) -> tuple[AsyncMock, AsyncMock, object]:
        monkeypatch.setattr(
            "mcp_synology.modules.filestation.helpers.ensure_known_shares",
            AsyncMock(side_effect=lambda _c, paths, _k, *_a, **_kw: list(paths)),
        )
        preview = AsyncMock(
            return_value=TestShareRootDelete.PREVIEW + ":\n  /writable: 2 file(s), 1 KiB"
        )
        monkeypatch.setattr(
            "mcp_synology.modules.filestation.operations.share_root_preview", preview
        )
        monkeypatch.setattr(
            "mcp_synology.modules.filestation.operations.share_contents",
            AsyncMock(
                return_value=["/writable/a", "/writable/b"] if contents is None else contents
            ),
        )
        monkeypatch.setattr(
            "mcp_synology.modules.filestation.helpers.find_missing_paths",
            AsyncMock(return_value=missing or []),
        )
        delete = AsyncMock(return_value="[+] Deleted")
        monkeypatch.setattr("mcp_synology.modules.filestation.operations.delete_files", delete)
        server, _, ctx = _make_ctx()
        register(ctx)
        return delete, preview, server._tool_manager._tools["delete_files"].fn

    @staticmethod
    def _mcp_ctx(elicits: bool, answer: object = None) -> MagicMock:
        fake = _fake_mcp_ctx()
        fake.request_context.session.check_client_capability = MagicMock(return_value=elicits)
        fake.elicit = AsyncMock(return_value=answer)
        return fake

    async def test_elicitation_accepted_deletes_the_contents(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp.server.elicitation import AcceptedElicitation

        from mcp_synology.modules.filestation import _ConfirmShareDelete

        delete, _, fn = self._setup(monkeypatch)
        mcp_ctx = self._mcp_ctx(True, AcceptedElicitation(data=_ConfirmShareDelete(delete=True)))
        out = await fn(ctx=mcp_ctx, paths=["/writable", "/other/x"])
        assert out.startswith("[+] Deleted")
        message = mcp_ctx.elicit.await_args.kwargs["message"]
        assert message.startswith(self.PREVIEW) and message.endswith("Delete all of it?")
        assert delete.await_args.kwargs["paths"] == ["/writable/a", "/writable/b", "/other/x"]

    @pytest.mark.parametrize("answer", ["decline", "cancel", "no"])
    async def test_elicitation_not_accepted_deletes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, answer: str
    ) -> None:
        from mcp.server.elicitation import (
            AcceptedElicitation,
            CancelledElicitation,
            DeclinedElicitation,
        )

        from mcp_synology.modules.filestation import _ConfirmShareDelete

        result = {
            "decline": DeclinedElicitation(),
            "cancel": CancelledElicitation(),
            "no": AcceptedElicitation(data=_ConfirmShareDelete(delete=False)),
        }[answer]
        delete, _, fn = self._setup(monkeypatch)
        out = await fn(ctx=self._mcp_ctx(True, result), paths=["/writable"])
        assert out.endswith("Nothing deleted: the user didn't confirm.")
        delete.assert_not_awaited()

    async def test_without_elicitation_a_token_then_the_delete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import re

        delete, preview, fn = self._setup(monkeypatch)
        first = await fn(ctx=self._mcp_ctx(False), paths=["/writable"])
        delete.assert_not_awaited()
        assert first.startswith(self.PREVIEW)
        token = re.search(r'confirm="([0-9a-f]{8})"', first)
        assert token is not None
        assert "good once, for 5 minutes" in first
        out = await fn(ctx=self._mcp_ctx(False), paths=["/writable"], confirm=token.group(1))
        assert out.startswith("[+] Deleted")
        assert delete.await_args.kwargs["paths"] == ["/writable/a", "/writable/b"]
        assert preview.await_count == 1  # no second preview on the confirmed call
        # Good once.
        with pytest.raises(ToolError, match="confirm token isn't valid"):
            await fn(ctx=self._mcp_ctx(False), paths=["/writable"], confirm=token.group(1))

    @pytest.mark.parametrize(
        ("confirm_paths", "token_case"),
        [(["/other"], "other paths"), (["/writable"], "made up")],
    )
    async def test_a_token_only_for_its_preview(
        self, monkeypatch: pytest.MonkeyPatch, confirm_paths: list[str], token_case: str
    ) -> None:
        import re

        delete, _, fn = self._setup(monkeypatch)
        first = await fn(ctx=self._mcp_ctx(False), paths=["/writable"])
        token = re.search(r'confirm="([0-9a-f]{8})"', first)
        assert token is not None
        value = token.group(1) if token_case == "other paths" else "deadbeef"
        with pytest.raises(ToolError) as exc:
            await fn(ctx=self._mcp_ctx(False), paths=confirm_paths, confirm=value)
        error = json.loads(str(exc.value))["error"]
        assert (error["code"], error["param"]) == ("invalid_parameter", "confirm")
        delete.assert_not_awaited()

    async def test_an_expired_token_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import re

        delete, _, fn = self._setup(monkeypatch)
        first = await fn(ctx=self._mcp_ctx(False), paths=["/writable"])
        token = re.search(r'confirm="([0-9a-f]{8})"', first)
        assert token is not None
        monkeypatch.setattr("mcp_synology.modules.filestation.operations.SHARE_ROOT_TOKEN_TTL", 0)
        import time as time_module

        real = time_module.monotonic
        monkeypatch.setattr(
            "mcp_synology.modules.filestation.operations.time.monotonic", lambda: real() + 301
        )
        with pytest.raises(ToolError, match="confirm token isn't valid"):
            await fn(ctx=self._mcp_ctx(False), paths=["/writable"], confirm=token.group(1))
        delete.assert_not_awaited()

    async def test_a_token_covers_the_whole_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import re

        delete, _, fn = self._setup(monkeypatch)
        first = await fn(ctx=self._mcp_ctx(False), paths=["/writable"])
        token = re.search(r'confirm="([0-9a-f]{8})"', first)
        assert token is not None
        with pytest.raises(ToolError) as exc:
            await fn(
                ctx=self._mcp_ctx(False), paths=["/writable", "/other/x"], confirm=token.group(1)
            )
        assert json.loads(str(exc.value))["error"]["param"] == "confirm"
        delete.assert_not_awaited()

    async def test_other_paths_in_the_call_are_in_the_preview(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, _, fn = self._setup(monkeypatch)
        out = await fn(ctx=self._mcp_ctx(False), paths=["/writable", "/other/x"])
        assert "\nAlso deleted in this call:\n  /other/x\n" in out

    async def test_a_blank_confirm_is_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        delete, _, fn = self._setup(monkeypatch)
        out = await fn(ctx=self._mcp_ctx(False), paths=["/writable/a"], confirm=" ")
        assert out.startswith("[+] Deleted")
        delete.assert_awaited_once()

    async def test_not_recursive_on_a_share_root_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        delete, preview, fn = self._setup(monkeypatch)
        with pytest.raises(ToolError) as exc:
            await fn(ctx=self._mcp_ctx(False), paths=["/writable"], recursive=False)
        assert json.loads(str(exc.value))["error"]["param"] == "recursive"
        preview.assert_not_awaited()
        delete.assert_not_awaited()

    async def test_a_confirmed_delete_has_a_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mcp.server.elicitation import AcceptedElicitation

        from mcp_synology.modules.filestation import _ConfirmShareDelete

        delete, _, fn = self._setup(monkeypatch)
        mcp_ctx = self._mcp_ctx(True, AcceptedElicitation(data=_ConfirmShareDelete(delete=True)))
        await fn(ctx=mcp_ctx, paths=["/writable", "/other/x"])
        kwargs = delete.await_args.kwargs
        assert kwargs["deadline"] is not None
        assert kwargs["listed"] == frozenset({"/writable/a", "/writable/b"})  # not /other/x

    async def test_confirm_without_a_share_root_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        delete, _, fn = self._setup(monkeypatch)
        with pytest.raises(ToolError) as exc:
            await fn(ctx=self._mcp_ctx(False), paths=["/writable/a"], confirm="deadbeef")
        assert json.loads(str(exc.value))["error"]["param"] == "confirm"
        delete.assert_not_awaited()

    async def test_an_empty_share_deletes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mcp.server.elicitation import AcceptedElicitation

        from mcp_synology.modules.filestation import _ConfirmShareDelete

        delete, _, fn = self._setup(monkeypatch, contents=[])
        mcp_ctx = self._mcp_ctx(True, AcceptedElicitation(data=_ConfirmShareDelete(delete=True)))
        out = await fn(ctx=mcp_ctx, paths=["/writable"])
        assert out == "Nothing to delete: /writable has nothing in it."
        delete.assert_not_awaited()
        mcp_ctx.elicit.assert_not_awaited()  # nothing to confirm (pre-QA on #451)

    async def test_two_empty_shares_have_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        delete, preview, fn = self._setup(monkeypatch, contents=[])
        out = await fn(ctx=self._mcp_ctx(False), paths=["/writable", "/other"])
        assert out == "Nothing to delete: /writable, /other have nothing in it."
        preview.assert_not_awaited()
        delete.assert_not_awaited()

    async def test_a_missing_other_path_is_refused_before_asking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mistyped path beside the share is refused before the preview, so the user
        isn't asked about a call that would then fail (pre-QA on #451)."""
        delete, preview, fn = self._setup(monkeypatch, missing=[("/other/typo", 408)])
        mcp_ctx = self._mcp_ctx(True)
        with pytest.raises(ToolError) as exc:
            await fn(ctx=mcp_ctx, paths=["/writable", "/other/typo"])
        error = json.loads(str(exc.value))["error"]
        assert error["code"] == "not_found" and "/other/typo (DSM error 408)" in error["message"]
        preview.assert_not_awaited()
        mcp_ctx.elicit.assert_not_awaited()
        delete.assert_not_awaited()

    async def test_the_share_root_path_is_never_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mcp.server.elicitation import AcceptedElicitation

        from mcp_synology.modules.filestation import _ConfirmShareDelete

        delete, _, fn = self._setup(monkeypatch)
        mcp_ctx = self._mcp_ctx(True, AcceptedElicitation(data=_ConfirmShareDelete(delete=True)))
        await fn(ctx=mcp_ctx, paths=["writable/", " /writable ", "/x/a"])
        sent = delete.await_args.kwargs["paths"]
        assert sent == ["/writable/a", "/writable/b", "/x/a"]


class TestShareRootDeleteOverMcp:
    """#451 through a real MCP client: one that declares elicitation is asked; one that
    doesn't gets the token preview."""

    @staticmethod
    def _server(monkeypatch: pytest.MonkeyPatch) -> tuple[MCPServer, AsyncMock]:
        monkeypatch.setattr(
            "mcp_synology.modules.filestation.helpers.ensure_known_shares",
            AsyncMock(side_effect=lambda _c, paths, _k, *_a, **_kw: list(paths)),
        )
        monkeypatch.setattr(
            "mcp_synology.modules.filestation.operations.share_root_preview",
            AsyncMock(return_value="This would delete everything:\n  /writable: 1 file(s)"),
        )
        monkeypatch.setattr(
            "mcp_synology.modules.filestation.operations.share_contents",
            AsyncMock(return_value=["/writable/a"]),
        )
        delete = AsyncMock(return_value="[+] Deleted 1 item(s)")
        monkeypatch.setattr("mcp_synology.modules.filestation.operations.delete_files", delete)
        server, _, ctx = _make_ctx()
        register(ctx)
        return server, delete

    async def test_a_client_with_elicitation_is_asked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp import types
        from mcp.client import Client

        server, delete = self._server(monkeypatch)
        asked: list[str] = []

        async def on_elicit(
            _context: object, params: types.ElicitRequestParams
        ) -> types.ElicitResult:
            asked.append(params.message)
            return types.ElicitResult(action="accept", content={"delete": True})

        # Legacy mode: a session with a channel for server-initiated requests.
        async with Client(server, elicitation_callback=on_elicit, mode="legacy") as client:
            result = await client.call_tool("delete_files", {"paths": ["/writable"]})
        text = " ".join(getattr(c, "text", "") for c in result.content)
        assert not result.is_error, text
        assert asked and asked[0].endswith("Delete all of it?")
        assert delete.await_args.kwargs["paths"] == ["/writable/a"]

    async def test_a_client_without_elicitation_gets_a_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp.client import Client

        server, delete = self._server(monkeypatch)
        async with Client(server) as client:
            result = await client.call_tool("delete_files", {"paths": ["/writable"]})
        text = " ".join(getattr(c, "text", "") for c in result.content)
        assert 'confirm="' in text and "Nothing deleted yet" in text
        delete.assert_not_awaited()

    async def test_a_client_whose_transport_cant_elicit_gets_a_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The modern (stateless) mode declares elicitation but has no back-channel for it.
        from mcp import types
        from mcp.client import Client

        server, delete = self._server(monkeypatch)

        async def on_elicit(
            _context: object, params: types.ElicitRequestParams
        ) -> types.ElicitResult:
            return types.ElicitResult(action="accept", content={"delete": True})

        async with Client(server, elicitation_callback=on_elicit) as client:
            result = await client.call_tool("delete_files", {"paths": ["/writable"]})
        text = " ".join(getattr(c, "text", "") for c in result.content)
        assert not result.is_error, text
        assert 'confirm="' in text
        delete.assert_not_awaited()

    @pytest.mark.parametrize("reply", ["error", "invalid"])
    async def test_a_failed_elicitation_is_not_a_yes(
        self, monkeypatch: pytest.MonkeyPatch, reply: str
    ) -> None:
        # A dismissed dialog (an error) or an answer that doesn't fit must never become a
        # token the model could use alone (pre-QA on #451).
        from mcp import types
        from mcp.client import Client

        server, delete = self._server(monkeypatch)

        async def on_elicit(
            _context: object, params: types.ElicitRequestParams
        ) -> types.ElicitResult | types.ErrorData:
            if reply == "error":
                return types.ErrorData(code=-32600, message="user closed the dialog")
            return types.ElicitResult(action="accept", content={"delete": "maybe"})

        async with Client(server, elicitation_callback=on_elicit, mode="legacy") as client:
            result = await client.call_tool("delete_files", {"paths": ["/writable"]})
        text = " ".join(getattr(c, "text", "") for c in result.content)
        assert "Nothing deleted: the confirmation didn't complete" in text
        assert 'confirm="' not in text
        delete.assert_not_awaited()
