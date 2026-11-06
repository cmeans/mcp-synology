#!/usr/bin/env python3
"""Refuse a release tag that can't be right, before anything publishes (#457).

`publish.yml`'s `gate` job runs this first. A PyPI version can't be reused, so a tag on the wrong
commit or with the wrong version is costly to undo. It checks that:

- the tag is `v` plus `pyproject.toml`'s version;
- `CHANGELOG.md` has a section for that version, which becomes the release notes and carries a
  security release's `**Security release:**` line, the one running servers announce;
- the tagged commit (HEAD) is on `origin/main` or a backport branch, `release/<major>.<minor>.x`
  (as v0.5.3 was, shipping a fix without main's unreleased work). Not `release/0.11.0`: that's
  how this repo names a release PR's branch, and a tag there would be on an unmerged commit.

Standard library only. It needs a checkout with those branches fetched.

Usage:
    python scripts/check_release.py v0.11.0
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path


def tag_problem(tag: str, version: str) -> str | None:
    """Say why `tag` doesn't name `version`, or None."""
    if tag == f"v{version}":
        return None
    return (
        f"tag {tag!r} doesn't match pyproject.toml's version {version!r}: tag 'v{version}', or "
        "bump the version first"
    )


def changelog_problem(changelog: str, version: str) -> str | None:
    """Say why `changelog` has no notes for `version`, or None.

    The heading matches what `publish.yml`'s `github-release` job extracts: `## <version>`, then a
    space or `(`.
    """
    heading = re.search(rf"^## {re.escape(version)}(?: |\().*$", changelog, re.MULTILINE)
    if heading is None:
        return (
            f"CHANGELOG.md has no '## {version} (<date>)' section: rename '## Unreleased' as the "
            "release steps say"
        )
    following = changelog[heading.end() :]
    next_heading = re.search(r"^## ", following, re.MULTILINE)
    section = following[: next_heading.start() if next_heading else None]
    # Subheadings alone (`### Added`) aren't notes.
    if not any(line.strip() and not re.match(r"#{1,6} ", line) for line in section.splitlines()):
        return f"CHANGELOG.md's section for {version} is empty"
    return None


def branch_problem(root: Path) -> str | None:
    """Say why HEAD isn't known to be on a release branch, or None. Unsure counts as a problem.

    Full ref names, so a tag named `origin/main` can't stand in for the branch.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "for-each-ref",
                "--contains=HEAD",
                "--format=%(refname)",
                "refs/remotes/origin/main",
                "refs/remotes/origin/release/*.*.x",
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
    if result.stdout.strip():
        return None
    return (
        "the tagged commit isn't known to be on origin/main or a release/<major>.<minor>.x "
        "branch: tag a commit that merged to one"
    )


def problems(tag: str, root: Path) -> list[str]:
    """Return every reason not to release `tag` from the checkout at `root`."""
    try:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        version = pyproject["project"]["version"]
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError) as error:
        return [f"couldn't read the version and CHANGELOG: {type(error).__name__}: {error}"]
    found = (
        tag_problem(tag, version),
        changelog_problem(changelog, version),
        branch_problem(root),
    )
    return [problem for problem in found if problem]


def main(argv: list[str], root: Path = Path()) -> int:
    if len(argv) != 1:
        print("usage: check_release.py <tag>", file=sys.stderr)
        return 2
    found = problems(argv[0], root)
    for problem in found:
        print(f"error: {problem}", file=sys.stderr)
    if found:
        print(
            "This run published nothing. If the tag is new: fix this, delete it, and tag again.",
            file=sys.stderr,
        )
        return 1
    print(f"{argv[0]} can be released.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
