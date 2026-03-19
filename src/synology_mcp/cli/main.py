"""Main click group, global flags, and serve subcommand."""

from __future__ import annotations

import sys

import click

from synology_mcp import __version__
from synology_mcp.cli.logging_ import _configure_logging, _init_early_logging
from synology_mcp.cli.version import (
    _check_for_update,
    _detect_installer,
    _do_auto_upgrade,
    _do_revert,
    _get_current_version,
    _load_global_state,
    _save_global_state,
)

_PYPI_PACKAGE = "synology-mcp"


@click.group(context_settings={"help_option_names": ["-h", "--help"]}, invoke_without_command=True)
@click.version_option(__version__, "-v", "--version", prog_name="synology-mcp")
@click.option("--check-update", is_flag=True, help="Check PyPI for a newer version")
@click.option(
    "--auto-upgrade",
    type=click.Choice(["enable", "disable"]),
    help="Enable or disable automatic upgrades",
)
@click.option(
    "--revert",
    nargs=1,
    required=False,
    default=None,
    metavar="[VERSION]",
    is_flag=False,
    flag_value="__PREVIOUS__",
    help="Revert to previous or specified version",
)
@click.pass_context
def main(
    ctx: click.Context,
    check_update: bool,
    auto_upgrade: str | None,
    revert: str | None,
) -> None:
    """synology-mcp — MCP server for Synology NAS."""
    if check_update:
        state = _load_global_state()
        latest = _check_for_update(state, force=True)
        _save_global_state(state)
        current = _get_current_version()
        if latest:
            click.echo(f"Update available: {current} -> {latest}")
            installer = _detect_installer()
            if installer == "uv":
                click.echo(f"Upgrade with: uv tool install {_PYPI_PACKAGE}@latest")
            elif installer == "pipx":
                click.echo(f"Upgrade with: pipx upgrade {_PYPI_PACKAGE}")
            else:
                click.echo(f"Upgrade with: uv tool install {_PYPI_PACKAGE}@latest")
        else:
            click.echo(f"You are running the latest version ({current}).")
        ctx.exit()

    if auto_upgrade is not None:
        state = _load_global_state()
        state["auto_upgrade"] = auto_upgrade == "enable"
        _save_global_state(state)
        status = "enabled" if state["auto_upgrade"] else "disabled"
        click.echo(f"Auto-upgrade {status}.")
        ctx.exit()

    if revert is not None:
        _do_revert(None if revert == "__PREVIOUS__" else revert)
        ctx.exit()

    # Track version changes for --revert
    state = _load_global_state()
    current = _get_current_version()
    last_known = state.get("running_version")
    if last_known and last_known != current:
        state["previous_version"] = last_known
    state["running_version"] = current
    _save_global_state(state)

    # Auto-upgrade check — only on interactive commands, not serve
    # (serve is launched by Claude Desktop; upgrading mid-launch is risky)
    if state.get("auto_upgrade") and ctx.invoked_subcommand != "serve":
        latest = _check_for_update(state)
        _save_global_state(state)
        if latest:
            _do_auto_upgrade(state)

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@click.option("-c", "--config", type=click.Path(), help="Path to config file")
def serve(config: str | None) -> None:
    """Start the MCP server (launched by Claude Desktop)."""
    _init_early_logging()

    from synology_mcp.core.config import load_config
    from synology_mcp.server import create_server

    try:
        app_config = load_config(config)
    except (FileNotFoundError, ValueError) as e:
        click.echo(click.style(f"Error: {e}", fg="red"), err=True)
        sys.exit(1)

    _configure_logging(app_config.logging.level, app_config.logging.file)

    server = create_server(app_config)
    server.run(transport="stdio")


# Import and attach subcommands — avoids circular imports since
# setup.py and check.py define standalone @click.command() functions.
from synology_mcp.cli.check import check  # noqa: E402
from synology_mcp.cli.setup import setup  # noqa: E402

main.add_command(setup)
main.add_command(check)
