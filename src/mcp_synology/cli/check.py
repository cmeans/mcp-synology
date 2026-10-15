"""Check command: validate stored credentials."""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import sys
from typing import TYPE_CHECKING

import click
import yaml

from mcp_synology.cli.logging_ import _configure_logging, _init_early_logging

if TYPE_CHECKING:
    from collections.abc import Iterator


@click.command()
@click.option("-c", "--config", type=click.Path(), help="Path to config file")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
def check(config: str | None, verbose: bool) -> None:
    """Validate stored credentials can authenticate."""
    _init_early_logging(verbose=verbose)

    from pydantic import ValidationError

    from mcp_synology.core.config import format_validation_error, load_config

    try:
        app_config = load_config(config)
    except ValidationError as e:
        # Must precede ValueError — pydantic.ValidationError subclasses it.
        click.echo(click.style(f"Error: {format_validation_error(e)}", fg="red"), err=True)
        sys.exit(1)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as e:
        click.echo(click.style(f"Error: {e}", fg="red"), err=True)
        sys.exit(1)

    if not verbose:
        _configure_logging(app_config.logging.level, app_config.logging.file)

    click.echo(f"Checking credentials for '{app_config.display_name}'...")
    asyncio.run(_check_login(app_config))


def connection_failure_message(base_url: str, host: str, exc: BaseException) -> str:
    """One clean line for a NAS that can't be reached, instead of an httpx traceback (#191).

    httpx's transport errors aren't ``OSError``, and their text is often empty, so the
    exception type is named. A TLS verification failure sits in the exception chain as
    ``ssl.SSLCertVerificationError`` and gets its own advice.
    """
    reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    cause: BaseException | None = exc
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:  # chains can loop
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLCertVerificationError):
            return (
                f"TLS certificate verification failed for {host}: {cause}. If this NAS uses "
                "Synology's default self-signed certificate, set connection.verify_ssl: false "
                "or install a trusted certificate."
            )
        cause = cause.__cause__ or cause.__context__
    return f"Can't connect to {base_url}: {reason}. Check the host, port and https settings."


@contextlib.contextmanager
def exit_on_connection_failure(base_url: str, host: str) -> Iterator[None]:
    """Turn an unreachable NAS into a red error line and exit 1 (check and setup, #191)."""
    import httpx

    try:
        yield
    except httpx.TransportError as e:
        message = connection_failure_message(base_url, host, e)
        click.echo(click.style(f"Error: {message}", fg="red"), err=True)
        sys.exit(1)


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

    with exit_on_connection_failure(base_url, config.connection.host):
        async with DsmClient(
            base_url=base_url,
            verify_ssl=config.connection.verify_ssl,
            timeout=config.connection.timeout,
        ) as client:
            from mcp_synology.core.errors import SynologyError

            try:
                await client.query_api_info()
            except SynologyError as e:
                click.echo(click.style(f"Error: DSM API query failed: {e}", fg="red"), err=True)
                sys.exit(1)

            auth = AuthManager(config, client)
            try:
                await auth.login()
                click.echo(click.style("Authentication successful!", fg="green"))
                await auth.logout()
            except (SynologyError, OSError) as e:
                click.echo(click.style(f"Authentication failed: {e}", fg="red"), err=True)
                sys.exit(1)
