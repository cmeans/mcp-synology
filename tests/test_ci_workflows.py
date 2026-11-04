"""Guards on the CI workflows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator

GITHUB = Path(__file__).resolve().parents[1] / ".github"
_RUNS_UV = re.compile(r"\buv\b")
_COMMENT_LINE = re.compile(r"^\s*#.*$", re.MULTILINE)


def _workflows() -> dict[str, Any]:
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((GITHUB / "workflows").glob("*.y*ml"))
    }


def _values(node: object, key: str) -> Iterator[Any]:
    """Every value of ``key``, however deep: a job's or a step's as well as the workflow's."""
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                yield value
            yield from _values(value, key)
    elif isinstance(node, list):
        for value in node:
            yield from _values(value, key)


def _runs_uv(workflow: object) -> bool:
    """Reading too much in is the safe direction: the variable is one line to add."""
    return any(
        _RUNS_UV.search(_COMMENT_LINE.sub("", script)) for script in _values(workflow, "run")
    )


def _lock_problem(workflow: dict[str, Any]) -> str | None:
    """Why this workflow's uv commands could use a stale lock, or None. By environment, not by
    reading each command: the variable covers `uv run` and any way of writing a sync, and
    parsing shell out of YAML found a new corner in every review (QA rounds 1 and 2 and pre-QA
    r3 on #544)."""
    if not _runs_uv(workflow):
        return None
    if (workflow.get("env") or {}).get("UV_LOCKED") != "1":
        return "no top-level env UV_LOCKED"
    for env in _values(workflow, "env"):
        if env and "UV_FROZEN" in env:
            return "UV_FROZEN skips the check"
        if env and env.get("UV_LOCKED", "1") != "1":
            return "a job or step turns UV_LOCKED off"
    return None


def test_every_workflow_that_runs_uv_refuses_a_stale_lock() -> None:
    """#115: without it, a PR that changes a dependency and forgets `uv lock` passes, tested on
    a lock nobody committed, and uv rewrites `uv.lock` on the runner."""
    problems = {name: _lock_problem(flow) for name, flow in _workflows().items()}
    assert {name: problem for name, problem in problems.items() if problem} == {}


def test_the_guard_sees_every_workflow_that_runs_uv() -> None:
    """The publish workflows above all: a release should be tested on what was committed."""
    assert {name for name, flow in _workflows().items() if _runs_uv(flow)} >= {
        "ci.yml",
        "publish.yml",
        "test-publish.yml",
        "vdsm.yml",
    }


@pytest.mark.parametrize(
    "workflow",
    [
        "jobs: {a: {steps: [{run: uv sync}]}}",
        "env: {UV_LOCKED: '0'}\njobs: {a: {steps: [{run: uv sync}]}}",
        "env: {OTHER: '1'}\njobs: {a: {steps: [{run: uv run pytest}]}}",
        "env: {UV_LOCKED: '1'}\njobs: {a: {env: {UV_LOCKED: '0'}, steps: [{run: uv sync}]}}",
        "env: {UV_LOCKED: '1'}\njobs: {a: {steps: [{run: uv sync, env: {UV_LOCKED: '0'}}]}}",
        "env: {UV_LOCKED: '1'}\njobs: {a: {steps: [{run: uv sync, env: {UV_FROZEN: '1'}}]}}",
    ],
)
def test_the_guard_catches_a_workflow_without_it(workflow: str) -> None:
    assert _lock_problem(yaml.safe_load(workflow)) is not None


@pytest.mark.parametrize(
    "workflow",
    [
        "env: {UV_LOCKED: '1'}\njobs: {a: {steps: [{run: uv sync}]}}",
        "env: {UV_LOCKED: '1'}\njobs: {a: {env: {OTHER: x}, steps: [{run: uv sync}]}}",
        "jobs: {a: {steps: [{run: echo hello}]}}",
        # Neither a comment nor a step's name is a command.
        'jobs: {a: {steps: [{run: "# about uv\\n echo hello"}]}}',
        "jobs: {a: {steps: [{name: uv sync, run: echo hello}]}}",
    ],
)
def test_the_guard_passes_a_workflow_with_it(workflow: str) -> None:
    assert _lock_problem(yaml.safe_load(workflow)) is None


def test_python_updates_keep_the_lock_current() -> None:
    """With a stale lock refused, Dependabot's `pip` ecosystem can't pass CI: it changed
    pyproject.toml without uv.lock (#179)."""
    config = yaml.safe_load((GITHUB / "dependabot.yml").read_text(encoding="utf-8"))
    ecosystems = {update["package-ecosystem"] for update in config["updates"]}
    assert "uv" in ecosystems
    assert "pip" not in ecosystems
