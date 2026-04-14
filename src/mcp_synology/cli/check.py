"""Check command: validate stored credentials."""

from __future__ import annotations

import asyncio
import sys

import click
import yaml

from mcp_synology.cli.logging_ import _configure_logging, _init_early_logging


@click.command()
@click.option("-c", "--config", type=click.Path(), help="Path to config file")
@click.option("--verbose", is_flag=True, help="Enable debug logging")
def check(config: str | None, verbose: bool) -> None:
    """Validate stored credentials can authenticate."""
    _init_early_logging(verbose=verbose)

    from mcp_synology.core.config import load_config

    try:
        app_config = load_config(config)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as e:
        click.echo(click.style(f"Error: {e}", fg="red"), err=True)
        sys.exit(1)

    if not verbose:
        _configure_logging(app_config.logging.level, app_config.logging.file)

    click.echo(f"Checking credentials for '{app_config.display_name}'...")
    asyncio.run(_check_login(app_config))


async def _check_login(config: object) -> None:
    """Validate credentials by attempting a login."""
    from mcp_synology.core.auth import AuthManager
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

        from mcp_synology.core.errors import SynologyError

        auth = AuthManager(config, client)
        try:
            await auth.login()
            click.echo(click.style("Authentication successful!", fg="green"))
            await auth.logout()
        except (SynologyError, OSError) as e:
            click.echo(click.style(f"Authentication failed: {e}", fg="red"), err=True)
            sys.exit(1)
