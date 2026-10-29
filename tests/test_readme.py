"""The README's module coverage must match the modules that ship (#435).

For every module in the server's registry, the README must have:

- a row in the "Supported modules" table whose READ and WRITE counts match
  ``MODULE_INFO`` (WRITE counts the WRITE and ADMIN tiers; ``—`` means none)
- a section (``### <name>``) whose ``**READ:**`` and ``**WRITE:**`` lines name
  exactly the module's tools in each tier
- in that section, a settings table naming exactly the fields of the module's
  ``settings_schema`` (or ``**Settings:** none.`` without one), and a ``yaml``
  block whose ``settings`` are valid for the schema
- each settings-table default, and each setting in a ``yaml`` block that says
  its values are the defaults, matching the default the code applies
- every section having the same parts: a ``yaml`` block, Accounts, DSM versions,
  Not covered and a Spec link

When this fails, update the README in the same PR as the code change.
"""

from __future__ import annotations

import inspect
import re
import types
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
    "storage": "Storage",
}

_TABLE_ROW = re.compile(r"^\| \[[^\]]+\]\(#[^)]+\) \| `([a-z]+)` \| ([^|]+) \| ([^|]+) \|", re.M)
_SETTING_ROW = re.compile(r"^\| `([a-z_]+)` \|", re.M)
_YAML_BLOCK = re.compile(r"```yaml\n(.*?)```", re.S)
_DEFAULT_CELL = re.compile(r"^\| `([a-z_]+)` \| ([^|]+) \|", re.M)

# Settings whose schema default is None and that the module resolves to something
# else (filestation/__init__.py, register(); test_resolved_defaults_match_the_code
# reads them there). Any other None default is described in words ("your Downloads
# folder", "none"), never as a value.
_RESOLVED_DEFAULTS: dict[tuple[str, str], object] = {
    ("filestation", "search_timeout"): "async_timeout",
    ("filestation", "dir_size_timeout"): "async_timeout",
    ("filestation", "delete_timeout"): 30,
    ("filestation", "copy_move_timeout"): 30,
    ("filestation", "upload_timeout"): 300,
    ("filestation", "download_timeout"): 300,
}


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


def _effective_default(module: str, field: str) -> object:
    schema = _MODULE_REGISTRY[module].MODULE_INFO.settings_schema
    assert schema is not None
    default = schema.model_fields[field].default
    if default is None:
        return _RESOLVED_DEFAULTS.get((module, field))
    return default


def _as_cell(value: object) -> str:
    text = str(value).lower() if isinstance(value, bool) else str(value)
    return f"`{text}`"


def test_every_module_is_documented() -> None:
    assert set(SECTIONS) == set(_MODULE_REGISTRY)


def test_coverage_table_lists_only_implemented_modules() -> None:
    """Every row of "Supported modules" is a module the server loads, in the README's
    order: the README lists no planned module (Container Manager was, #441)."""
    table = README.split("## Supported modules", 1)[1].split("**Coverage**", 1)[0]
    # GitHub renders an indented "|" line as a row too; skip the header and --- lines.
    rows = [line for line in table.splitlines() if line.lstrip().startswith("|")][2:]
    assert [m.group(1) for m in _TABLE_ROW.finditer(table)] == list(SECTIONS)
    assert len(rows) == len(_MODULE_REGISTRY)
    roadmap = re.findall(r"(?i)\b(?:planned|roadmap|coming (?:next|soon))\b", README)
    assert not roadmap, f"the README announces unbuilt work: {roadmap}"


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


@pytest.mark.parametrize("module", sorted(SECTIONS))
def test_section_defaults(module: str) -> None:
    section = _section(SECTIONS[module])
    if _MODULE_REGISTRY[module].MODULE_INFO.settings_schema is None:
        return
    for field, cell in _DEFAULT_CELL.findall(section):
        expected = _effective_default(module, field)
        if expected is None:
            assert not cell.strip().startswith("`"), f"{field}'s table default isn't a value"
        else:
            assert cell.strip() == _as_cell(expected), f"{field}'s table default"
    for block in _YAML_BLOCK.findall(section):
        if "the values shown are the defaults" not in block:
            continue
        settings = yaml.safe_load(block)["modules"][module].get("settings") or {}
        for field, value in settings.items():
            expected = _effective_default(module, field)
            if isinstance(expected, str) and expected in settings:  # e.g. async_timeout
                expected = settings[expected]
            assert value == expected, f"{field} in the yaml block"


def test_resolved_defaults_match_the_code() -> None:
    source = inspect.getsource(_MODULE_REGISTRY["filestation"].register)
    for (_, field), value in _RESOLVED_DEFAULTS.items():
        fallback = f"settings.{value}" if isinstance(value, str) else str(value)
        assert f"settings.{field} or {fallback})" in source, field


@pytest.mark.parametrize("module", sorted(SECTIONS))
def test_every_numeric_none_default_is_resolved(module: str) -> None:
    schema = _MODULE_REGISTRY[module].MODULE_INFO.settings_schema
    if schema is None:
        return
    for field, info in schema.model_fields.items():
        args = set(getattr(info.annotation, "__args__", ()))
        numeric = isinstance(info.annotation, types.UnionType) and args & {int, float}
        if info.default is None and numeric:
            assert (module, field) in _RESOLVED_DEFAULTS, f"{module}.{field}"


@pytest.mark.parametrize("module", sorted(SECTIONS))
def test_section_parts(module: str) -> None:
    section = _section(SECTIONS[module])
    for part in ("```yaml", "**Accounts.**", "**DSM versions:**", "**Not covered:**", "Spec: "):
        assert part in section, f"no {part} in ### {SECTIONS[module]}"
