"""Guard tests for File Station's batch runner (#512): `filestation/batch.py`.

Two parts:

- **The scenario matrix**: every tool in ``BATCH_TOOLS`` (restore both to one folder and to the
  items' original folders) × how an item's task can end × the item it happens to (the first, the
  middle or the last of three), against a scripted DSM (`fake_dsm.py`). Each cell checks the
  runner's invariants (below) and compares the ones broken with ``KNOWN_GAPS``, the cells today's
  behavior is known to get wrong, each with the issue that fixes it. A fix that closes a gap fails
  here until its cell leaves the table; a change that opens one fails too.
- **The structural test**: no module but ``batch.py`` calls ``poll_task``, and none but it and
  ``tasks.py`` stops a task or writes a ``TaskRecord``, or sends a start, status read or stop
  named by a literal method (directly or through ``task_request``) other than Search's and
  DirSize's, named by a literal API.
"""

from __future__ import annotations

import ast
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.filestation import batch, operations
from mcp_synology.modules.filestation.batch import BATCH_TOOLS, BatchOutcome
from mcp_synology.modules.filestation.helpers import shown
from mcp_synology.modules.filestation.tasks import TaskRecords, parse_handle
from tests.modules.filestation.fake_dsm import (
    CANCEL,
    RUNNING,
    FakeClock,
    FakeDsm,
    PathScript,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import respx

    from mcp_synology.core.client import DsmClient

WAIT = 2.0


@dataclass(frozen=True)
class Tool:
    name: str
    kind: str
    sources: tuple[str, ...]
    dests: tuple[str | None, ...]
    remove_src: bool
    call: Callable[[DsmClient, TaskRecords], Awaitable[str]]

    @property
    def api(self) -> str:
        return "SYNO.FileStation.Delete" if self.kind == "delete" else "SYNO.FileStation.CopyMove"


def _delete(client: DsmClient, records: TaskRecords) -> Awaitable[str]:
    return operations.delete_files(
        client,
        paths=["/s/d0", "/s/d1", "/s/d2"],  # type: ignore[list-item]
        timeout=WAIT,
        records=records,
        recycle_bin_status={},
    )


def _copy(client: DsmClient, records: TaskRecords) -> Awaitable[str]:
    return operations.copy_files(
        client,
        paths=["/s/src/f0", "/s/src/f1", "/s/src/f2"],  # type: ignore[list-item]
        dest_folder="/s/dst",  # type: ignore[arg-type]
        timeout=WAIT,
        records=records,
    )


def _move(client: DsmClient, records: TaskRecords) -> Awaitable[str]:
    return operations.move_files(
        client,
        paths=["/s/src/f0", "/s/src/f1", "/s/src/f2"],  # type: ignore[list-item]
        dest_folder="/s/dst",  # type: ignore[arg-type]
        timeout=WAIT,
        records=records,
    )


def _restore_flat(client: DsmClient, records: TaskRecords) -> Awaitable[str]:
    return operations.restore_from_recycle_bin(
        client,
        share="/s",  # type: ignore[arg-type]
        paths=["#recycle/f0", "#recycle/f1", "#recycle/f2"],
        dest_folder="/s/dst",  # type: ignore[arg-type]
        timeout=WAIT,
        records=records,
    )


def _restore_origin(client: DsmClient, records: TaskRecords) -> Awaitable[str]:
    return operations.restore_from_recycle_bin(
        client,
        share="/s",  # type: ignore[arg-type]
        paths=["#recycle/o0/f0", "#recycle/o1/f1", "#recycle/o2/f2"],
        timeout=WAIT,
        records=records,
    )


_SRC = ("/s/src/f0", "/s/src/f1", "/s/src/f2")
_BIN = ("/s/#recycle/f0", "/s/#recycle/f1", "/s/#recycle/f2")
TOOLS = [
    Tool("delete_files", "delete", ("/s/d0", "/s/d1", "/s/d2"), (None,) * 3, False, _delete),
    Tool("copy_files", "copy", _SRC, ("/s/dst",) * 3, False, _copy),
    Tool("move_files", "move", _SRC, ("/s/dst",) * 3, True, _move),
    Tool("restore_flat", "restore", _BIN, ("/s/dst",) * 3, True, _restore_flat),
    Tool(
        "restore_origin",
        "restore",
        ("/s/#recycle/o0/f0", "/s/#recycle/o1/f1", "/s/#recycle/o2/f2"),
        ("/s/o0", "/s/o1", "/s/o2"),
        True,
        _restore_origin,
    ),
]


def _running(tool: Tool, i: int) -> dict[str, Any]:
    if tool.kind == "delete":
        return {"api": tool.api, "params": {"recursive": True}}
    return {
        "api": tool.api,
        "params": {
            "dest_folder_path": tool.dests[i],
            "remove_src": tool.remove_src,
            "overwrite": False,
        },
    }


# How the item's task ends, as a change to its script, and the state the runner gives it.
SCENARIOS: dict[str, tuple[Callable[[Tool, int, PathScript], None], str]] = {
    "finishes": (lambda t, i, s: None, "done"),
    "start_dsm_error": (lambda t, i, s: setattr(s, "start", 400), "failed"),
    "start_no_answer": (
        lambda t, i, s: setattr(s, "start", httpx.ConnectTimeout("no answer")),
        "may_have_started",
    ),
    "finished_with_error": (
        lambda t, i, s: setattr(
            s, "statuses", [{"finished": True, "errors": [{"code": 415, "path": t.sources[i]}]}]
        ),
        "failed",
    ),
    "poll_dsm_error": (lambda t, i, s: setattr(s, "statuses", [RUNNING, 400]), "failed"),
    "stall_transport": (
        lambda t, i, s: setattr(s, "statuses", [RUNNING, httpx.ReadTimeout("no answer")]),
        "handed_off",
    ),
    "stall_502": (
        lambda t, i, s: setattr(s, "statuses", [RUNNING, httpx.Response(502)]),
        "handed_off",
    ),
    "stall_non_dsm": (
        lambda t, i, s: setattr(
            s, "statuses", [RUNNING, httpx.Response(200, text="<html>Sign in</html>")]
        ),
        "handed_off",
    ),
    # A delete whose path is gone counts as done (#521); a copy, move or restore can't tell.
    "lost": (lambda t, i, s: setattr(s, "statuses", [599]), "lost"),
    # Lost, and the path is still there: a delete checks it (a bounded getinfo), then hands off.
    "lost_still_there": (
        lambda t, i, s: (setattr(s, "statuses", [599]), setattr(s, "gone_after", False)),
        "handed_off",
    ),
    # Lost, and the path check settles nothing (a 407): unknown isn't gone, so it's handed off.
    "lost_unknown": (
        lambda t, i, s: (setattr(s, "statuses", [599]), setattr(s, "code_when_lost", 407)),
        "handed_off",
    ),
    # Lost, and the path check itself fails: a DSM error, or no answer. Not gone either.
    "lost_check_dsm_error": (
        lambda t, i, s: (
            setattr(s, "statuses", [599]),
            setattr(
                s,
                "code_when_lost",
                httpx.Response(200, json={"success": False, "error": {"code": 400}}),
            ),
        ),
        "handed_off",
    ),
    "lost_check_no_answer": (
        lambda t, i, s: (
            setattr(s, "statuses", [599]),
            setattr(s, "code_when_lost", httpx.ReadTimeout("no answer")),
        ),
        "handed_off",
    ),
    "budget_out": (lambda t, i, s: setattr(s, "statuses", [RUNNING]), "handed_off"),
    "cancel_mid_poll": (lambda t, i, s: setattr(s, "statuses", [RUNNING, CANCEL]), "canceled"),
    "already_running": (lambda t, i, s: setattr(s, "running", _running(t, i)), "handed_off"),
    # Another call's task, started during the earlier items' wait, lands the item's name in its
    # destination with overwrite (#587): refused, mid-batch only. Copy, move and restore only.
    "landing_refused": (lambda t, i, s: None, "failed"),
    # DSM skipped it (the name is already there, overwrite=false): copy, move and restore only.
    "skipped": (
        lambda t, i, s: setattr(
            s, "statuses", [{"finished": True, "skipstatus": {"status": "all"}}]
        ),
        "done",
    ),
}

POSITIONS = {"first": 0, "middle": 1, "last": 2}

# Cells today's behavior gets wrong, by (tool, scenario, position): the invariants broken, each
# kept until the issue named fixes it. Restore's two forms share copy's and move's gaps.
_COPY_FAMILY = ("copy_files", "move_files", "restore_flat", "restore_origin")


def _gaps() -> dict[tuple[str, str, str], frozenset[str]]:
    gaps: dict[tuple[str, str, str], frozenset[str]] = {}
    for tool in _COPY_FAMILY:
        for scenario in ("start_dsm_error", "poll_dsm_error", "finished_with_error"):
            for position in ("first", "middle"):
                # #482: a failure doesn't list what wasn't started.
                gaps[(tool, scenario, position)] = frozenset({"names_not_started"})
        for scenario in ("start_dsm_error", "poll_dsm_error"):
            for position in ("middle", "last"):
                # #482: a DSM error on the start or a status read doesn't say what was done.
                gaps[(tool, scenario, position)] = gaps.get(
                    (tool, scenario, position), frozenset()
                ) | {"names_done"}
        for position in ("middle", "last"):
            # #452: a finished task's error takes the table's flag (415: retryable), though
            # earlier items were done and the same call again would redo them.
            gaps[(tool, "finished_with_error", position)] = gaps.get(
                (tool, "finished_with_error", position), frozenset()
            ) | {"retryable"}
    return gaps


KNOWN_GAPS = _gaps()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    fake.install(monkeypatch)
    return fake


def _scripts(tool: Tool, scenario: str, at: int) -> dict[str, PathScript]:
    scripts = {src: PathScript(gone_after=tool.kind == "delete") for src in tool.sources}
    change, _ = SCENARIOS[scenario]
    change(tool, at, scripts[tool.sources[at]])
    return scripts


def _expected_state(tool: Tool, scenario: str) -> str:
    state = SCENARIOS[scenario][1]
    if state == "lost":
        return "done" if tool.kind == "delete" else "handed_off"
    return state


def _cells() -> list[Any]:
    cells = []
    for tool in TOOLS:
        for scenario in SCENARIOS:
            if scenario in ("skipped", "landing_refused") and tool.kind == "delete":
                continue
            for position in POSITIONS:
                if scenario == "landing_refused" and position == "first":
                    continue  # refused before anything starts: test_operations' #587 tests
                cells.append(
                    pytest.param(tool, scenario, position, id=f"{tool.name}-{scenario}-{position}")
                )
    return cells


@pytest.mark.parametrize(("tool", "scenario", "position"), _cells())
async def test_matrix(
    mock_client: DsmClient,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.Router,
    tool: Tool,
    scenario: str,
    position: str,
) -> None:
    at = POSITIONS[position]
    dsm = FakeDsm(_scripts(tool, scenario, at))
    if scenario == "landing_refused":
        name = tool.sources[at].rsplit("/", 1)[-1]
        dsm.later_tasks.append(
            (
                at,
                {
                    "finished": False,
                    "taskid": "FileStation_Other",
                    "api": "SYNO.FileStation.CopyMove",
                    "params": {
                        "path": [f"/s/other/{name}"],
                        "dest_folder_path": tool.dests[at],
                        "overwrite": True,
                        "remove_src": False,
                    },
                },
            )
        )
    dsm.mount(respx_mock)
    captured: list[BatchOutcome] = []
    real = operations.run_batch

    async def spy(*args: Any, **kwargs: Any) -> BatchOutcome:
        outcome = await real(*args, **kwargs)
        captured.append(outcome)
        return outcome

    monkeypatch.setattr(operations, "run_batch", spy)
    records = TaskRecords()
    expected = _expected_state(tool, scenario)

    text = ""
    error: dict[str, Any] | None = None
    if expected == "canceled":
        with pytest.raises(asyncio.CancelledError):
            await tool.call(mock_client, records)
    else:
        try:
            text = await tool.call(mock_client, records)
        except ToolError as e:
            error = json.loads(str(e))["error"]
            text = error["message"] + " " + str(error.get("suggestion") or "")
        except httpx.TransportError:
            text = ""  # the server's usual error, raised as it came

    broken: set[str] = set()
    # Stops only on cancellation or a DSM error code, never after a hand-off (CLAUDE.md, #245).
    stops = len(dsm.stops())
    if stops != (1 if scenario in ("poll_dsm_error", "cancel_mid_poll") else 0):
        broken.add("stops")

    if expected == "canceled":
        assert broken == KNOWN_GAPS.get((tool.name, scenario, position), frozenset())
        return

    assert len(captured) == 1
    outcome = captured[0]
    states = [o.state for o in outcome.items]
    # Every item exactly once, in order.
    if [o.item.source for o in outcome.items] != list(tool.sources):
        broken.add("one_outcome_per_item")
    want = (
        ["done"] * 3
        if expected == "done"
        else ["done"] * at + [expected] + ["not_started"] * (2 - at)
    )
    if states != want:
        broken.add("states")

    last = outcome.last
    # A handle exactly when an item may still be running: and then a record for it.
    handles = [line for line in text.splitlines() if line.startswith("Handle: `")]
    if (last is not None and last.state == "handed_off") != bool(handles):
        broken.add("handle")
    for line in handles:
        handle = parse_handle(line.removeprefix("Handle: "))
        if records.get(handle) is None:
            broken.add("record")
    if last is not None and last.state == "handed_off":
        # No DSM request between the hand-off and the reply: the call that decided it (the
        # task's last status read, or the task list that showed it already running) is the last.
        # A lost delete's check of its own path (#521) comes after the status read, so it
        # decides too.
        handed = last.item.source
        deciding = [
            n
            for n, c in enumerate(dsm.calls)
            if (c.api == tool.api and c.method == "status")
            or (c.api == "SYNO.FileStation.BackgroundTask" and c.method == "list")
            or (
                tool.kind == "delete"
                and c.method == "getinfo"
                and c.params.get("path") == handed
                and n > 0
                and dsm.calls[n - 1].method == "status"
            )
        ]
        if not deciding or deciding[-1] != len(dsm.calls) - 1:
            broken.add("request_after_handoff")
        # A stall or a lost task says so; only a task still running at the wait's end reads
        # as running on.
        lines = text.splitlines()
        first = lines[lines.index(handles[0]) - 1] if handles else ""
        on_time = scenario in ("budget_out", "already_running")
        if on_time != first.startswith("Still "):
            broken.add("handoff_reason")
        if last.running is not None and len(dsm.starts()) != at:
            broken.add("started_a_second_task")

    if last is not None and text:
        # What was done, and what wasn't started, is named in the reply.
        done = [o.item.source for o in outcome.items if o.state == "done"]
        if done and not all(shown(p) in text for p in done):
            broken.add("names_done")
        rest = [x.source for x in outcome.not_started()]
        if rest and not all(shown(p) in text for p in rest):
            broken.add("names_not_started")
    if error is not None and outcome.items[0].state == "done" and error.get("retryable"):
        # Something ran: the same call again isn't the way to retry (#452).
        broken.add("retryable")

    assert broken == KNOWN_GAPS.get((tool.name, scenario, position), frozenset()), (
        broken,
        states,
        text,
    )


def test_known_gaps_name_real_cells() -> None:
    cells = {(p.values[0].name, p.values[1], p.values[2]) for p in _cells()}
    assert set(KNOWN_GAPS) <= cells


# --- structural ---

SRC = Path(operations.__file__).resolve().parents[3]
_BATCH_APIS = {"SYNO.FileStation.Delete", "SYNO.FileStation.CopyMove"}


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _modules() -> list[tuple[str, ast.Module]]:
    return [
        (path.relative_to(SRC).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(SRC.rglob("*.py"))
    ]


def test_only_the_runner_starts_polls_stops_and_records() -> None:
    rules = {
        "poll_task": {"mcp_synology/modules/filestation/batch.py"},
        "_stop_background_task": {
            "mcp_synology/modules/filestation/batch.py",
            "mcp_synology/modules/filestation/tasks.py",
        },
        "TaskRecord": {
            "mcp_synology/modules/filestation/batch.py",
            "mcp_synology/modules/filestation/tasks.py",
        },
    }
    found: list[str] = []
    for rel, tree in _modules():
        for call in _calls(tree):
            name = _name(call)
            allowed = rules.get(name)
            if allowed is not None and rel not in allowed:
                found.append(f"{rel}:{call.lineno} {name}(")
            # records.add(...): a task record written outside the runner and tasks.py.
            func = call.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "add"
                and "records" in ast.unparse(func.value)
                and rel not in rules["TaskRecord"]
            ):
                found.append(f"{rel}:{call.lineno} records.add(")
            # A record added under any name: `<anything>.add(TaskRecord(...))`.
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "add"
                and any(isinstance(a, ast.Call) and _name(a) == "TaskRecord" for a in call.args)
                and rel not in rules["TaskRecord"]
            ):
                found.append(f"{rel}:{call.lineno} .add(TaskRecord(")
            # A task's start, status read or stop, positionally or by keyword: outside the runner
            # and tasks.py, only Search's and DirSize's, named by a literal API. An API in a
            # variable, an attribute or a constant could be Delete's or CopyMove's.
            # `task_request(client, api, method, …)`, Search's and DirSize's helper, takes them
            # after the client.
            positional = call.args[1:] if name == "task_request" else call.args
            given = dict(zip(("api", "method"), positional, strict=False))
            given.update({k.arg: k.value for k in call.keywords if k.arg in ("api", "method")})
            api, method = given.get("api"), given.get("method")
            if (
                isinstance(method, ast.Constant)
                and method.value in ("start", "status", "stop")
                and rel not in rules["_stop_background_task"]
                and not (
                    isinstance(api, ast.Constant)
                    and isinstance(api.value, str)
                    and api.value not in _BATCH_APIS
                )
            ):
                found.append(
                    f"{rel}:{call.lineno} {ast.unparse(api) if api else '?'} {method.value}"
                )
            # getattr(module, "poll_task") would hide a call too.
            if (
                name == "getattr"
                and len(call.args) >= 2
                and isinstance(call.args[1], ast.Constant)
                and call.args[1].value in rules
                and rel not in rules[call.args[1].value]
            ):
                found.append(f"{rel}:{call.lineno} getattr {call.args[1].value}")
        # An import under another name would hide a call from the rules above.
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    base = alias.name.rsplit(".", 1)[-1]
                    if base in rules and alias.asname not in (None, base):
                        found.append(f"{rel}:{node.lineno} {base} imported as {alias.asname}")
    assert found == [], "\n".join(found)


def test_the_runner_sends_the_start() -> None:
    tree = ast.parse(Path(batch.__file__).read_text())
    assert any(
        len(c.args) >= 2 and isinstance(c.args[1], ast.Constant) and c.args[1].value == "start"
        for c in _calls(tree)
    )


def test_every_tool_reaching_the_runner_is_in_the_matrix() -> None:
    public: set[str] = set()
    for _, tree in _modules():
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        }

        def reaches(name: str, seen: frozenset[str], functions: Any = functions) -> bool:
            node = functions.get(name)
            if node is None or name in seen:
                return False
            names = {_name(c) for c in _calls(node)}
            return "run_batch" in names or any(reaches(n, seen | {name}) for n in names)

        public |= {
            name
            for name in functions
            if not name.startswith("_") and name != "run_batch" and reaches(name, frozenset())
        }
    assert public == set(BATCH_TOOLS)
    assert {t.name for t in TOOLS} >= set(BATCH_TOOLS) - {"restore_from_recycle_bin"}
