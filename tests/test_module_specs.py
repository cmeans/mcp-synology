"""Module specs must match the tools that ship (#137).

Each module in the server's registry has a spec under ``docs/specs/``, and for
every tool the spec's section (``### N. `tool_name` ``) must state:

- the same permission tier as ``MODULE_INFO``
- the exact LLM-facing description (``**Tool description (LLM-facing):**``
  followed by a ``>`` block), whitespace-normalized
- the same parameter names as the registered tool (the first column of the
  ``**Parameters:**`` table, or ``**Parameters:** None.``)

Defaults and types aren't compared: several defaults are effective values the
handler fills in (the signature says ``None``), and ``int | None`` breaks
markdown table parsing. When this fails, update the spec in the same PR as the
code change.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from mcp_synology.modules import RegisterContext
from mcp_synology.server import _MODULE_REGISTRY

SPEC_DIR = Path(__file__).resolve().parent.parent / "docs" / "specs"
SPECS = {
    "filestation": "filestation-module-spec.md",
    "system": "system-module-spec.md",
    "downloadstation": "downloadstation-module-spec.md",
}

_HEADING = re.compile(r"^### \d+\. `([a-z_]+)`", re.M)
_TIER = re.compile(r"\*\*Permission tier:\*\*\s*(\w+)")
_DESCRIPTION = re.compile(r"\*\*Tool description \(LLM-facing\):\*\*\s*\n((?:>.*\n?)+)")
_PARAM_ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|", re.M)


def _sections(spec: str) -> dict[str, str]:
    heads = [(m.start(), m.group(1)) for m in _HEADING.finditer(spec)]
    return {
        name: spec[start : heads[i + 1][0] if i + 1 < len(heads) else len(spec)]
        for i, (start, name) in enumerate(heads)
    }


def _spec_params(section: str) -> set[str]:
    if re.search(r"\*\*Parameters:\*\*\s*None", section):
        return set()
    start = section.index("**Parameters:**")
    end = section.find("\n**", start + len("**Parameters:**"))
    return set(_PARAM_ROW.findall(section[start : end if end != -1 else None]))


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _registered(module: Any) -> dict[str, Any]:
    server = FastMCP("spec-check")
    module.register(
        RegisterContext(
            server=server,
            manager=MagicMock(),
            allowed_tools={t.name for t in module.MODULE_INFO.tools},
            settings_dict={},
            display_name="spec-check",
        )
    )
    return server._tool_manager._tools  # type: ignore[no-any-return]


def test_every_registered_module_has_a_spec() -> None:
    assert set(_MODULE_REGISTRY) == set(SPECS), (
        "Add a docs/specs/<module>-module-spec.md for every module (and list it in SPECS)."
    )


@pytest.mark.parametrize("module_name", sorted(SPECS))
def test_spec_matches_shipped_tools(module_name: str) -> None:
    module = _MODULE_REGISTRY[module_name]
    sections = _sections((SPEC_DIR / SPECS[module_name]).read_text())
    tools = {t.name: t for t in module.MODULE_INFO.tools}
    assert set(sections) == set(tools), f"{SPECS[module_name]}: tool headings differ from code"

    registered = _registered(module)
    problems: list[str] = []
    for name, info in tools.items():
        section = sections[name]
        tier = _TIER.search(section)
        if tier is None or tier.group(1).upper() != info.permission_tier.name:
            problems.append(f"{name}: permission tier")
        desc = _DESCRIPTION.search(section)
        spec_desc = _normalize(re.sub(r"^>\s?", "", desc.group(1), flags=re.M)) if desc else ""
        if spec_desc != _normalize(info.description):
            problems.append(f"{name}: description differs from MODULE_INFO")
        code_params = {p for p in inspect.signature(registered[name].fn).parameters if p != "ctx"}
        spec_params = _spec_params(section)
        if spec_params != code_params:
            problems.append(
                f"{name}: parameters spec={sorted(spec_params)} code={sorted(code_params)}"
            )
    assert not problems, f"{SPECS[module_name]} is out of date:\n  " + "\n  ".join(problems)
