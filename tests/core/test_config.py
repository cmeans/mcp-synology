"""Tests for core/config.py — config loading, validation, env var merging."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from mcp_synology.core.config import (
    AppConfig,
    ConnectionConfig,
    LoggingConfig,
    ModuleConfig,
    _derive_instance_id,
    _merge_env_overrides,
    discover_config_path,
    load_config,
)

if TYPE_CHECKING:
    from pathlib import Path


def _minimal_raw() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "connection": {"host": "192.168.1.100"},
        "modules": {"filestation": {"enabled": True}},
    }


class TestAppConfig:
    def test_minimal_config(self) -> None:
        config = AppConfig(**_minimal_raw())
        assert config.connection is not None
        assert config.connection.host == "192.168.1.100"
        assert config.connection.port == 5000  # default for http
        assert config.instance_id == "192-168-1-100"

    def test_full_config(self) -> None:
        raw: dict[str, Any] = {
            "schema_version": 1,
            "instance_id": "nas-primary",
            "connection": {
                "host": "nas.local",
                "port": 5001,
                "https": True,
                "verify_ssl": False,
                "timeout": 60,
            },
            "auth": {"username": "admin"},
            "modules": {
                "filestation": {
                    "enabled": True,
                    "permission": "write",
                    "settings": {"async_timeout": 180},
                },
            },
            "logging": {"level": "debug", "file": "/tmp/test.log"},
        }
        config = AppConfig(**raw)
        assert config.instance_id == "nas-primary"
        assert config.connection is not None
        assert config.connection.https is True
        assert config.modules["filestation"].permission == "write"

    def test_https_default_port(self) -> None:
        raw = _minimal_raw()
        raw["connection"]["https"] = True
        config = AppConfig(**raw)
        assert config.connection is not None
        assert config.connection.port == 5001

    def test_instance_id_from_ip(self) -> None:
        config = AppConfig(**_minimal_raw())
        assert config.instance_id == "192-168-1-100"

    def test_instance_id_from_hostname(self) -> None:
        raw = _minimal_raw()
        raw["connection"]["host"] = "nas.local"
        config = AppConfig(**raw)
        assert config.instance_id == "nas"

    def test_wrong_schema_version(self) -> None:
        raw = _minimal_raw()
        raw["schema_version"] = 99
        with pytest.raises(ValueError, match="schema_version"):
            AppConfig(**raw)

    def test_missing_modules(self) -> None:
        raw = _minimal_raw()
        raw["modules"] = {}
        with pytest.raises(ValueError, match="module"):
            AppConfig(**raw)

    def test_missing_connection_host(self) -> None:
        raw: dict[str, Any] = {
            "schema_version": 1,
            "modules": {"filestation": {"enabled": True}},
        }
        with pytest.raises(ValueError, match="host"):
            AppConfig(**raw)

    def test_unknown_top_level_key_rejected(self) -> None:
        raw = _minimal_raw()
        raw["typo_key"] = "oops"
        with pytest.raises(ValueError):
            AppConfig(**raw)

    def test_unknown_module_key_rejected(self) -> None:
        """A typo directly under a module (only enabled/permission/settings exist) is an
        error: `permision: write` used to be dropped, leaving the module read-only."""
        from pydantic import ValidationError

        from mcp_synology.core.config import format_validation_error

        raw = _minimal_raw()
        raw["modules"] = {"filestation": {"enabled": True, "permision": "write"}}
        with pytest.raises(ValidationError) as exc:
            AppConfig(**raw)
        assert "modules.filestation.permision" in format_validation_error(exc.value)

    def test_invalid_instance_id_characters(self) -> None:
        raw = _minimal_raw()
        raw["instance_id"] = "bad_chars!"
        with pytest.raises(ValueError, match="invalid characters"):
            AppConfig(**raw)

    def test_invalid_permission_value(self) -> None:
        raw = _minimal_raw()
        raw["modules"]["filestation"]["permission"] = "superadmin"
        with pytest.raises(ValueError):
            AppConfig(**raw)

    def test_module_defaults(self) -> None:
        mod = ModuleConfig()
        assert mod.enabled is True
        assert mod.permission == "read"
        assert mod.settings == {}

    def test_connection_defaults(self) -> None:
        conn = ConnectionConfig(host="test")
        assert conn.https is False
        assert conn.verify_ssl is True
        assert conn.timeout == 30

    def test_logging_defaults(self) -> None:
        log = LoggingConfig()
        assert log.level == "info"
        assert log.file is None


class TestEnabledModules:
    """#168: a config whose modules are all ``enabled: false`` would start with 0 tools."""

    def test_empty_modules_rejected(self) -> None:
        raw = _minimal_raw()
        raw["modules"] = {}
        with pytest.raises(ValueError, match="At least one module must be configured"):
            AppConfig(**raw)

    def test_every_module_disabled_rejected(self) -> None:
        raw = _minimal_raw()
        raw["modules"] = {"filestation": {"enabled": False}, "system": {"enabled": False}}
        with pytest.raises(ValueError, match="No module is enabled") as exc:
            AppConfig(**raw)
        assert "filestation, system" in str(exc.value)
        assert "filestation: {enabled: true}" in str(exc.value)

    def test_one_enabled_among_disabled_accepted(self) -> None:
        raw = _minimal_raw()
        raw["modules"] = {"filestation": {"enabled": False}, "system": {"enabled": True}}
        config = AppConfig(**raw)
        assert config.modules["system"].enabled


class TestCredentialWarnings:
    """#167: only a password is a secret; a username alone gets no plaintext warning."""

    @staticmethod
    def _load(tmp_path: Path, auth: dict[str, str], env: dict[str, str] | None = None) -> None:
        raw = _minimal_raw()
        raw["auth"] = auth
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(raw))
        clean = {k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")}
        with patch.dict(os.environ, {**clean, **(env or {})}, clear=True):
            load_config(str(path))

    def test_username_only_no_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="mcp_synology.core.config")
        self._load(tmp_path, {"username": "nobody"})
        assert "Plaintext" not in caplog.text
        assert "environment variable" not in caplog.text

    def test_plain_http_is_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#193: credentials in cleartext is a WARNING (it was INFO)."""
        raw = _minimal_raw()
        raw["connection"] = {"host": "192.168.1.100", "https": False}
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(raw))
        caplog.set_level(logging.WARNING, logger="mcp_synology.core.config")
        load_config(str(path))
        record = next(r for r in caplog.records if "HTTPS is disabled" in r.getMessage())
        assert record.levelno == logging.WARNING
        assert "docs/https.md" in record.getMessage()

    def test_https_config_has_no_plain_http_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        raw = _minimal_raw()
        raw["connection"] = {"host": "192.168.1.100", "https": True}
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(raw))
        caplog.set_level(logging.DEBUG, logger="mcp_synology.core.config")
        load_config(str(path))
        assert "HTTPS is disabled" not in caplog.text

    def test_password_in_file_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="mcp_synology.core.config")
        self._load(tmp_path, {"username": "nobody", "password": "s3cret"})
        assert "Plaintext password found in config file" in caplog.text

    def test_password_from_env_gets_env_wording(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="mcp_synology.core.config")
        self._load(tmp_path, {"username": "nobody"}, {"SYNOLOGY_PASSWORD": "s3cret"})
        assert "SYNOLOGY_PASSWORD environment variable" in caplog.text
        assert "Plaintext" not in caplog.text

    def test_username_from_env_only_no_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="mcp_synology.core.config")
        self._load(tmp_path, {}, {"SYNOLOGY_USERNAME": "nobody"})
        assert "Plaintext" not in caplog.text
        assert "environment variable" not in caplog.text


class TestDeriveInstanceId:
    def test_ip_address(self) -> None:
        assert _derive_instance_id("192.168.1.100") == "192-168-1-100"

    def test_hostname(self) -> None:
        assert _derive_instance_id("nas.local") == "nas"

    def test_simple_hostname(self) -> None:
        assert _derive_instance_id("mynas") == "mynas"


class TestEnvOverrides:
    def test_host_override(self) -> None:
        raw: dict[str, Any] = {}
        with patch.dict(os.environ, {"SYNOLOGY_HOST": "10.0.0.1"}):
            result = _merge_env_overrides(raw)
        assert result["connection"]["host"] == "10.0.0.1"

    def test_port_coerced_to_int(self) -> None:
        raw: dict[str, Any] = {"connection": {}}
        with patch.dict(os.environ, {"SYNOLOGY_PORT": "5001"}):
            result = _merge_env_overrides(raw)
        assert result["connection"]["port"] == 5001

    def test_https_coerced_to_bool(self) -> None:
        raw: dict[str, Any] = {"connection": {}}
        with patch.dict(os.environ, {"SYNOLOGY_HTTPS": "true"}):
            result = _merge_env_overrides(raw)
        assert result["connection"]["https"] is True

    def test_update_notices_override(self) -> None:
        raw: dict[str, Any] = {}
        with patch.dict(os.environ, {"SYNOLOGY_UPDATE_NOTICES": "security"}):
            result = _merge_env_overrides(raw)
        assert result["update_notices"] == "security"

    def test_env_does_not_override_when_not_set(self) -> None:
        raw: dict[str, Any] = {"connection": {"host": "original"}}
        env = {k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")}
        with patch.dict(os.environ, env, clear=True):
            result = _merge_env_overrides(raw)
        assert result["connection"]["host"] == "original"


class TestDiscoverConfigPath:
    def test_explicit_path(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("schema_version: 1\n")
        result = discover_config_path(str(config_file))
        assert result == config_file

    def test_explicit_path_not_found(self) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            discover_config_path("/nonexistent/config.yaml")

    def test_env_var_path(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("schema_version: 1\n")
        with patch.dict(os.environ, {"MCP_SYNOLOGY_CONFIG": str(config_file)}):
            result = discover_config_path()
        assert result == config_file


class TestLoadConfig:
    def test_load_minimal(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "schema_version: 1\n"
            "connection:\n"
            "  host: 192.168.1.100\n"
            "modules:\n"
            "  filestation:\n"
            "    enabled: true\n"
        )
        config = load_config(config_file)
        assert config.connection is not None
        assert config.connection.host == "192.168.1.100"
        assert "filestation" in config.modules


class TestTopLevelAllowedLocalPaths:
    """#257: one allowed_local_paths for every module, at the top level."""

    def test_accepts_home_relative_entries(self) -> None:
        config = AppConfig(**{**_minimal_raw(), "allowed_local_paths": ["~/x", "/srv/in"]})
        assert config.allowed_local_paths == ["~/x", "/srv/in"]

    def test_unset_by_default(self) -> None:
        assert AppConfig(**_minimal_raw()).allowed_local_paths is None

    @pytest.mark.parametrize("value", [["relative/dir"], []])
    def test_rejects_relative_or_empty(self, value: list[str]) -> None:
        with pytest.raises(ValidationError, match="allowed_local_paths"):
            AppConfig(**{**_minimal_raw(), "allowed_local_paths": value})

    def test_unknown_top_level_key_still_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AppConfig(**{**_minimal_raw(), "allowed_local_path": ["/x"]})


class TestUpdateNotices:
    """Per config, since configs may run different installs (#314)."""

    def test_defaults_to_all(self) -> None:
        assert AppConfig(**_minimal_raw()).update_notices == "all"

    @pytest.mark.parametrize("value", ["all", "security", "off"])
    def test_accepts_the_three_settings(self, value: str) -> None:
        assert AppConfig(**{**_minimal_raw(), "update_notices": value}).update_notices == value

    def test_rejects_anything_else(self) -> None:
        with pytest.raises(ValidationError, match="update_notices"):
            AppConfig(**{**_minimal_raw(), "update_notices": "weekly"})
