"""Check command: validate stored credentials."""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import sys
from typing import TYPE_CHECKING

import click
import httpx
import yaml

from mcp_synology.cli.logging_ import _configure_logging, _init_early_logging
from mcp_synology.core.redaction import redact_urls

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

    from mcp_synology.server import config_notices

    for notice in config_notices(app_config):
        click.echo(click.style(f"Warning: {notice}", fg="yellow"), err=True)

    # Before the login: someone running check because logins fail may be on an old
    # version (#314). `update_notices` doesn't apply here: a person asked.
    if app_config.check_for_updates:
        _report_update()

    click.echo(f"Checking credentials for '{app_config.display_name}'...")
    asyncio.run(_check_login(app_config))


def _report_update() -> None:
    """Say so when a newer mcp-synology is out (#314): best-effort, never fails check."""
    from mcp_synology.cli.version import _check_and_describe_update

    try:
        notice = _check_and_describe_update()
    except Exception:  # noqa: BLE001 - an update notice must never fail check
        return
    if notice:
        click.echo(click.style(notice, fg="yellow"))


def connection_failure_message(base_url: str, host: str, exc: BaseException) -> str:
    """One clean line for a NAS that can't be reached, instead of an httpx traceback (#191).

    httpx's transport errors aren't ``OSError``, and their text is often empty, so the
    exception type is named. A TLS verification failure sits in the exception chain as
    ``ssl.SSLCertVerificationError`` and gets its own advice.
    """
    text = redact_urls(str(exc))
    reason = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
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
    # Connected, then no answer in time or the connection dropped: the NAS is there but
    # busy, so the host, port and https settings aren't the problem (#254).
    if isinstance(exc, httpx.ReadTimeout | httpx.WriteTimeout):
        return (
            f"{base_url} accepted the connection but didn't answer in time ({reason}). The NAS "
            "may be busy: try again shortly, or raise connection.timeout."
        )
    if isinstance(exc, httpx.ReadError | httpx.WriteError | httpx.RemoteProtocolError):
        return (
            f"{base_url} accepted the connection but dropped it ({reason}). The NAS may be "
            "busy or restarting: try again shortly."
        )
    return f"Can't connect to {base_url}: {reason}. Check the host, port and https settings."


_NOT_DSM = "Is this a Synology DSM address? Check the host, port and https settings."


def not_dsm_message(base_url: str, status: int | None) -> str:
    """One clean line for a server that answers but isn't DSM's API (QA R1 on #210).

    A wrong host or port often reaches some other web server: a 404 or 500, or a 200
    HTML page such as a router login. ``status`` is None when the reply wasn't JSON.
    """
    if status is None:
        return f"{base_url} didn't return a DSM API response (not JSON). {_NOT_DSM}"
    message = f"{base_url} answered HTTP {status}, not a DSM API response."
    if status == 400 and base_url.startswith("http://"):
        # DSM's nginx answers plain HTTP sent to its HTTPS port (5001) with 400.
        message += " Plain HTTP sent to an HTTPS port gets 400; is https: true needed?"
    return f"{message} {_NOT_DSM}"


@contextlib.contextmanager
def exit_on_connection_failure(base_url: str, host: str) -> Iterator[None]:
    """Turn a NAS that can't be reached, or a server that isn't DSM, into a red error
    line and exit 1 instead of a traceback (check and setup, #191)."""
    import httpx

    from mcp_synology.core.errors import DsmHttpError, DsmReplyError

    try:
        yield
    except httpx.TransportError as e:
        message = connection_failure_message(base_url, host, e)
    except DsmHttpError as e:
        message = not_dsm_message(base_url, e.status)
    except DsmReplyError:
        message = not_dsm_message(base_url, None)
    else:
        return
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
            from mcp_synology.core.errors import (
                DsmHttpError,
                DsmReplyError,
                LoginRefusedError,
                SynologyError,
            )

            try:
                await client.query_api_info()
            except (DsmHttpError, DsmReplyError):
                raise  # not DSM's API: exit_on_connection_failure says so
            except SynologyError as e:
                click.echo(click.style(f"Error: DSM API query failed: {e}", fg="red"), err=True)
                sys.exit(1)

            auth = AuthManager(config, client)
            try:
                await auth.login()
                click.echo(click.style("Authentication successful!", fg="green"))
                await auth.logout()
            except DsmReplyError:
                raise  # a login page, not DSM: exit_on_connection_failure says so
            except DsmHttpError as e:
                # Its own suggestion says to run check; this is check (QA round 1, #233).
                next_step = "Check the NAS, or the proxy in front of it, and try again."
                message = f"Authentication failed: {e} {next_step}"
                click.echo(click.style(message, fg="red"), err=True)
                sys.exit(1)
            except (SynologyError, OSError) as e:
                if isinstance(e, LoginRefusedError) and isinstance(e.__cause__, SynologyError):
                    # A refusal from LoginRefusals: its text is about the running server
                    # (remembered refusals, restarts, run check). Here, DSM's own error says
                    # it (QA round 2 on #461).
                    e = e.__cause__
                hint = getattr(e, "suggestion", None)
                detail = f"{e} {hint}" if hint else str(e)
                click.echo(click.style(f"Authentication failed: {detail}", fg="red"), err=True)
                sys.exit(1)
