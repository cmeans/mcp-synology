"""Setup command: interactive config creation and credential flow."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import click
import yaml

from mcp_synology.cli.check import exit_on_connection_failure
from mcp_synology.cli.logging_ import _configure_logging, _init_early_logging
from mcp_synology.cli.startup import escape_what_stdout_cant_encode, load_or_exit

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

    from mcp_synology.core.config import AppConfig, format_validation_error

    try:
        app_config = AppConfig(**config_dict)
    except ValidationError as e:
        # Screened like every config error: pydantic's own text quotes the input (#471).
        click.echo(click.style(f"Error: {format_validation_error(e)}", fg="red"), err=True)
        sys.exit(1)

    # Prompt for credentials
    click.echo()
    username = click.prompt("DSM username").strip()
    password = click.prompt("DSM password", hide_input=True)

    # Store in keyring
    service = f"mcp-synology/{instance_id}"
    keyring_ok = _store_keyring(service, username, password)
    if not keyring_ok:
        return

    # Connect, validate login, fetch hostname
    click.echo("\nConnecting to NAS...")
    result = asyncio.run(_connect_and_login(app_config, username, password, service, verbose))
    if not result["success"]:
        return

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

    # Check for existing file before writing
    config_path = _CONFIG_DIR / f"{instance_id}.yaml"
    if config_path.exists() and not click.confirm(
        f"\n{config_path} already exists. Overwrite?", default=False
    ):
        click.echo("Aborted.")
        return

    # Write config file atomically (closes #70)
    from mcp_synology.core.fs import atomic_write_text

    raw_yaml = yaml.dump(config_dict, default_flow_style=False, sort_keys=False)
    header = "# Generated by mcp-synology setup\n"
    atomic_write_text(config_path, header + raw_yaml)
    click.echo(f"\nConfig written to {config_path}")
    if not_loaded:
        # Discovery reads config.yaml, so a bare `check` here would stop at the same file again.
        from mcp_synology.core.config import quoted_path

        click.echo(f"To use it, name it: mcp-synology check --config {quoted_path(config_path)}")

    # Emit Claude Desktop JSON snippet
    _emit_claude_desktop_snippet(app_config, config_path)


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
    click.echo(f"  Host: {app_config.connection.host}")
    click.echo(f"  Instance: {app_config.instance_id}")
    click.echo()

    username = click.prompt("DSM username").strip()
    password = click.prompt("DSM password", hide_input=True)

    service = f"mcp-synology/{app_config.instance_id}"
    keyring_ok = _store_keyring(service, username, password)
    if not keyring_ok:
        return

    click.echo("\nAttempting login...")
    asyncio.run(_setup_login(app_config, username, password, service))


def _store_keyring(service: str, username: str, password: str) -> bool:
    """Store credentials in the OS keyring. Returns True on success."""
    try:
        import keyring
        from keyring.errors import KeyringError

        keyring.set_password(service, "username", username)
        keyring.set_password(service, "password", password)
        click.echo("\nCredentials stored in OS keyring.")
        return True
    except (ImportError, KeyringError, OSError):
        click.echo(
            "\nKeyring not available. Set environment variables instead:\n"
            f"  export SYNOLOGY_USERNAME={username}\n"
            f"  export SYNOLOGY_PASSWORD=<your-password>"
        )
        return False


async def _attempt_login(
    client: Any,
    username: str,
    password: str,
    service: str,
) -> dict[str, Any]:
    """Attempt DSM login, handling 2FA if required.

    Returns dict with 'success' bool, and optionally 'sid'.
    On 2FA success, stores device token in keyring.
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
                device_id = data.get("did", "")
                if device_id:
                    import keyring

                    keyring.set_password(service, "device_id", device_id)
                    click.echo(click.style("2FA bootstrap complete!", fg="green"))
                    click.echo("Device token stored in keyring.")
                else:
                    click.echo(click.style("Login successful!", fg="green"))
            except SynologyError as e2:
                click.echo(click.style(f"Login failed: {e2}", fg="red"), err=True)
                return result
        else:
            click.echo(click.style(f"Login failed: {e}", fg="red"), err=True)
            return result
    except OSError as e:
        click.echo(click.style(f"Login failed: {e}", fg="red"), err=True)
        return result

    sid = data.get("sid")
    if sid:
        client.sid = sid
        if not result.get("success"):
            click.echo(click.style("Login successful!", fg="green"))
    result["success"] = True
    result["sid"] = sid
    return result


async def _connect_and_login(
    config: Any, username: str, password: str, service: str, verbose: bool
) -> dict[str, Any]:
    """Connect to NAS, validate login, fetch hostname. Returns result dict."""
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

            login_result = await _attempt_login(client, username, password, service)
            if not login_result["success"]:
                return result

            # Fetch NAS hostname. The logout runs whatever happens here, so a
            # timeout or dropped connection doesn't leave the session open (#404).
            try:
                dsm_info = await client.fetch_dsm_info()
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


async def _setup_login(config: object, username: str, password: str, service: str) -> None:
    """Attempt login during setup, handle 2FA if needed."""
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

            login_result = await _attempt_login(client, username, password, service)
            if not login_result["success"]:
                return

            # Logout, best effort as in _connect_and_login: the login worked (#404).
            if client.sid:
                await _logout(client)


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


def _emit_claude_desktop_snippet(config: Any, config_path: Path) -> None:
    """Print a Claude Desktop JSON snippet for the user to copy."""
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

    snippet = {"mcpServers": {f"synology-{config.display_name}": server_entry}}
    click.echo("\nAdd this to your Claude Desktop config (claude_desktop_config.json):\n")
    click.echo(json.dumps(snippet, indent=2))
