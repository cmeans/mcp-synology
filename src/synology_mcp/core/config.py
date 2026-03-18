"""YAML config loading + Pydantic validation.

Four-step loading:
1. Discover config file path
2. Parse YAML
3. Merge environment variable overrides
4. Apply defaults and validate with Pydantic
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1

# Environment variable mapping: env var name -> dotted config path
ENV_VAR_MAP: dict[str, str] = {
    "SYNOLOGY_HOST": "connection.host",
    "SYNOLOGY_PORT": "connection.port",
    "SYNOLOGY_HTTPS": "connection.https",
    "SYNOLOGY_USERNAME": "auth.username",
    "SYNOLOGY_PASSWORD": "auth.password",
    "SYNOLOGY_DEVICE_ID": "auth.device_id",
    "SYNOLOGY_INSTANCE_ID": "instance_id",
    "SYNOLOGY_LOG_LEVEL": "logging.level",
}


class ConnectionConfig(BaseModel):
    """NAS connection settings."""

    host: str
    port: int | None = None
    https: bool = False
    verify_ssl: bool = True
    timeout: int = Field(default=30, ge=1, le=600)


class AuthConfig(BaseModel):
    """Authentication credentials (last-resort plaintext)."""

    username: str | None = None
    password: str | None = None
    device_id: str | None = None


class ModuleConfig(BaseModel):
    """Per-module configuration."""

    enabled: bool = True
    permission: Literal["read", "write", "admin"] = "read"
    settings: dict[str, Any] = Field(default_factory=dict)


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: Literal["debug", "info", "warning", "error"] = "info"
    file: str | None = None


class AppConfig(BaseModel, extra="forbid"):
    """Top-level application configuration.

    extra="forbid" catches typos in top-level keys.
    """

    schema_version: int
    instance_id: str | None = None
    alias: str | None = None
    check_for_updates: bool = True
    connection: ConnectionConfig | None = None
    auth: AuthConfig = Field(default_factory=AuthConfig)
    modules: dict[str, ModuleConfig]
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @property
    def display_name(self) -> str:
        """Human-friendly name: alias > instance_id > host."""
        if self.alias:
            return self.alias
        if self.instance_id:
            return self.instance_id
        if self.connection:
            return self.connection.host
        return "synology"

    @model_validator(mode="after")
    def _validate_config(self) -> AppConfig:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            msg = (
                f"Config schema_version is {self.schema_version}, "
                f"but this server expects {CURRENT_SCHEMA_VERSION}."
            )
            raise ValueError(msg)

        if self.connection is None:
            msg = (
                "connection.host is required. "
                "Set it in the config file or via the SYNOLOGY_HOST environment variable."
            )
            raise ValueError(msg)

        if not self.modules:
            msg = "At least one module must be configured."
            raise ValueError(msg)

        # Apply default port from https setting
        if self.connection.port is None:
            self.connection.port = 5001 if self.connection.https else 5000

        # Derive instance_id from host if not set
        if self.instance_id is None:
            self.instance_id = _derive_instance_id(self.connection.host)

        # Validate instance_id format
        if self.instance_id and not re.match(r"^[a-z0-9-]+$", self.instance_id.lower()):
            msg = (
                f"instance_id '{self.instance_id}' contains invalid characters. "
                "Only alphanumeric characters and hyphens are allowed."
            )
            raise ValueError(msg)
        if self.instance_id:
            self.instance_id = self.instance_id.lower()

        return self


def _derive_instance_id(host: str) -> str:
    """Derive an instance ID from the host.

    IP addresses: dots -> hyphens (192.168.1.100 -> 192-168-1-100)
    Hostnames: first component (nas.local -> nas)
    """
    # IP address pattern
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        return host.replace(".", "-")
    # Hostname: take first component
    return host.split(".")[0].lower()


def discover_config_path(explicit_path: str | None = None) -> Path:
    """Find the config file using the discovery hierarchy.

    1. Explicit --config flag
    2. SYNOLOGY_MCP_CONFIG env var
    3. ~/.config/synology-mcp/config.yaml
    4. ./synology-mcp.yaml
    """
    if explicit_path:
        path = Path(explicit_path).expanduser()
        logger.debug("Config path from --config flag: %s", path)
        if not path.exists():
            msg = f"Config file not found: {path}"
            raise FileNotFoundError(msg)
        return path

    env_path = os.environ.get("SYNOLOGY_MCP_CONFIG")
    if env_path:
        path = Path(env_path).expanduser()
        logger.debug("Config path from SYNOLOGY_MCP_CONFIG env: %s", path)
        if not path.exists():
            msg = f"Config file not found (from SYNOLOGY_MCP_CONFIG): {path}"
            raise FileNotFoundError(msg)
        return path

    default_paths = [
        Path.home() / ".config" / "synology-mcp" / "config.yaml",
        Path.cwd() / "synology-mcp.yaml",
    ]
    for path in default_paths:
        logger.debug("Checking default config path: %s", path)
        if path.exists():
            logger.debug("Config found: %s", path)
            return path

    msg = (
        "No config file found. Create one at ~/.config/synology-mcp/config.yaml "
        "or specify with --config. See examples/ for sample configs."
    )
    raise FileNotFoundError(msg)


def _merge_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge environment variable overrides into raw config dict."""
    for env_var, dotted_path in ENV_VAR_MAP.items():
        value = os.environ.get(env_var)
        if value is None:
            continue

        logger.debug("Env override: %s -> %s", env_var, dotted_path)
        parts = dotted_path.split(".")
        target = raw
        for part in parts[:-1]:
            if part not in target:
                target[part] = {}
            target = target[part]

        key = parts[-1]
        # Type coercion for known types
        if key == "port":
            target[key] = int(value)
        elif key == "https":
            target[key] = value.lower() in ("true", "1", "yes")
        else:
            target[key] = value

    return raw


def _emit_warnings(config: AppConfig) -> None:
    """Emit warnings for insecure or unusual config."""
    if config.auth.username or config.auth.password:
        creds_from_env = bool(
            os.environ.get("SYNOLOGY_USERNAME") or os.environ.get("SYNOLOGY_PASSWORD")
        )
        if creds_from_env:
            logger.warning(
                "Credentials provided via environment variables. "
                "Consider using 'synology-mcp setup' to store them securely in the OS keyring."
            )
        else:
            logger.warning(
                "Plaintext credentials found in config file. "
                "Use 'synology-mcp setup' to store credentials securely in the OS keyring."
            )

    conn = config.connection
    if conn is not None:
        if not conn.https:
            logger.info(
                "HTTPS is disabled. Credentials will be sent in cleartext. "
                "Acceptable on a trusted LAN."
            )
        if conn.https and not conn.verify_ssl:
            logger.warning(
                "SSL certificate verification is disabled. "
                "Only use this on trusted networks with self-signed certificates."
            )

    for name, mod in config.modules.items():
        if not mod.enabled:
            logger.info("Module '%s' is listed but disabled.", name)


def _synthesize_env_config() -> AppConfig | None:
    """Synthesize a config from environment variables when no config file exists.

    Returns an AppConfig if SYNOLOGY_HOST is set, otherwise None.
    """
    host = os.environ.get("SYNOLOGY_HOST")
    if not host:
        return None

    logger.debug("No config file found; synthesizing from SYNOLOGY_HOST=%s", host)
    raw: dict[str, Any] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "connection": {"host": host},
        "modules": {"filestation": {"enabled": True, "permission": "read"}},
    }
    raw = _merge_env_overrides(raw)
    config = AppConfig(**raw)
    _emit_warnings(config)
    return config


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load, merge, validate, and return the application config.

    Args:
        path: Explicit config file path, or None for auto-discovery.

    Falls back to env-var-only mode if no config file is found but
    SYNOLOGY_HOST is set.
    """
    try:
        config_path = discover_config_path(str(path) if path else None)
    except FileNotFoundError:
        # If an explicit path was given, always fail
        if path:
            raise
        # Try env-var-only mode
        env_config = _synthesize_env_config()
        if env_config:
            return env_config
        raise

    logger.debug("Loading config from %s", config_path)
    raw_text = config_path.read_text(encoding="utf-8")
    raw: dict[str, Any] = yaml.safe_load(raw_text) or {}

    raw = _merge_env_overrides(raw)

    config = AppConfig(**raw)

    logger.debug(
        "Config loaded: instance_id=%s, host=%s, port=%s, https=%s, modules=%s",
        config.instance_id,
        config.connection.host if config.connection else None,
        config.connection.port if config.connection else None,
        config.connection.https if config.connection else None,
        list(config.modules.keys()),
    )
    for name, mod in config.modules.items():
        logger.debug("  Module '%s': enabled=%s, permission=%s", name, mod.enabled, mod.permission)

    _emit_warnings(config)
    return config
