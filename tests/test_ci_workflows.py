"""Guards on the CI workflows."""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
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


# The one workflow that must not follow the lock (#115): `uv sync --upgrade` is its job.
_LOCK_EXEMPT = "deps-newest.yml"


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
    2 and pre-QA r3 on #544). It keeps the workflows honest and catches the usual opt-outs; it
    isn't a security boundary. Not seen: a command that leaves the project (`cd`, `--directory`,
    `--project`, `--script`, a step's `working-directory`), `uvx` and `uv tool`, `env -i`, and a
    variable expanded in a script. `uv pip` doesn't read the lock at all, so it isn't covered."""
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
    problems = {
        name: _lock_problem(flow) for name, flow in _workflows().items() if name != _LOCK_EXEMPT
    }
    assert {name: problem for name, problem in problems.items() if problem} == {}


def _newest() -> dict[str, Any]:
    return _workflows()[_LOCK_EXEMPT]


def test_the_exempt_workflow_upgrades_runs_the_tests_and_keeps_nothing() -> None:
    """Being exempt is only safe while it tests the newest versions and throws them away."""
    workflow = _newest()
    steps = workflow["jobs"]["newest"]["steps"]
    assert _scripts(steps) == [
        "uv sync --upgrade --extra dev",
        "uv pip list",
        "timeout 900 uv run pytest -n auto --no-cov",
    ]
    # By name: an upload or a PR-making action would keep what this throws away.
    used = [step["uses"].split("@")[0] for step in steps if "uses" in step]
    assert used == ["actions/checkout", "astral-sh/setup-uv"]
    assert workflow["permissions"] == {"contents": "read"}
    assert steps[0]["with"]["persist-credentials"] is False


def test_the_exempt_workflow_runs_on_the_newest_python_ci_tests() -> None:
    matrix = _workflows()["ci.yml"]["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    steps = _newest()["jobs"]["newest"]["steps"]
    versions = [s["with"]["python-version"] for s in steps if "python-version" in s.get("with", {})]
    newest = max(matrix, key=lambda v: tuple(int(part) for part in str(v).split(".")))
    assert versions == [str(newest)]


def test_only_the_report_job_can_write_and_it_runs_only_on_main() -> None:
    workflow = _newest()
    # YAML reads a bare `on` as True.
    assert set(workflow[True]) == {"schedule", "workflow_dispatch", "pull_request"}
    assert {name: job.get("permissions") for name, job in workflow["jobs"].items()} == {
        "newest": None,
        "report": {"issues": "write"},
    }
    report = workflow["jobs"]["report"]
    # `!cancelled()`: without it, a job that `needs` a failed one doesn't run at all.
    assert report["if"] == (
        "${{ !cancelled() && github.ref == 'refs/heads/main' && "
        "github.event_name != 'pull_request' && needs.newest.result != 'cancelled' }}"
    )
    assert not [step for step in report["steps"] if "uses" in step]


@pytest.mark.parametrize(
    ("result", "open_issue", "expected"),
    [
        ("failure", "", "issue create"),
        ("failure", "7", "issue comment 7 --body Still failing: url"),
        ("success", "7", "issue close 7 --comment The newest allowed dependencies pass again: url"),
        ("success", "", None),
    ],
    ids=["failure, none open", "failure, one open", "success, one open", "success, none open"],
)
def test_the_report_script(
    tmp_path: Path, result: str, open_issue: str, expected: str | None
) -> None:
    """It can't run on a PR, and a real run writes a real issue, so a stand-in `gh` takes it."""
    log = tmp_path / "gh.log"
    stub = tmp_path / "gh"
    stub.write_text(
        f"""#!/bin/bash
if [ "$1 $2" = "issue list" ]; then echo "{open_issue}"; exit 0; fi
echo "$*" >> "{log}"
""",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    script = _newest()["jobs"]["report"]["steps"][0]["run"]
    done = subprocess.run(
        ["bash", "-e", "-c", script],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "RESULT": result,
            "RUN_URL": "url",
            "TITLE": "title",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    assert len(calls) == (0 if expected is None else 1), calls
    if expected:
        assert calls[0].startswith(expected)


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
        f"{_LOCKED}jobs: {{a: {{env: '${{{{ fromJSON(x).UV_LOCKED }}}}', "
        "steps: [{run: uv sync}]}}",
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


def test_ci_tests_every_python_the_package_claims() -> None:
    """Pre-QA on #548: the classifiers could claim a version CI no longer tests."""
    project = tomllib.loads((GITHUB.parent / "pyproject.toml").read_text(encoding="utf-8"))
    classifiers = {
        c.rsplit(" :: ", 1)[-1]
        for c in project["project"]["classifiers"]
        if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)
    }
    matrix = _workflows()["ci.yml"]["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    assert classifiers
    assert classifiers == {str(version) for version in matrix}


def test_python_updates_keep_the_lock_current() -> None:
    """With a stale lock refused, Dependabot's `pip` ecosystem can't pass CI: it changed
    pyproject.toml without uv.lock (#179)."""
    config = yaml.safe_load((GITHUB / "dependabot.yml").read_text(encoding="utf-8"))
    ecosystems = {update["package-ecosystem"] for update in config["updates"]}
    assert "uv" in ecosystems
    assert "pip" not in ecosystems
