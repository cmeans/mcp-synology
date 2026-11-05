"""Auth manager: session lifecycle, 2FA, credential resolution.

Strategy chain auto-detects 2FA vs non-2FA:
1. If device_id available -> attempt login with device token (2FA remembered device)
2. If no device_id and login succeeds -> simple login (no 2FA)
3. If no device_id and error 403 -> 2FA required, user must run setup CLI
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from typing import TYPE_CHECKING, Any

import keyring as kr
from keyring.errors import KeyringError

from mcp_synology.core.errors import (
    CHECK_COMMAND,
    SETUP_COMMAND,
    SETUP_NEW_CONFIG,
    AuthenticationError,
    CredentialHostError,
    LoginRefusedError,
    RememberedRefusalError,
    SessionExpiredError,
    SynologyError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp_synology.core.client import DsmClient
    from mcp_synology.core.config import AppConfig

logger = logging.getLogger(__name__)

# DSM Auth API error code for 2FA required
_ERROR_2FA_REQUIRED = 403


def _present_or_none(value: str | None) -> str | None:
    """Return value unchanged if it contains non-whitespace content; else None.

    Used to filter empty / whitespace-only credentials at every read site
    (env vars, plaintext config, OS keyring) so they fall through the
    resolution chain instead of flowing into `login()` as bogus credentials
    that surface as a generic DSM 400.
    """
    return value if (value and value.strip()) else None


def _recorded_host(service: str) -> str | None:
    """The host this keyring entry records, if any. Raises what the keyring raises."""
    return (kr.get_password(service, "host") or "").strip() or None


def _entry_is_for_this_host(
    config: AppConfig, service: str, *, may_record: bool
) -> tuple[bool, str | None]:
    """Whether this keyring entry is for ``connection.host``, and the host it records (#536).

    An entry from before the host was recorded may take it only when the config says which host
    is meant (``host_may_be_recorded``) and this login uses its password. That's written by
    ``AuthManager.login`` once DSM accepts the password, not here: a host that's down, or another
    NAS refusing it, would otherwise pin the entry and refuse its real config (pre-QA on #559).
    Raises what the keyring raises.
    """
    if config.connection is None:  # the config's own validation requires it
        raise RuntimeError("Config missing connection settings")
    from mcp_synology.core.config import comparable_host

    host = comparable_host(config.connection.host)
    recorded = _recorded_host(service)
    if recorded is None and may_record and config.host_may_be_recorded:
        config._host_to_record = host
        return True, host
    return recorded == host, recorded


def host_this_login_would_record(config: AppConfig) -> str | None:
    """The host a login would record on a keyring entry with no record, if any: for `check`, which
    asks first (maintainer, 2026-11-05). `serve` has no one to ask. A login that would be refused
    records nothing, and says so itself."""
    try:
        resolve_credentials(config)
    except SynologyError:
        return None
    return config._host_to_record


def entry_left_unrecorded(config: AppConfig) -> bool:
    """Whether this config's keyring entry holds credentials and no host. For `check`, whose
    login may not have used it (credentials from elsewhere), so nothing recorded it."""
    service = f"mcp-synology/{config.instance_id or 'default'}"
    try:
        return _recorded_host(service) is None and any(
            _present_or_none(kr.get_password(service, key))
            for key in ("username", "password", "device_id")
        )
    except Exception as e:  # backends raise their own types
        logger.debug("Keyring error reading the entry: %s", e, exc_info=True)
        return False


def _device_token_may_be_sent(config: AppConfig, service: str) -> bool:
    """A device token alone doesn't block a login with credentials from elsewhere, which carries
    none of the keyring's secrets, but it mustn't go to another host (maintainer, 2026-11-05).
    Without it, a 2FA account gets DSM's own error and its advice.

    Only reads: a login that uses none of the entry's password shouldn't settle whose it is. An
    entry with no record isn't known to be this host's, so its token stays out (QA round 1 on
    #559: recording here pinned another NAS's password to this host, and refused its real config).
    This warns once per config: credentials are resolved again for each call while a refused login
    is remembered.
    """
    if config.connection is None:  # the config's own validation requires it
        raise RuntimeError("Config missing connection settings")
    from mcp_synology.core.config import comparable_host

    try:
        matches = _recorded_host(service) == comparable_host(config.connection.host)
    except Exception as e:  # backends raise their own types
        logger.debug("Keyring error reading the host: %s", e, exc_info=True)
        # Not "isn't recorded", and the warning below stays for a real mismatch (QA round 2).
        if not config._device_token_read_warned:
            config._device_token_read_warned = True
            logger.warning(
                "Not sending the device token in keyring entry %r: couldn't read which host it's "
                "for.",
                config.instance_id or "default",
            )
        return False
    if not matches and not config._device_token_warned:
        config._device_token_warned = True
        # The login's own error is DSM's and doesn't mention the token (maintainer, 2026-11-05),
        # so this is the only place that gives the way out.
        logger.warning(
            "Not sending the device token in keyring entry %r: it isn't recorded for this host. "
            "%s.",
            config.instance_id or "default",
            how_to_record(config),
        )
    return matches


def _host_variables() -> list[str]:
    """The variables that stop a first use from recording the host, set for this server."""
    import os

    return [v for v in ("SYNOLOGY_HOST", "SYNOLOGY_INSTANCE_ID") if os.environ.get(v, "").strip()]


def _connection_variables() -> list[str]:
    """The variables that change how a terminal's command reaches the NAS, set for this server."""
    import os

    return [v for v in ("SYNOLOGY_PORT", "SYNOLOGY_HTTPS") if os.environ.get(v, "").strip()]


def _setup_command(config: AppConfig, *, new_instance: bool = False) -> str:
    """`setup` as this server needs it run. The variables aren't in a terminal's environment when
    a client's `env` block set them: it then stores for the file's host or instance (QA round 1
    on #559), or can't connect with the host but not the port or HTTPS (QA round 2). With no
    config file, plain `setup` takes the host from SYNOLOGY_HOST. ``new_instance``: the instance
    ID is changing through that variable, so it's to be set, and not to the same value."""
    command = SETUP_COMMAND if config._source_path else SETUP_NEW_CONFIG
    variables = [
        v
        for v in _host_variables() + _connection_variables()
        if not (new_instance and v == "SYNOLOGY_INSTANCE_ID")
    ]
    parts = ["that SYNOLOGY_INSTANCE_ID"] if new_instance else []
    if variables:
        parts.append(f"the same {' and '.join(variables)}")
    if parts:
        command += f" with {' and '.join(parts)}"
    return command


def own_instance_id_clause(config: AppConfig, nas: str = "this NAS") -> str:
    """The way that leaves the entry alone, as a clause: `setup` on the same entry replaces it, and
    it may be another NAS's (pre-QA on #559; the maintainer's wording, 2026-11-05)."""
    import os

    in_the_file = config._source_path and not os.environ.get("SYNOLOGY_INSTANCE_ID", "").strip()
    where = "in the config" if in_the_file else "with SYNOLOGY_INSTANCE_ID"
    # At a terminal, without that variable, `setup` would ask about this same entry (pre-QA on
    # #559).
    command = _setup_command(config, new_instance=not in_the_file)
    return (
        f"give {nas} its own instance_id {where}, then run {command}, which asks for the password "
        "and leaves this entry alone"
    )


def check_route(config: AppConfig) -> str | None:
    """How `check` would record this config's keyring entry, as a clause, or None if it can't: it
    records only an entry with no record, using its password, from a config named by its full path
    with neither host variable (QA round 1 and pre-QA on #559). It sends the stored password, once
    someone at the terminal says yes, so whoever offers it says whose credentials it's for (QA
    round 2 on #559)."""
    import os

    service = f"mcp-synology/{config.instance_id or 'default'}"
    try:
        if (
            config._source_path is None
            or _host_variables()
            or _recorded_host(service) is not None
            or _present_or_none(kr.get_password(service, "password")) is None
            # A password alone can't log in, and following this would say "No credentials found".
            or not (
                _present_or_none(os.environ.get("SYNOLOGY_USERNAME"))
                or _present_or_none(config.auth.username)
                or _present_or_none(kr.get_password(service, "username"))
            )
        ):
            return None
    except Exception as e:  # backends raise their own types
        logger.debug("Keyring error reading the entry: %s", e, exc_info=True)
        return None
    # The config's value is the variable's when that's set (_merge_env_overrides), and a terminal
    # has its own environment.
    in_the_file = _present_or_none(config.auth.password) and not _present_or_none(
        os.environ.get("SYNOLOGY_PASSWORD")
    )
    remove = "remove auth.password from the config and " if in_the_file else ""
    # Without them it can't connect, or sends the password over plain HTTP where the server would
    # use HTTPS (pre-QA on #559).
    connection = _connection_variables()
    # A username only in a variable has to be at the terminal too (pre-QA on #559).
    if _present_or_none(os.environ.get("SYNOLOGY_USERNAME")):
        try:
            kept = _present_or_none(kr.get_password(service, "username"))
        except Exception as e:  # backends raise their own types
            logger.debug("Keyring error reading the entry: %s", e, exc_info=True)
            return None
        if not kept:
            connection.append("SYNOLOGY_USERNAME")
    same = f" with the same {' and '.join(connection)} and" if connection else " with"
    # This is often read at a terminal, which can't see a client's `env` block, so it goes by what
    # the terminal does know (QA round 3 on #559).
    plain = (
        ""
        if config.connection is not None and config.connection.https
        else ". With this config it goes over plain HTTP: if a client's env block sets "
        "SYNOLOGY_HTTPS, set it here too"
    )
    return (
        f"{remove}run {_RECORD_HOST} at a terminal,{same} no SYNOLOGY_PASSWORD set. It asks before "
        f"sending the stored password there, and keeps any 2FA token{plain}"
    )


def how_to_record(config: AppConfig) -> str:
    """What someone at a terminal can do about this entry (the maintainer's wording, 2026-11-05,
    after QA round 2 and pre-QA on #559). Whether this is the right NAS isn't the question:
    `nas.office` is right for its own config while the entry holds `nas.home`'s password. `check`
    would send it there once told yes, so it's only for someone who knows whose credentials these
    are, and `setup` on this entry would replace the other NAS's. It doesn't name a recorded host,
    as it's also in a log; `check` prints that just before. A tool's reply gets
    `_advice_for_the_model` instead."""
    host = config.connection.host if config.connection else None
    route = check_route(config)
    if route:
        return (
            f"If you know these are the credentials for {host!r}, {route}. Otherwise, "
            f"{own_instance_id_clause(config)}"
        )
    service = f"mcp-synology/{config.instance_id or 'default'}"
    try:
        recorded = _recorded_host(service)
    except Exception as e:  # backends raise their own types
        logger.debug("Keyring error reading the entry: %s", e, exc_info=True)
        recorded = None
    if recorded:
        return (
            "If the host they were set up for is this NAS's old address, run "
            f"{_setup_command(config)} and replace the entry. If it's another NAS, "
            f"{own_instance_id_clause(config, 'this one')}"
        )
    clause = own_instance_id_clause(config)
    return clause[0].upper() + clause[1:]


_WRONG_NAS = (
    "check connection.host, SYNOLOGY_HOST and the instance ID. A NAS reached by two addresses "
    "needs a second config with its own instance_id."
)


# A URL: a relative path isn't in the wheel, and a model would read it from its working folder,
# which a planted file could fill (QA round 3 on #559).
_CREDENTIALS_DOC = (
    "https://github.com/cmeans/mcp-synology/blob/main/docs/credentials.md"
    "#credentials-are-tied-to-a-host"
)


def _advice_for_the_model(config: AppConfig, host: str) -> str:
    """This refusal's reply names no command that records the host: only a person can answer it,
    and a model can't know whose credentials these are, least of all from a config in a folder it
    was pointed at (QA round 2 on #559). `setup` asks someone for the password. The older
    unreachable, busy and refused-login replies still name `check` (the maintainer, 2026-11-05)."""
    return (
        f"If {host!r} is the right NAS, ask the user to {own_instance_id_clause(config, 'it')}. If "
        "they know these are its own credentials, or it's a new address for the same NAS, see "
        f"{_CREDENTIALS_DOC}. If it's the wrong NAS, {_WRONG_NAS}"
    )


def _refuse_another_hosts_credentials(config: AppConfig, service: str, *, may_record: bool) -> None:
    """Stop before login unless this keyring entry is for ``connection.host`` (#536).

    It's found by instance ID alone, and a derived ID is only the host's first label, so
    `nas.home` and `nas.office` share an entry. A warning would come too late: the password would
    already be on its way. There's no variable to skip this, since the same `env` block could set
    it. A keyring that can't answer refuses too.
    """
    if config.connection is None:  # the config's own validation requires it
        raise RuntimeError("Config missing connection settings")
    instance = config.instance_id or "default"
    try:
        matches, recorded = _entry_is_for_this_host(config, service, may_record=may_record)
    except Exception as e:  # backends raise their own types
        logger.debug("Keyring error reading the host: %s", e, exc_info=True)
        raise CredentialHostError(
            f"Couldn't check which host the credentials in keyring entry {instance!r} are for, "
            f"so they aren't being sent to {config.connection.host!r}.",
            suggestion=(
                "Check that the OS keyring is unlocked and try again, or set the credentials in "
                "SYNOLOGY_USERNAME and SYNOLOGY_PASSWORD."
            ),
        ) from None
    if matches:
        return
    # Not naming the recorded host in the text: an `env` block's author would learn the real NAS's
    # name from the reply. `check` adds it. The first-use conditions and the command that records
    # are in the docs and `check`'s output, not here: see `_advice_for_the_model`.
    host = config.connection.host
    reason = "are for another host" if recorded else "don't say which host they're for"
    error = CredentialHostError(
        f"The credentials in keyring entry {instance!r} {reason}, so they aren't being sent to "
        f"{host!r}.",
        suggestion=_advice_for_the_model(config, host),
    )
    error.recorded_host = recorded
    error.terminal_suggestion = f"{how_to_record(config)}. If it's the wrong NAS, {_WRONG_NAS}"
    raise error


def resolve_credentials(config: AppConfig) -> tuple[str, str, str | None]:
    """Resolve credentials from the storage hierarchy.

    Order: env vars -> config file -> keyring.
    Explicit sources (env, config) override the implicit default (keyring). A username or password
    from the keyring must be for this config's host, or this raises; a device token that isn't
    is left out (#536).
    Returns: (username, password, device_id or None)
    """
    import os
    import sys
    from pathlib import Path

    username: str | None = None
    password: str | None = None
    device_id: str | None = None

    # 1. Environment variables (highest priority — explicit override)
    # _present_or_none normalizes empty / whitespace-only to None so they
    # fall through to the next strategy instead of becoming bogus creds.
    username = _present_or_none(os.environ.get("SYNOLOGY_USERNAME"))
    if username:
        logger.debug("Username from env var SYNOLOGY_USERNAME: %s", username)
    password = _present_or_none(os.environ.get("SYNOLOGY_PASSWORD"))
    if password:
        logger.debug("Password from env var SYNOLOGY_PASSWORD")
    device_id = _present_or_none(os.environ.get("SYNOLOGY_DEVICE_ID"))
    if device_id:
        logger.debug("Device ID from env var SYNOLOGY_DEVICE_ID")

    # 2. Config file (explicit, if present)
    if not username and (cfg_user := _present_or_none(config.auth.username)):
        username = cfg_user
        logger.debug("Username from config file: %s", username)
    if not password and (cfg_pass := _present_or_none(config.auth.password)):
        password = cfg_pass
        logger.debug("Password from config file (plaintext)")
    if not device_id and (cfg_dev := _present_or_none(config.auth.device_id)):
        device_id = cfg_dev
        logger.debug("Device ID from config file")

    # 3. OS keyring (implicit default — set by 'mcp-synology setup')
    # Check keyring if we're missing username, password, OR device_id
    if not username or not password or not device_id:
        # On Linux, ensure D-Bus session address is available for keyring access.
        # When launched by Claude Desktop, the env var may not be inherited.
        # The keyring library reads DBUS_SESSION_BUS_ADDRESS from os.environ.
        if sys.platform == "linux" and not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            uid = os.getuid()
            socket_path = Path(f"/run/user/{uid}/bus")
            if socket_path.exists():
                dbus_addr = f"unix:path=/run/user/{uid}/bus"
                os.environ["DBUS_SESSION_BUS_ADDRESS"] = dbus_addr
                logger.debug("Set DBUS_SESSION_BUS_ADDRESS=%s for keyring access", dbus_addr)
            else:
                # INFO not DEBUG — without D-Bus the keyring path is dead
                # for this session; the operator needs to know how to
                # recover. Closes #38 (the operator-actionable hint).
                logger.info(
                    "D-Bus socket not found at %s; OS keyring is unavailable. "
                    f"Run {_setup_command(config)} from a real desktop session "
                    "(or under 'dbus-run-session'), or set credentials via the "
                    "SYNOLOGY_USERNAME / SYNOLOGY_PASSWORD / SYNOLOGY_DEVICE_ID "
                    "env vars to bypass the keyring entirely.",
                    socket_path,
                )

        try:
            service = f"mcp-synology/{config.instance_id or 'default'}"
            logger.debug("Trying keyring service: %s", service)
            kr_user = _present_or_none(kr.get_password(service, "username"))
            kr_pass = _present_or_none(kr.get_password(service, "password"))
            kr_device = _present_or_none(kr.get_password(service, "device_id"))
            uses_password = bool(kr_pass and not password)
            # A username with no password anywhere can't log in, so nothing's sent, and the
            # error below ("No credentials found") is the true one.
            if uses_password or (kr_user and not username and password):
                # Only its password settles whose entry it is (pre-QA on #559).
                _refuse_another_hosts_credentials(config, service, may_record=uses_password)
            elif (
                kr_device
                and username
                and password
                and not _device_token_may_be_sent(config, service)
            ):
                kr_device = None
            if kr_user and not username:
                username = kr_user
                logger.debug("Username from keyring: %s", username)
            if kr_pass and not password:
                password = kr_pass
                logger.debug("Password from keyring")
            if kr_device and not device_id:
                device_id = kr_device
                logger.debug("Device ID from keyring")
        except AuthenticationError:
            raise  # the refusal above
        except KeyringError as e:
            # Typed keyring failure (NoKeyringError, InitError,
            # KeyringLocked, PasswordSetError, etc.). Log at DEBUG with
            # `exc_info` so `mcp-synology check -v` surfaces the actual
            # cause (locked keychain on macOS is operator-actionable;
            # NoKeyringError on a headless host signals a config issue).
            # Closes #38 — previously caught by a bare `except Exception`
            # with a flat "Keyring not available." line that hid all of
            # these. Falls through to the no-credentials check below;
            # the resolver chain still works without keyring.
            logger.debug("Keyring access failed: %s", e, exc_info=True)
        except OSError as e:
            # D-Bus socket present but unreachable, permission errors on
            # the OS keychain backend, etc. Same DEBUG-with-exc_info
            # treatment as the typed-error case above. The D-Bus pre-check above
            # already emitted an INFO hint if the D-Bus
            # socket was missing entirely; this branch covers the
            # reach-but-fail variant.
            logger.debug("Keyring OS-level error: %s", e, exc_info=True)
        except Exception as e:
            # Backends raise their own types (pre-QA on #559: a traceback from `check`).
            logger.debug("Keyring error: %s", e, exc_info=True)

    if not username or not password:
        raise AuthenticationError(
            "No credentials found.",
            suggestion=(
                "Set the SYNOLOGY_USERNAME and SYNOLOGY_PASSWORD environment variables: this host "
                "has no OS keyring."
                if no_keyring_here()
                else f"Run {_setup_command(config)} to store credentials in the OS keyring, "
                "or set SYNOLOGY_USERNAME and SYNOLOGY_PASSWORD environment variables."
            ),
        )

    logger.debug(
        "Credentials resolved: user=%s, has_password=yes, has_device_id=%s",
        username,
        "yes" if device_id else "no",
    )
    return username, password, device_id


KEYRING = "the keyring"


def credential_sources(config: AppConfig, *, cli: bool = False) -> dict[str, str | None]:
    """Where each credential comes from, in `resolve_credentials`' order: an environment
    variable, a config-file key, the keyring (``KEYRING``), or None for a device token
    that isn't set anywhere but the keyring. `setup` writes only the keyring, so a
    credential set elsewhere must be changed where it is (#447, QA rounds 1-2 on #461)."""
    import os

    sources: dict[str, str | None] = {}
    for key, var in (
        ("username", "SYNOLOGY_USERNAME"),
        ("password", "SYNOLOGY_PASSWORD"),
        ("device_id", "SYNOLOGY_DEVICE_ID"),
    ):
        if _present_or_none(os.environ.get(var)):
            # `check` reads the shell's environment, the server its MCP client's config.
            sources[key] = (
                f"{var} in this shell's environment"
                if cli
                else f"{var} in the server's environment (the MCP client's config)"
            )
        elif _present_or_none(getattr(config.auth, key)):
            sources[key] = f"auth.{key} in the config file"
        else:
            sources[key] = KEYRING if key != "device_id" else None
    return sources


_SETUP_PICKED_UP = "the running server picks it up"
_RECORD_HOST = "'mcp-synology check --config <full path to your config file>'"
_TWO_FA = (403, 404, 406)
# `setup` never prints the token (maintainer, 2026-11-05), so it comes from a machine that
# can store one. Naming `setup` alone sent a host with no keyring in a circle (#481).
NO_KEYRING_2FA_ADVICE = (
    "This host has no OS keyring, so a 2FA device token can't be stored here. On a machine "
    f"with one, copy this config there and run {SETUP_COMMAND}, read the token with "
    "'uvx keyring get mcp-synology/<instance_id> device_id', and set it in "
    "SYNOLOGY_DEVICE_ID here"
)


def no_keyring_here() -> bool:
    """True when `keyring` found no backend, as on Docker. A locked or failing keyring is
    still one that `setup` can use once it works."""
    try:
        import keyring
        from keyring.backends.fail import Keyring

        return isinstance(keyring.get_keyring(), Keyring)
    except Exception as e:  # backends raise their own types
        logger.debug("Keyring backend check failed: %s", e, exc_info=True)
        return False


def refusal_advice(
    code: int | None, first: str, sources: dict[str, str | None], *, cli: bool = False
) -> str:
    """What fixes a refused login, by DSM's code and where the credential it concerns
    comes from (QA round 2 on #461). ``first`` is DSM's own suggestion for the code.
    ``cli`` leaves out what only a running server does (picking up the keyring, a restart):
    `check` runs once and reads the credentials anew."""
    picked = "" if cli else f"; {_SETUP_PICKED_UP}"
    restart = "" if cli else " and restart the server"
    if code == 400:
        password, username = sources.get("password"), sources.get("username")
        if password == KEYRING:
            advice = f"Run {SETUP_COMMAND} to store the right password{picked}"
        else:
            advice = (
                f"The password comes from {password}: correct it there{restart} "
                f"({SETUP_COMMAND} writes only the keyring)"
            )
        if username != KEYRING:
            advice += f". If the username is wrong, it comes from {username}: correct it there"
            if password == KEYRING:
                advice += restart
        return advice
    if code in _TWO_FA:
        device = sources.get("device_id")
        if no_keyring_here():
            advice = NO_KEYRING_2FA_ADVICE + restart
            if device is not None:
                advice = f"The device token comes from {device}. {advice}"
        elif device is None:
            advice = f"Run {SETUP_COMMAND} to sign in with a new 2FA code{picked}"
        else:
            advice = (
                f"The device token comes from {device}, which overrides the keyring: remove "
                f"it there, then run {SETUP_COMMAND} to sign in with a new 2FA code"
                + ("" if cli else ", and restart the server")
            )
        return f"{first}. Then: {advice[0].lower()}{advice[1:]}" if code == 406 else advice
    if code == 401:
        if cli:
            return first or "The account is disabled in DSM"
        return (
            f"{first}. Once it's enabled again, restart the server"
            if first
            else ("Once the account is enabled again, restart the server")
        )
    if cli:
        return first or "Check the credentials"
    explicit = sorted({v for k, v in sources.items() if v and v != KEYRING})
    where = (
        "mcp-synology tries again once the credentials change ("
        f"{SETUP_COMMAND} stores "
        f"new ones in the keyring; {_SETUP_PICKED_UP})"
    )
    if explicit:
        verb, them = ("overrides", "it") if len(explicit) == 1 else ("override", "those")
        where += f"; {'; '.join(explicit)} {verb} the keyring: change {them} there and "
        where += "restart the server"
    return f"{first}. {where}" if first else where


def credentials_fingerprint(username: str, password: str, device_id: str | None) -> str:
    """A digest of the credentials, so a refused login can be recognized without
    keeping a second copy of the password."""
    joined = "\0".join((username, password, device_id or ""))
    return hashlib.sha256(joined.encode()).hexdigest()


class LoginRefusals:
    """Remembers the credentials DSM refused, so they aren't sent again (#447).

    DSM's Auto Block (on by default: 10 failed logins within 5 minutes) blocks the
    computer's IP address, for the user's browser and every other client too. A refused
    login retried on every tool call, or on every session error, would trip it. The
    refusal holds until the credentials change (``mcp-synology setup`` writes new ones
    to the keyring) or the server restarts. A transport error or any other failure is
    never remembered: it says nothing about the credentials.
    """

    def __init__(self) -> None:
        self._refused: tuple[str, AuthenticationError] | None = None

    @property
    def any(self) -> bool:
        return self._refused is not None

    def check(self, fingerprint: str) -> None:
        """Raise the remembered refusal if these are the credentials DSM refused."""
        if self._refused is not None and self._refused[0] == fingerprint:
            error = self._refused[1]
            logger.debug("Not sending a login DSM already refused (error %s)", error.code)
            raise RememberedRefusalError(str(error), code=error.code, suggestion=error.suggestion)

    def refuse(
        self,
        fingerprint: str,
        error: AuthenticationError,
        sources: dict[str, str | None] | None = None,
    ) -> AuthenticationError:
        """Remember DSM's refusal; return the error to raise, which says what happens next.

        ``sources`` says where each credential comes from (``credential_sources``), so
        the suggestion names the fix for the credential DSM refused: `setup` for one in
        the keyring, the environment or config file for one set there.
        """
        reason = str(error).rstrip(".")
        first = (error.suggestion or "").rstrip(".")
        if sources is None:
            sources = {"username": KEYRING, "password": KEYRING, "device_id": None}
        where = refusal_advice(error.code, first, sources)
        refusal = LoginRefusedError(
            f"DSM refused the login: {reason}. mcp-synology won't send these credentials "
            "again until they change or the server restarts, because repeated failed "
            "logins can trip DSM's Auto Block, which blocks this computer's IP address "
            "from the NAS.",
            code=error.code,
            suggestion=(
                f"{where}. After the fix, {CHECK_COMMAND} tests the login once (each "
                "failed check counts toward Auto Block)."
            ),
        )
        # Keep a fresh copy: the raised one carries the login's frames (and its password)
        # in its traceback once raised.
        self._refused = (
            fingerprint,
            AuthenticationError(str(refusal), code=refusal.code, suggestion=refusal.suggestion),
        )
        return refusal

    def clear(self) -> None:
        self._refused = None


class AuthManager:
    """Manages DSM authentication and session lifecycle."""

    def __init__(
        self, config: AppConfig, client: DsmClient, refusals: LoginRefusals | None = None
    ) -> None:
        self._config = config
        self._client = client
        # Shared with the server's client manager, so a refusal outlives this
        # AuthManager when the first login fails (#447).
        self._refusals = refusals if refusals is not None else LoginRefusals()
        self._lock = asyncio.Lock()
        self._session_name = self._build_session_name()
        # Subscriber list — fired after a successful re-auth so module-owned
        # caches (e.g. filestation's `recycle_status`) can invalidate any
        # state that may have changed on the NAS between sessions. See #37.
        self._on_reauth_callbacks: list[Callable[[], None]] = []
        # Set when a login recorded the keyring entry's host, for `check` to say so.
        self.recorded_host: str | None = None

        # Register ourselves as the re-auth callback
        self._client.set_re_auth_callback(self._re_authenticate)
        logger.debug("AuthManager initialized, session name: %s", self._session_name)

    def add_on_reauth_callback(self, cb: Callable[[], None]) -> None:
        """Register a callback to fire after each successful session re-auth.

        Modules that cache NAS state which can drift mid-session (the canonical
        case is filestation's recycle-bin probe cache) subscribe here so they
        can clear the cache and let the next operation re-probe against fresh
        DSM state. Callbacks must be cheap and synchronous; an exception
        from one callback is logged and does not prevent the rest from firing.
        """
        self._on_reauth_callbacks.append(cb)

    def _build_session_name(self, session_key: str | None = None) -> str:
        """Build a DSM session name.

        ``session_key=None`` (current default) produces
        ``MCPSynology_{instance_id}_{uuid8}`` — one shared session per
        ``AuthManager`` instance, which is the only mode exercised today
        under the stdio transport.

        ``session_key=<str>`` produces ``MCPSynology_{instance_id}_{session_key}``
        for the planned per-client session model under Streamable HTTP. The
        helper accepts a key now so a future per-client session pool can derive
        names without changing this method's contract; no production code path
        invokes it with a non-None key today. See ADR 0001.
        """
        instance_id = self._config.instance_id or "default"
        suffix = session_key if session_key is not None else uuid.uuid4().hex[:8]
        return f"MCPSynology_{instance_id}_{suffix}"

    def _resolve_credentials(self) -> tuple[str, str, str | None]:
        """Resolve credentials from the storage hierarchy (``resolve_credentials``)."""
        return resolve_credentials(self._config)

    def _record_host(self) -> None:
        """Record the host of a keyring entry that had none, now DSM has accepted its password
        (#536). A failure doesn't undo the login: the next one tries again. Another login may have
        recorded a host since this one read the entry, and that one stays."""
        host, self._config._host_to_record = self._config._host_to_record, None
        if host is None:
            return
        instance = self._config.instance_id or "default"
        service = f"mcp-synology/{instance}"
        try:
            if _recorded_host(service) is not None:
                return
            kr.set_password(service, "host", host)
            recorded = _recorded_host(service)  # `check` tells people to rely on what it says
        except Exception as e:  # backends raise their own types
            logger.debug("Keyring error recording the host: %s", e, exc_info=True)
            recorded = None
        if recorded != host:
            logger.warning(
                "Couldn't record this host in keyring entry %r. Check that the OS keyring can be "
                "written to; the next login tries again.",
                instance,
            )
            return
        self.recorded_host = host
        logger.info("Keyring entry %r is now recorded for this config's host", instance)

    async def login(self) -> str:
        """Authenticate with the NAS and return a session ID.

        Uses the strategy chain to handle 2FA automatically. Credentials DSM already
        refused aren't sent again (#447): the refusal is raised without contacting DSM.
        """
        username, password, device_id = self._resolve_credentials()
        fingerprint = credentials_fingerprint(username, password, device_id)
        self._refusals.check(fingerprint)

        params: dict[str, Any] = {
            "account": username,
            "passwd": password,
            "format": "sid",
        }

        # Path B: 2FA with remembered device
        if device_id:
            logger.debug("Login path: 2FA with device token")
            params["device_name"] = "MCPSynology"
            params["device_id"] = device_id
        else:
            logger.debug("Login path: simple (no device token)")

        try:
            data = await self._send_login(params, device_id)
        except AuthenticationError as e:
            if e.code is None:
                raise
            raise self._refusals.refuse(fingerprint, e, credential_sources(self._config)) from e

        sid: str | None = data.get("sid")
        if not sid:
            raise AuthenticationError("Login succeeded but no session ID returned.")

        self._record_host()
        self._refusals.clear()
        self._client.sid = sid
        logger.info("Authenticated as '%s' (session: %s)", username, self._session_name)
        return sid

    async def _send_login(self, params: dict[str, Any], device_id: str | None) -> dict[str, Any]:
        """Send the login; a 403 becomes the 2FA error that says what to do."""
        try:
            # A POST keeps the password out of the URL (#235).
            return await self._client.request(
                "SYNO.API.Auth", "login", version=6, params=params, post_form=True
            )
        except SynologyError as e:
            if e.code == _ERROR_2FA_REQUIRED:
                if device_id:
                    # DSM no longer trusts the stored token, e.g. the trusted
                    # device was removed in DSM's security settings.
                    logger.debug("Login failed: DSM rejected the stored 2FA device token")
                    # The fix depends on where the token comes from, which the
                    # refusal's suggestion says (QA round 2 on #461).
                    message = (
                        "2FA is required and DSM rejected the device token "
                        "(it may have been removed or invalidated on the NAS)."
                    )
                else:
                    logger.debug("Login failed: 2FA required but no device token available")
                    # The refusal's suggestion says to run setup (QA round 3 on #461).
                    message = "2FA is required but no device token is available."
                raise AuthenticationError(
                    message,
                    code=_ERROR_2FA_REQUIRED,
                    suggestion=f"Run {_setup_command(self._config)}.",
                ) from e
            raise

    async def logout(self, *, raise_errors: bool = False) -> None:
        """Log out the current session.

        A DSM error is logged at DEBUG and swallowed (the session may already be
        expired), unless ``raise_errors`` is set: shutdown uses that to warn (#402). An
        expired session (106/107/119) is always logged at DEBUG: after a failed re-login
        it stays in place (#359), and shutdown sends it.
        """
        if not self._client.sid:
            return

        logger.debug("Logging out session '%s'", self._session_name)
        try:
            await self._client.request(
                "SYNO.API.Auth",
                "logout",
                version=6,
                params={"session": self._session_name},
            )
            logger.debug("Logout successful")
        except SessionExpiredError:
            # Already closed on DSM's side: after a failed re-login the expired session
            # stays in place (#359), and shutdown's logout sends it (QA round 2 on #461).
            logger.debug("Logout: the session had already expired.")
        except SynologyError:
            if raise_errors:
                raise
            logger.debug("Logout failed (session may already be expired).")
        finally:
            self._client.sid = None

    async def get_session(self) -> str:
        """Get a valid session ID, logging in if needed."""
        if self._client.sid:
            return self._client.sid
        logger.debug("No active session, logging in")
        return await self.login()

    async def _re_authenticate(self) -> None:
        """Re-authenticate transparently (called by DsmClient on session errors).

        DsmClient runs one re-login at a time and skips it when another request's
        re-login already replaced the session (#359). The expired session stays in
        place until the new one exists: cleared first, every request sent during the
        login would go without a session and queue its own re-login. A refused login
        isn't sent again (#447).

        Fires every registered ``on_reauth`` callback after the new session is
        live so module-owned caches (filestation's recycle-bin probe, etc.)
        can drop entries that may have drifted while the session was down.
        """
        async with self._lock:
            # DEBUG: a remembered refusal answers without a login; a real one logs
            # "Authenticated as" at INFO, and any other failure is logged by the client.
            logger.debug("Re-authenticating session '%s'.", self._session_name)
            await self.login()
            # NOTE: callbacks fire INSIDE `self._lock`. Per `add_on_reauth_callback`'s
            # contract they must be cheap and synchronous — anything heavy here would
            # block every other coroutine waiting to re-auth. Today's only subscriber
            # is `recycle_status.clear` (O(n_shares)); keep new callbacks similarly
            # trivial or move the work to a deferred task.
            for cb in self._on_reauth_callbacks:
                try:
                    cb()
                except Exception as e:  # noqa: BLE001 — best-effort dispatch
                    logger.warning("on_reauth callback %r raised: %s", cb, e)
