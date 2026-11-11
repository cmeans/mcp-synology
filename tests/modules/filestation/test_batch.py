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
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import DsmReplyError, RequestTooLongError
from mcp_synology.modules.filestation import batch, operations, tasks
from mcp_synology.modules.filestation.batch import BATCH_TOOLS, BatchOutcome
from mcp_synology.modules.filestation.helpers import shown
from mcp_synology.modules.filestation.tasks import TaskRecords, parse_handle
from tests.modules.filestation.fake_dsm import (
    CANCEL,
    FINISHED,
    HOLD,
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
    # 402 and 421 are retryable by their own flag (#490), but the task had started: not
    # retryable (the maintainer's Q5 on #512; pre-QA r1 on #512 PR 3).
    "poll_busy_error": (lambda t, i, s: setattr(s, "statuses", [RUNNING, 402]), "failed"),
    "finished_with_busy_error": (
        lambda t, i, s: setattr(
            s, "statuses", [{"finished": True, "errors": [{"code": 421, "path": t.sources[i]}]}]
        ),
        "failed",
    ),
    # The stop sent after that error isn't confirmed: the task may still run, so the failure
    # gives its handle and file_task knows it (#497).
    "poll_dsm_error_stop_unanswered": (
        lambda t, i, s: (
            setattr(s, "statuses", [RUNNING, 400]),
            setattr(s, "stop", httpx.ReadTimeout("no answer")),
        ),
        "failed",
    ),
    "poll_dsm_error_stop_refused": (
        lambda t, i, s: (setattr(s, "statuses", [RUNNING, 400]), setattr(s, "stop", 400)),
        "failed",
    ),
    # DSM answers the stop that it has no such task (401): that doesn't show it ended (#521), so
    # it's refused like any other, with a handle.
    "poll_dsm_error_stop_gone": (
        lambda t, i, s: (setattr(s, "statuses", [RUNNING, 400]), setattr(s, "stop", 401)),
        "failed",
    ),
    "poll_dsm_error_stop_not_sent": (
        lambda t, i, s: (
            setattr(s, "statuses", [RUNNING, 400]),
            setattr(s, "stop", httpx.ConnectError("refused")),
        ),
        "failed",
    ),
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
_POLL_ERRORS = (
    "poll_dsm_error",
    "poll_busy_error",
    "poll_dsm_error_stop_unanswered",
    "poll_dsm_error_stop_refused",
    "poll_dsm_error_stop_not_sent",
    "poll_dsm_error_stop_gone",
)
# The scenarios where the item's own task started before it failed: never retryable (Q5).
_STARTED = (*_POLL_ERRORS, "finished_with_error", "finished_with_busy_error")
# The poll errors whose task may still run: a stop DSM didn't confirm and didn't say was moot.
_MAY_RUN = (
    "poll_dsm_error_stop_unanswered",
    "poll_dsm_error_stop_refused",
    "poll_dsm_error_stop_not_sent",
    "poll_dsm_error_stop_gone",
)


def _gaps() -> dict[tuple[str, str, str], frozenset[str]]:
    """The cells known to break an invariant, each with the issue that fixes it. None today:
    #512 PR 3's one failure formatter closed #482's (what was done and not started) and #452's
    (retryable after items ran) for copy, move and restore."""
    return {}


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
    if stops != (1 if scenario in (*_POLL_ERRORS, "cancel_mid_poll") else 0):
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
    # A handle exactly when an item may still be running: handed off, or failed with a stop DSM
    # didn't confirm (#497); and then a record for it, with a stop note when the stop may have
    # reached DSM (#660).
    handles = [line for line in text.splitlines() if line.startswith("Handle: `")]
    if error is not None and error.get("handle"):
        handles.append(f"Handle: `{error['handle']}`")
    may_run = last is not None and (last.state == "handed_off" or scenario in _MAY_RUN)
    if may_run != bool(handles):
        broken.add("handle")
    for line in handles:
        handle = parse_handle(line.removeprefix("Handle: "))
        record = records.get(handle)
        if record is None:
            broken.add("record")
        elif (record.stop is not None) != (scenario == "poll_dsm_error_stop_unanswered"):
            broken.add("stop_note")
    if may_run and error is not None and error.get("retryable"):
        # Its task may still change things: running the call again now could meet it.
        broken.add("retryable")
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
    # From the scenario, not the runner's own flag: the item's task started (or may have).
    ran = outcome.items[0].state == "done" or (
        last is not None and (scenario in _STARTED or last.state == "may_have_started")
    )
    if error is not None and ran and error.get("retryable"):
        # Something ran, an earlier item or this one's own task: the same call again isn't the
        # way to retry (#452; the maintainer's Q5 on #512).
        broken.add("retryable")

    assert broken == KNOWN_GAPS.get((tool.name, scenario, position), frozenset()), (
        broken,
        states,
        text,
    )


@pytest.mark.parametrize("tool", TOOLS, ids=[t.name for t in TOOLS])
async def test_the_wait_is_the_calls_not_each_items(
    mock_client: DsmClient, clock: FakeClock, respx_mock: respx.Router, tool: Tool
) -> None:
    """#496: the first item runs for most of the wait and finishes; the second, still running,
    is handed off when the call's time is up, not after a wait of its own, and the third isn't
    started. The call's waiting stays within the setting (and the least an item gets)."""
    scripts = {src: PathScript(gone_after=tool.kind == "delete") for src in tool.sources}
    slow = int((WAIT * 0.75) / 0.5)  # status reads at poll_task's 0.5 s interval
    scripts[tool.sources[0]].statuses = [RUNNING] * slow + [dict(FINISHED)]
    scripts[tool.sources[1]].statuses = [RUNNING]
    FakeDsm(scripts).mount(respx_mock)
    text = await tool.call(mock_client, TaskRecords())
    assert "Handle: `" in text, text
    assert shown(tool.sources[2]) in text  # not started, and said so
    assert clock.now <= WAIT + batch.MIN_ITEM_WAIT, clock.now


@pytest.mark.parametrize("how", ["task", "anyio"])
@pytest.mark.parametrize("stop", ["ok", 401, 400, httpx.ReadTimeout("no answer")])
@pytest.mark.parametrize("tool", TOOLS, ids=[t.name for t in TOOLS])
async def test_a_call_canceled_during_a_start_stops_what_it_started(
    mock_client: DsmClient,
    clock: FakeClock,
    respx_mock: respx.Router,
    tool: Tool,
    stop: Any,
    how: str,
) -> None:
    """#394 C3: the call is canceled while DSM is still answering a start. The start's answer
    is awaited anyway, the task it started is stopped, and unless DSM confirmed that, the task
    is recorded (noted as stopped-but-unconfirmed when the stop may have reached DSM), so no
    task is left running that nothing knows of."""
    scripts = {src: PathScript(gone_after=tool.kind == "delete") for src in tool.sources}
    scripts[tool.sources[1]].start = HOLD
    scripts[tool.sources[1]].stop = stop
    dsm = FakeDsm(scripts)
    dsm.mount(respx_mock)
    records = TaskRecords()
    if how == "task":
        call = asyncio.ensure_future(tool.call(mock_client, records))
        await asyncio.wait_for(dsm.holding.wait(), 5)
        call.cancel()
        await asyncio.sleep(0)
        dsm.release.set()
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        # As the MCP SDK cancels: a level-triggered anyio scope, so an unshielded cleanup's
        # first await would be canceled again at once.
        async def run() -> None:
            await tool.call(mock_client, records)

        async with anyio.create_task_group() as group:
            group.start_soon(run)
            await asyncio.wait_for(dsm.holding.wait(), 5)
            asyncio.get_running_loop().call_later(0.05, dsm.release.set)
            group.cancel_scope.cancel()
    assert len(dsm.starts()) == 2  # the third was never started
    stops = dsm.stops()
    assert len(stops) == 1, dsm.methods()
    taskid = stops[0].params["taskid"].strip('"')
    record = records.get(parse_handle(f"{tool.kind}:{taskid}"))
    if stop == "ok":
        assert record is None  # stopped, as DSM confirmed: nothing to follow up
    else:
        assert record is not None and record.paths == [tool.sources[1]]
        assert (record.stop is not None) == isinstance(stop, httpx.ReadTimeout)


@pytest.mark.parametrize(
    ("stop", "says", "handle"),
    [
        ("ok", "DSM confirmed the stop of /s/d0's task", False),
        (
            httpx.ReadTimeout("x"),
            "a stop was sent for /s/d0's task, and DSM hasn't confirmed it",
            True,
        ),
        (400, "DSM refused the stop of /s/d0's task, so it may still be running", True),
        (401, "DSM refused the stop of /s/d0's task, so it may still be running", True),
        (httpx.ConnectError("x"), "the stop for /s/d0's task couldn't be sent", True),
    ],
)
async def test_a_poll_errors_reply_says_how_the_stop_ended(
    mock_client: DsmClient,
    clock: FakeClock,
    respx_mock: respx.Router,
    stop: Any,
    says: str,
    handle: bool,
) -> None:
    """#497: only a stop DSM confirmed is called one; otherwise the error gives the handle and
    the follow-up, and isn't retryable."""
    tool = TOOLS[0]
    scripts = {src: PathScript() for src in tool.sources}
    # 421 (in use) is retryable by its own flag (#490): the unconfirmed stop must override it.
    scripts[tool.sources[0]].statuses = [RUNNING, 421]
    scripts[tool.sources[0]].stop = stop
    FakeDsm(scripts).mount(respx_mock)
    with pytest.raises(ToolError) as raised:
        await tool.call(mock_client, TaskRecords())
    error = json.loads(str(raised.value))["error"]
    assert says in error["message"], error["message"]
    assert bool(error.get("handle")) == handle
    if handle:
        assert error["retryable"] is False
        assert f"file_task(handle={error['handle']!r})" in error["suggestion"]


@pytest.mark.parametrize("tool", TOOLS, ids=[t.name for t in TOOLS])
async def test_no_item_starts_once_the_calls_wait_is_over(
    mock_client: DsmClient, clock: FakeClock, respx_mock: respx.Router, tool: Tool
) -> None:
    """#496: the first item takes 1.5 s of the 2 s wait; the second, started with 0.5 s left,
    gets its least wait and finishes at 2 s. The call's wait is then over, so the third isn't
    started: the reply says so, names nothing as running, and names only what ran as done."""
    scripts = {src: PathScript(gone_after=tool.kind == "delete") for src in tool.sources}
    scripts[tool.sources[0]].statuses = [RUNNING, RUNNING, RUNNING, dict(FINISHED)]
    scripts[tool.sources[1]].statuses = [RUNNING, dict(FINISHED)]
    dsm = FakeDsm(scripts)
    dsm.mount(respx_mock)
    text = await tool.call(mock_client, TaskRecords())
    assert len(dsm.starts()) == 2, dsm.methods()
    assert clock.now <= WAIT + batch.MIN_ITEM_WAIT, clock.now
    setting = "delete_timeout" if tool.kind == "delete" else "copy_move_timeout"
    assert text.startswith(f"[!] This call's wait (2 s, {setting}) ran out after 2 of 3"), text
    assert text.endswith(f"Not started: {shown(tool.sources[2])}. Run it in another call."), text
    assert "Handle: `" not in text  # nothing is running
    # What wasn't started isn't named as done, nor its folder as emptied.
    done_part = text[: text.index("Not started:")]
    assert tool.sources[2].rsplit("/", 1)[-1] not in done_part, text
    assert "have been removed from" not in text, text


@pytest.mark.parametrize("tool", TOOLS, ids=[t.name for t in TOOLS])
async def test_no_item_starts_when_the_checks_before_it_use_up_the_wait(
    mock_client: DsmClient,
    clock: FakeClock,
    respx_mock: respx.Router,
    tool: Tool,
) -> None:
    """QA round 1 on #686: the BackgroundTask list checked before the second item's start takes
    the rest of the wait: that item isn't started, and the reply says the wait ran out."""
    scripts = {src: PathScript(gone_after=tool.kind == "delete") for src in tool.sources}

    class SlowList(FakeDsm):
        ran_slow = False

        def _answer(self, request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if request.content:
                params.update(dict(httpx.QueryParams(request.content.decode())))
            listing = params.get("api") == "SYNO.FileStation.BackgroundTask"
            if listing and len(self.starts()) == 1 and not self.ran_slow:
                self.ran_slow = True
                clock.now += WAIT  # the list took the rest of the wait
            return super()._answer(request)

    dsm = SlowList(scripts)
    dsm.mount(respx_mock)
    text = await tool.call(mock_client, TaskRecords())
    assert len(dsm.starts()) == 1, dsm.methods()
    assert "ran out after 1 of 3" in text, text
    assert "Handle: `" not in text


async def test_a_restores_recreated_folder_note_comes_before_not_started(
    mock_client: DsmClient,
    clock: FakeClock,
    respx_mock: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-QA r3 on #686: a restore to several places cut short by the wait keeps its
    ``Not started:`` line last, as the first line's "(listed last)" says."""
    tool = next(t for t in TOOLS if t.name == "restore_origin")
    scripts = {src: PathScript() for src in tool.sources}

    class SlowList(FakeDsm):
        ran_slow = False

        def _answer(self, request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if request.content:
                params.update(dict(httpx.QueryParams(request.content.decode())))
            listing = params.get("api") == "SYNO.FileStation.BackgroundTask"
            if listing and len(self.starts()) == 1 and not self.ran_slow:
                self.ran_slow = True
                clock.now += WAIT
            return super()._answer(request)

    SlowList(scripts).mount(respx_mock)

    async def recreated(client: Any, destinations: Any) -> tuple[list[str], list[str]]:
        return ["/s/o2"], []

    monkeypatch.setattr(operations, "_recreate_missing_folders", recreated)
    text = await tool.call(mock_client, TaskRecords())
    assert "Re-created /s/o2 (no longer there)" in text, text
    assert text.rstrip().splitlines()[-1].startswith("Not started:"), text


async def test_an_item_started_near_the_end_still_gets_its_least_wait(
    mock_client: DsmClient, clock: FakeClock, respx_mock: respx.Router
) -> None:
    """``MIN_ITEM_WAIT``: the second item starts with 0.5 s of the call's wait left and finishes
    on its second read, 0.5 s later, inside its least wait: it's done, not handed off."""
    tool = TOOLS[0]
    scripts = {src: PathScript() for src in tool.sources}
    scripts[tool.sources[0]].statuses = [RUNNING, RUNNING, RUNNING, dict(FINISHED)]
    scripts[tool.sources[1]].statuses = [RUNNING, dict(FINISHED)]
    FakeDsm(scripts).mount(respx_mock)
    text = await tool.call(mock_client, TaskRecords())
    assert "Handle: `" not in text, text
    assert shown(tool.sources[1]) in text


async def test_a_start_still_unanswered_after_a_cancel_is_given_up(
    mock_client: DsmClient,
    clock: FakeClock,
    respx_mock: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cancel's wait for the start's answer is bounded (``STOP_WAIT``): past it, the start
    is given up and logged (no task ID, so nothing can be stopped), and nothing is recorded."""
    monkeypatch.setattr(batch, "STOP_WAIT", 0.05)
    tool = TOOLS[0]
    scripts = {src: PathScript() for src in tool.sources}
    scripts[tool.sources[0]].start = HOLD
    dsm = FakeDsm(scripts)
    dsm.mount(respx_mock)
    records = TaskRecords()
    call = asyncio.ensure_future(tool.call(mock_client, records))
    await asyncio.wait_for(dsm.holding.wait(), 5)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert dsm.stops() == []
    assert "unanswered after a cancel" in caplog.text


@pytest.mark.parametrize("after", [400, httpx.ConnectError("x")])
async def test_a_start_that_fails_after_a_cancel_stops_nothing(
    mock_client: DsmClient, clock: FakeClock, respx_mock: respx.Router, after: Any
) -> None:
    """A start that answers with a DSM error, or fails, while the call is being canceled
    started no task: nothing is stopped or recorded."""
    tool = TOOLS[0]
    scripts = {src: PathScript() for src in tool.sources}
    scripts[tool.sources[0]].start = HOLD
    dsm = FakeDsm(scripts)
    dsm.after_hold = after
    dsm.mount(respx_mock)
    records = TaskRecords()
    call = asyncio.ensure_future(tool.call(mock_client, records))
    await asyncio.wait_for(dsm.holding.wait(), 5)
    call.cancel()
    await asyncio.sleep(0)
    dsm.release.set()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert dsm.stops() == []


@pytest.mark.parametrize("stop", [httpx.ReadTimeout("no answer"), 400, "ok", 401])
async def test_a_cancel_mid_poll_records_a_task_that_may_still_run(
    mock_client: DsmClient, clock: FakeClock, respx_mock: respx.Router, stop: Any
) -> None:
    """A canceled call's stop that DSM didn't confirm leaves the task recorded (noted when
    the stop may have reached DSM), as a cancel during the start does (#497)."""
    tool = TOOLS[0]
    scripts = {src: PathScript() for src in tool.sources}
    scripts[tool.sources[0]].statuses = [RUNNING, CANCEL]
    scripts[tool.sources[0]].stop = stop
    dsm = FakeDsm(scripts)
    dsm.mount(respx_mock)
    records = TaskRecords()
    with pytest.raises(asyncio.CancelledError):
        await tool.call(mock_client, records)
    taskid = dsm.stops()[0].params["taskid"].strip('"')
    record = records.get(parse_handle(f"delete:{taskid}"))
    if stop == "ok":
        assert record is None
    else:
        assert record is not None
        assert (record.stop is not None) == isinstance(stop, httpx.ReadTimeout)


def test_a_path_ending_in_a_dot_keeps_it() -> None:
    """Only the message's own period goes: `/s/report.` stays `/s/report.` (pre-QA on #512 PR 2)."""
    error = ToolError(
        json.dumps(
            {
                "error": {
                    "message": "Failed on: /s/d0. Not started: /s/report..",
                    "suggestion": "Check /s/d0.",
                }
            }
        )
    )
    outcome = batch.ItemOutcome(
        batch.Item("/s/d0", None), "failed", stop=tasks.StopResult.CONFIRMED
    )
    message = json.loads(str(batch._with_stop(error, outcome)))["error"]["message"]
    assert message == (
        "Failed on: /s/d0. Not started: /s/report.; DSM confirmed the stop of /s/d0's task."
    )


def test_an_error_that_isnt_an_envelope_is_left_as_it_is() -> None:
    error = ToolError("plain text")
    outcome = batch.ItemOutcome(
        batch.Item("/s/a", None), "failed", stop=tasks.StopResult.UNANSWERED
    )
    assert batch._with_stop(error, outcome) is error


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (RequestTooLongError("too long"), "not_sent"),
        (RuntimeError("odd"), "unanswered"),
        (DsmReplyError("not DSM"), "unanswered"),
    ],
)
async def test_stop_results_for_errors_outside_dsm(raised: Exception, expected: str) -> None:
    class Client:
        async def request(self, *args: Any, **kwargs: Any) -> Any:
            raise raised

    result = await tasks._stop_background_task(
        Client(),  # type: ignore[arg-type]
        "SYNO.FileStation.Delete",
        '"t"',
        2,
        logging.getLogger("test"),
    )
    assert result is tasks.StopResult(expected)


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


@pytest.mark.parametrize("tool", TOOLS, ids=[t.name for t in TOOLS])
async def test_the_task_list_before_a_start_waits_at_most_whats_left(
    mock_client: DsmClient,
    clock: FakeClock,
    respx_mock: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
    tool: Tool,
) -> None:
    """#512 PR 3: the BackgroundTask list read before each start gets what's left of the call's
    wait (at least ``MIN_CHECK_WAIT``), not the client's default timeout."""
    scripts = {src: PathScript(gone_after=tool.kind == "delete") for src in tool.sources}
    for script in scripts.values():
        script.statuses = [RUNNING, FINISHED]  # each item's polling takes some of the wait
    FakeDsm(scripts).mount(respx_mock)
    real = mock_client.request
    waits: list[float | None] = []

    async def spy(api: str, method: str, *args: Any, **kwargs: Any) -> Any:
        if api == "SYNO.FileStation.BackgroundTask":
            waits.append(kwargs.get("read_timeout"))
        return await real(api, method, *args, **kwargs)

    monkeypatch.setattr(mock_client, "request", spy)
    await tool.call(mock_client, TaskRecords())
    if tool.kind == "restore":
        # A restore's check before anything starts (and before the call's wait begins) isn't
        # bounded, as the spec says; then one read per item.
        assert waits[0] is None, waits
        waits = waits[1:]
    assert len(waits) == 3, waits
    assert all(w is not None and batch.MIN_CHECK_WAIT <= w <= WAIT for w in waits), waits
    # What's left, not the whole wait each time: it shrinks as the items run (pre-QA r4).
    assert waits[0] == WAIT, waits
    assert waits[0] > waits[1] > waits[2], waits


async def test_bounded_summary_falls_back_without_checks() -> None:
    """Out of time, the summary is the one made without reading the NAS (#512 PR 3)."""
    made: list[bool] = []

    async def make(probe: bool) -> str:
        made.append(probe)
        if probe:
            await asyncio.sleep(30)
        return f"probe={probe}"

    assert await batch.bounded_summary(make, 0.05, "test") == "probe=False"
    assert made == [True, False]
    assert await batch.bounded_summary(lambda probe: _quick(probe), 1.0, "test") == "probe=True"


async def _quick(probe: bool) -> str:
    return f"probe={probe}"


async def test_a_delete_summary_out_of_time_doesnt_wait_on_the_bin_check(
    mock_client: DsmClient,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#512 PR 3: a recycle-bin check that doesn't answer once the call's wait is over leaves
    the bin line out, as for one DSM didn't answer; the reply doesn't wait on it."""

    async def hang(*args: Any, **kwargs: Any) -> bool:
        await asyncio.sleep(30)
        return True

    monkeypatch.setattr(batch, "MIN_CHECK_WAIT", 0.05)
    monkeypatch.setattr(operations, "ensure_recycle_status", hang)
    adapter = operations.DeleteAdapter(
        mock_client, ["/s/d0"], paths_arg=["/s/d0"], recursive=True, recycle_bin_status={}
    )
    budget = batch.Budget(WAIT)
    budget.start()
    clock.now += WAIT * 2
    outcome = BatchOutcome(items=[batch.ItemOutcome(batch.Item("/s/d0", None), "done")])
    outcome.budget = budget
    text = await asyncio.wait_for(adapter.summary(outcome), 5)
    assert "Deleted 1 item(s):" in text, text
    assert "Recycle bin is enabled" not in text, text


async def test_a_skip_report_out_of_time_doesnt_read_the_kinds(
    mock_client: DsmClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#512 PR 3: without time to read what a skipped source is, the hedged wording."""

    async def unexpected(*args: Any, **kwargs: Any) -> dict[str, bool]:
        raise AssertionError("read the kinds with no time left")

    monkeypatch.setattr(operations, "paths_isdir", unexpected)
    text = await operations._copy_move_summary(
        mock_client, ["/s/a", "/s/b"], "/s/d", False, 0, ["/s/b"], probe=False
    )
    assert "1 skipped in whole or in part" in text, text


async def test_a_restore_whose_original_folder_is_refused_is_refused_up_front(
    mock_client: DsmClient, clock: FakeClock, respx_mock: respx.Router
) -> None:
    """#482: a 407 original folder was found only at its item's turn, after earlier items were
    restored. It's refused before anything moves."""
    tool = next(t for t in TOOLS if t.name == "restore_origin")
    dsm = FakeDsm({src: PathScript() for src in tool.sources})
    dsm.unclear["/s/o1"] = 407
    dsm.mount(respx_mock)
    with pytest.raises(ToolError) as refused:
        await tool.call(mock_client, TaskRecords())
    body = json.loads(str(refused.value))["error"]
    assert body["code"] == "permission_denied"
    assert body["message"] == (
        "Restore files refused: no permission to use the original folder /s/o1. "
        "Nothing was restored."
    )
    assert (body["param"], body["value"], body["retryable"]) == ("dest_folder", "/s/o1", False)
    assert dsm.starts() == []


async def test_a_restore_whose_later_folder_is_gone_says_what_was_restored(
    mock_client: DsmClient, clock: FakeClock, respx_mock: respx.Router
) -> None:
    """#482 (QA's differential review of #678): a later group's destination answering 408 after
    earlier items finished said "Nothing was restored". Now it's that item's failure, through the
    one formatter: what was restored, which item failed, what wasn't started."""
    tool = next(t for t in TOOLS if t.name == "restore_origin")

    class Deleted(FakeDsm):
        def _getinfo(self, path: str) -> httpx.Response:
            if path == "/s/o1" and self.starts():
                return self._ok({"files": [{"code": 408, "path": path}]})
            return super()._getinfo(path)

    dsm = Deleted({src: PathScript() for src in tool.sources})
    dsm.mount(respx_mock)
    with pytest.raises(ToolError) as refused:
        await tool.call(mock_client, TaskRecords())
    body = json.loads(str(refused.value))["error"]
    message = body["message"]
    assert "Nothing was" not in message, message
    assert message.startswith(
        "Restore files refused: the destination folder /s/o1 doesn't exist. "
        "Failed on: /s/#recycle/o1/f1. Already restored before this failure: "
        "/s/#recycle/o0/f0 (to /s/o0)."
    ), message
    assert message.endswith("Not started: /s/#recycle/o2/f2."), message
    assert body["retryable"] is False
    assert "leave out the paths already restored" in body["suggestion"]
    assert [c.params["path"] for c in dsm.starts()] == ["/s/#recycle/o0/f0"]


async def test_a_copy_summary_out_of_time_doesnt_wait_on_the_skip_lookup(
    mock_client: DsmClient,
    clock: FakeClock,
    respx_mock: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#512 PR 3 (pre-QA r1): a copy's group summary reads what a skipped source is; that read
    is bounded too, and out of time the summary keeps the hedged wording."""
    tool = next(t for t in TOOLS if t.name == "copy_files")
    scripts = _scripts(tool, "skipped", 0)
    FakeDsm(scripts).mount(respx_mock)

    async def hang(*args: Any, **kwargs: Any) -> dict[str, bool]:
        await asyncio.sleep(30)
        return {}

    monkeypatch.setattr(operations, "paths_isdir", hang)
    text = await asyncio.wait_for(tool.call(mock_client, TaskRecords()), WAIT + 3)
    assert "skipped in whole or in part" in text, text


@pytest.mark.parametrize("tool", TOOLS, ids=[t.name for t in TOOLS])
async def test_a_busy_failure_after_its_task_started_says_to_check_first(
    mock_client: DsmClient, clock: FakeClock, respx_mock: respx.Router, tool: Tool
) -> None:
    """Pre-QA r2 on #687: "Wait and retry" beside retryable false read as a plain retry. The
    task had started, so the advice says it may be partly done and to check first."""
    scripts = {src: PathScript() for src in tool.sources}
    scripts[tool.sources[0]].statuses = [RUNNING, 402]
    FakeDsm(scripts).mount(respx_mock)
    with pytest.raises(ToolError) as raised:
        await tool.call(mock_client, TaskRecords())
    error = json.loads(str(raised.value))["error"]
    assert error["retryable"] is False
    # Where its task may have left something: a delete's path, a copy's destination (its
    # source doesn't change), a move's or restore's source and destination (pre-QA r3).
    where = {
        "delete": tool.sources[0],
        "copy": tool.dests[0],
    }.get(tool.kind, f"{tool.sources[0]} and {tool.dests[0]}")
    assert error["suggestion"] == (
        f"The NAS is too busy. Wait and retry. It may be partly done: check {where} with "
        "list_files before running it again."
    ), error["suggestion"]


@pytest.mark.parametrize("tool", [t for t in TOOLS if t.kind != "delete"], ids=lambda t: t.name)
async def test_a_refused_item_after_others_has_no_check_of_its_own(
    mock_client: DsmClient, clock: FakeClock, respx_mock: respx.Router, tool: Tool
) -> None:
    """Pre-QA r3 on #687: a landing refusal of an item that never started gave "check <it> with
    list_files first", meant for an item that may be done. It says what to leave out only."""
    dsm = FakeDsm({src: PathScript() for src in tool.sources})
    name = tool.sources[1].rsplit("/", 1)[-1]
    # As the matrix's landing_refused cell: a task of another call, landing in item 1's folder
    # under its name, listed once item 0 has started.
    dsm.later_tasks.append(
        (
            1,
            {
                "finished": False,
                "taskid": "FileStation_Other",
                "api": "SYNO.FileStation.CopyMove",
                "params": {
                    "path": [f"/s/other/{name}"],
                    "dest_folder_path": tool.dests[1],
                    "overwrite": True,
                    "remove_src": False,
                },
            },
        )
    )
    dsm.mount(respx_mock)
    with pytest.raises(ToolError) as raised:
        await tool.call(mock_client, TaskRecords())
    suggestion = json.loads(str(raised.value))["error"]["suggestion"]
    assert "When retrying, leave out the paths already" in suggestion, suggestion
    assert "first." not in suggestion.split("When retrying", 1)[1], suggestion


async def test_a_restores_recreated_note_quotes_a_padded_folder(
    mock_client: DsmClient,
    clock: FakeClock,
    respx_mock: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-QA r3 on #687: the Re-created line quotes a padded folder, as the header does."""
    tool = next(t for t in TOOLS if t.name == "restore_origin")
    FakeDsm({src: PathScript() for src in tool.sources}).mount(respx_mock)

    async def recreated(client: Any, destinations: Any) -> tuple[list[str], list[str]]:
        return ["/s/o2 "], []

    monkeypatch.setattr(operations, "_recreate_missing_folders", recreated)
    text = await tool.call(mock_client, TaskRecords())
    assert f"Re-created {'/s/o2 '!r} (no longer there) to restore into." in text, text


async def test_a_list_read_that_fails_logs_its_type(
    mock_client: DsmClient,
    clock: FakeClock,
    respx_mock: respx.Router,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pre-QA r4 on #687: an httpx timeout's text is empty, so the WARNING that the duplicate-
    task guard is down names the error's type."""
    tool = TOOLS[0]

    class SlowList(FakeDsm):
        def _answer(self, request: httpx.Request) -> httpx.Response:
            if dict(request.url.params).get("api") == "SYNO.FileStation.BackgroundTask":
                raise httpx.ReadTimeout("")
            return super()._answer(request)

    SlowList({src: PathScript(gone_after=True) for src in tool.sources}).mount(respx_mock)
    with caplog.at_level(logging.WARNING):
        await tool.call(mock_client, TaskRecords())
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        m == "BackgroundTask list failed, skipping the running-task check: ReadTimeout: "
        for m in lines
    ), lines


async def test_a_summary_out_of_time_says_so_and_logs_it(
    mock_client: DsmClient,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pre-QA r4 on #687: the reply said DSM didn't answer, though this server cut the check
    short, and only a DEBUG line said why. It says the call's wait was over, at WARNING."""

    async def hang(*args: Any, **kwargs: Any) -> bool:
        await asyncio.sleep(30)
        return True

    monkeypatch.setattr(batch, "MIN_CHECK_WAIT", 0.05)
    monkeypatch.setattr(operations, "ensure_recycle_status", hang)
    adapter = operations.DeleteAdapter(
        mock_client, ["/s/d0"], paths_arg=["/s/d0"], recursive=True, recycle_bin_status={}
    )
    budget = batch.Budget(WAIT)
    budget.start()
    clock.now += WAIT * 2
    outcome = BatchOutcome(items=[batch.ItemOutcome(batch.Item("/s/d0", None), "done")])
    outcome.budget = budget
    with caplog.at_level(logging.WARNING):
        text = await asyncio.wait_for(adapter.summary(outcome), 5)
    assert "Didn't check whether /s has its recycle bin enabled (the call's wait was over)" in text
    assert "DSM didn't answer" not in text, text
    assert any("replying without them" in r.getMessage() for r in caplog.records), caplog.text


async def test_a_copy_cut_short_bounds_its_group_summary(
    mock_client: DsmClient,
    clock: FakeClock,
    respx_mock: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-QA r4 on #687: a group cut short by the call's wait is summarized within what's left
    too: its skip lookup doesn't hold the reply."""
    tool = next(t for t in TOOLS if t.name == "copy_files")
    scripts = _scripts(tool, "skipped", 0)

    class Slow(FakeDsm):
        def _answer(self, request: httpx.Request) -> httpx.Response:
            response = super()._answer(request)
            params = dict(request.url.params)
            if params.get("method") == "status":
                clock.now += WAIT  # the first item's task took the call's whole wait
            return response

    Slow(scripts).mount(respx_mock)

    async def hang(*args: Any, **kwargs: Any) -> dict[str, bool]:
        await asyncio.sleep(30)
        return {}

    monkeypatch.setattr(batch, "MIN_CHECK_WAIT", 0.05)
    monkeypatch.setattr(operations, "paths_isdir", hang)
    text = await asyncio.wait_for(tool.call(mock_client, TaskRecords()), 5)
    assert "ran out after 1 of 3" in text, text
    assert "skipped in whole or in part" in text, text
