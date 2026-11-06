#!/usr/bin/env python3
"""Refuse a release tag that can't be right, before anything publishes (#457).

`publish.yml`'s `gate` job runs this first. A PyPI version can't be reused, so a tag on the wrong
commit or with the wrong version is costly to undo. It checks that:

- the tag is `v` plus `pyproject.toml`'s version, and `server.json` says the same;
- `CHANGELOG.md` has a section for that version with notes, which become the release notes and
  carry a security release's `**Security release:**` line, the one running servers announce;
- no section above that one has entries: a commit after the release commit would ship those
  changes with no notes and no live test. A later commit that added no entry still passes;
- the tagged commit (HEAD) is on `origin/main` or, for a patch on a released line, this version's
  backport branch, `release/<major>.<minor>.x` (as v0.5.3 was, shipping a fix without main's
  unreleased work). Not for a `.0`, which has no released line to patch: `release/0.11.x` or
  `release/0.11.0` (how this repo names a release PR's branch) would be an unmerged commit.

Standard library only. It needs a checkout with those branches fetched. Each problem prints as a
GitHub Actions error, so it reaches the run's summary.

Usage:
    python scripts/check_release.py v0.11.0
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

_RELEASE_STEPS = "CLAUDE.md's 'Bumping the version for a release'"
_TAG_THE_MERGE = (
    "tag its squash commit on origin/main (for a backport, its commit on the "
    "release/<major>.<minor>.x branch)"
)
# A line that isn't a note: a heading, a rule, or a bare bullet. `_has_notes` has already removed
# comments.
_NOT_A_NOTE = re.compile(r"#{1,6}(\s.*)?|[-*_ ]{3,}|[-*+]")


def tag_problem(tag: str, version: str) -> str | None:
    """Say why `tag` doesn't name `version`, or None. It never offers `version` as the tag: when
    the release PR hasn't merged, that's the version already released."""
    if tag == f"v{version}":
        return None
    return (
        f"tag {tag!r} doesn't match this commit: a release tag is v plus the version in its "
        f"release commit's pyproject.toml, and this one says {version!r}. If the release PR hasn't "
        f"merged, merge it and {_TAG_THE_MERGE}."
    )


def _has_notes(section: str) -> bool:
    uncommented = re.sub(r"<!--.*?-->", "", section, flags=re.DOTALL)
    return any(
        (line := raw.strip()) and not _NOT_A_NOTE.fullmatch(line)
        for raw in uncommented.splitlines()
    )


def _section(changelog: str, heading_pattern: str) -> str | None:
    """The text under the first `## ` heading matching ``heading_pattern``, or None."""
    heading = re.search(rf"^## {heading_pattern}.*$", changelog, re.MULTILINE)
    if heading is None:
        return None
    following = changelog[heading.end() :]
    next_heading = re.search(r"^## ", following, re.MULTILINE)
    return following[: next_heading.start() if next_heading else None]


def changelog_problem(changelog: str, version: str) -> str | None:
    """Say why `changelog` has no notes for `version`, or None.

    The heading matches what `publish.yml`'s `github-release` job extracts: `## <version>`, then a
    space or `(`.
    """
    escaped = re.escape(version)
    section = _section(changelog, rf"{escaped}(?: |\()")
    if section is None:
        if re.search(rf"^## {escaped}\s*$", changelog, re.MULTILINE):
            return f"CHANGELOG.md's '## {version}' heading has no date: '## {version} (<date>)'"
        return f"CHANGELOG.md has no '## {version} (<date>)' section: see {_RELEASE_STEPS}"
    return None if _has_notes(section) else f"CHANGELOG.md's section for {version} has no notes"


def unreleased_problem(changelog: str, version: str, *, tag_matches: bool = True) -> str | None:
    """Say why this commit looks later than the release commit, or None: a release leaves nothing
    above its own section (an empty `## Unreleased` or none, as on a backport branch), so entries
    there are changes that aren't in this version's notes. The likeliest route is retagging after
    a refusal, when `origin/main` has moved on. Any `## ` section above it counts, whatever it's
    called. With no section for this version (`changelog_problem` says so), only the first does.

    When the tag isn't this version, `version` has already shipped, so its notes aren't the place
    for those entries, and the advice to move them stays out."""
    headings = list(re.finditer(r"^## .*$", changelog, re.MULTILINE))
    own = re.compile(rf"## {re.escape(version)}(?: |\(|\s*$)")
    stop = next((i for i, h in enumerate(headings) if own.match(h.group())), 1)
    for index, heading in enumerate(headings[:stop]):
        end = headings[index + 1].start() if index + 1 < len(headings) else None
        if not _has_notes(changelog[heading.end() : end]):
            continue
        name = heading.group().strip()
        later = f"find the release PR and {_TAG_THE_MERGE}"
        return (
            f"CHANGELOG.md's {name!r} has entries, so this commit holds changes that aren't in "
            f"{version}'s notes. "
            + (
                "If this is the release commit, move those entries into the version's notes in a "
                f"PR first. If it's a later commit, {later}"
                if tag_matches
                else f"{later[0].upper()}{later[1:]}"
            )
        )
    return None


def server_json_problem(root: Path, version: str) -> str | None:
    """Say why `server.json` doesn't advertise `version`, or None. A backport branch gets no CI,
    so nothing else would check it there."""
    try:
        server = json.loads((root / "server.json").read_text(encoding="utf-8"))
        found = [server["version"], *(package["version"] for package in server["packages"])]
        if len(found) < 2:
            return "server.json lists no packages, so it has no package version to check"
    except (OSError, ValueError, KeyError, TypeError) as error:
        return f"couldn't read server.json's versions: {type(error).__name__}: {error}"
    if all(value == version for value in found):
        return None
    return (
        f"server.json says {found}, not {version!r}: run scripts/sync-server-json.py in a PR, "
        f"and {_TAG_THE_MERGE} once it merges"
    )


def branch_problem(root: Path, version: str) -> str | None:
    """Say why HEAD isn't known to be on a branch `version` can ship from, or None. Unsure counts
    as a problem. A backport branch counts only for a patch: a `.0` has no released line to patch.

    Exact ref names: a tag named `origin/main` can't stand in for the branch, and git's patterns
    also match below a name, which `release/0.11.x/feature` mustn't.
    """
    parts = version.split(".")
    backport = ".".join(parts[:2]) + ".x" if len(parts) > 2 and parts[2] != "0" else None
    allowed = {"refs/remotes/origin/main"}
    if backport:
        allowed.add(f"refs/remotes/origin/release/{backport}")
    try:
        result = subprocess.run(
            [
                "git",
                "for-each-ref",
                "--contains=HEAD",
                "--format=%(refname)",
                *sorted(allowed),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        return f"couldn't run git to check the tagged commit's branch: {error}"
    if result.returncode != 0:
        return f"couldn't check the tagged commit's branch: {result.stderr.strip()}"
    if allowed & set(result.stdout.split()):
        return None
    where = f"origin/main or release/{backport}" if backport else "origin/main"
    other = (
        f" For a backport, push this commit to origin/release/{backport}."
        if backport
        else f" A {version} can't ship from a backport branch: those patch a released line."
    )
    return (
        f"the tagged commit isn't known to be on {where}. PRs are squash-merged, so a PR branch's "
        "own commit never lands there: if the PR hasn't merged, merge it first, then tag its "
        f"squash commit on origin/main.{other}"
    )


def problems(tag: str, root: Path) -> list[str]:
    """Return every reason not to release `tag` from the checkout at `root`."""
    try:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        version = pyproject["project"]["version"]
        if not isinstance(version, str):
            raise TypeError(f"[project].version is {version!r}, not a string")
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
        return [f"couldn't read the version and CHANGELOG: {type(error).__name__}: {error}"]
    found = (
        tag_problem(tag, version),
        server_json_problem(root, version),
        changelog_problem(changelog, version),
        unreleased_problem(changelog, version, tag_matches=tag == f"v{version}"),
        branch_problem(root, version),
    )
    return [problem for problem in found if problem]


def _annotation(message: str) -> str:
    """`message` as a GitHub Actions error, with the escaping its command format needs."""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error::{escaped}"


def main(argv: list[str], root: Path = Path()) -> int:
    if len(argv) != 1:
        print("usage: check_release.py <tag>", file=sys.stderr)
        return 2
    found = problems(argv[0], root)
    for problem in found:
        print(_annotation(problem))
    if found:
        print(
            "This run published nothing. If the tag is new: fix this, delete it, and tag the right "
            "commit. If it was already released, don't delete it."
        )
        return 1
    print(f"{argv[0]} passes the release checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
