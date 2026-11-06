#!/usr/bin/env python3
"""Refuse a release tag that can't be right, before anything publishes (#457).

`publish.yml`'s `gate` job runs this first. A PyPI version can't be reused, so a tag on the wrong
commit or with the wrong version is costly to undo. It checks that:

- the tag is `v` plus `pyproject.toml`'s version;
- `CHANGELOG.md` has a section for that version, which becomes the release notes and carries a
  security release's `**Security release:**` line, the one running servers announce;
- the tagged commit (HEAD) is on `origin/main`, so it went through a PR, CI and QA.

Standard library only. It needs a checkout with `origin/main` fetched.

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
    return f"tag {tag!r} doesn't match pyproject.toml's version {version!r}: expected 'v{version}'"


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
    if not following[: next_heading.start() if next_heading else None].strip():
        return f"CHANGELOG.md's section for {version} is empty"
    return None


def main_branch_problem(root: Path) -> str | None:
    """Say why HEAD isn't known to be on origin/main, or None. Unsure counts as a problem."""
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        return f"couldn't run git to check the tagged commit is on main: {error}"
    if result.returncode == 0:
        return None
    if result.returncode == 1:
        return "the tagged commit isn't on origin/main: tag a commit that merged to main"
    return f"couldn't check the tagged commit is on main: {result.stderr.strip()}"


def problems(tag: str, root: Path) -> list[str]:
    """Return every reason not to release `tag` from the checkout at `root`."""
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    found = (
        tag_problem(tag, version),
        changelog_problem(changelog, version),
        main_branch_problem(root),
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
        print("Nothing was published. Delete the tag, fix this, and tag again.", file=sys.stderr)
        return 1
    print(f"{argv[0]} can be released.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
