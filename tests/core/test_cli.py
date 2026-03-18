"""Tests for cli.py — CLI subcommands via click CliRunner."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

from click.testing import CliRunner

from synology_mcp.cli import main


class TestCli:
    def test_version(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.2.0" in result.output

    def test_version_short_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["-v"])
        assert result.exit_code == 0
        assert "0.2.0" in result.output

    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "synology-mcp" in result.output

    def test_help_short_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["-h"])
        assert result.exit_code == 0
        assert "synology-mcp" in result.output

    def test_serve_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--config" in result.output

    def test_setup_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["setup", "--help"])
        assert result.exit_code == 0
        assert "--config" in result.output
        assert "--list" in result.output

    def test_check_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["check", "--help"])
        assert result.exit_code == 0
        assert "--config" in result.output

    def test_serve_missing_config(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["serve", "--config", "/nonexistent/config.yaml"])
        assert result.exit_code != 0

    def test_serve_missing_config_error_in_red(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["serve", "-c", "/nonexistent/config.yaml"])
        assert result.exit_code != 0
        # Error goes to stderr
        assert "not found" in (result.output + (result.stderr if hasattr(result, "stderr") else ""))

    def test_short_config_flag_serve(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["serve", "-c", "/nonexistent/config.yaml"])
        assert result.exit_code != 0

    def test_short_config_flag_check(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["check", "-c", "/nonexistent/config.yaml"])
        assert result.exit_code != 0


class TestSetupList:
    def test_list_no_configs(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with patch("synology_mcp.cli._CONFIG_DIR", tmp_path / "nonexistent"):
            result = runner.invoke(main, ["setup", "--list"])
        assert result.exit_code == 0
        assert "No configurations found" in result.output

    def test_list_with_configs(self, tmp_path: Path) -> None:
        config_file = tmp_path / "my-nas.yaml"
        config_file.write_text(
            "schema_version: 1\n"
            "instance_id: my-nas\n"
            "alias: HomeNAS\n"
            "connection:\n"
            "  host: 192.168.1.100\n"
            "modules:\n"
            "  filestation:\n"
            "    enabled: true\n"
        )
        runner = CliRunner()
        with patch("synology_mcp.cli._CONFIG_DIR", tmp_path):
            result = runner.invoke(main, ["setup", "--list"])
        assert result.exit_code == 0
        assert "my-nas.yaml" in result.output
        assert "HomeNAS" in result.output
        assert "192.168.1.100" in result.output

    def test_list_short_flag(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with patch("synology_mcp.cli._CONFIG_DIR", tmp_path / "nonexistent"):
            result = runner.invoke(main, ["setup", "-l"])
        assert result.exit_code == 0
        assert "No configurations found" in result.output

    def test_list_empty_directory(self, tmp_path: Path) -> None:
        """Config dir exists but has no .yaml files."""
        runner = CliRunner()
        with patch("synology_mcp.cli._CONFIG_DIR", tmp_path):
            result = runner.invoke(main, ["setup", "--list"])
        assert result.exit_code == 0
        assert "No configurations found" in result.output

    def test_list_with_unparseable_config(self, tmp_path: Path) -> None:
        """Gracefully handle a config file that can't be parsed."""
        bad_file = tmp_path / "broken.yaml"
        bad_file.write_text("{{{{invalid yaml")
        runner = CliRunner()
        with patch("synology_mcp.cli._CONFIG_DIR", tmp_path):
            result = runner.invoke(main, ["setup", "--list"])
        assert result.exit_code == 0
        assert "broken.yaml" in result.output
        assert "could not parse" in result.output

    def test_list_multiple_configs(self, tmp_path: Path) -> None:
        """Multiple config files are listed."""
        for name, host in [("nas-a.yaml", "10.0.0.1"), ("nas-b.yaml", "10.0.0.2")]:
            (tmp_path / name).write_text(
                f"schema_version: 1\nconnection:\n  host: {host}\n"
                "modules:\n  filestation:\n    enabled: true\n"
            )
        runner = CliRunner()
        with patch("synology_mcp.cli._CONFIG_DIR", tmp_path):
            result = runner.invoke(main, ["setup", "--list"])
        assert "nas-a.yaml" in result.output
        assert "nas-b.yaml" in result.output
        assert "10.0.0.1" in result.output
        assert "10.0.0.2" in result.output


class TestSetupInteractive:
    def test_interactive_setup_creates_config(self, tmp_path: Path) -> None:
        """When no config file exists, interactive mode prompts and writes a file."""
        runner = CliRunner()
        config_dir = tmp_path / "config"

        clean_env: dict[str, str] = {
            k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")
        }

        connect_result: dict[str, Any] = {"success": True, "hostname": "MyNAS"}

        # Input order: host, https(n), permission(read), alias(""),
        # username, password, hostname-confirm(y)
        with (
            patch("synology_mcp.cli._CONFIG_DIR", config_dir),
            patch("synology_mcp.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("synology_mcp.cli._store_keyring", return_value=True),
            patch("synology_mcp.cli.asyncio.run", return_value=connect_result),
            patch.dict(os.environ, clean_env, clear=True),
        ):
            result = runner.invoke(
                main,
                ["setup"],
                input="192.168.1.50\nn\nread\n\nadmin\npassword\ny\n",
            )

        assert "Let's create one" in result.output
        assert result.exit_code == 0
        assert (config_dir / "192-168-1-50.yaml").exists()

    def test_interactive_setup_aborts_on_overwrite_decline(self, tmp_path: Path) -> None:
        """If config file exists and user declines overwrite, abort."""
        runner = CliRunner()
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "192-168-1-50.yaml").write_text("old\n")

        clean_env: dict[str, str] = {
            k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")
        }

        connect_result: dict[str, Any] = {"success": True}

        with (
            patch("synology_mcp.cli._CONFIG_DIR", config_dir),
            patch("synology_mcp.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("synology_mcp.cli._store_keyring", return_value=True),
            patch("synology_mcp.cli.asyncio.run", return_value=connect_result),
            patch.dict(os.environ, clean_env, clear=True),
        ):
            result = runner.invoke(
                main,
                ["setup"],
                input="192.168.1.50\nn\nread\n\nadmin\npassword\nn\n",
            )

        assert "Aborted" in result.output

    def test_interactive_setup_with_https(self, tmp_path: Path) -> None:
        """HTTPS prompts for verify_ssl."""
        runner = CliRunner()
        config_dir = tmp_path / "config"

        clean_env: dict[str, str] = {
            k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")
        }

        connect_result: dict[str, Any] = {"success": True}

        # Prompts: host, https(y), verify_ssl(n), permission(write), alias(""),
        # username, password
        with (
            patch("synology_mcp.cli._CONFIG_DIR", config_dir),
            patch("synology_mcp.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("synology_mcp.cli._store_keyring", return_value=True),
            patch("synology_mcp.cli.asyncio.run", return_value=connect_result),
            patch.dict(os.environ, clean_env, clear=True),
        ):
            result = runner.invoke(
                main,
                ["setup"],
                # host, https(y), permission(write), alias(MyNAS), verify_ssl(n), username, password
                input="nas.local\ny\nwrite\nMyNAS\nn\nadmin\npassword\n",
            )

        assert result.exit_code == 0, result.output
        config_path = config_dir / "nas.yaml"
        assert config_path.exists()
        import yaml

        data = yaml.safe_load(config_path.read_text())
        assert data["connection"]["https"] is True
        assert data["connection"]["verify_ssl"] is False
        assert data["alias"] == "MyNAS"

    def test_interactive_setup_keyring_failure(self, tmp_path: Path) -> None:
        """When keyring fails, show env var instructions and return."""
        runner = CliRunner()
        config_dir = tmp_path / "config"

        clean_env: dict[str, str] = {
            k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")
        }

        with (
            patch("synology_mcp.cli._CONFIG_DIR", config_dir),
            patch("synology_mcp.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("synology_mcp.cli._store_keyring", return_value=False),
            patch.dict(os.environ, clean_env, clear=True),
        ):
            result = runner.invoke(
                main,
                ["setup"],
                input="192.168.1.50\nn\nread\n\nadmin\npassword\n",
            )

        assert result.exit_code == 0
        # Should NOT have written a config file since keyring failed
        assert not (config_dir / "192-168-1-50.yaml").exists()

    def test_interactive_setup_login_failure(self, tmp_path: Path) -> None:
        """When login fails, don't write a config file."""
        runner = CliRunner()
        config_dir = tmp_path / "config"

        clean_env: dict[str, str] = {
            k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")
        }

        connect_result: dict[str, Any] = {"success": False}

        with (
            patch("synology_mcp.cli._CONFIG_DIR", config_dir),
            patch("synology_mcp.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("synology_mcp.cli._store_keyring", return_value=True),
            patch("synology_mcp.cli.asyncio.run", return_value=connect_result),
            patch.dict(os.environ, clean_env, clear=True),
        ):
            result = runner.invoke(
                main,
                ["setup"],
                input="192.168.1.50\nn\nread\n\nadmin\npassword\n",
            )

        assert result.exit_code == 0
        assert not (config_dir / "192-168-1-50.yaml").exists()


class TestSetupWithConfig:
    def test_setup_with_existing_config(self, tmp_path: Path) -> None:
        """Setup with --config uses the credential flow, not interactive."""
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
            patch("synology_mcp.cli._store_keyring", return_value=True),
            patch("synology_mcp.cli.asyncio.run", return_value=None),
        ):
            result = runner.invoke(
                main,
                ["setup", "-c", str(config_file)],
                input="admin\npassword\n",
            )

        assert "Setting up credentials" in result.output
        assert "test-nas" in result.output

    def test_setup_with_config_shows_display_name(self, tmp_path: Path) -> None:
        """Setup shows alias in output when available."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "schema_version: 1\n"
            "instance_id: test-nas\n"
            "alias: My NAS\n"
            "connection:\n"
            "  host: 192.168.1.100\n"
            "modules:\n"
            "  filestation:\n"
            "    enabled: true\n"
        )
        runner = CliRunner()
        with (
            patch("synology_mcp.cli._store_keyring", return_value=True),
            patch("synology_mcp.cli.asyncio.run", return_value=None),
        ):
            result = runner.invoke(
                main,
                ["setup", "-c", str(config_file)],
                input="admin\npassword\n",
            )

        assert "My NAS" in result.output


class TestStoreKeyring:
    def test_store_keyring_success(self) -> None:
        from synology_mcp.cli import _store_keyring

        mock_kr = MagicMock()
        # keyring is imported inside the function, so mock at the import target
        with patch.dict("sys.modules", {"keyring": mock_kr}):
            result = _store_keyring("synology-mcp/test", "admin", "secret")

        assert result is True
        assert mock_kr.set_password.call_count == 2

    def test_store_keyring_failure(self) -> None:
        from synology_mcp.cli import _store_keyring

        mock_kr = MagicMock()
        mock_kr.set_password.side_effect = OSError("No backend")
        mock_errors = MagicMock()
        mock_errors.KeyringError = type("KeyringError", (Exception,), {})
        with patch.dict("sys.modules", {"keyring": mock_kr, "keyring.errors": mock_errors}):
            result = _store_keyring("synology-mcp/test", "admin", "secret")

        assert result is False


class TestEmitClaudeDesktopSnippet:
    def test_snippet_includes_dbus_on_linux(self, tmp_path: Path) -> None:
        """Interactive setup on Linux includes DBUS in the Claude Desktop snippet."""
        runner = CliRunner()
        config_dir = tmp_path / "config"

        clean_env: dict[str, str] = {
            k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")
        }
        clean_env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/1000/bus"

        connect_result: dict[str, Any] = {"success": True}

        with (
            patch("synology_mcp.cli._CONFIG_DIR", config_dir),
            patch("synology_mcp.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("synology_mcp.cli._store_keyring", return_value=True),
            patch("synology_mcp.cli.asyncio.run", return_value=connect_result),
            patch.dict(os.environ, clean_env, clear=True),
            patch("sys.platform", "linux"),
        ):
            result = runner.invoke(
                main,
                ["setup"],
                input="192.168.1.50\nn\nread\n\nadmin\npassword\n",
            )

        assert result.exit_code == 0
        assert "DBUS_SESSION_BUS_ADDRESS" in result.output

    def test_snippet_no_dbus_on_macos(self, tmp_path: Path) -> None:
        """On non-Linux, no DBUS env var in the snippet."""
        runner = CliRunner()
        config_dir = tmp_path / "config"

        clean_env: dict[str, str] = {
            k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")
        }

        connect_result: dict[str, Any] = {"success": True}

        with (
            patch("synology_mcp.cli._CONFIG_DIR", config_dir),
            patch("synology_mcp.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("synology_mcp.cli._store_keyring", return_value=True),
            patch("synology_mcp.cli.asyncio.run", return_value=connect_result),
            patch.dict(os.environ, clean_env, clear=True),
            patch("sys.platform", "darwin"),
        ):
            result = runner.invoke(
                main,
                ["setup"],
                input="192.168.1.50\nn\nread\n\nadmin\npassword\n",
            )

        assert result.exit_code == 0
        assert "DBUS_SESSION_BUS_ADDRESS" not in result.output


class TestCheckCommand:
    def test_check_with_valid_config(self, tmp_path: Path) -> None:
        """Check command loads config and attempts login."""
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
        with patch("synology_mcp.cli.asyncio.run", return_value=None):
            result = runner.invoke(main, ["check", "-c", str(config_file)])

        assert "Checking credentials" in result.output
        assert "test-nas" in result.output

    def test_check_uses_display_name(self, tmp_path: Path) -> None:
        """Check shows alias when available."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "schema_version: 1\n"
            "alias: My Server\n"
            "connection:\n"
            "  host: 192.168.1.100\n"
            "modules:\n"
            "  filestation:\n"
            "    enabled: true\n"
        )
        runner = CliRunner()
        with patch("synology_mcp.cli.asyncio.run", return_value=None):
            result = runner.invoke(main, ["check", "-c", str(config_file)])

        assert "My Server" in result.output


class TestEnvVarMode:
    def test_serve_env_var_mode(self) -> None:
        """When SYNOLOGY_HOST is set and no config file, synthesize config."""
        from synology_mcp.core.config import _synthesize_env_config

        env = {"SYNOLOGY_HOST": "10.0.0.5"}
        with patch.dict(os.environ, env, clear=False):
            config = _synthesize_env_config()

        assert config is not None
        assert config.connection is not None
        assert config.connection.host == "10.0.0.5"
        assert config.instance_id == "10-0-0-5"
        assert config.modules["filestation"].permission == "read"

    def test_no_env_var_returns_none(self) -> None:
        from synology_mcp.core.config import _synthesize_env_config

        clean_env: dict[str, str] = {
            k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")
        }
        with patch.dict(os.environ, clean_env, clear=True):
            config = _synthesize_env_config()

        assert config is None

    def test_load_config_falls_back_to_env(self, tmp_path: Path) -> None:
        """load_config falls back to env-var mode when no config file exists."""
        from synology_mcp.core.config import load_config

        env: dict[str, str] = {
            "SYNOLOGY_HOST": "10.0.0.99",
        }
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")}
        clean_env.update(env)

        with patch.dict(os.environ, clean_env, clear=True):
            config = load_config(None)

        assert config is not None
        assert config.connection is not None
        assert config.connection.host == "10.0.0.99"

    def test_load_config_explicit_path_no_fallback(self) -> None:
        """Explicit --config path should not fall back to env-var mode."""
        import pytest

        from synology_mcp.core.config import load_config

        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yaml")

    def test_env_var_mode_with_port_override(self) -> None:
        """Env vars can override port and https in synthesized config."""
        from synology_mcp.core.config import _synthesize_env_config

        env: dict[str, str] = {
            "SYNOLOGY_HOST": "nas.local",
            "SYNOLOGY_PORT": "5001",
            "SYNOLOGY_HTTPS": "true",
        }
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")}
        clean_env.update(env)

        with patch.dict(os.environ, clean_env, clear=True):
            config = _synthesize_env_config()

        assert config is not None
        assert config.connection is not None
        assert config.connection.port == 5001
        assert config.connection.https is True


class TestAliasField:
    def test_alias_in_config(self) -> None:
        raw: dict[str, Any] = {
            "schema_version": 1,
            "alias": "HomeNAS",
            "connection": {"host": "192.168.1.100"},
            "modules": {"filestation": {"enabled": True}},
        }
        from synology_mcp.core.config import AppConfig

        config = AppConfig(**raw)
        assert config.alias == "HomeNAS"
        assert config.display_name == "HomeNAS"

    def test_display_name_falls_back_to_instance_id(self) -> None:
        raw: dict[str, Any] = {
            "schema_version": 1,
            "connection": {"host": "192.168.1.100"},
            "modules": {"filestation": {"enabled": True}},
        }
        from synology_mcp.core.config import AppConfig

        config = AppConfig(**raw)
        assert config.alias is None
        assert config.display_name == "192-168-1-100"

    def test_display_name_with_alias_and_instance_id(self) -> None:
        raw: dict[str, Any] = {
            "schema_version": 1,
            "instance_id": "my-nas",
            "alias": "Office NAS",
            "connection": {"host": "10.0.0.1"},
            "modules": {"filestation": {"enabled": True}},
        }
        from synology_mcp.core.config import AppConfig

        config = AppConfig(**raw)
        assert config.display_name == "Office NAS"


class TestFetchDsmInfo:
    async def test_fetch_dsm_info_not_in_cache(self) -> None:
        """When SYNO.DSM.Info is not in the API cache, return empty dict."""
        from synology_mcp.core.client import DsmClient

        async with DsmClient(base_url="http://nas:5000") as client:
            result = await client.fetch_dsm_info()
        assert result == {}

    async def test_fetch_dsm_info_in_cache(self) -> None:
        """When SYNO.DSM.Info is available, call getinfo and return data."""
        import respx

        from synology_mcp.core.client import DsmClient
        from synology_mcp.core.state import ApiInfoEntry

        with respx.mock:
            respx.get("http://nas:5000/webapi/entry.cgi").respond(
                json={
                    "success": True,
                    "data": {
                        "model": "DS1618+",
                        "hostname": "MyNAS",
                        "version_string": "DSM 7.1.1-42962 Update 6",
                    },
                }
            )
            async with DsmClient(base_url="http://nas:5000") as client:
                client._api_cache = {
                    "SYNO.DSM.Info": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2),
                }
                result = await client.fetch_dsm_info()

        assert result["hostname"] == "MyNAS"
        assert result["model"] == "DS1618+"
