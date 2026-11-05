"""YAML config loading + Pydantic validation.

Four-step loading:
1. Discover config file path
2. Parse YAML
3. Merge environment variable overrides
4. Apply defaults and validate with Pydantic
"""

from __future__ import annotations

import difflib
import errno
import ipaddress
import logging
import os
import re
import shlex
import stat
import unicodedata
import urllib.request
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal, get_args, get_origin

import httpx
import yaml
from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from mcp_synology.core.local_paths import (
    CONTROL_CHARACTER_ADVICE,
    check_local_path_entries,
    has_control_character,
)

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
    "255 with no leading zeros; a name's parts are at most 63 characters)"
)
# Its own advice: such a host meets every rule _INVALID_HOST states (pre-QA on #532).
_BAD_PUNYCODE = (
    "starts with a part beginning xn--, which isn't valid punycode: write the name as it's "
    "spelled, or copy its xn-- form exactly"
)
# A last part that's a number (or 0x hex) makes the whole host an IPv4 address to URL
# parsers and the resolver, which expand a short one: `192.168.1` is 192.168.0.1, the
# router, not the NAS (WHATWG URL's "ends in a number" rule; pre-QA r3 on #511 PR 2).
_NUMERIC_LABEL = re.compile(r"(?:\d+|0[xX][0-9a-fA-F]*)", re.ASCII)
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
        # Only if that part is what fails: the advice blamed a valid first part for a bad
        # later one (`xn--bcher-kva.my_nas`, #543).
        first = host.translate(_IDNA_DOTS).split(".", 1)[0]
        if first.lower().startswith("xn--") and _url_host(first) is None:
            return [_BAD_PUNYCODE]
        return [_INVALID_HOST]
    if bracketed:
        return []
    # As _INVALID_HOST says, which httpx doesn't check (QA round 1 on #532). On the host as
    # sent: `.host` decodes a first `xn--` part, so it would measure the short form (QA
    # round 2).
    sent = httpx.URL(f"http://{host}/").raw_host.decode("ascii")
    if any(len(label) > 63 for label in sent.split(".")):
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
    """``host`` as httpx reads it, or None if it refuses it: a name IDNA-mapped and lower-case,
    an IPv6 address as written. A name whose first part is `xn--` comes back decoded (`.host`
    does that); `raw_host` is what's sent.

    Both steps can fail: building the URL (`httpx.InvalidURL`), and reading `.host`,
    which decodes an `xn--` first label and raises idna's own error (a UnicodeError)
    for a malformed one, its text decoded from the host (QA round 1 on #532).
    """
    try:
        return httpx.URL(f"http://{host}/").host
    except (httpx.InvalidURL, UnicodeError):
        return None


def host_message(problems: list[str], *, hidden_note: bool = True) -> str:
    """The advice for a host `host_problems` refused, without the host.

    ``hidden_note=False`` leaves out why the host isn't shown, for setup's prompt, where
    it's right above (pre-QA on #532).
    """
    if problems == ["is empty"]:
        return "must be a bare host name or IP address, and it is empty."
    return (
        "must be a bare host name or IP address, and it "
        + "; it ".join(problems)
        + (". It isn't shown, since it may hold a password." if hidden_note else ".")
    )


_INSTANCE_ID_RULE = "lowercase letters, digits and hyphens; capitals are lowered"


def instance_id_problem(value: str) -> str | None:
    """Why ``value`` can't be an `instance_id`, or None. An empty one gave every such
    instance one keyring entry (`mcp-synology/default`) and session name prefix (#485).
    The limit is a host part's: a config file and a keyring entry are named after it, and
    a very long one crashed `setup` with the keyring already written (QA round 2 on #532).
    """
    if not value.strip():
        return (
            "is empty: leave instance_id out to derive it from the host, or set it to "
            f"{_INSTANCE_ID_RULE}"
        )
    if len(value) > 63:
        return "is longer than 63 characters"
    if not re.fullmatch(r"[a-z0-9-]+", value.lower()):
        return f"may hold only {_INSTANCE_ID_RULE}"
    return None


def _unquoted_text_problem(value: object) -> None:
    """Say why a text setting got a number, date or boolean, without showing it: the value
    is hidden there, and `123` looks like text in the file (QA round 2 on #532)."""
    if isinstance(value, bool | int | float | date):
        msg = "isn't text: YAML read it as a number, date or true/false, so put it in quotes."
        raise ValueError(msg)


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
    def _host_as_yaml_read_it(cls, value: object) -> object:
        # `host: [fd00::1]` unquoted is a YAML list holding the address.
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
            msg = (
                "is a YAML list, not text: an IPv6 address in square brackets needs quotes, "
                '"[fd00::1]".'
            )
            raise ValueError(msg)
        # Left blank, as `host: ""` says it (QA round 2 on #532).
        if value is None:
            raise ValueError(host_message(["is empty"]))
        _unquoted_text_problem(value)
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

    @field_validator("username", "password", "device_id", mode="before")
    @classmethod
    def _credential_as_yaml_read_it(cls, value: object) -> object:
        # `password: 123456` looks like text, and these values are never shown (pre-QA r6
        # on #532).
        _unquoted_text_problem(value)
        # A lone surrogate, also from a variable with bytes that aren't UTF-8, ended in a
        # traceback hashing the credentials (pre-QA r4 on #541).
        if isinstance(value, str) and any(unicodedata.category(c) == "Cs" for c in value):
            raise ValueError("has a lone surrogate or bytes that aren't UTF-8.")
        return value


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
    if value is not None and has_control_character(value):
        raise ValueError(CONTROL_CHARACTER_ADVICE)
    return value


def alias_problem(value: str) -> str | None:
    """Why ``value`` can't be an alias, or None. `setup` asks with the same rule, and
    `setup --list` shows only an alias that passes."""
    if not value.strip():
        return "is empty: give a name, or leave alias out."
    if has_control_character(value):
        return "has a control character, line break or lone surrogate: give a name on one line."
    return None


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
    # The file this came from, for errors found after loading; None in env-var-only mode.
    _source_path: Path | None = PrivateAttr(default=None)

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

    @field_validator("instance_id", mode="before")
    @classmethod
    def _instance_id_as_yaml_read_it(cls, value: object) -> object:
        _unquoted_text_problem(value)  # left blank (None) is still "not set"
        return value

    @field_validator("alias")
    @classmethod
    def _alias_has_no_control_character(cls, value: str | None) -> str | None:
        # It names the server and a line of `setup --list`: `x\n    Host: evil.example` forged
        # a Host line, and an escape sequence reached the terminal (QA round 3 on #532).
        if value is not None and (problem := alias_problem(value)):
            raise ValueError(problem)
        return value

    @field_validator("custom_instructions")
    @classmethod
    def _instructions_can_be_sent(cls, value: str | None) -> str | None:
        # A lone surrogate crashes the server's first reply, where `check` can't see it.
        if value is not None and any(unicodedata.category(c) == "Cs" for c in value):
            raise ValueError("has a lone surrogate, which can't be sent to a client.")
        return value

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
        # Every problem together (#511 PR 3). One line each: format_validation_error lists
        # them as separate errors.
        problems: list[str] = []
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            problems.append(
                f"Config schema_version is {self.schema_version}, "
                f"but this server expects {CURRENT_SCHEMA_VERSION}."
            )

        if self.connection is None:
            problems.append(
                "connection.host is required. "
                "Set it in the config file or via the SYNOLOGY_HOST environment variable."
            )

        if not self.modules:
            problems.append(
                "At least one module must be configured, "
                "e.g. modules: {filestation: {enabled: true}}."
            )
        elif not any(mod.enabled for mod in self.modules.values()):
            # A server with no enabled module has 0 tools: almost certainly a config mistake.
            listed = ", ".join(_printable(name) for name in sorted(self.modules))
            problems.append(
                f"No module is enabled (listed with enabled: false: {listed}). "
                "Enable at least one, e.g. filestation: {enabled: true}."
            )

        derived = self.instance_id is None
        if self.connection is not None:
            # Apply default port from https setting
            if self.connection.port is None:
                self.connection.port = 5001 if self.connection.https else 5000

            # Derive instance_id from host if not set
            if derived:
                self.instance_id = _derive_instance_id(self.connection.host)

        # Normalize and validate instance_id format. `is not None`: an empty one is
        # refused too (#485).
        if self.instance_id is not None:
            problem = instance_id_problem(self.instance_id)
            if derived and problem:
                # The host is already a bare name or address; what's left is an IPv6
                # address, or a name with characters outside a-z, 0-9 and hyphens. Name
                # where it came from: the file may look fine (QA round 2 on #532).
                from_env = (os.environ.get("SYNOLOGY_HOST") or "").strip()
                source = "SYNOLOGY_HOST" if from_env else "connection.host"
                problems.append(
                    f"{source} gives no usable instance_id: its first part has "
                    "characters other than letters a-z, digits and hyphens (an IPv6 address, "
                    "an underscore, or a letter outside a-z). Set instance_id, or the "
                    f"SYNOLOGY_INSTANCE_ID environment variable ({_INSTANCE_ID_RULE})."
                )
            elif problem:
                problems.append(f"instance_id {problem}.")
            else:
                self.instance_id = self.instance_id.lower()

        if problems:
            raise ValueError("\n".join(problems))
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


def _printable(name: str) -> str:
    """A name from the config as is, or quoted if it could forge a line (QA round 1 on #541)."""
    return name if name.isprintable() else repr(name)


def _config_exists(path: Path) -> bool:
    """Whether the config file is there. An error that isn't "no such file" stays an error, with
    the path: a folder without permission, a name too long. Never FileNotFoundError, which
    would mean env-only mode, ignoring the file (#483). By `stat`: `Path.exists` swallows
    every OSError on Python 3.14 (pre-QA r4 on #541)."""
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError, ValueError):  # ValueError: a NUL
        return False
    except OSError as e:
        msg = f"Config file can't be read: {_shown_path(path)!r}: {e.strerror or type(e).__name__}"
        raise OSError(msg) from e
    return True


def _shown_path(path: Path) -> str:
    """The path made absolute for messages, or as given if it can't be.

    `absolute()` raises when the working folder has been deleted; the message must
    still name the path (pre-QA on #529).
    """
    try:
        return str(path.absolute())
    except OSError:
        return str(path)


class WorkingFolderConfigNotLoadedError(FileNotFoundError):
    """A `./mcp-synology.yaml` is there, and it counts only when it's named (#552).

    A folder someone merely opened could otherwise name another host and be sent the password
    stored for this one, or ask for more access. A FileNotFoundError, so `setup` can still make
    a config from here, but `load_config` never answers it with env-only mode: that would move
    someone relying on this file from HTTPS to plain HTTP, the downgrade #499 refuses.
    """


def quoted_path(path: Path) -> str:
    """An absolute path as a POSIX shell takes it (on Windows, for reading only: see below), for a
    message that says to use it: `repr` switches to double quotes around a `'`, where `$(...)` in a
    folder's name runs when pasted (QA on #555). Each character that could forge a line is escaped,
    and only those, so the path stays readable; a path with one isn't one to use as shown."""
    text = _shown_path(path)
    return _quoted_for_a_shell(
        "".join(c if c.isprintable() else c.encode("unicode_escape").decode() for c in text)
    )


def _quoted_for_a_shell(text: str, *, windows: bool = os.name == "nt") -> str:
    """Quoted for a POSIX shell. On Windows, only double quotes, for reading: no one form is safe
    in both cmd.exe and PowerShell (`%VAR%` and `$(...)` still act inside them), and none of it
    could be run here. A hostile folder's safe route there is the relative --config (QA on #555)."""
    return f'"{text}"' if windows else shlex.quote(text)


def _refuse_a_working_folder_config() -> None:
    local_config = Path("mcp-synology.yaml")
    try:
        try:
            # Only a regular file is taken for someone's config. A pipe can be named, but not
            # one in a folder that's only been opened: reading it would wait for its writer.
            if not stat.S_ISREG(local_config.stat().st_mode):
                return
        except PermissionError:
            # A link into a locked folder is someone's config, so it refuses too. In a working
            # folder that can't be searched, `lstat` fails as well, and nothing can be said about
            # any file.
            try:
                local_config.lstat()
            except PermissionError:
                return
    except OSError as e:
        # Not there, or a dangling link or link loop (the name itself is fixed, so only a link's
        # target can be too long). Anything else (a stale mount) may be someone's config, and
        # env-only mode would be the downgrade the class refuses.
        if e.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENAMETOOLONG):
            return
        msg = (
            f"{_shown_path(local_config)!r} can't be checked: {e.strerror or type(e).__name__}. "
            "Fix the path or its permissions, or name a config file with --config."
        )
        raise OSError(msg) from e
    # "If you trust" and the full path: a model repairing a failed server, or someone editing an
    # MCP client's args, would otherwise paste the flag in as it stands, and whatever folder that
    # client starts in next would be loaded as named. "Don't copy": the same model could move the
    # file to the home location, which applies it in every folder.
    env_only = (
        "SYNOLOGY_HOST alone isn't used while this file is here. "
        if os.environ.get("SYNOLOGY_HOST", "").strip()
        else ""
    )
    msg = (
        f"{quoted_path(local_config)} isn't loaded automatically: a config in the folder "
        "you're in could send your stored credentials to another host. "
        f"{env_only}If you trust this file (it chooses the host your stored password goes to), "
        "pass --config ./mcp-synology.yaml, or its full path, without shell quotes, in an MCP "
        "client's config. To run without it, start from another folder or move it out of this "
        "one. Don't copy it to the home location unless you trust it."
    )
    raise WorkingFolderConfigNotLoadedError(msg)


def _expanded_config_path(value: str, source: str) -> Path:
    """``~`` expanded; an unknown ``~user`` is RuntimeError, a traceback in every command."""
    try:
        return Path(value).expanduser()
    except RuntimeError:
        msg = f"Config file not found (from {source}): {value!r}: no such home folder."
        raise FileNotFoundError(msg) from None


def discover_config_path(explicit_path: str | None = None) -> Path:
    """Find the config file using the discovery hierarchy.

    1. Explicit --config flag
    2. MCP_SYNOLOGY_CONFIG env var
    3. ~/.config/mcp-synology/config.yaml

    A ./mcp-synology.yaml is never found here: it has to be named (#552).
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
        path = _expanded_config_path(explicit_path, "--config")
        # Absolute in messages: an MCP client starts the server in a working folder the
        # user rarely knows (QA round 1 on #529).
        if not _config_exists(path):
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
        path = _expanded_config_path(env_path, "MCP_SYNOLOGY_CONFIG")
        if not _config_exists(path):
            # An error, never env-only mode: that would run read-only over plain HTTP
            # instead of the config the variable names (#499).
            msg = (
                f"Config file not found (from MCP_SYNOLOGY_CONFIG): {_shown_path(path)!r}. "
                "Fix the path."
            )
            raise FileNotFoundError(msg)
        logger.info("Config: %r (from MCP_SYNOLOGY_CONFIG)", _shown_path(path))
        return path

    home_config = Path.home() / ".config" / "mcp-synology" / "config.yaml"
    logger.debug("Checking default config path: %r", str(home_config))
    try:
        home_found = _config_exists(home_config)
    except OSError as e:
        msg = f"{e}. Fix the path or its permissions, or name a config file with --config."
        raise OSError(msg) from e
    if home_found:
        logger.info("Config: %r", str(home_config))
        return home_config
    _refuse_a_working_folder_config()
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
    field_type: Any = Annotated[annotation, *bounds] if bounds else annotation
    try:
        return TypeAdapter(field_type).validate_python(text)
    except ValidationError:
        raise EnvVarError(
            f"{env_var} must be {_env_expected(annotation, bounds)} (it sets {dotted_path})."
        ) from None


def _warn_always(msg: str) -> None:
    """Log ``msg`` as a WARNING even when the log level would hide it (#533).

    For security warnings only: `SYNOLOGY_LOG_LEVEL=error` used to hide that HTTPS was
    off, so `SYNOLOGY_HTTPS=off` turned it off without a word. `Logger.handle` skips the
    level check, so the record still goes through the handlers in the usual format. While
    the config loads, where these are logged, that's stderr and the CLI's early-records
    buffer, which replays them into `logging.file` once it's open, whatever the level
    (`show_always`; `cli/logging_.py`, #511 PR 3).
    """
    logger.handle(
        logger.makeRecord(
            logger.name,
            logging.WARNING,
            "(unknown file)",
            0,
            msg,
            (),
            None,
            extra={"show_always": True},
        )
    )


# Settings an env var mustn't change silently over the config file's own value: they
# say which NAS the credentials go to, how, and which keyring entry holds them (#533).
_WATCHED_OVERRIDES = ("connection.host", "connection.port", "connection.https", "instance_id")


def _file_value(raw: dict[str, Any], dotted_path: str) -> Any:
    """The value the config file sets at ``dotted_path``, or None."""
    target: Any = raw
    for part in dotted_path.split("."):
        if not isinstance(target, dict):
            return None
        target = target.get(part)
    return target


# Only an address: `[::1]/x`, `[a:b]@nas` or a zone (`[fe80::1%eth0]`) can differ by case.
_BRACKETED_ADDRESS = re.compile(r"\[[0-9A-Fa-f:.]+\]")


def _as_field_reads(dotted_path: str, value: Any) -> Any:
    """``value`` through the field's own type, or as written if the field refuses it."""
    try:
        return TypeAdapter(_env_field_annotation(dotted_path)).validate_python(value)
    except ValidationError:
        return value


def _as_compared(dotted_path: str, value: Any) -> Any:
    """``value`` as the loaded config would use it, for the override check.

    It goes through the field's own type, so every spelling pydantic accepts in the file
    compares equal (`https: 'on'`, `port: "5001"`; QA round 1 on #535). A value the field
    refuses stays as written, so it differs: validation reports it unless the variable
    replaces it. An instance ID is lowered, as the config does.

    A host name is compared as httpx sends it: lower-case, IDNA-mapped (`bücher.example`
    is `xn--bcher-kva.example`). A bracketed IPv6 address only ignores case, unless it has
    a zone, which names an interface. Any other host with a `:` stays as written, so it
    differs: httpx's `URL(host=)` would bracket a bare `::1`. Only the file's value can be
    such a host, or have `@` or `/`, which httpx escapes: the variable must be bare
    (`parse_env_value`), so neither form can equal it.
    Never read a host from a URL string, where `admin@nas` or `nas:5001` is just `nas`,
    which would hide the change.
    """
    typed = _as_field_reads(dotted_path, value)
    if not isinstance(typed, str):
        return typed
    if dotted_path == "connection.host":
        if ":" in typed:
            return typed.lower() if _BRACKETED_ADDRESS.fullmatch(typed) else typed
        try:
            return httpx.URL(scheme="https", host=typed).raw_host.decode("ascii")
        except httpx.InvalidURL:
            return typed
    return typed.lower() if dotted_path == "instance_id" else typed


def _merge_env_overrides(raw: dict[str, Any], *, from_file: bool = False) -> dict[str, Any]:
    """Merge environment variable overrides into the raw config dict.

    An empty (or blank) variable counts as unset, as the credentials already did
    (`auth.resolve_credentials`): `SYNOLOGY_HTTPS=` leaves the config's `https` alone
    instead of turning it off.

    With ``from_file``, a variable that changes a connection setting or the instance
    ID the file sets (or, for the instance ID, derives from its host) gets a WARNING
    that a log level can't hide, naming the variable, never the value (#533). It used
    to be DEBUG only, so `SYNOLOGY_HOST` sent the file's keyring credentials to another
    host without a word.
    """
    before: dict[str, Any] = {}
    id_is_derived = False
    if from_file:
        before = {path: _file_value(raw, path) for path in _WATCHED_OVERRIDES}
        # As the field reads it: `host: !!binary bmFz` is `nas`, and derives its ID.
        file_host = _as_field_reads("connection.host", before["connection.host"])
        if before["instance_id"] is None and isinstance(file_host, str) and file_host:
            before["instance_id"] = _derive_instance_id(file_host)
            id_is_derived = True

    for env_var, dotted_path in ENV_VAR_MAP.items():
        value = os.environ.get(env_var)
        if value is None or not value.strip():
            continue

        logger.debug("Env override: %s -> %s", env_var, dotted_path)
        parsed = parse_env_value(env_var, dotted_path, value)
        file_value = before.get(dotted_path)
        if file_value is not None and _as_compared(dotted_path, file_value) != _as_compared(
            dotted_path, parsed
        ):
            # A file with no `instance_id` key: someone checking it would find no such
            # setting (pre-QA r4 on #535).
            overridden = (
                "the instance_id the config file's host gives"
                if id_is_derived and dotted_path == "instance_id"
                else f"{dotted_path} from the config file"
            )
            _warn_always(
                f"{env_var} overrides {overridden}. Unset {env_var} if that isn't intended."
            )
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


def _no_proxy_covers(entry: str, target: httpx.URL) -> bool:
    """Whether httpx lets this `NO_PROXY` entry send `target` direct. Its rules, not the standard
    library's: `.nas.home` leaves `nas.home` on the proxy, and an entry can name a port or a
    scheme. An entry with no host only reorders httpx's proxy choices, so it counts as no cover:
    at worst a warning too many."""
    if "://" in entry:
        pattern = entry
    else:
        try:
            address = ipaddress.ip_address(entry.split("/")[0])
        except ValueError:
            address = None
        if address is None:
            pattern = f"all://{entry}" if entry.lower() == "localhost" else f"all://*{entry}"
        else:
            pattern = f"all://[{entry}]" if address.version == 6 else f"all://{entry}"
    try:
        url = httpx.URL(pattern)
    except httpx.InvalidURL:
        return False
    if url.scheme not in ("all", target.scheme) or url.port not in (None, target.port):
        return False
    if url.host.startswith("*."):
        return target.host.endswith(url.host[1:]) and target.host != url.host[1:]
    if url.host.startswith("*"):
        return target.host == url.host[1:] or target.host.endswith(f".{url.host[1:]}")
    return bool(url.host) and target.host == url.host


def _proxy_applies(conn: ConnectionConfig) -> bool:
    """Whether httpx would send requests to this host through a proxy from the environment or the
    system (#558): `trust_env` is on by default, so an MCP client's `env` block can route the
    login through one. httpx's own reading is private; a test compares this with it."""
    scheme = "https" if conn.https else "http"
    proxies = urllib.request.getproxies()
    if not (proxies.get(scheme) or proxies.get("all")):
        return False
    try:
        target = httpx.URL(f"{scheme}://{conn.host}:{conn.port}")
    except httpx.InvalidURL:
        return False
    entries = [entry.strip() for entry in proxies.get("no", "").split(",")]
    if "*" in entries:
        return False
    return not any(_no_proxy_covers(entry, target) for entry in entries if entry)


def _emit_warnings(config: AppConfig) -> None:
    """Emit warnings for insecure or unusual config."""
    # A username alone isn't a secret (keyring password + config username is the
    # recommended setup), so only a password triggers the warning (#167).
    # At any log level, as the transport warnings below (pre-QA on #533).
    if config.auth.password:
        # Blank counts as unset (_merge_env_overrides): the password is then the file's.
        if (os.environ.get("SYNOLOGY_PASSWORD") or "").strip():
            _warn_always(
                "Password provided via the SYNOLOGY_PASSWORD environment variable. "
                "Consider using 'mcp-synology setup' to store them securely in the OS keyring.",
            )
        else:
            _warn_always(
                "Plaintext password found in config file. "
                "Use 'mcp-synology setup' to store credentials securely in the OS keyring.",
            )

    conn = config.connection
    if conn is not None:
        # WARNINGs, as the risk warrants (#193), shown at any log level: at
        # SYNOLOGY_LOG_LEVEL=error, `SYNOLOGY_HTTPS=off` used to turn HTTPS off with no
        # output at all (#533).
        if not conn.https:
            _warn_always(
                "HTTPS is disabled. Credentials will be sent in cleartext. "
                "Acceptable on a trusted LAN; to turn it on, see https://github.com/cmeans/mcp-synology/blob/main/docs/https.md",
            )
        if conn.https and not conn.verify_ssl:
            _warn_always(
                "SSL certificate verification is disabled. "
                "Only use this on trusted networks with self-signed certificates.",
            )
        if _proxy_applies(conn):
            # Never its value: a proxy URL can hold a password.
            _warn_always(
                "Requests to this NAS, the login included, go through a proxy set in the "
                "environment or the system (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY). If that isn't "
                "intended, unset it, or add the host to NO_PROXY."
            )

    for name, mod in config.modules.items():
        if not mod.enabled:
            logger.info("Module %r is listed but disabled.", name)


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
    # Text of any length, and its message says what's wrong.
    if loc_parts[:1] == ["custom_instructions"]:
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
    # Nor the instance ID, whatever its type (`instance_id: 123`): anything may be pasted
    # there (#511 PR 2, decision 4; pre-QA on #532).
    if loc_parts[:1] == ["instance_id"]:
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
    the config's own key names, module names and keys in a module's settings, never a
    value."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


_MERGE_TAG = "tag:yaml.org,2002:merge"
# A plain `=` key. SafeLoader rewrites this tag to text only while flattening, which comes
# after the checks, and has no constructor for it (QA round 2 on #532).
_VALUE_TAG = "tag:yaml.org,2002:value"
# A repeated module or settings key is named only in this form, so it can't forge a line
# of the error (QA round 2 on #532).
_NAMEABLE_KEY = re.compile(r"[\w.-]+")
# A pasted or generated file could repeat thousands of keys; nobody fixes those one by one.
_MAX_KEY_PROBLEMS = 50


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
        # Only some types-PyYAML versions type this call, and mypy's strict mode fails on an ignore
        # it doesn't need, hence `unused-ignore` (#551). Its arguments are still checked.
        return super().construct_document(node)  # type: ignore[no-untyped-call,unused-ignore]

    def _check_written_keys(self, root: yaml.Node) -> None:
        # Every problem together, in file order (#511 PR 3): through an MCP client, each
        # round of fixing one and rerunning is a client restart.
        # Each node with the keys leading to it, so a repeat under `modules` or a module's
        # `settings` can be named (pre-QA on #532); a node shared by alias is checked once,
        # at the first path reached.
        problems: list[tuple[int, int, str]] = []
        stack: list[tuple[yaml.Node, tuple[str, ...]]] = [(root, ())]
        visited: set[int] = set()
        while stack:  # a loop, not recursion: an alias can make the graph share nodes
            node, path = stack.pop()
            if id(node) in visited:
                continue
            visited.add(id(node))
            if isinstance(node, yaml.MappingNode):
                problems.extend(self._check_mapping(node, path))
                # Reversed, so nodes pop in file order, and a node shared by alias is checked
                # where it's written: under `auth`, its keys stay hidden (QA round 3 on #532).
                for key_node, value_node in reversed(node.value):
                    name = key_node.value if isinstance(key_node, yaml.ScalarNode) else ""
                    stack.append((value_node, (*path, str(name))))
                    stack.append((key_node, path))
            elif isinstance(node, yaml.SequenceNode):
                stack.extend((item, path) for item in reversed(node.value))
        if problems:
            texts = [text for *_, text in sorted(problems)]
            more = len(texts) - _MAX_KEY_PROBLEMS
            if more > 0:
                texts = [*texts[:_MAX_KEY_PROBLEMS], f"… and {more} more like these."]
            raise ConfigKeyError("\n".join(texts))

    def _check_mapping(
        self, node: yaml.MappingNode, path: tuple[str, ...] = ()
    ) -> list[tuple[int, int, str]]:
        """This mapping's problems, each with the line and column it's on."""
        problems: list[tuple[int, int, str]] = []
        seen: dict[Any, tuple[int, int, bool]] = {}
        merge_at: tuple[int, int, bool] | None = None
        furthest = -1
        for key_node, value_node in node.value:
            # A key given by alias (`{*k: …}`) has its anchor's marks, from before this
            # mapping (pre-QA r4), in an earlier key or inside an earlier value (QA round 2
            # on #532): `_repeat_place` reports the mapping's own place then. A key written
            # here always comes after the last entry. A block value's end mark is the next
            # key's start.
            mark = key_node.start_mark
            clamped = mark.index < node.start_mark.index or mark.index <= furthest
            furthest = max(furthest, mark.index, value_node.end_mark.index - 1)
            line, column = mark.line + 1, mark.column + 1
            if key_node.tag == _MERGE_TAG:
                # Its keys may be overridden: YAML's own rule, not a repeat. A second `<<`
                # is a repeated key like any other (QA round 1 on #532). The first in a
                # list wins, so the later one goes first to keep the result (QA round 2).
                if merge_at is not None:
                    where = _repeat_place(node, merge_at, line, column, clamped)
                    problems.append(
                        (
                            line,
                            column,
                            f"The config file has `<<` twice in one section ({where}), and YAML "
                            "would silently let the later one win. Merge both in one, later one "
                            "first, since the first in a list wins: `<<: [*later, *earlier]`.",
                        )
                    )
                merge_at = (line, column, clamped)
                continue
            if node.flow_style and _has_no_colon(node, key_node, value_node):
                # Directly under `modules`, leaving it out would drop the module (QA round
                # 2 on #532).
                empty_advice = (
                    "to list a module with its defaults, give it a colon: "
                    "`{filestation: , photos: }`"
                    if path == ("modules",)
                    else "to leave a setting empty, leave it out"
                )
                problems.append(
                    (
                        line,
                        column,
                        f"The config file has a key with no value inside {{…}} (line {line}). "
                        "It isn't shown, since it may be part of a password. A colon needs a "
                        f"space after it (`host: nas`, not `host:nas`); {_SPLIT_HINT}; "
                        f"{empty_advice}.",
                    )
                )
                continue
            if not isinstance(key_node, yaml.ScalarNode):
                continue  # a list or mapping as a key: SafeLoader's own error follows
            key = (
                key_node.value if key_node.tag == _VALUE_TAG else self.construct_object(key_node)  # type: ignore[no-untyped-call,unused-ignore]
            )
            first = seen.get(key)
            if first is not None:
                # A module's name, and a key in its settings, can be named too: the
                # warnings already name them, since settings hold no credentials (pre-QA
                # on #532: a pasted second `filestation:` was "a key … isn't shown").
                known = isinstance(key, str) and (
                    key in _config_keys()
                    or (
                        _NAMEABLE_KEY.fullmatch(key) is not None
                        and (
                            path == ("modules",)
                            or (len(path) == 3 and path[0] == "modules" and path[2] == "settings")
                        )
                    )
                )
                where = _repeat_place(node, first, line, column, clamped)
                problems.append(
                    (
                        line,
                        column,
                        f"The config file has {f'`{key}`' if known else 'a key'} twice in one "
                        f"section ({where}), and YAML would silently keep only the last. "
                        "Remove one."
                        + ("" if known else " The key isn't shown: it may be part of a password."),
                    )
                )
                continue
            seen[key] = (line, column, clamped)
        return problems


def _repeat_place(
    node: yaml.MappingNode, first: tuple[int, int, bool], line: int, column: int, clamped: bool
) -> str:
    """Where a repeated key is: both lines, or on one line (`{k: 1, k: 2}`) the columns
    (pre-QA r3). If either came by alias, its marks are the mapping's, so "columns 4 and 4"
    (QA round 1 on #532); the mapping's own line is all there is."""
    if first[2] or clamped:
        start = node.start_mark.line + 1
        if node.flow_style:
            return f"in the {{…}} at line {start}"
        return f"in the section that starts at line {start}"
    if first[0] == line:
        return f"line {line}, columns {first[1]} and {column}"
    return f"lines {first[0]} and {line}"


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
    return format_config_errors(errors)


def format_config_errors(errors: list[Any]) -> str:
    """The block `format_validation_error` prints, for pydantic's error dicts.

    A model-level error holds one problem per line (`AppConfig._validate_config`), each
    listed and counted as its own.
    """
    lines: list[str] = []
    non_text_keys = _non_text_keys(errors)
    for err in errors:
        loc_parts = [str(p) for p in err.get("loc", ())]
        loc = _printable(_shown_location(err, loc_parts, non_text_keys))
        msg = str(err.get("msg", "validation error"))
        input_value = err.get("input")
        suffix = ""
        if _may_show(err, loc_parts):
            suffix = f" (got {input_value!r})" if input_value not in (None, "") else ""
        if not loc_parts:  # one wording for a model-level problem, alone or among several
            msg = msg.removeprefix("Value error, ")
        lines.extend(f"  - {loc}: {part}{suffix}" for part in msg.split("\n"))
    head = f"Configuration validation failed ({len(lines)} error(s)):"
    more = len(lines) - _MAX_KEY_PROBLEMS
    if more > 0:
        lines = [*lines[:_MAX_KEY_PROBLEMS], f"  … and {more} more like these."]
    return "\n".join([head, *lines])


def config_error_text(exc: Exception) -> str:
    """What `serve`, `check` and `setup` print for an error from loading a config.

    Line numbers point nowhere without the file, and at SYNOLOGY_LOG_LEVEL=warning the
    `Config:` line doesn't show (QA round 2 on #532), so the path is part of the error.
    """
    if isinstance(exc, ValidationError):
        text = format_validation_error(exc)
    elif isinstance(exc, yaml.YAMLError):
        text = format_yaml_error(exc)
    else:
        text = str(exc)
    return "\n".join([text, *getattr(exc, "__notes__", [])])


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load, merge, validate, and return the application config.

    Args:
        path: Explicit config file path, or None for auto-discovery.

    Falls back to env-var-only mode if no config file is found but
    SYNOLOGY_HOST is set, unless --config or MCP_SYNOLOGY_CONFIG names a file, which must
    exist (#499), or a ./mcp-synology.yaml is there, which has to be named or moved (#552).
    """
    try:
        config_path = discover_config_path(None if path is None else str(path))
    except WorkingFolderConfigNotLoadedError:
        raise  # never env-only mode: see the class
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
        config = _load_file(config_path)
    except EnvVarError:
        raise  # names the variable; nothing in the file to look at
    except (ValueError, yaml.YAMLError) as e:
        e.add_note(config_file_note(config_path))
        raise
    config._source_path = config_path

    logger.debug(
        "Config loaded: instance_id=%s, host=%s, port=%s, https=%s, modules=%s",
        config.instance_id,
        config.connection.host if config.connection else None,
        config.connection.port if config.connection else None,
        config.connection.https if config.connection else None,
        list(config.modules.keys()),
    )
    for name, mod in config.modules.items():
        logger.debug("  Module %r: enabled=%s, permission=%s", name, mod.enabled, mod.permission)

    _emit_warnings(config)
    return config


def config_file_note(path: Path) -> str:
    """The line an error adds to say which file it's about: quoted, so a newline in a
    folder's name can't forge a line of the error."""
    return f"Config file: {_shown_path(path)!r}"


def _load_file(config_path: Path) -> AppConfig:
    """Read, parse, merge the environment over, and validate one config file."""
    try:
        # `/dev/zero` would read until memory ran out. A pipe stays allowed: `--config <(generate)`.
        if config_path.is_char_device() or config_path.is_block_device():
            raise ValueError("The config file can't be read: it's a device, not a file.")
        raw_text = config_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        # Its message quotes the byte, which may be one of a password's (QA round 2 on #469).
        line = e.object[: e.start].count(b"\n") + 1
        raise ValueError(
            f"The config file isn't valid UTF-8 (line {line}). Save it as UTF-8 (in Notepad: "
            "File > Save as, Encoding: UTF-8)."
        ) from None
    except FileNotFoundError:
        raise  # removed since discovery: the callers' own "not found" handling
    except OSError as e:
        # A directory, or no permission (#483). ValueError: every command prints it.
        raise ValueError(f"The config file can't be read: {e.strerror or type(e).__name__}.") from e
    raw = parse_config_text(raw_text)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            "The config file must be a YAML mapping of settings "
            "(schema_version: 1, connection: ...)"
        )

    raw = _merge_env_overrides(raw, from_file=True)

    return AppConfig.model_validate(raw)
