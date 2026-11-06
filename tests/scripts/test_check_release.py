"""Tests for scripts/check_release.py, the release gate in publish.yml (#457)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_release.py"
# A hook's location variables would aim these tests' git at the real repository (#522).
_GIT_DROPPED = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_REFERENCE_BACKEND",
)
_CHANGELOG = (
    "# Changelog\n\n## Unreleased\n\n## 1.2.3 (2026-11-06)\n\n- A note.\n\n## 1.2.2\n\n- Old.\n"
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_release", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_release"] = module
    spec.loader.exec_module(module)
    return module


script = _load_script()


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo at 1.2.3 whose HEAD is on origin/main."""
    for name in _GIT_DROPPED:
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
    (root / "CHANGELOG.md").write_text(_CHANGELOG, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "release")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def test_a_good_tag_passes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert script.main(["v1.2.3"], repo) == 0
    assert capsys.readouterr().out == "v1.2.3 can be released.\n"


@pytest.mark.parametrize(
    "tag", ["1.2.3", "v1.2.4", "v1.2.3rc1", "v1.2.3 ", "V1.2.3", "v1.2", "v", "vv1.2.3", ""]
)
def test_a_tag_that_isnt_the_version_is_refused(
    repo: Path, tag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert script.main([tag], repo) == 1
    err = capsys.readouterr().err
    assert f"tag {tag!r} doesn't match pyproject.toml's version '1.2.3'" in err
    assert "This run published nothing" in err


@pytest.mark.parametrize(
    ("changelog", "expected"),
    [
        ("# Changelog\n\n## Unreleased\n\n- A note.\n", "has no '## 1.2.3 (<date>)' section"),
        ("## 1.2.31 (2026-11-06)\n\n- Other.\n", "has no '## 1.2.3 (<date>)' section"),
        ("## 1x2x3 (2026-11-06)\n\n- Other.\n", "has no '## 1.2.3 (<date>)' section"),
        ("## 1.2.3\n\n- No date, so no space.\n", "has no '## 1.2.3 (<date>)' section"),
        ("### 1.2.3 (2026-11-06)\n\n- Not a section.\n", "has no '## 1.2.3 (<date>)' section"),
        ("## 1.2.3 (2026-11-06)\n\n## 1.2.2\n\n- Old.\n", "section for 1.2.3 is empty"),
        ("## 1.2.3 (2026-11-06)\n  \n", "section for 1.2.3 is empty"),
    ],
)
def test_a_changelog_with_no_notes_is_refused(changelog: str, expected: str) -> None:
    problem = script.changelog_problem(changelog, "1.2.3")
    assert problem is not None
    assert expected in problem


@pytest.mark.parametrize(
    "changelog",
    [
        "## 1.2.3 (2026-11-06)\n\n- A note.\n",
        "## 1.2.3(2026-11-06)\n\n- A note.\n",
        _CHANGELOG,
    ],
)
def test_a_section_with_notes_is_accepted(changelog: str) -> None:
    assert script.changelog_problem(changelog, "1.2.3") is None


def test_a_section_of_only_subheadings_is_empty() -> None:
    changelog = "## 1.2.3 (2026-11-06)\n\n### Added\n\n### Fixed\n\n## 1.2.2\n\n- Old.\n"
    assert "is empty" in (script.changelog_problem(changelog, "1.2.3") or "")


def test_a_note_starting_with_a_hash_is_a_note() -> None:
    assert script.changelog_problem("## 1.2.3 (2026-11-06)\n\n#457 fixed it\n", "1.2.3") is None


def test_a_commit_on_another_branch_is_refused(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(repo, "commit", "-q", "--allow-empty", "-m", "on a branch, not merged")
    assert script.main(["v1.2.3"], repo) == 1
    assert "isn't known to be on origin/main" in capsys.readouterr().err


def test_an_older_commit_on_main_passes(repo: Path) -> None:
    _git(repo, "commit", "-q", "--allow-empty", "-m", "later")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "HEAD~1")
    assert script.branch_problem(repo) is None


def test_a_backport_branch_passes(repo: Path) -> None:
    """v0.5.3 shipped from release/0.5.x, off main, so it carried only its fix."""
    _git(repo, "commit", "-q", "--allow-empty", "-m", "backport")
    _git(repo, "update-ref", "refs/remotes/origin/release/0.5.x", "HEAD")
    assert script.branch_problem(repo) is None


@pytest.mark.parametrize(
    "ref",
    [
        # An open release PR's branch, as this repo names them: right version, unmerged commit.
        "refs/remotes/origin/release/0.11.0",
        "refs/remotes/origin/release/v0.5.2",
        "refs/remotes/origin/release/1.x",
        "refs/remotes/origin/release/feature",
        "refs/remotes/origin/release",
        "refs/remotes/origin/feature/x",
        "refs/remotes/origin/releases/0.5.x",
        "refs/remotes/other/main",
        "refs/heads/release/0.5.x",
        "refs/tags/origin/main",
        "refs/tags/refs/remotes/origin/main",
    ],
)
def test_only_the_remotes_main_and_backport_branches_count(repo: Path, ref: str) -> None:
    """In particular, a tag named `origin/main` can't stand in for the branch."""
    _git(repo, "commit", "-q", "--allow-empty", "-m", "off main")
    _git(repo, "update-ref", ref, "HEAD")
    assert "isn't known to be on origin/main" in (script.branch_problem(repo) or "")


def test_no_branches_is_refused_not_assumed(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A shallow or partial checkout can't show where the commit is, so it doesn't pass."""
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert script.main(["v1.2.3"], repo) == 1
    assert "isn't known to be on origin/main" in capsys.readouterr().err


def test_outside_a_repository_is_refused_not_assumed(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    assert "couldn't check the tagged commit's branch" in (script.branch_problem(outside) or "")


@pytest.mark.parametrize(
    ("pyproject", "exception"),
    [
        (None, "FileNotFoundError"),
        ("[project\n", "TOMLDecodeError"),
        ('[project]\ndynamic = ["version"]\n', "KeyError"),
    ],
)
def test_an_unreadable_project_is_a_refusal_not_a_traceback(
    repo: Path, pyproject: str | None, exception: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = repo / "pyproject.toml"
    if pyproject is None:
        path.unlink()
    else:
        path.write_text(pyproject, encoding="utf-8")
    assert script.main(["v1.2.3"], repo) == 1
    err = capsys.readouterr().err
    assert f"couldn't read the version and CHANGELOG: {exception}" in err
    assert "This run published nothing" in err


def test_a_refusal_reaches_the_process_exit_code(repo: Path) -> None:
    """The job fails only through this: `main()`'s return value has to become the exit code."""
    refused = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "v9.9.9"], cwd=repo, capture_output=True, check=False
    )
    assert refused.returncode == 1
    passed = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "v1.2.3"], cwd=repo, capture_output=True, check=False
    )
    assert passed.returncode == 0


def test_no_git_is_refused_not_assumed(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PATH", "")
    assert script.main(["v1.2.3"], repo) == 1
    assert "couldn't run git" in capsys.readouterr().err


def test_every_problem_is_reported_at_once(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _git(repo, "commit", "-q", "--allow-empty", "-m", "not on main")
    (repo / "CHANGELOG.md").write_text("## Unreleased\n", encoding="utf-8")
    assert script.main(["v9.9.9"], repo) == 1
    err = capsys.readouterr().err
    assert err.count("error: ") == 3


@pytest.mark.parametrize("argv", [[], ["v1.2.3", "extra"]])
def test_a_wrong_argument_count_is_a_usage_error(
    argv: list[str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert script.main(argv, repo) == 2
    assert "usage:" in capsys.readouterr().err


def test_a_real_release_section_passes() -> None:
    """The heading form the gate wants is the one the release steps and CHANGELOG use."""
    root = _SCRIPT_PATH.parents[1]
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert script.changelog_problem(text, "0.10.0") is None
