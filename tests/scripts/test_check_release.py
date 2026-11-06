"""Tests for scripts/check_release.py, the release gate in publish.yml (#457)."""

from __future__ import annotations

import importlib.util
import json
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
_SERVER = {"version": "1.2.3", "packages": [{"version": "1.2.3"}]}


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
    (root / "server.json").write_text(json.dumps(_SERVER), encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "release")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def test_a_good_tag_passes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert script.main(["v1.2.3"], repo) == 0
    assert capsys.readouterr().out == "v1.2.3 passes the release checks.\n"


@pytest.mark.parametrize(
    "tag", ["1.2.3", "v1.2.4", "v1.2.3rc1", "v1.2.3 ", "V1.2.3", "v1.2", "v", "vv1.2.3", ""]
)
def test_a_tag_that_isnt_the_version_is_refused(
    repo: Path, tag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert script.main([tag], repo) == 1
    out = capsys.readouterr().out
    assert f"tag {tag!r} doesn't match this commit: pyproject.toml here says '1.2.3'" in out
    assert "This run published nothing" in out


def test_the_tag_refusal_never_offers_the_version_already_released() -> None:
    """Tagging before the release PR merges is the likeliest slip, and `v0.10.0` is shipped."""
    problem = script.tag_problem("v0.11.0", "0.10.0")
    assert "'v0.10.0'" not in problem
    assert "If the release PR hasn't merged, merge it" in problem
    assert "squash commit on origin/main" in problem


@pytest.mark.parametrize(
    ("changelog", "expected"),
    [
        ("# Changelog\n\n## Unreleased\n\n- A note.\n", "has no '## 1.2.3 (<date>)' section"),
        ("## 1.2.31 (2026-11-06)\n\n- Other.\n", "has no '## 1.2.3 (<date>)' section"),
        ("## 1x2x3 (2026-11-06)\n\n- Other.\n", "has no '## 1.2.3 (<date>)' section"),
        ("### 1.2.3 (2026-11-06)\n\n- Not a section.\n", "has no '## 1.2.3 (<date>)' section"),
        ("## 1.2.3\n\n- No date.\n", "'## 1.2.3' heading has no date"),
        ("## 1.2.3 (2026-11-06)\n\n## 1.2.2\n\n- Old.\n", "section for 1.2.3 has no notes"),
        ("## 1.2.3 (2026-11-06)\n  \n", "section for 1.2.3 has no notes"),
    ],
)
def test_a_changelog_with_no_notes_is_refused(changelog: str, expected: str) -> None:
    problem = script.changelog_problem(changelog, "1.2.3")
    assert problem is not None
    assert expected in problem


@pytest.mark.parametrize(
    "body",
    [
        "### Added\n\n### Fixed\n",
        "  ### Added\n",
        "###\n",
        "<!-- TODO notes -->\n",
        "<!--\nTODO\nnotes\n-->\n",
        "---\n",
        "* * *\n",
        "- \n",
        "*\n",
    ],
)
def test_leftovers_arent_notes(body: str) -> None:
    changelog = f"## 1.2.3 (2026-11-06)\n\n{body}\n## 1.2.2\n\n- Old.\n"
    assert "has no notes" in (script.changelog_problem(changelog, "1.2.3") or "")


@pytest.mark.parametrize(
    "changelog",
    [
        "## 1.2.3 (2026-11-06)\n\n- A note.\n",
        "## 1.2.3(2026-11-06)\n\n- A note.\n",
        "## 1.2.3 (2026-11-06)\n\n#457 fixed it\n",
        "## 1.2.3 (2026-11-06)\n\n### Added\n\n- A note.\n",
        "## 1.2.3 (2026-11-06)\n\n**Security release:** a note.\n",
        _CHANGELOG,
    ],
)
def test_a_section_with_notes_is_accepted(changelog: str) -> None:
    assert script.changelog_problem(changelog, "1.2.3") is None


def test_a_commit_later_than_the_release_commit_is_refused(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Retagging origin/main after a refusal would ship what merged since, with no notes."""
    text = _CHANGELOG.replace("## Unreleased\n", "## Unreleased\n\n- Merged later.\n")
    (repo / "CHANGELOG.md").write_text(text, encoding="utf-8")
    assert script.main(["v1.2.3"], repo) == 1
    out = capsys.readouterr().out
    assert "'## Unreleased' has entries" in out
    assert "squash commit on origin/main" in out
    assert "If this is the release commit, move those entries" in out


@pytest.mark.parametrize("heading", ["## [Unreleased]", "## unreleased", "## UNRELEASED"])
def test_any_spelling_of_unreleased_counts(heading: str) -> None:
    changelog = f"{heading}\n\n- Later.\n\n## 1.2.3 (2026-11-06)\n\n- A note.\n"
    assert "has entries" in (script.unreleased_problem(changelog, "1.2.3") or "")


@pytest.mark.parametrize(
    "changelog",
    [
        _CHANGELOG,
        # Empty but for leftovers, and a backport branch may have no such section.
        "## Unreleased\n\n### Added\n\n## 1.2.3 (2026-11-06)\n\n- A note.\n",
        "## 1.2.3 (2026-11-06)\n\n- A note.\n",
        # Only the section's own text counts.
        "## 1.2.3 (2026-11-06)\n\n- A note about ## Unreleased.\n",
    ],
)
def test_an_empty_or_missing_unreleased_passes(changelog: str) -> None:
    assert script.unreleased_problem(changelog, "1.2.3") is None


@pytest.mark.parametrize(
    "server",
    [
        {"version": "1.2.2", "packages": [{"version": "1.2.3"}]},
        {"version": "1.2.3", "packages": [{"version": "1.2.2"}]},
        {"version": "1.2.3", "packages": [{"version": "1.2.3"}, {"version": "1.2.2"}]},
    ],
)
def test_a_stale_server_json_is_refused_with_the_rest(
    repo: Path, server: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "server.json").write_text(json.dumps(server), encoding="utf-8")
    assert script.main(["v1.2.3"], repo) == 1
    out = capsys.readouterr().out
    assert "server.json says" in out
    assert "sync-server-json.py" in out
    assert "passes the release checks" not in out
    assert "This run published nothing" in out


@pytest.mark.parametrize(
    "server",
    [None, "not json", "[]", '{"version": "1.2.3"}', '{"version": "1.2.3", "packages": ["x"]}'],
)
def test_an_unreadable_server_json_is_refused_not_a_traceback(
    repo: Path, server: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    path = repo / "server.json"
    if server is None:
        path.unlink()
    else:
        path.write_text(server, encoding="utf-8")
    assert script.main(["v1.2.3"], repo) == 1
    assert "couldn't read server.json's versions" in capsys.readouterr().out


def test_a_commit_on_another_branch_is_refused(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(repo, "commit", "-q", "--allow-empty", "-m", "on a branch, not merged")
    assert script.main(["v1.2.3"], repo) == 1
    out = capsys.readouterr().out
    assert "isn't known to be on origin/main or release/1.2.x" in out
    assert "squash-merged" in out


def test_an_older_commit_on_main_passes(repo: Path) -> None:
    _git(repo, "commit", "-q", "--allow-empty", "-m", "later")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "HEAD~1")
    assert script.branch_problem(repo, "1.2.3") is None


def test_this_versions_backport_branch_passes(repo: Path) -> None:
    """v0.5.3 shipped from release/0.5.x, off main, so it carried only its fix."""
    _git(repo, "commit", "-q", "--allow-empty", "-m", "backport")
    _git(repo, "update-ref", "refs/remotes/origin/release/1.2.x", "HEAD")
    assert script.branch_problem(repo, "1.2.3") is None


@pytest.mark.parametrize(
    "ref",
    [
        # An open release PR's branch, as this repo names them: right version, unmerged commit.
        "refs/remotes/origin/release/1.2.3",
        "refs/remotes/origin/release/v1.2.3",
        "refs/remotes/origin/release/1.2.3.x",
        "refs/remotes/origin/release/v1.2.x",
        # git's patterns match below a name too.
        "refs/remotes/origin/release/1.2.x/feature",
        # Another version's backport branch.
        "refs/remotes/origin/release/0.5.x",
        "refs/remotes/origin/release/1.3.x",
        "refs/remotes/origin/release/1.x",
        "refs/remotes/origin/release/hotfix.for.x",
        "refs/remotes/origin/release/feature",
        "refs/remotes/origin/release",
        "refs/remotes/origin/feature/x",
        "refs/remotes/origin/releases/1.2.x",
        "refs/remotes/other/main",
        "refs/heads/release/1.2.x",
        "refs/tags/origin/main",
        "refs/tags/refs/remotes/origin/main",
    ],
)
def test_only_the_remotes_main_and_this_versions_backport_count(repo: Path, ref: str) -> None:
    """In particular, a tag named `origin/main` can't stand in for the branch."""
    _git(repo, "commit", "-q", "--allow-empty", "-m", "off main")
    _git(repo, "update-ref", ref, "HEAD")
    assert "isn't known to be on origin/main" in (script.branch_problem(repo, "1.2.3") or "")


def test_no_branches_is_refused_not_assumed(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A shallow or partial checkout can't show where the commit is, so it doesn't pass."""
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert script.main(["v1.2.3"], repo) == 1
    assert "isn't known to be on origin/main" in capsys.readouterr().out


def test_outside_a_repository_is_refused_not_assumed(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    problem = script.branch_problem(outside, "1.2.3")
    assert "couldn't check the tagged commit's branch" in (problem or "")


def test_no_git_is_refused_not_assumed(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PATH", "")
    assert script.main(["v1.2.3"], repo) == 1
    assert "couldn't run git" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("pyproject", "exception"),
    [
        (None, "FileNotFoundError"),
        ("[project\n", "TOMLDecodeError"),
        ('[project]\ndynamic = ["version"]\n', "KeyError"),
        ("[project]\nversion = 1\n", "TypeError"),
        ('project = "x"\n', "TypeError"),
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
    out = capsys.readouterr().out
    assert f"couldn't read the version and CHANGELOG: {exception}" in out
    assert "This run published nothing" in out


@pytest.mark.parametrize("name", ["pyproject.toml", "CHANGELOG.md"])
def test_a_file_that_isnt_utf8_is_a_refusal_not_a_traceback(
    repo: Path, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / name).write_bytes(b"\xff\xfe")
    assert script.main(["v1.2.3"], repo) == 1
    assert "couldn't read the version and CHANGELOG: UnicodeDecodeError" in capsys.readouterr().out


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


def test_every_problem_is_reported_at_once_as_an_annotation(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(repo, "commit", "-q", "--allow-empty", "-m", "not on main")
    (repo / "CHANGELOG.md").write_text("## Unreleased\n\n- Later.\n", encoding="utf-8")
    (repo / "server.json").write_text("{}", encoding="utf-8")
    assert script.main(["v9.9.9"], repo) == 1
    lines = capsys.readouterr().out.splitlines()
    assert sum(line.startswith("::error::") for line in lines) == 5
    assert lines[-1].startswith("This run published nothing.")
    assert "If it was already released, don't delete it." in lines[-1]


def test_an_annotation_cant_be_split_or_decoded_by_its_text() -> None:
    assert script._annotation("a%0Ab\r\nc") == "::error::a%250Ab%0D%0Ac"


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
