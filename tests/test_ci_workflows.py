"""Guards on the CI workflows."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

GITHUB = Path(__file__).resolve().parents[1] / ".github"
_UV_SYNC = re.compile(r"\buv\s+(?:-\S+\s+)*sync\b")


def _uv_syncs() -> list[tuple[str, str]]:
    """Every `uv sync` that runs: comments are dropped, so one can't satisfy the guard."""
    found = []
    for path in sorted(GITHUB.rglob("*.y*ml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            code = line.split(" #")[0]
            if not code.lstrip().startswith("#") and _UV_SYNC.search(code):
                found.append((path.name, code.strip()))
    return found


def test_every_uv_sync_refuses_a_stale_lock() -> None:
    """#115: without `--locked`, a PR that changes a dependency and forgets `uv lock` passes,
    tested on a lock nobody committed, and the sync rewrites `uv.lock` on the runner."""
    syncs = _uv_syncs()
    assert syncs, "no uv sync found: the workflows moved, so this guard checks nothing"
    assert [sync for sync in syncs if "--locked" not in sync[1]] == []


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
