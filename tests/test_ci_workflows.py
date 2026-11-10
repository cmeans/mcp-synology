"""Guards on the CI workflows."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
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


def test_nothing_publishes_before_the_release_gate() -> None:
    """Every other job in publish.yml waits on `gate`, directly or through another job."""
    jobs = _workflows()["publish.yml"]["jobs"]

    def waits_for_gate(name: str) -> bool:
        needs = jobs[name].get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        return "gate" in needs or any(waits_for_gate(need) for need in needs)

    assert {name for name in jobs if name != "gate" and not waits_for_gate(name)} == set()
    # `if: always()` would run a job even when the gate failed.
    assert {name for name, job in jobs.items() if "if" in job} == set()


def test_the_gate_and_the_release_notes_read_the_same_heading() -> None:
    """check_release.py requires the section the github-release job's awk extracts."""
    text = (GITHUB / "workflows" / "publish.yml").read_text(encoding="utf-8")
    assert '$0 ~ "^## "version"( |\\\\()" {flag=1; next}' in text
    script = (GITHUB.parent / "scripts" / "check_release.py").read_text(encoding="utf-8")
    assert 'rf"{escaped}(?: |\\()"' in script


def test_the_release_gate_checks_the_tag_on_a_full_checkout_and_cant_be_softened() -> None:
    job = _workflows()["publish.yml"]["jobs"]["gate"]
    assert _scripts(job["steps"]) == ['python scripts/check_release.py "$TAG"']
    # The tag goes through env, never into the script: a tag name is free text.
    assert job["steps"][-1]["env"]["TAG"] == "${{ github.ref_name }}"
    checkout = job["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0  # A shallow clone has no origin/main history.
    assert checkout["with"]["persist-credentials"] is False
    assert "ref" not in checkout["with"]  # It must check the tag, not some branch.
    assert "permissions" not in job  # The workflow's contents: read.
    assert "continue-on-error" not in job
    assert "if" not in job
    assert not [step for step in job["steps"] if {"continue-on-error", "if"} & step.keys()]


def _floor_steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _workflows()["ci.yml"]["jobs"]["floor"]["steps"]
    return steps


def test_the_floor_job_tests_the_floors_not_the_lock() -> None:
    """`uv run` would sync back to the lock, so the job would pass without testing a floor."""
    assert _scripts(_floor_steps()) == [
        "timeout 900 uv sync --locked --extra dev",
        ".venv/bin/python scripts/runtime_floors.py > floors.txt\n"
        "timeout 900 uv pip install -r floors.txt\n",
        "uv pip check",
        "uv pip list",
        "timeout 900 .venv/bin/python -m pytest -n auto --no-cov --timeout=120",
    ]
    used = [step["uses"].split("@")[0] for step in _floor_steps() if "uses" in step]
    assert used == ["actions/checkout", "astral-sh/setup-uv"]
    assert _floor_steps()[0]["with"]["persist-credentials"] is False


def test_the_floor_job_runs_on_every_pr_and_isnt_soft() -> None:
    """A floor breaks when our code changes, so a path filter or continue-on-error misses it."""
    workflow = _workflows()["ci.yml"]
    triggers = workflow.get("on", workflow[True])  # YAML 1.1 reads a bare `on` as True.
    assert triggers["pull_request"] == {"branches": ["main"]}
    assert "if" not in workflow["jobs"]["floor"]
    assert "continue-on-error" not in workflow["jobs"]["floor"]
    assert not [step for step in _floor_steps() if {"continue-on-error", "if"} & step.keys()]


def test_the_floor_job_runs_on_the_oldest_and_newest_python_ci_tests() -> None:
    jobs = _workflows()["ci.yml"]["jobs"]
    matrix = jobs["test"]["strategy"]["matrix"]["python-version"]
    by_version = sorted(matrix, key=lambda v: tuple(int(part) for part in str(v).split(".")))
    assert jobs["floor"]["strategy"]["matrix"]["python-version"] == [by_version[0], by_version[-1]]
    assert jobs["floor"]["strategy"]["fail-fast"] is False
    versions = [
        s["with"]["python-version"] for s in _floor_steps() if "python-version" in s.get("with", {})
    ]
    assert versions == ["${{ matrix.python-version }}"]


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


_SCRIPTS = GITHUB / "scripts"
_CODEQL_APP = "github-advanced-security"


class _FakeGitHub:
    """A stand-in `gh` for the label scripts, which can't run on a PR: `workflow_run` and `schedule`
    run from main. It answers what `--jq` would have given, and logs every write."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.log = folder / "writes.log"
        self.answers = folder / "answers"
        self.answers.mkdir()
        stub = folder / "gh"
        stub.write_text(
            rf"""#!/bin/bash
ANSWERS="{self.answers}"
case "$*" in
  *workflows/ci.yml/runs?head_sha=abc\&*) cat "$ANSWERS/ci_run" 2>/dev/null; exit 0 ;;
  *workflows/vdsm.yml/runs?head_sha=abc\&*) cat "$ANSWERS/vdsm_run" 2>/dev/null; exit 0 ;;
  *actions/runs/1/jobs*) cat "$ANSWERS/ci_jobs" 2>/dev/null; exit 0 ;;
  *actions/runs/2/jobs*) cat "$ANSWERS/vdsm_jobs" 2>/dev/null; exit 0 ;;
  *commits/abc/check-runs?check_name=CodeQL\&app_id=57789\ *)
    [ -f "$ANSWERS/codeql_fail" ] && exit 1
    if [ -f "$ANSWERS/codeql_json" ]; then
      while [ "$1" != --jq ]; do shift; done
      jq -r "$2" "$ANSWERS/codeql_json"
    else
      cat "$ANSWERS/codeql" 2>/dev/null
    fi
    exit 0 ;;
  "pr view"*"--json headRefOid"*) [ -f "$ANSWERS/head_fail" ] && exit 1
    cat "$ANSWERS/head"; exit 0 ;;
  "pr view"*"--json labels"*) cat "$ANSWERS/labels" 2>/dev/null; exit 0 ;;
  "pr view"*"--json comments"*) [ -f "$ANSWERS/comments_fail" ] && exit 1
    cat "$ANSWERS/comments" 2>/dev/null; exit 0 ;;
  "pr list"*) if [ -f "$ANSWERS/prs_json" ]; then
      while [ "$1" != --jq ]; do shift; done
      jq -r "$2" "$ANSWERS/prs_json"
    else
      cat "$ANSWERS/prs" 2>/dev/null
    fi
    exit 0 ;;
esac
case "$*" in "pr comment"*) [ -f "$ANSWERS/comment_fail" ] && exit 1 ;; esac
echo "$*" >> "{self.log}"
""",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        self.answer(head="abc")

    def answer(self, **files: str) -> None:
        for name, text in files.items():
            (self.answers / name).write_text(text, encoding="utf-8")

    def runs(
        self,
        ci: str = "completed success",
        vdsm: str = "completed success",
        updated: str = "2026-01-01T00:00:00Z",
    ) -> None:
        self.answer(ci_run=f"1 {ci} {updated}\n", vdsm_run=f"2 {vdsm} {updated}\n")

    def writes(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def run(self, script: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(_SCRIPTS / script), *args],
            env={
                **os.environ,
                "PATH": f"{self.folder}{os.pathsep}{os.environ['PATH']}",
                "REPO": "o/r",
                # Only the script that takes it from the environment: the others must pass it on.
                **({"HEAD_SHA": "abc"} if script == "pr-checks-state.sh" else {}),
                "RECHECK_SECONDS": "0",
                **env,
            },
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.fixture
def github(tmp_path: Path) -> _FakeGitHub:
    return _FakeGitHub(tmp_path)


_GOOD = "completed\tsuccess\tbuild\n"
_GNU_DATE = pytest.mark.skipif(sys.platform == "darwin", reason="the script runs on ubuntu")


@pytest.mark.parametrize(
    ("ci_jobs", "vdsm_jobs", "expected"),
    [
        (_GOOD, _GOOD, "pass"),
        (_GOOD, "completed\tskipped\tbuild\n", "pass"),
        (_GOOD, "completed\tfailure\tvdsm (7.1)\n", "fail"),
        (_GOOD, "in_progress\tnone\tvdsm (7.1)\n", "pending"),
        (_GOOD, "completed\tnone\tvdsm (7.1)\n", "pending"),
        (_GOOD, "", "pending"),
    ],
    ids=["passed", "skipped", "failed job", "job running", "no conclusion", "no jobs listed"],
)
def test_the_state_is_judged_job_by_job(
    github: _FakeGitHub, ci_jobs: str, vdsm_jobs: str, expected: str
) -> None:
    github.runs()
    github.answer(ci_jobs=ci_jobs, vdsm_jobs=vdsm_jobs)
    done = github.run("pr-checks-state.sh")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == expected


@pytest.mark.parametrize(
    ("codeql", "expected"),
    [
        ("completed success\n", "pass"),
        ("completed neutral\n", "pass"),
        ("completed skipped\n", "pass"),
        ("completed failure\n", "fail"),
        ("completed action_required\n", "fail"),
        ("completed cancelled\n", "fail"),
        ("completed timed_out\n", "fail"),
        ("completed stale\n", "fail"),
        ("in_progress null\n", "pending"),
        ("queued null\n", "pending"),
        # Not every commit gets one, and pending would hold such a PR forever.
        ("", "pass"),
    ],
)
def test_codeqls_check_run_counts(github: _FakeGitHub, codeql: str, expected: str) -> None:
    """#578: it failed on #574 with the PR still on Awaiting CI and nobody told."""
    github.runs()
    github.answer(ci_jobs=_GOOD, vdsm_jobs=_GOOD, codeql=codeql)
    done = github.run("pr-checks-state.sh")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == expected


def test_a_codeql_failure_is_a_failure_while_others_still_run(github: _FakeGitHub) -> None:
    github.runs()
    github.answer(ci_jobs=_GOOD, vdsm_jobs="in_progress\tnone\tvdsm (7.1)\n")
    github.answer(codeql="completed failure\n")
    assert github.run("pr-checks-state.sh").stdout.strip() == "fail"


@pytest.mark.parametrize("codeql", ["in_progress null", "queued null", "completed null"])
def test_a_codeql_check_with_no_result_is_missing_for_the_recheck(
    github: _FakeGitHub, tmp_path: Path, codeql: str
) -> None:
    """Not `running`, which the recheck never comments on, so a stuck check can't hold a PR with
    no word. A completed one with no conclusion is pending, as a job is (#298)."""
    github.runs()
    github.answer(ci_jobs=_GOOD, vdsm_jobs=_GOOD, codeql=f"{codeql}\n")
    details = tmp_path / "details"
    done = github.run("pr-checks-state.sh", PENDING_DETAILS=str(details))
    assert done.stdout.strip() == "pending"
    lines = details.read_text(encoding="utf-8").splitlines()
    assert f"missing CodeQL: {codeql}" in lines
    assert "running" not in lines


def test_codeql_passing_doesnt_promote_while_another_result_is_pending(
    github: _FakeGitHub,
) -> None:
    github.runs(vdsm="in_progress null")
    github.answer(ci_jobs=_GOOD, codeql="completed success\n")
    assert github.run("pr-checks-state.sh").stdout.strip() == "pending"


def test_codeqls_check_run_is_only_read_when_it_can_change_the_answer(
    github: _FakeGitHub,
) -> None:
    """A failure elsewhere already decides it, so an API error here can't mask that."""
    github.runs()
    github.answer(ci_jobs="completed\tfailure\tbuild\n", vdsm_jobs=_GOOD, codeql_fail="")
    assert github.run("pr-checks-state.sh").stdout.strip() == "fail"


def test_an_unreadable_codeql_check_fails_the_run_not_the_pr(github: _FakeGitHub) -> None:
    """A token without `checks: read` must show as a broken job, never as a pass."""
    github.runs()
    github.answer(ci_jobs=_GOOD, vdsm_jobs=_GOOD, codeql_fail="")
    done = github.run("pr-checks-state.sh")
    assert done.returncode != 0
    assert done.stdout.strip() == ""


@pytest.mark.parametrize("name", ["pr-labels.yml", "pr-labels-ci.yml", "pr-labels-recheck.yml"])
def test_every_workflow_that_reads_the_state_can_read_check_runs(name: str) -> None:
    """The state script reads CodeQL's check run; a denied read would fail the labeling job."""
    workflow = _workflows()[name]
    readers = [
        job
        for job in workflow["jobs"].values()
        if any(".github/scripts/pr-labels" in step.get("run", "") for step in job["steps"])
    ]
    assert readers
    for job in readers:
        # A job's own block replaces the workflow's.
        scope = job["permissions"] if "permissions" in job else workflow.get("permissions") or {}
        assert scope.get("checks") == "read"


def test_a_run_not_listed_is_pending(github: _FakeGitHub) -> None:
    """#339: the listing came back empty for a CI run that had finished."""
    github.answer(vdsm_run="2 completed success 2026-01-01T00:00:00Z\n", vdsm_jobs=_GOOD)
    assert github.run("pr-checks-state.sh").stdout.strip() == "pending"


def test_a_run_that_failed_before_any_job_is_failed(github: _FakeGitHub) -> None:
    github.runs(ci="completed startup_failure")
    assert github.run("pr-checks-state.sh").stdout.strip() == "fail"


@pytest.mark.parametrize(
    ("labels", "state_jobs", "adopt", "expected"),
    [
        ("Awaiting CI\n", _GOOD, "", ["--remove-label Awaiting CI", "--add-label Ready for QA"]),
        ("CI Failed\n", _GOOD, "", ["--remove-label CI Failed", "--add-label Ready for QA"]),
        ("Awaiting CI\nMerge conflict\n", _GOOD, "", ["--remove-label Awaiting CI"]),
        ("Ready for QA Signoff\n", _GOOD, "", []),
        ("Awaiting CI\nDev Active\n", _GOOD, "", []),
        ("", _GOOD, "1", ["--add-label Ready for QA"]),
        ("", "", "1", ["--add-label Awaiting CI"]),
        ("", "completed\tfailure\tx\n", "1", ["--add-label CI Failed"]),
        (
            "Awaiting CI\n",
            "completed\tfailure\tx\n",
            "",
            ["--remove-label Awaiting CI", "--add-label CI Failed"],
        ),
        ("Awaiting CI\n", "", "", []),
        # The QA label is never the one taken away.
        (
            "Ready for QA\nQA Active\n",
            "completed\tfailure\tx\n",
            "",
            ["--remove-label Ready for QA", "--add-label CI Failed"],
        ),
        ("Ready for QA\n", "", "", []),
        ("Ready for QA\nQA Active\n", "", "", []),
    ],
    ids=[
        "pass",
        "green again",
        "pass with a conflict",
        "later label left alone",
        "dev isn't done",
        "adopted, pass",
        "adopted, pending",
        "adopted, fail",
        "fail",
        "pending",
        "fail beside a QA label",
        "a hand label, pending",
        "a hand label beside a QA label, pending",
    ],
)
def test_settling_the_labels(
    github: _FakeGitHub, labels: str, state_jobs: str, adopt: str, expected: list[str]
) -> None:
    github.runs()
    github.answer(ci_jobs=state_jobs, vdsm_jobs=state_jobs, labels=labels)
    done = github.run("pr-labels-settle.sh", "7", "abc", ADOPT=adopt)
    assert done.returncode == 0, done.stderr
    assert [write.split(" --repo o/r ")[1] for write in github.writes()] == expected


def test_a_commit_that_moved_on_is_left_to_its_own_runs(github: _FakeGitHub) -> None:
    github.runs()
    github.answer(ci_jobs=_GOOD, vdsm_jobs=_GOOD, labels="Awaiting CI\n", head="newer")
    done = github.run("pr-labels-settle.sh", "7", "abc")
    assert done.stdout.strip() == "moved"
    assert github.writes() == []


def test_the_recheck_promotes_a_stranded_green_pr(github: _FakeGitHub) -> None:
    """#549 and #339: nothing checked again after the last CI event."""
    github.runs()
    github.answer(ci_jobs=_GOOD, vdsm_jobs=_GOOD, labels="Awaiting CI\n", prs="7\tabc\t0\n")
    done = github.run("pr-labels-recheck.sh")
    assert done.returncode == 0, done.stderr
    assert any("--add-label Ready for QA" in write for write in github.writes())


@pytest.mark.skipif(sys.platform == "darwin", reason="the script runs on ubuntu")
def test_the_recheck_comments_once_when_a_result_never_comes(github: _FakeGitHub) -> None:
    github.runs()
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs="in_progress\tnone\tvdsm (7.1)\n",
        labels="Awaiting CI\n",
        prs="7\tabc\t0\n",
    )
    assert github.run("pr-labels-recheck.sh").returncode == 0
    comments = [write for write in github.writes() if write.startswith("pr comment 7")]
    assert len(comments) == 1
    assert "vdsm (7.1)" in " ".join(github.writes())
    # The missing names are in the marker, without their status (QA round 3 on #582).
    marker = "<!-- pr-labels-recheck abc vdsm.yml: vdsm (7.1) -->"
    assert marker in " ".join(github.writes())
    github.answer(comments=f"{marker}\n")
    assert github.run("pr-labels-recheck.sh").returncode == 0
    assert len([write for write in github.writes() if write.startswith("pr comment 7")]) == 1


@pytest.mark.parametrize(
    "setup",
    ["running", "approval", pytest.param("recent", marks=_GNU_DATE), "no run listed"],
)
def test_the_recheck_says_nothing_while_it_can_still_come(github: _FakeGitHub, setup: str) -> None:
    github.answer(labels="Awaiting CI\n", prs="7\tabc\t0\n", ci_jobs=_GOOD)
    if setup == "running":
        # A finished run's job has no result, but another run still going can yet settle it.
        github.runs(ci="in_progress null")
        github.answer(vdsm_jobs="completed\tnone\tx\n")
    elif setup == "approval":
        github.runs(ci="completed action_required")
        github.answer(vdsm_jobs="completed\tnone\tx\n")
    elif setup == "recent":
        now = subprocess.run(
            ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True, text=True, check=True
        ).stdout.strip()
        github.runs(updated=now)
        github.answer(vdsm_jobs="in_progress\tnone\tx\n")
    assert github.run("pr-labels-recheck.sh").returncode == 0
    assert not [write for write in github.writes() if write.startswith("pr comment")]


def test_the_recheck_carries_on_after_one_prs_error(github: _FakeGitHub) -> None:
    """One bad PR mustn't strand the rest, and the run still ends red for it."""
    github.runs()
    github.answer(
        ci_jobs=_GOOD, vdsm_jobs=_GOOD, labels="Awaiting CI\n", prs="7\tabc\t0\n8\tabc\t0\n"
    )
    stub = github.folder / "gh"
    stub.write_text(
        stub.read_text(encoding="utf-8").replace(
            'case "$*" in',
            'if [ "$1 $2 $3" = "pr view 7" ]; then exit 1; fi\ncase "$*" in',
        ),
        encoding="utf-8",
    )
    done = github.run("pr-labels-recheck.sh")
    assert done.returncode == 1
    assert any(write.startswith("pr edit 8") for write in github.writes())


def test_the_recheck_runs_on_a_schedule_with_only_what_it_needs() -> None:
    workflow = _workflows()["pr-labels-recheck.yml"]
    # PyYAML reads the `on` key as True.
    assert "schedule" in workflow[True]
    job = workflow["jobs"]["recheck"]
    assert workflow["permissions"] == {}
    assert job["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
        "actions": "read",
        "checks": "read",
    }
    assert "pr-labels-recheck.sh" in job["steps"][-1]["run"]


def test_the_checks_are_read_in_one_place() -> None:
    """Two copies of this logic had to be kept in sync by hand, and a third was coming (#549)."""
    for name in ("pr-labels.yml", "pr-labels-ci.yml", "pr-labels-recheck.yml"):
        text = (GITHUB / "workflows" / name).read_text(encoding="utf-8")
        assert "actions/runs/" not in text, name


# Not skipped on CI: a missing jq there would hide the query tests behind a green run.
_NEEDS_JQ = pytest.mark.skipif(
    shutil.which("jq") is None and "CI" not in os.environ, reason="needs jq"
)


def _pr(number: int, *labels: str, draft: bool = False, fork: bool = False) -> dict[str, Any]:
    return {
        "number": number,
        "headRefOid": "abc",
        "isDraft": draft,
        "isCrossRepository": fork,
        "labels": [{"name": label} for label in labels],
    }


def _check_run(conclusion: str | None, status: str = "completed", slug: str = _CODEQL_APP) -> Any:
    return {"status": status, "conclusion": conclusion, "app": {"slug": slug}}


@_NEEDS_JQ
@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([], "pass"),
        ([_check_run("failure", slug="github-actions")], "pass"),
        ([_check_run("failure")], "fail"),
        ([_check_run("success")], "pass"),
        ([_check_run(None, status="in_progress")], "pending"),
        ([_check_run("failure", slug="github-actions"), _check_run("failure")], "fail"),
        ([_check_run("success"), _check_run("failure", slug="github-actions")], "pass"),
        ([_check_run("failure"), _check_run("success")], "fail"),
    ],
    ids=[
        "no check run",
        "another app's job named CodeQL",
        "failed",
        "passed",
        "running",
        "the real one after another app's",
        "the real one before another app's",
        "two of its own: the first",
    ],
)
def test_the_codeql_query_on_what_the_api_answers(
    github: _FakeGitHub, runs: list[Any], expected: str
) -> None:
    """QA round 1 on #582: with the query's output canned, nothing tested the query."""
    github.runs()
    github.answer(ci_jobs=_GOOD, vdsm_jobs=_GOOD, codeql_json=json.dumps({"check_runs": runs}))
    assert github.run("pr-checks-state.sh").stdout.strip() == expected


@_NEEDS_JQ
@pytest.mark.parametrize(
    ("answers", "named"),
    [
        (
            {"ci_jobs": _GOOD, "vdsm_jobs": _GOOD, "codeql": "completed failure\n"},
            "CodeQL",
        ),
        (
            {"ci_jobs": _GOOD, "vdsm_jobs": "completed\tfailure\tx\ncompleted\tfailure\ty\n"},
            "vdsm.yml",
        ),
    ],
    ids=["CodeQL", "a job"],
)
def test_a_failure_at_ready_for_qa_comments_and_keeps_the_label(
    github: _FakeGitHub, answers: dict[str, str], named: str
) -> None:
    """#578, maintainer 2026-11-06: the label may be a person's decision."""
    github.runs()
    github.answer(labels="Ready for QA\n", prs_json=json.dumps([_pr(7, "Ready for QA")]), **answers)
    assert github.run("pr-labels-recheck.sh").returncode == 0
    writes = "\n".join(github.writes())
    assert "pr comment 7" in writes
    assert "pr-labels-recheck-failed abc" in writes
    # Exactly the name, once, with nothing else from the details.
    assert f"```\n{named}\n```" in writes
    assert "This PR stays at `Ready for QA`" in writes
    assert "--remove-label" not in writes
    assert "--add-label" not in writes


@_NEEDS_JQ
def test_the_failure_comment_s_advice_can_be_followed(github: _FakeGitHub) -> None:
    """QA round 3 on #582: "clears with a push" read as an empty commit, "on purpose" couldn't
    be checked, and "the Checks tab" was a UI pointer; a fork gets no CI Failed by itself."""
    github.runs()
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs=_GOOD,
        codeql="completed failure\n",
        labels="Ready for QA\n",
        prs_json=json.dumps([_pr(7, "Ready for QA")]),
    )
    assert github.run("pr-labels-recheck.sh").returncode == 0
    writes = "\n".join(github.writes())
    assert "`gh pr checks 7` names each failure and shows the current state" in writes
    assert "this comment isn't updated when a check is re-run" in writes
    assert (
        "needs what its alert reports fixed and pushed: an empty commit is expected to fail it"
        in writes
    )
    # Paginated, as the latest label is on a later page of a busy PR (pre-QA r4 on #582).
    assert "`gh api --paginate repos/o/r/issues/7/events --jq" in writes
    assert "on a fork or a draft nothing adds a label, so add `CI Failed` by hand" in writes
    assert "Checks tab" not in writes


@_NEEDS_JQ
def test_a_lagging_job_s_comment_doesnt_silence_a_stuck_codeql(github: _FakeGitHub) -> None:
    """QA round 3 on #582: one marker per commit meant a later stuck CodeQL got no comment."""
    github.runs(ci="in_progress null", vdsm="completed success")
    github.answer(
        vdsm_jobs=_GOOD,
        codeql="in_progress\n",
        labels="Awaiting CI\n",
        prs_json=json.dumps([_pr(7, "Awaiting CI")]),
        comments="<!-- pr-labels-recheck abc vdsm.yml: vdsm (7.1) -->\n",
    )
    github.runs(ci="completed success", vdsm="completed success")
    github.answer(ci_jobs=_GOOD)
    done = github.run("pr-labels-recheck.sh")
    assert done.returncode == 0, done.stderr
    writes = "\n".join(github.writes())
    assert "<!-- pr-labels-recheck abc CodeQL -->" in writes, done.stderr


@_NEEDS_JQ
def test_the_missing_marker_names_every_missing_result(github: _FakeGitHub) -> None:
    """Pre-QA r4 on #582: keeping only the first name passed every test."""
    github.runs(ci="completed success", vdsm="completed success")
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs="in_progress\tnull\tvdsm (7.1)\n",
        codeql="in_progress\n",
        labels="Awaiting CI\n",
        prs_json=json.dumps([_pr(7, "Awaiting CI")]),
    )
    done = github.run("pr-labels-recheck.sh")
    assert done.returncode == 0, done.stderr
    marker = next(line for line in github.writes() if "<!-- pr-labels-recheck abc" in line)
    assert "CodeQL" in marker and "vdsm.yml" in marker, marker


@_NEEDS_JQ
def test_a_forks_job_name_can_t_close_the_marker(github: _FakeGitHub) -> None:
    """The marker's character filter keeps a job name's "-->" out (pre-QA r4 on #582)."""
    github.runs(ci="completed success", vdsm="completed success")
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs="in_progress\tnull\tx --> y\n",
        labels="Awaiting CI\n",
        prs_json=json.dumps([_pr(7, "Awaiting CI", fork=True)]),
    )
    done = github.run("pr-labels-recheck.sh")
    assert done.returncode == 0, done.stderr
    marker = next(line for line in github.writes() if "<!-- pr-labels-recheck abc" in line)
    body = marker.split("--body ", 1)[1]
    assert body.count("-->") == 1 and body.endswith("-->"), marker


@_NEEDS_JQ
@pytest.mark.parametrize("comments", ["", "<!-- pr-labels-codeql-failed abc -->\n"])
def test_ci_failed_from_codeql_says_so(github: _FakeGitHub, comments: str) -> None:
    """QA round 3 on #582: at Awaiting CI with no workflow job failed, CodeQL's failure moved the
    label with no word on the PR; it gets one comment per commit."""
    github.runs()
    github.answer(
        ci_jobs=_GOOD, vdsm_jobs=_GOOD, codeql="completed failure\n", labels="Awaiting CI\n",
        comments=comments,
    )  # fmt: skip
    done = github.run("pr-labels-settle.sh", "7", "abc")
    assert done.returncode == 0, done.stderr
    writes = "\n".join(github.writes())
    assert "--add-label CI Failed" in writes
    if comments:
        assert "pr comment" not in writes
    else:
        assert "<!-- pr-labels-codeql-failed abc -->" in writes
        assert "re-running CI won't change it" in writes
        assert "this comment isn't updated" in writes
        assert "an empty commit is expected to fail it again" in writes


@_NEEDS_JQ
def test_ci_failed_already_there_still_gets_the_codeql_comment(github: _FakeGitHub) -> None:
    """Pre-QA r4 on #582: a job failed (CI Failed), was re-run green, and CodeQL failed too:
    the label doesn't change, but the reason did."""
    github.runs()
    github.answer(
        ci_jobs=_GOOD, vdsm_jobs=_GOOD, codeql="completed failure\n", labels="CI Failed\n"
    )
    assert github.run("pr-labels-settle.sh", "7", "abc").returncode == 0
    writes = "\n".join(github.writes())
    assert "--add-label" not in writes
    assert "<!-- pr-labels-codeql-failed abc -->" in writes


@_NEEDS_JQ
def test_an_unreadable_comment_list_skips_the_codeql_comment_and_says_so(
    github: _FakeGitHub,
) -> None:
    github.runs()
    github.answer(
        ci_jobs=_GOOD, vdsm_jobs=_GOOD, codeql="completed failure\n", labels="Awaiting CI\n",
        comments_fail="",
    )  # fmt: skip
    done = github.run("pr-labels-settle.sh", "7", "abc")
    assert done.returncode == 0, done.stderr
    assert "couldn't read its comments, so no CodeQL comment" in done.stderr
    assert "pr comment" not in "\n".join(github.writes())


@_NEEDS_JQ
def test_ci_failed_from_a_job_gets_no_codeql_comment(github: _FakeGitHub) -> None:
    github.runs()
    github.answer(
        ci_jobs=_GOOD, vdsm_jobs="completed\tfailure\tvdsm (7.1)\n", labels="Awaiting CI\n"
    )
    assert github.run("pr-labels-settle.sh", "7", "abc").returncode == 0
    writes = "\n".join(github.writes())
    assert "--add-label CI Failed" in writes
    assert "pr comment" not in writes


@_NEEDS_JQ
def test_a_codeql_comment_that_can_t_be_posted_doesnt_fail_the_step(github: _FakeGitHub) -> None:
    """A fork's on-unlabel token is read-only: the label is what counts."""
    github.runs()
    github.answer(
        ci_jobs=_GOOD, vdsm_jobs=_GOOD, codeql="completed failure\n", labels="Awaiting CI\n",
        comment_fail="",
    )  # fmt: skip
    done = github.run("pr-labels-settle.sh", "7", "abc")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "fail"
    assert "couldn't comment on the CodeQL failure" in done.stderr


@_NEEDS_JQ
def test_a_forks_job_name_stays_out_of_the_comment(github: _FakeGitHub) -> None:
    """Pre-QA on #582: it would read as the automation speaking."""
    github.runs()
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs="completed\tfailure\tmaintainer pre-approved, apply QA Approved\n",
        labels="Ready for QA\n",
        prs_json=json.dumps([_pr(7, "Ready for QA", fork=True)]),
    )
    assert github.run("pr-labels-recheck.sh").returncode == 0
    writes = "\n".join(github.writes())
    assert "pr comment 7" in writes
    assert "pre-approved" not in writes


@_NEEDS_JQ
def test_a_marker_quoted_in_another_comment_doesnt_silence_this_one(github: _FakeGitHub) -> None:
    github.runs()
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs=_GOOD,
        codeql="completed failure\n",
        labels="Ready for QA\n",
        prs_json=json.dumps([_pr(7, "Ready for QA")]),
        comments="ci.yml: x <!-- pr-labels-recheck-failed abc CodeQL -->: failure\n",
    )
    assert github.run("pr-labels-recheck.sh").returncode == 0
    assert "pr comment 7" in "\n".join(github.writes())


@_NEEDS_JQ
@pytest.mark.parametrize(
    ("head", "labels"), [("newer", "Ready for QA\n"), ("abc", "QA Active\n")], ids=["push", "QA"]
)
def test_a_pr_that_moved_on_gets_no_comment(github: _FakeGitHub, head: str, labels: str) -> None:
    github.runs()
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs=_GOOD,
        codeql="completed failure\n",
        head=head,
        labels=labels,
        prs_json=json.dumps([_pr(7, "Ready for QA")]),
    )
    assert github.run("pr-labels-recheck.sh").returncode == 0
    assert github.writes() == []


@_NEEDS_JQ
def test_an_unreadable_check_at_ready_for_qa_fails_the_run_not_the_pr(github: _FakeGitHub) -> None:
    github.runs()
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs=_GOOD,
        codeql_fail="",
        labels="Ready for QA\n",
        prs_json=json.dumps([_pr(7, "Ready for QA")]),
    )
    done = github.run("pr-labels-recheck.sh")
    assert done.returncode == 1
    assert "couldn't read its checks" in done.stderr
    assert github.writes() == []


@_NEEDS_JQ
def test_a_failure_at_ready_for_qa_comments_once_per_commit(github: _FakeGitHub) -> None:
    github.runs()
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs=_GOOD,
        codeql="completed failure\n",
        labels="Ready for QA\n",
        prs_json=json.dumps([_pr(7, "Ready for QA")]),
        comments="<!-- pr-labels-recheck-failed abc CodeQL -->\n",
    )
    assert github.run("pr-labels-recheck.sh").returncode == 0
    assert github.writes() == []


@_NEEDS_JQ
def test_a_later_failure_on_one_commit_gets_its_own_comment(github: _FakeGitHub) -> None:
    """Pre-QA on #582: the state stops at its first failure, and CodeQL went unnamed for good."""
    github.runs()
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs=_GOOD,
        codeql="completed failure\n",
        labels="Ready for QA\n",
        prs_json=json.dumps([_pr(7, "Ready for QA")]),
        comments="<!-- pr-labels-recheck-failed abc ci.yml -->\n",
    )
    assert github.run("pr-labels-recheck.sh").returncode == 0
    assert "pr-labels-recheck-failed abc CodeQL" in "\n".join(github.writes())


@_NEEDS_JQ
def test_a_failed_re_read_at_ready_for_qa_fails_the_run(github: _FakeGitHub) -> None:
    github.runs()
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs=_GOOD,
        codeql="completed failure\n",
        labels="Ready for QA\n",
        prs_json=json.dumps([_pr(7, "Ready for QA")]),
        head_fail="",
    )
    done = github.run("pr-labels-recheck.sh")
    assert done.returncode == 1
    assert "couldn't read it again" in done.stderr
    assert github.writes() == []


@_NEEDS_JQ
def test_a_stuck_check_at_ready_for_qa_gets_no_comment(github: _FakeGitHub) -> None:
    """It's where a hand label would put it, so "apply Ready for QA" would make no sense."""
    github.runs()
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs=_GOOD,
        codeql="in_progress null\n",
        labels="Ready for QA\n",
        prs_json=json.dumps([_pr(7, "Ready for QA")]),
    )
    assert github.run("pr-labels-recheck.sh").returncode == 0
    assert github.writes() == []


@_NEEDS_JQ
def test_the_recheck_writes_nothing_on_a_passing_ready_for_qa(github: _FakeGitHub) -> None:
    github.runs()
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs=_GOOD,
        codeql="completed success\n",
        labels="Ready for QA\n",
        prs_json=json.dumps([_pr(7, "Ready for QA")]),
    )
    assert github.run("pr-labels-recheck.sh").returncode == 0
    assert github.writes() == []


@_GNU_DATE
def test_the_recheck_comment_says_what_clears_a_stuck_codeql(github: _FakeGitHub) -> None:
    github.runs(updated="2026-01-01T00:00:00Z")
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs=_GOOD,
        codeql="in_progress null\n",
        labels="Awaiting CI\n",
        prs="7\tabc\t0\n",
    )
    assert github.run("pr-labels-recheck.sh").returncode == 0
    comment = "\n".join(github.writes())
    assert "CodeQL: in_progress null" in comment
    assert "re-running CI won't clear it" in comment
    assert "re-run the workflow in Actions" not in comment
    assert "apply `Ready for QA` by hand" in comment


@_GNU_DATE
def test_the_recheck_comment_gives_both_when_codeql_and_a_job_are_missing(
    github: _FakeGitHub,
) -> None:
    github.runs(updated="2026-01-01T00:00:00Z")
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs="completed\tnone\tvdsm (7.1)\n",
        codeql="in_progress null\n",
        labels="Awaiting CI\n",
        prs="7\tabc\t0\n",
    )
    assert github.run("pr-labels-recheck.sh").returncode == 0
    comment = "\n".join(github.writes())
    assert "re-run the workflow in Actions" in comment
    assert "re-running CI won't clear it" in comment
    assert "gh pr checks 7" in comment


@_GNU_DATE
def test_the_recheck_comment_leaves_codeql_out_otherwise(github: _FakeGitHub) -> None:
    github.runs(updated="2026-01-01T00:00:00Z")
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs="completed\tnone\tvdsm (7.1)\n",
        labels="Awaiting CI\n",
        prs="7\tabc\t0\n",
    )
    assert github.run("pr-labels-recheck.sh").returncode == 0
    comment = "\n".join(github.writes())
    assert "pr-labels-recheck abc" in comment
    assert "CodeQL" not in comment


@_NEEDS_JQ
@pytest.mark.parametrize(
    ("pr", "expected"),
    [
        (_pr(7, "Awaiting CI"), "7\tabc\t0"),
        (_pr(7, "CI Failed"), "7\tabc\t0"),
        (_pr(7, "Awaiting CI", fork=True), "7\tabc\t0"),
        (_pr(7, "CI Failed", fork=True), "7\tabc\t0"),
        (_pr(7), "7\tabc\t1"),
        (_pr(7, "bug"), "7\tabc\t1"),
        (_pr(7, draft=True), None),
        (_pr(7, fork=True), None),
        (_pr(7, "Dev Active"), None),
        (_pr(7, "QA Active"), None),
        (_pr(7, "Ready for QA Signoff"), None),
        (_pr(7, "QA Approved"), None),
        (_pr(7, "Ready for QA"), "7\tabc\t2"),
        (_pr(7, "Ready for QA", fork=True), "7\tabc\t2"),
        (_pr(7, "CI Failed", "Ready for QA"), "7\tabc\t0"),
        (_pr(7, "QA Failed"), None),
    ],
    ids=[
        "awaiting CI",
        "CI failed, may be green again",
        "a fork on awaiting CI",
        "a fork on CI failed",
        "no labels",
        "another label only",
        "a draft",
        "a fork",
        "dev active",
        "QA active",
        "signoff",
        "approved",
        "ready for QA",
        "a fork on ready for QA",
        "both, still settled",
        "QA failed",
    ],
)
def test_which_prs_the_recheck_settles(
    github: _FakeGitHub, pr: dict[str, Any], expected: str | None
) -> None:
    """Pre-QA on #549: with the filter's output canned, nothing tested the filter."""
    github.runs(ci="in_progress null", vdsm="in_progress null")
    github.answer(prs_json=json.dumps([pr]), labels="")
    done = github.run("pr-labels-recheck.sh")
    assert done.returncode == 0, done.stderr
    if not expected:
        assert "PR #7" not in done.stderr
    elif expected.split(chr(9))[2] == "2":
        assert "PR #7 at abc (Ready for QA: report only)" in done.stderr
    else:
        assert f"PR #7 at abc (adopt: {expected.split(chr(9))[2]})" in done.stderr


def test_the_recheck_lists_only_prs_into_main() -> None:
    assert "--base main" in (_SCRIPTS / "pr-labels-recheck.sh").read_text(encoding="utf-8")


def test_a_fork_run_waiting_for_approval_isnt_a_failure(github: _FakeGitHub) -> None:
    """Pre-QA on #549: it reads completed, with no jobs, and went to CI Failed."""
    github.runs(ci="completed action_required", vdsm="completed action_required")
    github.answer(labels="Awaiting CI\n", prs="7\tabc\t0\n")
    assert github.run("pr-labels-recheck.sh").returncode == 0
    assert github.writes() == []


@pytest.mark.parametrize("label", ["QA Approved", "QA Failed", "Ready for QA Signoff"])
def test_adopting_goes_by_the_labels_now(github: _FakeGitHub, label: str) -> None:
    """Pre-QA on #549: the list is minutes old by the time a later PR's turn comes."""
    github.runs()
    github.answer(ci_jobs=_GOOD, vdsm_jobs=_GOOD, labels=f"{label}\n")
    assert github.run("pr-labels-settle.sh", "7", "abc", ADOPT="1").returncode == 0
    assert github.writes() == []


def test_settling_prints_only_the_state(github: _FakeGitHub) -> None:
    """Pre-QA on #549: `gh pr edit` prints the PR's URL, and the recheck captures stdout."""
    stub = github.folder / "gh"
    stub.write_text(
        stub.read_text(encoding="utf-8") + 'echo "https://github.com/o/r/pull/7"\n',
        encoding="utf-8",
    )
    github.runs()
    github.answer(ci_jobs="", vdsm_jobs="", labels="")
    done = github.run("pr-labels-settle.sh", "7", "abc", ADOPT="1")
    assert done.stdout.strip() == "pending"


@_GNU_DATE
def test_a_comment_that_fails_doesnt_stop_the_others(github: _FakeGitHub) -> None:
    """Pre-QA on #549: `set -e` ended the loop there."""
    github.runs()
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs="in_progress\tnone\tx\n",
        labels="Awaiting CI\n",
        prs="7\tabc\t0\n8\tabc\t0\n",
    )
    stub = github.folder / "gh"
    stub.write_text(
        stub.read_text(encoding="utf-8").replace(
            'case "$*" in', 'if [ "$1 $2 $3" = "pr comment 7" ]; then exit 1; fi\ncase "$*" in'
        ),
        encoding="utf-8",
    )
    done = github.run("pr-labels-recheck.sh")
    assert done.returncode == 1
    assert any(write.startswith("pr comment 8") for write in github.writes())


@_GNU_DATE
def test_comments_it_cant_read_arent_no_comments(github: _FakeGitHub) -> None:
    """Pre-QA on #549: it would have posted again at every run."""
    github.runs()
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs="in_progress\tnone\tx\n",
        labels="Awaiting CI\n",
        prs="7\tabc\t0\n",
        comments_fail="",
    )
    assert github.run("pr-labels-recheck.sh").returncode == 1
    assert not [write for write in github.writes() if write.startswith("pr comment")]


def test_only_this_jobs_comments_count_for_the_marker() -> None:
    """Pre-QA on #549: anyone could post the marker to silence it."""
    text = (_SCRIPTS / "pr-labels-recheck.sh").read_text(encoding="utf-8")
    assert 'test("^github-actions(\\\\[bot\\\\])?$")' in text


def test_the_check_ignores_only_the_report_job_and_other_events() -> None:
    text = (_SCRIPTS / "pr-checks-state.sh").read_text(encoding="utf-8")
    assert 'select(.name != "tool-quality")' in text
    assert "event=pull_request" in text


@pytest.mark.parametrize(
    ("workflow", "job"),
    [
        ("pr-labels.yml", "on-unlabel"),
        ("pr-labels-ci.yml", "on-checks-complete"),
        ("pr-labels-recheck.yml", "recheck"),
    ],
)
def test_each_job_checks_out_the_script_it_runs(workflow: str, job: str) -> None:
    steps = _workflows()[workflow]["jobs"][job]["steps"]
    assert "actions/checkout@" in steps[0].get("uses", ""), "no checkout first"
    assert steps[0]["with"]["persist-credentials"] is False
    run = steps[-1]["run"]
    named = re.findall(r"\.github/scripts/[\w.-]+", run)
    assert named
    for script in named:
        assert (GITHUB.parent / script).is_file(), script


def test_a_push_reads_the_labels_now_not_from_the_event() -> None:
    """#565: the event's stale list said Awaiting CI was there, after on-label had removed it."""
    step = _workflows()["pr-labels.yml"]["jobs"]["on-push"]["steps"][0]
    assert "PR_LABELS" not in step["env"]
    assert 'gh pr view "$PR" --repo "$REPO" --json labels' in step["run"]


@pytest.mark.parametrize("label", ["QA Active", "Ready for QA Signoff", "QA Approved", "QA Failed"])
def test_a_pr_past_ready_for_qa_isnt_sent_back(github: _FakeGitHub, label: str) -> None:
    """Pre-QA on #549: on Awaiting CI as well, the recheck added Ready for QA beside it, and these
    writes trigger nothing that would clean up."""
    github.runs()
    github.answer(ci_jobs=_GOOD, vdsm_jobs=_GOOD, labels=f"Awaiting CI\n{label}\n")
    assert github.run("pr-labels-settle.sh", "7", "abc").returncode == 0
    assert not [write for write in github.writes() if "--add-label Ready for QA" in write]


def test_dev_active_gets_no_comment(github: _FakeGitHub) -> None:
    """Pre-QA on #549: it told someone to apply Ready for QA while dev wasn't done."""
    github.runs()
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs="completed\tnone\tx\n",
        labels="Awaiting CI\nDev Active\n",
        prs="7\tabc\t0\n",
    )
    assert github.run("pr-labels-recheck.sh").returncode == 0
    assert github.writes() == []


@_GNU_DATE
def test_a_run_not_listed_beside_a_finished_one_is_named_in_the_comment(
    github: _FakeGitHub,
) -> None:
    """#339's own case."""
    github.answer(
        vdsm_run="2 completed success 2026-01-01T00:00:00Z\n",
        vdsm_jobs=_GOOD,
        labels="Awaiting CI\n",
        prs="7\tabc\t0\n",
    )
    assert github.run("pr-labels-recheck.sh").returncode == 0
    assert "ci.yml: no run listed" in " ".join(github.writes())


@_GNU_DATE
def test_the_latest_finished_run_decides_how_long_it_has_waited(github: _FakeGitHub) -> None:
    now = subprocess.run(
        ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True, text=True, check=True
    ).stdout.strip()
    github.answer(
        ci_run="1 completed success 2026-01-01T00:00:00Z\n",
        vdsm_run=f"2 completed success {now}\n",
        ci_jobs=_GOOD,
        vdsm_jobs="completed\tnone\tx\n",
        labels="Awaiting CI\n",
        prs="7\tabc\t0\n",
    )
    assert github.run("pr-labels-recheck.sh").returncode == 0
    assert not [write for write in github.writes() if write.startswith("pr comment")]


def test_a_failure_takes_ready_for_qa_away(github: _FakeGitHub) -> None:
    github.runs()
    github.answer(ci_jobs="completed\tfailure\tx\n", vdsm_jobs=_GOOD, labels="Ready for QA\n")
    assert github.run("pr-labels-settle.sh", "7", "abc").returncode == 0
    assert any("--remove-label Ready for QA" in write for write in github.writes())


def test_a_command_reading_stdin_cant_take_the_list_of_prs(github: _FakeGitHub) -> None:
    """Every PR still gets its turn if something in the loop reads stdin."""
    stub = github.folder / "gh"
    stub.write_text(
        stub.read_text(encoding="utf-8").replace(
            'case "$*" in',
            'if [ "$1" = pr ] && [ "$2" = view ]; then cat >/dev/null; fi\ncase "$*" in',
        ),
        encoding="utf-8",
    )
    github.runs()
    github.answer(
        ci_jobs=_GOOD, vdsm_jobs=_GOOD, labels="Awaiting CI\n", prs="7\tabc\t0\n8\tabc\t0\n"
    )
    done = subprocess.run(
        ["bash", str(_SCRIPTS / "pr-labels-recheck.sh")],
        env={
            **os.environ,
            "PATH": f"{github.folder}{os.pathsep}{os.environ['PATH']}",
            "REPO": "o/r",
            "RECHECK_SECONDS": "0",
        },
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    assert done.returncode == 0, done.stderr
    assert any(write.startswith("pr edit 8") for write in github.writes())


@pytest.mark.parametrize(
    ("vdsm_jobs", "added"),
    [("in_progress\tnone\tx\n", "Awaiting CI"), ("completed\tfailure\tx\n", "CI Failed")],
    ids=["pending", "fail"],
)
def test_a_stale_adopt_adds_nothing_beside_a_label_set_since(
    github: _FakeGitHub, vdsm_jobs: str, added: str
) -> None:
    """QA round 2 on #567: only a passing state was covered, where another check also stops it."""
    github.runs()
    github.answer(ci_jobs=_GOOD, vdsm_jobs=vdsm_jobs, labels="QA Active\n")
    assert github.run("pr-labels-settle.sh", "7", "abc", ADOPT="1").returncode == 0
    assert not [write for write in github.writes() if f"--add-label {added}" in write]


def test_a_job_that_appears_on_a_later_read_counts(github: _FakeGitHub) -> None:
    """#298: a run read completed before its jobs had results."""
    github.runs()
    github.answer(ci_jobs=_GOOD)
    stub = github.folder / "gh"
    jobs = github.answers / "vdsm_jobs"
    stub.write_text(
        stub.read_text(encoding="utf-8").replace(
            'case "$*" in',
            f"""case "$*" in
  *actions/runs/2/jobs*)
    if [ -f "{jobs}" ]; then cat "{jobs}"; else printf '{_GOOD}' > "{jobs}"; fi; exit 0 ;;
esac
case "$*" in""",
            1,
        ),
        encoding="utf-8",
    )
    assert github.run("pr-checks-state.sh").stdout.strip() == "pass"


def test_a_skipped_run_with_no_jobs_is_pending(github: _FakeGitHub) -> None:
    github.runs(ci="completed skipped")
    assert github.run("pr-checks-state.sh").stdout.strip() == "pending"


def test_a_time_it_cant_read_ends_the_run_red(github: _FakeGitHub) -> None:
    github.runs(updated="not-a-time")
    github.answer(
        ci_jobs=_GOOD,
        vdsm_jobs="completed\tnone\tx\n",
        labels="Awaiting CI\n",
        prs="7\tabc\t0\n",
    )
    assert github.run("pr-labels-recheck.sh").returncode == 1


def test_the_schedule_is_every_15_minutes() -> None:
    assert _workflows()["pr-labels-recheck.yml"][True]["schedule"] == [{"cron": "*/15 * * * *"}]


@pytest.mark.parametrize("label", ["Ready for QA Signoff", "QA Active", "QA Approved", "QA Failed"])
def test_dev_active_coming_off_doesnt_send_a_pr_back(github: _FakeGitHub, label: str) -> None:
    """Pre-QA on #567: on-unlabel added Ready for QA beside QA Approved, a pair that must never
    coexist, and these writes trigger nothing that would clean up."""
    github.runs()
    github.answer(ci_jobs=_GOOD, vdsm_jobs=_GOOD, labels=f"{label}\n")
    assert github.run("pr-labels-settle.sh", "7", "abc", ADOPT="1").returncode == 0
    assert github.writes() == []


def test_dev_active_coming_off_uses_the_shared_script() -> None:
    run = _workflows()["pr-labels.yml"]["jobs"]["on-unlabel"]["steps"][-1]["run"]
    assert 'ADOPT=1 bash .github/scripts/pr-labels-settle.sh "$PR" "$HEAD_SHA"' in run
