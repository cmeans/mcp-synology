"""Setup command: interactive config creation and credential flow."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import click
import yaml

from mcp_synology.cli.logging_ import _configure_logging, _init_early_logging

_CONFIG_DIR = Path.home() / ".config" / "mcp-synology"


@click.command()
@click.option("-c", "--config", type=click.Path(), help="Path to config file")
@click.option("-l", "--list", "list_configs", is_flag=True, help="List existing configurations")
@click.option("--verbose", is_flag=True, help="Enable debug logging")
def setup(config: str | None, list_configs: bool, verbose: bool) -> None:
    """Interactive credential setup and 2FA bootstrap."""
    _init_early_logging(verbose=verbose)

    if list_configs:
        _list_configurations()
        return

    # If --config is given, use the existing config-file flow
    if config:
        _setup_with_config(config, verbose)
        return

    # Try to load an existing config via discovery
    from pydantic import ValidationError

    from mcp_synology.core.config import format_validation_error, load_config

    try:
        app_config = load_config(None)
    except FileNotFoundError:
        # No config file found — enter interactive creation mode
        _setup_interactive(verbose)
        return
    except ValidationError as e:
        # Must precede ValueError — pydantic.ValidationError subclasses it.
        click.echo(click.style(f"Error: {format_validation_error(e)}", fg="red"), err=True)
        sys.exit(1)
    except (ValueError, yaml.YAMLError) as e:
        click.echo(click.style(f"Error: {e}", fg="red"), err=True)
        sys.exit(1)

    # Config file exists — do credential setup against it
    _setup_credential_flow(app_config, verbose)


def _list_configurations() -> None:
    """List existing configuration files in the config directory."""
    if not _CONFIG_DIR.exists():
        click.echo("No configurations found.")
        click.echo(f"Config directory: {_CONFIG_DIR}")
        return

    yaml_files = sorted(_CONFIG_DIR.glob("*.yaml"))
    if not yaml_files:
        click.echo("No configurations found.")
        click.echo(f"Config directory: {_CONFIG_DIR}")
        return

    click.echo(f"Configurations in {_CONFIG_DIR}:\n")
    for f in yaml_files:
        try:
            raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            host = raw.get("connection", {}).get("host", "?")
            alias = raw.get("alias", "")
            instance_id = raw.get("instance_id", f.stem)
            label = alias or instance_id
            click.echo(f"  {f.name}")
            click.echo(f"    Name: {label}")
            click.echo(f"    Host: {host}")
            click.echo()
        except (OSError, yaml.YAMLError, AttributeError):
            click.echo(f"  {f.name}  (could not parse)")


def _setup_interactive(verbose: bool) -> None:
    """Interactive setup when no config file exists."""
    click.echo("No configuration file found. Let's create one.\n")

    # Prompt for connection details
    host = click.prompt("NAS host (IP or hostname)")
    use_https = click.confirm("Use HTTPS?", default=False)
    permission = click.prompt(
        "File Station permission",
        type=click.Choice(["read", "write"]),
        default="read",
    )

    # Derive instance_id for file naming
    from mcp_synology.core.config import _derive_instance_id

    instance_id = _derive_instance_id(host)

    # Prompt for alias
    alias_input = click.prompt("Alias (friendly name, optional)", default="", show_default=False)
    alias: str | None = alias_input.strip() or None

    # Build config dict
    conn: dict[str, Any] = {"host": host}
    if use_https:
        conn["https"] = True
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
    from mcp_synology.core.config import AppConfig

    try:
        app_config = AppConfig(**config_dict)
    except ValueError as e:
        click.echo(click.style(f"Config validation failed: {e}", fg="red"), err=True)
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
    if nas_hostname and not alias:
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

    # Emit Claude Desktop JSON snippet
    _emit_claude_desktop_snippet(app_config, config_path)


def _setup_with_config(config_path_str: str, verbose: bool) -> None:
    """Setup using an explicit config file path."""
    from pydantic import ValidationError

    from mcp_synology.core.config import format_validation_error, load_config

    try:
        app_config = load_config(config_path_str)
    except ValidationError as e:
        # Must precede ValueError — pydantic.ValidationError subclasses it.
        click.echo(click.style(f"Error: {format_validation_error(e)}", fg="red"), err=True)
        sys.exit(1)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as e:
        click.echo(click.style(f"Error: {e}", fg="red"), err=True)
        sys.exit(1)

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

    async with DsmClient(
        base_url=base_url,
        verify_ssl=config.connection.verify_ssl,
        timeout=config.connection.timeout,
    ) as client:
        await client.query_api_info()

        login_result = await _attempt_login(client, username, password, service)
        if not login_result["success"]:
            return result

        # Fetch NAS hostname
        dsm_info = await client.fetch_dsm_info()
        nas_hostname = dsm_info.get("hostname") or ""
        if nas_hostname:
            result["hostname"] = nas_hostname
        dsm_version = dsm_info.get("version_string", "")
        if dsm_version:
            result["dsm_version"] = dsm_version

        # Logout
        await client.request("SYNO.API.Auth", "logout", version=6, params={})

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

    async with DsmClient(
        base_url=base_url,
        verify_ssl=config.connection.verify_ssl,
        timeout=config.connection.timeout,
    ) as client:
        await client.query_api_info()

        login_result = await _attempt_login(client, username, password, service)
        if not login_result["success"]:
            return

        # Logout
        if client.sid:
            await client.request("SYNO.API.Auth", "logout", version=6, params={})


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
