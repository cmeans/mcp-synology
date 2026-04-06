"""Tests for core/config.py — config loading, validation, env var merging."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

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
