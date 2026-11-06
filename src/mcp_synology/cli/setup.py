"""Setup command: interactive config creation and credential flow."""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import click
import yaml

from mcp_synology.cli.check import exit_on_connection_failure
from mcp_synology.cli.logging_ import _configure_logging, _init_early_logging
from mcp_synology.cli.startup import escape_what_stdout_cant_encode, load_or_exit

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp_synology.core.config import AppConfig

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path.home() / ".config" / "mcp-synology"


@click.command()
@click.option("-c", "--config", type=click.Path(), help="Path to config file")
@click.option("-l", "--list", "list_configs", is_flag=True, help="List existing configurations")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
def setup(config: str | None, list_configs: bool, verbose: bool) -> None:
    """Interactive credential setup and 2FA bootstrap."""
    escape_what_stdout_cant_encode()
    _init_early_logging(verbose=verbose)

    if list_configs:
        _list_configurations()
        return

    # Every path below asks something. A pipe can answer, but with stdin closed, as under cron,
    # the first question was a traceback (pre-QA on #559).
    if sys.stdin is None:
        click.echo(
            click.style(
                "Error: setup asks questions, and stdin is closed. Run it at a terminal.", fg="red"
            ),
            err=True,
        )
        sys.exit(1)

    # If --config is given, use the existing config-file flow
    if config is not None:  # an empty --config is an error there, not "no flag"
        _setup_with_config(config, verbose)
        return

    # Try to load an existing config via discovery
    try:
        app_config = load_or_exit(None, configure_logging=False, missing_raises=True)
    except FileNotFoundError as e:
        # A file named by MCP_SYNOLOGY_CONFIG must exist: creating a config elsewhere
        # would ignore the variable without a word (#499).
        if os.environ.get("MCP_SYNOLOGY_CONFIG") is not None:
            click.echo(click.style(f"Error: {e}", fg="red"), err=True)
            sys.exit(1)
        # No config file found — enter interactive creation mode. One that's there and isn't
        # loaded is named, since "no file found" would be false (#552). Imported here: at the top,
        # every command would load pydantic, `--version` included.
        from mcp_synology.core.config import WorkingFolderConfigNotLoadedError

        _setup_interactive(
            verbose, str(e) if isinstance(e, WorkingFolderConfigNotLoadedError) else None
        )
        return

    # Config file exists — do credential setup against it
    _setup_credential_flow(app_config, verbose)


def _shown_name(name: str | Path) -> str:
    """A name from the disk, quoted if it could forge a line or move the terminal (#541)."""
    text = str(name)
    return text if text.isprintable() else repr(text)


def _list_configurations() -> None:
    """List existing configuration files in the config directory."""
    from mcp_synology.core.config import (
        alias_problem,
        format_yaml_error,
        host_problems,
        instance_id_problem,
        parse_config_text,
    )

    # By scandir: `Path.exists` swallows every OSError on Python 3.14, and `glob` on all of them,
    # which would read as no configurations (QA round 2 on #541).
    try:
        with os.scandir(_CONFIG_DIR) as entries:
            yaml_files = sorted(
                _CONFIG_DIR / entry.name
                for entry in entries
                if fnmatch.fnmatch(entry.name, "*.yaml")
            )
    except (FileNotFoundError, NotADirectoryError):
        yaml_files = []
    except OSError as e:
        click.echo(f"Config directory can't be read: {_shown_name(_CONFIG_DIR)}: {e.strerror}")
        return
    if not yaml_files:
        click.echo("No configurations found.")
        click.echo(f"Config directory: {_shown_name(_CONFIG_DIR)}")
        return

    click.echo(f"Configurations in {_shown_name(_CONFIG_DIR)}:\n")
    for f in yaml_files:
        shown = _shown_name(f.name)
        try:
            # A named pipe would wait for a writer, and the later files would go unlisted.
            if not f.is_file():
                click.echo(f"  {shown}  (could not parse)")
                continue
            raw = parse_config_text(f.read_text(encoding="utf-8")) or {}
            # An empty `connection:` is no connection settings (#492).
            host = (raw.get("connection") or {}).get("host")
            # Shown only when it's a bare host the server would accept: one written as a
            # URL can hold a password (#476, #471).
            if host is None:
                host = "?"
            elif not isinstance(host, str) or host_problems(host):
                host = "(not shown: not a bare host name or IP address)"
            # Only text the server would accept: `alias: {password: …}` printed the
            # mapping (#476).
            alias = raw.get("alias")
            instance_id = raw.get("instance_id")
            if isinstance(alias, str) and not alias_problem(alias):
                label = alias
            elif isinstance(instance_id, str) and not instance_id_problem(instance_id):
                label = instance_id.lower()  # as the server uses it
            else:
                label = _shown_name(f.stem)
            click.echo(f"  {shown}")
            click.echo(f"    Name: {label}")
            click.echo(f"    Host: {host}")
            click.echo()
        except yaml.YAMLError as e:
            # The reason `check` gives; format_yaml_error never quotes the file (QA round 2
            # on #532).
            click.echo(f"  {shown}  (could not parse)")
            for line in format_yaml_error(e).splitlines():
                click.echo(f"    {line}")
        except (OSError, ValueError, AttributeError):
            click.echo(f"  {shown}  (could not parse)")


def _setup_interactive(verbose: bool, not_loaded: str | None = None) -> None:
    """Interactive setup when no config file is loaded. ``not_loaded`` says why one that's there
    isn't."""
    if not_loaded:
        click.echo(f"{not_loaded}\n\nTo make a new config at the home location instead:\n")
    else:
        click.echo("No configuration file found. Let's create one.\n")

    from mcp_synology.core.config import (
        alias_problem,
        host_message,
        host_problems,
        instance_id_problem,
    )

    # Prompt for connection details. The host is checked here, by the config's own rule,
    # so a URL, port or user name gets advice and another try instead of a failure after
    # every other prompt (#471).
    while True:
        host = click.prompt("NAS host (IP or hostname)")
        problems = host_problems(
            host,
            port_setting="type the port at the DSM port question",
            https_setting='answer yes to "Use HTTPS?"',
            ipv6_form="[fd00::1]",
            credentials_setting="are asked for after these questions",
        )
        if not problems:
            break
        click.echo(
            click.style(f"  The host {host_message(problems, hidden_note=False)}", fg="red"),
            err=True,
        )
    # HTTPS by default (#193): DSM serves it on 5001 out of the box.
    use_https = click.confirm("Use HTTPS?", default=True)
    if not use_https:
        click.echo(
            click.style(
                "  Plain HTTP: your DSM password and session cross the network unencrypted. "
                "Fine on a network you trust. To switch later: https://github.com/cmeans/mcp-synology/blob/main/docs/https.md",
                fg="yellow",
            )
        )
    # A NAS on a custom port couldn't be set up interactively (#504 item 1): the host
    # takes no port, so it's asked for here, defaulting to DSM's.
    default_port = 5001 if use_https else 5000
    port = click.prompt("DSM port", default=default_port, type=click.IntRange(1, 65535))
    permission = click.prompt(
        "File Station permission",
        type=click.Choice(["read", "write"]),
        default="read",
    )

    # Derive instance_id for file naming
    from mcp_synology.core.config import _derive_instance_id

    instance_id = _derive_instance_id(host)
    # An IPv6 address, or a name with characters outside a-z, 0-9 and hyphens, gives no
    # usable ID: ask for one.
    if instance_id_problem(instance_id):
        click.echo(
            "  This host gives no usable instance ID (its first part has characters other "
            "than letters a-z, digits and hyphens)."
        )
        while True:
            instance_id = click.prompt(
                "Instance ID (names this config; lowercase letters, digits and hyphens)"
            ).lower()
            if not (problem := instance_id_problem(instance_id)):
                break
            # The file's "leave it out to derive it" advice doesn't apply here: this host
            # gives none (pre-QA r3).
            click.echo(f"  That instance ID {'is empty' if not instance_id.strip() else problem}.")

    # Prompt for alias
    while True:
        alias_input = click.prompt(
            "Alias (friendly name, optional)", default="", show_default=False
        )
        alias: str | None = alias_input.strip() or None
        if alias is None or not (problem := alias_problem(alias)):
            break
        click.echo(f"  That alias {problem}")

    # Build config dict
    conn: dict[str, Any] = {"host": host}
    if port != default_port:
        conn["port"] = port
    if use_https:
        conn["https"] = True
        click.echo(
            "  DSM's own certificate is self-signed, so verification fails unless you've "
            "installed a trusted one.\n  Without verification the connection is still "
            "encrypted, but not protected against a machine impersonating the NAS."
        )
        conn["verify_ssl"] = click.confirm("Verify SSL certificate?", default=False)

    config_dict: dict[str, Any] = {
        "schema_version": 1,
        "instance_id": instance_id,
        "connection": conn,
        "modules": {
            "filestation": {
                "enabled": True,
                "permission": permission,
            }
        },
    }
    if alias:
        config_dict["alias"] = alias

    # Validate before writing
    from pydantic import ValidationError

    from mcp_synology.core.config import AppConfig, format_validation_error, warn_if_proxied

    try:
        app_config = AppConfig(**config_dict)
    except ValidationError as e:
        # Screened like every config error: pydantic's own text quotes the input (#471).
        click.echo(click.style(f"Error: {format_validation_error(e)}", fg="red"), err=True)
        sys.exit(1)

    # This path doesn't go through load_config. Before the password is typed (#558).
    if app_config.connection is None:  # built from `conn` just above
        raise RuntimeError("Config missing connection settings")
    warn_if_proxied(app_config.connection)

    # Nothing is written until these are answered (#536).
    service = f"mcp-synology/{instance_id}"
    if not _may_replace_entry(service, host, app_config.connection.port, instance_id):
        return
    config_path = _CONFIG_DIR / f"{instance_id}.yaml"
    if config_path.exists() and not click.confirm(
        f"\n{config_path} already exists. Overwrite?", default=False
    ):
        click.echo("Aborted.")
        return

    # Prompt for credentials
    click.echo()
    username = click.prompt("DSM username").strip()
    password = click.prompt("DSM password", hide_input=True)

    # Connect, validate login, fetch hostname. The keyring is written only once DSM accepts the
    # login, so a mistyped password can't replace a working one (#454). Without a keyring the
    # file is still what's needed: stopping here left the user with variables and no config (#481).
    from mcp_synology.core.auth import no_keyring_here

    connection_port = app_config.connection.port

    def store(device_id: str | None) -> bool:
        return _store_keyring(service, username, password, host, connection_port, device_id)

    nas_hostname = None
    if no_keyring_here():
        keyring_ok = store(None)
    else:
        click.echo("\nConnecting to NAS...")
        result = asyncio.run(_connect_and_login(app_config, username, password, store, verbose))
        if not result["success"]:
            _exit_nothing_changed()
        keyring_ok = result["keyring_ok"]

        # Suggest hostname as alias if we fetched one and user didn't set an alias
        nas_hostname = result.get("hostname")
    if (
        isinstance(nas_hostname, str)
        and nas_hostname
        and not alias
        and not alias_problem(nas_hostname)
    ):
        use_hostname = click.confirm(
            f'NAS reports hostname "{nas_hostname}". Use as alias?', default=True
        )
        if use_hostname:
            alias = nas_hostname
            config_dict["alias"] = alias
            # Re-validate
            app_config = AppConfig(**config_dict)

    # Write config file atomically (closes #70)
    from mcp_synology.core.fs import atomic_write_text

    raw_yaml = yaml.dump(config_dict, default_flow_style=False, sort_keys=False)
    header = "# Generated by mcp-synology setup\n"
    try:
        atomic_write_text(config_path, header + raw_yaml)
    except OSError as e:
        # This path can now be reached with no login first, as on Docker. An unwritable home
        # stops earlier, at the state lock (#455).
        click.echo(
            click.style(
                f"Couldn't write {_shown_name(config_path)}: {e.strerror}. Fix the folder and run "
                "this again.",
                fg="red",
            ),
            err=True,
        )
        sys.exit(1)
    click.echo(f"\nConfig written to {config_path}")
    # Discovery reads config.yaml, not this file, so a bare `check` wouldn't find it.
    from mcp_synology.core.config import quoted_path

    lead = "To use it, name it" if keyring_ok else "With those variables set, test it"
    click.echo(f"{lead}: mcp-synology check --config {quoted_path(config_path)}")

    _echo_two_fa_advice()

    # Emit Claude Desktop JSON snippet
    _emit_claude_desktop_snippet(
        app_config,
        config_path,
        None
        if keyring_ok
        else {"SYNOLOGY_USERNAME": username, "SYNOLOGY_PASSWORD": "<your password>"},
    )


def _setup_with_config(config_path_str: str, verbose: bool) -> None:
    """Setup using an explicit config file path."""
    app_config = load_or_exit(config_path_str, configure_logging=False)

    _setup_credential_flow(app_config, verbose)


def _setup_credential_flow(app_config: Any, verbose: bool) -> None:
    """Credential setup flow for an existing config."""
    from mcp_synology.core.config import AppConfig

    if not isinstance(app_config, AppConfig):
        raise RuntimeError("Expected AppConfig instance")

    if not verbose:
        _configure_logging(app_config.logging.level, app_config.logging.file)

    if app_config.connection is None:
        raise RuntimeError("Config missing connection settings")
    click.echo(f"Setting up credentials for {app_config.display_name}")
    click.echo(f"  Host: {app_config.connection.host}:{app_config.connection.port}")
    click.echo(f"  Instance: {app_config.instance_id}")
    click.echo()

    service = f"mcp-synology/{app_config.instance_id}"
    instance = str(app_config.instance_id)
    connection = app_config.connection
    if not _may_replace_entry(service, connection.host, connection.port, instance, app_config):
        return

    username = click.prompt("DSM username").strip()
    password = click.prompt("DSM password", hide_input=True)

    # The keyring is written only once DSM accepts the login (#454).
    from mcp_synology.core.auth import no_keyring_here

    def store(device_id: str | None) -> bool:
        return _store_keyring(
            service, username, password, connection.host, connection.port, device_id
        )

    if no_keyring_here():
        keyring_ok = store(None)
    else:
        click.echo("\nAttempting login...")
        result = asyncio.run(_setup_login(app_config, username, password, store))
        if not result["success"]:
            _exit_nothing_changed()
        keyring_ok = result["keyring_ok"]
    if not keyring_ok:
        from mcp_synology.core.config import quoted_path
        from mcp_synology.core.errors import CHECK_NO_FILE

        # On variables alone (the Docker path) there's no file, and the bare command finds them.
        path = app_config._source_path
        if path:
            click.echo(
                "With those variables set, test it: mcp-synology check --config "
                f"{quoted_path(path)}"
            )
        else:
            click.echo(
                "With those variables and the server's SYNOLOGY_HOST, SYNOLOGY_HTTPS and "
                f"SYNOLOGY_PORT set, test it: {CHECK_NO_FILE}"
            )
        _echo_two_fa_advice()


def _may_replace_entry(
    service: str, host: str, port: int | None, instance: str, config: AppConfig | None = None
) -> bool:
    """Ask before replacing a keyring entry that isn't this host's (#536): two NAS with the same
    first label derive the same instance ID, and the other's config would then send this one's
    password there. With no keyring at all there's nothing to replace, and writing says so. One
    that fails now may work when written to, and another host's credentials would go unasked, so
    that stops here (QA round 1 on #559). Any exception counts: backends raise their own types.
    ``config`` is the file being set up; interactive setup hasn't written one yet."""
    from mcp_synology.core.auth import check_route, own_instance_id_clause
    from mcp_synology.core.config import comparable_host, comparable_target

    try:
        import keyring
        from keyring.errors import NoKeyringError

        recorded = (keyring.get_password(service, "host") or "").strip() or None
        # A record on its own, left by removing the rest, holds no credentials to replace.
        stored = any(
            keyring.get_password(service, k) for k in ("username", "password", "device_id")
        )
    except (ImportError, NoKeyringError):
        return True
    except Exception as e:
        logger.debug("Keyring error reading entry %r: %s", instance, e, exc_info=True)
        click.echo(
            f"\nCouldn't read keyring entry {instance!r}, so nothing was changed. Check that the "
            "OS keyring is unlocked and try again, or set SYNOLOGY_USERNAME and SYNOLOGY_PASSWORD "
            "instead"
            + (
                "."
                if config is not None
                # No file exists yet, and the credentials alone don't say which NAS. The port and
                # HTTPS too, or the login goes to plain HTTP on 5000 (pre-QA).
                else ", with SYNOLOGY_HOST, SYNOLOGY_PORT and SYNOLOGY_HTTPS or a config file you "
                "write yourself."
            )
        )
        sys.exit(1)
    if not stored or recorded == comparable_target(host, port):
        return True
    same_host = comparable_host(host)
    # A record of this host with no port is from before ports were recorded (#562).
    unknown = recorded is None or recorded == same_host
    if recorded is None:
        whose = "with no host recorded"
    elif recorded == same_host:
        whose = "with no port recorded"
    elif recorded.startswith(f"{same_host}:"):
        whose = "for another port on this host"
    else:
        whose = "for another host"
    if click.confirm(
        f"\nKeyring entry {instance!r} already holds credentials {whose}. Replacing it removes any "
        "2FA device token, and stops any other config using that entry from logging in until "
        f"it's set up again. Replace it for {host}:{port}?",
        default=False,
    ):
        return True
    own_id = (
        f"{own_instance_id_clause(config)}."
        if config is not None
        else "give this NAS its own instance_id in a config file, then run "
        "'mcp-synology setup --config <that file>'."
    )
    # An entry with no record may well be this NAS's own, from before hosts were recorded.
    route = None if not unknown or config is None else check_route(config)
    if not unknown:
        click.echo(f"Aborted, nothing changed. To keep both, {own_id}")
    else:
        own = route or "run this again and answer yes"
        click.echo(
            f"Aborted, nothing changed. If these are this NAS's own credentials, {own}. "
            f"If they're another NAS's, {own_id}"
        )
    return False


def _echo_two_fa_advice() -> None:
    """After the file exists: the advice says to copy it (pre-QA)."""
    from mcp_synology.core.auth import no_keyring_2fa_advice, no_keyring_here

    if no_keyring_here():
        click.echo(f"\nIf this account uses 2FA: {no_keyring_2fa_advice(cli=True)}.")


def _variable_lines(username: str, *, windows: bool = os.name == "nt") -> str:
    """The credential variables as a shell sets them. `NAME=value` alone doesn't reach a command
    run after it, and PowerShell is what a Windows terminal opens."""
    from mcp_synology.core.config import quoted_text

    if windows:
        # PowerShell needs quotes around any text, and takes curly quotes as closing them.
        name = "'" + re.sub("(['\u2018\u2019\u201a\u201b])", r"\1\1", username) + "'"
        return f"  $env:SYNOLOGY_USERNAME = {name}\n  $env:SYNOLOGY_PASSWORD = '<your password>'"
    return (
        f"  export SYNOLOGY_USERNAME={quoted_text(username)}\n"
        "  export SYNOLOGY_PASSWORD='<your password>'"
    )


def _store_keyring(
    service: str,
    username: str,
    password: str,
    host: str,
    port: int | None,
    device_id: str | None = None,
) -> bool:
    """Store credentials and the host they're for in the OS keyring. True on success.

    Callers with a keyring run this only after DSM accepts the login (#454).

    When the host or port changes, the old secrets go first and the new record before the new
    password, so a stop partway leaves nothing to send to the wrong host. The old device token
    would otherwise be sent to this one. A delete that didn't take stops here, with the old record
    still there: the backends raise the same error for "no such password" and for a real failure,
    and a chain of backends deletes from only the first. Any exception counts: backends raise their
    own types.

    ``device_id`` is a token this login just got, and it replaces a stored one. Without one, a
    stored token stays for the same username. For another it goes either way: it's that account's
    trust, not this one's (#454). A new token that can't be stored ends setup: the server can't log
    in to that account without it, whatever else is set.
    """
    from mcp_synology.core.auth import no_keyring_here
    from mcp_synology.core.config import comparable_target

    removed: list[str] = []
    stored = False
    try:
        import keyring
        from keyring.errors import PasswordDeleteError

        recorded = comparable_target(host, port)
        if (keyring.get_password(service, "host") or "").strip() != recorded:
            for key in ("device_id", "password", "username"):
                held = keyring.get_password(service, key)
                with contextlib.suppress(PasswordDeleteError):  # there may be none
                    keyring.delete_password(service, key)
                if keyring.get_password(service, key):
                    raise PasswordDeleteError(f"{key} is still stored")
                if held:
                    removed.append(key)
            keyring.set_password(service, "host", recorded)
        if keyring.get_password(service, "username") != username:
            with contextlib.suppress(PasswordDeleteError):  # there may be none
                keyring.delete_password(service, "device_id")
            if keyring.get_password(service, "device_id"):
                raise PasswordDeleteError("device_id is still stored")
        keyring.set_password(service, "username", username)
        keyring.set_password(service, "password", password)
        stored = True
        if device_id:
            keyring.set_password(service, "device_id", device_id)
        click.echo("\nCredentials stored in OS keyring.")
        if device_id:
            click.echo(click.style("2FA bootstrap complete!", fg="green"))
            click.echo("Device token stored in keyring.")
        return True
    except Exception as e:
        logger.debug("Keyring error storing credentials: %s", e, exc_info=True)
        gone = f" Already removed from this entry: {', '.join(removed)}." if removed else ""
        lead = (
            "Keyring not available."
            if no_keyring_here()
            else "Couldn't store these in the keyring."
        )
        if device_id:
            start = (
                "The username and password are stored, but the new"
                if stored
                else f"{lead}{gone} The new"
            )
            click.echo(
                click.style(
                    f"\n{start} 2FA device token couldn't be stored, so the server can't log in "
                    "without a code. Fix the keyring and run this again.",
                    fg="red",
                ),
                err=True,
            )
            sys.exit(1)
        click.echo(
            f"\n{lead}{gone} Set these environment variables where the server runs instead: "
            "in the `env` block of your MCP client's config, or in your shell, with your "
            "password in place of the placeholder:\n" + _variable_lines(username)
        )
        return False


async def _attempt_login(
    client: Any,
    username: str,
    password: str,
) -> dict[str, Any]:
    """Attempt DSM login, handling 2FA if required. It writes nothing to the keyring.

    Returns dict with 'success' bool, and optionally 'sid' and, on 2FA success, 'device_id' for
    the caller to store with the rest (#454).
    """
    from mcp_synology.core.errors import SynologyError

    result: dict[str, Any] = {"success": False}

    try:
        data = await client.request(
            "SYNO.API.Auth",
            "login",
            version=6,
            params={"account": username, "passwd": password, "format": "sid"},
            post_form=True,  # keeps the password out of the URL (#235)
        )
    except SynologyError as e:
        if e.code == 403:
            # 2FA required
            click.echo("2FA is required. Enter the OTP code from your authenticator app.")
            otp_code = click.prompt("OTP code")
            try:
                data = await client.request(
                    "SYNO.API.Auth",
                    "login",
                    version=6,
                    params={
                        "account": username,
                        "passwd": password,
                        "otp_code": otp_code,
                        "enable_device_token": "yes",
                        "device_name": "MCPSynology",
                        "format": "sid",
                    },
                    post_form=True,
                )
            except SynologyError as e2:
                click.echo(click.style(f"Login failed: {e2}", fg="red"), err=True)
                return result
        else:
            click.echo(click.style(f"Login failed: {e}", fg="red"), err=True)
            return result
    except OSError as e:
        click.echo(click.style(f"Login failed: {e}", fg="red"), err=True)
        return result

    # Without a session it isn't a login, whatever the reply says, so nothing gets stored (#454).
    sid = data.get("sid") if isinstance(data, dict) else None
    if not sid:
        click.echo(
            click.style("Login failed: the server's reply had no session ID.", fg="red"), err=True
        )
        return result
    client.sid = sid
    click.echo(click.style("Login successful!", fg="green"))
    device_id = data.get("did")
    if device_id:
        result["device_id"] = device_id
    result["success"] = True
    result["sid"] = sid
    return result


def _exit_nothing_changed() -> NoReturn:
    """After a failed login, which has said why: nothing was written, and a script can tell."""
    click.echo("Nothing was changed.", err=True)
    sys.exit(1)


async def _connect_and_login(
    config: Any,
    username: str,
    password: str,
    store: Callable[[str | None], bool],
    verbose: bool,
) -> dict[str, Any]:
    """Connect to NAS, validate login, store the credentials, fetch hostname. Returns result dict.

    ``store`` runs right after the login succeeds. The hostname only suggests an alias, so a failed
    read costs that and nothing else: the login worked, and its code is spent."""
    import httpx

    from mcp_synology.core.client import DsmClient
    from mcp_synology.core.config import AppConfig

    if not isinstance(config, AppConfig):
        raise RuntimeError("Expected AppConfig instance")
    if config.connection is None:
        raise RuntimeError("Config missing connection settings")

    protocol = "https" if config.connection.https else "http"
    base_url = f"{protocol}://{config.connection.host}:{config.connection.port}"

    result: dict[str, Any] = {"success": False}

    with exit_on_connection_failure(base_url, config.connection.host):
        async with DsmClient(
            base_url=base_url,
            verify_ssl=config.connection.verify_ssl,
            timeout=config.connection.timeout,
        ) as client:
            await client.query_api_info()

            login_result = await _attempt_login(client, username, password)
            if not login_result["success"]:
                return result

            # The logout runs whatever happens here, so a timeout, a dropped connection or an
            # interrupt doesn't leave the session open (#404).
            try:
                result["keyring_ok"] = store(login_result.get("device_id"))
                try:
                    dsm_info = await client.fetch_dsm_info()
                except httpx.TransportError as e:
                    click.echo(f"Couldn't read the NAS's hostname ({type(e).__name__}).")
                    dsm_info = {}
            finally:
                await _logout(client)
            nas_hostname = dsm_info.get("hostname") or ""
            if nas_hostname:
                result["hostname"] = nas_hostname
            dsm_version = dsm_info.get("version_string", "")
            if dsm_version:
                result["dsm_version"] = dsm_version

            result["success"] = True

    return result


async def _setup_login(
    config: object, username: str, password: str, store: Callable[[str | None], bool]
) -> dict[str, Any]:
    """Attempt login during setup, handle 2FA if needed, then store the credentials."""
    from mcp_synology.core.client import DsmClient
    from mcp_synology.core.config import AppConfig

    if not isinstance(config, AppConfig):
        raise RuntimeError("Expected AppConfig instance")
    if config.connection is None:
        raise RuntimeError("Config missing connection settings")

    protocol = "https" if config.connection.https else "http"
    base_url = f"{protocol}://{config.connection.host}:{config.connection.port}"

    with exit_on_connection_failure(base_url, config.connection.host):
        async with DsmClient(
            base_url=base_url,
            verify_ssl=config.connection.verify_ssl,
            timeout=config.connection.timeout,
        ) as client:
            await client.query_api_info()

            login_result = await _attempt_login(client, username, password)
            if not login_result["success"]:
                return {"success": False}

            # Logout, best effort as in _connect_and_login: the login worked (#404).
            try:
                keyring_ok = store(login_result.get("device_id"))
            finally:
                await _logout(client)
            return {"success": True, "keyring_ok": keyring_ok}


async def _logout(client: Any) -> None:
    """Log out, best effort and bounded by serve's shutdown limit (``LOGOUT_TIMEOUT``).

    DSM ends the session on its own timeout, so a failure is only logged. The bound keeps
    a NAS that stopped answering (the reason a hostname read just timed out) from doubling
    setup's wait with the client's full timeout (#404, QA round 1 on #459).
    """
    from mcp_synology.server import LOGOUT_TIMEOUT

    try:
        await asyncio.wait_for(
            client.request("SYNO.API.Auth", "logout", version=6, params={}), LOGOUT_TIMEOUT
        )
    except Exception as e:  # noqa: BLE001 - TimeoutError included; never fails setup
        logger.debug("Setup logout failed: %s: %s", type(e).__name__, e)


def _emit_claude_desktop_snippet(
    config: Any, config_path: Path, credentials_env: dict[str, str] | None = None
) -> None:
    """Print a Claude Desktop JSON snippet for the user to copy. ``credentials_env``: the
    variables to set when there's no keyring to read them from, with a placeholder for
    the password."""
    uvx_path = shutil.which("uvx") or "<path-to-uvx>"

    server_entry: dict[str, Any] = {
        "command": uvx_path,
        "args": [
            "mcp-synology",
            "serve",
            "--config",
            str(config_path),
        ],
    }

    # On Linux, include DBUS_SESSION_BUS_ADDRESS so the server process
    # can access the OS keyring (GNOME Keyring / KWallet via D-Bus).
    # Use the env var if set, otherwise construct the standard systemd path.
    if sys.platform == "linux":
        dbus_addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        if not dbus_addr:
            dbus_addr = f"unix:path=/run/user/{os.getuid()}/bus"
        server_entry["env"] = {"DBUS_SESSION_BUS_ADDRESS": dbus_addr}
    if credentials_env:
        server_entry.setdefault("env", {}).update(credentials_env)

    snippet = {"mcpServers": {f"synology-{config.display_name}": server_entry}}
    click.echo("\nAdd this to your Claude Desktop config (claude_desktop_config.json):\n")
    click.echo(json.dumps(snippet, indent=2))
