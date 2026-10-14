"""Check command: validate stored credentials."""

from __future__ import annotations

import asyncio
import ssl
import sys
from typing import NoReturn

import click
import httpx
import yaml

from mcp_synology.cli.logging_ import _configure_logging, _init_early_logging


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


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Walk ``__cause__`` / ``__context__`` without looping."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _is_tls_verification_failure(exc: BaseException) -> bool:
    return any(isinstance(item, ssl.SSLCertVerificationError) for item in _exception_chain(exc))


def _fail_check(message: str) -> NoReturn:
    click.echo(click.style(message, fg="red"), err=True)
    sys.exit(1)


def _report_unreachable(base_url: str, host: str, exc: BaseException) -> NoReturn:
    """Exit 1 with a clear connect/TLS message (no traceback). See #191."""
    reason = str(exc).strip() or type(exc).__name__
    if _is_tls_verification_failure(exc):
        _fail_check(
            f"TLS certificate verification failed for {host}: {reason}. "
            "If this NAS uses Synology's default self-signed certificate, "
            "set `verify_ssl: false` or install a trusted certificate."
        )
    _fail_check(f"Can't connect to {base_url}: {reason}. Check host, port and https settings.")


async def _check_login(config: object) -> None:
    """Validate credentials by attempting a login."""
    from mcp_synology.core.auth import AuthManager
    from mcp_synology.core.client import DsmClient
    from mcp_synology.core.config import AppConfig
    from mcp_synology.core.errors import SynologyError

    if not isinstance(config, AppConfig):
        raise RuntimeError("Expected AppConfig instance")
    if config.connection is None:
        raise RuntimeError("Config missing connection settings")

    protocol = "https" if config.connection.https else "http"
    host = config.connection.host
    base_url = f"{protocol}://{host}:{config.connection.port}"

    try:
        async with DsmClient(
            base_url=base_url,
            verify_ssl=config.connection.verify_ssl,
            timeout=config.connection.timeout,
        ) as client:
            await client.query_api_info()

            auth = AuthManager(config, client)
            await auth.login()
            click.echo(click.style("Authentication successful!", fg="green"))
            await auth.logout()
    except httpx.TransportError as e:
        # httpx.ConnectError is not an OSError subclass — it used to escape
        # and dump a traceback (#191).
        _report_unreachable(base_url, host, e)
    except ssl.SSLError as e:
        # SSLError subclasses OSError; catch before the OSError branch so TLS
        # failures get the certificate guidance instead of "Authentication failed".
        _report_unreachable(base_url, host, e)
    except (SynologyError, OSError) as e:
        _fail_check(f"Authentication failed: {e}")
