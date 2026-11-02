"""Tests for modules/filestation/helpers.py — path, size, polling, icons."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import SynologyError
from mcp_synology.core.formatting import format_size
from mcp_synology.modules.filestation.helpers import (
    correct_recycle_status_from_observation,
    ensure_known_shares,
    ensure_recycle_status,
    escape_multi_path,
    file_type_icon,
    matches_pattern,
    normalize_path,
    parse_human_size,
    parse_mtime,
    validate_additional,
    validate_share_path,
)
from tests.conftest import BASE_URL

if TYPE_CHECKING:
    from pathlib import Path

    from mcp_synology.core.client import DsmClient


class TestNormalizePath:
    def test_adds_leading_slash(self) -> None:
        assert normalize_path("video/test") == "/video/test"

    def test_preserves_leading_slash(self) -> None:
        assert normalize_path("/video/test") == "/video/test"

    def test_strips_trailing_slash(self) -> None:
        assert normalize_path("/video/test/") == "/video/test"

    def test_root_path(self) -> None:
        assert normalize_path("/") == "/"

    @pytest.mark.parametrize(
        ("path", "normalized"),
        [
            # Padding before a leading slash goes; nothing else is stripped (#488): DSM
            # keeps whitespace in a name exactly.
            ("  /video", "/video"),
            ("  /video  ", "/video  "),
            ("/video/x/ ", "/video/x/ "),
            ("/video/x/ /", "/video/x/ "),
            ("/video/foo ", "/video/foo "),
            ("/video/\tfoo\u00a0", "/video/\tfoo\u00a0"),
            # No leading slash: the whitespace is part of the first name.
            ("  video/x", "/  video/x"),
            ("   ", "/"),
            ("", "/"),
            # Slashes only: the root, not "" (pre-QA r2 on #510).
            ("//", "/"),
            ("///", "/"),
        ],
    )
    def test_takes_the_path_exactly(self, path: str, normalized: str) -> None:
        assert normalize_path(path) == normalized


class TestShown:
    """#489: a path in a message is shown as is, or with repr when padding or a character
    that doesn't print would otherwise be invisible."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/video/a b.mkv", "/video/a b.mkv"),
            ("/video/x", "/video/x"),
            ("/video/x/foo ", "'/video/x/foo '"),
            ("/video/ x/a", "'/video/ x/a'"),
            ("/video/x/ ", "'/video/x/ '"),
            ("/video/a\tb", "'/video/a\\tb'"),
            ("/video/a\u00a0b", "'/video/a\\xa0b'"),
            ("/video/\x1b[31m", "'/video/\\x1b[31m'"),
            # What the new-name rules call invisible is quoted too (pre-QA r5 on #510).
            ("/video/\u3164", "'/video/\\u3164'"),
            # A combining mark after a letter is part of it (NFD `á`): plain (QA r1 on #515).
            ("/video/a\u0301", "/video/a\u0301"),
            ("/video/\u0301a", repr("/video/\u0301a")),
            (
                "/video/\u0928\u092e\u0938\u094d\u0924\u0947",
                "/video/\u0928\u092e\u0938\u094d\u0924\u0947",
            ),
            ("/video/Favorites \u2764\ufe0f", "/video/Favorites \u2764\ufe0f"),
            ("/video/\U0001f468\u200d\U0001f469", "/video/\U0001f468\u200d\U0001f469"),
            ("/video/a\u200b", repr("/video/a\u200b")),
            ("/video/a\u202eb", repr("/video/a\u202eb")),
            ("/video/x\u200d", repr("/video/x\u200d")),
            ("/video/rep\u200dort.pdf", repr("/video/rep\u200dort.pdf")),
            ("/video/report.pdf\ufe0f", "'/video/report.pdf\\ufe0f'"),
            ("/video/1\ufe0f\u20e3", "/video/1\ufe0f\u20e3"),
            ("/video/\u2800x", "'/video/\\u2800x'"),
            # The same Unicode-based rule as new names (#515): an invisible character inside a
            # name is quoted; one where Unicode uses it isn't; newer emoji aren't unassigned.
            ("/video/rep\u00adort.pdf", repr("/video/rep\u00adort.pdf")),
            ("/video/rep\u200bort.pdf", repr("/video/rep\u200bort.pdf")),
            ("/video/ab\u200ccd", repr("/video/ab\u200ccd")),
            ("/video/\u0d23\u0d4d\u200d", "/video/\u0d23\u0d4d\u200d"),
            ("/video/\U0001fae9", "/video/\U0001fae9"),
            ("/video/\U000f0000", repr("/video/\U000f0000")),
            # Quoted, a character that draws nothing is written out, which repr alone
            # leaves as is (pre-QA on #515); interlinear annotation characters count.
            ("/video/report\u3164.pdf", "'/video/report\\u3164.pdf'"),
            ("/video/a\U000e0067", "'/video/a\\U000e0067'"),
            ("/video/x\ufffa", "'/video/x\\ufffa'"),
        ],
    )
    def test_shown(self, path: str, expected: str) -> None:
        from mcp_synology.modules.filestation.helpers import shown

        assert shown(path) == expected


class TestInvisibleProblem:
    """#515: Unicode's own definitions decide what hides (Default_Ignorable_Code_Point,
    UTS #39 joiner contexts, UTS #51 emoji sequences), not a list of ours."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("", None),
            ("plain", None),
            # A tag belongs only to a complete subdivision flag.
            ("\U0001f3f4\U000e0067\U000e0062", "contains an invisible character (U+E0067)"),
            ("x\U000e0067\U000e007f", "contains an invisible character (U+E0067)"),
            # A Persian ZWNJ needs a letter that joins on its far side too (UTS #39 A1).
            ("\u0645\u06cc\u200c", "contains an invisible character (U+200C)"),
            ("\u0645\u06cc\u200c.txt", "contains an invisible character (U+200C)"),
            # VS16 after a keycap base only when the keycap follows.
            ("1\ufe0f", "contains an invisible character (U+FE0F)"),
            # An ideographic variation selector only after an ideograph.
            ("a\U000e0100", "contains an invisible character (U+E0100)"),
            # A Mongolian vowel separator only between Mongolian letters.
            ("\u1820\u180e\u1821", None),
            ("a\u180eb", "contains an invisible character (U+180E)"),
            # NFD first: a precomposed letter plus the same mark is still a repeat.
            ("\u00e9\u0301", "repeats a combining mark (U+0301), which draws as one"),
            # One character whose own decomposition repeats a mark isn't a repeat: Gurung
            # Khema U+16121 is U+1611E twice (pre-QA on #515), and typed out, the two are
            # that character written another way (QA round 5 on #515).
            ("\U00016100\U00016121", None),
            ("\U00016100\U0001611e\U0001611e", None),
            ("\U00016100\U0001611e\U0001611e\U0001611f", None),
            (
                "\U00016100\U00016121\U00016121",
                "repeats a combining mark (U+16121), which draws as one",
            ),
            (
                "\U00016100\U0001611e\U0001611e\U0001611e\U0001611e",
                "repeats a combining mark (U+1611E), which draws as one",
            ),
            # Only U+16121's own pair is exempt: a mark after U+16126 or U+16128 that
            # repeats its last one is a repeat, and so is U+16121 followed by U+16126, whose
            # NFD (U+1611E four times, then U+1611F) repeats U+16121 (QA round 6 on #515).
            (
                "\U00016100\U00016126\U0001611f",
                "repeats a combining mark (U+1611F), which draws as one",
            ),
            (
                "\U00016100\U00016128\U00016120",
                "repeats a combining mark (U+16120), which draws as one",
            ),
            (
                "\U00016100\U0001611e\U00016126\U00016126",
                "repeats a combining mark (U+16126), which draws as one",
            ),
            (
                "\U00016100\U00016121\U00016126",
                "repeats a combining mark (U+1611E, part of U+16121), which draws as one",
            ),
            # U+1611E exactly twice is U+16121; a third is a repeat however it's typed,
            # so U+16121 before U+16123 or after U+1611E is too (pre-QA on #515).
            (
                "\U00016100\U00016121\U00016123",
                "repeats a combining mark (U+1611E, part of U+16121), which draws as one",
            ),
            (
                "\U00016100\U00016121\U0001611e",
                "repeats a combining mark (U+1611E), which draws as one",
            ),
            (
                "\U00016100\U00016121\U00016121\U0001611f",
                "repeats a combining mark (U+16121), which draws as one",
            ),
            # A mark from inside a character is named with the character it's part of, so
            # the model can find it (QA round 6 and pre-QA on #515).
            (
                "\U00016100\U0001611e\U00016121\U0001611e",
                "repeats a combining mark (U+1611E), which draws as one",
            ),
            (
                "\u0f40\u0f73\u0f75",
                "repeats a combining mark (U+0F71, part of U+0F75), which draws as one",
            ),
            # A long run stops looking for a named copy past the longest decomposition.
            (
                "\u0f40" + "\u0f73" * 5,
                "repeats a combining mark (U+0F71, part of U+0F73), which draws as one",
            ),
            (
                "\U00016100\U00016122\U00016124",
                "repeats a combining mark (U+16129, part of U+16124), which draws as one",
            ),
            (
                "\U00016100\U0001611e\U0001611e\U0001611e",
                "repeats a combining mark (U+1611E), which draws as one",
            ),
            # A copy of several whole characters names them; one starting with U+1611E
            # is a repeat, not U+16121's pair (pre-QA on #515).
            (
                "a\u0344\u0300\u0344\u0300",
                "repeats combining marks (U+0344 U+0300), which draw as one",
            ),
            (
                "\U00016100\U00016123\U00016120\U00016123\U00016120",
                "repeats combining marks (U+16123 U+16120), which draw as one",
            ),
            # However it's typed: the same NFD, the same verdict (pre-QA on #515).
            (
                "a\U00016121\U0001611e\U0001611f",
                "repeats a combining mark (U+1611E), which draws as one",
            ),
            (
                "a\U0001611e\U00016126",
                "repeats a combining mark (U+1611E, part of U+16126), which draws as one",
            ),
            # A mark that decomposes to several, typed twice, puts no mark next to itself:
            # the run repeats (QA round 5 on #515). The same on every Python: U+16122 is
            # Unicode 16, decomposed from the generated table.
            ("\u03b9\u0344\u0344", "repeats a combining mark (U+0344), which draws as one"),
            ("\u0f40\u0f76\u0f76", "repeats a combining mark (U+0F76), which draws as one"),
            ("\u0f40\u0f93\u0f93", "repeats a combining mark (U+0F93), which draws as one"),
            ("a\U00016122\U00016122", "repeats a combining mark (U+16122), which draws as one"),
            (
                "a\u0308\u0301\u0308\u0301",
                "repeats combining marks (U+0308 U+0301), which draw as one",
            ),
            ("a\u0344\u0308\u0301", "repeats combining marks (U+0308 U+0301), which draw as one"),
            ("\u03b9\u0344", None),
            ("\u0390", None),
            # Canonical ordering puts U+0316 (class 220) before the two U+0301 (230), so
            # they're adjacent: a repeat, by `regex`'s classes on every Python. U+10EFD
            # (Unicode 15) is class 220 there; Python 3.11 called it 0 (pre-QA on #515).
            ("a\u0301\u0316\u0301", "repeats a combining mark (U+0301), which draws as one"),
            (
                "\u0628\u0301\U00010efd\u0301.txt",
                "repeats a combining mark (U+0301), which draws as one",
            ),
            # Interlinear annotation characters draw nothing, though not default-ignorable.
            ("re\ufffaport", "contains an invisible character (U+FFFA)"),
            # Egyptian hieroglyph format controls draw nothing on their own (pre-QA on #515).
            ("rep\U00013430ort", "contains an invisible character (U+13430)"),
            ("a\U0001343fb", "contains an invisible character (U+1343F)"),
            # VS15/VS16 only after an emoji emoji-variation-sequences.txt registers.
            ("\U0001f600\ufe0e", "contains an invisible character (U+FE0E)"),
            ("\U0001f600\ufe0f", "contains an invisible character (U+FE0F)"),
            ("\u2764\ufe0e", None),
            # VS1-VS7 only in sequences StandardizedVariants.txt registers (pre-QA on #515).
            ("caf\u00e9\ufe00.pdf", "contains an invisible character (U+FE00)"),
            ("\u0434\ufe01oc", "contains an invisible character (U+FE01)"),
            ("\u2229\ufe00", None),
            # Only selectors Unicode has registered: VS1-VS7 and VS17-VS48 (QA round 2 on
            # #515); the others are in no registered sequence and only hide a value.
            ("\u2229\ufe06", "contains an invisible character (U+FE06)"),  # unregistered pair
            ("\u2229\ufe07", "contains an invisible character (U+FE07)"),
            ("\u5b57\ufe07", "contains an invisible character (U+FE07)"),  # QA round 3
            ("\U0001f600\ufe0d", "contains an invisible character (U+FE0D)"),
            ("\u5b57\U000e011f", None),
            ("\u5b57\U000e0120", "contains an invisible character (U+E0120)"),
            ("\u5b57\U000e01ef", "contains an invisible character (U+E01EF)"),
            # A Mongolian selector needs a Mongolian letter before it: the selectors are
            # Mongolian-script themselves and chained into hidden runs (QA round 2 on #515).
            ("\u182e\u1823\u1829\u182d\u1823\u182f\u180b", None),
            ("\u182c\u1820\u1837\u180e\u1820", None),
            ("\u1820\u180b\u180e\u1820", None),
            ("\u1820\u180b\u180c", "contains an invisible character (U+180C)"),
            (
                "\u1820" + "\u180b\u180c\u180d\u180f" * 50 + "\u1820",
                "contains an invisible character (U+180C)",
            ),
            ("\u1820" + "\u180e" * 500 + "\u1820", "contains an invisible character (U+180E)"),
            ("\u1802\u180b", "contains an invisible character (U+180B)"),
            # The vowel separator needs a Mongolian letter on both sides.
            ("\u1820\u180e", "contains an invisible character (U+180E)"),
            ("report \u1820\u180e.pdf", "contains an invisible character (U+180E)"),
            ("\u1802\u180e\u1820", "contains an invisible character (U+180E)"),
            ("\u1820\u180e\u1802", "contains an invisible character (U+180E)"),
            (
                "report \u1802" + "\u180e\u180b\u180e\u180c" * 40 + "\u1802.pdf",
                "contains an invisible character (U+180E)",
            ),
            # Pins the Unicode data of regex's floor: ★ stopped being Extended_Pictographic
            # after 2024.11.6, so a ZWJ between two is refused (see the scaffolding spec).
            ("\u2605\u200d\u2605", "contains an invisible character (U+200D)"),
            ("\u5b57\ufe00", "contains an invisible character (U+FE00)"),  # unregistered pair
            ("\u8c48\ufe00", None),  # registered: CJK compatibility ideograph U+F900
        ],
    )
    def test_invisible_problem(self, name: str, expected: str | None) -> None:
        from mcp_synology.modules.filestation.helpers import invisible_problem

        assert invisible_problem(name) == expected

    def test_a_long_tag_run_is_linear(self) -> None:
        """Each tag used to rescan its run: 10,000 tags took 10 s (pre-QA on #515)."""
        import time

        from mcp_synology.modules.filestation.helpers import invisible_problem, shown

        run = "\U0001f3f4" + "\U000e0067" * 50_000 + "\U000e007f"
        start = time.monotonic()
        assert invisible_problem(run) == "contains an invisible character (U+E0067)"
        assert shown("/video/" + run).startswith("'/video/")
        assert time.monotonic() - start < 2

    @pytest.mark.parametrize(
        "name",
        ["\u0f40" + "\u0f73" * 84, "\u0f40\u0f73\u0f73\u0f40" + "\u0f73" * 78],
        ids=["one-run", "later-run"],
    )
    def test_a_long_run_of_repeats_is_capped(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After an unnamed match, the search looks only up to the longest decomposition,
        in that run and every later one: a letter and 84 x U+0F73 took 36 ms per name
        (pre-QA on #515), and U+0F73 twice, then 78 more in a second run, 28 ms (QA round 7
        on #515). The test counts the matches described rather than timing them, so
        coverage and slow CI runners don't matter (pre-QA on #515): uncapped, these names
        describe 3,000 or more."""
        from mcp_synology.modules.filestation import helpers

        described = 0
        real = helpers._describe_repeat

        def counting(*args: Any) -> tuple[str, bool]:
            nonlocal described
            described += 1
            return real(*args)

        monkeypatch.setattr(helpers, "_describe_repeat", counting)
        assert len(name.encode()) <= 255
        assert helpers.invisible_problem(name) is not None
        assert 0 < described < 1000

    @pytest.mark.parametrize(
        "name",
        [
            "\u094d" * 40_000 + "\u200c\u0915",
            "\u094d" * 40_000 + "\u200d",
            "\u0644" + "\u064b" * 40_000 + "\u200c\u0644",
        ],
    )
    def test_a_long_mark_run_before_a_joiner_is_linear(self, name: str) -> None:
        """The joiner lookbehinds rescanned an unbounded run of marks: 40,000 took 9 s
        (QA round 2 on #515)."""
        import time

        from mcp_synology.modules.filestation.helpers import invisible_problem, shown

        start = time.monotonic()
        invisible_problem(name)
        shown("/video/" + name)
        assert time.monotonic() - start < 1

    def test_variant_selectors_are_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """At most 4 characters per name in a variant form; emoji VS16 and Mongolian
        selectors don't count (maintainer, QA round 2 on #515). The message names each
        one, since the selectors can't be seen."""
        from mcp_synology.modules.filestation import helpers

        assert helpers.SELECTOR_LIMIT == 4
        four = "\u8f9b\U000e0100\u845b\U000e0101\u90ce\U000e0100\u6f22\ufe00"
        assert helpers.invisible_problem(four) is None
        five = four + "\u5b57\U000e0100"
        assert helpers.invisible_problem(five) == (
            "has 5 variation selectors (\u8f9b U+E0100, \u845b U+E0101, \u90ce U+E0100, "
            "\u6f22 U+FE00, \u5b57 U+E0100), more than the 4 a name may have"
        )
        assert helpers.invisible_problem("\u2764\ufe0f" * 10) is None
        assert helpers.invisible_problem("\u1820\u180b\u1821\u180c" * 4) is None
        monkeypatch.setattr(helpers, "SELECTOR_LIMIT", 0)
        assert helpers.invisible_problem("\u5b57\U000e0100") == (
            "has 1 variation selector (\u5b57 U+E0100), more than the 0 a name may have"
        )

    def test_every_emoji_variation_sequence_passes(self) -> None:
        from mcp_synology.modules.filestation.helpers import invisible_problem
        from mcp_synology.modules.filestation.standardized_variants import EMOJI_VS_BASES

        refused = [
            base + vs
            for base in EMOJI_VS_BASES
            for vs in "\ufe0e\ufe0f"
            if invisible_problem(base + vs) is not None
        ]
        assert refused == []
        assert len(EMOJI_VS_BASES) > 300

    def test_every_standardized_variant_passes(self) -> None:
        """Every VS1-VS7 sequence StandardizedVariants.txt registers, but after ASCII."""
        from mcp_synology.modules.filestation.helpers import invisible_problem
        from mcp_synology.modules.filestation.standardized_variants import STANDARDIZED_BASES

        refused = [
            (base, vs)
            for vs, bases in STANDARDIZED_BASES.items()
            for base in bases
            if not base.isascii() and invisible_problem(base + vs) is not None
        ]
        assert refused == []
        assert sum(map(len, STANDARDIZED_BASES.values())) > 1000
        assert invisible_problem("0\ufe00") == "contains an invisible character (U+FE00)"

    def test_nfd_matches_unicodedata(self) -> None:
        """`_nfd_with_sources` is NFD: mixed combining classes (220/230, Tibetan 129/130,
        Hebrew), precomposed letters and Hangul, all known to Python 3.11, come out as
        `unicodedata` gives them, in canonical order (QA round 5 on #515)."""
        import unicodedata

        from mcp_synology.modules.filestation.helpers import _nfd_with_sources

        names = [
            "a\u0301\u0316\u0300\u0323",
            "\u00e9\u0316\u0301",
            "\u0f40\u0f72\u0f71\u0f74\u0f71",
            "\u0f40\u0f73\u0f75\u0f81",
            "\u05d1\u05bc\u05b8\u05b0\u05bc",
            "\u1e09\u0323\u01d5",
            "\ud55c\uae00\u1100\u1161",
            "\u03b9\u0344\u0345\u0314",
            "x\u0315\u0300\u05ae\u0300\u0301\u302a",
        ]
        for name in names:
            assert "".join(d for d, _ in _nfd_with_sources(name)) == unicodedata.normalize(
                "NFD", name
            ), name

    def test_decompositions_are_the_same_on_every_python(self) -> None:
        """Every canonical decomposition the running Python knows matches `_decompose`,
        and every mark one repeats is U+16121's exempt pair, so Python
        3.11 (Unicode 14) and 3.14 (Unicode 16) decompose and judge names alike (QA
        round 5 on #515)."""
        import sys
        import unicodedata

        from mcp_synology.modules.filestation import helpers
        from mcp_synology.modules.filestation.decompositions import NEW_DECOMPOSITIONS

        for cp in range(sys.maxunicode + 1):
            char = chr(cp)
            if not unicodedata.decomposition(char) or unicodedata.decomposition(char).startswith(
                "<"
            ):
                continue
            parts = helpers._decompose(char)
            assert parts == unicodedata.normalize("NFD", char), hex(cp)
            marks = [c for c in parts if helpers._MARK.match(c)]
            assert len(marks) <= helpers._LONGEST_DECOMPOSITION, hex(cp)
            for a, b in zip(parts, parts[1:], strict=False):
                if a == b and helpers._MARK.match(a):
                    # The only pair a decomposition repeats is U+16121's, which is exempt.
                    assert a * 2 in helpers._REPEATING_DECOMPOSITIONS, hex(cp)
        # The table's own entries too: CI's Pythons know none of them (QA round 7 on #515).
        for char in NEW_DECOMPOSITIONS:
            marks = [c for c in helpers._decompose(char) if helpers._MARK.match(c)]
            assert len(marks) <= helpers._LONGEST_DECOMPOSITION, hex(ord(char))
        assert helpers._REPEATING_DECOMPOSITIONS == {"\U0001611e\U0001611e": "\U00016121"}

    def test_no_character_repeats_a_mark_by_itself(self) -> None:
        """Every character with a canonical decomposition, alone (after a letter when it's
        a mark), passes: no decomposition is read as a repeat (pre-QA on #515)."""
        import sys
        import unicodedata

        from mcp_synology.modules.filestation import helpers

        refused = []
        for cp in range(sys.maxunicode + 1):
            char = chr(cp)
            decomposition = unicodedata.decomposition(char)
            if char in helpers.NEW_DECOMPOSITIONS or (
                decomposition and not decomposition.startswith("<")
            ):
                name = "a" + char if helpers._MARK.match(char) else char
                if helpers._repeated_marks(name) is not None:
                    refused.append(hex(cp))
        assert refused == []

    def test_canonically_equivalent_names_get_one_verdict(self) -> None:
        """Spellings of one name (precomposed, NFD and mixed) are all refused or all pass.

        Spelled out by hand: CI's Pythons (3.11 to 3.13) don't know Gurung Khema (Unicode
        16), so `unicodedata.normalize` can't produce these forms there; where it can,
        the test checks they really are one name (pre-QA on #515)."""
        import unicodedata

        from mcp_synology.modules.filestation.helpers import invisible_problem

        k, aa, i, ii = "\U00016100", "\U0001611e", "\U0001611f", "\U00016120"
        u, e, ai, o, au = "\U00016121", "\U00016123", "\U00016125", "\U00016126", "\U00016128"
        groups = [
            ["Caf\u00e9", "Cafe\u0301"],
            ["\u03b9\u0344", "\u03b9\u0308\u0301", "\u0390"],
            ["\u0f40\u0f73", "\u0f40\u0f71\u0f72"],
            ["a\u0344\u0344", "a\u0308\u0301\u0308\u0301", "a\u0344\u0308\u0301"],
            [k + u, k + aa + aa],
            [k + o, k + aa + aa + i, k + u + i],
            [k + au, k + aa + aa + ii, k + u + ii],
            [k + u + aa + i, k + aa + o, k + aa + aa + aa + i, k + aa + u + i],
            [k + u + e, k + aa + o, k + u + aa + i],
            [k + u + ai, k + aa + au, k + aa + aa + aa + ii],
            [k + u + u, k + aa * 4, k + aa + aa + u],
            [k + e + u, k + aa + i + aa + aa, k + e + aa + aa],
            [k + o + u, k + aa + aa + i + aa + aa, k + u + i + u],
        ]
        known = unicodedata.unidata_version >= "16"
        for group in groups:
            verdicts = {invisible_problem(form) is None for form in group}
            assert len(verdicts) == 1, group
            if known or all(ord(c) < 0x16100 for form in group for c in form):
                assert len({unicodedata.normalize("NFD", form) for form in group}) == 1, group

    def test_u_after_another_vowel_sign_passes(self) -> None:
        """U+16121 before a vowel sign that starts with U+1611E is refused (U+1611E three
        times in a row); after one it passes, since its NFD has no three in a row (QA
        round 7 on #515: the spec said "next to")."""
        from mcp_synology.modules.filestation.helpers import invisible_problem

        k, u = "\U00016100", "\U00016121"
        # Every vowel sign whose decomposition starts with U+1611E, except U+1611E itself
        # and U+16121 (three U+1611E in a row either way).
        for vowel in map(chr, (0x16122, 0x16123, 0x16125, 0x16126, 0x16127, 0x16128)):
            assert invisible_problem(k + vowel + u) is None, hex(ord(vowel))
            assert invisible_problem(k + u + vowel) is not None, hex(ord(vowel))

    def test_shown_skips_checking_a_part_longer_than_any_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check grows with the cube of a long run of different marks (each doubling
        costs up to 8 times as much; the stdlib NFD it once used was quadratic: QA round 3
        on #515). No NAS name is over 255 bytes, so such a part is quoted without the
        check (deterministic: a spy records whether the part was checked)."""
        from mcp_synology.modules.filestation import helpers

        checked: list[str] = []
        real = helpers.invisible_problem

        def spy(part: str) -> str | None:
            checked.append(part)
            return real(part)

        monkeypatch.setattr(helpers, "invisible_problem", spy)
        long_part = "a" + "\u0952\u0951" * 128  # 769 bytes
        assert helpers.shown("/video/" + long_part).startswith("'/video/a")
        assert long_part not in checked
        helpers.shown("/video/" + "a" * 255)
        assert "a" * 255 in checked

    def test_a_part_longer_than_any_name_is_quoted_escaped(self) -> None:
        """Unchecked, it's still escaped: ``repr`` would show U+3164 raw (QA round 4 on
        #515)."""
        from mcp_synology.modules.filestation.helpers import shown

        got = shown("/video/" + "a" * 300 + "\u3164")
        assert "\\u3164" in got
        assert "\u3164" not in got

    @staticmethod
    def _tags(text: str) -> str:
        return "".join(chr(0xE0000 + ord(c)) for c in text)

    @pytest.mark.parametrize("code", ["gbsct", "gbeng", "gbwls"])
    def test_a_subdivision_flag_passes(self, code: str) -> None:
        from mcp_synology.modules.filestation.helpers import invisible_problem

        assert invisible_problem("\U0001f3f4" + self._tags(code) + "\U000e007f") is None

    def test_flag_tags_need_the_flag(self) -> None:
        """A valid code after anything but 🏴 is hidden text too."""
        from mcp_synology.modules.filestation.helpers import invisible_problem

        name = "x" + self._tags("gbsct") + "\U000e007f"
        assert invisible_problem(name) == "contains an invisible character (U+E0067)"

    @pytest.mark.parametrize(
        "code",
        # A well-formed subdivision id too: only the three RGI flags pass, or several
        # flags could spell words (maintainer, QA round 2 on #515).
        ["ignore previous", "GBSCT", "gb", "gbscotland", "gb-sct", "usca", "fr75c", "ignore"],
    )
    def test_text_hidden_in_tags_is_refused(self, code: str) -> None:
        """Only the three RGI subdivision flags count; any other tag run is text hidden
        in the name ("ASCII smuggling", pre-QA on #515)."""
        from mcp_synology.modules.filestation.helpers import invisible_problem, shown

        name = "a\U0001f3f4" + self._tags(code) + "\U000e007f.pdf"
        assert (invisible_problem(name) or "").startswith("contains an invisible character")
        assert shown("/video/" + name).startswith("'/video/")


class TestValidateSharePath:
    def test_valid_share(self) -> None:
        result = validate_share_path("/video/test", {"video", "music"})
        assert result is None

    def test_unknown_share(self) -> None:
        result = validate_share_path("/unknown/test", {"video", "music"})
        assert result is not None
        assert "Unknown" in result
        assert "video" in result

    def test_empty_path(self) -> None:
        result = validate_share_path("/", {"video"})
        assert result is not None

    def test_recycle_path_rejected(self) -> None:
        result = validate_share_path("/#recycle/test", {"video"})
        assert result is not None
        assert "#recycle" in result

    def test_no_shares(self) -> None:
        result = validate_share_path("/video/test", set())
        assert result is not None
        assert "none" in result

    def test_messages_quote_with_repr(self) -> None:
        """A padded or tabbed name shows its whitespace once the JSON is decoded (#489);
        paths are taken exactly, so `/video /x` names a share "video " (#488)."""
        result = validate_share_path("/video /x", {"video"})
        assert result is not None
        assert result.startswith("Unknown shared folder 'video '. ")
        assert validate_share_path("/#recycle/\tx", {"video"}) == (
            "Invalid path '/#recycle/\\tx': must start with a shared folder name, not #recycle."
        )


class TestParseHumanSize:
    def test_integer_passthrough(self) -> None:
        assert parse_human_size(1024) == 1024

    def test_string_integer(self) -> None:
        assert parse_human_size("1024") == 1024

    def test_bytes(self) -> None:
        assert parse_human_size("500B") == 500

    def test_kilobytes(self) -> None:
        assert parse_human_size("1KB") == 1024

    def test_megabytes(self) -> None:
        assert parse_human_size("500MB") == 500 * 1024**2

    def test_gigabytes(self) -> None:
        assert parse_human_size("2GB") == 2 * 1024**3

    def test_terabytes(self) -> None:
        assert parse_human_size("1.5TB") == int(1.5 * 1024**4)

    def test_case_insensitive(self) -> None:
        assert parse_human_size("500mb") == 500 * 1024**2
        assert parse_human_size("500Mb") == 500 * 1024**2

    def test_decimal_values(self) -> None:
        assert parse_human_size("1.5GB") == int(1.5 * 1024**3)

    def test_with_spaces(self) -> None:
        assert parse_human_size("  500 MB  ") == 500 * 1024**2

    def test_binary_unit_names(self) -> None:
        assert parse_human_size("1KiB") == 1024
        assert parse_human_size("500MiB") == 500 * 1024**2
        assert parse_human_size("2GiB") == 2 * 1024**3
        assert parse_human_size("1.5TiB") == int(1.5 * 1024**4)
        assert parse_human_size("500mib") == 500 * 1024**2

    def test_kb_and_kib_mean_the_same(self) -> None:
        assert parse_human_size("500MB") == parse_human_size("500MiB")

    def test_formatted_size_parses_back(self) -> None:
        # What the tools print (format_size) is accepted as input unchanged.
        for size in (0, 1023, 1536 * 1024**2, int(1.5 * 1024**4), 1024**5, 3 * 1024**5, 1024**6):
            assert parse_human_size(format_size(size)) == size

    def test_invalid_binary_unit(self) -> None:
        for bad in ("500iB", "500KiiB", "500KBi"):
            with pytest.raises(ValueError, match="Invalid size"):
                parse_human_size(bad)

    def test_invalid_input(self) -> None:
        with pytest.raises(ValueError, match="Invalid size"):
            parse_human_size("not_a_size")

    def test_invalid_unit(self) -> None:
        with pytest.raises(ValueError, match="Invalid size"):
            parse_human_size("500EB")


class TestParseMtime:
    def test_calendar_date_treated_as_utc(self) -> None:
        # 2026-04-01 00:00:00 UTC = epoch 1775001600
        assert parse_mtime("2026-04-01") == 1775001600

    def test_iso_8601_with_offset(self) -> None:
        # 2026-04-01 12:00:00 UTC = epoch 1775044800
        assert parse_mtime("2026-04-01T12:00:00+00:00") == 1775044800

    def test_iso_8601_with_non_utc_offset(self) -> None:
        # 2026-04-01 12:00:00 UTC-05:00 = 17:00:00 UTC = epoch 1775062800
        assert parse_mtime("2026-04-01T12:00:00-05:00") == 1775062800

    def test_naive_iso_8601_treated_as_utc(self) -> None:
        # No tzinfo → UTC. Same epoch as the +00:00 case.
        assert parse_mtime("2026-04-01T12:00:00") == 1775044800

    def test_numeric_epoch_string_passthrough(self) -> None:
        assert parse_mtime("1775044800") == 1775044800

    def test_strips_whitespace(self) -> None:
        assert parse_mtime("  2026-04-01  ") == 1775001600

    def test_invalid_input(self) -> None:
        with pytest.raises(ValueError, match="Invalid mtime"):
            parse_mtime("not_a_date")

    def test_empty_string(self) -> None:
        with pytest.raises(ValueError, match="Invalid mtime"):
            parse_mtime("")


class TestFileTypeIcon:
    def test_directory_emoji(self) -> None:
        assert file_type_icon(True) == "\U0001f4c1"

    def test_directory_text(self) -> None:
        assert file_type_icon(True, style="text") == "[DIR]"

    def test_video_emoji(self) -> None:
        assert file_type_icon(False, "movie.mkv") == "\U0001f3ac"

    def test_video_text(self) -> None:
        assert file_type_icon(False, "movie.mp4", style="text") == "[VIDEO]"

    def test_generic_file_emoji(self) -> None:
        assert file_type_icon(False, "readme.txt") == "\U0001f4c4"

    def test_generic_file_text(self) -> None:
        assert file_type_icon(False, "readme.txt", style="text") == "[FILE]"


class TestEscapeMultiPath:
    def test_single(self) -> None:
        assert escape_multi_path(["/video/test"]) == "/video/test"

    def test_multiple(self) -> None:
        result = escape_multi_path(["/video/a", "/music/b"])
        assert result == "/video/a,/music/b"

    def test_comma_escape(self) -> None:
        result = escape_multi_path(["/video/a,b"])
        assert result == "/video/a\\,b"


class TestValidateAdditional:
    """Closes #41: `additional` field whitelist enforcement.

    DSM silently accepts unknown values (the field just doesn't appear in the
    response), so a typo like `"sze"` would never produce a visible error.
    `validate_additional` rejects unknown values up-front with a clear
    ToolError naming the bad value and listing the supported set.
    """

    @pytest.mark.parametrize(
        "value",
        [
            None,
            [],
            ["size"],
            ["size", "time"],
            ["real_path", "size", "owner", "perm"],
            ["mount_point_type"],
            ["type"],
        ],
    )
    def test_accepts_valid_or_empty(self, value: list[str] | None) -> None:
        validate_additional(value, tool_name="Get file info")  # no exception

    @pytest.mark.parametrize(
        "value,bad",
        [
            (["sze"], "sze"),
            (["size", "tme"], "tme"),
            (["mount_point_type", "junk"], "junk"),
            (["", "size"], ""),
            # get_file_info never shows volume_status (only list_share returns it),
            # so it's rejected rather than silently dropped (QA R1 on #240).
            (["volume_status"], "volume_status"),
        ],
    )
    def test_rejects_unknown(self, value: list[str], bad: str) -> None:
        with pytest.raises(ToolError) as exc:
            validate_additional(value, tool_name="Get file info")
        body = str(exc.value)
        assert "Get file info failed" in body
        assert bad in body

    def test_suggestion_lists_exactly_what_get_file_info_shows(self) -> None:
        import json

        with pytest.raises(ToolError) as exc:
            validate_additional(["bogus"], tool_name="Get file info")
        suggestion = json.loads(str(exc.value))["error"]["suggestion"]
        assert suggestion == (
            "Supported fields: mount_point_type, owner, perm, real_path, size, time, type."
        )


class TestMatchesPattern:
    def test_glob_match(self) -> None:
        assert matches_pattern("Severance.S02E10.mkv", "*.mkv")

    def test_glob_no_match(self) -> None:
        assert not matches_pattern("Severance.S02E10.mkv", "*.srt")

    def test_case_insensitive(self) -> None:
        assert matches_pattern("FILE.MKV", "*.mkv")

    def test_wildcard(self) -> None:
        assert matches_pattern("Severance.S02E10.mkv", "*Severance*")

    def test_question_mark_is_one_character(self) -> None:
        assert matches_pattern("ep1.mkv", "ep?.mkv")
        assert not matches_pattern("ep10.mkv", "ep?.mkv")

    def test_brackets_match_themselves(self) -> None:
        # #396: fnmatch read [...] as a character class.
        assert matches_pattern("[a]x.mkv", "[a]x.mkv")
        assert not matches_pattern("ax.mkv", "*[a]*")
        assert not matches_pattern("Show.1080.mkv", "*[1080p]*")
        assert matches_pattern("Show [1080p].mkv", "*[1080p]*")

    def test_other_regex_characters_match_themselves(self) -> None:
        assert matches_pattern("a+b (1).txt", "a+b (1).txt")
        assert not matches_pattern("aab (1).txt", "a+b (1).txt")
        assert matches_pattern("x.y", "x.y")
        assert not matches_pattern("xzy", "x.y")

    def test_bare_word_matches_only_the_whole_name(self) -> None:
        # The tool description says so: unlike DSM's `pattern`, no "anywhere" match.
        assert not matches_pattern("Show.sample.mkv", "sample")
        assert matches_pattern("sample", "SAMPLE")

    def test_comma_is_one_pattern_not_alternatives(self) -> None:
        assert not matches_pattern("a.nfo", "*.nfo,*.txt")
        assert not matches_pattern("a.txt", "*.nfo,*.txt")
        assert matches_pattern("a.nfo,b.txt", "*.nfo,*.txt")

    def test_many_stars_do_not_backtrack(self) -> None:
        # QA round 1 on #459: a regex with one `.*` per `*` took 427 ms per 100-character
        # name for `*a*a*a*a*b`, and grows with each star; fnmatch's translation doesn't.
        import time

        # Sized so the old matcher fails in about 2 s instead of hanging (QA round 2).
        start = time.perf_counter()
        for _ in range(5):
            assert not matches_pattern("a" * 100, "*a*a*a*a*b")
        assert time.perf_counter() - start < 0.5

    def test_star_spans_a_newline(self) -> None:
        # As fnmatch did: a name with a newline still matches "*".
        assert matches_pattern("a\nb.txt", "a*.txt")


# ---------- Recycle-bin probe + self-correct (closes #37) ----------


class TestEnsureRecycleStatus:
    """Lazy per-share recycle-bin probe with caching."""

    @pytest.mark.asyncio
    async def test_returns_cached_value_without_probing(self) -> None:
        client = AsyncMock()
        recycle_status = {"video": True, "music": False}

        result_video = await ensure_recycle_status(client, "video", recycle_status)
        result_music = await ensure_recycle_status(client, "music", recycle_status)

        assert result_video is True
        assert result_music is False
        client.request.assert_not_called()  # cache hit; no probe

    @pytest.mark.asyncio
    async def test_probe_success_caches_true(self) -> None:
        client = AsyncMock()
        client.request.return_value = {"files": []}  # any non-error response
        recycle_status: dict[str, bool] = {}

        result = await ensure_recycle_status(client, "video", recycle_status)

        assert result is True
        assert recycle_status == {"video": True}
        client.request.assert_awaited_once_with(
            "SYNO.FileStation.List",
            "list",
            params={"folder_path": "/video/#recycle", "limit": 1},
        )

    @pytest.mark.asyncio
    async def test_missing_bin_folder_is_off_but_not_cached(self) -> None:
        """#409: no #recycle, and the share's setting is admin-only (105): off for this
        call, checked again next time, since DSM re-creates #recycle at the next delete."""
        client = AsyncMock()
        client.request.side_effect = [
            SynologyError("not found", code=408),
            SynologyError("permission denied", code=105),
        ]
        recycle_status: dict[str, bool] = {}

        result = await ensure_recycle_status(client, "scratch", recycle_status)

        assert result is False
        assert recycle_status == {}
        assert client.request.await_args_list[1].args == ("SYNO.Core.Share", "list")
        assert client.request.await_args_list[1].kwargs == {
            "params": {"shareType": "all", "additional": '["recyclebin"]'}
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled", [True, False])
    async def test_missing_bin_folder_uses_the_share_setting(self, enabled: bool) -> None:
        """An admin reads the share's own setting: a definite answer, cached."""
        client = AsyncMock()
        shares = {
            "shares": [
                {"name": "other", "enable_recycle_bin": not enabled},
                {"name": "scratch", "enable_recycle_bin": enabled},
            ]
        }
        client.request.side_effect = [SynologyError("not found", code=408), shares]
        recycle_status: dict[str, bool] = {}

        assert await ensure_recycle_status(client, "scratch", recycle_status) is enabled
        assert recycle_status == {"scratch": enabled}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled", [True, False])
    async def test_home_uses_the_user_home_setting(self, enabled: bool) -> None:
        """/home's bin is the user home service's setting, which the `homes` share's own
        flag can contradict (vdsm). A new account's home has no #recycle yet."""
        client = AsyncMock()
        home = {"enable": True, "enable_recycle_bin": enabled}
        client.request.side_effect = [SynologyError("not found", code=408), home]
        recycle_status: dict[str, bool] = {}

        assert await ensure_recycle_status(client, "home", recycle_status) is enabled
        assert recycle_status == {"home": enabled}
        assert client.request.await_args_list[1].args == ("SYNO.Core.User.Home", "get")

    @pytest.mark.asyncio
    async def test_share_not_in_the_setting_list_is_not_cached(self) -> None:
        client = AsyncMock()
        client.request.side_effect = [
            SynologyError("not found", code=408),
            {"shares": [{"name": "other", "enable_recycle_bin": True}]},
        ]
        recycle_status: dict[str, bool] = {}

        assert await ensure_recycle_status(client, "scratch", recycle_status) is False
        assert recycle_status == {}

    @pytest.mark.asyncio
    async def test_probe_permission_denied_answers_true_uncached_with_warning(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("mcp_synology.modules.filestation.helpers._warned_recycle", set())
        client = AsyncMock()
        client.request.side_effect = SynologyError("permission denied", code=105)
        recycle_status: dict[str, bool] = {}

        with caplog.at_level(logging.WARNING, logger="mcp_synology.modules.filestation.helpers"):
            result = await ensure_recycle_status(client, "admin_only", recycle_status)

        assert result is True
        assert recycle_status == {}  # can't tell: checked again next time (#255)
        assert any(
            "Recycle-bin probe on /admin_only" in r.getMessage()
            and "DSM 105 (permission denied)" in r.getMessage()
            and "Grant the MCP service account" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_probe_unknown_error_answers_true_uncached_with_warning(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("mcp_synology.modules.filestation.helpers._warned_recycle", set())
        client = AsyncMock()
        client.request.side_effect = SynologyError("server confused", code=500)
        recycle_status: dict[str, bool] = {}

        with caplog.at_level(logging.WARNING, logger="mcp_synology.modules.filestation.helpers"):
            result = await ensure_recycle_status(client, "weird", recycle_status)

        assert result is True
        assert recycle_status == {}
        assert any("DSM error 500" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_cant_tell_warns_once_per_share(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#255: an uncached share is probed on every use, so it warns only the first time."""
        monkeypatch.setattr("mcp_synology.modules.filestation.helpers._warned_recycle", set())
        client = AsyncMock()
        client.request.side_effect = SynologyError("permission denied", code=105)
        with caplog.at_level(logging.DEBUG, logger="mcp_synology.modules.filestation.helpers"):
            for _ in range(3):
                await ensure_recycle_status(client, "locked", {})
        probes = [r for r in caplog.records if "Recycle-bin probe on /locked" in r.getMessage()]
        assert [r.levelno for r in probes] == [logging.WARNING, logging.DEBUG, logging.DEBUG]
        assert client.request.await_count == 3

    @pytest.mark.asyncio
    async def test_memoization_second_call_skips_probe(self) -> None:
        """Second call against the same share must not re-issue the List request."""
        client = AsyncMock()
        client.request.return_value = {"files": []}
        recycle_status: dict[str, bool] = {}

        await ensure_recycle_status(client, "video", recycle_status)
        await ensure_recycle_status(client, "video", recycle_status)

        # Probe ran exactly once despite two ensure calls.
        client.request.assert_awaited_once()


class TestCorrectRecycleStatusFromObservation:
    """In-place cache update when DSM behavior contradicts the cached bool."""

    def test_bin_folder_missing_drops_a_cached_true_and_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#409: a missing #recycle doesn't prove the bin is off, so the share is checked
        again on next use instead of being cached as off."""
        recycle_status = {"video": True}
        with caplog.at_level(logging.INFO, logger="mcp_synology.modules.filestation.helpers"):
            correct_recycle_status_from_observation(
                "video", observed_enabled=False, recycle_status=recycle_status
            )
        assert recycle_status == {}
        assert any(
            "Self-correcting recycle-bin cache on /video" in r.getMessage() for r in caplog.records
        )

    def test_bin_folder_seen_turns_a_cached_false_on(self) -> None:
        recycle_status = {"video": False}
        correct_recycle_status_from_observation(
            "video", observed_enabled=True, recycle_status=recycle_status
        )
        assert recycle_status == {"video": True}

    def test_agreement_is_no_op(self, caplog: pytest.LogCaptureFixture) -> None:
        recycle_status = {"video": True}
        with caplog.at_level(logging.INFO, logger="mcp_synology.modules.filestation.helpers"):
            correct_recycle_status_from_observation(
                "video", observed_enabled=True, recycle_status=recycle_status
            )
        assert recycle_status == {"video": True}
        # No "Self-correcting" log when cache agrees.
        assert not any(
            "Self-correcting" in r.getMessage()
            for r in caplog.records
            if r.name == "mcp_synology.modules.filestation.helpers"
        )

    @pytest.mark.parametrize(("observed", "cached"), [(False, {}), (True, {"scratch": True})])
    def test_missing_key(self, observed: bool, cached: dict[str, bool]) -> None:
        recycle_status: dict[str, bool] = {}
        correct_recycle_status_from_observation(
            "scratch", observed_enabled=observed, recycle_status=recycle_status
        )
        assert recycle_status == cached


class TestWithListedShareName:
    """#353: the share is renamed to its listed spelling; anything else is left alone."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/home/a", "/home/a"),  # already listed
            ("/Home/a/b", "/home/a/b"),  # another case
            ("HOME", "/home"),  # no leading slash
            ("/other/a", "/other/a"),  # not a listed share: left for the caller
            ("/", "/"),
        ],
    )
    def test_listed_spelling(self, path: str, expected: str) -> None:
        from mcp_synology.modules.filestation.helpers import _with_listed_share_name

        assert _with_listed_share_name(path, {"home", "video"}) == expected


class TestEnsureKnownShares:
    """#160: validate the share name against the NAS's share list before any DSM call."""

    @staticmethod
    def _route(share_lists: list[list[str]], calls: list[str]) -> None:
        answers = iter(share_lists)

        def side_effect(request: httpx.Request) -> httpx.Response:
            method = dict(request.url.params).get("method", "")
            calls.append(method)
            if method == "list_share":
                names = next(answers)
                shares = [{"name": n, "path": f"/{n}"} for n in names]
                return httpx.Response(200, json={"success": True, "data": {"shares": shares}})
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

    @respx.mock
    async def test_unknown_share_is_not_found_with_available_shares(
        self, mock_client: DsmClient
    ) -> None:
        calls: list[str] = []
        self._route([["testshare", "writable"], ["testshare", "writable"]], calls)
        with pytest.raises(ToolError) as exc_info:
            await ensure_known_shares(mock_client, ["/video/downloads/*"], set(), "path")
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "not_found"
        assert body["error"]["retryable"] is False
        assert "video" in body["error"]["message"]
        assert "testshare, writable" in body["error"]["message"]
        assert "list_shares" in body["error"]["message"]

    @respx.mock
    @pytest.mark.parametrize(
        "path",
        [
            "/writable/..",
            "/writable/../testshare/x",
            "/writable/.",
            "/writable/./",
            "writable/sub/../x",
            "/..",
            "/./writable",
            "/writable/ .. /x",  # padded: DSM's trimming is unprobed (pre-QA, #470)
        ],
    )
    async def test_dot_parts_are_refused_before_dsm(
        self, mock_client: DsmClient, path: str
    ) -> None:
        # #449: DSM refuses them too (418), but only after the share check, which read
        # just the first part. Nothing is sent, even when the share list is cached.
        calls: list[str] = []
        self._route([["testshare", "writable"]], calls)
        with pytest.raises(ToolError) as exc_info:
            await ensure_known_shares(mock_client, ["/testshare/ok", path], set(), "paths")
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "invalid_parameter"
        assert error["param"] == "paths"
        assert error["value"] == path
        assert error["message"] == f"Invalid path {path!r}: '.' and '..' parts aren't allowed."
        assert calls == []

    @respx.mock
    @pytest.mark.parametrize(
        "path", ["/writable/.hidden", "/writable/a..b", "/writable/...", "/writable/x."]
    )
    async def test_dots_inside_a_name_pass(self, mock_client: DsmClient, path: str) -> None:
        calls: list[str] = []
        self._route([["testshare", "writable"]], calls)
        assert await ensure_known_shares(mock_client, [path], set(), "path") == [path]

    @respx.mock
    async def test_errors_name_each_paths_argument(self, mock_client: DsmClient) -> None:
        # copy_files and move_files check paths and dest_folder together: an error names
        # the one at fault (not_found used to say "path" for every argument).
        calls: list[str] = []
        self._route([["testshare", "writable"], ["testshare", "writable"]], calls)
        params = ["paths", "paths", "dest_folder"]
        with pytest.raises(ToolError) as exc_info:
            await ensure_known_shares(
                mock_client, ["/testshare/a", "/writable/b", "/writable/../x"], set(), params
            )
        assert json.loads(str(exc_info.value))["error"]["param"] == "dest_folder"
        with pytest.raises(ToolError) as exc_info:
            await ensure_known_shares(
                mock_client, ["/testshare/a", "/writable/b", "/nope/x"], set(), params
            )
        error = json.loads(str(exc_info.value))["error"]
        assert (error["code"], error["param"]) == ("not_found", "dest_folder")

    @respx.mock
    async def test_dot_parts_refused_when_shares_cannot_be_listed(
        self, mock_client: DsmClient
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 105}}
        )
        with pytest.raises(ToolError, match="'.' and '..' parts"):
            await ensure_known_shares(mock_client, ["/video/.."], set(), "path")

    @respx.mock
    async def test_known_share_passes_and_cache_is_reused(self, mock_client: DsmClient) -> None:
        calls: list[str] = []
        self._route([["testshare", "writable"]], calls)
        cache: set[str] = set()
        await ensure_known_shares(mock_client, ["/writable/a", "writable/b"], cache, "path")
        await ensure_known_shares(mock_client, ["/testshare/c"], cache, "path")
        assert cache == {"testshare", "writable"}
        assert calls.count("list_share") == 1

    @respx.mock
    async def test_new_share_found_after_one_refresh(self, mock_client: DsmClient) -> None:
        calls: list[str] = []
        self._route([["writable", "media"]], calls)
        cache = {"writable"}  # filled before "media" existed
        await ensure_known_shares(mock_client, ["/media/x"], cache, "path")
        assert "media" in cache
        assert calls.count("list_share") == 1

    @respx.mock
    async def test_share_list_failure_does_not_block(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 105}}
        )
        cache: set[str] = set()
        await ensure_known_shares(
            mock_client, ["/whatever/x"], cache, "path"
        )  # DSM decides instead
        assert cache == set()

    @respx.mock
    async def test_no_paths_makes_no_request(self, mock_client: DsmClient) -> None:
        # e.g. upload_file with no dest_folder and no default_upload_dir configured
        calls: list[str] = []
        self._route([["writable"]], calls)
        cache: set[str] = set()
        assert await ensure_known_shares(mock_client, [], cache, "path") == []
        assert calls == [] and cache == set()

    @respx.mock
    async def test_share_names_match_case_insensitively(self, mock_client: DsmClient) -> None:
        """QA R1 (#173): DSM accepts /TESTSHARE/... for share "testshare" (DSM 7.2.2), so the
        check must too, without reloading the share list for a case difference."""
        calls: list[str] = []
        self._route([["testshare", "writable"]], calls)
        cache = {"testshare", "writable"}
        out = await ensure_known_shares(
            mock_client, ["/TESTSHARE/Documents", "/Writable/x"], cache, "path"
        )
        assert calls == []
        # Sent as listed (#353): DSM takes /TESTSHARE, but not /Home for the home service.
        assert out == ["/testshare/Documents", "/writable/x"]

    @respx.mock
    async def test_paths_come_back_with_the_listed_share_name(self, mock_client: DsmClient) -> None:
        """#353: /Home and /HOME get 408 where /home lists (vdsm 7.1 and 7.4.1, seska 7.1.1)."""
        calls: list[str] = []
        self._route([["home", "homes", "Backups"]], calls)
        out = await ensure_known_shares(
            mock_client,
            ["Home", "/HOME/docs/a.txt", "homes/x/", "/backups", "/Backups/y", "home/z"],
            set(),
            "paths",
        )
        # Changed only where the share name differs; anything else is left as given.
        assert out == ["/home", "/home/docs/a.txt", "homes/x/", "/Backups", "/Backups/y", "home/z"]

    @respx.mock
    async def test_paths_come_back_unchanged_when_dsm_wont_list(
        self, mock_client: DsmClient
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 105}}
        )
        assert await ensure_known_shares(mock_client, ["/Home/x"], set(), "path") == ["/Home/x"]

    @respx.mock
    async def test_recycle_as_share_is_rejected(self, mock_client: DsmClient) -> None:
        calls: list[str] = []
        self._route([["writable"]], calls)
        with pytest.raises(ToolError):
            await ensure_known_shares(mock_client, ["/#recycle/x"], {"writable"}, "path")


class TestPathsTakenExactly:
    """#488: every File Station tool sends a path exactly as given, so `…/foo ` (a name with
    a trailing space) and `…/ ` (a name " ") reach the item they name on DSM, never `…/foo`
    or the folder above. DSM keeps such names exactly (vdsm DSM 7.1 to 7.4.1, probe on
    #488). Each request is answered 408, so the call stops after its first lookup."""

    @staticmethod
    def _sent(calls: list[str], exact: str) -> None:
        # A JSON-quoted list (getinfo) escapes nothing in these paths, so the exact path
        # appears verbatim in the request if, and only if, it was sent unstripped.
        assert any(exact in c for c in calls), calls

    @staticmethod
    def _record(calls: list[str]) -> None:
        def side_effect(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url.params) + request.content.decode("latin-1"))
            if request.url.params.get("path") in ('["/video/a"]', "/video/a"):
                # move_dest's source exists, so the destination is looked up next.
                files = [{"path": "/video/a", "name": "a", "isdir": False}]
                return httpx.Response(200, json={"success": True, "data": {"files": files}})
            return httpx.Response(200, json={"success": False, "error": {"code": 408}})

        respx.route().mock(side_effect=side_effect)

    @respx.mock
    @pytest.mark.parametrize("exact", ["/video/x/foo ", "/video/x/ ", "/video/x/\tfoo"])
    @pytest.mark.parametrize(
        "tool",
        [
            "list_files",
            "get_file_info",
            "get_dir_size",
            "search_files",
            "rename",
            "copy_source",
            "move_source",
            "move_dest",
            "delete_files",
            "download_file",
            "upload_file",
        ],
    )
    async def test_tool_sends_the_path_exactly(
        self, mock_client: DsmClient, tmp_path: Path, tool: str, exact: str
    ) -> None:
        from urllib.parse import unquote_plus

        from mcp_synology.modules.filestation import (
            listing,
            metadata,
            operations,
            search,
            transfer,
        )

        calls: list[str] = []
        self._record(calls)
        local = tmp_path / "f.txt"
        local.write_text("x")
        run = {
            "list_files": lambda: listing.list_files(mock_client, path=exact),
            "get_file_info": lambda: metadata.get_file_info(mock_client, paths=[exact]),
            "get_dir_size": lambda: metadata.get_dir_size(mock_client, path=exact),
            "search_files": lambda: search.search_files(
                mock_client, folder_path=exact, pattern="*.mkv"
            ),
            "rename": lambda: operations.rename(mock_client, path=exact, new_name="n"),
            "copy_source": lambda: operations.copy_files(
                mock_client, paths=[exact], dest_folder="/video/d"
            ),
            "move_source": lambda: operations.move_files(
                mock_client, paths=[exact], dest_folder="/video/d"
            ),
            "move_dest": lambda: operations.move_files(
                mock_client, paths=["/video/a"], dest_folder=exact
            ),
            "delete_files": lambda: operations.delete_files(mock_client, paths=[exact]),
            "download_file": lambda: transfer.download_file(
                mock_client, path=exact, dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            ),
            "upload_file": lambda: transfer.upload_file(
                mock_client, local_path=str(local), dest_folder=exact, allowed_roots=[tmp_path]
            ),
        }[tool]
        with pytest.raises(ToolError):
            await run()
        sent = [unquote_plus(c) for c in calls]
        self._sent(sent, exact)
