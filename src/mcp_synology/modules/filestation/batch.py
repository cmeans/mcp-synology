"""The one runner for File Station's multi-path background tasks (#512).

``delete_files``, ``copy_files``, ``move_files`` and ``restore_from_recycle_bin`` each run one DSM
task per path (#68, #84). This module owns that loop: it starts each item's task, waits for it,
classifies how it ended, hands it off with a handle and writes its ``TaskRecord``, and lists what
wasn't started. What differs between the tools (the checks before anything starts, the start's
parameters, how a finished status reads, the reply's wording) is an adapter's
(``operations.py``'s ``DeleteAdapter`` and ``CopyMoveAdapter``).

The CLAUDE.md task rules are kept here and in ``tasks.poll_task``: a task still running when the
wait ends, or whose status read stalls, isn't answered, or isn't a DSM reply, is handed off and
never stopped; only cancellation and a DSM error code stop it, and a finished Delete is never
stopped; a 599 is read again (``poll_task``) and a lost item may be resolved by the adapter's
check; no DSM request is sent between a hand-off and its reply.

Photos isn't run here: each Photos call is one DSM task for all its IDs, with its own API
(``abort_task``, no 599, no records), so there is no per-item loop to share.

``tests/modules/filestation/test_batch.py`` is this module's guard: a scenario matrix over every
tool in ``BATCH_TOOLS`` against a scripted DSM, and a structural test that no other module starts
or polls these tasks or writes their records.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Protocol

import anyio
import httpx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import SynologyError
from mcp_synology.core.formatting import rewrapped_error
from mcp_synology.modules.filestation import tasks as _tasks
from mcp_synology.modules.filestation.helpers import dsm_str
from mcp_synology.modules.filestation.tasks import (
    STOP_WAIT,
    Handle,
    HandleKind,
    Handoff,
    MonotonicProgress,
    ProgressCallback,
    StopNote,
    StopResult,
    TaskRecord,
    TaskRecords,
    _stop_background_task,
    poll_task,
    progress_mark,
    progress_text,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence
    from contextlib import AbstractAsyncContextManager, AbstractContextManager

    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)

# The tools that reach `run_batch`; the scenario matrix runs over each of them.
BATCH_TOOLS = ("delete_files", "copy_files", "move_files", "restore_from_recycle_bin")


# The least an item's task is waited on when the call's budget is nearly spent: two status reads
# at poll_task's interval, so a task that ends at once is still reported as done.
MIN_ITEM_WAIT = 1.0


def _now() -> float:
    # The task module's clock, which the tests' fake clock replaces.
    return _tasks.monotonic()


@dataclass
class Budget:
    """How long a call waits on its tasks, all of them together (#496): ``delete_timeout`` or
    ``copy_move_timeout``. An item gets what's left when it starts (at least ``MIN_ITEM_WAIT``);
    a task still running when the call's time is up is handed off, and the rest are listed as
    not started."""

    per_call: float
    deadline: float | None = None

    def start(self) -> None:
        """The call's wait begins (once)."""
        if self.deadline is None:
            self.deadline = _now() + self.per_call

    def for_item(self) -> float:
        """The wait for the item about to be polled."""
        self.start()
        assert self.deadline is not None
        return max(self.deadline - _now(), MIN_ITEM_WAIT)


@dataclass(frozen=True)
class Item:
    """One source of the call, with its destination (None for a delete) and its group: the
    items an adapter checks and summarizes together (a restore to several places has one group
    per item, as it had one call each)."""

    source: str
    dest: str | None
    group: int = 0


ItemState = Literal["done", "failed", "may_have_started", "handed_off", "not_started"]


@dataclass
class ItemOutcome:
    """How one item ended.

    ``done``: its task finished and the adapter read it (``result``: the adapter's reading).
    ``failed``: ``error`` is the ToolError to report, or a DSM error to word. After a status
    read's DSM error, ``stop`` says how the stop sent for its task ended; unless DSM confirmed
    it, the task may still run, so it has a ``handoff`` (its handle) and a record (#497).
    ``may_have_started``: the start got no answer (``error``, a transport error).
    ``handed_off``: left running with ``handoff``; ``running`` is the task DSM listed when one
    was already running, so none was started.
    """

    item: Item
    state: ItemState
    result: Any = None
    error: BaseException | None = None
    stop: StopResult | None = None
    handoff: Handoff | None = None
    running: dict[str, Any] | None = None


@dataclass
class BatchOutcome:
    """One outcome per item, in order: the ones that ran, then at most one that ended the batch
    early (failed, may have started, handed off), then the ones not started."""

    items: list[ItemOutcome] = field(default_factory=list)

    @property
    def last(self) -> ItemOutcome | None:
        """The item that ended the batch early, if one did."""
        for outcome in self.items:
            if outcome.state not in ("done", "not_started"):
                return outcome
        return None

    def not_started(self) -> list[Item]:
        return [o.item for o in self.items if o.state == "not_started"]


class TaskAdapter(Protocol):
    """What a tool supplies to `run_batch`. One adapter per call: it may keep the call's state."""

    api: str
    kind: HandleKind

    @property
    def version(self) -> int: ...

    def batch_scope(self) -> AbstractContextManager[Any]:
        """Held for the whole run (copy, move, restore: the landing records, #597)."""
        ...

    async def begin_group(self, items: Sequence[Item]) -> None:
        """Checks before a group's first start; raises a refusal (it isn't an item's failure)."""
        ...

    def start_guard(self, item: Item) -> AbstractAsyncContextManager[Any]:
        """Held from `before_start` until the start returns (copy, move, restore: #587)."""
        ...

    async def before_start(self, item: Item) -> dict[str, Any] | None:
        """The task already running for this item (none is started then), or None. May raise a
        ToolError, the item's failure."""
        ...

    def start_params(self, item: Item) -> dict[str, str]: ...

    def started(self, item: Item, taskid: str) -> None:
        """The start answered, still inside `start_guard`."""
        ...

    def label(self, item: Item) -> str:
        """The item as poll_task's logs and progress messages name it."""
        ...

    def read_finished(self, item: Item, status: dict[str, Any]) -> Any:
        """A finished status's reading; raises a ToolError when the task reports a failure."""
        ...

    def lost_check(self, item: Item) -> Awaitable[bool | None] | None:
        """For a lost task (#521): an awaitable that answers whether the item is still there
        (False: done), or None when the adapter can't tell."""
        ...

    def item_done(self, item: Item, result: Any) -> None: ...

    def record_extras(self, outcome: ItemOutcome) -> dict[str, Any]:
        """The hand-off's TaskRecord fields beside its handle."""
        ...

    async def end_group(self, items: Sequence[Item]) -> None:
        """After a group's last item finished, before the next group's checks."""
        ...

    def failure(self, outcome: ItemOutcome, batch: BatchOutcome) -> NoReturn: ...

    def handoff_text(self, outcome: ItemOutcome, batch: BatchOutcome) -> str: ...

    async def summary(self, batch: BatchOutcome) -> str: ...


async def run_batch(
    client: DsmClient,
    adapter: TaskAdapter,
    items: Sequence[Item],
    *,
    budget: Budget,
    records: TaskRecords | None,
    on_progress: ProgressCallback | None = None,
) -> BatchOutcome:
    """Run ``items`` one task at a time, stopping at the first that doesn't finish."""
    # One progress token per tool call: keep its values increasing across items (#245).
    progress = MonotonicProgress.wrap(on_progress)
    batch = BatchOutcome()
    budget.start()
    with adapter.batch_scope():
        for i, item in enumerate(items):
            first_of_group = i == 0 or items[i - 1].group != item.group
            if first_of_group:
                await adapter.begin_group([x for x in items if x.group == item.group])
            outcome = await _run_item(client, adapter, item, budget, records, progress)
            batch.items.append(outcome)
            if outcome.state != "done":
                batch.items.extend(ItemOutcome(x, "not_started") for x in items[i + 1 :])
                return batch
            adapter.item_done(item, outcome.result)
            last_of_group = i == len(items) - 1 or items[i + 1].group != item.group
            if last_of_group:
                await adapter.end_group([x for x in items if x.group == item.group])
    return batch


async def _run_item(
    client: DsmClient,
    adapter: TaskAdapter,
    item: Item,
    budget: Budget,
    records: TaskRecords | None,
    progress: ProgressCallback | None,
) -> ItemOutcome:
    try:
        async with adapter.start_guard(item):
            running = await adapter.before_start(item)
            if running is None:
                try:
                    data = await _shielded_start(client, adapter, item, records)
                except SynologyError as e:
                    return ItemOutcome(item, "failed", error=e)
                taskid = str(data.get("taskid", ""))
                adapter.started(item, taskid)
    except ToolError as e:
        return ItemOutcome(item, "failed", error=e)
    except httpx.TransportError as e:
        # The start got no answer (a status with none hands the item off in poll_task): it
        # may have started.
        return ItemOutcome(item, "may_have_started", error=e)

    if running is not None:
        # Never start a second task on a path that already has one (#245).
        handoff = Handoff(Handle(adapter.kind, str(running["taskid"])), running)
        return _handed_off(
            adapter, ItemOutcome(item, "handed_off", handoff=handoff, running=running), records
        )

    result = await poll_task(
        client,
        api=adapter.api,
        version=adapter.version,
        taskid=taskid,
        wait=budget.for_item(),
        label=adapter.label(item),
        on_progress=progress,
    )
    if result.outcome == "poll_error":
        assert isinstance(result.error, SynologyError)
        outcome = ItemOutcome(item, "failed", error=result.error, stop=result.stop)
        if result.stop is not StopResult.CONFIRMED:
            # DSM didn't confirm the stop: the task may still run, so the reply gives its handle
            # and file_task knows it; a stop that may have reached DSM is noted (#497, #660).
            outcome.handoff = Handoff(Handle(adapter.kind, taskid), result.status)
            _record(
                adapter,
                outcome,
                records,
                stop_sent=result.stop is StopResult.UNANSWERED,
            )
        return outcome
    if result.outcome == "lost" and await _gone(client, adapter, item, taskid):
        # DSM can't say how it ended, but the item can: gone is what was asked for (#521).
        return ItemOutcome(item, "done")
    if result.outcome in ("handoff", "stalled", "lost"):
        # Left running on the NAS, not stopped (#245): the caller gets a handle. Lost too: it
        # may still be running, and the reply says what's known (#521).
        stall = result.error if result.outcome != "handoff" else None
        handoff = Handoff(Handle(adapter.kind, taskid), result.status, stall)
        return _handed_off(adapter, ItemOutcome(item, "handed_off", handoff=handoff), records)
    try:
        reading = adapter.read_finished(item, result.status)
    except ToolError as e:
        return ItemOutcome(item, "failed", error=e)
    return ItemOutcome(item, "done", result=reading)


async def _shielded_start(
    client: DsmClient, adapter: TaskAdapter, item: Item, records: TaskRecords | None
) -> dict[str, Any]:
    """Send the item's start. A call canceled while it's in flight (#394 C3) may still have
    started a task on the NAS: the start is shielded, its answer is awaited for up to
    ``STOP_WAIT``, and the task it names is stopped and recorded (with a ``StopNote`` when DSM
    didn't confirm the stop), so none is left running unknown. Then the cancellation goes on."""
    start = asyncio.ensure_future(
        client.request(
            adapter.api, "start", version=adapter.version, params=adapter.start_params(item)
        )
    )
    try:
        return await asyncio.shield(start)
    except asyncio.CancelledError:
        # anyio cancel scopes (the MCP SDK's) are level-triggered: shield the cleanup, or it is
        # canceled again at once and the NAS task is orphaned.
        with anyio.CancelScope(shield=True):
            await _stop_what_started(client, adapter, item, records, start)
        raise


async def _stop_what_started(
    client: DsmClient,
    adapter: TaskAdapter,
    item: Item,
    records: TaskRecords | None,
    start: asyncio.Future[Any],
) -> None:
    """After a cancel during the start: stop the task it started, if it started one."""
    with anyio.move_on_after(STOP_WAIT):
        with contextlib.suppress(BaseException):
            await asyncio.shield(start)
    if not start.done():
        start.cancel()
        logger.warning(
            "%s start for %s unanswered after a cancel: a task it started can't be stopped "
            "or followed (no task ID)",
            adapter.api,
            item.source,
        )
        return
    if start.cancelled() or start.exception() is not None:
        return  # no task started, or none DSM said it started
    taskid = str((start.result() or {}).get("taskid", ""))
    if not taskid:
        return
    stop = await _stop_background_task(
        client, adapter.api, dsm_str(taskid), adapter.version, logger
    )
    logger.warning(
        "%s for %s started as the call was canceled: stopping it %s",
        adapter.api,
        item.source,
        stop.value,
    )
    if stop is not StopResult.CONFIRMED:
        outcome = ItemOutcome(
            item, "failed", stop=stop, handoff=Handoff(Handle(adapter.kind, taskid), {})
        )
        _record(adapter, outcome, records, stop_sent=stop is StopResult.UNANSWERED)


async def _gone(client: DsmClient, adapter: TaskAdapter, item: Item, taskid: str) -> bool:
    """Whether a lost task's item is gone, by the adapter's bounded check (a stalled DSM mustn't
    hold the reply and its handle, #245)."""
    check = adapter.lost_check(item)
    if check is None:
        return False
    try:
        exists = await check
    except asyncio.CancelledError:
        # A canceled call stops its task, as `poll_task` does: this one may be running.
        with anyio.CancelScope(shield=True):
            await _stop_background_task(
                client, adapter.api, dsm_str(taskid), adapter.version, logger
            )
        raise
    except Exception as e:
        # Any answer that doesn't say it's gone leaves the outcome unknown.
        logger.warning(
            "Couldn't check %s after a lost %s: %s", item.source, adapter.kind, type(e).__name__
        )
        return False
    return exists is False


def _handed_off(
    adapter: TaskAdapter, outcome: ItemOutcome, records: TaskRecords | None
) -> ItemOutcome:
    """Record a handed-off item, so `file_task` can follow it up. No DSM request here (#245)."""
    _record(adapter, outcome, records)
    return outcome


def _record(
    adapter: TaskAdapter,
    outcome: ItemOutcome,
    records: TaskRecords | None,
    *,
    stop_sent: bool = False,
) -> None:
    """The one record writer: an item with a handle gets a ``TaskRecord``, with a ``StopNote``
    when a stop was sent and DSM hasn't confirmed it (#497, #660)."""
    assert outcome.handoff is not None
    if records is None:
        return
    record = TaskRecord(handle=outcome.handoff.handle, **adapter.record_extras(outcome))
    if stop_sent:
        status = outcome.handoff.status
        # The last read came before the stop, so it isn't a baseline yet (QA round 1 on #660).
        record.stop = StopNote(
            sent=time.time(),
            sent_clock=records.now(),
            progress=progress_text(status) if status else None,
            mark=progress_mark(status) if status else None,
            settled=False,
        )
    records.add(record)


async def batch_reply(adapter: TaskAdapter, batch: BatchOutcome) -> str:
    """The call's reply: the adapter's summary, hand-off text or error (raised)."""
    last = batch.last
    if last is None:
        return await adapter.summary(batch)
    if last.state == "handed_off":
        return adapter.handoff_text(last, batch)
    try:
        adapter.failure(last, batch)
    except ToolError as e:
        raise _with_stop(e, last) from None


# What a failure says of the stop sent after a status read's DSM error (#497): only a confirmed
# stop is called one; anything else leaves the task possibly running, with its handle.
_STOP_SAYS = {
    StopResult.CONFIRMED: "DSM confirmed the stop of its task",
    StopResult.UNANSWERED: "a stop was sent for its task, and DSM hasn't confirmed it",
    StopResult.REFUSED: "DSM refused the stop of its task, so it may still be running",
    StopResult.NOT_SENT: "the stop for its task couldn't be sent, so it may still be running",
}


def _with_stop(error: ToolError, outcome: ItemOutcome) -> ToolError:
    """``error`` with what became of the stop of ``outcome``'s task, and its handle when it may
    still be running (#497)."""
    if outcome.stop is None:
        return error
    try:
        body = json.loads(str(error))
        details = body["error"]
        message = str(details["message"]).rstrip(". ")
    except (ValueError, KeyError, TypeError):
        return error
    details["message"] = f"{message}; {_STOP_SAYS[outcome.stop]}."
    if outcome.handoff is not None:
        handle = str(outcome.handoff.handle)
        details["handle"] = handle
        # It may still change things on the NAS: running the call again now could meet it.
        details["retryable"] = False
        follow = f"Check it with file_task(handle={handle!r}) before running this again" + (
            "; don't send another stop yet: DSM can take a minute to confirm one."
            if outcome.stop is StopResult.UNANSWERED
            else ", and stop it there if it's still running."
        )
        advice = str(details.get("suggestion") or "").strip()
        details["suggestion"] = f"{advice.rstrip('.')}. {follow}" if advice else follow
    return rewrapped_error(body)


@contextlib.contextmanager
def no_scope() -> Any:
    """A `batch_scope` that holds nothing."""
    yield None
