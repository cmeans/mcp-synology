#!/usr/bin/env python3
"""Refuse a release tag that can't be right, before anything publishes (#457).

`publish.yml`'s `gate` job runs this first. A PyPI version can't be reused, so a tag on the wrong
commit or with the wrong version is costly to undo. It checks that:

- the tag is `v` plus `pyproject.toml`'s version, and `server.json` says the same;
- `CHANGELOG.md` has a section for that version with notes, which become the release notes and
  carry a security release's `**Security release:**` line, the one running servers announce;
- no section above that one has entries, and no Unreleased section anywhere does: a commit after
  the release commit would ship those changes with no notes and no live test. A later commit that
  added no entry still passes;
- `changelog.d/` holds no fragments (#731): the release PR assembles them into the version's
  notes, so one left there is a change the notes don't mention;
- the tagged commit (HEAD) is on `origin/main` or, for a patch on a released line, this version's
  backport branch, `release/<major>.<minor>.x` (as v0.5.3 was, shipping a fix without main's
  unreleased work), cut at an earlier final tag of that line and not from `main`, and containing
  that line's earlier releases. Not for a `.0`,
  which has no released line to patch: `release/0.11.x` or `release/0.11.0` (how this repo names a
  release PR's branch) would be an unmerged commit.

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
        f"merged, merge it and {_TAG_THE_MERGE}. If this is the commit to release and its version "
        "wasn't bumped, bump it first, in a PR (for a backport, on its branch)."
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
    # Before the section's own pattern, which a trailing space would satisfy.
    if re.search(rf"^## {escaped}\s*$", changelog, re.MULTILINE):
        return f"CHANGELOG.md's '## {version}' heading has no date: '## {version} (<date>)'."
    section = _section(changelog, rf"{escaped}(?: |\()")
    if section is None:
        return f"CHANGELOG.md has no '## {version} (<date>)' section: see {_RELEASE_STEPS}."
    return None if _has_notes(section) else f"CHANGELOG.md's section for {version} has no notes."


def unreleased_problem(changelog: str, version: str, *, tag_matches: bool = True) -> str | None:
    """Say why this commit looks later than the release commit, or None: a release leaves nothing
    above its own section (an empty `## Unreleased` or none, as on a backport branch), so entries
    there are changes that aren't in this version's notes. The likeliest route is retagging after
    a refusal, when `origin/main` has moved on. Any `## ` section above it counts, whatever it's
    called, and an Unreleased section anywhere. With no section for this version
    (`changelog_problem` says so), only the first does, unless it's a shipped version's: nothing
    there is unreleased.

    When the tag isn't this version, `version` has already shipped, so its notes aren't the place
    for those entries, and the advice to move them stays out."""
    headings = list(re.finditer(r"^## .*$", changelog, re.MULTILINE))
    own = re.compile(rf"## {re.escape(version)}(?: |\(|\s*$)")
    first_is_a_version = bool(headings) and re.match(r"## \[?\d", headings[0].group()) is not None
    default = 0 if first_is_a_version else 1
    stop = next((i for i, h in enumerate(headings) if own.match(h.group())), default)
    for index, heading in enumerate(headings):
        # Above it, whatever it's called; below it, only one that says it's unreleased.
        if index >= stop and not re.match(r"(?i)##\s+\[?unreleased\b", heading.group()):
            continue
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
                f"PR first. If it's a later commit, {later}."
                if tag_matches
                else f"{later[0].upper()}{later[1:]}."
            )
        )
    return None


# Files in changelog.d that aren't fragments (as in scripts/changelog_fragments.py).
_NOT_FRAGMENTS = frozenset({"README.md", "template.md", ".DS_Store"})


def _named(names: list[str], limit: int = 10) -> str:
    shown = ", ".join(names[:limit])
    return shown if len(names) <= limit else f"{shown} and {len(names) - limit} more"


def fragments_problem(root: Path) -> str | None:
    """Say which CHANGELOG fragments are still in `changelog.d/`, or None: the release PR's
    `towncrier build` assembles them into the version's notes and deletes them (#731), so one
    left is a change that isn't in this version's notes."""
    directory = root / "changelog.d"
    if not directory.is_dir():
        return None
    left = sorted(p.name for p in directory.iterdir() if p.name not in _NOT_FRAGMENTS)
    if not left:
        return None
    return (
        f"changelog.d/ still holds {len(left)} fragment(s) ({_named(left)}), changes that "
        f"aren't in this version's notes: assemble them in the release PR (see {_RELEASE_STEPS}). "
        "If this is the release PR's own squash commit, they merged after its build: move them "
        "into the version's notes in a PR that deletes them (it takes the 'no changelog' label), "
        "and tag that PR's squash commit. If "
        "it's a later commit, tag the release PR's squash commit."
    )


def server_json_problem(root: Path, version: str) -> str | None:
    """Say why `server.json` doesn't advertise `version`, or None. A backport branch gets no CI,
    so nothing else would check it there."""
    try:
        server = json.loads((root / "server.json").read_text(encoding="utf-8"))
        found = [server["version"], *(package["version"] for package in server["packages"])]
        if len(found) < 2:
            return "server.json lists no packages, so it has no package version to check."
    except (OSError, ValueError, KeyError, TypeError) as error:
        return f"couldn't read server.json's versions: {type(error).__name__}: {error}"
    if all(value == version for value in found):
        return None
    return (
        f"server.json says {found}, not {version!r}: run scripts/sync-server-json.py in a PR, "
        f"and {_TAG_THE_MERGE} once it merges."
    )


_LINE_TAG = re.compile(r"v[0-9]+\.[0-9]+\.(0|[1-9][0-9]*)")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # A ref name needn't be valid UTF-8, and a decoding error would be a traceback, not a refusal.
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def branch_problem(root: Path, version: str, *, tag_matches: bool = True) -> str | None:
    """Say why HEAD isn't known to be on a branch `version` can ship from, or None. Unsure counts
    as a problem. A backport branch counts only for a patch: a `.0` has no released line to patch.
    And it must have been cut from that line, not from `main`, which would ship all of `main`'s
    unreleased work, or from an older line, which would miss this one's releases: where it leaves
    `main` must be at an earlier final tag of the line, and it must contain the line's earlier
    releases, or a patch would ship without their fixes.

    Exact ref names: a tag named `origin/main` can't stand in for the branch, and git's patterns
    also match below a name, which `release/0.11.x/feature` mustn't.

    When the tag isn't this version, the version here says nothing about the branch it should be
    on, so the advice about a backport branch stays out.
    """
    main = "refs/remotes/origin/main"
    # A final patch only: a pre-release, like a `.0`, ships from main.
    is_patch = re.fullmatch(r"\d+\.\d+\.0*[1-9]\d*", version) is not None
    line = ".".join(version.split(".")[:2])
    # A tag that isn't this version may be a backport with its bump forgotten: the tag's refusal
    # says that, and this has nothing to add.
    backport = f"{line}.x" if is_patch or not tag_matches else None
    allowed = {main}
    if backport:
        allowed.add(f"refs/remotes/origin/release/{backport}")
    try:
        has_main = _git(root, "rev-parse", "--verify", "--quiet", main)
        if has_main.returncode == 1:
            return (
                "this checkout has no origin/main, so the tagged commit's branch can't be checked."
            )
        if has_main.returncode != 0:
            return f"couldn't check the tagged commit's branch: {has_main.stderr.strip()}"
        result = _git(
            root, "for-each-ref", "--contains=HEAD", "--format=%(refname)", *sorted(allowed)
        )
        if result.returncode != 0:
            return f"couldn't check the tagged commit's branch: {result.stderr.strip()}"
        found = allowed & set(result.stdout.split())
        if main in found or (found and not tag_matches):
            return None
        if found:
            base = _git(root, "merge-base", "HEAD", main)
            # At the cut point, not merely after it: a branch cut before the line began would ship
            # without its first release. Named like a final release of this line, and earlier.
            tags = _git(
                root,
                "tag",
                "--no-column",
                "--list",
                f"v{line}.*",
                "--points-at",
                base.stdout.strip(),
            )
            if base.returncode != 0 or tags.returncode != 0:
                return (
                    f"couldn't check where release/{backport} was cut from: it may share no "
                    f"history with origin/main. Cut it from the last v{line}.* tag."
                )
            earlier = (
                int(found_tag.group(1))
                for found_tag in map(_LINE_TAG.fullmatch, tags.stdout.split("\n"))
                if found_tag
            )
            this_patch = int(version.split(".")[2])
            if any(patch < this_patch for patch in earlier):
                # And on top of the line's earlier releases, or it would ship without their fixes.
                missed = _git(
                    root, "tag", "--no-column", "--list", f"v{line}.*", "--no-merged", "HEAD"
                )
                if missed.returncode != 0:
                    return f"couldn't check what release/{backport} contains."
                unmerged = map(_LINE_TAG.fullmatch, missed.stdout.split("\n"))
                skipped = [
                    found.group(0)
                    for found in unmerged
                    if found and int(found.group(1)) < this_patch
                ]
                if not skipped:
                    return None
                return (
                    f"release/{backport} doesn't contain {', '.join(skipped)}, so this patch would "
                    f"ship without them. Cut it from the last v{line}.* tag."
                )
            cut_point = base.stdout.strip()[:7]
            return (
                f"release/{backport} wasn't cut at a final v{line}.* tag earlier than this "
                f"version: it leaves origin/main at {cut_point}. Cut it from the last "
                f"v{line}.* tag."
            )
    except (OSError, ValueError) as error:  # ValueError: a NUL in an argument
        return f"couldn't run git to check the tagged commit's branch: {error}"
    where = f"origin/main or release/{backport}" if backport and tag_matches else "origin/main"
    if not tag_matches:
        other = ""
    elif backport:
        other = f" For a backport, push this commit to origin/release/{backport}."
    else:
        other = f" {version} can't ship from a backport branch: those patch a released line."
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
        fragments_problem(root),
        branch_problem(root, version, tag_matches=tag == f"v{version}"),
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
