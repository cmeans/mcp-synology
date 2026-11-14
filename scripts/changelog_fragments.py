#!/usr/bin/env python3
"""Check the CHANGELOG fragments in `changelog.d/` (#731).

Each PR writes its CHANGELOG entry as a file there, and the release PR assembles them with
towncrier (CLAUDE.md, "Bumping the version for a release"). towncrier's own checks leave two gaps
this one closes: `towncrier build` would leave a misnamed file out of the notes (here it's
refused), and `towncrier check` passes any PR that edits `CHANGELOG.md` (here a PR passes only by
adding or changing a fragment named for its own number and removing none, as a release PR that
assembled every fragment, or with the `no changelog` label).

Each problem prints as a GitHub Actions error. How a fragment renders is read with `cmarkgfm`,
the Python binding of cmark-gfm, GitHub's own Markdown parser (QA r3 on #732), pinned in the dev
extra and in CI's `uv run --with`; everything else is standard library.

Usage:
    python scripts/changelog_fragments.py check
    python scripts/changelog_fragments.py check --base <base commit> --pr <N> --labels "<a>,<b>"
    python scripts/changelog_fragments.py notes <towncrier build --draft's output>
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import cmarkgfm
from cmarkgfm.cmark import Options

DIRECTORY = "changelog.d"
# Files in the directory that aren't fragments (also towncrier's `ignore`, in pyproject.toml).
# macOS writes .DS_Store into a folder Finder opens; towncrier would refuse it (pre-QA r6 on #732).
NOT_FRAGMENTS = frozenset({"README.md", "template.md", ".DS_Store"})
# The release tools read CHANGELOG.md line by line (publish.yml's awk, the release gate), so a
# heading at a line's start ends a section there even inside a code block, where GitHub shows it
# as code (pre-QA r5 on #731).
_LINE_HEADING = re.compile(r"#{1,6}(?:[ \t]|$)")
_BOM = "\ufeff"
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


# GitHub's own options for its Markdown, as far as structure goes, plus each block's source lines.
_OPTIONS = Options.CMARK_OPT_SOURCEPOS | Options.CMARK_OPT_FOOTNOTES
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# A setext heading's underline, at any depth (after a list marker or a quote's `>`).
_UNDERLINE = re.compile(r"[ \t>*+0-9.)-]*?(?:=+|-+)[ \t]*$")
_VOID = frozenset({"area", "br", "col", "embed", "hr", "img", "input", "source", "track", "wbr"})


@dataclass
class _Block:
    """A top-level block of rendered notes: its tag (`h2`, `ul`, `p`, `pre`, `hr`, ... or `html`
    for an HTML block, which GitHub writes as a comment with no source position), the line
    (0-based) it starts on (a setext heading's underline), the first line after the block before
    it (`gap`: where a block GitHub writes nothing for, such as a footnote definition, sat), a
    heading's text and a list's items' lines."""

    kind: str
    line: int
    gap: int
    text: str = ""
    items: list[int] = field(default_factory=list)


class _Shape(HTMLParser):
    """The top-level blocks of cmark-gfm's HTML, and the lines of every heading inside one."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.lines = lines
        self.depth = 0
        self.end = -1  # the last line of the last top-level block GitHub gave a position
        self.blocks: list[_Block] = []
        self.nested: list[int] = []

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
            underline = (n for n in range(first + 1, last + 1) if _UNDERLINE.match(self.at(n)))
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

    def handle_comment(self, data: str) -> None:
        if self.depth == 0:
            gap = self._after()
            self.blocks.append(_Block("html", gap, gap))


@dataclass
class _Layout:
    """The notes as GitHub renders them: each category's items' lines, and the first block that
    breaks the layout, with why (`heading`, `html` or `split`), or None."""

    categories: dict[str, list[int]]
    problem: tuple[str, _Block] | None


def _layout(text: str) -> _Layout:
    """Read notes as GitHub renders them, with cmark-gfm, GitHub's own Markdown parser (QA r3 on
    #732: four rounds of regex rules each missed a Markdown rule, and markdown-it-py, a CommonMark
    parser, missed GitHub's HTML blocks after a list item's text and its footnotes): `## <version>`,
    any free text, then each category's `### <name>` and one list. The first problem is a heading
    inside a block, a heading that isn't the next category's (`heading`), an HTML block outside
    every list (`html`), or a block between a category's heading and its list or after its list
    (`split`)."""
    shape = _Shape(text.splitlines())
    shape.feed(cmarkgfm.github_flavored_markdown_to_html(text, options=_OPTIONS))
    shape.close()
    problems = [("heading", _Block("heading", line, line)) for line in shape.nested]
    categories: dict[str, list[int]] = {}
    names = list(CATEGORIES.values())
    current: str | None = None
    for index, block in enumerate(shape.blocks):
        if block.kind == "html":
            problems.append(("html", block))
        elif index == 0:
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
        else:
            problems.append(("split", block))
    first = min(problems, key=lambda p: p[1].line) if problems else None
    return _Layout(categories, first)


SKIP_LABEL = "no changelog"
_ENTRY = re.compile(r"(?P<pr>[1-9][0-9]*)\.(?:added|changed|fixed)(?:\.[1-9][0-9]*)?\.md")
_RELEASE = re.compile(r"\+[a-z0-9][a-z0-9-]*\.release\.md")
_HOW = "see changelog.d/README.md"
# What the running servers' update check looks for in a release's notes (cli/version.py): only a
# release's own text may hold it (pre-QA r5 on #731).
SECURITY_MARKER = "**Security release:**"
# towncrier's start marker (pyproject.toml), or a comment that would hide the rest of the notes.
_COMMENT = "<!--"


def fragment_problems(root: Path) -> list[str]:
    """Every reason a file in `changelog.d/` wouldn't assemble into the notes as written."""
    directory = root / DIRECTORY
    if not directory.is_dir():
        return [f"{DIRECTORY}/ is missing: {_HOW}."]
    found: list[str] = []
    for path in sorted(directory.iterdir()):
        name = path.name
        if name in NOT_FRAGMENTS:
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
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            found.append(f"couldn't read {shown}: {type(error).__name__}")
            continue
        problem = _content_problem(text, entry.group("pr") if entry else None)
        if problem:
            found.append(f"{shown}: {problem}")
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
    lines = text.strip("\n").splitlines()
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


def _more_than_one(shown: str) -> str:
    return (
        f"it holds more than one entry ({shown} starts another); give each its own file ({_HOW})."
    )


def _render_problem(lines: list[str], lead: int, *, release: bool) -> str | None:
    """Why the fragment, rendered among its neighbors as a release assembles it, would change
    them or show what the notes don't hold (QA r3 on #732), or None. An entry must stay one item
    of its category's list, with the entries around it and the next category as they were; a
    release's own text, before the categories, must leave them as they were. Neither may hold a
    heading, at any depth, or an HTML block outside the entry."""
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
        if kind == "heading":
            # Only a line of the fragment can be one; the neighbors' headings are the template's.
            return (
                f"{shown or 'a line'} reads as a heading on GitHub; the notes add the category "
                f"headings ({_HOW})."
            )
        if kind == "html":
            # Indented into the entry, GitHub keeps one inside it; at a line's start, it ends the
            # entry and turns the next PR's into raw text (QA r3 on #732).
            return (
                f"{shown or 'a line'} starts an HTML block outside the entry, which would show "
                f"the entries after it as raw text: indent it 2 spaces or more, or write it in "
                f"Markdown ({_HOW})."
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
    if {name: len(items) for name, items in layout.categories.items()} != expected:
        # A block the fragment opens and never closes (a code fence, most likely) took in the
        # notes after it: the entries below it and the next category never render.
        return (
            "it opens a block it doesn't close (a code fence, most likely), which would swallow "
            "the rest of the notes."
        )
    return None


def branch_problem(root: Path, base: str, pr: int, labels: list[str]) -> str | None:
    """Why this branch needs a fragment it doesn't have, or None: it adds or changes one named for
    PR ``pr`` (`<pr>.<type>[.N].md`) and removes none, is a release PR that assembled them all,
    or has the skip label. Another PR's fragment doesn't count: editing it would lose that entry,
    and one named for an issue would cite the wrong number in the notes (pre-QA r1 on #731)."""
    if SKIP_LABEL in labels:
        return None
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
    removed: list[str] = []
    own = False
    for line in changed:
        status, _, path = line.partition("\t")
        name = Path(path).name
        if name in NOT_FRAGMENTS:
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
    return sorted(p.name for p in directory.iterdir() if p.name not in NOT_FRAGMENTS)


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
    try:
        text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return [f"couldn't read CHANGELOG.md: {type(error).__name__}"]
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
    category has fragments, and no other heading, nor an HTML block outside the lists. Each
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
        shown = notes.splitlines()[line][:60] if line < len(notes.splitlines()) else ""
        what = {"heading": "a heading", "html": "an HTML block", "split": "a block"}[kind]
        problems.append(
            f"towncrier's draft of the notes has {what} where the layout doesn't take one, at "
            f"line {line + 1} of the notes, {shown!r}: check the fragment it comes from."
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
        try:
            draft = args.draft.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            print(_annotation(f"couldn't read {args.draft}: {type(error).__name__}"))
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
