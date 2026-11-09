"""Tests for cli/setup.py — interactive setup, credential flow, 2FA bootstrap.

cli/setup.py was at 63% (86/233 missing) after PR #16. The remaining gaps
are concentrated in three async helpers — `_attempt_login`, `_connect_and_login`,
`_setup_login` — plus a handful of small error paths in the synchronous
flow (`_setup_credential_flow`, `_setup_with_config`, the load_config
validation-error branch in the `setup` command itself).

Strategy:
- Async helpers tested directly with `AsyncMock` clients (no respx; the
  client's `request()` method is mocked at the boundary)
- Sync flows tested via `CliRunner` with `input=` and patched module
  globals (`_CONFIG_DIR`, `_store_keyring`, etc.) — same pattern as the
  pre-existing `TestSetupInteractive` class in test_cli.py
- The `_emit_claude_desktop_snippet` Linux fallback (when
  `DBUS_SESSION_BUS_ADDRESS` is unset) is tested by clearing the env var
  and confirming the constructed `unix:path=/run/user/{uid}/bus` value
  appears in the snippet

Coverage target per #14: cli/setup.py 63% → 90%+.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from mcp_synology.cli import setup as setup_mod
from mcp_synology.cli.main import main
from tests.conftest import skip_unless_the_locale_can_encode

if TYPE_CHECKING:
    from pathlib import Path


# Helper used across multiple test classes
def _make_test_app_config(host: str = "1.2.3.4", https: bool = False) -> Any:
    """Build an AppConfig pointing at the given host/protocol."""
    from mcp_synology.core.config import AppConfig

    raw: dict[str, Any] = {
        "schema_version": 1,
        "instance_id": host.replace(".", "-"),
        "connection": {"host": host, "https": https},
        "modules": {"filestation": {"enabled": True}},
    }
    return AppConfig(**raw)


# ---------- _attempt_login ----------


class TestAttemptLogin:
    """Direct tests of the async _attempt_login helper.

    The helper takes a `client` object and calls `client.request("SYNO.API.Auth", "login", ...)`.
    All paths can be exercised by passing a MagicMock with an AsyncMock `request`.
    """

    @staticmethod
    def _make_client(request_mock: AsyncMock) -> MagicMock:
        client = MagicMock()
        client.request = request_mock
        client.sid = None
        return client

    async def test_login_success_no_2fa(self) -> None:
        request = AsyncMock(return_value={"sid": "test-sid-123"})
        client = self._make_client(request)
        result = await setup_mod._attempt_login(client, "admin", "pw")
        assert result["success"] is True
        assert result["sid"] == "test-sid-123"
        assert client.sid == "test-sid-123"

    async def test_login_failure_non_2fa_error(self) -> None:
        from mcp_synology.core.errors import SynologyError

        request = AsyncMock(side_effect=SynologyError("bad password", code=400))
        client = self._make_client(request)
        result = await setup_mod._attempt_login(client, "admin", "pw")
        assert result["success"] is False

    async def test_login_2fa_required_then_success_with_device_token(self) -> None:
        from mcp_synology.core.errors import SynologyError

        # First call: 403 (2FA required). Second call: success with did
        request = AsyncMock(
            side_effect=[
                SynologyError("2fa", code=403),
                {"sid": "sid-after-2fa", "did": "device-token-xyz"},
            ]
        )
        client = self._make_client(request)

        with patch("keyring.set_password") as set_pw:
            runner = CliRunner()
            # Drive the OTP prompt — _attempt_login uses click.prompt internally
            with runner.isolation(input="123456\n"):
                result = await setup_mod._attempt_login(client, "admin", "pw")

        assert result["success"] is True
        assert result["sid"] == "sid-after-2fa"
        # Handed back for the caller to store with the rest: a login writes nothing (#454).
        assert result["device_id"] == "device-token-xyz"
        set_pw.assert_not_called()
        # Both requests issued, each as a POST so the password and OTP code stay
        # out of the URL (#235)
        assert request.await_count == 2
        assert [c.kwargs.get("post_form") for c in request.await_args_list] == [True, True]
        assert request.await_args_list[1].kwargs["params"]["otp_code"] == "123456"

    async def test_login_2fa_required_then_success_without_device_token(self) -> None:
        """2FA succeeds but DSM doesn't echo back a `did`: nothing would let the server log in."""
        from mcp_synology.core.errors import SynologyError

        request = AsyncMock(
            side_effect=[
                SynologyError("2fa", code=403),
                {"sid": "sid-after-2fa"},  # no `did` field
            ]
        )
        client = self._make_client(request)

        runner = CliRunner()
        with runner.isolation(input="123456\n"):
            result = await setup_mod._attempt_login(client, "admin", "pw")

        assert result["success"] is False
        assert "device_id" not in result

    async def test_login_2fa_then_failure(self) -> None:
        from mcp_synology.core.errors import SynologyError

        request = AsyncMock(
            side_effect=[
                SynologyError("2fa", code=403),
                SynologyError("bad otp", code=404),
            ]
        )
        client = self._make_client(request)

        runner = CliRunner()
        with runner.isolation(input="000000\n"):
            result = await setup_mod._attempt_login(client, "admin", "pw")
        assert result["success"] is False

    async def test_login_oserror(self) -> None:
        request = AsyncMock(side_effect=OSError("connection refused"))
        client = self._make_client(request)
        result = await setup_mod._attempt_login(client, "admin", "pw")
        assert result["success"] is False


# ---------- _connect_and_login ----------


class TestConnectAndLogin:
    @staticmethod
    def _make_client_cm(request_mock: AsyncMock, dsm_info: dict[str, str]) -> MagicMock:
        """Construct a fake DsmClient suitable for `async with DsmClient(...) as client`."""
        client = MagicMock()
        client.request = request_mock
        client.sid = None
        client.query_api_info = AsyncMock(return_value=None)
        client.fetch_dsm_info = AsyncMock(return_value=dsm_info)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        return client

    async def test_connect_and_login_success_with_hostname(self) -> None:
        config = _make_test_app_config()
        request = AsyncMock(
            side_effect=[
                {"sid": "test-sid"},  # login
                {},  # logout
            ]
        )
        client = self._make_client_cm(request, {"hostname": "MyNAS", "version_string": "DSM 7.1"})
        with patch("mcp_synology.core.client.DsmClient", return_value=client):
            result = await setup_mod._connect_and_login(
                config, "admin", "pw", lambda _device_id: True, verbose=False
            )
        assert result["success"] is True
        assert result["hostname"] == "MyNAS"
        assert result["dsm_version"] == "DSM 7.1"

    async def test_connect_and_login_success_no_hostname(self) -> None:
        config = _make_test_app_config()
        request = AsyncMock(side_effect=[{"sid": "test-sid"}, {}])
        client = self._make_client_cm(request, {})  # empty dsm_info
        with patch("mcp_synology.core.client.DsmClient", return_value=client):
            result = await setup_mod._connect_and_login(
                config, "admin", "pw", lambda _device_id: True, verbose=False
            )
        assert result["success"] is True
        assert "hostname" not in result

    async def test_connect_and_login_https(self) -> None:
        config = _make_test_app_config(host="nas.example.com", https=True)
        request = AsyncMock(side_effect=[{"sid": "test-sid"}, {}])
        client = self._make_client_cm(request, {})
        with patch("mcp_synology.core.client.DsmClient", return_value=client) as dsm_client:
            await setup_mod._connect_and_login(
                config, "admin", "pw", lambda _device_id: True, verbose=False
            )
        # Confirm https URL was constructed
        kwargs = dsm_client.call_args.kwargs
        assert kwargs["base_url"].startswith("https://")

    async def test_connect_and_login_login_failure(self) -> None:
        from mcp_synology.core.errors import SynologyError

        config = _make_test_app_config()
        request = AsyncMock(side_effect=SynologyError("bad creds", code=400))
        client = self._make_client_cm(request, {})
        with patch("mcp_synology.core.client.DsmClient", return_value=client):
            result = await setup_mod._connect_and_login(
                config, "admin", "pw", lambda _device_id: True, verbose=False
            )
        assert result["success"] is False

    async def test_connect_and_login_logs_out_when_dsm_info_fails(self) -> None:
        # #404: a transport error from fetch_dsm_info used to skip the logout. #454: it costs
        # only the alias suggestion, since the login worked.
        import httpx

        config = _make_test_app_config()
        request = AsyncMock(side_effect=[{"sid": "test-sid"}, {}])
        client = self._make_client_cm(request, {})
        client.fetch_dsm_info = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        with patch("mcp_synology.core.client.DsmClient", return_value=client):
            result = await setup_mod._connect_and_login(
                config, "admin", "pw", lambda _device_id: True, verbose=False
            )
        assert result["success"] is True
        assert "hostname" not in result
        assert [c.args[:2] for c in request.await_args_list] == [
            ("SYNO.API.Auth", "login"),
            ("SYNO.API.Auth", "logout"),
        ]

    async def test_connect_and_login_succeeds_when_logout_fails(self) -> None:
        # The login and DSM info worked; a failed logout only leaves the session to time out.
        from mcp_synology.core.errors import SynologyError

        config = _make_test_app_config()
        request = AsyncMock(side_effect=[{"sid": "test-sid"}, SynologyError("gone", code=119)])
        client = self._make_client_cm(request, {"hostname": "MyNAS"})
        with patch("mcp_synology.core.client.DsmClient", return_value=client):
            result = await setup_mod._connect_and_login(
                config, "admin", "pw", lambda _device_id: True, verbose=False
            )
        assert result["success"] is True
        assert result["hostname"] == "MyNAS"

    @pytest.mark.parametrize("where", ["dsm_info_fails", "setup_login"])
    async def test_logout_is_bounded(self, where: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """QA round 1 on #459: a NAS that stopped answering held the logout for the
        client's whole timeout, doubling setup's wait. It's bounded by LOGOUT_TIMEOUT."""
        import asyncio
        import time

        import httpx

        monkeypatch.setattr("mcp_synology.server.LOGOUT_TIMEOUT", 0.05)

        async def request(api: str, method: str, *args: object, **kwargs: object) -> dict[str, str]:
            if method == "logout":
                await asyncio.sleep(1)
            return {"sid": "test-sid"}

        config = _make_test_app_config()
        client = self._make_client_cm(AsyncMock(side_effect=request), {})
        start = time.perf_counter()
        with patch("mcp_synology.core.client.DsmClient", return_value=client):
            if where == "dsm_info_fails":
                client.fetch_dsm_info = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
                await setup_mod._connect_and_login(
                    config, "a", "pw", lambda _device_id: True, verbose=False
                )
            else:
                client.sid = "test-sid"
                await setup_mod._setup_login(config, "a", "pw", lambda _device_id: True)
        assert time.perf_counter() - start < 0.5

    async def test_connect_and_login_rejects_non_appconfig(self) -> None:
        with pytest.raises(RuntimeError, match="AppConfig"):
            await setup_mod._connect_and_login("not a config", "u", "p", "s", verbose=False)

    async def test_connect_and_login_rejects_missing_connection(self) -> None:
        config = _make_test_app_config()
        config.connection = None  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="connection"):
            await setup_mod._connect_and_login(config, "u", "p", "s", verbose=False)


# ---------- _setup_login ----------


class TestSetupLogin:
    @staticmethod
    def _make_client_cm(request_mock: AsyncMock) -> MagicMock:
        client = MagicMock()
        client.request = request_mock
        client.sid = None
        client.query_api_info = AsyncMock(return_value=None)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        return client

    async def test_setup_login_success_logs_out(self) -> None:
        config = _make_test_app_config()
        request = AsyncMock(side_effect=[{"sid": "test-sid"}, {}])
        client = self._make_client_cm(request)
        with patch("mcp_synology.core.client.DsmClient", return_value=client):
            await setup_mod._setup_login(config, "admin", "pw", lambda _device_id: True)
        # Login + logout: 2 requests
        assert request.await_count == 2

    async def test_setup_login_survives_a_failed_logout(self) -> None:
        # A timeout on the logout used to exit setup with a connection error (#404).
        import httpx

        config = _make_test_app_config()
        request = AsyncMock(side_effect=[{"sid": "test-sid"}, httpx.ReadTimeout("slow")])
        client = self._make_client_cm(request)
        with patch("mcp_synology.core.client.DsmClient", return_value=client):
            await setup_mod._setup_login(config, "admin", "pw", lambda _device_id: True)
        assert request.await_count == 2

    async def test_setup_login_failure_skips_logout(self) -> None:
        from mcp_synology.core.errors import SynologyError

        config = _make_test_app_config()
        request = AsyncMock(side_effect=SynologyError("nope", code=400))
        client = self._make_client_cm(request)
        with patch("mcp_synology.core.client.DsmClient", return_value=client):
            await setup_mod._setup_login(config, "admin", "pw", lambda _device_id: True)
        # Only the login attempt was made; logout skipped because login failed
        assert request.await_count == 1

    async def test_setup_login_rejects_non_appconfig(self) -> None:
        with pytest.raises(RuntimeError, match="AppConfig"):
            await setup_mod._setup_login("not a config", "u", "p", "s")

    async def test_setup_login_rejects_missing_connection(self) -> None:
        config = _make_test_app_config()
        config.connection = None  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="connection"):
            await setup_mod._setup_login(config, "u", "p", "s")


# ---------- _setup_credential_flow ----------


class TestSetupCredentialFlow:
    def test_rejects_non_appconfig(self) -> None:
        with pytest.raises(RuntimeError, match="AppConfig"):
            setup_mod._setup_credential_flow("not a config", verbose=False)

    def test_rejects_missing_connection(self) -> None:
        config = _make_test_app_config()
        config.connection = None  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="connection"):
            setup_mod._setup_credential_flow(config, verbose=False)

    def test_keyring_failure_returns_early(self, tmp_path: Path) -> None:
        """No keyring short-circuits before the asyncio.run(_setup_login) call."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "schema_version: 1\n"
            "instance_id: test-nas\n"
            "connection:\n"
            "  host: 192.168.1.100\n"
            "modules:\n"
            "  filestation:\n"
            "    enabled: true\n"
        )
        runner = CliRunner()
        with (
            patch("mcp_synology.core.auth.no_keyring_here", return_value=True),
            patch("mcp_synology.cli.setup._store_keyring", return_value=False),
            patch("mcp_synology.cli.setup.asyncio.run") as run_mock,
        ):
            result = runner.invoke(
                main,
                ["setup", "-c", str(config_file)],
                input="admin\nsecret\n",
            )
        assert result.exit_code == 0
        # Login should NOT have been attempted because there's no keyring to store it in
        run_mock.assert_not_called()


class TestUsernameTheServerUses:
    """#624: setup asked for a username with no default even when the config (or
    SYNOLOGY_USERNAME, applied to it) set one, and stored a different answer's password, which
    the server then sent with the config's username."""

    CONFIG = (
        "schema_version: 1\n"
        "instance_id: test-nas\n"
        "connection:\n"
        "  host: 192.168.1.100\n"
        "auth:\n"
        "  username: bob\n"
        "modules:\n"
        "  filestation:\n"
        "    enabled: true\n"
    )

    def _run(
        self,
        tmp_path: Path,
        typed: str,
        *,
        config: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[Any, MagicMock, MagicMock]:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config or self.CONFIG)
        clean = {k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")}
        runner = CliRunner()
        with (
            patch("mcp_synology.core.auth.no_keyring_here", return_value=False),
            patch("mcp_synology.cli.setup._may_replace_entry", return_value=True),
            patch("mcp_synology.cli.setup._store_keyring", return_value=True) as store,
            patch(
                "mcp_synology.cli.setup.asyncio.run",
                return_value={"success": True, "keyring_ok": True},
            ),
            patch("mcp_synology.cli.setup._setup_login", new=MagicMock()) as login,
            patch.dict(os.environ, {**clean, **(env or {})}, clear=True),
        ):
            result = runner.invoke(main, ["setup", "-c", str(config_file)], input=typed)
        return result, login, store

    REFUSED = (
        "The config's auth.username is bob, and that's the account the server logs in as, "
        "so its password is the one to store here. Answer that name exactly, or stop (Ctrl-C) "
        "and change that setting first to use another account. Nothing was sent or stored."
    )

    def test_the_config_s_username_is_the_default(self, tmp_path: Path) -> None:
        result, login, _store = self._run(tmp_path, "\nsecret\n")
        assert result.exit_code == 0, result.output
        assert "DSM username [bob]:" in result.output
        assert login.call_args.args[1] == "bob"

    def test_typing_the_same_name_is_fine(self, tmp_path: Path) -> None:
        result, login, _store = self._run(tmp_path, "  bob \nsecret\n")
        assert result.exit_code == 0, result.output
        assert login.call_args.args[1] == "bob"

    def test_a_padded_config_username_is_sent_as_the_server_sends_it(self, tmp_path: Path) -> None:
        config = self.CONFIG.replace("username: bob", "username: ' bob '")
        result, login, _store = self._run(tmp_path, "bob\nsecret\n", config=config)
        assert result.exit_code == 0, result.output
        # Shown with its padding, as it will be sent (pre-QA r2 on #627).
        assert "DSM username [' bob ']:" in result.output
        assert login.call_args.args[1] == " bob "

    def test_another_name_is_refused_and_asked_again(self, tmp_path: Path) -> None:
        result, login, _store = self._run(tmp_path, "alice\nbob\nsecret\n")
        assert result.exit_code == 0, result.output
        assert self.REFUSED in result.output
        assert result.output.count("DSM username [bob]:") == 2
        assert login.call_args.args[1] == "bob"

    def test_stopping_after_a_refusal_sends_and_stores_nothing(self, tmp_path: Path) -> None:
        result, login, store = self._run(tmp_path, "alice\n")
        assert result.exit_code == 1
        assert self.REFUSED in result.output
        assert "DSM password" not in result.output
        login.assert_not_called()
        store.assert_not_called()

    def test_synology_username_wins_over_the_config(self, tmp_path: Path) -> None:
        env = {"SYNOLOGY_USERNAME": "carol"}
        result, login, _store = self._run(tmp_path, "\nsecret\n", env=env)
        assert "DSM username [carol]:" in result.output
        assert login.call_args.args[1] == "carol"
        result, login, _store = self._run(tmp_path, "bob\n", env=env)
        assert result.exit_code == 1
        assert "SYNOLOGY_USERNAME is set to carol, and that's the account" in result.output
        login.assert_not_called()

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_synology_username_is_unset(self, tmp_path: Path, blank: str) -> None:
        """A blank variable is skipped by the server too, and isn't named as the source."""
        result, login, _store = self._run(
            tmp_path, "x\n\nsecret\n", env={"SYNOLOGY_USERNAME": blank}
        )
        assert "DSM username [bob]:" in result.output
        assert "The config's auth.username is bob," in result.output  # not the variable
        assert login.call_args.args[1] == "bob"

    def test_a_username_that_could_move_the_terminal_is_shown_escaped(self, tmp_path: Path) -> None:
        """Pre-QA on #627: the default went to the terminal raw."""
        config = self.CONFIG.replace("username: bob", 'username: "bob\\e[2J\\nPassword:"')
        result, login, _store = self._run(tmp_path, "x\n\nsecret\n", config=config)
        assert result.exit_code == 0, result.output
        assert "\x1b" not in result.output and "bob\nPassword" not in result.output
        # The refusal shows it the same way.
        assert "The config's auth.username is 'bob\\x1b[2J\\nPassword:', and" in result.output
        assert "DSM username ['bob\\x1b[2J\\nPassword:']:" in result.output
        assert login.call_args.args[1] == "bob\x1b[2J\nPassword:"

    def test_without_a_username_anything_goes(self, tmp_path: Path) -> None:
        config = self.CONFIG.replace("auth:\n  username: bob\n", "")
        result, login, _store = self._run(tmp_path, "alice\nsecret\n", config=config)
        assert result.exit_code == 0, result.output
        assert "DSM username: " in result.output and "DSM username [" not in result.output
        assert login.call_args.args[1] == "alice"

    def test_a_new_config_takes_any_name(self, tmp_path: Path) -> None:
        """Pre-QA on #627: the config the interactive path writes has no username, and the
        terminal's SYNOLOGY_USERNAME isn't necessarily the MCP client's: it decides nothing."""
        clean = {k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")}
        runner = CliRunner()
        with (
            patch("mcp_synology.cli.setup._CONFIG_DIR", tmp_path / "config"),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("mcp_synology.cli.setup._store_keyring", return_value=False),
            # No login: the test is about the prompt (pre-QA r2 on #627: it reached the network).
            patch(
                "mcp_synology.cli.setup.asyncio.run",
                return_value={"success": True, "keyring_ok": False},
            ),
            patch("mcp_synology.cli.setup._connect_and_login", new=MagicMock()) as login,
            patch.dict(os.environ, {**clean, "SYNOLOGY_USERNAME": "carol"}, clear=True),
        ):
            result = runner.invoke(main, ["setup"], input="nas.local\nn\n\nread\n\nadmin\npw\n")
        assert "DSM username: " in result.output and "DSM username [" not in result.output
        assert "SYNOLOGY_USERNAME is set to" not in result.output
        assert login.call_args.args[1] == "admin"


# ---------- setup command top-level error paths ----------


class TestSetupCommandErrorPaths:
    def test_setup_with_invalid_config_path_exits_nonzero(self, tmp_path: Path) -> None:
        """`setup -c <bad>` triggers _setup_with_config's load_config exception path."""
        config_file = tmp_path / "broken.yaml"
        config_file.write_text(
            "schema_version: 999\n"
            "connection:\n"
            "  host: 1.2.3.4\n"
            "modules:\n"
            "  filestation:\n"
            "    enabled: true\n"
        )
        runner = CliRunner()
        result = runner.invoke(main, ["setup", "-c", str(config_file)])
        assert result.exit_code == 1
        assert "Error" in result.output

    def test_setup_with_discovered_valid_config_runs_credential_flow(self, tmp_path: Path) -> None:
        """`setup` (no -c) → discover_config_path returns a valid config → credential flow."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        good = config_dir / "default.yaml"
        good.write_text(
            "schema_version: 1\n"
            "instance_id: test-nas\n"
            "connection:\n"
            "  host: 1.2.3.4\n"
            "modules:\n"
            "  filestation:\n"
            "    enabled: true\n"
        )

        clean_env: dict[str, str] = {
            k: val for k, val in os.environ.items() if not k.startswith("SYNOLOGY_")
        }

        runner = CliRunner()
        with (
            patch("mcp_synology.core.config.discover_config_path", return_value=good),
            patch("mcp_synology.cli.setup._store_keyring", return_value=True),
            patch(
                "mcp_synology.cli.setup.asyncio.run",
                return_value={"success": True, "keyring_ok": True},
            ),
            patch.dict(os.environ, clean_env, clear=True),
        ):
            result = runner.invoke(main, ["setup"], input="admin\nsecret\n")
        assert result.exit_code == 0
        assert "Setting up credentials" in result.output

    def test_setup_with_validation_error_in_discovered_config(self, tmp_path: Path) -> None:
        """`setup` (no -c) → load_config(None) raises ValueError → exits 1."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        # Place a config that discover_config_path will find but fails validation
        bad = config_dir / "default.yaml"
        bad.write_text(
            "schema_version: 999\n"
            "connection:\n"
            "  host: 1.2.3.4\n"
            "modules:\n"
            "  filestation:\n"
            "    enabled: true\n"
        )

        clean_env: dict[str, str] = {
            k: val for k, val in os.environ.items() if not k.startswith("SYNOLOGY_")
        }

        runner = CliRunner()
        with (
            patch(
                "mcp_synology.core.config.discover_config_path",
                return_value=bad,
            ),
            patch.dict(os.environ, clean_env, clear=True),
        ):
            result = runner.invoke(main, ["setup"])
        assert result.exit_code == 1
        assert "Error" in result.output


# ---------- _setup_interactive validation failure ----------


class TestSetupInteractiveValidationFailure:
    @staticmethod
    def _clean_env() -> dict[str, str]:
        return {k: val for k, val in os.environ.items() if not k.startswith("SYNOLOGY_")}

    def test_validation_failure_during_interactive_exits_nonzero(self, tmp_path: Path) -> None:
        """If AppConfig(**config_dict) raises after the user fills out prompts, exit 1,
        with the error screened like every config error (#471: it printed pydantic's own
        text, which quotes the input).

        The prompts check the host and instance ID by the config's own rules, so nothing
        a user types reaches this; AppConfig is patched to fail with a real error.
        """
        from pydantic import ValidationError

        from mcp_synology.core.config import AppConfig

        try:
            AppConfig.model_validate(
                {
                    "schema_version": 1,
                    "connection": {"host": "nas", "timeout": 0},
                    "modules": {"filestation": {}},
                }
            )
        except ValidationError as e:
            error = e
        config_dir = tmp_path / "config"
        runner = CliRunner()
        with (
            patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("mcp_synology.core.config.AppConfig", side_effect=error),
            patch.dict(os.environ, self._clean_env(), clear=True),
        ):
            # host, https, port, permission, alias: fewer prompts since validation fails
            result = runner.invoke(main, ["setup"], input="192.168.1.50\nn\n\nread\n\n")
        assert result.exit_code == 1
        assert "Error: Configuration validation failed (1 error(s)):" in result.output
        assert "connection.timeout:" in result.output
        assert "input_value" not in result.output

    def test_a_host_that_isnt_bare_is_asked_again(self, tmp_path: Path) -> None:
        """#471: a URL with credentials, or a port, gets advice and another try."""
        config_dir = tmp_path / "config"
        runner = CliRunner()
        with (
            patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("mcp_synology.cli.setup._store_keyring", return_value=False),
            patch.dict(os.environ, self._clean_env(), clear=True),
        ):
            result = runner.invoke(
                main,
                ["setup"],
                input="https://admin:Hunter2x@nas\nnas:5001\nnas.local\nn\n\nread\n\nadmin\npw\n",
            )
        assert 'drop the http:// or https:// (for HTTPS, answer yes to "Use HTTPS?")' in (
            result.output
        )
        assert "type the port at the DSM port question instead" in result.output
        # Setup asks for them later: not the file's "under auth" (QA round 1 on #532).
        assert "user names and passwords are asked for after these questions" in result.output
        # The host was just typed above, so no "isn't shown" note (pre-QA on #532).
        assert "It isn't shown" not in result.output
        # Only where CliRunner echoes what was typed; the advice never repeats it.
        assert result.output.casefold().count("hunter2x") == 1
        assert result.output.count("NAS host (IP or hostname)") == 3

    def test_a_host_with_no_usable_id_asks_for_one(self, tmp_path: Path) -> None:
        """An IPv6 address or a name like `my_nas` gives no instance ID: setup asks."""
        config_dir = tmp_path / "config"
        stored: list[str] = []
        runner = CliRunner()
        with (
            patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("mcp_synology.core.auth.no_keyring_here", return_value=True),
            patch(
                "mcp_synology.cli.setup._store_keyring",
                side_effect=lambda service, *_: stored.append(service) and False,
            ),
            patch.dict(os.environ, self._clean_env(), clear=True),
        ):
            result = runner.invoke(
                main,
                ["setup"],
                input="my_nas\nn\n\nread\n   \nbad id\nMy-NAS\n\nadmin\npw\n",
            )
        # The host is blamed once; the typed ID gets its own message (pre-QA r1), without
        # the file's "leave it out" advice, which can't apply here (pre-QA r3).
        assert result.output.count("This host gives no usable instance ID") == 1
        assert "  That instance ID is empty.\n" in result.output
        assert "leave instance_id out" not in result.output
        assert "That instance ID may hold only lowercase letters" in result.output
        assert result.output.count("Instance ID (names this config") == 3
        assert stored == ["mcp-synology/my-nas"]

    def test_an_unbracketed_ipv6_host_gets_no_quotes_advice(self, tmp_path: Path) -> None:
        """Pre-QA r6 on #532: quotes are for the file's form, not an answer to a question."""
        with (
            patch("mcp_synology.cli.setup._CONFIG_DIR", tmp_path / "config"),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("mcp_synology.cli.setup._store_keyring", return_value=False),
            patch.dict(os.environ, self._clean_env(), clear=True),
        ):
            result = CliRunner().invoke(
                main, ["setup"], input="fd00::1\nnas.local\nn\n\nread\n\nadmin\npw\n"
            )
        assert "write it in square brackets, [fd00::1]." in result.output

    def test_a_very_long_id_is_asked_again(self, tmp_path: Path) -> None:
        """QA round 2 on #532: it was a traceback from the file name, with the keyring
        already written."""
        config_dir = tmp_path / "config"
        stored: list[str] = []
        with (
            patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("mcp_synology.core.auth.no_keyring_here", return_value=True),
            patch(
                "mcp_synology.cli.setup._store_keyring",
                side_effect=lambda service, *_: stored.append(service) and False,
            ),
            patch.dict(os.environ, self._clean_env(), clear=True),
        ):
            result = CliRunner().invoke(
                main,
                ["setup"],
                input=f"my_nas\nn\n\nread\n{'a' * 400}\nmy-nas\n\nadmin\npw\n",
            )
        assert "  That instance ID is longer than 63 characters.\n" in result.output
        assert result.output.count("Instance ID (names this config") == 2
        assert stored == ["mcp-synology/my-nas"]

    @pytest.mark.parametrize(
        ("https", "port", "written"),
        [("n", "", None), ("y", "", None), ("n", "5555", 5555), ("y", "5000", 5000)],
    )
    def test_the_port_is_asked_for(
        self, tmp_path: Path, https: str, port: str, written: int | None
    ) -> None:
        """#504 item 1: a NAS on a custom port can be set up interactively; the default
        is DSM's for the protocol chosen, and isn't written, so the port follows a later
        change of `https` (QA round 1 on #532: a written 5001 would stay on plain HTTP)."""
        captured: list[Any] = []

        async def fake_connect(app_config: Any, *_: Any) -> dict[str, Any]:
            captured.append(app_config)
            return {"success": True, "keyring_ok": True}

        config_dir = tmp_path / "config"
        verify = "n\n" if https == "y" else ""
        runner = CliRunner()
        with (
            patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("mcp_synology.cli.setup._store_keyring", return_value=True),
            patch("mcp_synology.cli.setup._connect_and_login", side_effect=fake_connect),
            patch.dict(os.environ, self._clean_env(), clear=True),
        ):
            result = runner.invoke(
                main,
                ["setup"],
                input=f"nas.local\n{https}\n{port}\nread\n\n{verify}admin\npw\n",
            )
        assert result.exit_code == 0, result.output
        (app_config,) = captured
        default = 5001 if https == "y" else 5000
        assert app_config.connection.port == (written or default)
        connection = yaml.safe_load((config_dir / "nas.yaml").read_text())["connection"]
        assert connection.get("port") == written


# ---------- _emit_claude_desktop_snippet Linux fallback ----------


class TestEmitClaudeDesktopSnippetLinuxFallback:
    def test_linux_constructs_dbus_path_when_env_unset(self, tmp_path: Path) -> None:
        """When DBUS_SESSION_BUS_ADDRESS isn't set, the Linux branch constructs
        /run/user/{uid}/bus and includes it in the snippet."""
        config_dir = tmp_path / "config"

        clean_env: dict[str, str] = {
            k: val
            for k, val in os.environ.items()
            if not k.startswith("SYNOLOGY_") and k != "DBUS_SESSION_BUS_ADDRESS"
        }

        connect_result: dict[str, Any] = {"success": True, "keyring_ok": True}

        runner = CliRunner()
        with (
            patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("mcp_synology.cli.setup._store_keyring", return_value=True),
            patch("mcp_synology.cli.setup.asyncio.run", return_value=connect_result),
            patch.dict(os.environ, clean_env, clear=True),
            patch("sys.platform", "linux"),
            patch("os.getuid", return_value=1234, create=True),
        ):
            result = runner.invoke(
                main,
                ["setup"],
                input="192.168.1.50\nn\n\nread\n\nadmin\npassword\n",
            )

        assert result.exit_code == 0
        assert "/run/user/1234/bus" in result.output

    def test_linux_uses_dbus_env_when_set(self, tmp_path: Path) -> None:
        """When DBUS_SESSION_BUS_ADDRESS is set, the snippet uses that value."""
        config_dir = tmp_path / "config"

        clean_env: dict[str, str] = {
            k: val for k, val in os.environ.items() if not k.startswith("SYNOLOGY_")
        }
        clean_env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/custom/bus"

        connect_result: dict[str, Any] = {"success": True, "keyring_ok": True}

        runner = CliRunner()
        with (
            patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("mcp_synology.cli.setup._store_keyring", return_value=True),
            patch("mcp_synology.cli.setup.asyncio.run", return_value=connect_result),
            patch.dict(os.environ, clean_env, clear=True),
            patch("sys.platform", "linux"),
        ):
            result = runner.invoke(
                main,
                ["setup"],
                input="192.168.1.50\nn\n\nread\n\nadmin\npassword\n",
            )

        assert result.exit_code == 0
        assert "/custom/bus" in result.output


# ---------- atomic config-file write (closes #70) ----------


class TestSetupAtomicConfigWrite:
    """Regression for #70 — interactive setup persists the config file via
    `atomic_write_text` (sibling .tmp + Path.replace), not the bare
    `path.write_text` that the original implementation used. Verifies the
    parent dir is auto-created (atomic_write_text handles it, replacing the
    explicit `_CONFIG_DIR.mkdir(...)` that the call site previously did) and
    that no `.tmp` sibling lingers after a successful write.
    """

    def test_setup_writes_config_atomically_with_no_tmp_sibling(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"  # intentionally not pre-created

        clean_env: dict[str, str] = {
            k: val for k, val in os.environ.items() if not k.startswith("SYNOLOGY_")
        }

        connect_result: dict[str, Any] = {"success": True, "keyring_ok": True}

        runner = CliRunner()
        with (
            patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("mcp_synology.cli.setup._store_keyring", return_value=True),
            patch("mcp_synology.cli.setup.asyncio.run", return_value=connect_result),
            patch.dict(os.environ, clean_env, clear=True),
        ):
            result = runner.invoke(
                main,
                ["setup"],
                input="192.168.1.50\nn\n\nread\n\nadmin\npassword\n",
            )

        assert result.exit_code == 0, f"setup failed:\n{result.output}"

        # atomic_write_text creates parents; the test deliberately did not
        # pre-create config_dir, so its existence proves the helper ran.
        assert config_dir.exists() and config_dir.is_dir()

        # No .tmp sibling left behind on the success path.
        names = sorted(p.name for p in config_dir.iterdir())
        assert not any(n.endswith(".tmp") for n in names), (
            f"unexpected .tmp file lingered after setup: {names}"
        )

        # Exactly one YAML config file with the expected header + host.
        yamls = [p for p in config_dir.iterdir() if p.suffix == ".yaml"]
        assert len(yamls) == 1, f"expected exactly one yaml in config dir, got: {names}"
        content = yamls[0].read_text(encoding="utf-8")
        assert content.startswith("# Generated by mcp-synology setup\n")
        assert "192.168.1.50" in content


def test_list_never_shows_a_mistagged_password(tmp_path: Path) -> None:
    """#386: `setup --list` parses every config; a tag that doesn't fit its value
    (`!!int`, `!!bool`) raised with the value in a traceback."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "a.yaml").write_text("schema_version: 1\nauth: {password: !!int Hunter2x}\n")
    (config_dir / "b.yaml").write_text("schema_version: 1\nauth: {password: !!bool Hunter2x}\n")
    (config_dir / "c.yaml").write_bytes(b"schema_version: 1\nauth: {password: Hunter2x\xff}\n")
    with patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir):
        result = CliRunner().invoke(main, ["setup", "--list"])
    assert result.exit_code == 0, result.output
    assert "hunter" not in result.output.casefold()
    assert "a.yaml  (could not parse)" in result.output
    assert "b.yaml  (could not parse)" in result.output
    assert "c.yaml  (could not parse)" in result.output  # not UTF-8: a ValueError


def test_list_gives_the_reason_a_file_cant_be_read(tmp_path: Path) -> None:
    """QA round 2 on #532: a repeated key was a bare "(could not parse)", where `check`
    gives the lines. The reason names no value."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "a.yaml").write_text(
        "schema_version: 1\nauth:\n  password: Hunter2x\n  password: Spring2y\n"
    )
    (config_dir / "b.yaml").write_text("schema_version: 1\nauth: {password: !!int Hunter2x}\n")
    with patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir):
        result = CliRunner().invoke(main, ["setup", "--list"])
    assert result.exit_code == 0, result.output
    assert "a.yaml  (could not parse)" in result.output
    assert "The config file has `password` twice in one section (lines 3 and 4)" in result.output
    assert "b.yaml  (could not parse)" in result.output
    assert "a value YAML can't build" in result.output
    assert "hunter" not in result.output.casefold()
    assert "spring" not in result.output.casefold()


@pytest.mark.parametrize("encoding", ["iso-8859-1", "cp866", "utf-8"])
def test_list_survives_a_stdout_that_cant_encode_the_reason(tmp_path: Path, encoding: str) -> None:
    """QA round 3 on #532: the reason holds `…`, stdout is strict about encoding, and the
    crash left the later files unlisted."""
    skip_unless_the_locale_can_encode("b-ब.yaml")
    config_dir = tmp_path / ".config" / "mcp-synology"
    config_dir.mkdir(parents=True)
    (config_dir / "a.yaml").write_text("schema_version: 1\nauth: {username: me, secret}\n")
    # Its name can't be encoded either (pre-QA on #532).
    (config_dir / "b-ब.yaml").write_text("schema_version: 1\nauth: {username: me, secret}\n")
    (config_dir / "z.yaml").write_text("schema_version: 1\nconnection: {host: nas}\n")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from mcp_synology.cli.main import main; main(['setup', '--list'])",
        ],
        capture_output=True,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PYTHONIOENCODING": encoding,
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        },
        timeout=60,
    )
    output = result.stdout.decode(encoding)
    assert result.returncode == 0, result.stderr
    assert "a.yaml  (could not parse)" in output
    assert "z.yaml" in output
    assert "with no value inside" in output
    if encoding != "utf-8":
        # Escaped, as the code's comment promises, not `?` (#543).
        assert "{\\u2026}" in output
        assert "b-\\u092c.yaml" in output


def test_list_with_stdout_closed_doesnt_crash(tmp_path: Path) -> None:
    """Python's `sys.stdout` is None then, with no `reconfigure` to call (#543)."""
    config_dir = tmp_path / ".config" / "mcp-synology"
    config_dir.mkdir(parents=True)
    (config_dir / "z.yaml").write_text("schema_version: 1\nconnection: {host: nas}\n")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout = None; "
            "from mcp_synology.cli.main import main; main(['setup', '--list'])",
        ],
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        },
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def _run_cli(
    tmp_path: Path, *args: str, encoding: str = "utf-8"
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            f"from mcp_synology.cli.main import main; main({list(args)!r})",
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PYTHONIOENCODING": encoding,
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        },
        timeout=60,
    )


@pytest.mark.parametrize(
    "args", [("check", "--config"), ("setup", "--config")], ids=["check", "setup"]
)
def test_an_alias_stdout_cant_encode_doesnt_crash(tmp_path: Path, args: tuple[str, ...]) -> None:
    """Pre-QA on #541: `setup --list` had this fixed, and `check`, which the CHANGELOG's
    Upgrading block sends everyone to, still ended in a traceback."""
    config = tmp_path / "nas.yaml"
    config.write_text(
        'schema_version: 1\nalias: "\\u092c"\ncheck_for_updates: false\n'
        "connection: {host: 127.0.0.1, port: 9, https: false}\nmodules: {filestation: }\n",
        encoding="utf-8",
    )
    result = _run_cli(tmp_path, *args, str(config), encoding="iso-8859-1")
    assert b"UnicodeEncodeError" not in result.stderr
    assert "\\u092c" in result.stdout.decode("iso-8859-1")


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("a\n    Host: evil.example\n.yaml", "'a\\n    Host: evil.example\\n.yaml'"),
        ("b\x1b[31mred.yaml", "'b\\x1b[31mred.yaml'"),
    ],
    ids=["line_break", "terminal_escape"],
)
def test_list_doesnt_print_a_file_name_raw(tmp_path: Path, name: str, shown: str) -> None:
    """Pre-QA on #541: `alias` couldn't forge a line in the list, but a file's name could."""
    config_dir = tmp_path / ".config" / "mcp-synology"
    config_dir.mkdir(parents=True)
    (config_dir / name).write_text("schema_version: 1\nconnection: {host: nas}\n")
    output = _run_cli(tmp_path, "setup", "--list").stdout.decode("utf-8")
    assert shown in output
    assert "\n    Host: evil.example\n" not in output
    assert "\x1b" not in output


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes here")
def test_list_doesnt_wait_on_a_named_pipe(tmp_path: Path) -> None:
    """Pre-QA on #541: it waited for a writer, and the later files went unlisted."""
    config_dir = tmp_path / ".config" / "mcp-synology"
    config_dir.mkdir(parents=True)
    os.mkfifo(config_dir / "a.yaml")
    (config_dir / "z.yaml").write_text("schema_version: 1\nconnection: {host: nas}\n")
    result = _run_cli(tmp_path, "setup", "--list")
    output = result.stdout.decode("utf-8")
    assert result.returncode == 0, result.stderr
    assert "a.yaml  (could not parse)" in output
    assert "z.yaml" in output


def test_list_survives_a_config_folder_without_search_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA round 1 on #541: `is_file` raises there on 3.11, and the rest went unlisted."""
    from pathlib import Path

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "a.yaml").write_text("schema_version: 1\nconnection: {host: nas}\n")

    def refused(self: Path) -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "is_file", refused)
    with patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir):
        result = CliRunner().invoke(main, ["setup", "--list"])
    assert result.exit_code == 0, result.output
    assert "a.yaml  (could not parse)" in result.output


def test_list_shows_only_bare_hosts_and_text_names(tmp_path: Path) -> None:
    """#476, #471: a host written with credentials, or a name given as a mapping, isn't
    printed; #492: an empty `connection:` lists instead of failing to parse."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "a.yaml").write_text(
        "schema_version: 1\nconnection: {host: 'https://admin:Hunter2x@nas'}\n"
        "alias: {password: Hunter2x}\ninstance_id: {x: Hunter2x}\n"
    )
    (config_dir / "b.yaml").write_text("schema_version: 1\nconnection:\n")
    (config_dir / "c.yaml").write_text(
        "schema_version: 1\nconnection: {host: nas.local}\ninstance_id: Home\n"
    )
    (config_dir / "e.yaml").write_text(
        "schema_version: 1\nconnection: {host: nas}\ninstance_id: Bad ID\n"
    )
    (config_dir / "d.yaml").write_text("schema_version: 1\nconnection: {host: 12}\nalias: Office\n")
    with patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir):
        result = CliRunner().invoke(main, ["setup", "--list"])
    assert result.exit_code == 0, result.output
    assert "hunter" not in result.output.casefold()
    assert (
        "  a.yaml\n    Name: a\n    Host: (not shown: not a bare host name or IP address)\n"
    ) in result.output
    assert "  b.yaml\n    Name: b\n    Host: ?\n" in result.output
    assert "  c.yaml\n    Name: home\n    Host: nas.local\n" in result.output
    assert (
        "  d.yaml\n    Name: Office\n    Host: (not shown: not a bare host name or IP address)\n"
    ) in result.output
    # An instance_id the server would refuse isn't the name: the file's stem is.
    assert "  e.yaml\n    Name: e\n    Host: nas\n" in result.output


def test_setup_reports_a_config_it_cant_read(tmp_path: Path) -> None:
    """`setup --config` with a missing file, and plain `setup` on a discovered config
    that raises ValueError, exit 1 with the message (#386: these branches split out)."""
    missing = tmp_path / "nope.yaml"
    result = CliRunner().invoke(main, ["setup", "--config", str(missing)])
    assert result.exit_code == 1 and "Error: " in result.output
    with patch("mcp_synology.server.load_config", side_effect=ValueError("not UTF-8")):
        result = CliRunner().invoke(main, ["setup"])
    assert result.exit_code == 1 and "Error: not UTF-8" in result.output
