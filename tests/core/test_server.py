"""Tests for server.py — server creation and tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_synology.server import _BASE_INSTRUCTIONS, SharedClientManager, create_server
from tests.conftest import make_test_config

if TYPE_CHECKING:
    from pathlib import Path


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
        config = make_test_config(modules={"filestation": {"enabled": False}})
        server = create_server(config)
        assert server is not None

    def test_server_with_unknown_module(self) -> None:
        config = make_test_config(
            modules={
                "filestation": {"enabled": True},
                "unknown_module": {"enabled": True},
            }
        )
        server = create_server(config)
        assert server is not None

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
    def test_instructions_mention_path_format(self) -> None:
        assert "PATH FORMAT" in _BASE_INSTRUCTIONS

    def test_instructions_mention_file_sizes(self) -> None:
        assert "FILE SIZES" in _BASE_INSTRUCTIONS

    def test_instructions_mention_recycle_bin(self) -> None:
        assert "RECYCLE BIN" in _BASE_INSTRUCTIONS

    def test_instructions_mention_list_shares_first(self) -> None:
        assert "list_shares" in _BASE_INSTRUCTIONS


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
        # The instructions should contain both our custom text and the base text
        instructions = server.instructions or ""
        assert "EXTRA RULES FOR THIS NAS" in instructions
        # Custom text appears before base
        custom_idx = instructions.index("EXTRA RULES FOR THIS NAS")
        base_idx = instructions.index("PATH FORMAT")
        assert custom_idx < base_idx

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

    def test_instructions_file_missing_falls_back_to_base(self, tmp_path: Path) -> None:
        config = make_test_config(instructions_file=str(tmp_path / "nope.md"))
        server = create_server(config)
        assert server is not None
        instructions = server.instructions or ""
        # Falls back to _BASE_INSTRUCTIONS
        assert "PATH FORMAT" in instructions


class TestSharedClientManagerLifecycle:
    """Direct tests of the SharedClientManager — get_client lazy init,
    with_update_notice clearing behavior, signal handler installation,
    background update check, cleanup_session."""

    def test_with_update_notice_appends_and_clears(self) -> None:
        config = make_test_config()
        manager = SharedClientManager(config)
        manager._update_notice = "\n--- update notice ---"
        # First call: notice appended AND cleared
        result = manager.with_update_notice("hello")
        assert result == "hello\n--- update notice ---"
        assert manager._update_notice is None
        # Second call: notice already cleared, just returns input
        result = manager.with_update_notice("world")
        assert result == "world"

    def test_with_update_notice_no_notice(self) -> None:
        config = make_test_config()
        manager = SharedClientManager(config)
        # _update_notice is None by default; passes input through unchanged
        assert manager.with_update_notice("hello") == "hello"

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

    async def test_get_client_schedules_bg_update_check(self) -> None:
        config = make_test_config(check_for_updates=True)
        manager = SharedClientManager(config)

        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.query_api_info = AsyncMock(return_value={})
        fake_auth = MagicMock()
        fake_auth.login = AsyncMock(return_value="sid")

        async def _no_op() -> None:
            return None

        with (
            patch("mcp_synology.server.DsmClient", return_value=fake_client),
            patch("mcp_synology.server.AuthManager", return_value=fake_auth),
            patch.object(SharedClientManager, "_bg_update_check", return_value=_no_op()),
        ):
            await manager.get_client()
            # Let the scheduled task run to completion so we don't leak it
            if manager._bg_task is not None:
                await manager._bg_task

        assert manager._bg_task is not None

    async def test_get_client_skips_bg_update_check_when_disabled(self) -> None:
        config = make_test_config(check_for_updates=False)
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

    def test_install_cleanup_handlers_registers_atexit_and_signals(self) -> None:
        config = make_test_config()
        manager = SharedClientManager(config)
        with (
            patch("atexit.register") as atexit_register,
            patch("signal.signal") as signal_signal,
        ):
            manager.install_cleanup_handlers()
        atexit_register.assert_called_once_with(manager._cleanup_session)
        # Two signal handlers: SIGTERM, SIGINT
        assert signal_signal.call_count == 2

    def test_signal_handler_calls_cleanup_and_raises_systemexit(self) -> None:
        """Trigger the inner signal handler closure to walk lines 130-132."""
        import signal as signal_mod

        config = make_test_config()
        manager = SharedClientManager(config)
        captured_handlers: dict[int, object] = {}

        def _capture(signum: int, handler: object) -> None:
            captured_handlers[signum] = handler

        with (
            patch("atexit.register"),
            patch("signal.signal", side_effect=_capture),
        ):
            manager.install_cleanup_handlers()

        sigterm_handler = captured_handlers[signal_mod.SIGTERM]
        with patch.object(manager, "_cleanup_session") as cleanup, pytest.raises(SystemExit) as exc:
            sigterm_handler(signal_mod.SIGTERM, None)  # type: ignore[operator]
        cleanup.assert_called_once()
        assert exc.value.code == 128 + int(signal_mod.SIGTERM)

    def test_cleanup_session_returns_early_when_no_auth(self) -> None:
        config = make_test_config()
        manager = SharedClientManager(config)
        # _auth is None by default
        manager._cleanup_session()  # should not raise
        assert manager._auth is None

    def test_cleanup_session_no_running_loop_runs_logout(self) -> None:
        """When there's no event loop, _cleanup_session falls back to asyncio.run."""
        config = make_test_config()
        manager = SharedClientManager(config)
        manager._auth = MagicMock()
        manager._auth.logout = AsyncMock()
        manager._client = MagicMock()
        manager._client.__aexit__ = AsyncMock()

        # Patch asyncio.get_running_loop to raise (no loop) so we hit the fallback
        with patch("asyncio.get_running_loop", side_effect=RuntimeError("no loop")):
            manager._cleanup_session()
        manager._auth.logout.assert_awaited_once()
        manager._client.__aexit__.assert_awaited_once()

    async def test_cleanup_session_with_running_loop_creates_task(self) -> None:
        config = make_test_config()
        manager = SharedClientManager(config)
        manager._auth = MagicMock()
        manager._auth.logout = AsyncMock()
        manager._client = MagicMock()
        manager._client.__aexit__ = AsyncMock()

        # Inside an async test, asyncio.get_running_loop() works → creates a task
        manager._cleanup_session()
        assert manager._cleanup_task is not None
        await manager._cleanup_task
        manager._auth.logout.assert_awaited_once()

    def test_cleanup_session_swallows_logout_errors(self) -> None:
        """Logout failures during shutdown must not raise — best effort."""
        config = make_test_config()
        manager = SharedClientManager(config)
        manager._auth = MagicMock()
        manager._auth.logout = AsyncMock(side_effect=RuntimeError("server died"))
        manager._client = MagicMock()
        manager._client.__aexit__ = AsyncMock(side_effect=RuntimeError("client dead"))

        with patch("asyncio.get_running_loop", side_effect=RuntimeError("no loop")):
            manager._cleanup_session()  # must not raise

    async def test_bg_update_check_with_newer_version(self) -> None:
        config = make_test_config(check_for_updates=True)
        manager = SharedClientManager(config)

        with (
            patch("mcp_synology.cli._load_global_state", return_value={}),
            patch("mcp_synology.cli._save_global_state"),
            patch("mcp_synology.cli._check_for_update", return_value="9.9.9"),
        ):
            await manager._bg_update_check()
        assert manager._update_notice is not None
        assert "9.9.9" in manager._update_notice

    async def test_bg_update_check_no_update(self) -> None:
        config = make_test_config(check_for_updates=True)
        manager = SharedClientManager(config)
        with (
            patch("mcp_synology.cli._load_global_state", return_value={}),
            patch("mcp_synology.cli._save_global_state"),
            patch("mcp_synology.cli._check_for_update", return_value=None),
        ):
            await manager._bg_update_check()
        assert manager._update_notice is None

    async def test_bg_update_check_swallows_errors(self) -> None:
        """OSError/ValueError/KeyError during update check must not raise."""
        config = make_test_config(check_for_updates=True)
        manager = SharedClientManager(config)
        with (
            patch("mcp_synology.cli._load_global_state", side_effect=OSError("disk full")),
        ):
            await manager._bg_update_check()  # must not raise
        assert manager._update_notice is None


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
