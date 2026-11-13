#!/usr/bin/env python3
"""Check the CHANGELOG fragments in `changelog.d/` (#731).

Each PR writes its CHANGELOG entry as a file there, and the release PR assembles them with
towncrier (CLAUDE.md, "Bumping the version for a release"). towncrier's own checks leave two gaps
this one closes: `towncrier build` would leave a misnamed file out of the notes (here it's
refused), and `towncrier check` passes any PR that edits `CHANGELOG.md` (here a PR passes only by
adding or changing a fragment named for its own number, by removing fragments as a release PR
does, or with the `no changelog` label).

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
NOT_FRAGMENTS = frozenset({"README.md", "template.md"})
SKIP_LABEL = "no changelog"
_ENTRY = re.compile(r"(?P<pr>[1-9][0-9]*)\.(?:added|changed|fixed)(?:\.[1-9][0-9]*)?\.md")
_RELEASE = re.compile(r"\+[a-z0-9][a-z0-9-]*\.release\.md")
_HOW = "see changelog.d/README.md"


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
    if not text.strip():
        return "it's empty."
    lines = text.strip("\n").splitlines()
    # A heading would end the section it's assembled into: towncrier writes the headings.
    if any(re.match(r"#{1,6} ", line) for line in _outside_code(lines)):
        return f"it has a heading; the notes add the category headings ({_HOW})."
    if pr is None:
        return None
    if not lines[0].startswith("- "):
        return "an entry starts with `- `, as it would under its heading in CHANGELOG.md."
    if any(re.match(r"[-*+] ", line) for line in _outside_code(lines[1:])):
        return f"it holds more than one entry; give each its own file ({_HOW})."
    number = re.search(r"\(#([0-9]+)", lines[0])
    if number is None or number.group(1) != pr:
        return f"its first (#N) isn't #{pr}, the PR number in its name."
    return None


def _outside_code(lines: list[str]) -> list[str]:
    """``lines`` without the ones inside fenced code blocks."""
    kept, fenced = [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            kept.append(line)
    return kept


def branch_problem(root: Path, base: str, pr: int, labels: list[str]) -> str | None:
    """Why this branch needs a fragment it doesn't have, or None: it adds or changes one named for
    PR ``pr`` (`<pr>.<type>[.N].md`), removes fragments (a release PR's build does), or has the
    skip label. Another PR's fragment doesn't count: editing it would lose that entry, and one
    named for an issue would cite the wrong number in the notes (pre-QA r1 on #731)."""
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
            text=True,
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError) as error:
        return f"couldn't list this branch's changes against {base}: {type(error).__name__}"
    removed: list[str] = []
    for line in changed:
        status, _, path = line.partition("\t")
        name = Path(path).name
        if name in NOT_FRAGMENTS:
            continue
        if status == "D":
            removed.append(name)
            continue
        entry = _ENTRY.fullmatch(name)
        # A release's own text (+<name>.release.md) never counts: the release PR's build removes
        # it with the rest, so counting it only let any PR pass by adding one (pre-QA r2 on #731).
        if status in ("A", "M") and entry and entry.group("pr") == str(pr):
            return None
    if removed:
        # Only a release PR removes fragments: one that bumps the version and adds its section
        # (pre-QA r3 on #731; "none left" alone let a PR delete another PR's last entry).
        release = _release_version(root, base)
        left = _fragments_left(root)
        if release and not left:
            return None
        if release:
            return (
                f"this release PR ({release}) leaves {len(left)} CHANGELOG fragment(s) "
                f"({', '.join(left)}), most likely merged after its build: merge main and run "
                "the build again."
            )
        return (
            f"this PR removes CHANGELOG fragments ({', '.join(sorted(removed))}) but isn't a "
            "release PR (it doesn't bump pyproject.toml's version and add that version's "
            "section): restore them, then add its own entry as "
            f"{DIRECTORY}/{pr}.<added|changed|fixed>.md. A backport's carry-back, which removes "
            f"the entries it ships, takes the {SKIP_LABEL!r} label (ask the maintainer)."
        )
    return (
        "this PR adds no CHANGELOG fragment of its own: add "
        f"{DIRECTORY}/{pr}.<added|changed|fixed>.md "
        f"({_HOW}; an entry in CHANGELOG.md, or another PR's fragment, doesn't count), or, for a "
        f"change that needs no entry, ask the maintainer for the {SKIP_LABEL!r} label, then rerun "
        "this job."
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
            text=True,
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
