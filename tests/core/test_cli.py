"""Tests for cli package — CLI subcommands via click CliRunner."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from mcp_synology import __version__
from mcp_synology.cli import main


class TestCli:
    def test_version(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_version_short_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["-v"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "mcp-synology" in result.output

    def test_help_short_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["-h"])
        assert result.exit_code == 0
        assert "mcp-synology" in result.output

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

    def test_serve_malformed_yaml_clean_error(self, tmp_path: Path) -> None:
        """serve with a malformed YAML config should exit 1 with a clean Error: line."""
        config_file = tmp_path / "malformed.yaml"
        config_file.write_text("schema_version: 1\nconnection: {host: 1.2.3.4\n")

        runner = CliRunner()
        result = runner.invoke(main, ["serve", "-c", str(config_file)])
        assert result.exit_code == 1
        assert "Error:" in result.output
        assert "Traceback" not in result.output

    def test_setup_malformed_yaml_clean_error(self, tmp_path: Path) -> None:
        """setup with a malformed YAML config should exit 1 with a clean Error: line."""
        config_file = tmp_path / "malformed.yaml"
        config_file.write_text("schema_version: 1\nconnection: {host: 1.2.3.4\n")

        runner = CliRunner()
        result = runner.invoke(main, ["setup", "-c", str(config_file)])
        assert result.exit_code == 1
        assert "Error:" in result.output
        assert "Traceback" not in result.output

    def test_setup_discovery_malformed_yaml_clean_error(self, tmp_path: Path) -> None:
        """setup (no -c) with a malformed discovered config should exit cleanly.

        Exercises the discovery path in setup.py that uses load_config(None).
        Uses MCP_SYNOLOGY_CONFIG to force discovery to point at the bad file.
        """
        config_file = tmp_path / "discovered.yaml"
        config_file.write_text("schema_version: 1\nconnection: {host: 1.2.3.4\n")

        runner = CliRunner()
        result = runner.invoke(main, ["setup"], env={"MCP_SYNOLOGY_CONFIG": str(config_file)})
        assert result.exit_code == 1
        assert "Error:" in result.output
        assert "Traceback" not in result.output

    # --- Pydantic ValidationError handling (closes #34) ---
    #
    # Same pattern as the malformed-YAML tests above, exercised at all four
    # load_config() call sites. The reproducer is a config that parses as
    # valid YAML but fails AppConfig's top-level Pydantic validation —
    # AppConfig declares `extra="forbid"`, so an unknown top-level key
    # triggers a ValidationError reliably regardless of which fields the
    # config sets.

    _BAD_VALIDATION_CONFIG = (
        "schema_version: 1\n"
        "connection:\n"
        "  host: 1.2.3.4\n"
        "modules:\n"
        "  filestation: {}\n"
        "totally_unknown_top_level_field: oops\n"
    )

    def test_serve_validation_error_clean_error(self, tmp_path: Path) -> None:
        """serve with a Pydantic-invalid config exits 1 with no traceback."""
        config_file = tmp_path / "bad.yaml"
        config_file.write_text(self._BAD_VALIDATION_CONFIG)

        runner = CliRunner()
        result = runner.invoke(main, ["serve", "-c", str(config_file)])
        assert result.exit_code == 1
        assert "Error: Configuration validation failed" in result.output
        assert "totally_unknown_top_level_field" in result.output
        assert "Traceback" not in result.output

    def test_check_validation_error_clean_error(self, tmp_path: Path) -> None:
        """check with a Pydantic-invalid config exits 1 with no traceback."""
        config_file = tmp_path / "bad.yaml"
        config_file.write_text(self._BAD_VALIDATION_CONFIG)

        runner = CliRunner()
        result = runner.invoke(main, ["check", "-c", str(config_file)])
        assert result.exit_code == 1
        assert "Error: Configuration validation failed" in result.output
        assert "totally_unknown_top_level_field" in result.output
        assert "Traceback" not in result.output

    def test_setup_validation_error_clean_error(self, tmp_path: Path) -> None:
        """setup -c with a Pydantic-invalid config exits 1 with no traceback."""
        config_file = tmp_path / "bad.yaml"
        config_file.write_text(self._BAD_VALIDATION_CONFIG)

        runner = CliRunner()
        result = runner.invoke(main, ["setup", "-c", str(config_file)])
        assert result.exit_code == 1
        assert "Error: Configuration validation failed" in result.output
        assert "totally_unknown_top_level_field" in result.output
        assert "Traceback" not in result.output

    def test_setup_discovery_validation_error_clean_error(self, tmp_path: Path) -> None:
        """setup (no -c) discovery path: Pydantic-invalid config exits cleanly."""
        config_file = tmp_path / "discovered.yaml"
        config_file.write_text(self._BAD_VALIDATION_CONFIG)

        runner = CliRunner()
        result = runner.invoke(main, ["setup"], env={"MCP_SYNOLOGY_CONFIG": str(config_file)})
        assert result.exit_code == 1
        assert "Error: Configuration validation failed" in result.output
        assert "totally_unknown_top_level_field" in result.output
        assert "Traceback" not in result.output

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
        with patch("mcp_synology.cli.setup._CONFIG_DIR", tmp_path / "nonexistent"):
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
        with patch("mcp_synology.cli.setup._CONFIG_DIR", tmp_path):
            result = runner.invoke(main, ["setup", "--list"])
        assert result.exit_code == 0
        assert "my-nas.yaml" in result.output
        assert "HomeNAS" in result.output
        assert "192.168.1.100" in result.output

    def test_list_short_flag(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with patch("mcp_synology.cli.setup._CONFIG_DIR", tmp_path / "nonexistent"):
            result = runner.invoke(main, ["setup", "-l"])
        assert result.exit_code == 0
        assert "No configurations found" in result.output

    def test_list_empty_directory(self, tmp_path: Path) -> None:
        """Config dir exists but has no .yaml files."""
        runner = CliRunner()
        with patch("mcp_synology.cli.setup._CONFIG_DIR", tmp_path):
            result = runner.invoke(main, ["setup", "--list"])
        assert result.exit_code == 0
        assert "No configurations found" in result.output

    def test_list_with_unparseable_config(self, tmp_path: Path) -> None:
        """Gracefully handle a config file that can't be parsed."""
        bad_file = tmp_path / "broken.yaml"
        bad_file.write_text("{{{{invalid yaml")
        runner = CliRunner()
        with patch("mcp_synology.cli.setup._CONFIG_DIR", tmp_path):
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
        with patch("mcp_synology.cli.setup._CONFIG_DIR", tmp_path):
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
            patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("mcp_synology.cli.setup._store_keyring", return_value=True),
            patch("mcp_synology.cli.setup.asyncio.run", return_value=connect_result),
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
            patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("mcp_synology.cli.setup._store_keyring", return_value=True),
            patch("mcp_synology.cli.setup.asyncio.run", return_value=connect_result),
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
            patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("mcp_synology.cli.setup._store_keyring", return_value=True),
            patch("mcp_synology.cli.setup.asyncio.run", return_value=connect_result),
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
            patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("mcp_synology.cli.setup._store_keyring", return_value=False),
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
            patch("mcp_synology.cli.setup._store_keyring", return_value=True),
            patch("mcp_synology.cli.setup.asyncio.run", return_value=None),
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
            patch("mcp_synology.cli.setup._store_keyring", return_value=True),
            patch("mcp_synology.cli.setup.asyncio.run", return_value=None),
        ):
            result = runner.invoke(
                main,
                ["setup", "-c", str(config_file)],
                input="admin\npassword\n",
            )

        assert "My NAS" in result.output


class TestStoreKeyring:
    def test_store_keyring_success(self) -> None:
        from mcp_synology.cli.setup import _store_keyring

        mock_kr = MagicMock()
        # keyring is imported inside the function, so mock at the import target
        with patch.dict("sys.modules", {"keyring": mock_kr}):
            result = _store_keyring("mcp-synology/test", "admin", "secret")

        assert result is True
        assert mock_kr.set_password.call_count == 2

    def test_store_keyring_failure(self) -> None:
        from mcp_synology.cli.setup import _store_keyring

        mock_kr = MagicMock()
        mock_kr.set_password.side_effect = OSError("No backend")
        mock_errors = MagicMock()
        mock_errors.KeyringError = type("KeyringError", (Exception,), {})
        with patch.dict("sys.modules", {"keyring": mock_kr, "keyring.errors": mock_errors}):
            result = _store_keyring("mcp-synology/test", "admin", "secret")

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
            patch("mcp_synology.cli.setup._CONFIG_DIR", config_dir),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
            patch("mcp_synology.cli.setup._store_keyring", return_value=True),
            patch("mcp_synology.cli.setup.asyncio.run", return_value=connect_result),
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
        with patch("mcp_synology.cli.check.asyncio.run", return_value=None):
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
        with patch("mcp_synology.cli.check.asyncio.run", return_value=None):
            result = runner.invoke(main, ["check", "-c", str(config_file)])

        assert "My Server" in result.output


class TestEnvVarMode:
    def test_serve_env_var_mode(self) -> None:
        """When SYNOLOGY_HOST is set and no config file, synthesize config."""
        from mcp_synology.core.config import _synthesize_env_config

        env = {"SYNOLOGY_HOST": "10.0.0.5"}
        with patch.dict(os.environ, env, clear=False):
            config = _synthesize_env_config()

        assert config is not None
        assert config.connection is not None
        assert config.connection.host == "10.0.0.5"
        assert config.instance_id == "10-0-0-5"
        assert config.modules["filestation"].permission == "read"

    def test_no_env_var_returns_none(self) -> None:
        from mcp_synology.core.config import _synthesize_env_config

        clean_env: dict[str, str] = {
            k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")
        }
        with patch.dict(os.environ, clean_env, clear=True):
            config = _synthesize_env_config()

        assert config is None

    def test_load_config_falls_back_to_env(self, tmp_path: Path) -> None:
        """load_config falls back to env-var mode when no config file exists."""
        from mcp_synology.core.config import load_config

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

        from mcp_synology.core.config import load_config

        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yaml")

    def test_env_var_mode_with_port_override(self) -> None:
        """Env vars can override port and https in synthesized config."""
        from mcp_synology.core.config import _synthesize_env_config

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
        from mcp_synology.core.config import AppConfig

        config = AppConfig(**raw)
        assert config.alias == "HomeNAS"
        assert config.display_name == "HomeNAS"

    def test_display_name_falls_back_to_instance_id(self) -> None:
        raw: dict[str, Any] = {
            "schema_version": 1,
            "connection": {"host": "192.168.1.100"},
            "modules": {"filestation": {"enabled": True}},
        }
        from mcp_synology.core.config import AppConfig

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
        from mcp_synology.core.config import AppConfig

        config = AppConfig(**raw)
        assert config.display_name == "Office NAS"


class TestFetchDsmInfo:
    async def test_fetch_dsm_info_not_in_cache(self) -> None:
        """When SYNO.DSM.Info is not in the API cache, return empty dict."""
        from mcp_synology.core.client import DsmClient

        async with DsmClient(base_url="http://nas:5000") as client:
            result = await client.fetch_dsm_info()
        assert result == {}

    async def test_fetch_dsm_info_in_cache(self) -> None:
        """When SYNO.DSM.Info is available, call getinfo and return data."""
        import respx

        from mcp_synology.core.client import DsmClient
        from mcp_synology.core.state import ApiInfoEntry

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


class TestCheckLogin:
    """Tests for cli/check.py:_check_login — the async login validator."""

    @staticmethod
    def _make_fake_client() -> MagicMock:
        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        fake_client.query_api_info = AsyncMock(return_value=None)
        return fake_client

    async def test_check_login_success(self) -> None:
        from mcp_synology.cli.check import _check_login
        from tests.conftest import make_test_config

        config = make_test_config()
        fake_client = self._make_fake_client()
        fake_auth = MagicMock()
        fake_auth.login = AsyncMock()
        fake_auth.logout = AsyncMock()

        with (
            patch("mcp_synology.core.client.DsmClient", return_value=fake_client),
            patch("mcp_synology.core.auth.AuthManager", return_value=fake_auth),
        ):
            await _check_login(config)

        fake_auth.login.assert_awaited_once()
        fake_auth.logout.assert_awaited_once()

    async def test_check_login_failure_synology_error(self) -> None:
        import pytest

        from mcp_synology.cli.check import _check_login
        from mcp_synology.core.errors import SynologyError
        from tests.conftest import make_test_config

        config = make_test_config()
        fake_client = self._make_fake_client()
        fake_auth = MagicMock()
        fake_auth.login = AsyncMock(side_effect=SynologyError("bad creds"))

        with (
            patch("mcp_synology.core.client.DsmClient", return_value=fake_client),
            patch("mcp_synology.core.auth.AuthManager", return_value=fake_auth),
            pytest.raises(SystemExit) as exc_info,
        ):
            await _check_login(config)

        assert exc_info.value.code == 1

    async def test_check_login_failure_oserror(self) -> None:
        import pytest

        from mcp_synology.cli.check import _check_login
        from tests.conftest import make_test_config

        config = make_test_config()
        fake_client = self._make_fake_client()
        fake_auth = MagicMock()
        fake_auth.login = AsyncMock(side_effect=OSError("connection refused"))

        with (
            patch("mcp_synology.core.client.DsmClient", return_value=fake_client),
            patch("mcp_synology.core.auth.AuthManager", return_value=fake_auth),
            pytest.raises(SystemExit) as exc_info,
        ):
            await _check_login(config)

        assert exc_info.value.code == 1

    async def test_check_login_rejects_non_appconfig(self) -> None:
        import pytest

        from mcp_synology.cli.check import _check_login

        with pytest.raises(RuntimeError, match="AppConfig"):
            await _check_login("not a config")

    async def test_check_login_rejects_missing_connection(self) -> None:
        import pytest

        from mcp_synology.cli.check import _check_login
        from tests.conftest import make_test_config

        config = make_test_config()
        config.connection = None  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="connection"):
            await _check_login(config)

    def test_check_command_invalid_config_exits_nonzero(self, tmp_path: Path) -> None:
        """check with a config that fails schema validation exits 1 with an error."""
        config_file = tmp_path / "wrong_schema.yaml"
        config_file.write_text(
            "schema_version: 999\n"
            "connection:\n"
            "  host: 1.2.3.4\n"
            "modules:\n"
            "  filestation:\n"
            "    enabled: true\n"
        )

        runner = CliRunner()
        result = runner.invoke(main, ["check", "-c", str(config_file)])
        assert result.exit_code == 1
        assert "Error" in result.output

    def test_check_command_malformed_yaml_clean_error(self, tmp_path: Path) -> None:
        """check with a malformed YAML file should exit 1 with a clean Error: line.

        Regression test — a typo or indentation error in the user's config
        used to surface as a raw yaml.ScannerError traceback because
        check.py only caught (FileNotFoundError, ValueError).
        """
        config_file = tmp_path / "malformed.yaml"
        # Unclosed brace — pyyaml raises ScannerError/ParserError
        config_file.write_text("schema_version: 1\nconnection: {host: 1.2.3.4\n")

        runner = CliRunner()
        result = runner.invoke(main, ["check", "-c", str(config_file)])
        assert result.exit_code == 1
        assert "Error:" in result.output
        # The raw traceback leaks "Traceback (most recent call last)"; our
        # clean handler should not.
        assert "Traceback" not in result.output

    def test_check_command_verbose_flag(self, tmp_path: Path) -> None:
        """--verbose enables debug logging early."""
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
        with patch("mcp_synology.cli.check.asyncio.run", return_value=None):
            result = runner.invoke(main, ["check", "-c", str(config_file), "--verbose"])
        assert result.exit_code == 0


class TestMainGroupOptions:
    """Tests for cli/main.py top-level options: --check-update, --auto-upgrade, --revert."""

    def test_check_update_with_newer_version_uv_installer(self) -> None:
        runner = CliRunner()
        with (
            patch("mcp_synology.cli.main._load_global_state", return_value={}),
            patch("mcp_synology.cli.main._save_global_state"),
            patch("mcp_synology.cli.main._check_for_update", return_value="9.9.9"),
            patch("mcp_synology.cli.main._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.main._detect_installer", return_value="uv"),
        ):
            result = runner.invoke(main, ["--check-update"])

        assert result.exit_code == 0
        assert "Update available" in result.output
        assert "0.5.0" in result.output
        assert "9.9.9" in result.output
        assert "uv tool install" in result.output

    def test_check_update_with_newer_version_pipx_installer(self) -> None:
        runner = CliRunner()
        with (
            patch("mcp_synology.cli.main._load_global_state", return_value={}),
            patch("mcp_synology.cli.main._save_global_state"),
            patch("mcp_synology.cli.main._check_for_update", return_value="9.9.9"),
            patch("mcp_synology.cli.main._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.main._detect_installer", return_value="pipx"),
        ):
            result = runner.invoke(main, ["--check-update"])

        assert result.exit_code == 0
        assert "pipx upgrade" in result.output

    def test_check_update_with_newer_version_unknown_installer(self) -> None:
        """Falls back to uv tool install when installer can't be detected."""
        runner = CliRunner()
        with (
            patch("mcp_synology.cli.main._load_global_state", return_value={}),
            patch("mcp_synology.cli.main._save_global_state"),
            patch("mcp_synology.cli.main._check_for_update", return_value="9.9.9"),
            patch("mcp_synology.cli.main._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.main._detect_installer", return_value=None),
        ):
            result = runner.invoke(main, ["--check-update"])

        assert result.exit_code == 0
        assert "uv tool install" in result.output

    def test_check_update_no_newer_version(self) -> None:
        runner = CliRunner()
        with (
            patch("mcp_synology.cli.main._load_global_state", return_value={}),
            patch("mcp_synology.cli.main._save_global_state"),
            patch("mcp_synology.cli.main._check_for_update", return_value=None),
            patch("mcp_synology.cli.main._get_current_version", return_value="0.5.0"),
        ):
            result = runner.invoke(main, ["--check-update"])

        assert result.exit_code == 0
        assert "latest version" in result.output

    def test_auto_upgrade_enable(self) -> None:
        runner = CliRunner()
        saved_state: dict[str, Any] = {}

        def _save(state: dict[str, Any]) -> None:
            saved_state.update(state)

        with (
            patch("mcp_synology.cli.main._load_global_state", return_value={}),
            patch("mcp_synology.cli.main._save_global_state", side_effect=_save),
        ):
            result = runner.invoke(main, ["--auto-upgrade", "enable"])

        assert result.exit_code == 0
        assert "enabled" in result.output
        assert saved_state["auto_upgrade"] is True

    def test_auto_upgrade_disable(self) -> None:
        runner = CliRunner()
        saved_state: dict[str, Any] = {}

        def _save(state: dict[str, Any]) -> None:
            saved_state.update(state)

        with (
            patch("mcp_synology.cli.main._load_global_state", return_value={}),
            patch("mcp_synology.cli.main._save_global_state", side_effect=_save),
        ):
            result = runner.invoke(main, ["--auto-upgrade", "disable"])

        assert result.exit_code == 0
        assert "disabled" in result.output
        assert saved_state["auto_upgrade"] is False

    def test_revert_with_flag_value_uses_previous(self) -> None:
        """--revert=__PREVIOUS__ (the click flag_value form) → _do_revert(None)."""
        runner = CliRunner()
        with patch("mcp_synology.cli.main._do_revert") as do_revert:
            result = runner.invoke(main, ["--revert=__PREVIOUS__"])
        assert result.exit_code == 0
        do_revert.assert_called_once_with(None)

    def test_revert_with_explicit_version(self) -> None:
        """--revert=0.4.1 → _do_revert("0.4.1")."""
        runner = CliRunner()
        with patch("mcp_synology.cli.main._do_revert") as do_revert:
            result = runner.invoke(main, ["--revert=0.4.1"])
        assert result.exit_code == 0
        do_revert.assert_called_once_with("0.4.1")

    def test_no_subcommand_shows_help(self) -> None:
        runner = CliRunner()
        with (
            patch("mcp_synology.cli.main._load_global_state", return_value={}),
            patch("mcp_synology.cli.main._save_global_state"),
            patch("mcp_synology.cli.main._get_current_version", return_value="0.5.0"),
        ):
            result = runner.invoke(main, [])
        assert result.exit_code == 0
        assert "Usage" in result.output

    def test_version_change_recorded_as_previous(self) -> None:
        """When running version differs from last_known, record it as previous."""
        runner = CliRunner()
        saved_state: dict[str, Any] = {}

        def _save(state: dict[str, Any]) -> None:
            saved_state.update(state)

        with (
            patch(
                "mcp_synology.cli.main._load_global_state",
                return_value={"running_version": "0.4.0"},
            ),
            patch("mcp_synology.cli.main._save_global_state", side_effect=_save),
            patch("mcp_synology.cli.main._get_current_version", return_value="0.5.0"),
        ):
            result = runner.invoke(main, [])

        assert result.exit_code == 0
        assert saved_state["previous_version"] == "0.4.0"
        assert saved_state["running_version"] == "0.5.0"

    def test_auto_upgrade_triggers_on_non_serve_subcommand(self, tmp_path: Path) -> None:
        """When auto_upgrade enabled and a non-serve subcommand runs, upgrade fires."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "schema_version: 1\n"
            "instance_id: test\n"
            "connection:\n"
            "  host: 1.2.3.4\n"
            "modules:\n"
            "  filestation:\n"
            "    enabled: true\n"
        )

        runner = CliRunner()
        with (
            patch(
                "mcp_synology.cli.main._load_global_state",
                return_value={"auto_upgrade": True},
            ),
            patch("mcp_synology.cli.main._save_global_state"),
            patch("mcp_synology.cli.main._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.main._check_for_update", return_value="9.9.9"),
            patch("mcp_synology.cli.main._do_auto_upgrade") as upgrade,
            patch("mcp_synology.cli.check.asyncio.run", return_value=None),
        ):
            runner.invoke(main, ["check", "-c", str(config_file)])

        upgrade.assert_called_once()

    def test_auto_upgrade_skipped_when_no_update(self, tmp_path: Path) -> None:
        """auto_upgrade enabled but no newer version → no upgrade attempt."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "schema_version: 1\n"
            "instance_id: test\n"
            "connection:\n"
            "  host: 1.2.3.4\n"
            "modules:\n"
            "  filestation:\n"
            "    enabled: true\n"
        )

        runner = CliRunner()
        with (
            patch(
                "mcp_synology.cli.main._load_global_state",
                return_value={"auto_upgrade": True},
            ),
            patch("mcp_synology.cli.main._save_global_state"),
            patch("mcp_synology.cli.main._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.main._check_for_update", return_value=None),
            patch("mcp_synology.cli.main._do_auto_upgrade") as upgrade,
            patch("mcp_synology.cli.check.asyncio.run", return_value=None),
        ):
            runner.invoke(main, ["check", "-c", str(config_file)])

        upgrade.assert_not_called()

    def test_serve_command_uses_create_server_and_runs_stdio(self, tmp_path: Path) -> None:
        """serve loads config, creates server, runs with stdio transport."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "schema_version: 1\n"
            "instance_id: test\n"
            "connection:\n"
            "  host: 1.2.3.4\n"
            "modules:\n"
            "  filestation:\n"
            "    enabled: true\n"
        )

        fake_server = MagicMock()
        runner = CliRunner()
        with (
            patch("mcp_synology.server.create_server", return_value=fake_server) as create,
            patch(
                "mcp_synology.cli.main._load_global_state",
                return_value={},
            ),
            patch("mcp_synology.cli.main._save_global_state"),
            patch("mcp_synology.cli.main._get_current_version", return_value="0.5.0"),
        ):
            result = runner.invoke(main, ["serve", "-c", str(config_file)])

        assert result.exit_code == 0, result.output
        create.assert_called_once()
        fake_server.run.assert_called_once_with(transport="stdio")


class TestCliLogging:
    """Tests for cli/logging_.py — early and config-driven logging setup."""

    @staticmethod
    def _reset_root_logger() -> list[Any]:
        """Snapshot and clear root logger handlers + level for an isolated test.

        Returns the saved state so the test can restore it in a finally block.
        Without this, basicConfig() no-ops when other tests have already
        attached handlers, and assertions on level become non-deterministic.
        """
        import logging

        root = logging.getLogger()
        saved = (root.level, list(root.handlers))
        for h in list(root.handlers):
            root.removeHandler(h)
        return [saved]

    @staticmethod
    def _restore_root_logger(snapshot: list[Any]) -> None:
        import logging

        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        level, handlers = snapshot[0]
        root.setLevel(level)
        for h in handlers:
            root.addHandler(h)

    def test_init_early_logging_verbose(self) -> None:
        """--verbose forces DEBUG regardless of env var."""
        import logging

        from mcp_synology.cli.logging_ import _init_early_logging

        snapshot = self._reset_root_logger()
        try:
            with patch.dict(os.environ, {"SYNOLOGY_LOG_LEVEL": "warning"}, clear=False):
                _init_early_logging(verbose=True)
            assert logging.getLogger().level == logging.DEBUG
        finally:
            self._restore_root_logger(snapshot)

    def test_init_early_logging_env_var(self) -> None:
        """SYNOLOGY_LOG_LEVEL env var sets level when not verbose."""
        import logging

        from mcp_synology.cli.logging_ import _init_early_logging

        snapshot = self._reset_root_logger()
        try:
            with patch.dict(os.environ, {"SYNOLOGY_LOG_LEVEL": "warning"}, clear=False):
                _init_early_logging(verbose=False)
            assert logging.getLogger().level == logging.WARNING
        finally:
            self._restore_root_logger(snapshot)

    def test_init_early_logging_default_level(self) -> None:
        """No env var → INFO."""
        import logging

        from mcp_synology.cli.logging_ import _init_early_logging

        snapshot = self._reset_root_logger()
        try:
            clean_env = {k: v for k, v in os.environ.items() if k != "SYNOLOGY_LOG_LEVEL"}
            with patch.dict(os.environ, clean_env, clear=True):
                _init_early_logging(verbose=False)
            assert logging.getLogger().level == logging.INFO
        finally:
            self._restore_root_logger(snapshot)

    def test_configure_logging_with_log_file(self, tmp_path: Path) -> None:
        """log_file argument adds a FileHandler to the root logger."""
        import logging

        from mcp_synology.cli.logging_ import _configure_logging

        snapshot = self._reset_root_logger()
        try:
            log_file = tmp_path / "test.log"
            _configure_logging("debug", str(log_file))

            root = logging.getLogger()
            file_handlers = [
                h
                for h in root.handlers
                if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == log_file
            ]
            assert file_handlers, "expected a FileHandler for the log_file path"
            # Close the file handle on the FileHandler so the underlying file
            # descriptor doesn't leak past tmp_path teardown — _restore_root_logger
            # only detaches handlers, it doesn't close them.
            for h in file_handlers:
                h.close()
        finally:
            self._restore_root_logger(snapshot)

    def test_configure_logging_without_log_file(self) -> None:
        """No log_file → no FileHandler added (only level changes)."""
        import logging

        from mcp_synology.cli.logging_ import _configure_logging

        snapshot = self._reset_root_logger()
        try:
            before = list(logging.getLogger().handlers)
            _configure_logging("info")
            after = list(logging.getLogger().handlers)
            # Same handlers (level may differ but we don't add new ones).
            assert len(after) == len(before)
        finally:
            self._restore_root_logger(snapshot)
