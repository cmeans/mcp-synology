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

import re
from pathlib import Path

from mcp_synology.server import _MODULE_REGISTRY

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
