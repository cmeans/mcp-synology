"""Tests for server.py — server creation and tool registration."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from mcp_synology.modules import PermissionTier
from mcp_synology.server import (
    _BASE_INSTRUCTIONS,
    _MODULE_INSTRUCTIONS,
    _MODULE_REGISTRY,
    _MODULE_WRITE_INSTRUCTIONS,
    SharedClientManager,
    _compact_schema,
    _strip_html_comments,
    _SynologyServer,
    config_notices,
    create_server,
)
from tests.conftest import BASE_URL, make_test_config

if TYPE_CHECKING:
    from collections.abc import Iterator


class TestCreateServer:
    def test_server_creation(self) -> None:
        config = make_test_config()
        server = create_server(config)
        assert server is not None

    def test_server_with_read_permission(self) -> None:
        config = make_test_config(modules={"filestation": {"enabled": True, "permission": "read"}})
        server = create_server(config)
        assert server is not None

    def test_server_with_write_permission(self) -> None:
        config = make_test_config(modules={"filestation": {"enabled": True, "permission": "write"}})
        server = create_server(config)
        assert server is not None

    def test_server_with_disabled_module(self) -> None:
        config = make_test_config(
            modules={"filestation": {"enabled": False}, "system": {"enabled": True}}
        )
        tools = {t.name for t in create_server(config)._tool_manager.list_tools()}
        assert tools == {"get_system_info", "get_resource_usage"}

    def test_server_with_unknown_module(self) -> None:
        config = make_test_config(
            modules={
                "filestation": {"enabled": True},
                "unknown_module": {"enabled": True},
            }
        )
        server = create_server(config)
        assert server is not None

    def test_config_notices_name_unknown_settings_and_modules(self) -> None:
        """Typos in settings and module names are named (the spec's promised warnings)."""
        config = make_test_config(
            modules={
                "filestation": {
                    "enabled": True,
                    "settings": {"allowed_local_path": ["/tmp"], "delete_timeout": 60},
                },
                "system": {"enabled": False, "settings": {"anything": 1}},
                "downloadstaton": {"enabled": True},
            }
        )
        notices = config_notices(config)
        assert len(notices) == 3, notices
        fs = next(n for n in notices if "allowed_local_path'" in n)
        assert "modules.filestation.settings" in fs and "allowed_local_paths" in fs
        assert not any("'delete_timeout'" in n for n in notices)  # a real setting isn't flagged
        assert any("modules.system.settings" in n and "has no settings" in n for n in notices)
        assert any("Unknown module 'downloadstaton'" in n and "photos" in n for n in notices)

    def test_config_notices_quiet_for_a_clean_config(self) -> None:
        config = make_test_config(
            modules={"filestation": {"enabled": True, "settings": {"delete_timeout": 60}}}
        )
        assert config_notices(config) == []

    def test_create_server_logs_the_notices(self, caplog: pytest.LogCaptureFixture) -> None:
        config = make_test_config(
            modules={"filestation": {"enabled": True, "settings": {"delete_timout": 60}}}
        )
        with caplog.at_level(logging.WARNING, logger="mcp_synology.server"):
            create_server(config)
        assert any("delete_timout" in r.getMessage() for r in caplog.records)

    def test_server_uses_display_name_for_hostname(self) -> None:
        """Server should use config.display_name, not raw host."""
        config = make_test_config(alias="My NAS")
        assert config.display_name == "My NAS"
        server = create_server(config)
        assert server is not None

    def test_server_with_custom_settings(self) -> None:
        """Custom filestation settings are applied."""
        config = make_test_config(
            modules={
                "filestation": {
                    "enabled": True,
                    "permission": "write",
                    "settings": {
                        "file_type_indicator": "text",
                        "async_timeout": 300,
                        "search_timeout": 600,
                        "search_poll_interval": 2.0,
                        "hide_recycle_in_listings": False,
                    },
                }
            }
        )
        server = create_server(config)
        assert server is not None


class TestMcpInstructions:
    def test_core_file_holds_only_shared_guidance(self) -> None:
        assert "SERVER IDENTITY" in _BASE_INSTRUCTIONS
        assert "FILE SIZES" in _BASE_INSTRUCTIONS
        # Every connection says how to present times: UTC ones converted, zone-less ones not.
        times = " ".join(_BASE_INSTRUCTIONS.split("TIMES:", 1)[1].split())
        assert "Times ending in UTC are exact: give them in the user's own time zone." in times
        assert "Times with no zone are local clock times; show them as they are." in times
        # File Station's sections live in filestation.md (#169).
        sent = _strip_html_comments(_BASE_INSTRUCTIONS)
        assert "list_shares" not in sent
        assert "File Station" not in sent

    def test_filestation_file_holds_its_sections(self) -> None:
        text = _MODULE_INSTRUCTIONS["filestation"]
        for section in ("PATH FORMAT", "DOWNLOADING", "list_shares"):
            assert section in text
        write = _MODULE_WRITE_INSTRUCTIONS["filestation"]
        for section in ("MOVING AND ORGANIZING", "LONG OPERATIONS", "file_task"):
            assert section in write


def _instructions(**modules: dict[str, Any]) -> str:
    return create_server(make_test_config(modules=modules)).instructions or ""


# What each tool says about itself stays in its own description (#377).
_FS_SECTIONS = ("FILE STATION", "PATH FORMAT", "list_shares", "DOWNLOADING")
_FS_WRITE_SECTIONS = ("MOVING AND ORGANIZING", "LONG OPERATIONS", "file_task")


def _guidance(text: str) -> str:
    """The instructions before the PERMISSIONS section, which names withheld tools."""
    return text.split("\n\nPERMISSIONS:", 1)[0]


def _without_permissions(text: str) -> str:
    """Everything but the PERMISSIONS section (LOCAL MACHINE and LOCAL PATHS follow it)."""
    before, _, rest = text.partition("\n\nPERMISSIONS:")
    _, _, after = rest.partition("\n\n")
    return before + "\n\n" + after


def _write_tools(module: str) -> list[str]:
    return [
        t.name
        for t in _MODULE_REGISTRY[module].MODULE_INFO.tools
        if t.permission_tier is PermissionTier.WRITE
    ]


class TestInstructionsPerModule:
    """Only the enabled modules' sections are sent (#169, #304)."""

    @pytest.mark.parametrize(
        "modules",
        [
            {"system": {"enabled": True}},
            {"downloadstation": {"enabled": True, "permission": "write"}},
            {"photos": {"enabled": True, "permission": "write"}},
            # Listed but disabled counts as not enabled.
            {"filestation": {"enabled": False}, "system": {"enabled": True}},
        ],
        ids=["system", "downloadstation", "photos", "filestation-disabled"],
    )
    def test_no_file_station_sections_without_file_station(self, modules: dict[str, Any]) -> None:
        text = _instructions(**modules)
        assert "You are connected to a Synology NAS through mcp-synology." in text
        assert "FILE SIZES" in text
        for section in _FS_SECTIONS + _FS_WRITE_SECTIONS:
            assert section not in _guidance(text), section

    @pytest.mark.parametrize("permission", ["read", "write"])
    def test_file_station_sections_when_enabled(self, permission: str) -> None:
        text = _guidance(_instructions(filestation={"enabled": True, "permission": permission}))
        for section in _FS_SECTIONS:
            assert section in text, section
        # The core file comes first, then File Station's.
        assert text.index("FILE SIZES") < text.index("FILE STATION")

    @pytest.mark.parametrize("permission", ["write", "admin"])
    def test_file_station_write_guidance_with_write_tools(self, permission: str) -> None:
        text = _guidance(_instructions(filestation={"enabled": True, "permission": permission}))
        for section in _FS_WRITE_SECTIONS:
            assert section in text, section
        # After File Station's shared sections.
        assert text.index("DOWNLOADING") < text.index("MOVING AND ORGANIZING")

    @pytest.mark.parametrize("module", ["filestation", "photos"])
    def test_read_only_guidance_names_no_write_tools(self, module: str) -> None:
        """At permission: read, only PERMISSIONS names the withheld WRITE tools (#318)."""
        text = _instructions(**{module: {"enabled": True, "permission": "read"}})
        assert "LOCAL PATHS:" in text  # the download tool is registered
        guidance = _without_permissions(text)
        for tool in _write_tools(module):
            assert not re.search(rf"\b{tool}\b", guidance), tool
            assert tool in text.split("\n\nPERMISSIONS:", 1)[1], tool  # still listed as withheld
        for section in _FS_WRITE_SECTIONS:
            assert section not in guidance, section

    @pytest.mark.parametrize(
        ("module", "upload", "download"),
        [
            ("filestation", "upload_file", "download_file"),
            ("photos", "upload_photos", "download_photos"),
        ],
    )
    def test_local_paths_names_only_registered_tools(
        self, module: str, upload: str, download: str
    ) -> None:
        read = _instructions(**{module: {"enabled": True, "permission": "read"}})
        write = _instructions(**{module: {"enabled": True, "permission": "write"}})
        read_paths = read.split("LOCAL PATHS:", 1)[1]
        assert f"{download} only uses local paths" in read_paths
        assert upload not in read_paths
        assert f"{upload} and {download} only use local paths" in write.split("LOCAL PATHS:", 1)[1]

    @pytest.mark.parametrize("module", ["filestation", "photos", "downloadstation"])
    async def test_read_tools_name_no_write_tools(self, module: str) -> None:
        """A READ tool's description and schema don't name the module's WRITE tools (#318)."""
        server = create_server(make_test_config(modules={module: {"enabled": True}}))
        tools, _ = await _served(server)
        for tool in tools:
            text = (tool.description or "") + json.dumps(tool.input_schema)
            for name in _write_tools(module):
                assert not re.search(rf"\b{name}\b", text), (tool.name, name)

    def test_photos_write_guidance_with_write_tools(self) -> None:
        read = _guidance(_instructions(photos={"enabled": True, "permission": "read"}))
        write = _guidance(_instructions(photos={"enabled": True, "permission": "write"}))
        assert "SYNOLOGY PHOTOS" in read
        assert "photo_task" not in read
        assert "delete_photos" in write
        assert "photo_task" in write

    def test_system_only_has_no_local_machine(self) -> None:
        text = _instructions(system={"enabled": True})
        assert "LOCAL MACHINE" not in text
        assert "LOCAL PATHS" not in text
        assert "Home directory" not in text

    @pytest.mark.parametrize(
        "modules",
        [
            {"filestation": {"enabled": True, "permission": "read"}},  # download_file
            {"downloadstation": {"enabled": True, "permission": "write"}},  # torrent files
            {"photos": {"enabled": True, "permission": "read"}},  # download_photos
        ],
        ids=["filestation", "downloadstation", "photos"],
    )
    def test_local_machine_opens_local_paths(
        self, modules: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("mcp_synology.server._platform_label", lambda: "Linux")
        text = _instructions(**modules)
        local = text.index("LOCAL MACHINE:")
        # Directly before LOCAL PATHS, with nothing (e.g. PERMISSIONS) between them.
        assert "local filesystem.\n\nLOCAL PATHS:" in text
        assert "This MCP server runs on the user's Linux machine." in text
        assert f"Home directory: {Path.home()}" in text
        assert "{" not in text[local:]  # template variables filled in

    @pytest.mark.parametrize(
        "modules",
        [
            {"filestation": {"enabled": True, "permission": "read"}},
            {"system": {"enabled": True}},
            {"downloadstation": {"enabled": True, "permission": "write"}},
            {"photos": {"enabled": True, "permission": "write"}},
            {"filestation": {"enabled": True, "permission": "write"}},
            {"photos": {"enabled": True, "permission": "read"}},
            {
                m: {"enabled": True, "permission": "write"}
                for m in ("filestation", "system", "downloadstation", "photos")
            },
        ],
        ids=[
            "filestation",
            "system",
            "downloadstation",
            "photos",
            "filestation-write",
            "photos-read",
            "all",
        ],
    )
    def test_no_comment_text_is_sent(self, modules: dict[str, Any]) -> None:
        text = _instructions(**modules)
        assert "<!--" not in text
        assert "-->" not in text
        assert "Template variables" not in text
        assert "\n\n\n" not in text

    def test_comments_are_stripped_per_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unclosed comment in one file can't pair with a `-->` in the next one."""
        monkeypatch.setattr(
            "mcp_synology.server._BASE_INSTRUCTIONS", "CORE TEXT\n<!-- unclosed note"
        )
        monkeypatch.setitem(
            _MODULE_INSTRUCTIONS, "filestation", "FILE STATION TEXT\n<!-- a note -->\nMORE"
        )
        text = _instructions(filestation={"enabled": True, "permission": "read"})
        assert "FILE STATION TEXT" in text
        assert "MORE" in text


class TestFileStationSettings:
    def test_default_settings(self) -> None:
        from mcp_synology.modules.filestation import FileStationSettings

        s = FileStationSettings()
        assert s.hide_recycle_in_listings is False
        assert s.file_type_indicator == "emoji"
        assert s.async_timeout == 120
        assert s.search_timeout is None
        assert s.copy_move_timeout is None
        assert s.delete_timeout is None
        assert s.dir_size_timeout is None
        assert s.search_poll_interval == 1.0

    def test_specific_timeouts_override(self) -> None:
        from mcp_synology.modules.filestation import FileStationSettings

        s = FileStationSettings(
            async_timeout=60,
            search_timeout=300,
            copy_move_timeout=180,
        )
        assert s.async_timeout == 60
        assert s.search_timeout == 300
        assert s.copy_move_timeout == 180
        assert s.delete_timeout is None  # falls back to async_timeout

    def test_search_poll_interval_bounds(self) -> None:
        import pytest

        from mcp_synology.modules.filestation import FileStationSettings

        with pytest.raises(ValueError):
            FileStationSettings(search_poll_interval=0.1)  # below minimum 0.5
        with pytest.raises(ValueError):
            FileStationSettings(search_poll_interval=20.0)  # above maximum 10.0


class TestPlatformLabel:
    def test_macos(self) -> None:
        from mcp_synology.server import _platform_label

        with patch("platform.system", return_value="Darwin"):
            assert _platform_label() == "macOS"

    def test_linux(self) -> None:
        from mcp_synology.server import _platform_label

        with patch("platform.system", return_value="Linux"):
            assert _platform_label() == "Linux"

    def test_windows(self) -> None:
        from mcp_synology.server import _platform_label

        with patch("platform.system", return_value="Windows"):
            assert _platform_label() == "Windows"


class TestCreateServerInstructionPaths:
    def test_custom_instructions_prepended(self) -> None:
        config = make_test_config(custom_instructions="EXTRA RULES FOR THIS NAS")
        server = create_server(config)
        assert server is not None
        # The instructions should contain both our custom text and the built-in text
        instructions = server.instructions or ""
        assert "EXTRA RULES FOR THIS NAS" in instructions
        # Custom text appears before the built-in text
        custom_idx = instructions.index("EXTRA RULES FOR THIS NAS")
        builtin_idx = instructions.index("PATH FORMAT")
        assert custom_idx < builtin_idx

    def test_custom_instructions_keep_literal_braces(self) -> None:
        """Only the documented variables are filled in: other braces are text. With
        str.format_map, this config made create_server raise KeyError and serve fail."""
        config = make_test_config(
            custom_instructions='Reply as JSON like {"a": 1}; {unknown} stays; NAS {host}:{port}.'
        )
        instructions = create_server(config).instructions or ""
        assert 'Reply as JSON like {"a": 1}; {unknown} stays; NAS nas:5000.' in instructions

    def test_instructions_file_keeps_literal_braces(self, tmp_path: Path) -> None:
        instructions_file = tmp_path / "custom.md"
        instructions_file.write_text("Use {display_name}; a set is {1, 2}; {{ stays }}.")
        config = make_test_config(instructions_file=str(instructions_file))
        instructions = create_server(config).instructions or ""
        assert instructions.startswith("Use test; a set is {1, 2}; {{ stays }}.")

    def test_instructions_file_loaded(self, tmp_path: Path) -> None:
        instructions_file = tmp_path / "custom.md"
        instructions_file.write_text("FULLY REPLACED INSTRUCTIONS for {display_name}")
        config = make_test_config(instructions_file=str(instructions_file))
        server = create_server(config)
        assert server is not None
        instructions = server.instructions or ""
        assert "FULLY REPLACED INSTRUCTIONS" in instructions
        # Template variables get expanded — display_name should be filled in
        assert "{display_name}" not in instructions

    def test_instructions_file_missing_falls_back_to_builtin(self, tmp_path: Path) -> None:
        config = make_test_config(instructions_file=str(tmp_path / "nope.md"))
        server = create_server(config)
        assert server is not None
        instructions = server.instructions or ""
        # Falls back to the full built-in instructions, with File Station's section and
        # the generated ones, as if no file were set.
        assert "SERVER IDENTITY" in instructions
        assert "PATH FORMAT" in instructions
        assert "LOCAL MACHINE:" in instructions
        assert "LOCAL PATHS:" in instructions

    def test_instructions_file_missing_still_gets_permissions(self, tmp_path: Path) -> None:
        config = make_test_config(
            instructions_file=str(tmp_path / "nope.md"),
            modules={"filestation": {"enabled": True, "permission": "read"}},
        )
        assert "PERMISSIONS:" in (create_server(config).instructions or "")


class TestSharedClientManagerLifecycle:
    """Direct tests of the SharedClientManager — get_client lazy init,
    update notice delivery, signal handler installation,
    background update check, cleanup_session."""

    async def test_take_update_notice_once_and_claims_it(self) -> None:
        manager = SharedClientManager(make_test_config())
        manager._update_notice, manager._pending_version = "Update!", "9.9.9"
        with patch("mcp_synology.cli.version.claim_announcement", return_value=True) as claim:
            assert await manager.take_update_notice() == "\n\n---\nUpdate!"
            assert await manager.take_update_notice() == ""
        claim.assert_called_once_with("9.9.9")
        assert manager._announced_version == "9.9.9"

    async def test_two_connections_on_one_install_announce_once(self) -> None:
        # Each connection's check queues it; the first to deliver claims it (#314).
        first, second = (SharedClientManager(make_test_config()) for _ in range(2))
        for m in (first, second):
            m._update_notice, m._pending_version = "Update!", "9.9.9"
        assert await first.take_update_notice() == "\n\n---\nUpdate!"
        assert await second.take_update_notice() == ""

    async def test_take_update_notice_without_one(self) -> None:
        assert await SharedClientManager(make_test_config()).take_update_notice() == ""

    async def test_start_update_check_once_and_only_when_enabled(self) -> None:
        async def _no_op() -> None:
            return None

        off = SharedClientManager(make_test_config(check_for_updates=False))
        off.start_update_check()
        assert off._bg_task is None
        on = SharedClientManager(make_test_config(check_for_updates=True))
        with patch.object(SharedClientManager, "_bg_update_check", side_effect=_no_op):
            on.start_update_check()
            task = on._bg_task
            on.start_update_check()
            assert on._bg_task is task
            assert task is not None
            await task

    async def test_get_client_lazy_init_happy_path(self) -> None:
        config = make_test_config()
        manager = SharedClientManager(config)

        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.query_api_info = AsyncMock(return_value={})
        fake_auth = MagicMock()
        fake_auth.login = AsyncMock(return_value="sid-123")

        with (
            patch("mcp_synology.server.DsmClient", return_value=fake_client),
            patch("mcp_synology.server.AuthManager", return_value=fake_auth),
        ):
            client = await manager.get_client()

        assert client is fake_client
        fake_client.__aenter__.assert_awaited_once()
        fake_client.query_api_info.assert_awaited_once()
        fake_auth.login.assert_awaited_once()
        # Subsequent call short-circuits
        client2 = await manager.get_client()
        assert client2 is fake_client
        # __aenter__ should still only have been called once
        fake_client.__aenter__.assert_awaited_once()

    async def test_get_client_https(self) -> None:
        config = make_test_config(connection={"host": "nas", "port": 5001, "https": True})
        manager = SharedClientManager(config)

        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.query_api_info = AsyncMock(return_value={})
        fake_auth = MagicMock()
        fake_auth.login = AsyncMock(return_value="sid")

        with (
            patch("mcp_synology.server.DsmClient", return_value=fake_client) as dsm_client_cls,
            patch("mcp_synology.server.AuthManager", return_value=fake_auth),
        ):
            await manager.get_client()
        kwargs = dsm_client_cls.call_args.kwargs
        assert kwargs["base_url"].startswith("https://")

    async def test_get_client_raises_when_connection_missing(self) -> None:
        config = make_test_config()
        config.connection = None  # type: ignore[assignment]
        manager = SharedClientManager(config)
        with pytest.raises(RuntimeError, match="connection"):
            await manager.get_client()

    async def test_get_client_no_longer_starts_the_update_check(self) -> None:
        # The server's lifespan starts it (#314), so it doesn't wait for a DSM login.
        config = make_test_config(check_for_updates=True)
        manager = SharedClientManager(config)

        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.query_api_info = AsyncMock(return_value={})
        fake_auth = MagicMock()
        fake_auth.login = AsyncMock(return_value="sid")

        with (
            patch("mcp_synology.server.DsmClient", return_value=fake_client),
            patch("mcp_synology.server.AuthManager", return_value=fake_auth),
        ):
            await manager.get_client()
        assert manager._bg_task is None

    def test_install_signal_handlers_registers_sigterm_and_sigint(self) -> None:
        """No atexit handler any more: it ran after the loop ended and the logout never
        completed (#402). Signals raise SystemExit so the lifespan teardown logs out."""
        import signal as signal_mod

        manager = SharedClientManager(make_test_config())
        captured: dict[int, Any] = {}
        with (
            patch("atexit.register") as atexit_register,
            patch("signal.signal", side_effect=lambda n, h: captured.__setitem__(n, h)),
        ):
            manager.install_signal_handlers()
        atexit_register.assert_not_called()
        assert set(captured) == {signal_mod.SIGTERM, signal_mod.SIGINT}
        with pytest.raises(SystemExit) as exc:
            captured[signal_mod.SIGTERM](signal_mod.SIGTERM, None)
        assert exc.value.code == 128 + int(signal_mod.SIGTERM)

    def _manager_with_session(self) -> SharedClientManager:
        manager = SharedClientManager(make_test_config())
        manager._auth = MagicMock()
        manager._auth.logout = AsyncMock()
        manager._client = MagicMock()
        manager._client.__aexit__ = AsyncMock()
        return manager

    async def test_aclose_logs_out_and_closes_the_client(self) -> None:
        manager = self._manager_with_session()
        auth, client = manager._auth, manager._client
        await manager.aclose()
        auth.logout.assert_awaited_once()  # type: ignore[union-attr]
        client.__aexit__.assert_awaited_once()  # type: ignore[union-attr]
        assert manager._auth is None and manager._client is None
        await manager.aclose()  # a second call does nothing
        auth.logout.assert_awaited_once()  # type: ignore[union-attr]

    async def test_aclose_without_a_session_does_nothing(self) -> None:
        manager = SharedClientManager(make_test_config())
        await manager.aclose()
        assert manager._auth is None and manager._client is None

    async def test_aclose_logs_a_failed_logout_and_still_closes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        manager = self._manager_with_session()
        client = manager._client
        manager._auth.logout = AsyncMock(side_effect=httpx.ConnectError("gone"))  # type: ignore[union-attr]
        with caplog.at_level(logging.WARNING, logger="mcp_synology.server"):
            await manager.aclose()
        assert "Couldn't log out of DSM on shutdown (ConnectError: gone)" in caplog.text
        client.__aexit__.assert_awaited_once()  # type: ignore[union-attr]

    async def test_aclose_warns_on_a_dsm_error_reply(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A DSM error or a proxy's page on logout warns too, through the real AuthManager:
        logout(raise_errors=True) lets it reach aclose instead of a DEBUG line."""
        import respx

        from mcp_synology.core.auth import AuthManager
        from tests.conftest import BASE_URL, make_api_cache, make_client

        manager = SharedClientManager(make_test_config())
        client = make_client(make_api_cache())
        await client.__aenter__()
        client.sid = "s"
        manager._client = client
        manager._auth = AuthManager(make_test_config(), client)
        with (
            respx.mock as mock,
            caplog.at_level(logging.WARNING, logger="mcp_synology.server"),
        ):
            mock.get(f"{BASE_URL}/webapi/entry.cgi").respond(
                json={"success": False, "error": {"code": 105}}
            )
            await manager.aclose()
        assert "Couldn't log out of DSM on shutdown" in caplog.text

    async def test_aclose_cancels_the_update_check(self) -> None:
        import asyncio

        import anyio

        manager = self._manager_with_session()

        async def forever() -> None:
            await anyio.sleep(60)

        manager._bg_task = asyncio.create_task(forever())
        await manager.aclose()
        await asyncio.sleep(0)
        assert manager._bg_task.cancelled()

    async def test_aclose_gives_up_on_a_logout_that_doesnt_answer(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anyio

        import mcp_synology.server as server_mod

        monkeypatch.setattr(server_mod, "LOGOUT_TIMEOUT", 0.05)
        manager = self._manager_with_session()
        client = manager._client

        async def hang(**_: Any) -> None:
            await anyio.sleep(10)

        manager._auth.logout = hang  # type: ignore[union-attr,method-assign]
        with caplog.at_level(logging.WARNING, logger="mcp_synology.server"):
            await manager.aclose()
        assert "DSM didn't answer the logout within 0.05 s on shutdown" in caplog.text
        client.__aexit__.assert_awaited_once()  # type: ignore[union-attr]

    async def test_aclose_still_logs_out_when_the_run_is_cancelled(self) -> None:
        """SIGTERM unwinds the loop by cancelling: the logout is shielded."""
        import anyio

        manager = self._manager_with_session()
        auth = manager._auth
        done = anyio.Event()

        async def slow_logout(**_: Any) -> None:
            await anyio.sleep(0.05)
            done.set()

        auth.logout = slow_logout  # type: ignore[union-attr,method-assign]
        with anyio.CancelScope() as scope:
            scope.cancel()
            await manager.aclose()
        assert done.is_set()

    async def test_server_lifespan_closes_the_manager_when_the_run_ends(self) -> None:
        """The logout runs in the server's lifespan teardown, inside the live loop (#402)."""
        with patch.object(SharedClientManager, "aclose", new_callable=AsyncMock) as aclose:
            server = create_server(make_test_config())
            async with Client(server):
                aclose.assert_not_awaited()
        aclose.assert_awaited_once()

    async def test_bg_update_check_with_newer_version(self) -> None:
        config = make_test_config(check_for_updates=True)
        manager = SharedClientManager(config)

        with (
            patch(
                "mcp_synology.cli.version.refresh_update_check",
                return_value=("9.9.9", False, {}),
            ),
        ):
            await manager._bg_update_check()
        assert manager._update_notice is not None
        assert "9.9.9" in manager._update_notice
        assert manager._pending_version == "9.9.9"

    async def test_bg_update_check_logs_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        manager = SharedClientManager(make_test_config(check_for_updates=True))
        state = {"latest_is_security": True}
        caplog.set_level(logging.WARNING, logger="mcp_synology.server")
        with (
            patch(
                "mcp_synology.cli.version.refresh_update_check",
                return_value=("9.9.9", bool(state.get("latest_is_security")), state),
            ),
        ):
            await manager._bg_update_check()
        assert "mcp-synology 9.9.9 is available, a security release" in caplog.text
        assert manager._update_notice is not None
        assert manager._update_notice.startswith("A security release of mcp-synology")

    @pytest.mark.parametrize(
        ("state", "announced_here"),
        [
            # Announced within the week, by any instance on the machine.
            (
                {
                    "announced": {
                        sys.prefix: {"version": "9.9.9", "at": "2999-01-01T00:00:00+00:00"}
                    }
                },
                None,
            ),
            # This session's instructions already carry it.
            ({}, "9.9.9"),
        ],
    )
    async def test_bg_update_check_holds_back_an_announced_version(
        self, state: dict[str, Any], announced_here: str | None
    ) -> None:
        manager = SharedClientManager(make_test_config(check_for_updates=True))
        manager._announced_version = announced_here
        with (
            patch(
                "mcp_synology.cli.version.refresh_update_check",
                return_value=("9.9.9", bool(state.get("latest_is_security")), state),
            ),
        ):
            await manager._bg_update_check()
        assert manager._update_notice is None

    async def test_server_lifespan_starts_the_update_check(self) -> None:
        with patch.object(SharedClientManager, "start_update_check") as start:
            server = create_server(make_test_config(check_for_updates=False))
            start.assert_not_called()
            async with Client(server):
                start.assert_called_once()

    async def test_bg_update_check_no_update(self) -> None:
        config = make_test_config(check_for_updates=True)
        manager = SharedClientManager(config)
        with (
            patch(
                "mcp_synology.cli.version.refresh_update_check",
                return_value=(None, False, {}),
            ),
        ):
            await manager._bg_update_check()
        assert manager._update_notice is None

    async def test_bg_update_check_swallows_errors(self, caplog: pytest.LogCaptureFixture) -> None:
        """OSError/ValueError/KeyError during update check must not raise,
        AND the actual exception must be logged at DEBUG with exc_info so
        `SYNOLOGY_LOG_LEVEL=debug` surfaces the cause (closes #39, half 1).
        """
        import logging

        config = make_test_config(check_for_updates=True)
        manager = SharedClientManager(config)
        with (
            patch(
                "mcp_synology.cli.version.refresh_update_check", side_effect=OSError("disk full")
            ),
            caplog.at_level(logging.DEBUG, logger="mcp_synology.server"),
        ):
            await manager._bg_update_check()  # must not raise
        assert manager._update_notice is None
        # The actual exception is logged at DEBUG with exc_info — pre-#39
        # this branch was a bare `pass` and left no breadcrumb.
        matching = [
            r
            for r in caplog.records
            if r.levelname == "DEBUG" and "Update check failed" in r.getMessage()
        ]
        assert matching, (
            f"expected DEBUG log on update-check failure, got: "
            f"{[r.getMessage() for r in caplog.records]}"
        )
        assert matching[0].exc_info is not None, (
            "expected exc_info traceback on the update-check failure log"
        )

    async def test_bg_update_check_timeout_logged_and_swallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A stuck executor task is bounded by asyncio.timeout(10); the
        coroutine returns normally with a DEBUG log noting the timeout.
        Closes #39 (half 2). Pre-fix, an executor that hung after the
        urlopen 5s socket timeout (e.g. pathological YAML parsing, slow
        disk) would keep this coroutine alive indefinitely.
        """
        import logging

        config = make_test_config(check_for_updates=True)
        manager = SharedClientManager(config)

        # Simulate a hung executor by patching asyncio.timeout to raise
        # TimeoutError immediately (the inner await still runs but the
        # context manager is what enforces the bound; raising on enter
        # exercises the same except-arm a real timeout would).
        async def _slow_check(*args: object, **kwargs: object) -> None:
            # Use a sleep longer than the 10s timeout. We patch
            # asyncio.timeout to use a tiny window so this test is fast.
            await asyncio.sleep(5)

        # Patch the inner executor call to a slow coroutine, AND tighten
        # the timeout window to make the test fast. Easier path: patch
        # `loop.run_in_executor` to return a never-completing future and
        # reduce the timeout to ~0.05s so the test completes quickly.
        original_timeout = asyncio.timeout

        def _quick_timeout(_seconds: float) -> Any:
            # Force a tight 50ms window regardless of caller's argument so
            # the test isn't waiting 10s of real time.
            return original_timeout(0.05)

        async def _stuck_executor(*args: object, **kwargs: object) -> str:
            await asyncio.sleep(5)
            return "9.9.9"

        async def _stub_run_in_executor(*args: object, **kwargs: object) -> str:
            return await _stuck_executor()

        with (
            patch("asyncio.timeout", side_effect=_quick_timeout),
            patch.object(
                asyncio.get_event_loop(),
                "run_in_executor",
                side_effect=_stub_run_in_executor,
            ),
            caplog.at_level(logging.DEBUG, logger="mcp_synology.server"),
        ):
            await manager._bg_update_check()  # must not raise

        assert manager._update_notice is None  # no notice on timeout
        matching = [
            r
            for r in caplog.records
            if r.levelname == "DEBUG" and "Update check timed out" in r.getMessage()
        ]
        assert matching, (
            f"expected DEBUG log on update-check timeout, got: "
            f"{[r.getMessage() for r in caplog.records]}"
        )


class TestSharedClientManagerSubscribeOnReauth:
    """Reauth-callback subscription works pre-auth (queued) and post-auth (direct).

    Closes #37 — modules use this hook to invalidate caches that may have
    drifted on the NAS between sessions (e.g. filestation's recycle-bin
    probe cache).
    """

    def test_pre_auth_subscription_is_queued(self) -> None:
        """Subscribing before AuthManager exists queues the callback."""
        config = make_test_config()
        manager = SharedClientManager(config)

        cb = MagicMock()
        manager.subscribe_on_reauth(cb)

        assert manager._auth is None
        assert cb in manager._pending_reauth_callbacks

    async def test_pending_callbacks_flush_on_get_client(self) -> None:
        """Once `get_client` lazily creates the AuthManager, queued callbacks
        are forwarded and the pending list is cleared.
        """
        config = make_test_config()
        manager = SharedClientManager(config)

        cb1 = MagicMock()
        cb2 = MagicMock()
        manager.subscribe_on_reauth(cb1)
        manager.subscribe_on_reauth(cb2)

        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.query_api_info = AsyncMock(return_value={})

        forwarded_callbacks: list[object] = []
        fake_auth = MagicMock()
        fake_auth.login = AsyncMock(return_value="sid")
        fake_auth.add_on_reauth_callback = MagicMock(side_effect=forwarded_callbacks.append)

        with (
            patch("mcp_synology.server.DsmClient", return_value=fake_client),
            patch("mcp_synology.server.AuthManager", return_value=fake_auth),
        ):
            await manager.get_client()

        # Both callbacks were forwarded to the AuthManager in the order
        # they were subscribed, and the pending queue was cleared.
        assert forwarded_callbacks == [cb1, cb2]
        assert manager._pending_reauth_callbacks == []

    async def test_post_auth_subscription_attaches_directly(self) -> None:
        """Subscribing after AuthManager exists skips the queue."""
        config = make_test_config()
        manager = SharedClientManager(config)

        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.query_api_info = AsyncMock(return_value={})
        fake_auth = MagicMock()
        fake_auth.login = AsyncMock(return_value="sid")
        fake_auth.add_on_reauth_callback = MagicMock()

        with (
            patch("mcp_synology.server.DsmClient", return_value=fake_client),
            patch("mcp_synology.server.AuthManager", return_value=fake_auth),
        ):
            await manager.get_client()

        cb = MagicMock()
        manager.subscribe_on_reauth(cb)

        # Attached directly — no queueing.
        fake_auth.add_on_reauth_callback.assert_called_once_with(cb)
        assert manager._pending_reauth_callbacks == []


class TestSharedClientManagerUnreachableNas:
    """#138: an unreachable NAS used to give every tool an empty error message
    ("Error executing tool list_shares: "), because httpx.ConnectTimeout('')
    stringifies to ''."""

    @staticmethod
    def _envelope(exc: Exception) -> dict[str, Any]:
        import json

        return json.loads(str(exc))["error"]  # type: ignore[no-any-return]

    @pytest.mark.parametrize(
        ("raised", "code"),
        [
            (httpx.ConnectTimeout, "timeout"),
            (httpx.ConnectError, "unavailable"),
        ],
    )
    @respx.mock
    async def test_transport_failure_is_a_structured_error(
        self, raised: type[Exception], code: str
    ) -> None:
        from mcp.server.mcpserver.exceptions import ToolError

        manager = SharedClientManager(make_test_config())
        respx.get(f"{BASE_URL}/webapi/query.cgi").mock(side_effect=raised)
        with pytest.raises(ToolError) as exc:
            await manager.get_client()
        err = self._envelope(exc.value)
        assert err["code"] == code
        assert err["message"].startswith("Can't reach the NAS at http://nas:5000")
        assert err["retryable"] is True
        assert "powered on" in err["suggestion"]
        assert "mcp-synology check" in err["suggestion"]

    @pytest.mark.parametrize("raised", [httpx.ReadTimeout, httpx.WriteTimeout])
    @respx.mock
    async def test_slow_nas_is_not_unreachable(self, raised: type[Exception]) -> None:
        """#254: a busy NAS that doesn't answer the login in time was told "can't reach",
        and the model then blamed the network. It's a timeout on a reachable NAS."""
        from mcp.server.mcpserver.exceptions import ToolError

        manager = SharedClientManager(make_test_config())
        respx.get(f"{BASE_URL}/webapi/query.cgi").mock(side_effect=raised)
        with pytest.raises(ToolError) as exc:
            await manager.get_client()
        err = self._envelope(exc.value)
        assert err["code"] == "timeout"
        assert err["retryable"] is True
        assert err["message"] == (
            "The NAS at http://nas:5000 accepted the connection but didn't answer within "
            f"30 s ({raised.__name__}: Mock Error). It may be busy (heavy load, or a large "
            "delete or copy)."
        )
        assert "connection.timeout" in err["suggestion"]
        assert "powered on" not in err["suggestion"]

    @pytest.mark.parametrize("raised", [httpx.ReadError, httpx.RemoteProtocolError])
    @respx.mock
    async def test_dropped_connection_is_not_unreachable(self, raised: type[Exception]) -> None:
        """Connected, then dropped: "could not connect" blamed the network too (#254)."""
        from mcp.server.mcpserver.exceptions import ToolError

        manager = SharedClientManager(make_test_config())
        respx.get(f"{BASE_URL}/webapi/query.cgi").mock(side_effect=raised("gone"))
        with pytest.raises(ToolError) as exc:
            await manager.get_client()
        err = self._envelope(exc.value)
        assert (err["code"], err["retryable"]) == ("unavailable", True)
        assert err["message"] == (
            f"The NAS at http://nas:5000 accepted the connection but dropped it "
            f"({raised.__name__}: gone). It may be busy or restarting."
        )
        assert "powered on" not in err["suggestion"]

    @respx.mock
    async def test_pool_timeout_never_sent_anything(self) -> None:
        from mcp.server.mcpserver.exceptions import ToolError

        manager = SharedClientManager(make_test_config())
        respx.get(f"{BASE_URL}/webapi/query.cgi").mock(side_effect=httpx.PoolTimeout(""))
        with pytest.raises(ToolError) as exc:
            await manager.get_client()
        err = self._envelope(exc.value)
        assert err["code"] == "timeout"
        assert err["message"] == (
            "No connection to the NAS at http://nas:5000 was free within 30 s (PoolTimeout), "
            "so the request wasn't sent."
        )

    @respx.mock
    async def test_url_in_the_error_text_is_redacted(self) -> None:
        """As every other transport error (#232): DSM URLs carry passwd or _sid."""
        from mcp.server.mcpserver.exceptions import ToolError

        manager = SharedClientManager(make_test_config())
        respx.get(f"{BASE_URL}/webapi/query.cgi").mock(
            side_effect=httpx.ReadTimeout(
                "timed out at http://nas:5000/webapi/entry.cgi?passwd=SECRET"
            )
        )
        with pytest.raises(ToolError) as exc:
            await manager.get_client()
        assert "SECRET" not in str(exc.value)

    @respx.mock
    async def test_half_open_client_is_closed_and_next_call_retries(self) -> None:
        from mcp.server.mcpserver.exceptions import ToolError

        from mcp_synology.core.client import DsmClient

        manager = SharedClientManager(make_test_config())
        route = respx.get(f"{BASE_URL}/webapi/query.cgi").mock(side_effect=httpx.ConnectError)
        closed: list[DsmClient] = []
        original_exit = DsmClient.__aexit__

        async def spy_exit(self: DsmClient, *args: object) -> None:
            closed.append(self)
            await original_exit(self, *args)

        with patch.object(DsmClient, "__aexit__", spy_exit), pytest.raises(ToolError):
            await manager.get_client()
        assert len(closed) == 1
        assert manager._client is None
        with pytest.raises(ToolError):
            await manager.get_client()
        assert route.call_count == 2  # not cached as broken

    async def test_login_failure_also_closes_the_client(self) -> None:
        from mcp_synology.core.errors import AuthenticationError

        manager = SharedClientManager(make_test_config())
        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        fake_client.query_api_info = AsyncMock(return_value={})
        fake_auth = MagicMock()
        fake_auth.login = AsyncMock(side_effect=AuthenticationError("bad password"))
        with (
            patch("mcp_synology.server.DsmClient", return_value=fake_client),
            patch("mcp_synology.server.AuthManager", return_value=fake_auth),
            pytest.raises(AuthenticationError),
        ):
            await manager.get_client()
        fake_client.__aexit__.assert_awaited_once()
        assert manager._client is None


class TestWithheldToolsNotice:
    """A read-only connection hides its WRITE tools, so neither the model nor a
    directory scorer (Glama, 2026-04) could tell they exist. The instructions say
    which tools are withheld and how to enable them."""

    def _instructions(self, **overrides: Any) -> str:
        server = create_server(make_test_config(**overrides))
        return server.instructions or ""

    def test_read_only_module_lists_withheld_write_tools(self) -> None:
        text = self._instructions(modules={"filestation": {"enabled": True, "permission": "read"}})
        assert "PERMISSIONS" in text
        assert "read-only" in text
        for tool in (
            "upload_file",
            "delete_files",
            "move_files",
            "rename",
            "restore_from_recycle_bin",
            "create_folder",
            "copy_files",
        ):
            assert tool in text.split("PERMISSIONS", 1)[1]
        assert "permission: write" in text

    def test_nothing_withheld_means_no_section(self) -> None:
        """Every module enabled at ``write``: no PERMISSIONS section at all."""
        text = self._instructions(
            modules={
                "filestation": {"enabled": True, "permission": "write"},
                "system": {"enabled": True, "permission": "write"},
                "downloadstation": {"enabled": True, "permission": "write"},
                "photos": {"enabled": True, "permission": "write"},
                "storage": {"enabled": True},
            }
        )
        assert "PERMISSIONS" not in text

    def test_module_not_enabled_is_named(self) -> None:
        text = self._instructions(modules={"filestation": {"enabled": True, "permission": "write"}})
        assert "downloadstation" in text
        assert "not enabled" in text

    def test_storage_not_enabled_names_the_administrator_requirement(self) -> None:
        text = self._instructions(modules={"filestation": {"enabled": True, "permission": "write"}})
        line = next(x for x in text.splitlines() if "The storage module" in x)
        assert "not enabled" in line
        assert "(it needs a DSM administrator account)" in line
        other = next(x for x in text.splitlines() if "The photos module" in x)
        assert "administrator" not in other

    def test_storage_section_only_when_storage_is_enabled(self) -> None:
        """#49: the STORAGE section says the tools need an administrator account."""
        on = self._instructions(modules={"storage": {"enabled": True}})
        assert "\n\nSTORAGE:\n" in on
        assert "only a DSM administrator account" in on
        assert "get_storage_overview" in on
        off = self._instructions(modules={"filestation": {"enabled": True}})
        assert "\n\nSTORAGE:\n" not in off

    def test_disabled_module_is_told_to_enable_not_add(self) -> None:
        """QA on #156: a module listed with ``enabled: false`` is already under
        ``modules:``; the fix the user needs is ``enabled: true``."""
        text = self._instructions(
            modules={
                "filestation": {"enabled": True, "permission": "write"},
                "downloadstation": {"enabled": False, "permission": "write"},
            }
        )
        section = text.split("PERMISSIONS", 1)[1]
        assert "downloadstation" in section
        assert "`enabled: true`" in section
        assert "add `downloadstation` under `modules:`" not in section

    def test_full_instructions_file_is_left_alone(self, tmp_path: Path) -> None:
        custom = tmp_path / "custom.md"
        custom.write_text("ONLY THESE INSTRUCTIONS")
        text = self._instructions(
            instructions_file=str(custom),
            modules={"filestation": {"enabled": True, "permission": "read"}},
        )
        assert text == "ONLY THESE INSTRUCTIONS"

    def test_withheld_tools_are_not_registered(self) -> None:
        """The notice changes nothing about registration: still read-only."""
        server = create_server(
            make_test_config(modules={"filestation": {"enabled": True, "permission": "read"}})
        )
        names = set(server._tool_manager._tools)
        assert "delete_files" not in names
        assert "list_files" in names


class TestCompactSchema:
    """`_compact_schema` drops `title`s and collapses `anyOf: [T, null]` + null default (#163)."""

    def test_drops_titles_at_every_level(self) -> None:
        schema = {
            "title": "tool_listArguments",
            "type": "object",
            "properties": {"path": {"title": "Path", "type": "string"}},
            "required": ["path"],
        }
        assert _compact_schema(schema) == {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }

    def test_collapses_optional_scalar(self) -> None:
        node = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "P"}
        assert _compact_schema(node) == {"type": "string"}

    def test_collapses_when_null_branch_comes_first(self) -> None:
        node = {"anyOf": [{"type": "null"}, {"type": "integer"}], "default": None}
        assert _compact_schema(node) == {"type": "integer"}

    def test_collapses_optional_list_and_keeps_items(self) -> None:
        node = {
            "anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}],
            "default": None,
            "title": "Additional",
        }
        assert _compact_schema(node) == {"items": {"type": "string"}, "type": "array"}

    def test_keeps_description_enum_examples(self) -> None:
        node = {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": None,
            "description": "Sort order.",
            "enum": ["queue", "created", "completed"],
            "examples": ["completed"],
        }
        assert _compact_schema(node) == {
            "type": "string",
            "description": "Sort order.",
            "enum": ["queue", "created", "completed"],
            "examples": ["completed"],
        }

    def test_non_null_default_is_left_as_union(self) -> None:
        node = {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": 5, "title": "X"}
        assert _compact_schema(node) == {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
            "default": 5,
        }

    def test_union_of_two_real_types_is_untouched(self) -> None:
        node = {
            "anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}],
            "default": None,
        }
        assert _compact_schema(node) == node

    def test_keeps_non_null_defaults_and_nested_objects(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "limit": {"default": 25, "title": "Limit", "type": "integer"},
                "opts": {
                    "type": "object",
                    "title": "Opts",
                    "properties": {"x": {"title": "X", "type": "boolean", "default": False}},
                },
            },
        }
        assert _compact_schema(schema) == {
            "type": "object",
            "properties": {
                "limit": {"default": 25, "type": "integer"},
                "opts": {
                    "type": "object",
                    "properties": {"x": {"type": "boolean", "default": False}},
                },
            },
        }

    def test_all_null_union_is_untouched(self) -> None:
        node = {"anyOf": [{"type": "null"}, {"type": "null"}], "default": None}
        assert _compact_schema(node) == node

    def test_does_not_mutate_input(self) -> None:
        node = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "P"}
        before = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "P"}
        _compact_schema(node)
        assert node == before


ALL_MODULES: dict[str, Any] = {
    "filestation": {"enabled": True, "permission": "write"},
    "system": {"enabled": True},
    "downloadstation": {"enabled": True, "permission": "write"},
    "photos": {"enabled": True, "permission": "write"},
    "storage": {"enabled": True},
}


def _walk(node: Any) -> Iterator[Any]:
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


async def _served(server: MCPServer) -> tuple[list[Any], str]:
    """Tools and instructions exactly as an MCP client receives them."""
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        instructions = client.instructions
    return list(tools), instructions or ""


class TestServerInfo:
    async def test_initialize_reports_mcp_synology_version(self) -> None:
        """#166: mcp 1.x's FastMCP sent the mcp SDK's version as serverInfo.version."""
        from importlib.metadata import version

        server = create_server(make_test_config())
        async with Client(server) as client:
            info = client.server_info
        assert info is not None
        assert info.version == version("mcp-synology")
        assert info.version != version("mcp")

    async def test_initialize_lists_svg_and_png_icons(self) -> None:
        """#229: clients that display icons must support PNG; SVG is only a SHOULD."""
        server = create_server(make_test_config())
        async with Client(server) as client:
            info = client.server_info
        assert info is not None
        icons = info.icons or []
        assert sorted((i.mime_type, i.theme) for i in icons) == [
            ("image/png", "dark"),
            ("image/png", "light"),
            ("image/svg+xml", "dark"),
            ("image/svg+xml", "light"),
        ]
        for icon in icons:
            if icon.mime_type == "image/svg+xml":
                assert icon.src.startswith("https://"), icon.src
                assert icon.sizes == ["any"]
                continue
            prefix = "data:image/png;base64,"
            assert icon.src.startswith(prefix)
            png = base64.b64decode(icon.src.removeprefix(prefix), validate=True)
            assert png.startswith(b"\x89PNG\r\n\x1a\n")
            assert icon.sizes == ["128x128"]
            # The declared size is the image's: width and height from the IHDR chunk.
            assert png[16:24] == (128).to_bytes(4, "big") * 2


class TestNoSecretsInErrors:
    """#232: an HTTP error status from DSM put the request URL, with passwd or _sid,
    into the error text the model got and into the ERROR log's traceback."""

    _INFO = {
        "success": True,
        "data": {
            "SYNO.API.Auth": {"path": "auth.cgi", "minVersion": 1, "maxVersion": 6},
            "SYNO.FileStation.List": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
        },
    }

    @staticmethod
    def _logged(caplog: pytest.LogCaptureFixture) -> str:
        import traceback

        text = caplog.text
        for record in caplog.records:
            if record.exc_info:
                text += "".join(traceback.format_exception(*record.exc_info))
        return text

    @pytest.fixture
    def creds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SYNOLOGY_USERNAME", "chris")
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "S3cretPW")

    async def _call(self, caplog: pytest.LogCaptureFixture) -> str:
        server = create_server(make_test_config())
        with caplog.at_level(logging.DEBUG):
            async with Client(server) as client:
                result = await client.call_tool("list_shares", {})
        assert result.is_error
        return " ".join(getattr(c, "text", "") for c in result.content)

    @respx.mock
    @pytest.mark.usefixtures("creds")
    async def test_login_http_error(self, caplog: pytest.LogCaptureFixture) -> None:
        respx.get(f"{BASE_URL}/webapi/query.cgi").respond(json=self._INFO)
        respx.post(f"{BASE_URL}/webapi/auth.cgi").respond(status_code=502)
        text = await self._call(caplog)
        assert "S3cretPW" not in text
        assert "S3cretPW" not in self._logged(caplog)
        err = json.loads(text.removeprefix("Error executing tool list_shares: "))["error"]
        assert err["code"] == "unavailable"
        assert "SYNO.API.Auth/login: DSM answered HTTP 502." in err["message"]
        assert err["retryable"] is True

    @respx.mock
    @pytest.mark.usefixtures("creds")
    async def test_tool_http_error(self, caplog: pytest.LogCaptureFixture) -> None:
        respx.get(f"{BASE_URL}/webapi/query.cgi").respond(json=self._INFO)
        respx.post(f"{BASE_URL}/webapi/auth.cgi").respond(
            json={"success": True, "data": {"sid": "SIDSECRET"}}
        )
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(status_code=500)
        text = await self._call(caplog)
        assert "SIDSECRET" not in text
        assert "SIDSECRET" not in self._logged(caplog)
        assert '"code": "unavailable"' in text
        assert "SYNO.FileStation.List/list_share: DSM answered HTTP 500." in text

    async def test_crash_text_is_redacted(self) -> None:
        """Backstop: any other exception naming a DSM URL loses its query values."""
        crash = RuntimeError(
            "boom for url 'http://nas:5000/webapi/auth.cgi?account=chris&passwd=S3cretPW'"
        )
        server = create_server(make_test_config())
        with patch.object(SharedClientManager, "get_client", side_effect=crash):
            async with Client(server) as client:
                result = await client.call_tool("list_shares", {})
        text = " ".join(getattr(c, "text", "") for c in result.content)
        assert text.startswith("Error executing tool list_shares: boom for url ")
        assert "S3cretPW" not in text
        assert "passwd=***" in text


class TestNetworkErrorNamesTheCall:
    """QA R1 on #239: a network error in a tool names the DSM API and method (#237)."""

    @respx.mock
    async def test_tool_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SYNOLOGY_USERNAME", "chris")
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "pw")
        respx.get(f"{BASE_URL}/webapi/query.cgi").respond(json=TestNoSecretsInErrors._INFO)
        respx.post(f"{BASE_URL}/webapi/auth.cgi").respond(
            json={"success": True, "data": {"sid": "sid"}}
        )
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=httpx.ReadTimeout(""))
        server = create_server(make_test_config())
        async with Client(server) as client:
            result = await client.call_tool("list_shares", {})
        text = " ".join(getattr(c, "text", "") for c in result.content)
        err = json.loads(text.removeprefix("Error executing tool list_shares: "))["error"]
        assert err["code"] == "timeout"
        assert err["message"] == (
            "DSM didn't answer SYNO.FileStation.List/list_share in time (ReadTimeout)."
        )


class TestToolErrorDetail:
    """mcp 2.x sends only "Error executing tool <name>" for an exception that isn't a
    ToolError; v1 included the message. Login failures escape as AuthenticationError,
    so without the server's call_tool() override the model would lose "run mcp-synology
    setup" and every other auth hint (#114)."""

    @staticmethod
    async def _call(exc: Exception) -> str:
        server = create_server(make_test_config())
        with patch.object(SharedClientManager, "get_client", side_effect=exc):
            async with Client(server) as client:
                result = await client.call_tool("list_shares", {})
        assert result.is_error
        return " ".join(getattr(c, "text", "") for c in result.content)

    @staticmethod
    def _structured(text: str) -> dict[str, Any]:
        """The error envelope, after the prefix every tool error carries (QA round 1, #226)."""
        prefix = "Error executing tool list_shares: "
        assert text.startswith(prefix), text
        error: dict[str, Any] = json.loads(text.removeprefix(prefix))["error"]
        return error

    @staticmethod
    def _tool_log(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
        (record,) = [r for r in caplog.records if r.name.startswith("mcp") and "Tool" in r.msg]
        return record

    async def test_login_failure_is_a_structured_auth_error(self) -> None:
        from mcp_synology.core.errors import AuthenticationError

        text = await self._call(
            AuthenticationError(
                "No credentials found.", suggestion="Run 'mcp-synology setup' to store them."
            )
        )
        err = self._structured(text)
        assert err["code"] == "auth_failed"
        assert "No credentials found." in err["message"]
        assert "list_shares" in err["message"]
        assert err["suggestion"] == "Run 'mcp-synology setup' to store them."
        assert err["help_url"].endswith("#auth_failed")

    async def test_other_synology_error_keeps_its_code(self) -> None:
        from mcp_synology.core.errors import DsmUnavailableError

        text = await self._call(DsmUnavailableError("DSM is restarting", code=150))
        err = self._structured(text)
        assert err["code"] == "unavailable"
        assert "DSM error 150" in err["message"]
        assert err["retryable"] is False  # the class's own flag, carried through

    async def test_unexpected_exception_keeps_its_message(self) -> None:
        text = await self._call(KeyError("model"))
        assert text == "Error executing tool list_shares: 'model'"

    async def test_unexpected_exception_is_logged_as_a_crash(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A real bug must reach the server log at ERROR with its traceback (QA round 1)."""
        with caplog.at_level(logging.INFO, logger="mcp"):
            await self._call(KeyError("model"))
        record = self._tool_log(caplog)
        assert record.levelno == logging.ERROR
        assert record.exc_info is not None
        assert isinstance(record.exc_info[1].__cause__, KeyError)

    async def test_synology_error_is_logged_as_anticipated(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from mcp_synology.core.errors import AuthenticationError

        with caplog.at_level(logging.INFO, logger="mcp"):
            await self._call(AuthenticationError("No credentials found."))
        record = self._tool_log(caplog)
        assert record.levelno == logging.INFO
        assert not record.exc_info

    async def test_network_timeout_is_a_structured_timeout(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """httpx timeouts stringify to '': the model got "Error executing tool
        delete_files: " with nothing after it (#237)."""
        with caplog.at_level(logging.INFO, logger="mcp"):
            text = await self._call(httpx.ReadTimeout(""))
        err = self._structured(text)
        assert err["code"] == "timeout"
        assert err["message"] == "DSM didn't answer in time (ReadTimeout)."
        assert err["retryable"] is True
        assert "connection.timeout" in err["suggestion"]
        assert self._tool_log(caplog).levelno == logging.INFO  # anticipated, not a crash

    async def test_dropped_connection_is_unavailable(self) -> None:
        text = await self._call(httpx.RemoteProtocolError("Server disconnected"))
        err = self._structured(text)
        assert err["code"] == "unavailable"
        assert err["message"] == (
            "The connection to DSM failed (RemoteProtocolError: Server disconnected)."
        )

    async def test_crash_with_empty_message_names_the_exception(self) -> None:
        text = await self._call(RuntimeError(""))
        assert text == "Error executing tool list_shares: RuntimeError"

    async def test_tool_error_passes_through_unchanged(self) -> None:
        from mcp.server.mcpserver.exceptions import ToolError

        text = await self._call(ToolError('{"status": "error", "error": {"code": "timeout"}}'))
        assert text.endswith('{"status": "error", "error": {"code": "timeout"}}')
        assert text.count("Error executing tool") == 1


class TestServedSchemas:
    """Every tool's advertised schema is compacted, and nothing a model needs is lost (#163)."""

    async def test_client_sees_no_titles_or_null_unions(self) -> None:
        tools, _ = await _served(create_server(make_test_config(modules=ALL_MODULES)))
        assert len(tools) == 59
        for tool in tools:
            for node in _walk(tool.input_schema):
                if isinstance(node, dict):
                    assert "title" not in node, tool.name
                    assert {"type": "null"} not in node.get("anyOf", []), tool.name

    @staticmethod
    def _expected(node: Any) -> Any:
        """Independent statement of the compaction rule (QA round 1, #164): drop `title`
        keys; turn {"anyOf": [T, {"type": "null"}], "default": null, ...rest} into
        {...T, ...rest}; leave everything else exactly as it was."""
        if isinstance(node, list):
            return [TestServedSchemas._expected(v) for v in node]
        if not isinstance(node, dict):
            return node
        kept = {k: TestServedSchemas._expected(v) for k, v in node.items() if k != "title"}
        branches = kept.get("anyOf")
        if (
            isinstance(branches, list)
            and len(branches) == 2
            and {"type": "null"} in branches
            and kept.get("default", "unset") is None
        ):
            (real,) = [b for b in branches if b != {"type": "null"}]
            rest = {k: v for k, v in kept.items() if k not in ("anyOf", "default")}
            return {**real, **rest}
        return kept

    async def test_nothing_else_lost(self) -> None:
        server = create_server(make_test_config(modules=ALL_MODULES))
        compacted = {t.name: t.input_schema for t in await server.list_tools()}
        raw = {t.name: t.input_schema for t in await MCPServer.list_tools(server)}
        assert compacted.keys() == raw.keys()
        for name, schema in raw.items():
            # Exhaustive: every key the rule doesn't remove (items, enum, description,
            # examples, required, ...) must survive unchanged.
            assert compacted[name] == self._expected(schema), name

    @respx.mock
    async def test_explicit_null_survives_a_real_tool_call(self) -> None:
        """Review finding: the null claim must hold through MCP's call path, not just
        the pydantic model, so an SDK that starts validating the advertised schema
        (#114) fails here instead of silently rejecting clients that send nulls."""
        respx.get(f"{BASE_URL}/webapi/query.cgi").mock(side_effect=httpx.ConnectError("down"))
        server = create_server(make_test_config(modules=ALL_MODULES))
        async with Client(server) as client:
            result = await client.call_tool("search_files", {"folder_path": "/x", "pattern": None})
        text = " ".join(getattr(c, "text", "") for c in result.content)
        assert result.is_error
        assert "validation error" not in text.lower(), text
        assert "unavailable" in text, text  # got past validation and reached the NAS call

    async def test_validation_still_accepts_explicit_null_and_omission(self) -> None:
        server = create_server(make_test_config(modules=ALL_MODULES))
        tool = server._tool_manager.get_tool("search_files")
        assert tool is not None
        arg_model = tool.fn_metadata.arg_model
        explicit = arg_model.model_validate({"folder_path": "/x", "pattern": None})
        omitted = arg_model.model_validate({"folder_path": "/x"})
        assert explicit.pattern is None
        assert omitted.pattern is None
        (served,) = [t for t in await server.list_tools() if t.name == "search_files"]
        assert json.dumps(served.input_schema).count('"null"') == 0


class TestInstructionComments:
    """HTML comments are notes for whoever edits the file; models never get them (#163)."""

    def test_builtin_template_comment_not_served(self) -> None:
        instructions = create_server(make_test_config()).instructions or ""
        assert "<!--" not in instructions
        assert "Template variables available" not in instructions
        assert "SERVER IDENTITY" in instructions  # text around the comment survives

    def test_custom_instructions_comment_with_unknown_placeholder(self) -> None:
        config = make_test_config(custom_instructions="RULES <!-- uses {not_a_var} -->\nMORE")
        instructions = create_server(config).instructions or ""
        assert "<!--" not in instructions
        assert "RULES \nMORE" in instructions

    def test_instructions_file_comment_removed(self, tmp_path: Path) -> None:
        path = tmp_path / "custom.md"
        path.write_text("TOP for {display_name}\n<!-- editor note {host} -->\nBOTTOM\n")
        instructions = create_server(make_test_config(instructions_file=str(path))).instructions
        assert instructions == "TOP for test\nBOTTOM"

    def test_strip_keeps_line_structure(self) -> None:
        assert _strip_html_comments("A\n<!-- c -->\nB") == "A\nB"
        assert _strip_html_comments("A<!-- c -->B") == "AB"
        assert _strip_html_comments("A <!--\nmulti\nline\n-->\nB") == "A \nB"

    def test_unclosed_comment_left_alone(self) -> None:
        assert _strip_html_comments("keep <!-- never closed\nB") == "keep <!-- never closed\nB"
        config = make_test_config(custom_instructions="NOTE <!-- open")
        assert "NOTE <!-- open" in (create_server(config).instructions or "")

    def test_trailing_comment_keeps_line_break(self) -> None:
        # Review finding: a comment that ends a line must not join it to the next.
        text = "- Use for media <!-- per Bob -->\n- Never delete"
        assert _strip_html_comments(text) == "- Use for media \n- Never delete"

    def test_indented_own_line_comment_removes_whole_line(self) -> None:
        assert _strip_html_comments("x\n  <!-- c -->\nz") == "x\nz"

    def test_comment_with_trailing_text_does_not_swallow_later_lines(self) -> None:
        # QA round 1 (#164): a line-leading comment followed by text must not let the
        # whole-line alternative run on to a later "-->" and delete everything between.
        text = "<!-- note --> keep this\nmore text\n<!-- other -->\nend\n"
        assert _strip_html_comments(text) == " keep this\nmore text\nend\n"

    def test_guardrail_between_comments_survives(self) -> None:
        text = "A\n  <!-- a --> B\nC\n<!-- b -->\nD\n"
        assert _strip_html_comments(text) == "A\n   B\nC\nD\n"

    def test_text_without_comments_is_unchanged(self) -> None:
        text = "PATH FORMAT:\nAll paths start with a share.\n"
        assert _strip_html_comments(text) == text


# Characters a model receives before any conversation (schemas + descriptions + instructions)
# for ALL_MODULES. Raise this deliberately, in the same PR, when adding text a model needs
# (e.g. #129's parameter descriptions); the review then sees the growth. Set by #163 to the
# compacted size + 2%. Raised by #245 (file_task and the "Long operations" instructions) to
# the size scripts/measure_tools.py reports: 15,831 -> 17,327 (the last 141 tell the model
# not to stop and restart a delete whose count hasn't moved). Raised by #257 to 17,582: the
# LOCAL PATHS section names create_download's torrent folders and the top-level setting, and
# says a module's list replaces the Downloads default when there's no top-level list.
# Raised by #258 to 29,742 (+12,160): the opt-in Photos module's 19 tools and its
# instructions section. A connection without Photos enabled doesn't carry them.
# Raised by #296 to 29,886 (+144): create_download's description says the destination must
# be an existing folder and to create a new one first with File Station's create_folder, or
# ask the user to create it in DSM.
# Raised by #308 to 29,930 (+44): upload_photos' description says the album must already
# exist and to create it first with photo_album.
# Raised by #309 to 29,993 (+63): search_files' description says mtime_from is inclusive and
# mtime_to exclusive.
# Raised by #313 to 30,182 (+189): Photos' descriptions and instructions name File
# Station's tools as such, and what to do without them (#304).
# Set by #311 to 30,174 (-8): measured with a fixed home directory, /fake/home. The
# budgets above were measured with CI's /home/runner, which the instructions name 4 times.
# Raised by #317 to 30,220 (+46): the instructions are split by module, and only the
# enabled modules' sections are sent. With every module enabled that adds a "FILE STATION
# (...)" heading (+67), shortens the first line (-20) and drops a blank line (-1). A
# connection without File Station sends about 3,300-3,450 characters fewer.
# Raised by #317 (#318) to 30,305 (+85): File Station's and Photos' WRITE-tool guidance is
# sent only when those tools are registered, under headings of its own (UPLOADING,
# RESTORING DELETED FILES). A File Station read connection sends 1,396 characters fewer.
# Lowered by #317 to 30,247 (-58): list_photos, list_recycle_bin and get_resource_usage no
# longer point at tools a connection may not have (tag_photos and restoring at read; File
# Station's search and copies).
# Raised by #327 to 30,333 (+86): download_file's description says a folder downloads
# as <folder>.zip, not a whole shared folder (#330).
# Raised by #332 to 30,428 (+95): get_dir_size's description says it counts only what the
# DSM user can read, leaving out a subfolder it can't read without notice (#336).
# Raised by #342 to 30,465 (+37): edit_photo's description says taken_time is a local
# date-time with no zone, an offset ignored (Photos stores wall-clock time). Then to
# 30,575 (+110): photo_share_link's and photo_request's descriptions say expires takes a
# date (the end of that day, UTC) or a date-time with an offset. Then to 30,582 (+7):
# photo_request's names never too.
# Raised by #350 to 30,874 (+292): a TIMES section says UTC times are exact instants to
# give in the user's zone and zone-less ones local clock times; the Photos section says
# taken times are the photo's own clock time, not to be converted; set_schedule's plan
# is in the NAS's local hours.
# Raised by #352 to 31,239 (+365): list_files' and list_recycle_bin's descriptions say
# DSM sorts folders and files separately; list_recycle_bin's also says a pattern
# searches the whole bin and what Modified and ctime mean there (#333).
# Raised by #356 to 31,302 (+63): photo_share_link's description says setting a
# password or an expiry needs public view or download (#348).
# Raised by #364 to 31,347 (+45): list_recycle_bin's offset parameter, which its
# "Use offset=N to see more" hint names.
# Raised by #373 (#247) to 50,126 (+18,779, +60%; maintainer-approved): every tool parameter
# gets a schema description, 182 of them where only file_task's 2 had one (~14,500
# characters of text, ~3,500 of `"description"` keys), and Glama's lowest-scoring File
# Station descriptions were rewritten (list_recycle_bin, list_files, create_folder,
# delete_files, restore_from_recycle_bin). Prose that only repeated a tool's own schema
# went (Download Station's descriptions -592 net). Facts were not moved into the
# instructions: claude.ai's desktop app doesn't receive them, and an instructions_file
# replaces them, so a tool's own text has to carry what the model needs to use it.
# Raised by #376 to 50,322 (+196): list_downloads' status_filter names every state in its
# groups (finishing, hash checking, waiting for a file host), and list_recycle_bin's
# description and pattern say the search stops at 200 folders and 5,000 items, and that the
# reply says when it stops early (#375).
# Lowered by #377 to 48,445 (-1,877; and outputSchema is now counted): tools are registered
# as unstructured, so none has the SDK's generated {"result": string} outputSchema (6,937
# characters on the wire that this budget didn't count) and each reply is sent once, not
# again as structuredContent. filestation.md and filestation-write.md keep only what no
# single tool says (path format, the default download folder, the #recycle folder,
# organizing files, long operations); the rest is in the tools' own descriptions since #247.
# Raised by #390 to 49,094 (+649): the five local-path parameters (download_file,
# upload_file, create_download, download_photos, upload_photos) name the allowed folders
# themselves instead of pointing to the instructions, which claude.ai never receives
# (0.10.0rc6 live test); get_system_info's description lists everything it returns now
# (every field DSM sends, the NAS's local time and code page included) and what only an
# administrator gets; get_resource_usage says it needs one; edit_photo's description says
# one of only spaces shows as "—".
# Raised by #401 to 49,314 (+220): get_system_info's description says the network-card
# flag comes only from DSM 7.2.2 and later and redundant power only on models that report
# it (the DS1618+ on DSM 7.1.1 sends neither; 0.10.0rc7 answer key). +163 in QA round 1:
# the Photos `ids` and `folders` parameters state their 1,000-per-call limit, which only
# the error used to give; list_photo_collections' limit reads 1-1,000.
# Raised by #417 to 49,659 (+345): upload_photos' description says it starts no new file
# after the task timeout (at most 45 s) and lists the files not tried (#416), and
# file_task's and photo_task's say not to poll in a loop, guidance that lived only in the
# instructions, which a client in the 0.10.0rc9 live test never received (#382).
# Raised by #418 to 49,882 (+223): pause_download says a finished task is
# reported as "not paused: already finished" instead of promising error 405 (Download
# Station 3.9.5 accepts the pause), edit_download says a finished task's file isn't moved,
# and download_photos' size says the preview sizes are the short side, never larger than
# the original (0.10.0rc9 live test).
# Raised by #422 to 49,963 (+81): pause_download says its per-task refusals happen only
# when something else in the call can be paused, and that a lone finished or
# already-paused task fails the whole call (#421, 0.10.0rc10 live test).
# Raised by #49 to 55,411 (+5,448): the Storage module's 11 READ tools and its
# instructions section (the administrator requirement). A connection without Storage
# carries only its one-line "not enabled" entry in PERMISSIONS (about 160 characters).
# +72 in QA round 1: list_snapshots' offset and limit say they page the shared folders
# when no folder is given.
# +30 by #397: create_download's `uri` says commas inside a URI are kept.
PAYLOAD_BUDGET_CHARS = 55_513


class TestPayloadBudget:
    async def test_payload_within_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The LOCAL PATHS instructions name folders under the home directory, so measure
        # with a fixed one (#311). It mustn't exist: a real path could be a symlink (on
        # Fedora Silverblue /home is one), which LOCAL PATHS resolves, or hold a
        # user-dirs.dirs naming another Downloads folder.
        monkeypatch.setenv("HOME", "/fake/home")
        monkeypatch.setenv("USERPROFILE", "/fake/home")
        tools, instructions = await _served(create_server(make_test_config(modules=ALL_MODULES)))
        size = sum(
            len(json.dumps(t.input_schema))
            + len(t.description or "")
            + (len(json.dumps(t.output_schema)) if t.output_schema else 0)
            for t in tools
        )
        size += len(instructions)
        assert size <= PAYLOAD_BUDGET_CHARS, (
            f"served payload is {size} chars, budget {PAYLOAD_BUDGET_CHARS} "
            f"(+{size - PAYLOAD_BUDGET_CHARS}). If the growth is intended, raise "
            "PAYLOAD_BUDGET_CHARS in this PR."
        )


class TestTextOnlyResults:
    """Tools are unstructured (#377): no outputSchema in the list, each reply sent once."""

    async def test_no_served_tool_has_an_output_schema(self) -> None:
        tools, _ = await _served(create_server(make_test_config(modules=ALL_MODULES)))
        with_schema = [t.name for t in tools if t.output_schema]
        assert not with_schema, f"tools with an outputSchema: {with_schema}"

    async def test_a_text_reply_is_sent_once(self) -> None:
        server = _SynologyServer("t")

        @server.tool(name="echo")
        async def echo(text: str) -> str:
            return f"you said {text}"

        async with Client(server) as client:
            result = await client.call_tool("echo", {"text": "hi"})
        assert [c.text for c in result.content] == ["you said hi"]  # type: ignore[union-attr]
        assert result.structured_content is None

    async def test_an_explicit_structured_tool_stays_structured(self) -> None:
        server = _SynologyServer("t")

        @server.tool(name="echo", structured_output=True)
        async def echo(text: str) -> str:
            return text

        tools, _ = await _served(server)
        assert tools[0].output_schema is not None


class TestParameterDescriptions:
    """Every tool parameter tells the model what it is (#247): the description is the
    only place a model learns a parameter's format, values and default meaning."""

    async def test_every_parameter_has_a_description(self) -> None:
        tools, _ = await _served(create_server(make_test_config(modules=ALL_MODULES)))
        missing = [
            f"{tool.name}.{name}"
            for tool in tools
            for name, schema in tool.input_schema.get("properties", {}).items()
            if not str(schema.get("description", "")).strip()
        ]
        assert not missing, f"parameters without a description: {missing}"

    async def test_local_path_parameters_name_the_allowed_folders(self, tmp_path: Path) -> None:
        """claude.ai never receives the instructions' LOCAL PATHS (0.10.0rc5 and rc6 live
        tests), so each parameter that takes a local path names the folders itself."""
        shared = tmp_path / "shared"
        config = make_test_config(modules=ALL_MODULES, allowed_local_paths=[str(shared)])
        tools, _ = await _served(create_server(config))
        params = {
            ("download_file", "dest_folder"),
            ("upload_file", "local_path"),
            ("create_download", "torrent_file_path"),
            ("download_photos", "dest_folder"),
            ("upload_photos", "local_paths"),
        }
        described = {
            (t.name, name): str(schema.get("description", ""))
            for t in tools
            for name, schema in t.input_schema.get("properties", {}).items()
            if (t.name, name) in params
        }
        assert set(described) == params
        missing = [p for p, text in described.items() if str(shared.resolve()) not in text]
        assert not missing, f"descriptions that don't name the allowed folder: {missing}"

    async def test_photos_id_lists_state_their_limit(self) -> None:
        """check_ids refuses more than 1,000 IDs or folders (#401, QA round 1): each
        parameter it checks says so, so the model doesn't learn it from the error."""
        tools, _ = await _served(create_server(make_test_config(modules=ALL_MODULES)))
        params = {
            ("tag_photos", "ids"),
            ("edit_photo", "ids"),
            ("delete_photos", "ids"),
            ("photo_album", "ids"),
            ("copy_photos", "ids"),
            ("copy_photos", "folders"),
            ("move_photos", "ids"),
            ("move_photos", "folders"),
            ("delete_photo_folders", "folders"),
        }
        described = {
            (t.name, name): str(schema.get("description", ""))
            for t in tools
            for name, schema in t.input_schema.get("properties", {}).items()
            if (t.name, name) in params
        }
        assert set(described) == params
        missing = [p for p, text in described.items() if "1,000" not in text]
        assert not missing, f"descriptions that don't state the 1,000 limit: {missing}"

    async def test_edit_photo_says_whitespace_reads_as_none(self) -> None:
        """get_photo shows a description of only spaces as "—" (#370); edit_photo says so,
        since Muse found it surprising in the 0.10.0rc6 live test."""
        tools, _ = await _served(create_server(make_test_config(modules=ALL_MODULES)))
        edit = next(t for t in tools if t.name == "edit_photo")
        text = edit.input_schema["properties"]["description"]["description"]
        assert "only spaces" in text and "—" in text

    async def test_no_description_relies_on_the_instructions(self) -> None:
        """A tool's own text carries what the model needs (#247): some clients drop the
        instructions, so no description sends the model to them."""
        tools, _ = await _served(create_server(make_test_config(modules=ALL_MODULES)))
        texts = [(t.name, t.description or "") for t in tools] + [
            (f"{t.name}.{name}", str(schema.get("description", "")))
            for t in tools
            for name, schema in t.input_schema.get("properties", {}).items()
        ]
        pointing = [name for name, text in texts if "instructions" in text.lower()]
        assert not pointing, f"descriptions that point to the server instructions: {pointing}"


class TestLocalPathsNotice:
    """The model is told which local folders upload_file/download_file may use, so it
    picks a path that works instead of discovering the limit from a refusal."""

    def _instructions(self, settings: dict[str, Any], permission: str = "write") -> str:
        config = make_test_config(
            modules={
                "filestation": {"enabled": True, "permission": permission, "settings": settings}
            }
        )
        return create_server(config).instructions or ""

    def test_lists_the_allowed_folders(self, tmp_path: Path) -> None:
        text = self._instructions({"allowed_local_paths": [str(tmp_path / "media")]})
        section = text.split("LOCAL PATHS", 1)[1]
        assert str((tmp_path / "media").resolve()) in section
        assert "allowed_local_paths" in section

    def test_lists_the_shared_folders(self, tmp_path: Path) -> None:
        """#257: the top-level list alone decides the folders (no Downloads default)."""
        config = make_test_config(allowed_local_paths=[str(tmp_path / "shared")])
        section = (create_server(config).instructions or "").split("LOCAL PATHS", 1)[1]
        assert str((tmp_path / "shared").resolve()) in section
        from mcp_synology.core.local_paths import default_downloads_dir

        assert str(default_downloads_dir().resolve()) not in section

    def test_download_station_torrent_files_are_named(self, tmp_path: Path) -> None:
        """#257: with both modules, the section names each module's folders."""
        config = make_test_config(
            allowed_local_paths=[str(tmp_path / "shared")],
            modules={
                "filestation": {"enabled": True, "permission": "write"},
                "downloadstation": {
                    "enabled": True,
                    "permission": "write",
                    "settings": {"allowed_local_paths": [str(tmp_path / "torrents")]},
                },
            },
        )
        section = (create_server(config).instructions or "").split("LOCAL PATHS", 1)[1]
        assert "create_download" in section
        assert "torrent_file_path" in section
        assert str((tmp_path / "torrents").resolve()) in section
        assert "top level" in section

    def test_photos_folders_are_named(self, tmp_path: Path) -> None:
        """#258: upload_photos reads from and download_photos writes to Photos' folders."""
        config = make_test_config(
            modules={
                "photos": {
                    "enabled": True,
                    "permission": "write",  # upload_photos is a WRITE tool (#318)
                    "settings": {"allowed_local_paths": [str(tmp_path / "pics")]},
                }
            }
        )
        section = (create_server(config).instructions or "").split("LOCAL PATHS", 1)[1]
        assert "upload_photos and download_photos" in section
        assert str((tmp_path / "pics").resolve()) in section

    def test_read_only_connection_still_gets_it_for_download_file(self, tmp_path: Path) -> None:
        text = self._instructions({"allowed_local_paths": [str(tmp_path)]}, permission="read")
        assert "LOCAL PATHS" in text

    def test_no_transfer_tools_means_no_section(self) -> None:
        config = make_test_config(modules={"system": {"enabled": True, "permission": "read"}})
        assert "LOCAL PATHS" not in (create_server(config).instructions or "")

    def test_instructions_file_is_left_alone(self, tmp_path: Path) -> None:
        f = tmp_path / "mine.md"
        f.write_text("MINE", encoding="utf-8")
        config = make_test_config(instructions_file=str(f))
        assert "LOCAL PATHS" not in (create_server(config).instructions or "")


class TestPhotosNotice:
    """#258: the Photos section comes with the module, so connections without it don't
    carry its text."""

    def test_present_when_enabled(self) -> None:
        config = make_test_config(modules={"photos": {"enabled": True, "permission": "write"}})
        text = create_server(config).instructions or ""
        section = text.split("SYNOLOGY PHOTOS:", 1)[1]
        assert "space=shared" in section
        assert "confirm with the user first" in section
        assert "photo_task" in section
        # #304: File Station's tools may not be on this connection, so they're named as
        # File Station's, and the section doesn't lean on file_task.
        flat = " ".join(section.split())
        assert "File Station's tools take paths, when that module is enabled" in flat
        assert "file_task" not in section
        assert (
            "Taken times are the photo's own local clock time (the camera's, or the NAS's for "
            "a file without EXIF): don't convert them." in flat
        )

    def test_absent_otherwise(self) -> None:
        assert "SYNOLOGY PHOTOS" not in (create_server(make_test_config()).instructions or "")


class TestUpdateNoticeDelivery:
    """The update notice reaches every module's tools and the instructions (#314)."""

    @staticmethod
    def _server(notice: str | None) -> _SynologyServer:
        from mcp.server.mcpserver.exceptions import ToolError

        server = _SynologyServer("notice-test")
        manager = SharedClientManager(make_test_config(check_for_updates=False))
        manager._update_notice, manager._pending_version = notice, "9.9.9"
        server.update_notices = manager

        @server.tool()
        def get_resource_usage() -> str:  # a System tool, not File Station
            return "CPU: 5%"

        @server.tool()
        def broken() -> str:
            raise ToolError("nope")

        return server

    async def test_the_first_result_of_any_tool_carries_it_once(self) -> None:
        server = self._server("Update!")
        async with Client(server) as client:
            first = await client.call_tool("get_resource_usage", {})
            second = await client.call_tool("get_resource_usage", {})
        assert first.content[0].text == "CPU: 5%\n\n---\nUpdate!"  # type: ignore[union-attr]
        assert second.content[0].text == "CPU: 5%"  # type: ignore[union-attr]

    async def test_a_failing_notice_never_fails_the_tool(self) -> None:
        server = self._server("Update!")
        with patch("mcp_synology.cli.version.claim_announcement", side_effect=TypeError("bad")):
            async with Client(server) as client:
                result = await client.call_tool("get_resource_usage", {})
        assert not result.is_error
        assert result.content[0].text == "CPU: 5%"  # type: ignore[union-attr]

    async def test_an_error_result_carries_it_too(self) -> None:
        server = self._server("Update!")
        async with Client(server) as client:
            result = await client.call_tool("broken", {})
        assert result.is_error
        text = result.content[0].text  # type: ignore[union-attr]
        assert "nope" in text
        assert text.endswith("\n\n---\nUpdate!")

    async def test_no_notice_leaves_results_alone(self) -> None:
        async with Client(self._server(None)) as client:
            result = await client.call_tool("get_resource_usage", {})
        assert result.content[0].text == "CPU: 5%"  # type: ignore[union-attr]

    @staticmethod
    def _cache(latest: str, **extra: Any) -> None:
        from mcp_synology.cli import _save_global_state

        _save_global_state({"latest_known_version": latest, **extra})

    def test_a_corrupt_global_state_does_not_stop_the_server(self) -> None:
        # Every instance shares the file; a bad write mustn't stop them all (QA round 1).
        from mcp_synology.cli.version import _global_state_path

        path = _global_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for content in (b"- a\n", b"a string\n", b"\xff\xfe\n"):
            path.write_bytes(content)
            create_server(make_test_config(check_for_updates=True))

    def test_instructions_leave_out_an_ordinary_release(self) -> None:
        # An MCP client keeps a server across chats: the instructions reach every chat.
        self._cache("99.0.0")
        text = create_server(make_test_config(check_for_updates=True)).instructions or ""
        assert "UPDATE AVAILABLE" not in text

    def test_instructions_carry_a_security_release_every_time(self) -> None:
        from mcp_synology.cli import _load_global_state

        self._cache("99.0.0", latest_is_security=True)
        for _ in range(2):
            text = create_server(make_test_config(check_for_updates=True)).instructions or ""
            expected = "UPDATE AVAILABLE:\nA security release of mcp-synology is available: 99.0.0"
            assert expected in text
        assert "announced" not in _load_global_state()  # nothing to claim: always said

    @pytest.mark.parametrize(
        ("latest", "overrides"),
        [
            ("0.0.1", {}),
            ("99.0.0", {"check_for_updates": False}),
            ("99.0.0", {"update_notices": "off"}),
        ],
    )
    def test_no_instructions_line(self, latest: str, overrides: dict[str, Any]) -> None:
        self._cache(latest, latest_is_security=True)
        config = make_test_config(**{"check_for_updates": True, **overrides})
        assert "UPDATE AVAILABLE" not in (create_server(config).instructions or "")

    @pytest.mark.parametrize(
        ("setting", "security", "queued"),
        [
            ("all", False, True),
            ("all", True, True),
            ("security", False, False),
            ("security", True, True),
            ("off", True, False),
        ],
    )
    async def test_update_notices_setting(self, setting: str, security: bool, queued: bool) -> None:
        manager = SharedClientManager(
            make_test_config(check_for_updates=True, update_notices=setting)
        )
        with (
            patch(
                "mcp_synology.cli.version.refresh_update_check",
                return_value=("9.9.9", security, {"latest_is_security": security}),
            ),
        ):
            await manager._bg_update_check()
        assert (manager._update_notice is not None) is queued

    def test_the_session_does_not_announce_the_same_version_twice(self) -> None:
        self._cache("99.0.0", latest_is_security=True)
        server = create_server(make_test_config(check_for_updates=True))
        assert isinstance(server, _SynologyServer)
        assert server.update_notices is not None
        assert server.update_notices._announced_version == "99.0.0"


class TestUpdateNoticeEdges:
    """Results that can't carry text, and a claim that fails (pre-QA round 2, #314)."""

    @staticmethod
    def _manager() -> SharedClientManager:
        manager = SharedClientManager(make_test_config(check_for_updates=False))
        manager._update_notice, manager._pending_version = "Update!", "9.9.9"
        return manager

    async def test_a_result_without_text_gets_its_own_text_block(self) -> None:
        from mcp.types import CallToolResult, ImageContent

        server = _SynologyServer("edges")
        server.update_notices = self._manager()
        image = ImageContent(type="image", data="aGk=", mime_type="image/png")
        with patch.object(
            _SynologyServer, "_call_tool", return_value=CallToolResult(content=[image])
        ):
            result = await server.call_tool("t", {})
        assert isinstance(result, CallToolResult)
        assert result.content[0] == image
        assert result.content[1].text == "\n\n---\nUpdate!"  # type: ignore[union-attr]

    async def test_a_non_call_result_leaves_the_notice_for_later(self) -> None:
        server = _SynologyServer("edges")
        manager = self._manager()
        server.update_notices = manager
        other = MagicMock()  # e.g. an InputRequiredResult
        with patch.object(_SynologyServer, "_call_tool", return_value=other):
            assert await server.call_tool("t", {}) is other
        assert manager._update_notice == "Update!"

    async def test_a_failed_claim_keeps_the_notice(self) -> None:
        manager = self._manager()
        with (
            patch("mcp_synology.cli.version.claim_announcement", side_effect=OSError("lock")),
            pytest.raises(OSError),
        ):
            await manager.take_update_notice()
        assert manager._update_notice == "Update!"
        assert manager._announced_version is None
