"""Guards on the CI workflows."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _uv_syncs() -> list[tuple[str, str]]:
    return [
        (path.name, line.strip())
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.search(r"\buv sync\b", line)
    ]


def test_every_uv_sync_refuses_a_stale_lock() -> None:
    """#115: without `--locked`, a PR that changes a dependency and forgets `uv lock` passes,
    tested on versions nobody committed, and the sync rewrites `uv.lock` on the runner."""
    syncs = _uv_syncs()
    assert syncs, "no uv sync found: the workflows moved, so this guard checks nothing"
    assert [sync for sync in syncs if "--locked" not in sync[1]] == []


def test_the_guard_sees_the_release_workflows() -> None:
    """A release should be tested on what was committed, so these above all."""
    assert {name for name, _ in _uv_syncs()} >= {
        "ci.yml",
        "publish.yml",
        "test-publish.yml",
        "vdsm.yml",
    }
