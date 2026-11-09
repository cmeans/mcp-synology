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

and the table must list only those modules, with no roadmap wording ("planned",
"roadmap", "upcoming", "coming next/soon") anywhere in the README (#441).

When this fails, update the README in the same PR as the code change.
"""

from __future__ import annotations

import inspect
import re
import tomllib
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from mcp_synology.modules import PermissionTier
from mcp_synology.server import _MODULE_REGISTRY

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")

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
    # The section's first table: its "|" lines up to the first line that isn't one.
    section = README.split("## Supported modules", 1)[1].split("\n## ", 1)[0]
    lines = section.splitlines()
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith("|")), None)
    assert start is not None, "no table under ## Supported modules"
    end = next(
        (i for i in range(start, len(lines)) if not lines[i].lstrip().startswith("|")),
        len(lines),
    )
    table = "\n".join(lines[start:end])
    # GitHub renders an indented "|" line as a row too; skip the header and --- lines.
    rows = [line for line in table.splitlines() if line.lstrip().startswith("|")][2:]
    assert [m.group(1) for m in _TABLE_ROW.finditer(table)] == list(SECTIONS)
    assert len(rows) == len(_MODULE_REGISTRY)
    roadmap = re.findall(
        r"(?i)\b(?:planned|road-?map|upcoming|coming[\s-]+(?:next|soon))\b", README
    )
    assert not roadmap, f"roadmap wording in the README: {roadmap}"


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


# PyPI shows the README with its relative links rewritten at build time (#487), by the
# hatch-fancy-pypi-readme hook in pyproject.toml. These build the wheel's metadata with
# hatchling's own PEP 517 hook (what `uv build` runs) and read its long description.
_ROOT = Path(__file__).resolve().parent.parent
_VERSION = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
    "version"
]
_TAG_URL = f"https://github.com/cmeans/mcp-synology/blob/v{_VERSION}/"
# Every way Markdown or HTML names a link target: inline (with or without a title or angle
# brackets), a reference definition, and an HTML href or src.
_TARGETS = re.compile(
    r"\]\(\s*<?([^)\s>]+)"  # [text](target "title") and [text](<target>)
    r"|^\s{0,3}\[(?!\^)[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)"  # [ref]: target (not a [^footnote])
    r"|\b(?:href|src|srcset)=[\"']([^\"']+)[\"']",  # <a href="..."> <img src="...">
    re.M,
)
_ABSOLUTE = ("https://", "http://", "#", "mailto:")


def _targets(text: str) -> list[str]:
    return [next(g for g in m.groups() if g) for m in _TARGETS.finditer(text)]


@pytest.fixture(scope="module")
def pypi_description(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The long description of the wheel `uv build` would make from this checkout."""
    import os
    from email.parser import Parser

    from hatchling.build import prepare_metadata_for_build_wheel

    out = tmp_path_factory.mktemp("metadata")
    here = Path.cwd()
    os.chdir(_ROOT)
    try:
        dist_info = prepare_metadata_for_build_wheel(str(out))
    finally:
        os.chdir(here)
    metadata = Parser().parsestr((out / dist_info / "METADATA").read_text(encoding="utf-8"))
    assert metadata["Description-Content-Type"] == "text/markdown"
    body = metadata.get_payload()
    assert isinstance(body, str)
    return body


def test_link_targets_finds_every_form() -> None:
    text = (
        '[a](docs/a.md) [b](docs/b.md "B") [c](<docs/c.md>)\n'
        '[d]: docs/d.md\n<a href="docs/e.md">e</a> <img src="docs/f.png">\n[^1]: A footnote.'
    )
    assert _targets(text) == [
        "docs/a.md", "docs/b.md", "docs/c.md", "docs/d.md", "docs/e.md", "docs/f.png"
    ]  # fmt: skip


def test_pypi_description_is_the_whole_readme(pypi_description: str) -> None:
    assert "$HFPR" not in pypi_description
    # Each README line is there, links aside.
    strip = re.compile(r"\]\([^)]*\)")
    assert strip.sub("]()", pypi_description).strip() == strip.sub("]()", README).strip()


def test_readme_has_no_relative_image() -> None:
    """A relative image would be rewritten to a GitHub page, not the image, so PyPI would show
    it broken: an image takes an absolute raw.githubusercontent.com URL, as the logo does."""
    images = re.findall(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)", README)
    images += re.findall(r"<img\b[^>]*\bsrc=[\"']([^\"']+)", README)
    assert [i for i in images if not i.startswith(("https://", "http://"))] == []


def test_pypi_description_has_no_relative_link(pypi_description: str) -> None:
    """Each link on the PyPI page is absolute or an anchor in the page itself."""
    relative = [t for t in _targets(pypi_description) if not t.startswith(_ABSOLUTE)]
    assert relative == []


def test_pypi_description_links_go_to_files_at_the_release_tag(pypi_description: str) -> None:
    """Every README link that's relative becomes a link to that file or folder at the
    release's tag, so the PyPI page of a release links to the docs as they were in it."""
    relative = [t for t in _targets(README) if not t.startswith(_ABSOLUTE)]
    rewritten = [t for t in _targets(pypi_description) if t.startswith(_TAG_URL)]
    assert len(rewritten) == len(relative) > 0
    for link in rewritten:
        path = link.removeprefix(_TAG_URL).split("#", 1)[0]
        assert (_ROOT / path).exists(), link
