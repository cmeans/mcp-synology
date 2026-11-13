"""Tests for scripts/changelog_fragments.py (#731)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "changelog_fragments.py"
_SPEC = importlib.util.spec_from_file_location("changelog_fragments", _PATH)
assert _SPEC and _SPEC.loader
script = importlib.util.module_from_spec(_SPEC)
sys.modules["changelog_fragments"] = script
_SPEC.loader.exec_module(script)

ROOT = Path(__file__).resolve().parents[2]


def _dir(tmp_path: Path, files: dict[str, str]) -> Path:
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    for name, text in {"README.md": "how\n", "template.md": "{{ x }}\n", **files}.items():
        (fragments / name).write_text(text, encoding="utf-8")
    return tmp_path


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
        ("- **x** (#732): the wrong PR\n", "isn't #731"),
        ("- **x**: no number at all\n", "isn't #731"),
    ],
)
def test_bad_content_is_refused(tmp_path: Path, text: str, says: str) -> None:
    found = script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text}))
    assert len(found) == 1 and says in found[0], found


def test_code_fences_hide_headings_and_bullets(tmp_path: Path) -> None:
    text = "- **x** (#731): y\n\n  ```\n  # not a heading\n  - not an entry\n  ```\n"
    assert script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text})) == []


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
    assert script.branch_problem(branch, "main", []) is None


def test_a_branch_that_removes_fragments_passes(branch: Path) -> None:
    """A release PR's towncrier build deletes them."""
    (branch / "changelog.d" / "700.fixed.md").unlink()
    (branch / "CHANGELOG.md").write_text("## 1.0.0 (2026-01-01)\n", encoding="utf-8")
    _commit(branch)
    assert script.branch_problem(branch, "main", []) is None


def test_a_branch_that_only_edits_the_changelog_is_refused(branch: Path) -> None:
    """towncrier check passes this; the entry belongs in a fragment."""
    (branch / "CHANGELOG.md").write_text("- **x** (#731): y\n", encoding="utf-8")
    _commit(branch)
    problem = script.branch_problem(branch, "main", [])
    assert problem is not None and "adds no CHANGELOG fragment" in problem


def test_the_readme_or_template_alone_isnt_a_fragment(branch: Path) -> None:
    (branch / "changelog.d" / "README.md").write_text("more\n", encoding="utf-8")
    _commit(branch)
    assert script.branch_problem(branch, "main", []) is not None


def test_the_skip_label_passes(branch: Path) -> None:
    (branch / "x.txt").write_text("x\n", encoding="utf-8")
    _commit(branch)
    assert script.branch_problem(branch, "main", ["bug", "no changelog"]) is None


def test_an_unknown_base_is_refused_not_passed(branch: Path) -> None:
    problem = script.branch_problem(branch, "no-such-ref", [])
    assert problem is not None and "couldn't list" in problem


def test_main_reports_every_problem(branch: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (branch / "changelog.d" / "bad.md").write_text("x\n", encoding="utf-8")
    _commit(branch)
    assert script.main(["check", "--base", "main", "--labels", ""], branch) == 1
    out = capsys.readouterr().out
    assert out.count("::error::") == 1  # bad.md counts as a change, so the branch check passes
    ok = branch.parent / "ok"
    ok.mkdir()
    assert script.main(["check"], _dir(ok, {})) == 0
