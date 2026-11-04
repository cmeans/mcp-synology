"""How every command gets its config: validated, or one clean error and exit 1 (#511)."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, NoReturn

import click
import yaml

from mcp_synology.cli.logging_ import _configure_logging

if TYPE_CHECKING:
    from mcp_synology.core.config import AppConfig


def escape_what_stdout_cant_encode() -> None:
    """`check` and `setup` only, never `serve`, whose stdout is the MCP protocol. Stdout is
    strict about encoding, and an alias, a file's name or a reason (`…`) can hold what it can't
    encode: the crash left `check` without an answer and `setup --list`'s later files unlisted
    (QA round 3 and pre-QA on #532, pre-QA on #541). `sys.stdout` is None if stdout is closed
    (#543)."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")


def _fail(message: str) -> NoReturn:
    click.echo(click.style(f"Error: {message}", fg="red"), err=True)
    sys.exit(1)


def load_or_exit(
    config: str | None, *, configure_logging: bool = True, missing_raises: bool = False
) -> AppConfig:
    """`server.validate_config`, then logging from the result, for `serve`, `check` and
    `setup`. Nothing else in the CLI loads a config (`tests/core/test_config_parity.py`).

    Args:
        config: The `--config` value, or None for discovery.
        configure_logging: False under `--verbose`, which keeps the early DEBUG logging.
        missing_raises: Raise FileNotFoundError instead of exiting, for `setup`, which
            goes on to create a config.
    """
    from mcp_synology.core.config import config_error_text
    from mcp_synology.server import validate_config

    try:
        app_config = validate_config(config)
        if configure_logging:
            _configure_logging(app_config.logging.level, app_config.logging.file)
    except FileNotFoundError as e:
        if missing_raises:
            raise
        _fail(str(e))
    except (ValueError, yaml.YAMLError, OSError) as e:
        _fail(config_error_text(e))
    return app_config
