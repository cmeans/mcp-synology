#!/usr/bin/env python3
"""Check the CHANGELOG fragments in `changelog.d/` (#731).

Each PR writes its CHANGELOG entry as a file there, and the release PR assembles them with
towncrier (CLAUDE.md, "Bumping the version for a release"). towncrier's own checks leave two gaps
this one closes: `towncrier build` would leave a misnamed file out of the notes (here it's
refused), and `towncrier check` passes any PR that edits `CHANGELOG.md` (here a PR passes only by
adding or changing a fragment named for its own number and removing none, as a release PR that
assembled every fragment, or with the `no changelog` label).

Each problem prints as a GitHub Actions error. How a fragment renders is read with `cmarkgfm`,
the Python binding of cmark-gfm, the parser GitHub's Markdown is built on (QA r3 on #732; cmark-gfm
0.29.0.gfm.13 in cmarkgfm 2025.10.22: what GitHub's own pipeline adds on top of it isn't seen).
It's in the dev extra and in the `changelog` dependency group; everything else is standard library.

Usage (after `uv sync --extra dev`):
    uv run python scripts/changelog_fragments.py check
    uv run python scripts/changelog_fragments.py check --base <base> --pr <N> --labels "<a>,<b>"
    uv run python scripts/changelog_fragments.py notes <towncrier build --draft's output>
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
import tomllib
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

try:
    import cmarkgfm
    from cmarkgfm.cmark import Options
except ImportError:  # pre-QA r8 on #732: a plain `python` gave a traceback
    sys.exit(
        "changelog_fragments.py needs cmarkgfm: run it with `uv run python ...` after "
        "`uv sync --extra dev` (CI: `uv run --locked --only-group changelog python ...`)."
    )

DIRECTORY = "changelog.d"
# Files in the directory that aren't fragments, as glob patterns (also towncrier's `ignore`, in
# pyproject.toml, and check_release.py's copy): hidden files (macOS's .DS_Store and ._ files,
# an editor's .swp, .gitkeep), editor backups (`~`) and Windows' folder files, which towncrier
# would refuse and the check called misnamed fragments (pre-QA r6 on #732, #734).
NOT_FRAGMENTS = frozenset({"README.md", "template.md", ".*", "*~", "Thumbs.db", "desktop.ini"})


def not_a_fragment(name: str) -> bool:
    """Whether a file named ``name`` in `changelog.d/` is one of NOT_FRAGMENTS's patterns,
    case ignored, as towncrier 26.9.0 matches its `ignore` (it lowercases both)."""
    return any(fnmatch.fnmatchcase(name.lower(), pattern.lower()) for pattern in NOT_FRAGMENTS)


# The release tools read CHANGELOG.md line by line (publish.yml's awk, the release gate), so a
# heading at a line's start ends a section there even inside a code block, where GitHub shows it
# as code (pre-QA r5 on #731).
_LINE_HEADING = re.compile(r"#{1,6}(?:[ \t]|$)")
_BOM = "\ufeff"
# A footnote definition's start after any indent, list markers or `>` (only for naming its line).
_FOOTNOTE = re.compile(r"[ \t>*+0-9.)-]*\[\^(?:[^\[\]\\]|\\[\s\S])+\]:")
# A bullet list item's start, as CommonMark reads one at column 0 to 3.
_BULLET = re.compile(r" {0,3}[-*+](?:[ \t]|$)")
# The categories' headings, in towncrier's order (pyproject.toml's types, the template).
CATEGORIES = {"added": "Added", "changed": "Changed", "fixed": "Fixed"}
# A fragment is rendered between neighbors as a release assembles it (changelog.d/template.md):
# the version's heading, then an entry above it and one below it in its category, then the next
# category. An entry's neighbors are other PRs' entries, so what it does to them is what to check.
_TITLE = "## 0.0.0 (2000-01-01)"
_ABOVE = "- **Above** (#1): the entry above this one"
_BELOW = "- **Below** (#2): the entry below this one"
_NEXT = "- **Next** (#3): the next category's entry"


# GitHub's Markdown options, as far as structure goes, plus each block's source lines.
_OPTIONS = Options.CMARK_OPT_SOURCEPOS | Options.CMARK_OPT_FOOTNOTES
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# What may stand before a setext underline: indent, list markers and a quote's `>`.
_UNDER_PREFIX = re.compile(r"[ \t>*+0-9.)-]*")


def _is_underline(text: str) -> bool:
    """Whether ``text`` is a setext heading's underline, at any depth (after list markers or a
    quote's `>`): a run of `=` or `-` ending the line. Linear: a regex with a lazy prefix that can
    hold `-` took quadratic time on a long run of dashes (pre-QA r9 on #732)."""
    body = text.rstrip(" \t")
    if not body or body[-1] not in "=-":
        return False
    return _UNDER_PREFIX.fullmatch(body.rstrip(body[-1])) is not None


# A link reference or footnote definition, `[label]:` or `[^label]:`, as a candidate the parser
# then confirms (QA r4 on #732). A definition starts a line, after any indent, list markers or
# `>`, so the pattern is anchored there and tried once a line: tried at every `[`, a run of `\[`
# was quadratic (QA r5). Its label can run onto later lines, holds no unescaped `[`
# (CommonMark), and `\` escapes any character, a line break included (pre-QA r10).
_DEFINITION = re.compile(r"(?m)^[ \t>*+0-9.)-]*\[(\^?)((?:[^\[\]\\]|\\[\s\S])+)\]:")
# Where raw HTML is, outside code and backslash escapes (only for naming its line in a
# refusal): a tag that closes, on its line or a later one, not an autolink (`<https://x>`,
# `<a@b.c>`), and not a `<` that never closes (`a<b and`), which aren't raw HTML (#734).
_RAW_TAG = re.compile(
    r"<(?![A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*>)(?![^\s<>@]+@[^\s<>]+>)[A-Za-z/!?][^<>]*>"
)
# The pinned towncrier, as CLAUDE.md's release step 4 and CI's draft run it (a test checks the
# three agree): towncrier isn't a project dependency, so a bare `towncrier` is missing or
# unpinned (#735).
TOWNCRIER = (
    "uvx --from towncrier==26.9.0 --with click==8.5.1 --with jinja2==3.1.6 "
    "--with markupsafe==3.0.4 towncrier"
)
# A fragment's size limit: an entry is a line or a short paragraph, a release's text a few
# (the largest entry so far is 5 KiB). Rendering one costs memory (QA r4 on #732).
_MAX_BYTES = 16 * 1024
_VOID = frozenset({"area", "br", "col", "embed", "hr", "img", "input", "source", "track", "wbr"})


@dataclass
class _Block:
    """A top-level block of rendered notes: its tag (`h2`, `ul`, `p`, `pre`, `hr`, ...), the line
    (0-based) it starts on (a setext heading's underline), the first line after the block before
    it (`gap`: where a block GitHub writes nothing for, such as a footnote definition, sat), a
    heading's text, and a list's items' lines and rendered text. Raw HTML isn't a block here:
    `_Shape.raw` holds it, wherever it is."""

    kind: str
    line: int
    gap: int
    text: str = ""
    items: list[int] = field(default_factory=list)
    item_texts: list[str] = field(default_factory=list)


class _Shape(HTMLParser):
    """The top-level blocks of cmark-gfm's HTML, and the lines of every heading inside one."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.lines = lines
        self.depth = 0
        self.end = -1  # the last line of the last top-level block GitHub gave a position
        self.blocks: list[_Block] = []
        self.nested: list[int] = []
        self.raw: list[int] = []  # where raw HTML is, as near as GitHub's output tells

    def at(self, line: int) -> str:
        return self.lines[line] if 0 <= line < len(self.lines) else ""

    def _after(self) -> int:
        line = self.end + 1
        while line < len(self.lines) and not self.lines[line].strip():
            line += 1
        return line

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        position = re.fullmatch(r"(\d+):\d+-(\d+):\d+", dict(attrs).get("data-sourcepos") or "")
        first, last = (int(position[1]) - 1, int(position[2]) - 1) if position else (None, None)
        line = first
        if tag in _HEADINGS and first is not None and last is not None and first != last:
            # A setext heading: name its underline, the line that makes it one. cmark-gfm's end
            # for one can run onto the next line, so the underline is looked for, not assumed.
            underline = (n for n in range(first + 1, last + 1) if _is_underline(self.at(n)))
            line = next(underline, first)
        if self.depth == 0:
            gap = self._after()
            self.blocks.append(_Block(tag, gap if line is None else line, gap))
            if last is not None:
                self.end = last
        elif tag in _HEADINGS:
            self.nested.append(self._after() if line is None else line)
        if tag == "li" and self.depth == 1 and self.blocks and first is not None:
            self.blocks[-1].items.append(first)
            self.blocks[-1].item_texts.append("")
        if tag not in _VOID:
            self.depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.depth -= 1

    def handle_endtag(self, tag: str) -> None:
        if tag not in _VOID:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.depth >= 1 and self.blocks and self.blocks[-1].kind in _HEADINGS:
            self.blocks[-1].text += data
        if self.depth >= 2 and self.blocks and self.blocks[-1].item_texts:
            self.blocks[-1].item_texts[-1] += data

    def handle_comment(self, data: str) -> None:
        # cmark-gfm's safe mode writes raw HTML, anywhere, as this comment: GitHub renders the
        # HTML itself, sanitized, so a `<h2>` or a `</li></ul>` inside an entry would pass a
        # check of the comment's place alone (QA r4 on #732). Any raw HTML is refused.
        self.raw.append(self._after())


@dataclass
class _Layout:
    """The notes as GitHub renders them: each category's items' lines, and the first block that
    breaks the layout, with why (`heading`, `html` or `split`), or None."""

    categories: dict[str, list[int]]
    problem: tuple[str, _Block] | None
    item_texts: dict[str, list[str]] = field(default_factory=dict)


def _layout(text: str) -> _Layout:
    """Read notes as GitHub renders them, with cmark-gfm, the parser GitHub's Markdown is built
    on (QA r3 on #732: four rounds of regex rules each missed a Markdown rule, and markdown-it-py,
    a CommonMark parser, missed GitHub's HTML blocks after a list item's text and its footnotes):
    `## <version>`, any free text, then each category's `### <name>` and one list. The first
    problem is a heading inside a block, a heading that isn't the next category's (`heading`), raw
    HTML anywhere (`html`), or a block between a category's heading and its list or after its
    list (`split`)."""
    shape = _Shape(text.split("\n"))
    shape.feed(cmarkgfm.github_flavored_markdown_to_html(text, options=_OPTIONS))
    shape.close()
    problems = [("heading", _Block("heading", line, line)) for line in shape.nested]
    problems += [("html", _Block("html", line, line)) for line in shape.raw]
    categories: dict[str, list[int]] = {}
    item_texts: dict[str, list[str]] = {}
    names = list(CATEGORIES.values())
    current: str | None = None
    for index, block in enumerate(shape.blocks):
        if index == 0:
            if block.kind != "h2":
                problems.append(("heading" if block.kind in _HEADINGS else "split", block))
        elif block.kind in _HEADINGS:
            text_ = block.text.strip()
            last = names.index(current) if current else -1
            if block.kind != "h3" or text_ not in names or names.index(text_) <= last:
                problems.append(("heading", block))
            else:
                current = text_
                categories[current] = []
        elif current is None:
            pass  # the release's own text, before the first category
        elif block.kind == "ul" and not categories[current]:
            categories[current] = block.items
            item_texts[current] = block.item_texts
        else:
            problems.append(("split", block))
    first = min(problems, key=lambda p: p[1].line) if problems else None
    return _Layout(categories, first, item_texts)


SKIP_LABEL = "no changelog"
_ENTRY = re.compile(r"(?P<pr>[1-9][0-9]*)\.(?:added|changed|fixed)(?:\.[1-9][0-9]*)?\.md")
_RELEASE = re.compile(r"\+[a-z0-9][a-z0-9-]*\.release\.md")
_HOW = "see changelog.d/README.md"
# What the running servers' update check looks for in a release's notes (cli/version.py): only a
# release's own text may hold it (pre-QA r5 on #731).
SECURITY_MARKER = "**Security release:**"
# towncrier's start marker (pyproject.toml), or a comment that would hide the rest of the notes.
_COMMENT = "<!--"


def _read_text(path: Path, shown: str) -> tuple[str, str | None]:
    """``path``'s text and None, or "" and why it can't be read. A file that isn't UTF-8 is
    told how to save it (#734): Windows PowerShell 5.1's `echo ... >` and `Out-File` write
    UTF-16 with a byte-order mark, and an "ANSI" editor or `Set-Content` writes cp1252."""
    try:
        data = path.read_bytes()
    except OSError as error:
        return "", f"couldn't read {shown}: {type(error).__name__}"
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return "", f"{shown} is UTF-32: save it as UTF-8 without a byte-order mark ({_HOW})."
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "", (
            f"{shown} is UTF-16 (Windows PowerShell's `>` and Out-File write it): save it as "
            f"UTF-8 without a byte-order mark ({_HOW})."
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        line = data.count(b"\n", 0, error.start) + 1
        return "", (
            f"{shown} isn't UTF-8 (line {line} holds a byte "
            f"that doesn't decode; an editor's \"ANSI\" or cp1252 save does that): save it as "
            f"UTF-8 without a byte-order mark ({_HOW})."
        )
    # Universal newlines, as read_text() gave: \r\n and \r become \n.
    return text.replace("\r\n", "\n").replace("\r", "\n"), None


def fragment_problems(root: Path) -> list[str]:
    """Every reason a file in `changelog.d/` wouldn't assemble into the notes as written."""
    directory = root / DIRECTORY
    if not directory.is_dir():
        return [f"{DIRECTORY}/ is missing: {_HOW}."]
    found: list[str] = []
    contents: dict[str, list[str]] = {}
    for path in sorted(directory.iterdir()):
        name = path.name
        if not_a_fragment(name):
            continue
        shown = f"{DIRECTORY}/{name}"
        entry, release = _ENTRY.fullmatch(name), _RELEASE.fullmatch(name)
        if path.is_symlink():
            # towncrier reads through a link: the build would copy whatever it points at, on the
            # machine that builds the release, into the public notes (pre-QA r5 on #731).
            found.append(f"{shown} is a symbolic link: a fragment is a file of its own ({_HOW}).")
            continue
        if not path.is_file() or not (entry or release):
            found.append(
                f"{shown} isn't a fragment name, so the notes would leave it out: name it "
                f"<PR>.<added|changed|fixed>.md, or +<name>.release.md for a release's own "
                f"text ({_HOW})."
            )
            continue
        text, unreadable = _read_text(path, shown)
        if unreadable:
            found.append(unreadable)
            continue
        problem = _content_problem(text, entry.group("pr") if entry else None)
        if problem:
            found.append(f"{shown}: {problem}")
        # towncrier merges identical entries only within a category (pre-QA r10 on #732).
        category = entry.group(0).split(".")[1] if entry else "release"
        contents.setdefault(f"{category}\n{text.strip()}", []).append(shown)
    for names in contents.values():
        if len(names) > 1:
            # towncrier prints identical entries once, so the notes would lose one (QA r4).
            found.append(
                f"{_named(names)} hold the same text in one category, which towncrier would "
                f"print once: "
                f"remove one or give each its own ({_HOW})."
            )
    return found


def _content_problem(text: str, pr: str | None) -> str | None:
    """Why ``text`` isn't a fragment's content, or None. ``pr`` is the entry's PR number from its
    name, or None for a release's own text."""
    if text.startswith(_BOM):
        # Windows Notepad's "UTF-8" adds one; without this, the entry check below says the
        # file doesn't start with `- `, which it visibly does (pre-QA r7 on #732).
        return "it starts with a byte-order mark (U+FEFF): save it as UTF-8 without a BOM."
    if not text.strip():
        return "it's empty."
    size = len(text.encode("utf-8"))
    if size > _MAX_BYTES:
        return (
            f"it's {size:,} bytes, over the {_MAX_BYTES // 1024} KiB a fragment may be: an entry "
            f"is a line or a short paragraph, a release's own text a few ({_HOW})."
        )
    # Markdown, towncrier and publish.yml's awk break lines only at \n, \r and \r\n; str's
    # splitlines() breaks at eight more, so the check read another document (QA r4 on #732).
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    invisible = _invisible_problem(text)
    if invisible:
        return invisible
    lines = text.strip("\n").split("\n")
    lead = len(text) - len(text.lstrip("\n"))  # blank lines before the first, for line numbers
    for at, line in enumerate(lines, start=1 + lead):
        if _LINE_HEADING.match(line):
            return (
                f"line {at}, {line[:60]!r}, reads as a heading; the notes add the category "
                f"headings ({_HOW})."
            )
    if _COMMENT in text:
        return "it has `<!--`, which would hide the rest of the notes (towncrier's marker too)."
    if pr is not None:
        if SECURITY_MARKER in text:
            return (
                f"only a release's own text (+<name>.release.md) says {SECURITY_MARKER}: the "
                f"servers' update check announces any release whose notes hold it ({_HOW})."
            )
        if not lines[0].startswith("- "):
            return "an entry starts with `- `, as it would under its heading in CHANGELOG.md."
    problem = _render_problem(lines, lead, release=pr is None)
    if problem:
        return problem
    if pr is not None:
        number = re.search(r"\(#([0-9]+)", lines[0])
        if number is None or number.group(1) != pr:
            return f"its first (#N) isn't #{pr}, the PR number in its name."
    return None


def _invisible_problem(text: str) -> str | None:
    """Why ``text`` holds a character a reader of the notes can't see or that a terminal acts
    on, or None: a control character but tab and line feed, a format character (zero-width and
    direction marks among them), a line or paragraph separator (QA r4 on #732), a space other
    than U+0020 (pre-QA r10). tests/test_hidden_characters.py refuses a few more in every
    tracked file (U+2800, Hangul fillers), with Unicode data this check runs without. Named, as
    the byte-order mark is, since none of them shows in an editor."""
    for index, character in enumerate(text):
        category = unicodedata.category(character)
        if (
            (category == "Cc" and character not in "\t\n")
            or category in ("Cf", "Zl", "Zp")
            or (category == "Zs" and character != " ")
        ):
            line = text.count("\n", 0, index) + 1
            name = unicodedata.name(character, "a control character")
            if character == "\0" and text.count("\0") * 3 >= len(text):
                # UTF-16 without a byte-order mark reads as UTF-8 with a NUL beside each
                # ASCII character (#734).
                return (
                    f"it holds U+0000 throughout, as UTF-16 does: save it as UTF-8 without a "
                    f"byte-order mark ({_HOW})."
                )
            return (
                f"line {line} holds U+{ord(character):04X} ({name}), which doesn't show in the "
                f"notes or acts on a terminal: remove it ({_HOW})."
            )
    return None


def _more_than_one(shown: str) -> str:
    return (
        f"it holds more than one entry ({shown} starts another); give each its own file ({_HOW})."
    )


def _render_problem(lines: list[str], lead: int, *, release: bool) -> str | None:
    """Why the fragment, rendered among its neighbors as a release assembles it, would change
    them or show what the notes don't hold (QA r3 on #732), or None. An entry must stay one item
    of its category's list, with the entries around it and the next category as they were; a
    release's own text, before the categories, must leave them as they were. Neither may hold a
    heading or raw HTML, at any depth."""
    if release:
        before = [_TITLE, ""]
        after = ["", f"### {CATEGORIES['added']}", "", _NEXT]
        expected = {CATEGORIES["added"]: 1}
    else:
        before = [_TITLE, "", f"### {CATEGORIES['added']}", "", _ABOVE]
        after = [_BELOW, "", f"### {CATEGORIES['fixed']}", "", _NEXT]
        expected = {CATEGORIES["added"]: 3, CATEGORIES["fixed"]: 1}
    layout = _layout("\n".join([*before, *lines, *after]) + "\n")

    def where(line: int) -> str | None:
        """The fragment's own line at the notes' ``line``, for the message, or None."""
        index = line - len(before)
        if 0 <= index < len(lines):
            return f"line {index + 1 + lead}, {lines[index][:60]!r},"
        return None

    if layout.problem:
        kind, block = layout.problem
        # A block GitHub writes nothing for (a footnote definition) shows only as the next
        # block's start: the line that did it is the first after the block before.
        line = block.line if where(block.line) or kind != "split" else block.gap
        shown = where(line)
        index = line - len(before)
        footnote = shown is not None and _FOOTNOTE.match(lines[index]) is not None
        if block.kind == "section" or footnote:
            # GitHub's footnotes section, which it puts at the end of the document: in
            # CHANGELOG.md, after every older release (pre-QA r8 on #732). At column 0 a
            # definition also ends the list, but indenting it wouldn't help, so say what would.
            body = "\n".join(lines)
            code_free = _no_code(body)
            marks = [m for m in _DEFINITION.finditer(body) if m[1]]
            marks = [m for m in marks if code_free[m.start(1) - 1] == "["] or marks
            notes = [body.count("\n", 0, m.start(1)) for m in marks]
            at = f"line {notes[0] + 1 + lead}, {lines[notes[0]][:60]!r}, " if notes else ""
            return (
                f"{at}defines a footnote, which GitHub would show at the end of CHANGELOG.md, "
                f"after every older release: write the note in the entry's text ({_HOW})."
            )
        if kind == "heading":
            # Only a line of the fragment can be one; the neighbors' headings are the template's.
            return (
                f"{shown or 'a line'} reads as a heading on GitHub; the notes add the category "
                f"headings ({_HOW})."
            )
        if kind == "html":
            # At a line's start an HTML block ends the entry and turns the next PR's into raw
            # text (QA r3 on #732); inside it, GitHub renders the tags, so `<h2>` adds a heading
            # and `</li></ul>` closes the list (QA r4). Only the line holding it is named.
            body = "\n".join(lines)
            # An escape (`\<`, `\\`) is blanked first, so `\<b>` isn't a tag and `\\<br>` is.
            found = _RAW_TAG.search(re.sub(r"\\[^\n]", "  ", _no_code(body)))
            at = "it"
            if found:
                raw = body.count("\n", 0, found.start())
                at = f"line {raw + 1 + lead}, {lines[raw][:60]!r},"
            return (
                f"{at} holds raw HTML, which GitHub renders as HTML or drops: write it in "
                f"Markdown, and a placeholder such as `<name>` in backticks ({_HOW})."
            )
        if shown and _BULLET.match(lines[line - len(before)].expandtabs(4)):
            # Another marker (`*`, `+`) starts a list of its own: to the author, a second entry.
            return _more_than_one(shown)
        return (
            f"{shown or 'a line'} starts a block outside the entry, which would split or swallow "
            f"the notes after it: indent the entry's later lines 2 spaces or more ({_HOW})."
        )
    if not release:
        items = layout.categories.get(CATEGORIES["added"], [])
        extra = [line for line in items[1:-1] if line > len(before)]
        if len(items) > expected[CATEGORIES["added"]] and extra:
            return _more_than_one(where(extra[0]) or "a line")
        item_texts = layout.item_texts.get(CATEGORIES["added"], [])
        if len(item_texts) == expected[CATEGORIES["added"]] and not item_texts[1].strip():
            # `- [x]: url (#N)` is a definition, so the item renders empty (QA r4 on #732).
            return f"the entry renders with no text on GitHub ({_HOW})."
    if {name: len(items) for name, items in layout.categories.items()} != expected:
        # A block the fragment opens and never closes (a code fence, most likely) took in the
        # notes after it: the entries below it and the next category never render.
        return (
            "it opens a block it doesn't close (a code fence, most likely), which would swallow "
            "the rest of the notes."
        )
    return _definition_problem(lines, lead, before, after)


def _no_code(text: str) -> str:
    """``text`` with its fenced blocks and code spans blanked out, line breaks kept, for naming a
    line (never for deciding): either can span lines (pre-QA r10 on #732)."""

    def blank(found: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", found[0])

    # A fence closes on a run of its own character at least as long as the one that opened it
    # (#734: a longer close blanked everything after it).
    fenced = re.sub(r"(?ms)^ *((`|~)\2{2,}).*?(?:^ *\1\2*[ \t]*$|\Z)", blank, text)
    return re.sub(r"(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)", blank, fenced)


def _probe_renders(notes: list[str], references: list[str]) -> list[str]:
    """How GitHub renders each of ``references`` after ``notes``, one paragraph each, in order.
    Each is a paragraph of its own: in one, a backtick or a `<` in one label paired with
    another's into a code span or a tag, and neither became a link (QA r5 on #732). Only the
    paragraphs from the references' own lines count, so a footnotes section, whose paragraphs
    carry its definitions' earlier lines, needs no special case (#735)."""
    probe = "\n\n".join(["\n".join(notes), *references]) + "\n"
    rendered = cmarkgfm.github_flavored_markdown_to_html(probe, options=_OPTIONS)
    found = re.findall(r'<p data-sourcepos="(\d+):[^"]*">(.*?)</p>', rendered, re.S)
    return [html for line, html in found if int(line) >= len(notes) + 2]


def _labels(found: re.Match[str]) -> list[str]:
    """The labels GitHub may match for a candidate whose label runs onto later lines: its
    whitespace collapsed; with each later line's quote markers taken off (QA r5 on #732); and
    with them taken off only up to the first line's quote depth. All are probed: a `>` on a
    line indented into a paragraph, or past the quote's depth, is text in the label (#735), and
    which one a line holds isn't read here."""
    collapsed = " ".join(found[2].split())
    unquoted = " ".join(re.sub(r"\n[ \t>]*", " ", found[2]).split())
    # In a quote, a later line's own `>` past the quote's depth is text (pre-QA r1 on #735:
    # `> [foo` / `>     >bar]` is GitHub's `foo >bar`, which neither form above gives).
    depth = found[0][: found[0].index("[")].count(">")
    in_quote = " ".join(re.sub(rf"\n(?:[ \t]*>[ ]?){{0,{depth}}}", " ", found[2]).split())
    return list(dict.fromkeys([collapsed, unquoted, in_quote]))


def _definition_problem(
    lines: list[str], lead: int, before: list[str], after: list[str]
) -> str | None:
    """Why the fragment defines a link reference or a footnote, or None. A definition applies to
    the whole document: `[label]` or `[^label]` in any other entry, or in an older release, would
    take its URL or its note, shown at the end of CHANGELOG.md (pre-QA r9 and QA r4 on #732).
    Each candidate's references (one per label GitHub may match) are rendered after the notes
    twice, with the fragment and without it: one that renders otherwise with it is a label the
    fragment defined, whatever it links to (GitHub links an email by itself, pre-QA r10)."""
    body = "\n".join(lines)
    candidates = list(_DEFINITION.finditer(body))
    if not candidates:
        return None
    references: list[str] = []
    owners: list[int] = []
    for number, found in enumerate(candidates):
        for label in _labels(found):
            references.append(f"QZ{len(references)}ZQ [{found[1]}{label}]")
            owners.append(number)
    with_it = _probe_renders([*before, *lines, *after, ""], references)
    without = _probe_renders([*before, *after, ""], references)
    defined = sorted(
        {owners[n] for n, html in enumerate(with_it) if n >= len(without) or html != without[n]}
    )
    if not defined:
        return None
    # Labels match regardless of case, so `[foo]: b` in a code block renders as defined when
    # `[Foo]: url` is the definition: name one outside code (#735).
    code_free = _no_code(body)
    outside = [n for n in defined if code_free[candidates[n].start(1) - 1] == "["]
    # When every one that renders as defined is in code, the definition itself isn't a
    # candidate (no prefix the pattern takes): the code line is the nearest to name.
    found = candidates[(outside or defined)[0]]
    index = body.count("\n", 0, found.start(1))
    at = f"line {index + 1 + lead}, {lines[index][:60]!r},"
    if found[1]:
        return (
            f"{at} defines a footnote, which GitHub would show at the end of CHANGELOG.md, "
            f"after every older release, and give to any other entry's [^{found[2]}]: write "
            f"the note in the entry's text ({_HOW})."
        )
    return (
        f"{at} defines a link reference, which would apply to all of CHANGELOG.md, other "
        f"entries and older releases too: write the link inline, [text](url) ({_HOW})."
    )


def branch_problem(root: Path, base: str, pr: int, labels: list[str]) -> str | None:
    """Why this branch needs a fragment it doesn't have, or None: it adds or changes one named for
    PR ``pr`` (`<pr>.<type>[.N].md`) and removes none, is a release PR that assembled them all,
    or has the skip label. Another PR's fragment doesn't count: editing it would lose that entry,
    and one named for an issue would cite the wrong number in the notes (pre-QA r1 on #731).
    A release's own text (`+<name>.release.md`) is added or changed only by a release PR, the
    label or not: it holds `**Security release:**`, which the running servers announce to every
    session (QA r4 on #732)."""
    try:
        # --no-renames: a fragment moved out of changelog.d is its removal and another file's
        # addition, not a change to a fragment.
        changed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "diff",
                "--name-status",
                "--no-renames",
                f"{base}...HEAD",
                "--",
                DIRECTORY,
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError) as error:
        return f"couldn't list this branch's changes against {base}: {type(error).__name__}"
    release_texts = sorted(
        Path(path).name
        for status, _, path in (line.partition("\t") for line in changed)
        if status in ("A", "M") and _RELEASE.fullmatch(Path(path).name)
    )
    if release_texts and _release_version(root, base) is None and _bumped(root, base):
        # Written before the build, as release step 4 says: the build makes it a release PR.
        return (
            f"this PR bumps the version to {_head_version(root)} and adds or changes a release's "
            f"own text ({_named(release_texts)}), but CHANGELOG.md has no section for it yet: "
            f"run `{TOWNCRIER} build --version {_head_version(root)} --yes` (CLAUDE.md, release "
            f"step 4), which assembles the text and every fragment into it."
        )
    if release_texts and _release_version(root, base) is None:
        return (
            f"this PR adds or changes a release's own text ({_named(release_texts)}), which only "
            f"a release PR does, the {SKIP_LABEL!r} label or not: it's printed first in the "
            f"release's notes, and a `**Security release:**` line there is announced by every "
            f"running server "
            f"({_HOW}). Move it into the release PR."
        )
    if release_texts and any(_RELEASE.fullmatch(name) for name in _fragments_left(root)):
        # A release PR may add its text only to build it: left unbuilt, it opens the next
        # release's notes, the label or not (QA r5 on #732).
        # The section is there (that's what makes it a release PR), so towncrier refuses to
        # build again: the text goes in by hand, as _removal_problem says (pre-QA r11).
        # Reverting the build to build again also reverted the version bump when the release
        # was committed as one commit (release step 6), and the branch stopped being a release
        # PR (#735): the hand move is the advice.
        version = _release_version(root, base)
        left = _fragments_left(root)
        texts = [name for name in left if _RELEASE.fullmatch(name)]
        others = [name for name in left if name not in texts]
        also = (
            f", and each other fragment's entry ({_named(others)}) into its category there"
            if others
            else ""
        )
        return (
            f"this release PR ({version}) leaves its own text unbuilt ({_named(texts)}), the "
            f"{SKIP_LABEL!r} label or not: towncrier won't build {version} again, so move the "
            f"text into the '## {version}' section by hand (a `**Security release:**` line "
            f"first){also}, and delete the files."
        )
    if SKIP_LABEL in labels:
        return None
    removed: list[str] = []
    own = False
    for line in changed:
        status, _, path = line.partition("\t")
        name = Path(path).name
        if not_a_fragment(name):
            continue
        entry = _ENTRY.fullmatch(name)
        if status == "D":
            # Its own fragment, moved to another category or rewritten, is no other PR's loss.
            if not (entry and entry.group("pr") == str(pr)):
                removed.append(name)
            continue
        # A release's own text (+<name>.release.md) never counts: the release PR's build removes
        # it with the rest, so counting it only let any PR pass by adding one (pre-QA r2 on #731).
        if status in ("A", "M") and entry and entry.group("pr") == str(pr):
            own = True
    # Removals first: a PR's own fragment doesn't excuse deleting another's (pre-QA r4 on #731).
    if removed:
        return _removal_problem(root, base, removed)
    if own:
        return None
    return (
        "this PR adds no CHANGELOG fragment of its own: add "
        f"{DIRECTORY}/{pr}.<added|changed|fixed>.md "
        f"({_HOW}; an entry in CHANGELOG.md, or another PR's fragment, doesn't count), or, for a "
        f"change that needs no entry, ask the maintainer for the {SKIP_LABEL!r} label, then rerun "
        "this job."
    )


def _named(names: list[str], limit: int = 10) -> str:
    """``names`` for a message, the first ``limit`` and a count of the rest."""
    shown = ", ".join(names[:limit])
    return shown if len(names) <= limit else f"{shown} and {len(names) - limit} more"


def _removal_problem(root: Path, base: str, removed: list[str]) -> str | None:
    """Why a PR that removes fragments can't pass as it is, or None for a release PR that
    assembled them all. Only a release removes fragments without the label (pre-QA r3 and r4 on
    #731): a release is a branch that bumps `pyproject.toml`'s version and adds its
    `## <version> (` section. Every other removal (a revert, a backport's carry-back, moving
    late fragments into a release's notes) is a judgment the maintainer makes with the label."""
    release = _release_version(root, base)
    left = _fragments_left(root)
    if release and not left:
        return None
    if release:
        # A rebuild is refused (towncrier: "already produced newsfiles for this version").
        return (
            f"this release PR ({release}) leaves {len(left)} CHANGELOG fragment(s) "
            f"({_named(left)}), most likely merged to main after its build: merge main, move each "
            f"one's entry into the '## {release}' section by hand and delete the file."
        )
    return (
        f"this PR removes CHANGELOG fragments ({_named(sorted(removed))}), which only a release "
        "PR does (one that bumps pyproject.toml's version and adds its section). If that was a "
        "mistake, restore them. If it's meant (a revert dropping the reverted PR's entry, a "
        "backport's carry-back, or moving fragments that merged after a release's build into its "
        f"notes), ask the maintainer for the {SKIP_LABEL!r} label, then rerun this job."
    )


def _fragments_left(root: Path) -> list[str]:
    directory = root / DIRECTORY
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if not not_a_fragment(p.name))


def _git_show(root: Path, revision: str, path: str) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), "show", f"{revision}:{path}"],
            capture_output=True,
            # CHANGELOG.md isn't ASCII: decoding by the locale crashed on a C or cp1252 locale,
            # as check_release.py's _git explains (pre-QA r6 on #732).
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def _version(pyproject: str | None) -> str | None:
    try:
        version = tomllib.loads(pyproject or "")["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError, TypeError):
        return None
    return version if isinstance(version, str) else None


def _head_version(root: Path) -> str:
    """`pyproject.toml`'s version on this branch, or `<version>` when it can't be read."""
    try:
        return _version((root / "pyproject.toml").read_text(encoding="utf-8")) or "<version>"
    except (OSError, UnicodeDecodeError):
        return "<version>"


def _bumped(root: Path, base: str) -> bool:
    """Whether `pyproject.toml`'s version differs from the base's."""
    try:
        head = _version((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return False
    return head is not None and head != _version(_git_show(root, base, "pyproject.toml"))


def _release_version(root: Path, base: str) -> str | None:
    """The version this branch releases, or None: `pyproject.toml`'s version differs from the
    base's, and `CHANGELOG.md` gains that version's `## <version> (` section."""
    try:
        head_pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        head_changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    version = _version(head_pyproject)
    if version is None or version == _version(_git_show(root, base, "pyproject.toml")):
        return None
    heading = re.compile(rf"^## {re.escape(version)} \(", re.MULTILINE)
    if not heading.search(head_changelog) or heading.search(
        _git_show(root, base, "CHANGELOG.md") or ""
    ):
        return None
    return version


def _start_string(root: Path) -> str | None:
    """towncrier's `start_string` from `pyproject.toml`: where the build inserts the release.
    Read, never copied, so the check and the build can't disagree (pre-QA r3 on #731)."""
    try:
        config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        value = config["tool"]["towncrier"]["start_string"]
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError):
        return None
    return value if isinstance(value, str) and value.strip() else None


def changelog_problems(root: Path) -> list[str]:
    """Why `CHANGELOG.md` isn't laid out as the build needs (pre-QA r2 and r3 on #731): one
    `## Unreleased` heading, towncrier's `start_string` exactly once (newline included, as
    towncrier matches it), inside that section, and nothing else there. Elsewhere, or with
    trailing spaces, the build writes the release above `# Changelog` or into an older
    version's notes; an entry there would reach only the tag gate, after the release PR
    merged."""
    start = _start_string(root)
    if start is None:
        return ["pyproject.toml has no [tool.towncrier] start_string, which the build needs."]
    text, problem = _read_text(root / "CHANGELOG.md", "CHANGELOG.md")
    if problem:
        return [problem]
    marker = start.strip()
    found: list[str] = []
    headings = list(re.finditer(r"^## Unreleased[ \t]*$", text, re.MULTILINE))
    if len(headings) != 1:
        found.append(
            f"CHANGELOG.md has {len(headings)} '## Unreleased' headings; it needs exactly one, "
            "above its releases, holding only towncrier's start marker."
        )
    if text.count(start) != 1:
        found.append(
            f"CHANGELOG.md holds {text.count(start)} of towncrier's start marker, {marker!r}, on "
            "a line of its own; it needs exactly one, under '## Unreleased', where the "
            "release's section goes."
        )
    if not headings:
        return found
    rest = text[headings[0].end() :]
    following = re.search(r"^## ", rest, re.MULTILINE)
    section = rest[: following.start() if following else None]
    if start not in section:
        found.append(f"towncrier's start marker, {marker!r}, isn't under '## Unreleased'.")
    if section.replace(start, "").replace(marker, "").strip():
        found.append(
            "CHANGELOG.md's '## Unreleased' holds something besides towncrier's marker: an "
            f"entry goes in a fragment, {DIRECTORY}/<PR>.<added|changed|fixed>.md ({_HOW})."
        )
    return found


def notes_problems(root: Path, draft: str) -> list[str]:
    """Why the next release's notes, as `towncrier build --draft` writes them, wouldn't show
    as the fragments say (QA r3 on #732): read as GitHub renders them, they hold the version's
    heading, then only the category headings, each with one list of as many items as that
    category has fragments, and no other heading, nor raw HTML. Each
    fragment is checked on its own too; this checks what the template makes of them together."""
    found = re.search(r"^## ", draft, re.MULTILINE)
    if found is None:
        return ["towncrier's draft of the notes has no '## <version>' heading."]
    notes = draft[found.start() :]
    layout = _layout(notes)
    problems: list[str] = []
    if layout.problem:
        kind, block = layout.problem
        line = block.line
        rows = notes.split("\n")
        shown = rows[line][:60] if line < len(rows) else ""
        if kind == "html":
            # GitHub's output gives raw HTML no position: `check` names the fragment and line.
            problems.append(
                "towncrier's draft of the notes holds raw HTML: run `check`, which names the "
                "fragment and its line."
            )
        else:
            what = {"heading": "a heading", "split": "a block"}[kind]
            problems.append(
                f"towncrier's draft of the notes has {what} where the layout doesn't take one, "
                f"at line {line + 1} of the notes, {shown!r}: check the fragment it comes from."
            )
    counts: dict[str, int] = {}
    directory = root / DIRECTORY
    for path in directory.iterdir() if directory.is_dir() else []:
        entry = _ENTRY.fullmatch(path.name)
        if entry:
            name = CATEGORIES[path.name.split(".")[1]]
            counts[name] = counts.get(name, 0) + 1
    rendered = {name: len(items) for name, items in layout.categories.items()}
    if rendered != counts:
        problems.append(
            f"towncrier's draft of the notes renders {rendered or 'no entries'} where "
            f"changelog.d/ holds {counts or 'no entries'} (entries per category)."
        )
    return problems


def _annotation(message: str) -> str:
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error::{escaped}"


def main(argv: list[str], root: Path = Path()) -> int:
    # A refusal quotes the fragment's own text: on a console that can't encode it (cp1252 on
    # Windows, a pipe), print an escape rather than crash before printing (QA r5 on #732), as
    # src/mcp_synology/cli/startup.py does.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(prog="changelog_fragments.py")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="check the fragments (and a PR's, with --base)")
    check.add_argument("--base", help="the PR's base commit: also require a fragment")
    check.add_argument("--pr", type=int, help="the PR's number (with --base)")
    check.add_argument("--labels", default="", help="the PR's labels, comma-separated")
    notes = commands.add_parser("notes", help="check towncrier's draft of the next notes")
    notes.add_argument("draft", type=Path, help="the file `towncrier build --draft` wrote")
    args = parser.parse_args(argv)
    if args.command == "notes":
        draft, problem = _read_text(args.draft, str(args.draft))
        if problem:
            print(_annotation(problem))
            return 1
        found = notes_problems(root, draft)
        for message in found:
            print(_annotation(message))
        if not found:
            print("The draft of the next release's notes renders as its fragments say.")
        return 1 if found else 0
    found = fragment_problems(root) + changelog_problems(root)
    if args.base:
        if args.pr is None:
            parser.error("--base needs --pr")
        labels = [label.strip() for label in args.labels.split(",") if label.strip()]
        problem = branch_problem(root, args.base, args.pr, labels)
        if problem:
            found.append(problem)
    for problem in found:
        print(_annotation(problem))
    if found:
        return 1
    print("The CHANGELOG fragments check passes.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
