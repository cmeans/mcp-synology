#!/usr/bin/env python3
"""Check the CHANGELOG fragments in `changelog.d/` (#731).

Each PR writes its CHANGELOG entry as a file there, and the release PR assembles them with
towncrier (CLAUDE.md, "Bumping the version for a release"). towncrier's own checks leave two gaps
this one closes: `towncrier build` would leave a misnamed file out of the notes (here it's
refused), and `towncrier check` passes any PR that edits `CHANGELOG.md` (here a PR passes only by
adding or changing a fragment named for its own number and removing none, as a release PR that
assembled every fragment, or with the `no changelog` label).

Standard library only. Each problem prints as a GitHub Actions error.

Usage:
    python scripts/changelog_fragments.py check
    python scripts/changelog_fragments.py check --base <base commit> --pr <N> --labels "<a>,<b>"
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

DIRECTORY = "changelog.d"
# Files in the directory that aren't fragments (also towncrier's `ignore`, in pyproject.toml).
# macOS writes .DS_Store into a folder Finder opens; towncrier would refuse it (pre-QA r6 on #732).
NOT_FRAGMENTS = frozenset({"README.md", "template.md", ".DS_Store"})
# A Markdown heading as CommonMark reads one inside the entry: a fragment is a list item whose
# content starts at column 2, so up to 5 spaces of indent (2 + 3), then 1-6 #, then a space, a
# tab or the line's end; or a setext underline (=== or ---) (pre-QA r6 and QA r2 on #732: each
# renders as a heading on GitHub's release page). Tabs are expanded to 4 columns first.
_HEADING = re.compile(r" {0,5}(?:#{1,6}(?:[ \t]|$)|(?:=+|-+)[ \t]*$)")
# A nested list item or block quote inside the entry, up to 5 spaces in (6 or more is indented
# code, pre-QA r7 on #732): a heading after one renders at any depth.
_NESTED = re.compile(r" {0,5}(?:(?:[-*+]|[0-9]{1,9}[.)])[ \t]+|>[ \t]?)+")
_NESTED_HEADING = re.compile(r"#{1,6}(?:[ \t]|$)")
# A line at column 0 or 1 isn't inside the entry, so one that opens a block ends the list: a
# fence or `<pre>` (and the like) turns the rest of the notes, later categories too, into one
# block; a quote, an ordered item or a thematic break splits the category's list (pre-QA r7 on
# #732, rendered with GitHub's Markdown API). Indent an entry's continuation lines 2 or more.
_BLOCK_START = re.compile(
    r" ?(?:```|~~~|<(?:pre|script|style|textarea|div)\b|>|[0-9]{1,9}[.)](?:[ \t]|$)"
    r"|(?:\*[ \t]*){3,}$|(?:_[ \t]*){3,}$)",
    re.IGNORECASE,
)
_FENCE = re.compile(r" {0,3}(?:```|~~~)")
_BOM = "\ufeff"


def _heading_line(lines: list[str]) -> tuple[int, str] | None:
    """The first line (1-based number, text) that would render as a heading, or None. Only lines
    indented 6 or more columns (indented code inside the entry) are exempt."""
    for number, line in enumerate(lines, start=1):
        expanded = line.expandtabs(4)
        if _HEADING.match(expanded):
            return number, line
        nested = _NESTED.match(expanded)
        if nested and _NESTED_HEADING.match(expanded[nested.end() :]):
            return number, line
    return None


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
    # Line by line, code blocks too: every reader of CHANGELOG.md (publish.yml's awk, the release
    # gate) is line-based, so a heading inside a fence still ends the section (pre-QA r5 on #731).
    heading = _heading_line(lines)
    if heading:
        at, shown = heading
        at += lead  # the file's own line number
        return (
            f"line {at}, {shown[:60]!r}, reads as a heading; the notes add the category "
            f"headings ({_HOW})."
        )
    if _COMMENT in text:
        return "it has `<!--`, which would hide the rest of the notes (towncrier's marker too)."
    if pr is None:
        # A release's own text may hold a fenced block (an Upgrading example) at column 0, but
        # an unclosed one would swallow the rest of the notes.
        if sum(1 for line in lines if _FENCE.match(line)) % 2:
            return (
                "it opens a code fence (``` or ~~~) it doesn't close, which would swallow the "
                "rest of the notes."
            )
        return None
    if SECURITY_MARKER in text:
        return (
            f"only a release's own text (+<name>.release.md) says {SECURITY_MARKER}: the "
            f"servers' update check announces any release whose notes hold it ({_HOW})."
        )
    if not lines[0].startswith("- "):
        return "an entry starts with `- `, as it would under its heading in CHANGELOG.md."
    if any(re.match(r"[-*+] ", line) for line in lines[1:]):
        return f"it holds more than one entry; give each its own file ({_HOW})."
    for at, line in enumerate(lines[1:], start=2 + lead):
        if _BLOCK_START.match(line.expandtabs(4)):
            return (
                f"line {at}, {line[:60]!r}, starts a block outside the entry, which would split "
                f"or swallow the notes after it: indent the entry's later lines 2 spaces or more "
                f"({_HOW})."
            )
    number = re.search(r"\(#([0-9]+)", lines[0])
    if number is None or number.group(1) != pr:
        return f"its first (#N) isn't #{pr}, the PR number in its name."
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
    args = parser.parse_args(argv)
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
