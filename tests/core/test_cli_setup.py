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
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from mcp_synology.cli import setup as setup_mod
from mcp_synology.cli.main import main

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
        result = await setup_mod._attempt_login(client, "admin", "pw", "service")
        assert result["success"] is True
        assert result["sid"] == "test-sid-123"
        assert client.sid == "test-sid-123"

    async def test_login_failure_non_2fa_error(self) -> None:
        from mcp_synology.core.errors import SynologyError

        request = AsyncMock(side_effect=SynologyError("bad password", code=400))
        client = self._make_client(request)
        result = await setup_mod._attempt_login(client, "admin", "pw", "service")
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
                result = await setup_mod._attempt_login(client, "admin", "pw", "service")

        assert result["success"] is True
        assert result["sid"] == "sid-after-2fa"
        # Device token stored under the service
        set_pw.assert_called_once_with("service", "device_id", "device-token-xyz")
        # Both requests issued
        assert request.await_count == 2

    async def test_login_2fa_required_then_success_without_device_token(self) -> None:
        """2FA succeeds but DSM doesn't echo back a `did` — still succeeds."""
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
            result = await setup_mod._attempt_login(client, "admin", "pw", "service")

        assert result["success"] is True
        assert result["sid"] == "sid-after-2fa"

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
            result = await setup_mod._attempt_login(client, "admin", "pw", "service")
        assert result["success"] is False

    async def test_login_oserror(self) -> None:
        request = AsyncMock(side_effect=OSError("connection refused"))
        client = self._make_client(request)
        result = await setup_mod._attempt_login(client, "admin", "pw", "service")
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
            result = await setup_mod._connect_and_login(config, "admin", "pw", "svc", verbose=False)
        assert result["success"] is True
        assert result["hostname"] == "MyNAS"
        assert result["dsm_version"] == "DSM 7.1"

    async def test_connect_and_login_success_no_hostname(self) -> None:
        config = _make_test_app_config()
        request = AsyncMock(side_effect=[{"sid": "test-sid"}, {}])
        client = self._make_client_cm(request, {})  # empty dsm_info
        with patch("mcp_synology.core.client.DsmClient", return_value=client):
            result = await setup_mod._connect_and_login(config, "admin", "pw", "svc", verbose=False)
        assert result["success"] is True
        assert "hostname" not in result

    async def test_connect_and_login_https(self) -> None:
        config = _make_test_app_config(host="nas.example.com", https=True)
        request = AsyncMock(side_effect=[{"sid": "test-sid"}, {}])
        client = self._make_client_cm(request, {})
        with patch("mcp_synology.core.client.DsmClient", return_value=client) as dsm_client:
            await setup_mod._connect_and_login(config, "admin", "pw", "svc", verbose=False)
        # Confirm https URL was constructed
        kwargs = dsm_client.call_args.kwargs
        assert kwargs["base_url"].startswith("https://")

    async def test_connect_and_login_login_failure(self) -> None:
        from mcp_synology.core.errors import SynologyError

        config = _make_test_app_config()
        request = AsyncMock(side_effect=SynologyError("bad creds", code=400))
        client = self._make_client_cm(request, {})
        with patch("mcp_synology.core.client.DsmClient", return_value=client):
            result = await setup_mod._connect_and_login(config, "admin", "pw", "svc", verbose=False)
        assert result["success"] is False

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
            await setup_mod._setup_login(config, "admin", "pw", "svc")
        # Login + logout: 2 requests
        assert request.await_count == 2

    async def test_setup_login_failure_skips_logout(self) -> None:
        from mcp_synology.core.errors import SynologyError

        config = _make_test_app_config()
        request = AsyncMock(side_effect=SynologyError("nope", code=400))
        client = self._make_client_cm(request)
        with patch("mcp_synology.core.client.DsmClient", return_value=client):
            await setup_mod._setup_login(config, "admin", "pw", "svc")
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
        """Keyring failure short-circuits before the asyncio.run(_setup_login) call."""
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
            patch("mcp_synology.cli.setup._store_keyring", return_value=False),
            patch("mcp_synology.cli.setup.asyncio.run") as run_mock,
        ):
            result = runner.invoke(
                main,
                ["setup", "-c", str(config_file)],
                input="admin\nsecret\n",
            )
        assert result.exit_code == 0
        # Login should NOT have been attempted because keyring failed
        run_mock.assert_not_called()


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
            patch("mcp_synology.cli.setup.asyncio.run", return_value=None),
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
    def test_validation_failure_during_interactive_exits_nonzero(self, tmp_path: Path) -> None:
        """If AppConfig(**config_dict) raises after the user fills out prompts, exit 1.

        Triggered by patching `_derive_instance_id` to return an invalid id (with
        characters AppConfig rejects), which fails validation at the first
        AppConfig(**config_dict) construction call inside _setup_interactive.
        """
        config_dir = tmp_path / "config"

        clean_env: dict[str, str] = {
            k: val for k, val in os.environ.items() if not k.startswith("SYNOLOGY_")
        }

        runner = CliRunner()
        with (
            patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch(
                "mcp_synology.core.config._derive_instance_id",
                return_value="INVALID ID WITH SPACES!",
            ),
            patch.dict(os.environ, clean_env, clear=True),
        ):
            # host, https, permission, alias — fewer prompts since validation fails
            result = runner.invoke(
                main,
                ["setup"],
                input="192.168.1.50\nn\nread\n\n",
            )
        assert result.exit_code == 1
        assert "Config validation failed" in result.output


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

        connect_result: dict[str, Any] = {"success": True}

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
                input="192.168.1.50\nn\nread\n\nadmin\npassword\n",
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

        connect_result: dict[str, Any] = {"success": True}

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
                input="192.168.1.50\nn\nread\n\nadmin\npassword\n",
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

        connect_result: dict[str, Any] = {"success": True}

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
                input="192.168.1.50\nn\nread\n\nadmin\npassword\n",
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
