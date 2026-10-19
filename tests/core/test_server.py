"""Tests for server.py — server creation and tool registration."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from mcp_synology.server import (
    _BASE_INSTRUCTIONS,
    SharedClientManager,
    _compact_schema,
    _strip_html_comments,
    create_server,
)
from tests.conftest import BASE_URL, make_test_config

if TYPE_CHECKING:
    from collections.abc import Iterator
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

    async def test_bg_update_check_swallows_errors(self, caplog: pytest.LogCaptureFixture) -> None:
        """OSError/ValueError/KeyError during update check must not raise,
        AND the actual exception must be logged at DEBUG with exc_info so
        `SYNOLOGY_LOG_LEVEL=debug` surfaces the cause (closes #39, half 1).
        """
        import logging

        config = make_test_config(check_for_updates=True)
        manager = SharedClientManager(config)
        with (
            patch("mcp_synology.cli._load_global_state", side_effect=OSError("disk full")),
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
            patch("mcp_synology.cli._load_global_state", return_value={}),
            patch("mcp_synology.cli._save_global_state"),
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
            }
        )
        assert "PERMISSIONS" not in text

    def test_module_not_enabled_is_named(self) -> None:
        text = self._instructions(modules={"filestation": {"enabled": True, "permission": "write"}})
        assert "downloadstation" in text
        assert "not enabled" in text

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
        assert len(tools) == 48
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
PAYLOAD_BUDGET_CHARS = 29_993


class TestPayloadBudget:
    async def test_payload_within_budget(self) -> None:
        tools, instructions = await _served(create_server(make_test_config(modules=ALL_MODULES)))
        size = sum(len(json.dumps(t.input_schema)) + len(t.description or "") for t in tools)
        size += len(instructions)
        assert size <= PAYLOAD_BUDGET_CHARS, (
            f"served payload is {size} chars, budget {PAYLOAD_BUDGET_CHARS} "
            f"(+{size - PAYLOAD_BUDGET_CHARS}). If the growth is intended, raise "
            "PAYLOAD_BUDGET_CHARS in this PR."
        )


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
                    "permission": "read",
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

    def test_absent_otherwise(self) -> None:
        assert "SYNOLOGY PHOTOS" not in (create_server(make_test_config()).instructions or "")
