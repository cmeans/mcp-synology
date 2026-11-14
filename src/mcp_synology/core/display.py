"""How text from the NAS or this machine is shown in a reply, so nothing in it can hide or
pass for the reply's own structure (#697): Unicode's characters that draw nothing
(`invisible_problem`, #510, #515), `shown` for a NAS path, `shown_local` for a local one,
`json_quoted` for a name in a line of text (#695), and the quoting helpers they share.

Moved here from File Station's helpers so `core` can use them (`core/local_paths.py`).
Importers use `core.display` directly; `filestation.helpers` keeps no aliases."""

from __future__ import annotations

import contextlib
import json
import os
import re
import unicodedata

import regex

from mcp_synology.core.decompositions import NEW_DECOMPOSITIONS
from mcp_synology.core.formatting import RGI_FLAG_TAGS
from mcp_synology.core.standardized_variants import (
    EMOJI_VS_BASES,
    STANDARDIZED_BASES,
)

# -- invisible characters (#510) ----------------------------------------------------------
# Built on Unicode's own definitions and data files, not a judgment of ours (maintainer,
# 2026-10-31, after prior-art research on #515): Default_Ignorable_Code_Point
# (DerivedCoreProperties.txt) is Unicode's set of characters that draw nothing; UTS #39
# section 3.1.1.1 says where the zero-width joiner and non-joiner belong in words; UTS #51
# says where emoji sequences use a joiner, a variation selector or tag characters; UTS #39
# section 5.4 flags a repeated combining mark. The `regex` package carries current Unicode
# data (Python 3.11's `unicodedata` is Unicode 14, which calls newer emoji unassigned).

_IGNORABLE = regex.compile(r"\p{Default_Ignorable_Code_Point}")
_MARK = regex.compile(r"[\p{Mn}\p{Me}]")
_MARK_RUN = regex.compile(r"[\p{Mn}\p{Me}]{2,}")
_NOT_PRINTED = regex.compile(r"[\p{Cc}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}]")
_BRAILLE_BLANK = "\u2800"  # draws nothing, but Unicode doesn't call it default-ignorable

# UTS #39 section 3.1.1.1. $M1 is a nonspacing mark with a nonzero combining
# class. Not applied: the rule's single-script condition (a letter of another script
# beside the joiner); mixed-script look-alikes are a separate check (#516).
# Mark runs in the lookbehinds are bounded (`_MARKS`): unbounded, a long run of marks with
# no letter before it made each joiner rescan it, quadratic (QA round 2 on #515). Real
# letters carry a few marks, far below the bound.
_MARKS = "{0,32}"
_M1 = r"(?:(?!\p{ccc=0})\p{Mn})"
_ZWNJ_A1 = regex.compile(
    rf"(?<=[\p{{jt=D}}\p{{jt=L}}]\p{{jt=T}}{_MARKS})\u200c(?=[\p{{jt=D}}\p{{jt=R}}])"
)
_ZWNJ_A2 = regex.compile(
    rf"(?<=\p{{L}}\p{{Mn}}{_MARKS}\p{{ccc=Virama}}{_M1}{_MARKS})\u200c(?={_M1}{_MARKS}\p{{L}})"
)
_ZWJ_B = regex.compile(
    rf"(?<=\p{{L}}\p{{Mn}}{_MARKS}\p{{ccc=Virama}}{_M1}{_MARKS})\u200d"
    r"(?!\p{Indic_Syllabic_Category=Vowel_Dependent})"
)
# Two uses the Unicode core spec documents beyond UTS #39's patterns (pre-QA on #515): a ZWJ
# between a letter and a virama (Bengali ra-phala, `র<ZWJ><U+09CD>যালি`, chapter 12), and a
# ZWNJ between two Arabic-script letters whatever their joining type (Persian writes
# `در<ZWNJ>ها` with one after a right-joining letter, chapter 9; maintainer, 2026-10-31: real
# names come first).
_ZWJ_BEFORE_VIRAMA = regex.compile(rf"(?<=\p{{L}}\p{{Mn}}{_MARKS})\u200d(?=\p{{ccc=Virama}})")
_ZWNJ_ARABIC = regex.compile(
    rf"(?<=(?=\p{{scx=Arab}})\p{{L}}\p{{jt=T}}{_MARKS})\u200c(?=(?=\p{{scx=Arab}})\p{{L}})"
)
# UTS #51: a joiner between two emoji (👨<ZWJ>👩<ZWJ>👧, 🏳<VS16><ZWJ>🌈); VS15/VS16 after an
# emoji, or after a keycap base when the keycap follows (1<VS16><U+20E3>); the other variation
# selectors after the characters they vary (CJK ideographs, Mongolian letters, math symbols).
_EMOJI_ZWJ = regex.compile(
    r"(?<=[\p{Extended_Pictographic}\p{Emoji_Modifier}\ufe0f])\u200d(?=\p{Extended_Pictographic})"
)
# VS15/VS16 after a non-ASCII emoji: `_emoji_presentation` (only bases
# emoji-variation-sequences.txt registers; pre-QA on #515). After a keycap base, only when
# the keycap follows (1<VS16><U+20E3>).
_EMOJI_VS = regex.compile(r"(?<=[0-9#*])[\ufe0e\ufe0f](?=\u20e3)")
# Only the variation selectors Unicode has ever registered (StandardizedVariants.txt,
# IVD_Sequences.txt 2026-08-03): VS1-VS7 (U+FE00-FE06) and VS17-VS48 (U+E0100-E011F).
# VS8-VS14 and VS49-VS256 are in no registered sequence, so one only hides a value
# ("variation-selector smuggling", one byte per character; maintainer, QA round 2 on
# #515). VS1-VS7 only in a sequence StandardizedVariants.txt registers
# (`_standardized_variant`; a list of kinds of character missed fullwidth punctuation and
# math letters, pre-QA on #515); VS17-VS48 after an ideograph.
_OTHER_VS = regex.compile(
    r"(?<=\p{Ideographic})[\U000e0100-\U000e011f]"
    # A Mongolian selector after a Mongolian *letter*: the selectors are scx=Mong
    # themselves, so "after a Mongolian character" let them chain into a hidden run of any
    # length (QA round 2 on #515). The vowel separator sits between two letters, after
    # at most one selector.
    r"|(?<=(?=\p{scx=Mong})\p{L})[\u180b-\u180d\u180f]"
    r"|(?<=(?=\p{scx=Mong})\p{L}[\u180b-\u180d\u180f]?)\u180e(?=(?=\p{scx=Mong})\p{L})"
)
# A cap on the registered selectors that mark a character's variant (VS1-VS7 and the
# ideographic ones; not emoji VS15/VS16 or Mongolian's, which spelling uses freely): each
# can still carry about 5 bits, and a long run changes how a model tokenizes the name. 4
# covers a family and given name written with variant kanji (maintainer, QA round 2 on
# #515). A constant, not a setting (maintainer): a real name it refuses is a bug to report.
_COUNTED_VS = regex.compile(r"[\ufe00-\ufe06\U000e0100-\U000e011f]")
SELECTOR_LIMIT = 4


NAME_BYTES = 255  # the longest name ext4 and Btrfs (DSM's volumes) allow, in UTF-8 bytes

_FLAG = "\U0001f3f4"  # waving black flag: the base of a subdivision flag's tag sequence


def _is_tag(c: str) -> bool:
    return 0xE0020 <= ord(c) <= 0xE007F


def _tag_run_end(text: str, i: int) -> int:
    """The index just past the run of tag characters starting at ``text[i]``."""
    end = i
    while end < len(text) and _is_tag(text[end]):
        end += 1
    return end


# The three RGI subdivision flags' tags (UTS #51): `formatting.RGI_FLAG_TAGS`, shared with the
# reply escaping (#604).
_RGI_FLAG_TAGS = RGI_FLAG_TAGS


def _is_flag_tags(text: str, start: int, end: int) -> bool:
    """Whether ``text[start:end]``, a run of tags, makes a subdivision flag with the 🏴
    before it."""
    return start > 0 and text[start - 1] == _FLAG and text[start:end] in _RGI_FLAG_TAGS


def _ignorable_belongs(text: str, i: int) -> bool:
    """Whether ``text[i]``, a default-ignorable character, is where Unicode uses one: a
    joiner in a word (UTS #39) or an emoji sequence, a variation selector after the
    character it varies, a tag in a flag (UTS #51). Every other one (a zero-width space,
    a soft hyphen, a word joiner, a BOM, a filler, a combining grapheme joiner) hides.
    Tags are judged a run at a time by `invisible_problem`."""
    return (
        _standardized_variant(text, i)
        or _emoji_presentation(text, i)
        or any(p.match(text, i) for p in _BELONGS)
    )


def _emoji_presentation(text: str, i: int) -> bool:
    """Whether ``text[i]``, VS15 or VS16, follows a non-ASCII emoji
    emoji-variation-sequences.txt registers it for (``❤<VS16>``; not ``😀``+VS16, which has no
    text form)."""
    return text[i] in "\ufe0e\ufe0f" and i > 0 and text[i - 1] in EMOJI_VS_BASES


def _standardized_variant(text: str, i: int) -> bool:
    """Whether ``text[i]``, VS1-VS7, follows a character StandardizedVariants.txt
    registers it after; never an ASCII one (``0`` + VS1 looks like ``0``)."""
    bases = STANDARDIZED_BASES.get(text[i])
    return bases is not None and i > 0 and not text[i - 1].isascii() and text[i - 1] in bases


_BELONGS = (
    _ZWNJ_A1,
    _ZWNJ_A2,
    _ZWJ_B,
    _ZWJ_BEFORE_VIRAMA,
    _ZWNJ_ARABIC,
    _EMOJI_ZWJ,
    _EMOJI_VS,
    _OTHER_VS,
)
# Format characters that draw nothing on their own, though Unicode doesn't call them
# default-ignorable (pre-QA on #515): the interlinear annotation characters and the
# Egyptian hieroglyph format controls (quadrat layout; refused rather than judged in
# context, as no file name needs them). The other such format characters (prepended
# concatenation marks, U+0600-0605...) draw a sign.
_ANNOTATION = frozenset("\ufff9\ufffa\ufffb" + "".join(map(chr, range(0x13430, 0x13440))))


def draws_nothing(c: str) -> bool:
    """A character that draws nothing on its own: whitespace, a default-ignorable
    character, a combining mark, or the Braille blank (#510). A name of only these has no
    visible character."""
    return (
        c.isspace()
        or bool(_IGNORABLE.match(c) or _MARK.match(c))
        or c == _BRAILLE_BLANK
        or c in _ANNOTATION
    )


def invisible_problem(name: str) -> str | None:
    """Why ``name`` (one path part) hides a character, or None: a default-ignorable
    character where Unicode doesn't use one (`_ignorable_belongs`), the Braille blank at an
    end, a combining mark with no character to go on, the same combining mark or marks
    twice in a row (UTS #39 section 5.4: it draws as one), or more than `SELECTOR_LIMIT` variation
    selectors that mark a character's variant (an unregistered selector is refused as
    invisible). One rule for new names and `shown`
    (#510)."""
    if not name:
        return None
    # UTS #39 states its joiner contexts on NFC text; the patterns take any number of
    # marks, so they read decomposed text the same, and the name is checked as given.
    # A name in neither form (marks typed out of canonical order) can get another
    # verdict than its NFD from these and the selector checks; only the repeat check
    # reads the NFD (pre-QA on #515: such names are junk, not real spellings).
    text = name
    i = 0
    while i < len(text):
        c = text[i]
        if _is_tag(c):
            # A run of tags belongs only to a subdivision flag: 🏴, tags, the closing tag.
            # One look per run keeps a long run linear (pre-QA on #515).
            end = _tag_run_end(text, i)
            if not _is_flag_tags(text, i, end):
                return f"contains an invisible character (U+{ord(c):04X})"
            i = end
            continue
        if c in _ANNOTATION or (_IGNORABLE.match(c) and not _ignorable_belongs(text, i)):
            return f"contains an invisible character (U+{ord(c):04X})"
        i += 1
    variants = [m.start() for m in _COUNTED_VS.finditer(text)]
    if len(variants) > SELECTOR_LIMIT:
        # Name each one: the selectors themselves can't be seen (pre-QA on #515).
        which = ", ".join(f"{text[i - 1]} U+{ord(text[i]):04X}" for i in variants)
        return (
            f"has {len(variants)} variation selector{'s' if len(variants) != 1 else ''} "
            f"({which}), more than the "
            f"{SELECTOR_LIMIT} a name may have"
        )
    if _BRAILLE_BLANK in (text[0], text[-1]):
        return "starts or ends with a blank character (U+2800)"
    if _MARK.match(text[0]):
        return "starts with a combining mark, which has no character to go on"
    return _repeated_marks(name)


def _repeated_marks(name: str) -> str | None:
    """Why ``name``'s NFD has a combining mark, or a run of them, twice in a row, or None.

    A run counts, not just one mark: U+0344 is U+0308 U+0301, so U+0344 typed twice puts
    no mark next to itself (QA round 5 on #515). The verdict reads the NFD alone, so
    canonically equivalent names get one verdict however they were typed. The one pair
    that isn't a repeat is a character's own decomposition into one mark twice (U+16121
    is U+1611E U+1611E), typed out or not, and only where that mark appears exactly
    twice: a third U+1611E, or U+16121 twice, is a repeat (QA round 6 on #515).

    After a match that can only be named as part of a character, the search looks for a
    copy of whole characters only up to the longest decomposition, in that run and every
    later one, so the cost stays bounded (QA round 7 on #515). The verdict is the same; the
    reason may then name that first match rather than a longer named copy further on."""
    decomposed = _nfd_with_sources(name)
    nfd = "".join(d for d, _ in decomposed)
    first: str | None = None
    for low, high in _mark_runs(nfd):
        limit = (high - low) // 2
        if first is not None:
            # A run after one that already matched gets the same cap (QA round 7 on #515).
            limit = min(limit, _LONGEST_DECOMPOSITION)
        for size in range(1, limit + 1):
            if size > limit:
                break
            for at in range(low, high - 2 * size + 1):
                copy = nfd[at + size : at + 2 * size]
                if nfd[at : at + size] != copy:
                    continue
                if size == 1 and _own_pair(nfd, at, low, high):
                    continue
                sources = [i for _, i in decomposed[at + size : at + 2 * size]]
                reason, named = _describe_repeat(name, copy, sources)
                if named:
                    return reason
                if first is None:
                    # After the first match, look for a named one only up to the longest
                    # decomposition, so a long run of repeats isn't described match by
                    # match: a letter and 84 x U+0F73 (255 bytes) took 36 ms (pre-QA on #515).
                    first = reason
                    limit = min(limit, max(size, _LONGEST_DECOMPOSITION))
    return first


def _own_pair(nfd: str, at: int, low: int, high: int) -> bool:
    """Whether the pair at ``at`` is U+16121's decomposition standing alone: that mark
    exactly twice in a row, not three or more."""
    mark = nfd[at]
    return (
        mark * 2 in _REPEATING_DECOMPOSITIONS
        and (at == low or nfd[at - 1] != mark)
        and (at + 2 == high or nfd[at + 2] != mark)
    )


def _mark_runs(nfd: str) -> list[tuple[int, int]]:
    """The spans of two or more consecutive combining marks in ``nfd``."""
    return [match.span() for match in _MARK_RUN.finditer(nfd)]


def _describe_repeat(name: str, copy: str, sources: list[int]) -> tuple[str, bool]:
    """The reason for a repeated ``copy``, and whether it names what was typed.

    It names the characters typed when the copy is whole characters of the name;
    otherwise it names the marks and the characters they're part of, so the model
    can find them in its own text (pre-QA on #515: U+0F75 holds U+0F71)."""
    typed = [name[i] for i in dict.fromkeys(sources)]
    whole = "".join(_decompose(c) for c in typed) == copy

    def codes(chars: str | list[str]) -> str:
        return " ".join(f"U+{ord(c):04X}" for c in chars)

    part_of = "" if typed == list(copy) else f", part of {codes(typed)}"
    if len(copy) == 1:
        return f"repeats a combining mark ({codes(copy)}{part_of}), which draws as one", whole
    if whole and len(typed) == 1:
        return f"repeats a combining mark ({codes(typed)}), which draws as one", True
    if whole:
        return f"repeats combining marks ({codes(typed)}), which draw as one", True
    return f"repeats combining marks ({codes(copy)}{part_of}), which draw as one", whole


def _ccc_pattern() -> regex.Pattern[str]:
    names = []
    for n in range(1, 255):
        with contextlib.suppress(regex.error):
            regex.compile(rf"\p{{ccc={n}}}")
            names.append(rf"(?P<c{n}>\p{{ccc={n}}})")
    return regex.compile("|".join(names))


# Canonical combining classes from `regex`'s Unicode data, not the interpreter's: with
# `unicodedata.combining`, a mark Python 3.11 doesn't know (U+10EFD) sorted differently,
# so a name passed on 3.11 and was refused on 3.12 (pre-QA on #515).
_CCC = _ccc_pattern()


def _combining_class(char: str) -> int:
    match = _CCC.match(char)
    return int(match.lastgroup[1:]) if match and match.lastgroup else 0


def _decompose(char: str) -> str:
    """``char``'s full canonical decomposition, the same on every Python.

    Decompositions never change once a character is assigned, so `unicodedata` has them
    all up to its Unicode version (14.0 on Python 3.11); a character assigned since comes
    from the generated table, from the Unicode version of `regex`'s data. Without it,
    U+16121 was left as is on 3.11 and decomposed on 3.14 (QA round 5 on #515).
    """
    parts = NEW_DECOMPOSITIONS.get(char)
    if parts is None:
        return unicodedata.normalize("NFD", char)
    return "".join(_decompose(part) for part in parts)


def _repeating_decompositions() -> dict[str, str]:
    """The full decompositions that are one mark repeated, each with the character it
    composes to: only Gurung Khema U+16121 (U+1611E twice). U+16126 and U+16128 contain
    it and need nothing more (QA round 6 on #515); `test_helpers` checks no older
    character repeats a mark."""
    found = {}
    for char in NEW_DECOMPOSITIONS:
        parts = _decompose(char)
        if len(parts) > 1 and len(set(parts)) == 1 and _MARK.match(parts[0]):
            found[parts] = char
    return found


_REPEATING_DECOMPOSITIONS = _repeating_decompositions()
# The most marks one character decomposes to (3: Gurung Khema U+16126 and U+16128, and
# Greek letters such as U+1F82; `test_helpers` checks every decomposition against it).
_LONGEST_DECOMPOSITION = 3


def _nfd_with_sources(name: str) -> list[tuple[str, int]]:
    """NFD of ``name``, each character paired with the index of the one it came from.

    NFD is each character's own decomposition (`_decompose`), then canonical ordering: a
    stable sort by combining class within each run of non-zero classes, with the classes
    from the installed `regex`. The decompositions don't depend on the Python (the table is
    Unicode 18.0.0, the `regex` floor's data), so the result is the same on every Python.
    """
    out = [(d, i) for i, c in enumerate(name) for d in _decompose(c)]
    classes = [_combining_class(d) for d, _ in out]
    start = 0
    while start < len(out):
        if not classes[start]:
            start += 1
            continue
        end = start
        while end < len(out) and classes[end]:
            end += 1
        run = sorted(range(start, end), key=classes.__getitem__)
        out[start:end] = [out[k] for k in run]
        start = end
    return out


# How `invisible_problem`'s reasons start (the selector cap's: `is_selector_problem`):
# callers pick their advice by it without
# running the check again on an argument another check already refused (pre-QA on #515).
_INVISIBLE_REASONS = (
    "contains an invisible character",
    "starts or ends with a blank character",
    "starts with a combining mark",
    "repeats a combining mark",
    "repeats combining marks",
)


def is_selector_problem(problem: str) -> bool:
    """Whether ``problem`` is `invisible_problem`'s cap on variation selectors."""
    return problem.startswith("has ") and "variation selector" in problem


def is_invisible_problem(problem: str) -> bool:
    """Whether ``problem`` is a reason `invisible_problem` gives."""
    return problem.startswith(_INVISIBLE_REASONS) or is_selector_problem(problem)


def _hides(c: str) -> bool:
    """A character inside a path that a plain message would hide or mangle on its own: a
    control, unassigned or private-use character, a line separator, or whitespace other
    than a plain space (a tab, a no-break space)."""
    return bool(_NOT_PRINTED.match(c)) or (c != " " and c.isspace())


def escaped_repr(path: str) -> str:
    """``repr(path)``, with every character that draws nothing written as ``\\uXXXX`` too
    (``repr`` leaves U+3164 or VS16 as is), so the quoted path shows what's hiding."""
    out = []
    for c in repr(path):
        if _IGNORABLE.match(c) or c == _BRAILLE_BLANK:
            o = ord(c)
            out.append(f"\\u{o:04x}" if o <= 0xFFFF else f"\\U{o:08x}")
        else:
            out.append(c)
    return "".join(out)


def shown(path: str) -> str:
    """``path`` for a message: as is, or quoted (`escaped_repr`, with escapes) when a
    part has whitespace at its start or end, a character that doesn't print (a tab, a
    no-break space) or one `invisible_problem` refuses, so a stray space or a hidden
    character is visible once paths are taken exactly (#488, #489, #515). A path holding
    ``", "`` and nothing else to show is quoted without escapes, unless it holds a
    backslash (`plain_quoted`, #685, below); so is one that starts or ends with a quote,
    which would read as a quoted name (#691).

    The rule (#510): a refusal of an argument as given quotes it with ``!r`` (the caller's
    own text, shown exactly), or ``escaped_repr``, which also shows what ``!r`` leaves as is
    (`refuse_same_names`, `refuse_planted_bin`; the older ``!r`` refusals are on #458), but a
    path on this machine, refused or not, goes through `shown_local` (#691: repr doubles each
    Windows backslash); an
    error or list that names a path the server checked or got
    from DSM uses ``shown`` (plain unless something would be invisible or the path holds
    ``", "``), and so does a reply
    naming what a call acted on, successful or not: a delete's, copy's, move's or restore's
    summary and its skipped list (#519), a rename's, upload's, download's or create_folder's
    reply and get_dir_size's title (#688), since those are the paths a model will act on next.
    A folder written with its trailing slash is ``{shown(folder)}/``: the folder quoted, then
    the slash (#688).
    Listings of what's on the NAS (list_files, search_files, list_recycle_bin, get_file_info)
    show the names DSM returns as DSM does, escaped by the client (#604); their titles, which
    name the path the caller asked about, use ``shown`` (#690).

    A path holding ``", "`` is quoted too (#685): a folder named ``a, photos/family`` would
    read as two paths, ``/s/a`` and the share-relative ``photos/family`` the server accepts,
    where a path is written inline in a sentence or in `file_task`'s table cell, the one list
    still joined by ``", "``; every other list of paths is one a line (#693,
    `formatting.path_list`). A line that puts other text between or after paths (" and ",
    " (to ") quotes a path holding it with `shown_beside`."""
    if any(_needs_escapes(part) for part in path.split("/") if part):
        return escaped_repr(path)
    # Marks, joiners and variation selectors where Unicode uses them show as written
    # (`नमस्ते`, NFD `Café`, 👨<ZWJ>👩<ZWJ>👧, ❤<VS16>); anywhere else they're quoted
    # (`rep<ZWJ>ort.pdf`).
    if ", " in path or edge_quote(path):
        # Quoted only, with no escapes: nothing else needs showing, so what's inside the
        # quotes is the path exactly, as the server instructions tell a model (pre-QA r2 on
        # #692: repr doubled a backslash, `C:\new` read as `C:\\new`). A name that starts or
        # ends with a quote is quoted too, so `'a.jpg'` can't read as a quoted `a.jpg`
        # (pre-QA r4 on #691).
        return plain_quoted(path)
    return path


def json_quoted(text: str) -> str:
    """``text`` as a JSON string, for a name set in a line of other text (#695): a ``"`` in it
    is ``\\"``, so neither a quote nor the line's own separators inside it can read as the
    line's structure, and each character that draws nothing or a message would hide (a
    zero-width space, a no-break space, a private-use character) is written ``\\uXXXX``, as
    `escaped_repr` writes one, so ``"Ann<ZWSP>"`` can't pass for ``"Ann"``. Still valid
    JSON for the same text (one outside the BMP as its surrogate pair).

    By `shown`'s rule (#515): a name Unicode uses as written (an emoji ZWJ sequence, a
    Persian ZWNJ, ``❤<VS16>``) stays as is; escapes come only when `invisible_problem` refuses
    the name or it holds a character a message would hide, and then a leading combining mark
    is escaped too, as it would sit on the opening quote (pre-QA r3 on #695). A name longer
    than any NAS name is escaped unchecked, as `shown` does."""
    plain = json.dumps(text, ensure_ascii=False)
    long = len(text.encode("utf-8", "surrogatepass")) > NAME_BYTES
    if not long and invisible_problem(text) is None and not any(map(_hides, text)):
        return plain
    out = []
    for n, c in enumerate(plain):
        lead_mark = n == 1 and bool(_MARK.match(c))
        # What `escaped_repr` escapes, and the annotation characters, which repr escapes for
        # it (pre-QA r4 on #695: they were left raw).
        if lead_mark or _IGNORABLE.match(c) or c == _BRAILLE_BLANK or _hides(c) or c in _ANNOTATION:
            o = ord(c)
            if o > 0xFFFF:
                o -= 0x10000
                out.append(f"\\u{0xD800 + (o >> 10):04x}\\u{0xDC00 + (o & 0x3FF):04x}")
            else:
                out.append(f"\\u{o:04x}")
        else:
            out.append(c)
    return "".join(out)


# Marks a model reads as quoting or code: every character with Unicode's Quotation_Mark
# property (straight and typographic quotes, guillemets, low and reversed quotes, CJK corner
# brackets, their fullwidth and vertical forms: #711), a backtick (pre-QA r5 on #691) and
# its fullwidth form, and look-alikes without the property: the quotation mark ornaments
# (U+275B-2760, U+1F676-1F678; pre-QA r1 on #711), the primes and reversed primes
# (U+2032-2037, U+2057) and the modifier letters prime, double prime, apostrophe and double
# apostrophe (U+02B9, U+02BA, U+02BC, U+02EE; QA r1 on #724). Not `_MARKS`, the lookbehinds'
# mark-run bound above (QA r1 on #724).
_QUOTE_MARKS = (
    r"[\p{Quotation_Mark}`\uff40\u275b-\u2760\U0001f676-\U0001f678"
    r"\u2032-\u2037\u2057\u02b9\u02ba\u02bc\u02ee]"
)
# At the end, before any combining marks: `a'` with an accent on the quote still ends in it.
_EDGE_QUOTE = regex.compile(rf"^{_QUOTE_MARKS}|{_QUOTE_MARKS}\p{{M}}*\Z")


def edge_quote(text: str) -> bool:
    """``text`` starts or ends with a quote mark, so written as is it could read as quoted."""
    return _EDGE_QUOTE.search(text) is not None


def _needs_escapes(part: str) -> bool:
    """Whether a path's ``part`` holds what `shown` writes with escapes: padding, a character
    that doesn't print or that `invisible_problem` refuses. A part longer than any NAS name
    counts unchecked: `invisible_problem`'s repeated-mark search grows with the cube of a long
    run of different marks (the stdlib NFD it once used was quadratic: QA round 3 on #515),
    and new names are capped at 255 bytes before it runs."""
    if len(part.encode("utf-8", "surrogatepass")) > NAME_BYTES:
        return True
    return part != part.strip() or invisible_problem(part) is not None or any(map(_hides, part))


def shown_local(path: str | os.PathLike[str]) -> str:
    """A path on this machine, for a message (#691), by `shown`'s rule for a NAS path: as the
    OS writes it, Windows backslashes too, unless a part (split by the OS's separators) holds
    what `shown` would quote, padding, a tab, a character that draws nothing or ``", "``; then
    the whole path is quoted, as `shown` quotes one (`plain_quoted` for ``", "`` alone, else
    `escaped_repr`), so in any quoted path a backslash is an escape, as the instructions' PATH
    FORMAT says. Not a part at a time: a part that itself starts and ends with a quote would
    read like a quoted one (pre-QA r2 on #691). A path that starts or ends with a quote is
    quoted too, so it can't read as a quoted path (pre-QA r3). The text isn't normalized
    (``Path`` would drop ``./`` and a trailing slash), so the caller's own path reads as they
    sent it."""
    text = os.fspath(path)
    seps = "".join(re.escape(s) for s in (os.sep, os.altsep) if s)
    if any(_needs_escapes(part) for part in re.split(f"[{seps}]", text) if part):
        return escaped_repr(text)
    if not text or ", " in text or edge_quote(text):
        return plain_quoted(text)
    return text


def shown_beside(path: str, marker: str) -> str:
    """``path`` as `shown` writes it, quoted (`plain_quoted`) when it holds ``marker``, the text
    a line puts right after or between paths (`" and "`, `" (to "`), so a name holding it can't
    read as that line's structure (pre-QA r1 on #693)."""
    text = shown(path)
    return plain_quoted(path) if _holds_marker(path, marker) and text == path else text


def _holds_marker(path: str, marker: str) -> bool:
    """Whether ``path`` would read as part of its line's ``marker``: it holds it, or, before
    a marker starting ``" ("`` (``" (to "`` too), it ends with ``":"``, which would read as the
    line's own ``": "`` (QA r1 on #717; Photos' `shown_folder` quotes it the same way)."""
    return marker in path or (marker.startswith(" (") and path.endswith(":"))


def shown_local_beside(path: str | os.PathLike[str], marker: str) -> str:
    """A local path as `shown_local` writes it, quoted (`plain_quoted`) when it holds
    ``marker``, the text its line puts right after it (`": "`, `" ("`), as `shown_beside` does
    for a NAS path (#706)."""
    text = shown_local(path)
    raw = os.fspath(path)
    return plain_quoted(raw) if _holds_marker(raw, marker) and text == raw else text


def plain_quoted(path: str) -> str:
    """``path`` in quotes that hold it exactly: single quotes, or double when it holds a
    single one; with both, or with a backslash, `escaped_repr`'s form. In any quoted path a
    backslash is then always an escape, as the server instructions say: a plain-quoted
    ``C:\temp`` would read as ``C:<TAB>emp`` (pre-QA r3 on #692)."""
    if "\\" in path:
        return escaped_repr(path)
    if "'" not in path:
        return f"'{path}'"
    if '"' not in path:
        return f'"{path}"'
    return escaped_repr(path)
