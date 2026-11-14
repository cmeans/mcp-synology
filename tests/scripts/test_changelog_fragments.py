"""Tests for scripts/changelog_fragments.py (#731)."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "changelog_fragments.py"
_SPEC = importlib.util.spec_from_file_location("changelog_fragments", _PATH)
assert _SPEC and _SPEC.loader
script = importlib.util.module_from_spec(_SPEC)
sys.modules["changelog_fragments"] = script
_SPEC.loader.exec_module(script)

ROOT = Path(__file__).resolve().parents[2]


def _dir(tmp_path: Path, files: dict[str, str]) -> Path:
    fragments = tmp_path / "changelog.d"
    fragments.mkdir(parents=True)
    for name, text in {"README.md": "how\n", "template.md": "{{ x }}\n", **files}.items():
        (fragments / name).write_text(text, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n<!-- towncrier release notes start -->\n",
        encoding="utf-8",
    )
    _pyproject(tmp_path, "1.0.0")
    return tmp_path


_START = "<!-- towncrier release notes start -->"


def _pyproject(root: Path, version: str) -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nversion = "{version}"\n\n[tool.towncrier]\nstart_string = "{_START}\\n"\n',
        encoding="utf-8",
    )


def _release(root: Path, version: str) -> None:
    """What a release PR's build leaves: the version bumped, its section under the marker."""
    _pyproject(root, version)
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## Unreleased\n\n{_START}\n\n## {version} (2026-01-01)\n\n- x\n",
        encoding="utf-8",
    )


def test_the_repos_own_fragments_pass() -> None:
    assert script.fragment_problems(ROOT) == []


@pytest.mark.parametrize(
    "name",
    [
        "731.added.md",
        "731.changed.md",
        "731.fixed.md",
        "731.fixed.1.md",
        "731.fixed.12.md",
    ],
)
def test_good_entry_names_pass(tmp_path: Path, name: str) -> None:
    root = _dir(tmp_path, {name: "- **Title** (#731): why — closes #1\n"})
    assert script.fragment_problems(root) == []


def test_a_release_fragment_takes_free_text(tmp_path: Path) -> None:
    text = "**Security release:** fixes X.\n\nUpgrading:\n\n- step one\n- step two\n"
    assert script.fragment_problems(_dir(tmp_path, {"+security.release.md": text})) == []


@pytest.mark.parametrize(
    "name",
    [
        "731.fix.md",
        "731.fixed.txt",
        "731.fixed",
        "pr731.fixed.md",
        "0731.fixed.md",
        "731.fixed.0.md",
        "+Security.release.md",
        "+notes.fixed.md",
        "notes.md",
        "731.Fixed.md",
    ],
)
def test_a_misnamed_file_is_refused(tmp_path: Path, name: str) -> None:
    """towncrier build would leave it out of the notes, silently."""
    found = script.fragment_problems(_dir(tmp_path, {name: "- **x** (#731): y\n"}))
    assert len(found) == 1 and "isn't a fragment name" in found[0], found


def test_a_folder_is_refused(tmp_path: Path) -> None:
    root = _dir(tmp_path, {})
    (root / "changelog.d" / "731.fixed.md").mkdir()
    assert "isn't a fragment name" in script.fragment_problems(root)[0]


@pytest.mark.parametrize(
    ("text", "says"),
    [
        ("", "empty"),
        ("\n  \n", "empty"),
        ("**Title** (#731): no bullet\n", "starts with `- `"),
        ("### Fixed\n\n- **x** (#731): y\n", "heading"),
        ("- **x** (#731): y\n- **z** (#731): w\n", "more than one entry"),
        ("- **x** (#731): y\n* **z** (#731): w\n", "more than one entry"),
        ("- **x** (#731): y\n+ **z** (#731): w\n", "more than one entry"),
        ("- **x** (#732): the wrong PR\n", "isn't #731"),
        ("- **x**: no number at all\n", "isn't #731"),
    ],
)
def test_bad_content_is_refused(tmp_path: Path, text: str, says: str) -> None:
    found = script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text}))
    assert len(found) == 1 and says in found[0], found


def test_indented_lines_are_neither_headings_nor_entries(tmp_path: Path) -> None:
    text = (
        "- **x** (#731): y\n\n      # not a heading: indented code inside the entry\n"
        "        ## nor this\n  - not an entry\n"
    )
    assert script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text})) == []


@pytest.mark.parametrize(
    "line",
    [
        "##\tTab heading",
        "##",
        "#",
        "   ## Three spaces",
        " # One space",
        "###### Six",
        # QA r2 on #732: inside the entry (content at column 2), GitHub renders these too.
        "    ## Four spaces",
        "     ## Five spaces",
        "\t## Tab",
        "\n    ## Four after a blank line",
        "  more\n  ===",
        "  more\n  ---",
        "  - ## In a nested item",
        "  > ## In a block quote",
    ],
)
def test_every_commonmark_heading_is_refused(tmp_path: Path, line: str) -> None:
    """Pre-QA r6 and QA r2 on #732: GitHub renders these as headings inside the release notes."""
    found = script.fragment_problems(
        _dir(tmp_path, {"731.fixed.md": f"- **x** (#731): y\n{line}\n"})
    )
    assert len(found) == 1 and "reads as a heading" in found[0], found


def test_a_heading_refusal_names_its_line(tmp_path: Path) -> None:
    """QA r2 on #732: a tab or a bare # doesn't look like a heading in an editor."""
    found = script.fragment_problems(
        _dir(tmp_path, {"731.fixed.md": "- **x** (#731): y\n  more\n##\tTab\n"})
    )
    assert found == [
        "changelog.d/731.fixed.md: line 3, '##\\tTab', reads as a heading; the notes add the "
        "category headings (see changelog.d/README.md)."
    ], found


def test_a_heading_as_the_entry_itself_is_refused(tmp_path: Path) -> None:
    found = script.fragment_problems(_dir(tmp_path, {"731.fixed.md": "- ## x (#731): y\n"}))
    assert len(found) == 1 and "line 1" in found[0], found


@pytest.mark.parametrize(
    "line", ["####### Seven", "#hashtag", "      ## Six spaces is code", "\t  ## Tab and two"]
)
def test_what_commonmark_doesnt_read_as_a_heading_passes(tmp_path: Path, line: str) -> None:
    found = script.fragment_problems(
        _dir(tmp_path, {"731.fixed.md": f"- **x** (#731): y\n{line}\n"})
    )
    assert found == [], found


def test_a_ds_store_is_not_a_fragment(tmp_path: Path) -> None:
    """Pre-QA r6 on #732: macOS Finder writes one; towncrier refused it and so did the check."""
    root = _dir(tmp_path, {})
    (root / "changelog.d" / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    assert script.fragment_problems(root) == []
    assert script._fragments_left(root) == []


def test_git_output_is_read_as_utf8_whatever_the_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-QA r6 on #732: decoding by the locale crashed on CHANGELOG.md's non-ASCII bytes
    under a C or cp1252 locale."""
    seen: list[dict[str, object]] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(script.subprocess, "run", fake_run)
    script._git_show(Path("."), "HEAD", "CHANGELOG.md")
    script.branch_problem(Path("."), "main", 731, [])
    assert len(seen) == 2
    for kwargs in seen:
        assert kwargs.get("encoding") == "utf-8" and kwargs.get("errors") == "replace", kwargs


@pytest.mark.parametrize(
    "text",
    [
        # publish.yml's awk stops at any line starting "## ": a fence doesn't hide it.
        "- **x** (#731): y\n\n  ```\n## 0.0.2 (fake)\n  ```\n",
        # A line with inline backticks used to flip the fence state for the lines after it.
        "- **x** (#731): y ```a``` b\n## 0.0.1 (fake)\n",
    ],
)
def test_a_column_zero_heading_in_a_fence_is_refused(tmp_path: Path, text: str) -> None:
    """Pre-QA r5 on #731: the release body was cut short at the fake heading."""
    found = script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text}))
    assert len(found) == 1 and "heading" in found[0], found


def test_a_fragment_that_is_a_link_is_refused(tmp_path: Path) -> None:
    """Pre-QA r5 on #731: towncrier copies the link's target into the notes."""
    root = _dir(tmp_path, {"731.fixed.md": "- **x** (#731): y\n"})
    (tmp_path / "secret").write_text("token\n", encoding="utf-8")
    (root / "changelog.d" / "+notes.release.md").symlink_to(tmp_path / "secret")
    found = script.fragment_problems(root)
    assert len(found) == 1 and "+notes.release.md is a symbolic link" in found[0], found


@pytest.mark.parametrize(
    "text",
    [
        "- **x** (#731): y <!-- towncrier release notes start -->\n",
        "- **x** (#731): y\n\n<!-- towncrier release notes start -->\n",
        "- **x** (#731): y <!--\n",
    ],
)
def test_a_comment_is_refused(tmp_path: Path, text: str) -> None:
    for name in ("731.fixed.md", "+x.release.md"):
        found = script.fragment_problems(_dir(tmp_path / name, {name: text}))
        assert len(found) == 1 and "`<!--`" in found[0], (name, found)


def test_only_a_release_fragment_says_security_release(tmp_path: Path) -> None:
    """Pre-QA r5 on #731: the update check announces any release whose notes hold it."""
    from mcp_synology.cli import version

    assert script.SECURITY_MARKER == version._SECURITY_MARKER
    entry = f"- **Tidy logs** (#731): quieter. {script.SECURITY_MARKER} upgrade now.\n"
    found = script.fragment_problems(_dir(tmp_path / "a", {"731.fixed.md": entry}))
    assert len(found) == 1 and "only a release's own text" in found[0], found
    release = f"{script.SECURITY_MARKER} fixes a path check; every user should upgrade.\n"
    assert script.fragment_problems(_dir(tmp_path / "b", {"+security.release.md": release})) == []


def test_a_release_fragment_with_a_heading_is_refused(tmp_path: Path) -> None:
    found = script.fragment_problems(_dir(tmp_path, {"+x.release.md": "## 9.9.9\n"}))
    assert "heading" in found[0]


def test_a_missing_directory_is_refused(tmp_path: Path) -> None:
    assert "missing" in script.fragment_problems(tmp_path)[0]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo with changelog.d on main and a feature branch checked out."""
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    root = _dir(tmp_path, {"700.fixed.md": "- **old** (#700): y\n"})
    _git(root, "init", "-q", "-b", "main")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    return root


def _commit(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "change")


def test_a_branch_that_adds_a_fragment_passes(branch: Path) -> None:
    (branch / "changelog.d" / "731.fixed.md").write_text("- **x** (#731): y\n", encoding="utf-8")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is None


def test_a_branch_that_removes_fragments_passes(branch: Path) -> None:
    """A release PR's towncrier build deletes them, bumping the version and adding its section."""
    (branch / "changelog.d" / "700.fixed.md").unlink()
    _release(branch, "1.1.0")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is None


def test_removing_the_last_fragment_without_a_release_is_refused(branch: Path) -> None:
    """Pre-QA r3 on #731: "none left" alone let a PR delete another PR's last entry."""
    (branch / "changelog.d" / "700.fixed.md").unlink()
    _commit(branch)
    problem = script.branch_problem(branch, "main", 731, [])
    assert problem is not None and "only a release PR does" in problem and "restore them" in problem
    assert "'no changelog' label" in problem


def test_a_release_that_leaves_fragments_is_told_which(branch: Path) -> None:
    (branch / "changelog.d" / "701.fixed.md").write_text("- **b** (#701): y\n", encoding="utf-8")
    _commit(branch)
    _git(branch, "branch", "-f", "base")
    (branch / "changelog.d" / "700.fixed.md").unlink()
    _release(branch, "1.1.0")
    _commit(branch)
    problem = script.branch_problem(branch, "base", 731, [])
    assert problem is not None and "release PR (1.1.0) leaves 1" in problem, problem
    # towncrier won't rebuild a version already in CHANGELOG.md (pre-QA r4 on #731): by hand.
    assert "701.fixed.md" in problem and "by hand" in problem
    assert "run the build" not in problem


def test_a_branch_that_only_edits_the_changelog_is_refused(branch: Path) -> None:
    """towncrier check passes this; the entry belongs in a fragment."""
    (branch / "CHANGELOG.md").write_text("- **x** (#731): y\n", encoding="utf-8")
    _commit(branch)
    problem = script.branch_problem(branch, "main", 731, [])
    assert problem is not None and "adds no CHANGELOG fragment" in problem


def test_the_readme_or_template_alone_isnt_a_fragment(branch: Path) -> None:
    (branch / "changelog.d" / "README.md").write_text("more\n", encoding="utf-8")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is not None


def test_the_skip_label_passes(branch: Path) -> None:
    (branch / "x.txt").write_text("x\n", encoding="utf-8")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, ["bug", "no changelog"]) is None


def test_an_unknown_base_is_refused_not_passed(branch: Path) -> None:
    problem = script.branch_problem(branch, "no-such-ref", 731, [])
    assert problem is not None and "couldn't list" in problem


def test_main_reports_every_problem(branch: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (branch / "changelog.d" / "bad.md").write_text("x\n", encoding="utf-8")
    _commit(branch)
    assert script.main(["check", "--base", "main", "--pr", "731", "--labels", ""], branch) == 1
    out = capsys.readouterr().out
    # bad.md is misnamed, and isn't the PR's own fragment either.
    assert out.count("::error::") == 2
    ok = branch.parent / "ok"
    ok.mkdir()
    assert script.main(["check"], _dir(ok, {})) == 0


def test_another_prs_fragment_doesnt_count(branch: Path) -> None:
    """Pre-QA r1 on #731: rewriting 700.fixed.md would lose #700's entry."""
    (branch / "changelog.d" / "700.fixed.md").write_text("- **new** (#700): y\n", encoding="utf-8")
    _commit(branch)
    problem = script.branch_problem(branch, "main", 731, [])
    assert problem is not None and "of its own" in problem


def test_a_fragment_named_for_an_issue_doesnt_count(branch: Path) -> None:
    """The notes would cite #730, the issue, not the PR."""
    (branch / "changelog.d" / "730.fixed.md").write_text("- **x** (#730): y\n", encoding="utf-8")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is not None


def test_a_second_entry_of_its_own_counts(branch: Path) -> None:
    (branch / "changelog.d" / "731.added.1.md").write_text("- **x** (#731): y\n", encoding="utf-8")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is None


def test_a_release_fragment_isnt_a_prs_own(branch: Path) -> None:
    """Pre-QA r2 on #731: counting +<name>.release.md let any PR pass by adding one; a release
    PR's build removes it with the rest, so it passes by removal."""
    (branch / "changelog.d" / "+notes.release.md").write_text(
        "- **my feature** (#12): x\n", encoding="utf-8"
    )
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is not None


def test_removing_some_fragments_isnt_a_release(branch: Path) -> None:
    """Without a version bump, removing fragments is refused and the advice is to restore them,
    not to run the build (which would write a bogus release)."""
    (branch / "changelog.d" / "701.fixed.md").write_text("- **b** (#701): y\n", encoding="utf-8")
    _commit(branch)
    _git(branch, "branch", "-f", "base")
    (branch / "changelog.d" / "700.fixed.md").unlink()
    _commit(branch)
    problem = script.branch_problem(branch, "base", 731, [])
    assert problem is not None and "restore them" in problem, problem
    assert "run the build" not in problem


def test_another_prs_fragment_renamed_to_ours_is_refused(branch: Path) -> None:
    """The renamed file still cites #700, so the content check refuses it."""
    (branch / "changelog.d" / "700.fixed.md").rename(branch / "changelog.d" / "731.fixed.md")
    _commit(branch)
    found = script.fragment_problems(branch)
    assert any("isn't #731" in problem for problem in found), found


def test_base_needs_pr(branch: Path) -> None:
    with pytest.raises(SystemExit):
        script.main(["check", "--base", "main"], branch)


def test_the_three_lists_of_non_fragments_agree() -> None:
    """Pre-QA r1 on #731: towncrier's ignore list and the two scripts' copies drift apart only
    at release time, when the gate would refuse the tag."""
    import tomllib

    _gate_spec = importlib.util.spec_from_file_location(
        "check_release_for_lists", ROOT / "scripts" / "check_release.py"
    )
    assert _gate_spec and _gate_spec.loader
    gate = importlib.util.module_from_spec(_gate_spec)
    _gate_spec.loader.exec_module(gate)
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ignored = frozenset(pyproject["tool"]["towncrier"]["ignore"])
    assert ignored == script.NOT_FRAGMENTS == gate._NOT_FRAGMENTS


def test_moving_its_own_entry_to_another_category_counts(branch: Path) -> None:
    """--no-renames: git would call 731.added.md -> 731.fixed.md a rename, which matches
    nothing; as a removal plus an addition, the addition is the PR's own fragment."""
    _git(branch, "branch", "-f", "base")
    (branch / "changelog.d" / "731.added.md").write_text("- **x** (#731): y\n", encoding="utf-8")
    _commit(branch)
    _git(branch, "branch", "-f", "base")
    (branch / "changelog.d" / "731.added.md").rename(branch / "changelog.d" / "731.fixed.md")
    _commit(branch)
    assert script.branch_problem(branch, "base", 731, []) is None


_HEAD = "# Changelog\n\n## Unreleased\n\n<!-- towncrier release notes start -->\n\n"


@pytest.mark.parametrize(
    ("text", "says"),
    [
        (_HEAD + "## 1.0.0 (2026-01-01)\n\n- x\n", None),
        (
            "# Changelog\n\n## Unreleased\n\n## 1.0.0 (2026-01-01)\n",
            "0 of towncrier's start marker",
        ),
        (_HEAD + _HEAD, "2 of towncrier's start marker"),
        (
            _HEAD.replace("\n\n<!--", "\n\n### Fixed\n\n- **x** (#1): y\n\n<!--")
            + "## 1.0.0 (x)\n",
            "besides towncrier's marker",
        ),
        (_HEAD + "- **stray** (#1): y\n\n## 1.0.0 (x)\n", "besides towncrier's marker"),
        (
            "# Changelog\n\n## Unreleased\n\n"
            "<!-- towncrier release notes start -->  \n\n## 1.0.0 (x)\n",
            "0 of towncrier's start marker",
        ),
        (
            "# Changelog\n\n"
            "<!-- towncrier release notes start -->\n\n## Unreleased\n\n## 1.0.0 (x)\n",
            "isn't under '## Unreleased'",
        ),
        (
            "# Changelog\n\n## Unreleased\n\n## 1.0.0 (x)\n\n### Added\n\n"
            "<!-- towncrier release notes start -->\n- y\n",
            "isn't under '## Unreleased'",
        ),
        (
            _HEAD + "## 1.0.0 (x)\n\n## Unreleased\n\n- **stray** (#1): y\n",
            "2 '## Unreleased' headings",
        ),
        (
            "# Changelog\n\n<!-- towncrier release notes start -->\n\n## 1.0.0 (x)\n",
            "0 '## Unreleased'",
        ),
    ],
)
def test_the_changelogs_layout_is_checked(tmp_path: Path, text: str, says: str | None) -> None:
    """Pre-QA r2 on #731: without the marker the build writes above '# Changelog', and an entry
    under Unreleased would reach the tag gate only after the release PR merged."""
    (tmp_path / "CHANGELOG.md").write_text(text, encoding="utf-8")
    _pyproject(tmp_path, "1.0.0")
    found = script.changelog_problems(tmp_path)
    if says is None:
        assert found == []
    else:
        assert any(says in problem for problem in found), found


def test_the_repos_own_changelog_is_laid_out_right() -> None:
    assert script.changelog_problems(ROOT) == []


def test_check_runs_the_layout_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _dir(tmp_path, {})
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n- **x** (#1): y\n", encoding="utf-8"
    )
    assert script.main(["check"], root) == 1
    assert "start marker" in capsys.readouterr().out


def test_the_towncrier_pins_agree() -> None:
    """Pre-QA r3 on #731: CI's draft and the release build must run the same towncrier."""
    import re as _re

    pins = set()
    for path in (
        ROOT / ".github" / "workflows" / "ci.yml",
        ROOT / "CLAUDE.md",
        ROOT / "pyproject.toml",
    ):
        pins |= set(
            _re.findall(r"towncrier==([0-9][0-9.]*[0-9])", path.read_text(encoding="utf-8"))
        )
    assert len(pins) == 1, pins
    for path in (ROOT / ".github" / "workflows" / "ci.yml", ROOT / "CLAUDE.md"):
        assert "towncrier==" in path.read_text(encoding="utf-8"), path


def test_fragment_only_prs_skip_vdsm() -> None:
    """Pre-QA r3 on #731: every PR adds a fragment, so a docs-only PR must still skip vdsm."""
    vdsm = (ROOT / ".github" / "workflows" / "vdsm.yml").read_text(encoding="utf-8")
    found = re.search(r"grep -qvE '([^']+)'", vdsm)
    assert found is not None
    docs_only = re.compile(found.group(1))
    for path in ("changelog.d/731.fixed.md", "changelog.d/+security.release.md", "README.md"):
        assert docs_only.search(path), path
    for path in ("changelog.d/sub/731.fixed.md", "changelog.d/731.py", "src/x.md", "scripts/a.py"):
        assert not docs_only.search(path), path


def test_a_version_bump_without_its_section_isnt_a_release(branch: Path) -> None:
    (branch / "changelog.d" / "700.fixed.md").unlink()
    _pyproject(branch, "1.1.0")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is not None


def test_a_section_without_a_version_bump_isnt_a_release(branch: Path) -> None:
    """The version stays 1.0.0 (the base's), though the branch adds a '## 1.0.0 (' section."""
    (branch / "changelog.d" / "700.fixed.md").unlink()
    _release(branch, "1.0.0")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is not None


def test_its_own_fragment_doesnt_excuse_deleting_another(branch: Path) -> None:
    """Pre-QA r4 on #731: the own-fragment pass ran before the removal rule."""
    (branch / "changelog.d" / "700.fixed.md").unlink()
    (branch / "changelog.d" / "731.fixed.md").write_text("- **x** (#731): y\n", encoding="utf-8")
    _commit(branch)
    problem = script.branch_problem(branch, "main", 731, [])
    assert problem is not None and "700.fixed.md" in problem


def test_a_release_section_already_on_the_base_isnt_a_release(branch: Path) -> None:
    """Pre-QA r4 on #731: a step-7 follow-up (the base already holds '## 1.1.0 (') that
    removes a fragment needs the label, not the release pass."""
    _release(branch, "1.1.0")
    (branch / "changelog.d" / "701.fixed.md").write_text("- **b** (#701): y\n", encoding="utf-8")
    _commit(branch)
    _git(branch, "branch", "-f", "base")
    (branch / "changelog.d" / "701.fixed.md").unlink()
    _commit(branch)
    problem = script.branch_problem(branch, "base", 731, [])
    assert problem is not None and "'no changelog' label" in problem
    assert script.branch_problem(branch, "base", 731, ["no changelog"]) is None


def test_long_lists_are_cut_short() -> None:
    names = [f"{n}.fixed.md" for n in range(1, 13)]
    assert script._named(names).endswith("and 2 more"), script._named(names)
    assert "12.fixed.md" not in script._named(names)
    assert "3.fixed.md" in script._named(names[:3])


def test_only_ci_has_a_job_named_changelog() -> None:
    """QA r1 on #732: a required check is matched by name, and another workflow's `changelog`
    job, skipped on every PR it doesn't run for, would satisfy it."""
    # A check run is named for the job's `name:` when it has one, else for its id (pre-QA r6 on
    # #732: a `name: changelog` on another job brought the clash back unseen).
    owners = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        jobs = yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}
        for job_id, job in jobs.items():
            shown = job.get("name", job_id) if isinstance(job, dict) else job_id
            if str(shown).strip().casefold() == "changelog":
                owners.append(f"{path.name}:{job_id}")
    assert owners == ["ci.yml:changelog"], owners
