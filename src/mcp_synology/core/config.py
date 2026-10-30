"""YAML config loading + Pydantic validation.

Four-step loading:
1. Discover config file path
2. Parse YAML
3. Merge environment variable overrides
4. Apply defaults and validate with Pydantic
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from mcp_synology.core.local_paths import check_local_path_entries

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
    "SYNOLOGY_UPDATE_NOTICES": "update_notices",
}


class ConnectionConfig(BaseModel, extra="forbid"):
    """NAS connection settings.

    extra="forbid", as for every config section but module settings (#386): a typo
    here is an error, not a dropped key. `htps: true` used to load with `https` false,
    so the server logged in, password included, over plain HTTP.
    """

    host: str
    port: int | None = None
    https: bool = False
    verify_ssl: bool = True
    timeout: int = Field(default=30, ge=1, le=600)


class AuthConfig(BaseModel, extra="forbid"):
    """Credentials that override the keyring; a password here is plaintext on disk.

    extra="forbid" (#386): `pasword:` used to be dropped without a word.
    """

    username: str | None = None
    password: str | None = None
    device_id: str | None = None


class ModuleConfig(BaseModel, extra="forbid"):
    """Per-module configuration.

    extra="forbid": only these three keys exist, so any other is a typo (`permision:
    write` used to be dropped, leaving the module read-only). Unknown keys in
    `settings` and unknown module names are warnings instead (`server.config_notices`),
    so a config written for a newer version still loads.
    """

    enabled: bool = True
    permission: Literal["read", "write", "admin"] = "read"
    settings: dict[str, Any] = Field(default_factory=dict)


class LoggingConfig(BaseModel, extra="forbid"):
    """Logging configuration. extra="forbid" (#386): `levle: debug` is an error."""

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
    # Which update notices reach the model for this connection (#314): every new version
    # (ordinary ones once a week, security releases every session), only security
    # releases, or none. The log line and `mcp-synology check` report either way.
    update_notices: Literal["all", "security", "off"] = "all"
    custom_instructions: str | None = None
    instructions_file: str | None = None
    connection: ConnectionConfig | None = None
    auth: AuthConfig = Field(default_factory=AuthConfig)
    modules: dict[str, ModuleConfig]
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    # Local folders every module's tools may use; each module's own allowed_local_paths
    # adds to it (#257). Unset keeps the per-module rule from before #257.
    allowed_local_paths: list[str] | None = Field(default=None, min_length=1)

    @field_validator("update_notices", mode="before")
    @classmethod
    def _yaml_bool_notices(cls, value: object) -> object:
        # YAML 1.1 reads an unquoted `off` as false (and `on` as true), so
        # `update_notices: off` arrives as a bool.
        if value is False:
            return "off"
        if value is True:
            return "all"
        return value

    @field_validator("allowed_local_paths")
    @classmethod
    def _absolute_local_paths(cls, value: list[str] | None) -> list[str] | None:
        return check_local_path_entries("allowed_local_paths", value)

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
            msg = (
                "At least one module must be configured, "
                "e.g. modules: {filestation: {enabled: true}}."
            )
            raise ValueError(msg)
        if not any(mod.enabled for mod in self.modules.values()):
            # A server with no enabled module has 0 tools: almost certainly a config mistake.
            listed = ", ".join(sorted(self.modules))
            msg = (
                f"No module is enabled (listed with enabled: false: {listed}). "
                "Enable at least one, e.g. filestation: {enabled: true}."
            )
            raise ValueError(msg)

        # Apply default port from https setting
        if self.connection.port is None:
            self.connection.port = 5001 if self.connection.https else 5000

        # Derive instance_id from host if not set
        derived = self.instance_id is None
        if derived:
            self.instance_id = _derive_instance_id(self.connection.host)

        # Normalize and validate instance_id format
        if self.instance_id:
            self.instance_id = self.instance_id.lower()
            if not re.match(r"^[a-z0-9-]+$", self.instance_id):
                # A derived one isn't quoted: a host written as a URL can carry a password
                # (`https://admin:…@nas`, #386).
                what = (
                    "The instance_id derived from connection.host"
                    if derived
                    else f"instance_id '{self.instance_id}'"
                )
                msg = (
                    f"{what} contains invalid characters. Only lowercase alphanumeric "
                    "characters and hyphens are allowed"
                    + (
                        ": set connection.host to a bare host name or address, or set instance_id."
                        if derived
                        else "."
                    )
                )
                raise ValueError(msg)

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
    2. MCP_SYNOLOGY_CONFIG env var
    3. ~/.config/mcp-synology/config.yaml
    4. ./mcp-synology.yaml
    """
    if explicit_path:
        path = Path(explicit_path).expanduser()
        logger.debug("Config path from --config flag: %s", path)
        if not path.exists():
            msg = f"Config file not found: {path}"
            raise FileNotFoundError(msg)
        return path

    env_path = os.environ.get("MCP_SYNOLOGY_CONFIG")
    if env_path:
        path = Path(env_path).expanduser()
        logger.debug("Config path from MCP_SYNOLOGY_CONFIG env: %s", path)
        if not path.exists():
            msg = f"Config file not found (from MCP_SYNOLOGY_CONFIG): {path}"
            raise FileNotFoundError(msg)
        return path

    default_paths = [
        Path.home() / ".config" / "mcp-synology" / "config.yaml",
        Path.cwd() / "mcp-synology.yaml",
    ]
    for path in default_paths:
        logger.debug("Checking default config path: %s", path)
        if path.exists():
            logger.debug("Config found: %s", path)
            return path

    msg = (
        "No config file found. Create one at ~/.config/mcp-synology/config.yaml "
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
    # A username alone isn't a secret (keyring password + config username is the
    # recommended setup), so only a password triggers the warning (#167).
    if config.auth.password:
        if os.environ.get("SYNOLOGY_PASSWORD"):
            logger.warning(
                "Password provided via the SYNOLOGY_PASSWORD environment variable. "
                "Consider using 'mcp-synology setup' to store them securely in the OS keyring."
            )
        else:
            logger.warning(
                "Plaintext password found in config file. "
                "Use 'mcp-synology setup' to store credentials securely in the OS keyring."
            )

    conn = config.connection
    if conn is not None:
        if not conn.https:
            # A WARNING, as the risk warrants; it also survives SYNOLOGY_LOG_LEVEL=warning (#193).
            logger.warning(
                "HTTPS is disabled. Credentials will be sent in cleartext. "
                "Acceptable on a trusted LAN; to turn it on, see https://github.com/cmeans/mcp-synology/blob/main/docs/https.md"
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


def _may_show(err: Any, loc_parts: list[str]) -> bool:
    """Whether an error's rejected value may be shown: only a plain value (text, a number,
    true or false) outside `auth:`, and never an unknown key's (`pasword:` holds the
    password wherever it's misplaced, #386). A section or the whole config, which a root or
    missing-field error carries as its input (`auth` and env-var overrides included), is
    never shown."""
    if not isinstance(err.get("input"), str | int | float | bool):
        return False
    # A mapping key that failed (`settings.2024.[key]`): the input is the key itself.
    if "[key]" in err.get("loc", ()):
        return False
    # An unknown key's value, or a section given a plain value (`connection: https://
    # admin:…@nas` is a "should be a dictionary" error carrying that text).
    if err.get("type") in (
        "extra_forbidden",
        "invalid_key",
        "model_type",
        "dict_type",
        "model_attributes_type",
    ):
        return False
    return not (loc_parts and loc_parts[0] == "auth")


def _section_keys(loc_parts: list[str]) -> list[str]:
    """The keys the config section holding this location defines."""
    models: dict[str, type[BaseModel]] = {
        "connection": ConnectionConfig,
        "auth": AuthConfig,
        "logging": LoggingConfig,
    }
    if len(loc_parts) == 1:
        model: type[BaseModel] = AppConfig
    elif loc_parts[0] == "modules" and len(loc_parts) == 3:
        model = ModuleConfig
    else:
        model = models.get(loc_parts[0], AppConfig)
    return list(model.model_fields)


def _shown_location(err: Any, loc_parts: list[str]) -> str:
    """The error's location, except an unknown key that may be part of a password.

    YAML splits a value with a comma inside a flow mapping (`{password: a,Hunter2x}`): the
    second half arrives as a key with no value, in any section (#386). So an unknown key
    isn't named when it has no value, or when it's under `auth`, unless it's a near-miss of
    a key its section defines (`htps`, `pasword`, `levle` are named). An unknown key with a
    value elsewhere (`totally_unknown: x`) is named, as before.
    """
    loc = ".".join(loc_parts) if loc_parts else "<root>"
    raw_loc = err.get("loc", ())
    if "[key]" in raw_loc:
        # A key of a free-form mapping (module settings) that isn't text: `{x: Hunter,2024}`
        # fails as `<settings>.2024.[key]`. Name the mapping, not the key.
        cut = raw_loc.index("[key]") - 1
        where = ".".join(str(p) for p in raw_loc[:cut])
        return (
            (f"{where}." if where else "")
            + "<a key that isn't text, not shown: it may be part of a password; quote a "
            "value with a comma inside {…}>"
        )
    if err.get("type") == "invalid_key" and loc_parts:
        # A key YAML read as a number or a date (`{password: Summer,2024}` splits into a
        # key 2024): never a real config key, and possibly part of a password (QA round 1).
        where = ".".join(loc_parts[:-1])
        return (
            (f"{where}." if where else "")
            + "<a key that isn't text, not shown: it may be part of a password; quote a "
            "value with a comma inside {…}>"
        )
    if err.get("type") == "extra_forbidden" and loc_parts:
        key = loc_parts[-1]
        suspect = err.get("input") is None or loc_parts[0] == "auth"
        valid = _section_keys(loc_parts)
        if suspect and not difflib.get_close_matches(
            key.lower(), [k.lower() for k in valid], n=1, cutoff=0.75
        ):
            where = ".".join(loc_parts[:-1])
            return (
                (f"{where}." if where else "")
                + "<an unknown key, not shown since it may hold or be part of a password (a "
                "comma inside {…} splits a value into a key); valid keys here: "
                f"{', '.join(sorted(valid))}>"
            )
    return loc


_YAML_KEY = re.compile(r"\s*-?\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?\s*:")


class ConfigValueError(yaml.YAMLError):
    """A value PyYAML parsed but couldn't build: a tag that doesn't fit it, such as
    `!!int Hunter2x`. PyYAML raises ValueError or AttributeError there, with the value in
    the message (#386)."""


def parse_config_text(text: str) -> Any:
    """`yaml.safe_load`, with any error building a value raised as `ConfigValueError`.

    A tag that doesn't fit its value makes PyYAML raise ValueError (`!!int Hunter2x`),
    KeyError (`!!bool Hunter2x`), AttributeError (`!!timestamp Hunter2x`) and so on, each
    naming the value (#386). Every loader of a config file goes through here.
    """
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        raise
    except RecursionError:
        raise ConfigValueError("RecursionError") from None
    except Exception as e:  # noqa: BLE001 - any constructor error; its text names the value
        raise ConfigValueError(type(e).__name__) from None


def _config_keys() -> set[str]:
    """Every key the config's own sections define: the only ones a YAML error may name,
    so a password's text on its own line is never mistaken for a key (#386)."""
    models = (AppConfig, ConnectionConfig, AuthConfig, LoggingConfig, ModuleConfig)
    return {name for model in models for name in model.model_fields}


def _key_on_line(mark: Any) -> str | None:
    buffer = getattr(mark, "buffer", None)
    if not isinstance(buffer, str):
        return None
    lines = buffer.splitlines()
    if mark.line >= len(lines):
        return None
    # A BOM (Windows Notepad's default) starts the first line.
    key = _YAML_KEY.match(lines[mark.line].lstrip("\ufeff"))
    return key.group(1) if key and key.group(1) in _config_keys() else None


def format_yaml_error(exc: yaml.YAMLError) -> str:
    """Say where a config file isn't valid YAML, without quoting it (#386).

    PyYAML's own message quotes the offending line, and its problem text can quote the
    value (`found undefined alias 'Hunter2x'` for `password: *Hunter2x`), so a password
    with a YAML-special character would be printed. This names the line and column, where
    the broken value starts when PyYAML only notices at the end of the file (an
    unterminated quote or bracket), and the config key on that line, never the text.
    """
    hint = (
        "The text there isn't shown, since it may be a password: check its quoting (a "
        "value starting with a YAML special character, such as * ! @ % & ` | > [ { or a "
        "quote, or containing ': ' or ' #', needs quotes) and indentation."
    )
    if isinstance(exc, ConfigValueError) and str(exc) == "RecursionError":
        return (
            "The config file is nested too deeply for YAML to read. Check its brackets and "
            "indentation."
        )
    if isinstance(exc, ConfigValueError):
        return (
            "The config file has a value YAML can't build, such as one tagged !!int or "
            f"!!timestamp that doesn't fit the tag. {hint}"
        )
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        position = getattr(exc, "position", None)  # a ReaderError: a character YAML refuses
        if isinstance(exc, yaml.reader.ReaderError) and isinstance(position, int):
            return (
                f"The config file has a character YAML doesn't accept (character "
                f"{position + 1} of the file). {hint}"
            )
        return f"The config file isn't valid YAML. {hint}"
    where = f"line {mark.line + 1}, column {mark.column + 1}"
    start = getattr(exc, "context_mark", None)
    context = str(getattr(exc, "context", "") or "")
    # PyYAML notices an unclosed quote or bracket only at the end of the file; its context
    # then marks where that value starts. A block context marks the whole document.
    if start is not None and start.line != mark.line and ("quoted" in context or "flow" in context):
        where += f" (in the value that starts at line {start.line + 1})"
        mark = start
    if key := _key_on_line(mark):
        where += f", on the `{key}` line"
    return f"The config file isn't valid YAML at {where}. {hint}"


def format_validation_error(exc: Any) -> str:
    """Render a pydantic.ValidationError as a multi-line user-friendly block.

    Replaces pydantic's default multi-line traceback-style block with a
    list of `<dotted.location>: <message>` lines under a one-line header.
    Caller still wraps in a `click.echo(... fg='red')` + `sys.exit(1)` to
    match the project's standard CLI error envelope.
    """
    errors = exc.errors()
    if not errors:
        return f"Configuration validation failed: {exc}"
    lines = [f"Configuration validation failed ({len(errors)} error(s)):"]
    for err in errors:
        loc_parts = [str(p) for p in err.get("loc", ())]
        loc = _shown_location(err, loc_parts)
        msg = err.get("msg", "validation error")
        input_value = err.get("input")
        suffix = ""
        if _may_show(err, loc_parts):
            suffix = f" (got {input_value!r})" if input_value not in (None, "") else ""
        lines.append(f"  - {loc}: {msg}{suffix}")
    return "\n".join(lines)


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
    raw: dict[str, Any] = parse_config_text(raw_text) or {}

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
