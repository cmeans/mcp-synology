"""The README's module coverage must match the modules that ship (#435).

For every module in the server's registry, the README must have:

- a row in the "Supported modules" table whose READ and WRITE counts match
  ``MODULE_INFO`` (WRITE counts the WRITE and ADMIN tiers; ``—`` means none)
- a section (``### <name>``) whose ``**READ:**`` and ``**WRITE:**`` lines name
  exactly the module's tools in each tier
- in that section, a settings table naming exactly the fields of the module's
  ``settings_schema`` (or ``**Settings:** none.`` without one), and a ``yaml``
  block whose ``settings`` are valid for the schema

When this fails, update the README in the same PR as the code change.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from mcp_synology.modules import PermissionTier
from mcp_synology.server import _MODULE_REGISTRY

README = (Path(__file__).resolve().parent.parent / "README.md").read_text()

SECTIONS = {
    "system": "System",
    "filestation": "File Station",
    "photos": "Synology Photos",
    "downloadstation": "Download Station",
}

_TABLE_ROW = re.compile(r"^\| \[[^\]]+\]\(#[^)]+\) \| `([a-z]+)` \| ([^|]+) \| ([^|]+) \|", re.M)
_SETTING_ROW = re.compile(r"^\| `([a-z_]+)` \|", re.M)
_YAML_BLOCK = re.compile(r"```yaml\n(.*?)```", re.S)


def _count(cell: str) -> int:
    cell = cell.strip()
    if cell == "—":
        return 0
    match = re.match(r"(\d+)\b", cell)
    assert match, f"no tool count in table cell {cell!r}"
    return int(match.group(1))


def _section(title: str) -> str:
    heading = f"\n### {title}\n"
    start = README.index(heading)
    end = re.compile(r"^#{2,3} ", re.M).search(README, start + len(heading))
    return README[start : end.start() if end else len(README)]


def _tools(section: str, tier: str) -> set[str]:
    match = re.search(rf"^- \*\*{tier}:\*\* (.+)$", section, re.M)
    assert match, f"no **{tier}:** line"
    return set(re.findall(r"`([a-z_]+)`", match.group(1)))


def _tiers(module: str) -> tuple[set[str], set[str]]:
    tools = _MODULE_REGISTRY[module].MODULE_INFO.tools
    read = {t.name for t in tools if t.permission_tier == PermissionTier.READ}
    return read, {t.name for t in tools} - read


def test_every_module_is_documented() -> None:
    assert set(SECTIONS) == set(_MODULE_REGISTRY)


@pytest.mark.parametrize("module", sorted(SECTIONS))
def test_coverage_table_counts(module: str) -> None:
    rows = {m.group(1): (m.group(2), m.group(3)) for m in _TABLE_ROW.finditer(README)}
    assert module in rows, f"no Supported modules row for `{module}`"
    read, write = _tiers(module)
    assert (_count(rows[module][0]), _count(rows[module][1])) == (len(read), len(write))


@pytest.mark.parametrize("module", sorted(SECTIONS))
def test_section_tool_lists(module: str) -> None:
    section = _section(SECTIONS[module])
    read, write = _tiers(module)
    assert _tools(section, "READ") == read
    assert _tools(section, "WRITE") == write


@pytest.mark.parametrize("module", sorted(SECTIONS))
def test_section_settings(module: str) -> None:
    section = _section(SECTIONS[module])
    schema = _MODULE_REGISTRY[module].MODULE_INFO.settings_schema
    blocks = [yaml.safe_load(b) for b in _YAML_BLOCK.findall(section)]
    config: dict[str, Any] = next(b for b in blocks if module in (b or {}).get("modules", {}))
    module_config = config["modules"][module]
    if schema is None:
        assert "**Settings:** none." in section
        assert "settings" not in module_config
        return
    assert set(_SETTING_ROW.findall(section)) == set(schema.model_fields)
    settings = module_config.get("settings") or {}
    # The settings models ignore unknown keys, so check the names first.
    assert set(settings) <= set(schema.model_fields)
    schema.model_validate(settings)
