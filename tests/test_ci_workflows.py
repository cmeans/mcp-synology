"""Guards on the CI workflows."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator

GITHUB = Path(__file__).resolve().parents[1] / ".github"
_COMMENT_LINE = re.compile(r"^\s*#.*$", re.MULTILINE)
_CONTINUATION = re.compile(r"\\\n\s*")
_OPERATOR = set("();<>|&")


def _commands(line: str) -> Iterator[list[str]]:
    """A line of shell as commands, each a list of words: quotes and comments are the shell's,
    not a regex's (pre-QA r2 on #544)."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.commenters = "#"
    lexer.whitespace_split = True
    command: list[str] = []
    for token in lexer:
        if set(token) <= _OPERATOR:
            yield command
            command = []
        else:
            command.append(token)
    yield command


def _is_sync(command: list[str]) -> bool:
    names = [word.rsplit("/", 1)[-1] for word in command]  # `/usr/bin/uv` is `uv` too
    if "uv" not in names:
        return False
    after_uv = command[names.index("uv") + 1 :]
    return "sync" in after_uv and "run" not in after_uv[: after_uv.index("sync")]


def _syncs_in(script: str) -> list[list[str]]:
    """Each command in a `run:` script that runs `uv sync`. By command, not by line: a continued
    line hides the sync, and another command's `--locked` isn't this one's (QA round 1 on
    #544). Not seen: a variable holding `uv` (`"$UV" sync`), a sync in backticks or a quoted
    `bash -c`, and one `uv run` starts. This keeps the workflows honest; it isn't a security
    boundary."""
    joined = _CONTINUATION.sub(" ", _COMMENT_LINE.sub("", script))
    return [
        command
        for line in joined.splitlines()
        if "uv" in line and "sync" in line  # the only lines it has to tokenize
        for command in _commands(line)
        if _is_sync(command)
    ]


def _is_locked(command: list[str]) -> bool:
    """The last of `--locked` and `--no-locked` wins; after `--`, words aren't uv's options."""
    words = command[: command.index("--")] if "--" in command else command
    flags = [word for word in words if word in ("--locked", "--no-locked")]
    return bool(flags) and flags[-1] == "--locked"


def _run_scripts(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "run" and isinstance(value, str):
                yield value
            else:
                yield from _run_scripts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _run_scripts(value)


def _uv_syncs() -> list[tuple[str, list[str]]]:
    """The `uv sync` commands in a workflow's `run:` steps that `_syncs_in` sees, with the
    file's name."""
    return [
        (path.name, sync)
        for path in sorted(GITHUB.rglob("*.y*ml"))
        for script in _run_scripts(yaml.safe_load(path.read_text(encoding="utf-8")))
        for sync in _syncs_in(script)
    ]


def test_every_uv_sync_refuses_a_stale_lock() -> None:
    """#115: without `--locked`, a PR that changes a dependency and forgets `uv lock` passes,
    tested on a lock nobody committed, and the sync rewrites `uv.lock` on the runner."""
    syncs = _uv_syncs()
    assert syncs, "no uv sync found: the workflows moved, so this guard checks nothing"
    assert [(name, " ".join(sync)) for name, sync in syncs if not _is_locked(sync)] == []


@pytest.mark.parametrize(
    "script",
    [
        "uv sync --extra dev",
        "uv sync --extra dev&&echo --locked",
        "uv sync --extra dev || echo --locked",
        "uv sync --extra dev; echo --locked",
        "uv sync --extra dev | tee --locked",
        "uv sync --extra dev & echo --locked",
        "uv sync --extra dev > --locked",
        "uv sync --extra dev < --locked",
        "uv sync --locked,x",
        "(uv sync --extra dev) --locked",
        "uv sync --extra dev -- --locked",
        "uv sync --no-locked",
        "uv sync --locked --no-locked",
        "uv sync --locked-foo",
        "uv sync --extra dev  # --locked",
        "uv sync --extra dev\t# --locked",
        'echo " #" && uv sync --extra dev',
        "# a note \\\nuv sync --extra dev",
        "uv \\\n    sync --extra dev",
        "uv --directory . sync --extra dev",
        "uv --python 3.12 sync",
        "/usr/bin/uv sync --extra dev",
        "./uv sync --extra dev",
        '"$HOME/.local/bin/uv" sync',
    ],
)
def test_the_guard_catches_a_sync_without_the_flag(script: str) -> None:
    syncs = _syncs_in(script)
    assert syncs
    assert not all(_is_locked(sync) for sync in syncs)


@pytest.mark.parametrize(
    ("script", "count"),
    [
        ("uv sync --locked --extra dev", 1),
        ("uv sync --extra dev --locked && uv run pytest", 1),
        ("uv sync \\\n    --locked \\\n    --extra dev", 1),
        ("uv --quiet sync --locked", 1),
        ("(uv sync --locked)", 1),
        ("uv sync --no-locked --locked", 1),
        ("# uv sync", 0),
        ("uv run sync", 0),
        ("uv run pytest", 0),
    ],
)
def test_the_guard_passes_a_locked_sync(script: str, count: int) -> None:
    syncs = _syncs_in(script)
    assert len(syncs) == count
    assert all(_is_locked(sync) for sync in syncs)


def test_the_guard_reads_run_steps_after_yaml() -> None:
    """A step's name isn't a sync; quotes and folding are YAML's (pre-QA r2 on #544)."""
    workflow = """
    steps:
      - name: uv sync
      - run: "uv sync --locked"
      - run: >-
          uv sync
          --locked
    """
    scripts = list(_run_scripts(yaml.safe_load(workflow)))
    syncs = [sync for script in scripts for sync in _syncs_in(script)]
    assert len(syncs) == 2
    assert all(_is_locked(sync) for sync in syncs)


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
