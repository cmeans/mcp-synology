"""Tool names fit Claude's limit with room for the server's name (#682).

Claude loads a server's tools through tool search, whose tool references are refused over 64
characters (the API's own `name` limit is 128, `^[a-zA-Z0-9_-]{1,128}$`; aligning the two was
declined upstream: anthropics/claude-code#16425). The client puts the server's name in front of
ours: ``mcp__<server>__<tool>`` in Claude Code, ``mcp__remote-devices__<server>__<tool>`` when
Claude Desktop reaches a server on the user's computer. Every user's server name has to fit in
what our longest tool name leaves, so a longer tool name is a decision made here, with a reason,
and the README's limits are computed from it.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from mcp_synology.cli import main
from mcp_synology.cli.setup import _emit_claude_desktop_snippet, _hostname_alias_fits
from mcp_synology.server import (
    _MODULE_REGISTRY,
    DESKTOP_PREFIX,
    TOOL_SEARCH_LIMIT,
    _module_plan,
    create_server,
    server_name_problem,
    suggested_server_name,
)
from tests.conftest import make_test_config

if TYPE_CHECKING:
    from mcp_synology.core.config import AppConfig

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")

API_LIMIT = 64
# The longest tool name allowed: `restore_from_recycle_bin`'s. Raising it shortens every user's
# server name by as much (the README's limits follow from it).
MAX_TOOL_NAME = 24
APP_PREFIX = "mcp__remote-devices__"  # Claude Desktop reaching a server on the computer
CODE_PREFIX = "mcp__"  # Claude Code
SEPARATOR = "__"
_API_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _tool_names() -> list[str]:
    return sorted(
        tool.name for module in _MODULE_REGISTRY.values() for tool in module.MODULE_INFO.tools
    )


def _server_name_limit(prefix: str) -> int:
    return API_LIMIT - len(prefix) - len(SEPARATOR) - max(map(len, _tool_names()))


def test_every_tool_name_fits_the_budget() -> None:
    names = _tool_names()
    assert len(names) > 50, names
    too_long = [n for n in names if len(n) > MAX_TOOL_NAME]
    assert too_long == [], (
        f"{too_long} are over {MAX_TOOL_NAME} characters: each character comes off every "
        "user's server name (raise MAX_TOOL_NAME only on purpose, and say why)"
    )
    assert all(_API_NAME.match(n) for n in names)


def test_the_budget_is_the_longest_name() -> None:
    """The ceiling is today's longest name, so the README's limits are what users get."""
    assert max(map(len, _tool_names())) == MAX_TOOL_NAME


def test_the_readme_states_the_server_name_limits() -> None:
    longest = max(_tool_names(), key=len)
    app, code = _server_name_limit(APP_PREFIX), _server_name_limit(CODE_PREFIX)
    assert (app, code) == (17, 33)
    match = re.search(
        r"tool search, which refuses a tool whose full name is over (\d+) characters, and the "
        r"client builds that name from the server's name and the tool's: `([a-z_-]+)<server>__"
        r"<tool>` in Claude Code, and `([a-z_-]+)<server>__<tool>` when Claude Desktop reaches a "
        r"server on your computer\. The longest tool name is `([a-z_]+)` \((\d+) characters\), "
        r"so the server's name can be at most (\d+) characters in Claude Desktop \(`synology-` "
        r"plus an alias, or without one the instance ID, of up to (\d+): `([^`]+)` fits\) and "
        r"(\d+) in Claude Code\. .*?\"should have at most (\d+) characters\"",
        README,
    )
    assert match, "the README's server-name limits paragraph is missing or reworded"
    limit, code_prefix, app_prefix, name, length, app_text, alias, example, code_text, error = (
        match.groups()
    )
    assert int(limit) == API_LIMIT == int(error)
    assert (code_prefix, app_prefix) == (CODE_PREFIX, APP_PREFIX)
    assert name == longest and int(length) == len(longest)
    assert int(app_text) == app and int(code_text) == code
    assert int(alias) == app - len("synology-")
    assert len(example) <= app


# --- the warning `setup` and `check` give (maintainer, 2026-11-11) ---

WRITE_FILES = {"filestation": {"enabled": True, "permission": "write"}}


def _config(**overrides: object) -> AppConfig:
    return make_test_config(**{"modules": WRITE_FILES, **overrides})


def test_the_constants_are_the_servers() -> None:
    assert (TOOL_SEARCH_LIMIT, DESKTOP_PREFIX) == (API_LIMIT, APP_PREFIX)


@pytest.mark.parametrize(
    ("overrides", "fits"),
    [
        ({"alias": "HomeNAS"}, True),  # synology-HomeNAS, 16
        ({"instance_id": "home-nas"}, True),  # synology-home-nas, 17: exactly the limit
        ({"instance_id": "diskstation"}, False),  # a host name such as DiskStation: 20
        ({"instance_id": "192-168-1-100"}, False),  # an IP address as the host: 22
        # Only the tools this config serves count: read-only File Station's longest is shorter.
        ({"instance_id": "diskstation", "modules": {"filestation": {"enabled": True}}}, True),
    ],
)
def test_the_suggested_server_name_is_checked(overrides: dict[str, object], fits: bool) -> None:
    config = _config(**overrides)
    problem = server_name_problem(config)
    assert (problem is None) == fits, problem
    served = [t for *_, allowed in _module_plan(config) for t in allowed]
    full = len(DESKTOP_PREFIX) + len(suggested_server_name(config)) + 2 + max(map(len, served))
    assert (full <= API_LIMIT) == fits


def test_the_warning_says_what_to_do() -> None:
    problem = server_name_problem(_config(instance_id="diskstation"))
    assert problem == (
        "the server name setup suggests, 'synology-diskstation' (20 characters), is too long "
        "for Claude Desktop: with restore_from_recycle_bin (24 characters), a tool's full name "
        "would be over Claude's 64-character limit, and a conversation fails once Claude loads "
        "that tool. Set an alias of up to 8 characters in the config (e.g. alias: nas), or "
        "name the server 17 characters or fewer in your MCP client's config. If it already is, "
        "or you use only Claude Code (where 33 characters fit), this doesn't apply."
    )


@pytest.mark.parametrize(
    ("alias", "instance_id", "fits"),
    [
        # The alias names the server when there is one, whatever the instance ID.
        ("long-alias", "nas", False),
        ("nas", "diskstation", True),
    ],
)
def test_the_alias_wins_over_the_instance_id(alias: str, instance_id: str, fits: bool) -> None:
    assert (server_name_problem(_config(alias=alias, instance_id=instance_id)) is None) == fits


def test_serve_names_itself_so_and_logs_the_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="mcp_synology.server"):
        assert create_server(_config(alias="HomeNAS")).name == "synology-HomeNAS"
        assert "server name setup suggests" not in caplog.text
        create_server(_config(instance_id="diskstation"))
    assert "the server name setup suggests, 'synology-diskstation'" in caplog.text


def test_setup_offers_the_host_name_only_when_it_fits(capsys: pytest.CaptureFixture[str]) -> None:
    """Setup offered DSM's host name as the alias, then warned it was too long (pre-QA r2)."""
    config_dict = {
        "schema_version": 1,
        "instance_id": "nas",
        "connection": {"host": "nas"},
        "modules": WRITE_FILES,
    }
    assert _hostname_alias_fits(config_dict, "HomeNAS")
    assert capsys.readouterr().out == ""
    assert not _hostname_alias_fits(config_dict, "DiskStation")
    assert "not offered as the alias" in capsys.readouterr().out


def test_check_warns(tmp_path: Path) -> None:
    """`check` prints it with the config's other warnings, before it tries to log in."""
    config = tmp_path / "config.yaml"
    config.write_text(
        "schema_version: 1\ninstance_id: diskstation\n"
        # A closed local port: the login fails at once, after the warning.
        "connection:\n  host: 127.0.0.1\n  port: 1\n  timeout: 1\n"
        "auth:\n  username: x\n  password: y\n"
        "modules:\n  filestation:\n    enabled: true\n    permission: write\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(main, ["check", "-c", str(config)])
    assert "Warning: the server name setup suggests, 'synology-diskstation'" in result.output


def test_setup_warns_under_its_snippet(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    _emit_claude_desktop_snippet(_config(instance_id="diskstation"), tmp_path / "c.yaml")
    out, err = capsys.readouterr()
    assert '"synology-diskstation"' in out
    assert "Warning: the server name setup suggests, 'synology-diskstation'" in err
    _emit_claude_desktop_snippet(_config(alias="HomeNAS"), tmp_path / "c.yaml")
    assert "Warning" not in capsys.readouterr().err
