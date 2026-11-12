"""File Station helpers: path normalization, size parsing, async polling."""

from __future__ import annotations

import contextlib
import fnmatch
import json
import logging
import os
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any, NoReturn

import httpx
import regex

from mcp_synology.core.client import DsmClient
from mcp_synology.core.errors import AuthenticationError, ErrorCode, SynologyError, error_from_code
from mcp_synology.core.formatting import (
    ESCAPED_NAME_ADVICE,
    RGI_FLAG_TAGS,
    error_response,
    holds_escape,
)
from mcp_synology.core.redaction import redact_urls
from mcp_synology.modules.filestation.decompositions import NEW_DECOMPOSITIONS
from mcp_synology.modules.filestation.standardized_variants import (
    EMOJI_VS_BASES,
    STANDARDIZED_BASES,
)

logger = logging.getLogger(__name__)


def never_sent(exc: Exception) -> bool:
    """The request never reached DSM: its connection couldn't be opened (as Photos'
    ``photo_task`` says). A pool timeout belongs here too, but that's #442."""
    return isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)


def dsm_str(value: str) -> str:
    """JSON-quote a free-text parameter value for a JSON-format File Station API.

    DSM parses these values as JSON first (#121), so an unquoted value that is
    valid JSON is read as that type: a folder named ``2024`` or ``true`` gets
    error 400, a ``list`` pattern of ``2024`` crashes the request (HTTP 502),
    and a Search/DirSize task ID like ``17918443788CE649B9`` matches no task.
    Synology's API guide quotes every such value. Paths need no quoting: they
    start with ``/`` and so are never valid JSON.
    """
    return json.dumps(value, ensure_ascii=False)


# A quoted task ID that DSM rejects outright. Reported once on DSM 7.2.2
# (esausilva/synology-api-sdk#1), never seen on vdsm 7.1/7.2.2 or a real
# DS1618+, so it's a fallback, not the default.
_QUOTED_TASKID_REJECTED = frozenset({599, 600})


def describe_transport_error(exc: httpx.TransportError) -> str:
    """``ReadTimeout``, or ``ConnectError: [Errno 111] ...``; never empty (#237).

    httpx's timeouts stringify to ``''``. Any URL in the text is redacted.
    """
    text = redact_urls(str(exc))
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def unanswered_status_response(
    operation: str,
    path: str,
    exc: httpx.TransportError,
    *,
    task_note: str = "",
    already: str = "",
    suggestion: str | None = None,
) -> NoReturn:
    """Raise the structured error for a background task whose status DSM stopped answering.

    DSM can stall for longer than the connection timeout while it deletes or copies
    very large files (#237); httpx's error used to escape with an empty message.
    """
    timed_out = isinstance(exc, httpx.TimeoutException)
    what = "didn't answer" if timed_out else "lost the connection during"
    if never_sent(exc):
        # The connection was never opened, so the request never reached DSM (as Photos says),
        # and a longer timeout or a smaller folder wouldn't help.
        what = "couldn't be reached for"
        suggestion = (
            "Check that the NAS is on and reachable from this machine, then try again; "
            "list the path to see where the task got to."
        )
    note = f" {task_note}" if task_note else ""
    error_response(
        ErrorCode.TIMEOUT if timed_out else ErrorCode.UNAVAILABLE,
        f"{operation} failed: DSM {what} a status request on path: {shown(path)}"
        f" ({describe_transport_error(exc)}).{note}{already}",
        retryable=True,
        param="path",
        value=path,
        suggestion=suggestion
        or (
            "List the path to see where it got to before retrying. DSM can stall on very "
            "large files; a larger connection timeout (connection.timeout in the config) "
            "gives it longer to answer, or use DSM's own File Station for this one."
        ),
    )


async def task_request(
    client: DsmClient,
    api: str,
    method: str,
    taskid: str,
    *,
    version: int | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call a Search/DirSize task method with ``taskid`` JSON-quoted (#121).

    An unquoted ID whose first character after the 10-digit timestamp is a
    digit is misread, so ``list`` answers DSM's unknown-task reply, ``status``
    fails with 599, and ``stop``/``clean`` silently clean nothing. If DSM
    rejects the quoted form (599/600), retry once unquoted.
    """
    base = dict(params or {})
    try:
        return await client.request(
            api, method, version=version, params={**base, "taskid": dsm_str(taskid)}
        )
    except SynologyError as e:
        if e.code not in _QUOTED_TASKID_REJECTED:
            raise
        logger.warning(
            "%s/%s rejected quoted taskid %s (DSM %s); retrying unquoted",
            api,
            method,
            taskid,
            e.code,
        )
        return await client.request(api, method, version=version, params={**base, "taskid": taskid})


# The `additional` fields get_file_info accepts: exactly the ones its output shows,
# so nothing requested is silently dropped (#149). It's the only tool with the
# parameter; the listing tools fetch fixed fields. `volume_status` is left out: only
# list_share returns it. Documented in `docs/specs/filestation-module-spec.md`.
_VALID_ADDITIONAL_FIELDS: frozenset[str] = frozenset(
    {
        "real_path",
        "size",
        "owner",
        "time",
        "perm",
        "type",
        "mount_point_type",
    }
)


def validate_additional(values: list[str] | None, *, tool_name: str) -> None:
    """Reject unknown `additional` field names before they hit DSM.

    DSM accepts unknown values silently (the field just doesn't appear in the
    response), which makes typos invisible to callers. Validating up-front
    surfaces the typo as a clear ToolError naming the bad value and listing
    the supported set.
    """
    if not values:
        return
    unknown = [v for v in values if v not in _VALID_ADDITIONAL_FIELDS]
    if unknown:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"{tool_name} failed: unknown 'additional' field(s): {sorted(set(unknown))!r}.",
            retryable=False,
            param="additional",
            value=values,
            suggestion=("Supported fields: " + ", ".join(sorted(_VALID_ADDITIONAL_FIELDS)) + "."),
        )


# Size unit multipliers, all binary (1 KiB = 1024 bytes). Output is labeled KiB/MiB/...;
# input also takes KB/MB/... with the same binary meaning, since that's what users type.
_SIZE_UNITS: dict[str, int] = {
    "B": 1,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
    "PIB": 1024**5,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
    "PB": 1024**5,
}

# Pattern for parsing human-readable sizes like "1.5GiB", "500 MB"
_SIZE_PATTERN = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(B|[KMGTP]i?B)\s*$",
    re.IGNORECASE,
)

# Video file extensions for icon display
_VIDEO_EXTENSIONS = frozenset(
    {
        "mkv",
        "mp4",
        "avi",
        "mov",
        "wmv",
        "flv",
        "webm",
        "m4v",
        "mpg",
        "mpeg",
        "ts",
    }
)


def strip_padding(path: str) -> str:
    """``path`` without padding: whitespace before a leading ``/``, or the whole of a path
    that is only whitespace (#488). Anything else is part of the path, kept exactly."""
    if not path.strip():
        return ""
    unpadded = path.lstrip()
    return unpadded if unpadded.startswith("/") else path


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


def name_key(path: str) -> str:
    """A path's last part as a same-name check compares it: ignoring case, the widest way, as a
    refusal costs less than a file lost on a file system that folds case (#495)."""
    return path.rsplit("/", 1)[-1].casefold()


def _escaped_repr(path: str) -> str:
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
    """``path`` for a message: as is, or quoted (`_escaped_repr`, with escapes) when a
    part has whitespace at its start or end, a character that doesn't print (a tab, a
    no-break space) or one `invisible_problem` refuses, so a stray space or a hidden
    character is visible once paths are taken exactly (#488, #489, #515). A path holding
    ``", "`` and nothing else to show is quoted without escapes, unless it holds a
    backslash (`plain_quoted`, #685, below); so is one that starts or ends with a quote,
    which would read as a quoted name (#691).

    The rule (#510): a refusal of an argument as given quotes it with ``!r`` (the caller's
    own text, shown exactly), or ``_escaped_repr``, which also shows what ``!r`` leaves as is
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
        return _escaped_repr(path)
    # Marks, joiners and variation selectors where Unicode uses them show as written
    # (`नमस्ते`, NFD `Café`, 👨<ZWJ>👩<ZWJ>👧, ❤<VS16>); anywhere else they're quoted
    # (`rep<ZWJ>ort.pdf`).
    if ", " in path or _edge_quote(path):
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
    `_escaped_repr` writes one, so ``"Ann<ZWSP>"`` can't pass for ``"Ann"``. Still valid
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
        # What `_escaped_repr` escapes, and the annotation characters, which repr escapes for
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


# Marks a model reads as quoting or code: straight and typographic quotes, a backtick
# (pre-QA r5 on #691).
_QUOTES = ("'", '"', "`", "\u2018", "\u2019", "\u201c", "\u201d")


def _edge_quote(text: str) -> bool:
    """``text`` starts or ends with a quote, so written as is it could read as quoted."""
    return text.startswith(_QUOTES) or text.endswith(_QUOTES)


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
    `_escaped_repr`), so in any quoted path a backslash is an escape, as the instructions' PATH
    FORMAT says. Not a part at a time: a part that itself starts and ends with a quote would
    read like a quoted one (pre-QA r2 on #691). A path that starts or ends with a quote is
    quoted too, so it can't read as a quoted path (pre-QA r3). The text isn't normalized
    (``Path`` would drop ``./`` and a trailing slash), so the caller's own path reads as they
    sent it."""
    text = os.fspath(path)
    seps = "".join(re.escape(s) for s in (os.sep, os.altsep) if s)
    if any(_needs_escapes(part) for part in re.split(f"[{seps}]", text) if part):
        return _escaped_repr(text)
    if not text or ", " in text or _edge_quote(text):
        return plain_quoted(text)
    return text


def shown_beside(path: str, marker: str) -> str:
    """``path`` as `shown` writes it, quoted (`plain_quoted`) when it holds ``marker``, the text
    a line puts right after or between paths (`" and "`, `" (to "`), so a name holding it can't
    read as that line's structure (pre-QA r1 on #693)."""
    text = shown(path)
    return plain_quoted(path) if marker in path and text == path else text


def plain_quoted(path: str) -> str:
    """``path`` in quotes that hold it exactly: single quotes, or double when it holds a
    single one; with both, or with a backslash, `_escaped_repr`'s form. In any quoted path a
    backslash is then always an escape, as the server instructions say: a plain-quoted
    ``C:\temp`` would read as ``C:<TAB>emp`` (pre-QA r3 on #692)."""
    if "\\" in path:
        return _escaped_repr(path)
    if "'" not in path:
        return f"'{path}'"
    if '"' not in path:
        return f'"{path}"'
    return _escaped_repr(path)


def normalize_path(path: str) -> str:
    """Normalize a file path for the DSM API.

    - Drop whitespace before a leading ``/`` (padding around the path, not part of it)
    - Prepend `/` if missing
    - Strip trailing `/` (unless root)

    Nothing else is stripped (#488): DSM keeps whitespace in a name exactly (``foo ``,
    `` foo``, ``foo<TAB>``, ``foo<NBSP>`` and ``" "`` are names beside ``foo``; getinfo,
    List, Rename and Delete act on exactly the named one; vdsm DSM 7.1, 7.2.2, 7.3.2 and
    7.4.1, admin and not). Stripping sent ``/video/x/foo `` as ``/video/x/foo``, a different
    item, and ``/video/x/ `` as its parent. A path of whitespace only is empty.
    """
    path = strip_padding(path)
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        # A path of slashes only is the root, not an empty string (pre-QA on #510).
        path = path.rstrip("/") or "/"
    return path


def validate_share_path(path: str, known_shares: set[str]) -> str | None:
    """Validate that the first path component is a known shared folder.

    Returns an error message if invalid, or None if valid.
    """
    normalized = normalize_path(path)
    parts = normalized.split("/")
    # parts[0] is empty string (before leading /), parts[1] is the share name
    if len(parts) < 2 or not parts[1]:
        return f"Invalid path {path!r}: must start with a shared folder name."

    share = parts[1]
    # Strip #recycle if present — user might be browsing recycle bin
    if share == "#recycle":
        return f"Invalid path {path!r}: must start with a shared folder name, not #recycle."

    # DSM takes an ordinary share in any case: /TESTSHARE/... works for share "testshare"
    # (DSM 7.1 and 7.2.2; QA R1 on #173). The user home service doesn't: /Home is 408 (#353).
    if share.casefold() not in {s.casefold() for s in known_shares}:
        available = ", ".join(sorted(known_shares)) if known_shares else "(none)"
        # A path passed back with the quotes a reply showed it in (`shown`): say so, as
        # advice only, so a share really named that way isn't refused (pre-QA r2 on #692).
        quotes = (
            " If the path came from a reply in quotes, pass it without them."
            if share in ("'", '"')
            else ""
        )
        return (
            f"Unknown shared folder {share!r}. "
            f"Available shares: {available}. "
            f"Use list_shares to see all shared folders.{quotes}"
        )
    return None


def parse_human_size(value: str | int) -> int:
    """Parse a human-readable size string into bytes.

    Accepts:
        - Integers (treated as bytes): 1048576
        - Strings: "500MiB", "2GiB", "1.5TiB" (case-insensitive), up to PiB. KB/MB/GB/TB/PB
          are accepted too and mean the same binary units.

    Raises ValueError for invalid input.
    """
    if isinstance(value, int):
        return value

    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())

    match = _SIZE_PATTERN.match(str(value))
    if not match:
        msg = (
            f"Invalid size '{value}'. "
            f"Use a number (bytes) or a human-readable size like '500MiB', '2GiB', '1.5TiB'."
        )
        raise ValueError(msg)

    number = float(match.group(1))
    unit = match.group(2).upper()
    return int(number * _SIZE_UNITS[unit])


def parse_mtime(value: str) -> int:
    """Parse an mtime filter into Unix epoch seconds.

    Accepts ``YYYY-MM-DD``, ISO 8601 datetime (with or without
    timezone offset), or a numeric epoch-seconds string. Naive
    datetimes are treated as UTC for stable cross-host behavior.

    Raises ValueError for unrecognized input.
    """
    s = value.strip()
    if s.lstrip("-").isdigit():
        return int(s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        msg = (
            f"Invalid mtime value {value!r}. "
            "Use ISO 8601 (e.g. '2026-04-01T12:00:00+00:00'), "
            "a calendar date ('YYYY-MM-DD'), or Unix epoch seconds."
        )
        raise ValueError(msg) from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def file_type_icon(is_dir: bool, filename: str = "", style: str = "emoji") -> str:
    """Get a file type indicator icon.

    Args:
        is_dir: Whether this is a directory.
        filename: The filename (used to detect video files).
        style: "emoji" or "text".
    """
    if is_dir:
        return "\U0001f4c1" if style == "emoji" else "[DIR]"

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in _VIDEO_EXTENSIONS:
        return "\U0001f3ac" if style == "emoji" else "[VIDEO]"

    return "\U0001f4c4" if style == "emoji" else "[FILE]"


def escape_multi_path(paths: list[str]) -> str:
    """Escape and comma-join paths for DSM multi-path parameters.

    Delegates to DsmClient.escape_path_param.
    """
    return DsmClient.escape_path_param(paths)


def matches_pattern(filename: str, pattern: str) -> bool:
    """Check if a filename matches a wildcard pattern (case-insensitive).

    Only ``*`` and ``?`` are wildcards; everything else, ``[...]`` and commas included,
    matches itself, and the pattern matches the whole name. DSM's own patterns don't read
    ``[...]`` as a class either, but differ otherwise: a comma separates alternatives, and
    a pattern without ``*`` or ``?`` matches anywhere (``dsm_pattern_matcher``). With
    fnmatch, ``*[1080p]*`` was a character class that left out every name containing 1,
    0, 8 or p (#396), so ``[`` is escaped as ``[[]``. fnmatch itself stays: its translation
    doesn't backtrack, while a regex with one ``.*`` per ``*`` does, badly, on long names.
    """
    return fnmatch.fnmatchcase(filename.lower(), pattern.lower().replace("[", "[[]"))


# Closes #37. Lazy per-share recycle-bin probe + observation-based self-correction.
#
# `recycle_status` is a closure-captured `dict[str, bool]` shared across every
# tool handler in the filestation module (see modules/filestation/__init__.py
# where it's created and threaded through the `recycle_bin_status=` kwarg).
# Before this helper landed, the dict was created empty and never populated:
# every share looked like recycle-on by default, so `delete_files` always told
# the user their files were recoverable from `#recycle` even when the share
# had recycle disabled and the data was actually gone.
#
# Strategy: probe lazily per share on first observation, cache the result for
# the life of the session, and let the auth manager invalidate the cache on
# session re-auth (since admin could toggle `#recycle` between sessions).
#
# Probe: `SYNO.FileStation.List` on `/{share}/#recycle/` with `limit=1`: one entry at
# most (DSM's `limit=0` means all of them, #353).
#   - Success                     -> recycle bin enabled (True), cached
#   - DSM 408 (no `#recycle`)     -> ask the share's own setting (`SYNO.Core.Share list`,
#       `additional=["recyclebin"]`; for /home, the user home service's,
#       `SYNO.Core.User.Home get`), which only an administrator can read: its answer is
#       cached. For anyone else (DSM 105) the answer is False, NOT cached (#409): DSM
#       creates `#recycle` when the share is created and again at the first delete into
#       the bin, so a missing folder means the bin is off, or on with nothing deleted into
#       it yet (a new account's home), or that someone deleted `#recycle` itself. Checked
#       again on each use, a delete into a bin that's on shows up as soon as DSM re-creates
#       the folder.
#   - Anything else (105 permission denied, another DSM error, no answer) -> unknown
#       (None), NOT cached (#255): the next call probes again. `ensure_recycle_status`
#       then answers True so `delete_files` stays optimistic about recoverability;
#       `list_shares` shows "couldn't check". A WARNING names the cause.
# Shares whose bin couldn't be checked were warned about once per server run; later
# misses log at DEBUG, since an uncached share is checked again on every use (#255).
_warned_recycle: set[str] = set()


def _warn_once(share_name: str, message: str, *args: object) -> None:
    level = logging.DEBUG if share_name in _warned_recycle else logging.WARNING
    _warned_recycle.add(share_name)
    logger.log(level, message, *args)


async def _share_recycle_setting(client: DsmClient, share_name: str) -> bool | None:
    """The share's own recycle-bin setting, or None when this account can't read it.

    Only an administrator can read it (105 for anyone else, seska DSM 7.1.1). The user
    home folder ``/home`` takes the user home service's setting (``SYNO.Core.User.Home``,
    Control Panel > User > Advanced): the ``homes`` share's own flag can disagree with it
    (vdsm: ``homes`` off while every user's home bin is on).
    """
    try:
        if share_name == "home":
            data = await client.request("SYNO.Core.User.Home", "get")
            setting = data.get("enable_recycle_bin")
            return setting if isinstance(setting, bool) else None
        data = await client.request(
            "SYNO.Core.Share",
            "list",
            params={"shareType": "all", "additional": json.dumps(["recyclebin"])},
        )
    except SynologyError as e:
        logger.debug("Can't read /%s's recycle-bin setting: %s", share_name, e.code or e)
        return None
    for share in data.get("shares", []):
        if share.get("name") == share_name and isinstance(share.get("enable_recycle_bin"), bool):
            return bool(share["enable_recycle_bin"])
    return None


async def probe_recycle_status(client: DsmClient, share_name: str) -> tuple[bool | None, bool]:
    """Probe `/{share_name}/#recycle`: (True on, False off or no bin folder, None can't
    tell; definite).

    ``definite`` says whether the answer may be cached. A missing ``#recycle`` that
    the share's own setting can't explain is ``(False, False)``: no bin folder yet.
    """
    try:
        await client.request(
            "SYNO.FileStation.List",
            "list",
            params={"folder_path": f"/{share_name}/#recycle", "limit": 1},
        )
    except SynologyError as e:
        if e.code == 408 and not isinstance(e, AuthenticationError):
            setting = await _share_recycle_setting(client, share_name)
            if setting is not None:
                logger.debug(
                    "Probed recycle bin on /%s: #recycle missing; the share's setting is %s",
                    share_name,
                    "on" if setting else "off",
                )
                return setting, True
            logger.debug("Probed recycle bin on /%s: no #recycle folder yet", share_name)
            return False, False
        if e.code == 105:
            _warn_once(
                share_name,
                "Recycle-bin probe on /%s returned DSM 105 (permission denied); can't tell "
                "whether its recycle bin is on. Grant the MCP service account read access "
                "on /%s/#recycle for accurate delete-files messaging.",
                share_name,
                share_name,
            )
        else:
            _warn_once(
                share_name,
                "Recycle-bin probe on /%s failed (%s); can't tell whether its recycle bin "
                "is on. It will be checked again on next use.",
                share_name,
                f"DSM error {e.code}" if e.code is not None else type(e).__name__,
            )
        return None, False
    logger.debug("Probed recycle bin on /%s: enabled", share_name)
    return True, True


async def ensure_recycle_status(
    client: DsmClient,
    share_name: str,
    recycle_status: dict[str, bool],
) -> bool:
    """Return cached recycle-bin status for `share_name`, probing when it isn't cached.

    Caches a definite answer in `recycle_status` in place so later calls skip the
    probe. A missing ``#recycle`` that isn't definite answers False uncached (#409),
    and a probe that can't tell answers True uncached (the optimistic default that
    keeps delete-files messaging hopeful, #255).
    """
    if share_name in recycle_status:
        return recycle_status[share_name]
    status, definite = await probe_recycle_status(client, share_name)
    if status is None:
        return True
    if definite:
        recycle_status[share_name] = status
    return status


def correct_recycle_status_from_observation(
    share_name: str,
    observed_enabled: bool,
    recycle_status: dict[str, bool],
) -> None:
    """Update the cache when a tool observes DSM behavior that contradicts it.

    Seeing ``/share/#recycle`` list proves the bin is on: cache True. Not finding it
    (408 from `list_recycle_bin`) proves nothing (#409), so it only drops a cached
    True, and the next call checks again.
    """
    cached = recycle_status.get(share_name)
    if cached == observed_enabled:
        return
    if cached is not None:
        logger.info(
            "Self-correcting recycle-bin cache on /%s: cached=%s, observed=%s",
            share_name,
            cached,
            observed_enabled,
        )
    if observed_enabled:
        recycle_status[share_name] = True
    else:
        recycle_status.pop(share_name, None)


_WILDCARD_CHARS = ("*", "?")


async def find_missing_paths(client: DsmClient, paths: list[str]) -> list[tuple[str, int]]:
    """Return ``(path, dsm_code)`` for each path that ``getinfo`` reports as missing.

    DSM answers a missing path with a per-file ``{"code": 408, "path": ...}`` entry rather
    than a failed request (DSM 7.1 and 7.2.2; #161, #162). An answer with no entry for a
    path proves nothing either way, so that path isn't reported. A failed request raises
    ``SynologyError``, or a transport error with no answer, for the caller to map.
    """
    version = min(2, client.negotiate_version("SYNO.FileStation.List", max_version=2))
    missing: list[tuple[str, int]] = []
    for path in paths:
        data = await client.request(
            "SYNO.FileStation.List", "getinfo", version=version, params={"path": path}
        )
        for entry in data.get("files", []):
            if "code" in entry:
                missing.append((str(entry.get("path") or path), int(entry["code"])))
    return missing


async def path_exists(client: DsmClient, path: str) -> bool | None:
    """One ``getinfo``: True when DSM shows the path, False when it answers 408 for it.

    None when the answer shows neither (no entry, or another code such as 407). A failed
    request raises for the caller to handle.
    """
    version = min(2, client.negotiate_version("SYNO.FileStation.List", max_version=2))
    data = await client.request(
        "SYNO.FileStation.List", "getinfo", version=version, params={"path": path}
    )
    for entry in data.get("files", []):
        if entry.get("code") == 408:
            return False
        if isinstance(entry.get("isdir"), bool):
            return True
    return None


async def paths_isdir(client: DsmClient, paths: list[str]) -> dict[str, bool]:
    """Return ``{path: isdir}`` for each path whose type ``getinfo`` reports.

    A path DSM gives no ``isdir`` for (missing, or an unexpected shape) is left out. A
    failed request raises ``SynologyError``, or a transport error with no answer, for the caller
    to handle.
    """
    version = min(2, client.negotiate_version("SYNO.FileStation.List", max_version=2))
    kinds: dict[str, bool] = {}
    for path in paths:
        data = await client.request(
            "SYNO.FileStation.List", "getinfo", version=version, params={"path": path}
        )
        for entry in data.get("files", []):
            if isinstance(entry.get("isdir"), bool):
                kinds[path] = entry["isdir"]
    return kinds


_NO_PERMISSION = frozenset({105, 407})
_CHECK_ACCESS = "check it and the account's access to it in DSM."
_OTHER_CODE = "DSM couldn't report the path: check it and the account's access to it in DSM."


def missing_text(path: str, code: int) -> str:
    """A path ``getinfo`` couldn't report, in words (#620: "(DSM error 408)" alone left the
    model to translate DSM's code; a 407 was called "not found"), keeping the code."""
    why = (
        "no permission"
        if code in _NO_PERMISSION
        else {408: "doesn't exist"}.get(code, "can't be read")
    )
    return f"{shown(path)}: {why} (DSM error {code})"


def unreadable_paths_error(missing: list[tuple[str, int]]) -> tuple[ErrorCode, str]:
    """The error code and suggestion for paths ``getinfo`` answered with a code (#620): every
    path refused for permission is ``permission_denied``, every missing one ``not_found``;
    a mix gets each kind's own advice (not the shared "retry with paths that exist", which a
    path refused for permission can't follow: QA round 3 on #606). A per-path code other
    than 408, 105 or 407 gets one fixed message under ``filestation_error``: the generic
    mapping of such a code (a session code's "re-authentication will be attempted", 599's
    "background task") doesn't fit a path's entry (QA round 1 on #623)."""
    codes = [code for _, code in missing]
    perm = error_from_code(407, "SYNO.FileStation.List").suggestion or ""
    if codes and all(code in _NO_PERMISSION for code in codes):
        return ErrorCode.PERMISSION_DENIED, perm
    if all(code == 408 for code in codes):
        return ErrorCode.NOT_FOUND, missing_paths_suggestion(missing)
    gone = [m for m in missing if m[1] == 408]
    parts = []
    if gone:
        parts.append("Use list_files or search_files to find the correct path.")
        wildcard = _wildcard_hint(gone)
        if wildcard:
            parts.append(wildcard)
        if any(holds_escape(path) for path, _ in gone):
            parts.append(ESCAPED_NAME_ADVICE)
    if any(code in _NO_PERMISSION for code in codes):
        parts.append(f"For one with no permission: {perm}")
    if any(code != 408 and code not in _NO_PERMISSION for code in codes):
        parts.append(
            _OTHER_CODE
            if len(parts) == 0
            else f"For one DSM gave another code for: {_CHECK_ACCESS}"
        )
    if gone:
        return ErrorCode.NOT_FOUND, " ".join(parts)
    if any(code in _NO_PERMISSION for code in codes):
        return ErrorCode.PERMISSION_DENIED, " ".join(parts)
    return ErrorCode.FILESTATION_ERROR, " ".join(parts)


def _wildcard_hint(missing: list[tuple[str, int]]) -> str:
    """The advice for a missing path that holds a wildcard character, or ""."""
    if any(ch in path for path, _ in missing for ch in _WILDCARD_CHARS):
        return (
            "Paths are literal, not wildcard patterns: use search_files (or list_files with a "
            "pattern) to find the files, then pass their full paths."
        )
    return ""


def missing_paths_suggestion(missing: list[tuple[str, int]]) -> str:
    """Suggestion for a call refused because paths don't exist (adds a wildcard hint, and one for
    a name copied from a reply that showed it escaped)."""
    hint = _wildcard_hint(missing)
    wildcard = f" {hint}" if hint else ""
    if missing and all(holds_escape(path) for path, _ in missing):
        # Listing again would show the same escape: lead with what ends the loop (pre-QA r2).
        return ESCAPED_NAME_ADVICE + wildcard
    suggestion = (
        "Check the paths with list_files or search_files, then retry with paths that exist."
        + wildcard
    )
    if any(holds_escape(path) for path, _ in missing):
        # A listing shows the same escape again, so "check with list_files" alone loops (#604).
        suggestion += f" {ESCAPED_NAME_ADVICE}"
    return suggestion


async def _refresh_known_shares(client: DsmClient, known_shares: set[str]) -> bool:
    """Reload ``known_shares`` from ``list_share``; return False if DSM won't say."""
    try:
        data = await client.request("SYNO.FileStation.List", "list_share", params={})
    except SynologyError as e:
        logger.warning("Couldn't list shared folders to validate paths (DSM %s): %s", e.code, e)
        return False
    known_shares.clear()
    known_shares.update(str(s["name"]) for s in data.get("shares", []) if s.get("name"))
    logger.debug("Known shares: %s", sorted(known_shares))
    return True


def _with_listed_share_name(path: str, known_shares: set[str]) -> str:
    """Return ``path`` with its share named as ``list_share`` names it (#353).

    DSM takes an ordinary share in any case (``/TESTSHARE`` for ``testshare``), but not the
    user home service: ``/Home`` and ``/HOME`` get 408 where ``/home`` lists (vdsm DSM 7.1
    and 7.4.1; seska's DSM 7.1.1, admin and not). So the listed spelling is always sent.
    """
    parts = normalize_path(path).split("/")
    if len(parts) < 2 or not parts[1] or parts[1] in known_shares:
        return path
    listed = next((s for s in known_shares if s.casefold() == parts[1].casefold()), None)
    if listed is None:
        return path
    return "/".join(["", listed, *parts[2:]])


def _is_dot(part: str) -> bool:
    """``.`` or ``..``, padded with any whitespace: whether DSM trims padding beside a dot
    is unprobed, so every padded form is refused (#449, #510)."""
    return part.strip() in (".", "..")


def refuse_dot_parts(
    path: str,
    param: str,
    suggestion: str = "Write the path out in full from the shared folder, e.g. /video/Movies.",
) -> None:
    """Raise ``invalid_parameter`` for a NAS path with a ``.`` or ``..`` part (#449).

    DSM refuses them itself (File Station answers 418 "Illegal name or path"; CreateFolder
    1100, on DSM 7.1 through 7.4.1), but only after the share check, which reads just
    the first part, so ``/video/../photo`` passed as a ``video`` path and anything keyed
    on the share (the recycle-bin note after a delete, a hidden ``#recycle``) used the
    wrong one.
    """
    if any(_is_dot(part) for part in normalize_path(path).split("/") if part):
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"Invalid path {path!r}: '.' and '..' parts aren't allowed.",
            retryable=False,
            param=param,
            value=path,
            suggestion=suggestion,
        )


async def ensure_known_shares(
    client: DsmClient,
    paths: list[str],
    known_shares: set[str],
    params: str | list[str],
    suggestion: str | None = None,
) -> list[str]:
    """Raise ``not_found`` for a path whose first component isn't a shared folder (#160),
    and ``invalid_parameter`` for one with a ``.`` or ``..`` part (#449).

    ``params`` is the tool argument each path came from (one name for all, or one per
    path), so an error names the argument the caller passed. ``suggestion`` replaces both
    errors' suggestions (a path from a setting is fixed in the config, not the call).

    Returns ``paths`` in order, each with its share named as DSM lists it (#353): DSM
    takes an ordinary share in any case, but the home service only as ``home``.

    ``known_shares`` is a cache owned by the caller (cleared on re-auth). It's filled on
    first use and reloaded once when a path names a share it doesn't hold, so a share
    created since is still found. For an account that isn't an admin, the list is the
    shares it can see, which is what it should be told about. If DSM won't list shares,
    the check is skipped, ``paths`` come back unchanged and DSM's own error stands.
    """
    if not paths:
        return []
    names = [params] * len(paths) if isinstance(params, str) else params
    for path, param in zip(paths, names, strict=True):
        if suggestion:
            refuse_dot_parts(path, param, suggestion)
        else:
            refuse_dot_parts(path, param)

    def _shares() -> set[str]:
        return {
            normalize_path(p).split("/")[1].casefold() for p in paths if len(normalize_path(p)) > 1
        }

    known = {s.casefold() for s in known_shares}
    if (not known_shares or not _shares() <= known) and not await _refresh_known_shares(
        client, known_shares
    ):
        return list(paths)
    for path, param in zip(paths, names, strict=True):
        problem = validate_share_path(path, known_shares)
        if problem:
            error_response(
                ErrorCode.NOT_FOUND,
                problem,
                retryable=False,
                param=param,
                value=path,
                suggestion=suggestion or "Call list_shares to see the shared folders on this NAS.",
            )
    return [_with_listed_share_name(path, known_shares) for path in paths]
