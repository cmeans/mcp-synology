"""YAML config loading + Pydantic validation.

Four-step loading:
1. Discover config file path
2. Parse YAML
3. Merge environment variable overrides
4. Apply defaults and validate with Pydantic
"""

from __future__ import annotations

import difflib
import ipaddress
import logging
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, get_args, get_origin

import httpx
import yaml
from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

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


# Characters a host name or IP address never has, beyond the ones `host_problems`
# explains on their own (`:`, `/`, `@`, brackets, whitespace).
_HOST_OTHER_BAD = frozenset("\\?#%\"'<>^`{|},;=&+$!*()~")


_BRACKETED = re.compile(r"\[([^\]]*)\](?::(.*))?")
_IPV6_ZONE = re.compile(r"[A-Za-z0-9._~-]+")
_IDNA_DOTS = str.maketrans({"。": ".", "．": ".", "｡": "."})
# Quoted in the config file: unquoted, `host: [fd00::1]` is a YAML list (pre-QA r2 on
# #511 PR 2). An env var or setup's prompt takes it as typed.
_IPV6_FILE_FORM = '"[fd00::1]", in quotes'
_INVALID_HOST = (
    "isn't a valid host name or IP address (an IPv4 address has four numbers from 0 to "
    "255 with no leading zeros; a name's parts are at most 63 characters, of letters, "
    "digits and hyphens)"
)
# A last part that's a number (or 0x hex) makes the whole host an IPv4 address to URL
# parsers and the resolver, which expand a short one: `192.168.1` is 192.168.0.1, the
# router, not the NAS (WHATWG URL's "ends in a number" rule; pre-QA r3 on #511 PR 2).
_NUMERIC_LABEL = re.compile(r"(?:\d+|0[xX][0-9a-fA-F]*)")
_SHORT_IPV4 = (
    "ends in a number, so it's read as an IPv4 address, but it isn't a whole one: write all "
    "four numbers (192.168.1.100), or a name whose last part isn't a number"
)


def host_problems(
    host: str,
    port_setting: str = "set connection.port",
    https_setting: str = "set https: true",
    ipv6_form: str = _IPV6_FILE_FORM,
    credentials_setting: str = "go under auth or in the keyring (mcp-synology setup)",
) -> list[str]:
    """What keeps ``host`` from being a bare host name or IP address, as advice (#511 PR 2).

    The URL is built as ``http(s)://{host}:{port}``, so a scheme, user name, port or path
    written into the host made a broken URL (a traceback, #504) or put a password into
    the server instructions, tool errors and logs (#476, #471). An IPv6 address goes in
    square brackets (`[fd00::1]`), as in a URL. Never quotes the host: it can hold a
    password. ``port_setting``, ``https_setting``, ``ipv6_form`` and
    ``credentials_setting`` say how to set the port, HTTPS, an IPv6 address and the
    credentials where the host came from (the file, an env var, setup's questions).
    """
    bracket_advice = (
        "has square brackets: square brackets hold only an IPv6 address, around all of "
        f"it, {ipv6_form}"
    )
    if not host.strip():
        return ["is empty"]
    problems = []
    rest = host
    if "://" in rest:
        problems.append(f"has a scheme: drop the http:// or https:// (for HTTPS, {https_setting})")
        rest = rest.split("://", 1)[1]
    if "@" in rest:
        problems.append(
            f"has a user name: user names and passwords {credentials_setting}, never in the host"
        )
        rest = rest.rsplit("@", 1)[1]
    if "/" in rest:
        problems.append("has a path: the host takes no path")
        rest = rest.split("/", 1)[0]
    port = f"has a port: {port_setting} instead"
    if bracketed := _BRACKETED.fullmatch(rest):
        # `[fd00::1]` is the right form, also inside a URL (`https://[fd00::1]:5001`):
        # only what surrounds it is wrong.
        try:
            ipaddress.IPv6Address(bracketed[1])
        except ValueError:
            problems.append(bracket_advice)
        else:
            # ipaddress takes any zone text (`fe80::1%a\b`); a URL's zone is RFC 6874's
            # unreserved characters (QA round 1 on #532).
            zone = bracketed[1].partition("%")[2]
            if "%" in bracketed[1] and not _IPV6_ZONE.fullmatch(zone):
                problems.append(
                    "has an IPv6 zone (after %) with characters other than letters, digits, "
                    "'.', '_', '~' and '-'"
                )
        if bracketed[2] is not None:
            problems.append(port)
    else:
        try:
            ipaddress.IPv6Address(rest)
        except ValueError:
            pass
        else:
            return [*problems, f"is an IPv6 address: write it in square brackets, {ipv6_form}"]
        if "[" in rest or "]" in rest:
            # A half-bracketed IPv6 address has colons too: they aren't a port then.
            problems.append(bracket_advice)
        elif ":" in rest:
            problems.append(port)
    if any(c.isspace() or not c.isprintable() for c in host):
        problems.append("has a space or an invisible character")
    if not bracketed and any(c in _HOST_OTHER_BAD for c in rest):
        problems.append("has a character a host name can't have")
    if not problems and (rest.startswith(".") or ".." in rest):
        problems.append("has an empty part (a leading dot, or two dots in a row)")
    if problems:
        return problems
    # What's left is a name or address that looks bare; the URL library has the final
    # say (`999.1.1.1`, `01.2.3.4`, a name IDNA can't encode), or the URL built from it
    # fails with a traceback (pre-QA r2 on #511 PR 2).
    normalized = _url_host(host)
    if normalized is None:
        return [_INVALID_HOST]
    if bracketed:
        return []
    # As _INVALID_HOST says, which httpx doesn't check (QA round 1 on #532).
    if any(len(label) > 63 for label in normalized.split(".")):
        return [_INVALID_HOST]
    # The numeric rule reads the host as httpx will use it: IDNA maps `。`, `．` and `｡`
    # to `.`, so `192。168。1` is `192.168.1` (pre-QA r4), as WHATWG checks "ends in a
    # number" after domain-to-ASCII.
    bare = normalized.removesuffix(".")
    if _NUMERIC_LABEL.fullmatch(bare.rsplit(".", 1)[-1]):
        try:
            ipaddress.IPv4Address(bare)
        except ValueError:
            return [_SHORT_IPV4]
        if bare != normalized:
            # `192.168.1.100.` resolves nowhere, and gave the instance ID `192`, shared by
            # every such host (pre-QA r4). A name's trailing dot (`nas.local.`) is fine.
            return ["has a dot after the IPv4 address: drop it"]
    return []


def _url_host(host: str) -> str | None:
    """``host`` as httpx will use it (IDNA-mapped, lower-case), or None if it refuses it.

    Both steps can fail: building the URL (`httpx.InvalidURL`), and reading `.host`,
    which decodes an `xn--` first label and raises idna's own error (a UnicodeError)
    for a malformed one, its text decoded from the host (QA round 1 on #532).
    """
    try:
        return httpx.URL(f"http://{host}/").host
    except (httpx.InvalidURL, UnicodeError):
        return None


def host_message(problems: list[str]) -> str:
    """The advice for a host `host_problems` refused, without the host."""
    if problems == ["is empty"]:
        return "must be a bare host name or IP address, and it is empty."
    return (
        "must be a bare host name or IP address, and it "
        + "; it ".join(problems)
        + ". It isn't shown, since it may hold a password."
    )


_INSTANCE_ID_RULE = "lowercase letters, digits and hyphens; capitals are lowered"


def instance_id_problem(value: str) -> str | None:
    """Why ``value`` can't be an `instance_id`, or None. An empty one put every such
    instance's state file in one shared folder (#485)."""
    if not value.strip():
        return (
            "is empty: leave instance_id out to derive it from the host, or set it to "
            f"{_INSTANCE_ID_RULE}"
        )
    if not re.fullmatch(r"[a-z0-9-]+", value.lower()):
        return f"may hold only {_INSTANCE_ID_RULE}"
    return None


def _not_a_bool(value: object) -> object:
    """Refuse true/false for a number: pydantic reads it as 1 or 0, so `port: yes` loaded
    as port 1 and `timeout: on` as one second (pre-QA r3 on #511 PR 2)."""
    if isinstance(value, bool):
        msg = "must be a whole number, not true or false (YAML reads yes/no/on/off as those)."
        raise ValueError(msg)
    return value


class ConnectionConfig(BaseModel, extra="forbid"):
    """NAS connection settings.

    extra="forbid", as for every config section but module settings (#386): a typo
    here is an error, not a dropped key. `htps: true` used to load with `https` false,
    so the server logged in, password included, over plain HTTP.
    """

    host: str
    port: int | None = Field(default=None, ge=1, le=65535)
    https: bool = False
    verify_ssl: bool = True
    timeout: int = Field(default=30, ge=1, le=600)

    @field_validator("port", "timeout", mode="before")
    @classmethod
    def _no_bool_number(cls, value: object) -> object:
        return _not_a_bool(value)

    @field_validator("host", mode="before")
    @classmethod
    def _bracketed_but_unquoted(cls, value: object) -> object:
        # `host: [fd00::1]` unquoted is a YAML list holding the address.
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
            msg = (
                "is a YAML list, not text: an IPv6 address in square brackets needs quotes, "
                '"[fd00::1]".'
            )
            raise ValueError(msg)
        return value

    @field_validator("host")
    @classmethod
    def _bare_host(cls, value: str) -> str:
        if problems := host_problems(value):
            raise ValueError(host_message(problems))
        return value


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

    @model_validator(mode="before")
    @classmethod
    def _empty_is_defaults(cls, value: object) -> object:
        # `filestation:` with nothing under it is `filestation: {}` (#492).
        return {} if value is None else value

    @field_validator("settings", mode="before")
    @classmethod
    def _empty_settings(cls, value: object) -> object:
        return {} if value is None else value


def _no_empty_path(name: str, value: str | None) -> str | None:
    """An empty or blank file path names no file: an error, not "none" (#511; the same
    class as `--config ''`), so a log file or instructions file the user meant to set
    isn't dropped without a word."""
    if value is not None and not value.strip():
        msg = f"is empty: give a file's path, or leave {name} out."
        raise ValueError(msg)
    return value


class LoggingConfig(BaseModel, extra="forbid"):
    """Logging configuration. extra="forbid" (#386): `levle: debug` is an error."""

    level: Literal["debug", "info", "warning", "error"] = "info"
    file: str | None = None

    @field_validator("file")
    @classmethod
    def _file_not_empty(cls, value: str | None) -> str | None:
        return _no_empty_path("logging.file", value)


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

    @field_validator("schema_version", mode="before")
    @classmethod
    def _no_bool_schema_version(cls, value: object) -> object:
        return _not_a_bool(value)

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

    @field_validator("auth", "logging", "modules", mode="before")
    @classmethod
    def _empty_section(cls, value: object) -> object:
        # An empty section (`auth:`, which YAML reads as null) is the same as one left
        # out, whatever the environment sets (#492, #475). An empty `modules:` then gets
        # the "at least one module" error. `connection:` stays None: its own error says
        # the host is needed.
        return {} if value is None else value

    @field_validator("instructions_file")
    @classmethod
    def _instructions_file_not_empty(cls, value: str | None) -> str | None:
        return _no_empty_path("instructions_file", value)

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

        # Normalize and validate instance_id format. `is not None`: an empty one is
        # refused too (#485).
        if self.instance_id is not None:
            if derived and instance_id_problem(self.instance_id):
                # The host is already a bare name or address; what's left is an IPv6
                # address, or a name with characters outside a-z, 0-9 and hyphens.
                msg = (
                    "connection.host gives no usable instance_id: its first part has "
                    "characters other than letters a-z, digits and hyphens (an IPv6 address, "
                    "an underscore, or a letter outside a-z). Set instance_id, or the "
                    f"SYNOLOGY_INSTANCE_ID environment variable ({_INSTANCE_ID_RULE})."
                )
                raise ValueError(msg)
            if problem := instance_id_problem(self.instance_id):
                raise ValueError(f"instance_id {problem}.")
            self.instance_id = self.instance_id.lower()

        return self


def _derive_instance_id(host: str) -> str:
    """Derive an instance ID from the host.

    IP addresses: dots -> hyphens (192.168.1.100 -> 192-168-1-100)
    Hostnames: first component (nas.local -> nas)
    """
    # IDNA reads `。`, `．` and `｡` as dots, and host_problems accepts them, so they split
    # here too: `nas．local` is `nas` (QA round 1 on #532).
    host = host.translate(_IDNA_DOTS)
    # IP address pattern
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        return host.replace(".", "-")
    # Hostname: take first component
    return host.split(".")[0].lower()


def _shown_path(path: Path) -> str:
    """The path made absolute for messages, or as given if it can't be.

    `absolute()` raises when the working folder has been deleted; the message must
    still name the path (pre-QA on #529).
    """
    try:
        return str(path.absolute())
    except OSError:
        return str(path)


def discover_config_path(explicit_path: str | None = None) -> Path:
    """Find the config file using the discovery hierarchy.

    1. Explicit --config flag
    2. MCP_SYNOLOGY_CONFIG env var
    3. ~/.config/mcp-synology/config.yaml
    4. ./mcp-synology.yaml
    """
    # An empty path names no file, so it's an error, the same as a missing one (#499;
    # maintainer, 2026-11-02): `--config ''` used to fall through to the default
    # locations, and with SYNOLOGY_HOST set to env-only mode.
    if explicit_path is not None:
        if not explicit_path.strip():
            # No "leave it out" advice: without a config file, a set SYNOLOGY_HOST means
            # env-only mode, read-only over plain HTTP, the outcome this refuses (QA
            # round 2 on #529).
            msg = "--config is empty: give a config file's path."
            raise FileNotFoundError(msg)
        path = Path(explicit_path).expanduser()
        # Absolute in messages: an MCP client starts the server in a working folder the
        # user rarely knows (QA round 1 on #529).
        if not path.exists():
            # Quoted: a stray \r or space at the end would otherwise be invisible.
            msg = f"Config file not found: {_shown_path(path)!r}"
            raise FileNotFoundError(msg)
        # Quoted, as in the errors: a newline in a folder's name would forge a log line
        # (QA round 2 on #529).
        logger.info("Config: %r (from --config)", _shown_path(path))
        return path

    # Set but blank is an error, unlike the SYNOLOGY_* variables (maintainer,
    # 2026-11-02): an unfilled "" in an MCP client's env block would otherwise drop
    # the config it was meant to name without a word.
    env_path = os.environ.get("MCP_SYNOLOGY_CONFIG")
    if env_path is not None:
        if not env_path.strip():
            msg = "MCP_SYNOLOGY_CONFIG is set but empty: set it to a config file's path."
            raise FileNotFoundError(msg)
        path = Path(env_path).expanduser()
        if not path.exists():
            # An error, never env-only mode: that would run read-only over plain HTTP
            # instead of the config the variable names (#499).
            msg = (
                f"Config file not found (from MCP_SYNOLOGY_CONFIG): {_shown_path(path)!r}. "
                "Fix the path."
            )
            raise FileNotFoundError(msg)
        logger.info("Config: %r (from MCP_SYNOLOGY_CONFIG)", _shown_path(path))
        return path

    default_paths = [
        Path.home() / ".config" / "mcp-synology" / "config.yaml",
        Path.cwd() / "mcp-synology.yaml",
    ]
    for path in default_paths:
        # Quoted: the working folder's name could forge a log line (QA round 3 on #529).
        logger.debug("Checking default config path: %r", str(path))
        if path.exists():
            logger.info("Config: %r", str(path.absolute()))
            return path

    msg = (
        "No config file found. Create one at ~/.config/mcp-synology/config.yaml "
        "or specify with --config. See examples/ for sample configs."
    )
    raise FileNotFoundError(msg)


class EnvVarError(ValueError):
    """An environment variable's value isn't one its config field takes (#501).

    The message names the variable and what it takes, never the value: it may be a
    password (a value pasted into the wrong variable).
    """


def _env_field_bounds(dotted_path: str) -> tuple[Any, ...]:
    """The config field's range constraints (`port`'s 1 to 65535), as pydantic metadata."""
    section, _, name = dotted_path.rpartition(".")
    model: Any = _SECTION_MODELS[section] if section else AppConfig
    return tuple(
        m
        for m in model.model_fields[name].metadata
        if getattr(m, "ge", None) is not None or getattr(m, "le", None) is not None
    )


def _env_field_annotation(dotted_path: str) -> Any:
    """The type of the config field at ``dotted_path`` (``connection.https``)."""
    model: Any = AppConfig
    annotation: Any = None
    for part in dotted_path.split("."):
        annotation = model.model_fields[part].annotation
        # A section (`connection: ConnectionConfig | None`): step into its model.
        model = next(
            (
                a
                for a in (annotation, *get_args(annotation))
                if isinstance(a, type) and issubclass(a, BaseModel)
            ),
            None,
        )
    return annotation


def _env_expected(annotation: Any, bounds: tuple[Any, ...] = ()) -> str:
    """What a field of ``annotation`` takes, for an env var's error."""
    if get_origin(annotation) is Literal:
        kind = annotation
    else:
        kind = next((a for a in get_args(annotation) if a is not type(None)), annotation)
    if kind is bool:
        return "true or false (also 1/0, yes/no, on/off)"
    if kind is int:
        low = next((m.ge for m in bounds if getattr(m, "ge", None) is not None), None)
        high = next((m.le for m in bounds if getattr(m, "le", None) is not None), None)
        bounded = low is not None and high is not None
        return "a whole number" + (f" from {low} to {high}" if bounded else "")
    if get_origin(kind) is Literal:
        return "one of " + ", ".join(str(v) for v in get_args(kind))
    # A text field takes any value, so it never fails to parse and never gets here.
    return "a valid value"  # pragma: no cover


# The boolean words an env var takes, exactly the documented ones.
_ENV_BOOLS = {
    "true": True,
    "1": True,
    "yes": True,
    "on": True,
    "false": False,
    "0": False,
    "no": False,
    "off": False,
}


def parse_env_value(env_var: str, dotted_path: str, value: str) -> Any:
    """``value``, read from ``env_var``, as its config field's type (#501).

    Booleans take only the documented words, `true`/`false`, `1`/`0`, `yes`/`no`,
    `on`/`off`, in any case (not pydantic's one-letter `t`/`f`/`y`/`n`: `SYNOLOGY_HTTPS=n`
    would turn HTTPS off; QA round 1 on #529). Whole numbers and choices are parsed by
    pydantic with the field's own type, choices in any case (`SYNOLOGY_LOG_LEVEL=DEBUG`;
    the config file's are lower-case), with `update_notices` also taking the words the
    file's YAML reads as booleans (`no` is off). Typed values are stripped; text values
    (host, credentials, `instance_id`) are taken as given, as in the file. A value of the
    wrong type, or one that breaks its field's own rule (the port's range, a bare host, an
    `instance_id`'s characters; #511 PR 2), is an error naming the variable, never quoting
    the value.

    `SYNOLOGY_HTTPS` used to be true for `true`, `1` or `yes` and false for anything
    else, so `on` (or a typo) turned HTTPS off even over `https: true` in the config, and
    the password went in cleartext.
    """
    annotation = _env_field_annotation(dotted_path)
    kinds = [a for a in get_args(annotation) if a is not type(None)] or [annotation]
    if kinds == [str]:
        # Text: taken as given, then held to the field's own rule.
        if dotted_path == "connection.host" and (
            problems := host_problems(
                value,
                port_setting="set SYNOLOGY_PORT",
                https_setting="set SYNOLOGY_HTTPS=true",
                ipv6_form="[fd00::1]",
                credentials_setting=(
                    "go in SYNOLOGY_USERNAME and SYNOLOGY_PASSWORD or in the keyring "
                    "(mcp-synology setup)"
                ),
            )
        ):
            raise EnvVarError(f"{env_var} (it sets {dotted_path}) {host_message(problems)}")
        if dotted_path == "instance_id" and (problem := instance_id_problem(value)):
            raise EnvVarError(f"{env_var} {problem} (it sets {dotted_path}).")
        return value
    text = value.strip()
    if kinds == [bool]:
        word = _ENV_BOOLS.get(text.lower())
        if word is None:
            raise EnvVarError(
                f"{env_var} must be {_env_expected(annotation)} (it sets {dotted_path})."
            )
        return word
    if dotted_path == "update_notices":
        # As in the file, where YAML reads `no`/`false`/`off` as false and `yes`/`true`/
        # `on` as true, and `_yaml_bool_notices` maps them to off and all. A word list,
        # not a YAML parse: any other text must reach the choice check below, so it's
        # an EnvVarError naming the variable (pre-QA on #511).
        text = {"yes": "all", "true": "all", "on": "all", "no": "off", "false": "off"}.get(
            text.lower(), text
        )
    if get_origin(annotation) is Literal or any(
        get_origin(a) is Literal for a in get_args(annotation)
    ):
        text = text.lower()
    bounds = _env_field_bounds(dotted_path)
    field_type = Annotated[annotation, *bounds] if bounds else annotation
    try:
        return TypeAdapter(field_type).validate_python(text)
    except ValidationError:
        raise EnvVarError(
            f"{env_var} must be {_env_expected(annotation, bounds)} (it sets {dotted_path})."
        ) from None


def _merge_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge environment variable overrides into the raw config dict.

    An empty (or blank) variable counts as unset, as the credentials already did
    (`auth.resolve_credentials`): `SYNOLOGY_HTTPS=` leaves the config's `https` alone
    instead of turning it off.
    """
    for env_var, dotted_path in ENV_VAR_MAP.items():
        value = os.environ.get(env_var)
        if value is None or not value.strip():
            continue

        logger.debug("Env override: %s -> %s", env_var, dotted_path)
        parsed = parse_env_value(env_var, dotted_path, value)
        parts = dotted_path.split(".")
        target = raw
        for part in parts[:-1]:
            if target.get(part) is None:
                target[part] = {}
            if not isinstance(target[part], dict):
                raise ValueError(f"Config section '{part}' must be a YAML mapping of settings")
            target = target[part]
        target[parts[-1]] = parsed

    return raw


def _emit_warnings(config: AppConfig) -> None:
    """Emit warnings for insecure or unusual config."""
    # A username alone isn't a secret (keyring password + config username is the
    # recommended setup), so only a password triggers the warning (#167).
    if config.auth.password:
        # Blank counts as unset (_merge_env_overrides): the password is then the file's.
        if (os.environ.get("SYNOLOGY_PASSWORD") or "").strip():
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
    # Blank counts as unset, as in _merge_env_overrides.
    host = (os.environ.get("SYNOLOGY_HOST") or "").strip()
    if not host:
        return None

    logger.info("Config: environment variables only (no config file; SYNOLOGY_HOST is set)")
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
    # The host can hold a password (`admin:…@nas`, #476); its own error says what's wrong.
    if loc_parts[:2] == ["connection", "host"]:
        return False
    return not (loc_parts and loc_parts[0] == "auth")


# The sections whose keys are checked: both the near-miss rule and the YAML-error key names
# read them from here.
_SECTION_MODELS: dict[str, type[BaseModel]] = {
    "connection": ConnectionConfig,
    "auth": AuthConfig,
    "logging": LoggingConfig,
}

# Conditional: a comma split can leave a key with or without a value (`a,B: x` gives `B: x`),
# so the hint says when it applies (QA rounds 4 and 5 on #469). One wording for every message.
# Which comma: every flow mapping has commas between its entries (QA round 6 on #469).
_SPLIT_HINT = (
    "if a value inside {…} has a comma of its own (not the one between entries), quote that value"
)
_NOT_TEXT_KEY = f"<a key that isn't text, not shown: it may be part of a password; {_SPLIT_HINT}>"


def _section_keys(loc_parts: list[str]) -> list[str]:
    """The keys the config section holding this location defines."""
    if len(loc_parts) == 1:
        model: type[BaseModel] = AppConfig
    elif loc_parts[0] == "modules" and len(loc_parts) == 3:
        model = ModuleConfig
    else:
        model = _SECTION_MODELS.get(loc_parts[0], AppConfig)
    return list(model.model_fields)


def _non_text_keys(errors: list[Any]) -> set[tuple[Any, ...]]:
    """The mappings' keys pydantic reported as not text (`modules.7319.[key]`), as the
    location of the key itself (`("modules", 7319)`)."""
    return {
        tuple(raw[: raw.index("[key]")])
        for raw in (tuple(err.get("loc", ())) for err in errors)
        if "[key]" in raw
    }


def _shown_location(
    err: Any, loc_parts: list[str], non_text_keys: set[tuple[Any, ...]] | None = None
) -> str:
    """The error's location, except an unknown key that may be part of a password.

    YAML splits a value with a comma inside a flow mapping (`{password: a,Hunter2x}`): the
    second half arrives as a key with no value, in any section (#386). So an unknown key
    isn't named when it has no value, or when it's under `auth`; a near-miss of a key its
    section defines names that key instead (`pasword` reads `<an unknown key close to
    "password", …>`), since `Spring,Password1` splits off `Password1`. An unknown key with a
    value elsewhere (`totally_unknown: x`) is named, as before. A near-miss is matched in
    any case (`USERNAME`). A key YAML read as something other than text (`{password:
    Summer,2024}` splits off a key 2024), anywhere, module settings included, is never
    named: only the mapping it's in.
    """
    loc = ".".join(loc_parts) if loc_parts else "<root>"
    raw_loc = err.get("loc", ())
    if "[key]" in raw_loc:
        # A key of a free-form mapping (module settings) that isn't text: `{x: Hunter,2024}`
        # fails as `<settings>.2024.[key]`. Name the mapping, not the key.
        cut = raw_loc.index("[key]") - 1
        where = ".".join(str(p) for p in raw_loc[:cut])
        return (f"{where}." if where else "") + _NOT_TEXT_KEY
    for hidden in non_text_keys or ():
        if tuple(raw_loc[: len(hidden)]) == hidden:
            # Another error about that same key (`modules: {…, 7319}` also fails as
            # `modules.7319`, "should be a dictionary"): it isn't named there either (#469
            # QA round 4). The path below it still says which field failed.
            where = ".".join(str(p) for p in hidden[:-1])
            below = "".join(f".{p}" for p in raw_loc[len(hidden) :])
            return (f"{where}." if where else "") + _NOT_TEXT_KEY + below
    if err.get("type") == "invalid_key" and loc_parts:
        # A key YAML read as a number or a date (`{password: Summer,2024}` splits into a
        # key 2024): never a real config key, and possibly part of a password (#469 QA
        # round 1).
        where = ".".join(loc_parts[:-1])
        return (f"{where}." if where else "") + _NOT_TEXT_KEY
    if err.get("type") == "extra_forbidden" and loc_parts:
        key = loc_parts[-1]
        suspect = err.get("input") is None or loc_parts[0] == "auth"
        valid = _section_keys(loc_parts)
        if suspect:
            # A comma split can leave a fragment with a value too (`{password: a,B: x}` gives
            # `B: x`), so the hint is conditional, never left out (QA rounds 4 and 5 on #469).
            split = _SPLIT_HINT
            where = ".".join(loc_parts[:-1])
            by_lower = {k.lower(): k for k in valid}
            close = difflib.get_close_matches(key.lower(), list(by_lower), n=1, cutoff=0.75)
            if close:
                # A typo's help without its text: `Spring,Password1` splits off a key
                # `Password1`, close to `password` (QA round 2 on #469).
                return (f"{where}." if where else "") + (
                    f'<an unknown key close to "{by_lower[close[0]]}", not shown since it may '
                    f"be, or hold, part of a password: fix its spelling, or, {split}>"
                )
            return (
                (f"{where}." if where else "")
                + "<an unknown key, not shown since it may be, or hold, part of a password; "
                + f"{split}; valid keys here: {', '.join(sorted(valid))}>"
            )
    return loc


_YAML_KEY = re.compile(r"\s*-?\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?\s*:")


class ConfigValueError(yaml.YAMLError):
    """A value PyYAML parsed but couldn't build: a tag that doesn't fit it, such as
    `!!int Hunter2x`. PyYAML raises ValueError or AttributeError there, with the value in
    the message (#386). ``kind`` names the error PyYAML raised (``RecursionError`` for a
    file nested too deeply)."""

    kind: str = ""


class ConfigKeyError(yaml.YAMLError):
    """A mapping in the config file repeats a key, or has a key with no value inside
    `{…}` (#464). ``message`` is the whole user-facing text, with line numbers and only
    the config's own key names, never a value."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


_MERGE_TAG = "tag:yaml.org,2002:merge"


class _ConfigLoader(yaml.SafeLoader):
    """SafeLoader that refuses a repeated key and a valueless key in a flow mapping (#464).

    PyYAML keeps the last of two equal keys without a word, so `https: true` then
    `https: false`, two `password:` lines, or a second, empty `connection:` silently
    replaced what the user wrote. And YAML splits `{password: Spring,device_id}` at the
    comma: the fragment `device_id` became a key with no value, and the password was cut
    short with no error. A key with a colon and nothing after it (`{device_id: }`) is
    still a key set to null; only the colon-less form is refused.
    """

    def construct_document(self, node: yaml.Node) -> Any:
        # Every mapping is checked as written, before anything is built: SafeLoader
        # flattens `<<: *base` merges in place (merged keys put in front of the node's
        # own), so a check while building would see an override as a repeat, both in the
        # merging mapping and in an anchor reused by alias after it was flattened.
        self._check_written_keys(node)
        return super().construct_document(node)  # type: ignore[no-untyped-call]

    def _check_written_keys(self, root: yaml.Node) -> None:
        stack, visited = [root], set()
        while stack:  # a loop, not recursion: an alias can make the graph share nodes
            node = stack.pop()
            if id(node) in visited:
                continue
            visited.add(id(node))
            if isinstance(node, yaml.MappingNode):
                self._check_mapping(node)
                stack.extend(n for pair in node.value for n in pair)
            elif isinstance(node, yaml.SequenceNode):
                stack.extend(node.value)

    def _check_mapping(self, node: yaml.MappingNode) -> None:
        seen: dict[Any, tuple[int, int, bool]] = {}
        merge_line: int | None = None
        for key_node, value_node in node.value:
            # A key given by alias (`{*k: …}`) has its anchor's marks, from before this
            # mapping: report the mapping's own place then (pre-QA r4).
            mark = key_node.start_mark
            clamped = mark.index < node.start_mark.index
            if clamped:
                mark = node.start_mark
            line, column = mark.line + 1, mark.column + 1
            if key_node.tag == _MERGE_TAG:
                # Its keys may be overridden: YAML's own rule, not a repeat. A second `<<`
                # is a repeated key like any other (QA round 1 on #532).
                if merge_line is not None:
                    raise ConfigKeyError(
                        f"The config file has `<<` twice in one section (lines {merge_line} "
                        f"and {line}), and YAML would silently let the later one win. Merge "
                        "both in one: `<<: [*a, *b]`."
                    )
                merge_line = line
                continue
            if node.flow_style and _has_no_colon(node, key_node, value_node):
                raise ConfigKeyError(
                    f"The config file has a key with no value inside {{…}} (line {line}). "
                    "It isn't shown, since it may be part of a password. A colon needs a "
                    f"space after it (`host: nas`, not `host:nas`); {_SPLIT_HINT}; to leave "
                    "a setting empty, leave it out."
                )
            if not isinstance(key_node, yaml.ScalarNode):
                continue  # a list or mapping as a key: SafeLoader's own error follows
            key = self.construct_object(key_node)  # type: ignore[no-untyped-call]
            first = seen.get(key)
            if first is not None:
                known = isinstance(key, str) and key in _config_keys()
                # On one line (`{k: 1, k: 2}`), the columns say where (pre-QA r3), unless
                # a key came by alias: its column is the mapping's, so "columns 4 and 4"
                # (QA round 1 on #532).
                if first[2] or clamped:
                    where = f"in the {{…}} at line {line}"
                elif first[0] == line:
                    where = f"line {line}, columns {first[1]} and {column}"
                else:
                    where = f"lines {first[0]} and {line}"
                raise ConfigKeyError(
                    f"The config file has {f'`{key}`' if known else 'a key'} twice in one "
                    f"section ({where}), and YAML would silently keep only the last. "
                    "Remove one."
                    + ("" if known else " The key isn't shown: it may be part of a password.")
                )
            seen[key] = (line, column, clamped)


def _has_no_colon(mapping: yaml.MappingNode, key_node: yaml.Node, value_node: yaml.Node) -> bool:
    """Whether a flow mapping's entry was written without a colon (`{a: 1, b}`)."""
    if not (isinstance(value_node, yaml.ScalarNode) and value_node.value == ""):
        return False
    if value_node.tag != "tag:yaml.org,2002:null":
        return False
    # An alias (`{a: *empty}`, `{*k: }`) reuses its anchor's node, written before this
    # mapping, so the marks don't describe this entry (pre-QA r3 on #511 PR 2).
    start = mapping.start_mark.index
    if key_node.start_mark.index < start or value_node.start_mark.index < start:
        return False
    if value_node.start_mark.index < key_node.end_mark.index:
        return False  # `{a: &z , b: *z}`: an anchor earlier in this same mapping
    buffer = getattr(key_node.end_mark, "buffer", None)
    if not isinstance(buffer, str):
        return False  # pragma: no cover - text input always keeps its buffer
    between = buffer[key_node.end_mark.index : value_node.start_mark.index]
    # The colon must come right after the key (spaces aside): a comment holding one
    # (`{password: Spr,ing # x: y`) doesn't count (pre-QA r1 on #511 PR 2).
    return not between.lstrip().startswith(":")


def parse_config_text(text: str, *, check_keys: bool = True) -> Any:
    """Load a config file's YAML: `yaml.safe_load` refusing repeated keys and valueless
    flow keys (`ConfigKeyError`, #464), with any error building a value raised as
    `ConfigValueError`.

    A tag that doesn't fit its value makes PyYAML raise ValueError (`!!int Hunter2x`),
    KeyError (`!!bool Hunter2x`), AttributeError (`!!timestamp Hunter2x`) and so on, each
    naming the value (#386). Every loader of a config file goes through here.

    ``check_keys=False`` skips the key checks, for a file the program writes (the shared
    `global.yaml`): there a repeated key keeps the last, as before, rather than making
    the whole file unreadable (QA round 1 on #532).
    """
    loader = _ConfigLoader if check_keys else yaml.SafeLoader
    try:
        return yaml.load(text, Loader=loader)  # noqa: S506 - SafeLoader or its subclass
    except yaml.YAMLError:
        raise
    except Exception as e:  # noqa: BLE001 - any constructor error; its text names the value
        error = ConfigValueError(type(e).__name__)
        error.kind = type(e).__name__
        raise error from None


def _config_keys() -> set[str]:
    """Every key the config's own sections define: the only ones a YAML error may name,
    so a password's text on its own line is never mistaken for a key (#386)."""
    models: list[type[BaseModel]] = [AppConfig, ModuleConfig, *_SECTION_MODELS.values()]
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
        "quote, or containing ': ' or ' #', needs quotes; inside double quotes a backslash "
        "starts an escape, so write a Windows path in single quotes, 'C:\\Users\\me', or "
        "double each backslash) and indentation."
    )
    if isinstance(exc, ConfigKeyError):
        return exc.message
    if isinstance(exc, ConfigValueError) and exc.kind == "RecursionError":
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
    non_text_keys = _non_text_keys(errors)
    for err in errors:
        loc_parts = [str(p) for p in err.get("loc", ())]
        loc = _shown_location(err, loc_parts, non_text_keys)
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
    SYNOLOGY_HOST is set, unless --config or MCP_SYNOLOGY_CONFIG names a file:
    that file must exist (#499).
    """
    try:
        config_path = discover_config_path(None if path is None else str(path))
    except FileNotFoundError:
        # A file named by --config or MCP_SYNOLOGY_CONFIG must exist: never fall back
        # to env-only mode (#499).
        if path is not None or os.environ.get("MCP_SYNOLOGY_CONFIG") is not None:
            raise
        # Try env-var-only mode
        env_config = _synthesize_env_config()
        if env_config:
            return env_config
        raise

    logger.debug("Loading config from %r", str(config_path))  # quoted, as the INFO line
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        # Its message quotes the byte, which may be one of a password's (QA round 2 on #469).
        line = e.object[: e.start].count(b"\n") + 1
        raise ValueError(
            f"The config file isn't valid UTF-8 (line {line}). Save it as UTF-8 (in Notepad: "
            "File > Save as, Encoding: UTF-8)."
        ) from None
    raw = parse_config_text(raw_text)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            "The config file must be a YAML mapping of settings "
            "(schema_version: 1, connection: ...)"
        )

    raw = _merge_env_overrides(raw)

    config = AppConfig.model_validate(raw)

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
