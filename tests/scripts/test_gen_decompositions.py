"""Tests for scripts/gen_decompositions.py (#515).

The full Unicode files aren't in the repo, so these feed it small excerpts in the real
formats and check that its output, rendered from them, is the generated module's shape:
only characters newer than Unicode 14.0, canonical decompositions only.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gen_decompositions.py"

AGES = """# DerivedAge-18.0.0.txt
00C0..00C5    ; 1.1 #  [6] LATIN CAPITAL LETTER A WITH GRAVE..
0344          ; 1.1 #       COMBINING GREEK DIALYTIKA TONOS
105C9         ; 16.0 #      TODHRI LETTER EI
16121..16128  ; 16.0 #  [8] GURUNG KHEMA VOWEL SIGN U..
"""

DATA = """00C0;LATIN CAPITAL LETTER A WITH GRAVE;Lu;0;L;0041 0300;;;;N;;;;00E0;
00BD;VULGAR FRACTION ONE HALF;No;0;ON;<fraction> 0031 2044;;;1/2;N;;;;;
0344;COMBINING GREEK DIALYTIKA TONOS;Mn;230;NSM;0308 0301;;;;N;;;;;
105C9;TODHRI LETTER EI;Lo;0;L;105D2 0307;;;;N;;;;;
16121;GURUNG KHEMA VOWEL SIGN U;Mn;0;NSM;1611E 1611E;;;;N;;;;;
16126;GURUNG KHEMA VOWEL SIGN O;Mn;0;NSM;16121 1611F;;;;N;;;;;
1611E;GURUNG KHEMA VOWEL SIGN AA;Mn;0;NSM;;;;;N;;;;;
"""


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_decompositions", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_decompositions"] = module
    spec.loader.exec_module(module)
    return module


def test_ages_and_decompositions_parse() -> None:
    gen = _load()
    version, ages = gen.parse_ages(AGES)
    assert version == "DerivedAge-18.0.0.txt"
    assert ages[0x00C3] == (1, 1)
    assert ages[0x16125] == (16, 0)
    decompositions = gen.parse_decompositions(DATA)
    assert decompositions[0x0344] == [0x0308, 0x0301]
    assert 0x00BD not in decompositions  # a compatibility decomposition
    assert 0x1611E not in decompositions


def test_render_keeps_only_characters_newer_than_python_311() -> None:
    gen = _load()
    version, ages = gen.parse_ages(AGES)
    decompositions = gen.parse_decompositions(DATA)
    table = gen.newer_than_floor(decompositions, ages)
    assert sorted(table) == [0x105C9, 0x16121, 0x16126]
    namespace: dict[str, object] = {}
    exec(gen.render(version, "UnicodeData-18.0.0.txt", table), namespace)
    assert namespace["NEW_DECOMPOSITIONS"] == {
        "\U000105c9": "\U000105d2\u0307",
        "\U00016121": "\U0001611e\U0001611e",
        "\U00016126": "\U00016121\U0001611f",
    }


def test_the_generated_module_is_current() -> None:
    """The committed module is formatted as the script renders it. Its values are checked
    against `unicodedata` by test_helpers on a Python that knows them (3.14: Unicode 16)."""
    gen = _load()
    from mcp_synology.modules.filestation.decompositions import NEW_DECOMPOSITIONS

    table = {ord(c): [ord(p) for p in parts] for c, parts in NEW_DECOMPOSITIONS.items()}
    rendered = gen.render("DerivedAge-18.0.0.txt", "UnicodeData-18.0.0.txt", table)
    assert gen.OUTPUT.read_text(encoding="utf-8") == rendered
