"""Guards on the CI workflows."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

GITHUB = Path(__file__).resolve().parents[1] / ".github"
_UV_SYNC = re.compile(r"\buv\s+(?:-\S+\s+)*sync\b")
_COMMAND_BREAK = re.compile(r"&&|\|\||;|\|")


def _syncs_in(text: str) -> list[str]:
    """Each command in ``text`` that runs `uv sync`. By command, not by line: a continued line
    hides the sync, and another command's `--locked` isn't this one's (QA round 1 on #544).
    Comments are dropped, so one can't satisfy the guard."""
    found = []
    for line in re.sub(r"\\\n\s*", " ", text).splitlines():
        code = line.split(" #")[0]
        if code.lstrip().startswith("#"):
            continue
        found += [cmd.strip() for cmd in _COMMAND_BREAK.split(code) if _UV_SYNC.search(cmd)]
    return found


def _unlocked(syncs: list[str]) -> list[str]:
    return [sync for sync in syncs if "--locked" not in sync.split()]


def _uv_syncs() -> list[tuple[str, str]]:
    """Every `uv sync` that runs in a workflow, with its file's name."""
    return [
        (path.name, sync)
        for path in sorted(GITHUB.rglob("*.y*ml"))
        for sync in _syncs_in(path.read_text(encoding="utf-8"))
    ]


def test_every_uv_sync_refuses_a_stale_lock() -> None:
    """#115: without `--locked`, a PR that changes a dependency and forgets `uv lock` passes,
    tested on a lock nobody committed, and the sync rewrites `uv.lock` on the runner."""
    syncs = _uv_syncs()
    assert syncs, "no uv sync found: the workflows moved, so this guard checks nothing"
    assert _unlocked([sync for _, sync in syncs]) == []


@pytest.mark.parametrize(
    "text",
    [
        "run: uv sync --extra dev",
        "run: uv sync --extra dev&&echo --locked",
        "run: uv sync --extra dev || echo --locked",
        "run: uv sync --extra dev; echo --locked",
        "run: uv sync --extra dev | tee --locked",
        "run: uv sync --no-locked",
        "run: uv sync --locked-foo",
        "run: uv sync  # --locked",
        "run: |\n  uv \\\n    sync --extra dev",
    ],
)
def test_the_guard_catches_a_sync_without_the_flag(text: str) -> None:
    assert _unlocked(_syncs_in(text)) != []


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("run: uv sync --locked --extra dev", 1),
        ("run: uv sync --extra dev --locked && uv run pytest", 1),
        ("run: |\n  uv sync \\\n    --locked \\\n    --extra dev", 1),
        ("run: uv --quiet sync --locked", 1),
        ("# run: uv sync", 0),
        ("run: uv run pytest", 0),
    ],
)
def test_the_guard_passes_a_locked_sync(text: str, count: int) -> None:
    syncs = _syncs_in(text)
    assert len(syncs) == count
    assert _unlocked(syncs) == []


def test_the_guard_sees_every_workflow_that_syncs() -> None:
    """The publish workflows above all: a release should be tested on what was committed."""
    assert {name for name, _ in _uv_syncs()} >= {
        "ci.yml",
        "publish.yml",
        "test-publish.yml",
        "vdsm.yml",
    }


def test_python_updates_keep_the_lock_current() -> None:
    """With `--locked`, Dependabot's `pip` ecosystem can't pass CI: it changed
    pyproject.toml without uv.lock (#179)."""
    config = yaml.safe_load((GITHUB / "dependabot.yml").read_text(encoding="utf-8"))
    ecosystems = {update["package-ecosystem"] for update in config["updates"]}
    assert "uv" in ecosystems
    assert "pip" not in ecosystems
