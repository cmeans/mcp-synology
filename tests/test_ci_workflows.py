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
# Plain text, so it fails loudly on a mention too: the safe direction, and one line to change.
_OPT_OUTS = ("UV_FROZEN", "UV_NO_SYNC", "UV_NO_PROJECT")
_OPT_OUT = re.compile(
    "|".join(
        ("--frozen", "--no-locked", "--no-sync", "--no-project", "--no_workspace", "UV_LOCKED")
        + _OPT_OUTS
    )
)


def _workflows() -> dict[str, Any]:
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((GITHUB / "workflows").glob("*.y*ml"))
    }


def _jobs(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return list((workflow.get("jobs") or {}).values())


def _scripts(steps: list[dict[str, Any]]) -> list[str]:
    return [_COMMENT_LINE.sub("", step["run"]) for step in steps if "run" in step]


def _workflow_scripts(workflow: dict[str, Any]) -> list[str]:
    return [script for job in _jobs(workflow) for script in _scripts(job.get("steps") or [])]


def _inner_envs(workflow: dict[str, Any]) -> Iterator[dict[str, Any] | str]:
    """Each job's and step's environment: a mapping, or an expression as a string, which the
    names are looked for in. An expression that doesn't spell them, like a matrix value, isn't
    seen."""
    for job in _jobs(workflow):
        for holder in (job, *(job.get("steps") or [])):
            if holder.get("env"):
                yield holder["env"]


def _runs_uv(workflow: dict[str, Any]) -> bool:
    return any(_RUNS_UV.search(script) for script in _workflow_scripts(workflow))


def _lock_problem(workflow: dict[str, Any]) -> str | None:
    """Why this workflow's `uv sync` and `uv run` could use a stale lock, or None. By environment,
    not by parsing each command: that found a new shell corner in every review (QA rounds 1 and
    2 and pre-QA r3 on #544). It keeps the workflows honest and catches the usual opt-outs; it isn't a security boundary.
    Not seen: a command that leaves the project (`cd`, `--directory`, `--project`, `--script`, a
    step's `working-directory`), `uvx` and `uv tool`, `env -i`, and a variable expanded in a
    script. `uv pip` doesn't read the lock at all, so it isn't covered."""
    if not _runs_uv(workflow):
        return None
    if (workflow.get("env") or {}).get("UV_LOCKED") != "1":
        return 'top-level env UV_LOCKED must be the string "1"'
    for env in _inner_envs(workflow):
        for name in ("UV_LOCKED", *_OPT_OUTS):
            if name in env:
                return f"a job or step sets {name}"
    for name in _OPT_OUTS:
        if name in workflow["env"]:
            return f"the workflow sets {name}"
    for script in _workflow_scripts(workflow):
        found = _OPT_OUT.search(script)
        if found:
            return f"a run step has {found.group()}, which gets past UV_LOCKED"
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


def test_no_local_action_runs_uv() -> None:
    """A composite action inherits the workflow's environment, so this guard wouldn't know
    which workflows need the variable. If one ever runs uv, teach the guard that first."""
    running_uv = [
        path.name
        for path in (GITHUB / "actions").rglob("action.y*ml")
        for script in _scripts(
            yaml.safe_load(path.read_text(encoding="utf-8"))["runs"].get("steps") or []
        )
        if _RUNS_UV.search(script)
    ]
    assert running_uv == []


_LOCKED = "env: {UV_LOCKED: '1'}\n"


@pytest.mark.parametrize(
    "workflow",
    [
        "jobs: {a: {steps: [{run: uv sync}]}}",
        "env: {UV_LOCKED: '0'}\njobs: {a: {steps: [{run: uv sync}]}}",
        "env: {UV_LOCKED: true}\njobs: {a: {steps: [{run: uv sync}]}}",
        "env: {OTHER: '1'}\njobs: {a: {steps: [{run: uv run pytest}]}}",
        f"{_LOCKED}jobs: {{a: {{env: {{UV_LOCKED: '0'}}, steps: [{{run: uv sync}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: uv sync, env: {{UV_LOCKED: '0'}}}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: uv sync, env: {{UV_FROZEN: '1'}}}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: uv run x, env: {{UV_NO_SYNC: '1'}}}}]}}}}",
        "env: {UV_LOCKED: '1', UV_NO_SYNC: '1'}\njobs: {a: {steps: [{run: uv run x}]}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: uv sync --frozen}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: uv run --frozen x}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: uv sync --no-locked}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: UV_LOCKED=0 uv sync}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: env -u UV_LOCKED uv sync}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: UV_FROZEN=1 uv sync}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: UV_NO_SYNC=1 uv run x}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: UV_NO_PROJECT=1 uv run x}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: uv run --no-sync x}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: uv run --no-project x}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: uv run --no_workspace x}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{env: '${{{{ fromJSON(x).UV_LOCKED }}}}', steps: [{{run: uv sync}}]}}}}",
        "env: {UV_LOCKED: '1', UV_FROZEN: '1'}\njobs: {a: {steps: [{run: uv sync}]}}",
        "env: {UV_LOCKED: '1', UV_NO_PROJECT: '1'}\njobs: {a: {steps: [{run: uv sync}]}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: 'echo UV_LOCKED=0 >> $GITHUB_ENV'}}, "
        "{run: uv sync}]}}",
    ],
)
def test_the_guard_catches_a_workflow_that_can_use_a_stale_lock(workflow: str) -> None:
    assert _lock_problem(yaml.safe_load(workflow)) is not None


@pytest.mark.parametrize(
    "workflow",
    [
        f"{_LOCKED}jobs: {{a: {{steps: [{{run: uv sync}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{env: {{OTHER: x}}, steps: [{{run: uv sync}}]}}}}",
        f"{_LOCKED}defaults: {{run: {{shell: bash}}}}\njobs: {{a: {{steps: [{{run: uv sync}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{defaults: {{run: {{shell: bash}}}}, steps: [{{run: uv sync}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{env: '${{{{ matrix.env }}}}', steps: [{{run: uv sync}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{strategy: {{matrix: {{env: [x]}}}}, steps: [{{run: uv sync}}]}}}}",
        f"{_LOCKED}jobs: {{a: {{steps: [{{uses: x/y@v1, with: {{env: p}}}}, {{run: uv sync}}]}}}}",
    ],
)
def test_the_guard_passes_a_locked_workflow(workflow: str) -> None:
    assert _lock_problem(yaml.safe_load(workflow)) is None


@pytest.mark.parametrize(
    "workflow",
    [
        "jobs: {a: {steps: [{run: echo hello}]}}",
        "jobs: {a: {steps: [{run: echo uvicorn}]}}",
        "defaults: {run: {shell: bash}}\njobs: {a: {steps: [{run: echo hello}]}}",
        'jobs: {a: {steps: [{run: "# about uv\\n echo hello"}]}}',
        "jobs: {a: {steps: [{name: uv sync, run: echo hello}]}}",
    ],
)
def test_the_guard_ignores_a_workflow_that_doesnt_run_uv(workflow: str) -> None:
    assert _lock_problem(yaml.safe_load(workflow)) is None


def test_python_updates_keep_the_lock_current() -> None:
    """With a stale lock refused, Dependabot's `pip` ecosystem can't pass CI: it changed
    pyproject.toml without uv.lock (#179)."""
    config = yaml.safe_load((GITHUB / "dependabot.yml").read_text(encoding="utf-8"))
    ecosystems = {update["package-ecosystem"] for update in config["updates"]}
    assert "uv" in ecosystems
    assert "pip" not in ecosystems
