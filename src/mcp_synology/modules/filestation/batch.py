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
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Protocol

import anyio
import httpx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import SynologyError
from mcp_synology.core.formatting import rewrapped_error
from mcp_synology.modules.filestation import tasks as _tasks
from mcp_synology.modules.filestation.helpers import (
    _escaped_repr,
    dsm_str,
    ends_in_list,
    path_list,
    sentence,
    shown,
    then,
)
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
    from collections.abc import Awaitable, Callable, Sequence
    from contextlib import AbstractAsyncContextManager, AbstractContextManager

    from mcp_synology.core.client import DsmClient
    from mcp_synology.modules.filestation.paths import Plan

logger = logging.getLogger(__name__)

# The tools that reach `run_batch`; the scenario matrix runs over each of them.
BATCH_TOOLS = ("delete_files", "copy_files", "move_files", "restore_from_recycle_bin")


# The least an item's task is waited on when the call's budget is nearly spent: two status reads
# at poll_task's interval, so a task that ends at once is still reported as done.
MIN_ITEM_WAIT = 1.0

# The least a check's read gets when the call's budget is nearly spent (the task list before a
# start, a summary's checks): one read, as poll_task's status reads get.
MIN_CHECK_WAIT = 1.0


def _now() -> float:
    # The task module's clock, which the tests' fake clock replaces.
    return _tasks.monotonic()


@dataclass
class Budget:
    """How long a call waits on its tasks, all of them together (#496): ``delete_timeout`` or
    ``copy_move_timeout``. An item gets what's left when it starts (at least ``MIN_ITEM_WAIT``);
    a task still running when the call's time is up is handed off, no item starts after it, and
    the rest are listed as not started. The task list read before each start and the summary's
    checks get what's left too, at least ``MIN_CHECK_WAIT`` (#512 PR 3); the start itself
    doesn't: cut short, it would leave a task that may have started."""

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

    def for_check(self) -> float:
        """The wait for a check's read: the task list before a start, a summary's checks."""
        self.start()
        assert self.deadline is not None
        return max(self.deadline - _now(), MIN_CHECK_WAIT)

    def spent(self) -> bool:
        """The call's wait is over: no further item is started (#496)."""
        return self.deadline is not None and _now() >= self.deadline


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
    # Its task started (a status read's error, or a finished task's): it may have done part of
    # its work, so its failure isn't retryable (the maintainer's Q5 on #512).
    started: bool = False


@dataclass
class BatchOutcome:
    """One outcome per item, in order: the ones that ran, then at most one that ended the batch
    early (failed, may have started, handed off), then the ones not started. ``out_of_time``:
    the call's wait was over before the next item, so the rest weren't started (#496)."""

    items: list[ItemOutcome] = field(default_factory=list)
    out_of_time: float | None = None
    # The call's budget, which bounds the summary's checks (#512 PR 3).
    budget: Budget | None = None

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

    async def before_start(self, item: Item, read_timeout: float) -> dict[str, Any] | None:
        """The task already running for this item (none is started then), or None. May raise a
        ToolError, the item's failure. ``read_timeout`` bounds its task list read (#512 PR 3):
        a list that doesn't answer in time skips the guard, and the item isn't started once the
        call's wait is over."""
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

    async def end_group(self, items: Sequence[Item], wait: float) -> None:
        """After a group's last item finished, before the next group's checks. ``wait`` bounds
        its summary's checks (`bounded_summary`)."""
        ...

    def cause(self, outcome: ItemOutcome) -> NoReturn:
        """Raise the failure of ``outcome``'s item on its own: what the call did before it and
        what wasn't started are `failure_error`'s to add (#482)."""
        ...

    def progress_text(self, outcome: ItemOutcome) -> str:
        """What the call did before ``outcome``'s item failed, as ``. Already <done> before this
        failure: …`` (with what DSM skipped), or "" when it did nothing."""
        ...

    def retry_advice(self) -> str:
        """What to leave out when retrying after some items were done (#480, #482)."""
        ...

    def check_clause(self, item: Item) -> str:
        """Where to look for what ``item``'s task may have done, as ``check … with list_files``:
        a delete's path; a copy's destination (its source doesn't change); a move's or
        restore's source and destination (pre-QA r3 on #687)."""
        ...

    def amend(self, error: ToolError) -> ToolError:
        """The adapter's last note on a failure after items were done (copy: what they
        replaced, #620)."""
        ...

    def handoff_text(self, outcome: ItemOutcome, batch: BatchOutcome) -> str: ...

    async def summary(self, batch: BatchOutcome) -> str: ...


async def run_batch(
    client: DsmClient,
    adapter: TaskAdapter,
    plan: Plan,
    items: Sequence[Item],
    *,
    budget: Budget,
    records: TaskRecords | None,
    on_progress: ProgressCallback | None = None,
) -> BatchOutcome:
    """Run ``items`` one task at a time, stopping at the first that doesn't finish. ``plan``:
    the landing checks' result (`paths.plan_landings`, #684); every item's source and landing
    must be one it checked, so no batch runs a source that skipped them."""
    unchecked = [(x.source, x.dest) for x in items if not plan.allows(x.source, x.dest)]
    if unchecked:
        raise RuntimeError(f"run_batch: items the plan didn't check: {unchecked!r}")
    # One progress token per tool call: keep its values increasing across items (#245).
    progress = MonotonicProgress.wrap(on_progress)
    batch = BatchOutcome(budget=budget)
    budget.start()
    with adapter.batch_scope():
        for i, item in enumerate(items):
            first_of_group = i == 0 or items[i - 1].group != item.group
            if i > 0 and budget.spent():  # also re-checked just before the start (_run_item)
                # The call's wait is over: the rest aren't started (#496). A group cut short is
                # summarized with what it did.
                if not first_of_group:
                    done = [x for x in items[:i] if x.group == item.group]
                    await adapter.end_group(done, budget.for_check())
                batch.items.extend(ItemOutcome(x, "not_started") for x in items[i:])
                batch.out_of_time = budget.per_call
                return batch
            if first_of_group:
                try:
                    await adapter.begin_group([x for x in items if x.group == item.group])
                except ToolError as e:
                    if not batch.items:
                        raise  # a refusal before anything ran: it says so itself
                    # A restore's later destination refused after earlier items finished: it's
                    # that item's failure, so the reply says what was done (#482).
                    batch.items.append(ItemOutcome(item, "failed", error=e))
                    batch.items.extend(ItemOutcome(x, "not_started") for x in items[i + 1 :])
                    return batch
            outcome = await _run_item(
                client, adapter, item, budget, records, progress, first=i == 0
            )
            if outcome.state == "not_started":
                # The wait ran out during the checks before its start (a slow task list).
                if not first_of_group:
                    await adapter.end_group(
                        [x for x in items[:i] if x.group == item.group], budget.for_check()
                    )
                batch.items.extend(ItemOutcome(x, "not_started") for x in items[i:])
                batch.out_of_time = budget.per_call
                return batch
            batch.items.append(outcome)
            if outcome.state != "done":
                batch.items.extend(ItemOutcome(x, "not_started") for x in items[i + 1 :])
                return batch
            adapter.item_done(item, outcome.result)
            last_of_group = i == len(items) - 1 or items[i + 1].group != item.group
            if last_of_group:
                await adapter.end_group(
                    [x for x in items if x.group == item.group], budget.for_check()
                )
    return batch


async def _run_item(
    client: DsmClient,
    adapter: TaskAdapter,
    item: Item,
    budget: Budget,
    records: TaskRecords | None,
    progress: ProgressCallback | None,
    *,
    first: bool = True,
) -> ItemOutcome:
    try:
        async with adapter.start_guard(item):
            running = await adapter.before_start(item, budget.for_check())
            if running is None and not first and budget.spent():
                # The checks before the start (a BackgroundTask list) used up the wait: no new
                # start once it's over (QA round 1 on #686).
                return ItemOutcome(item, "not_started")
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

    def canceled(stop: StopResult) -> None:
        # A canceled call's stop that DSM didn't confirm: the task may still run (#497).
        if stop not in (StopResult.CONFIRMED,):
            outcome = ItemOutcome(
                item, "failed", stop=stop, handoff=Handoff(Handle(adapter.kind, taskid), {})
            )
            _record(adapter, outcome, records, stop_sent=stop is StopResult.UNANSWERED)

    result = await poll_task(
        client,
        api=adapter.api,
        version=adapter.version,
        taskid=taskid,
        wait=budget.for_item(),
        label=adapter.label(item),
        on_progress=progress,
        on_cancel_stop=canceled,
    )
    if result.outcome == "poll_error":
        assert isinstance(result.error, SynologyError)
        outcome = ItemOutcome(item, "failed", error=result.error, stop=result.stop, started=True)
        if result.stop not in (StopResult.CONFIRMED,):
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
    if result.outcome == "lost" and await _gone(client, adapter, item, taskid, records):
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
        return ItemOutcome(item, "failed", error=e, started=True)
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
    if stop not in (StopResult.CONFIRMED,):
        outcome = ItemOutcome(
            item, "failed", stop=stop, handoff=Handoff(Handle(adapter.kind, taskid), {})
        )
        _record(adapter, outcome, records, stop_sent=stop is StopResult.UNANSWERED)


async def _gone(
    client: DsmClient,
    adapter: TaskAdapter,
    item: Item,
    taskid: str,
    records: TaskRecords | None = None,
) -> bool:
    """Whether a lost task's item is gone, by the adapter's bounded check (a stalled DSM mustn't
    hold the reply and its handle, #245)."""
    check = adapter.lost_check(item)
    if check is None:
        return False
    try:
        exists = await check
    except asyncio.CancelledError:
        # A canceled call stops its task, as `poll_task` does: this one may be running; one DSM
        # didn't confirm is recorded, as at the other cancel points (pre-QA r2 on #512 PR 2).
        with anyio.CancelScope(shield=True):
            stop = await _stop_background_task(
                client, adapter.api, dsm_str(taskid), adapter.version, logger
            )
            if stop is not StopResult.CONFIRMED:
                outcome = ItemOutcome(
                    item, "failed", stop=stop, handoff=Handoff(Handle(adapter.kind, taskid), {})
                )
                _record(adapter, outcome, records, stop_sent=stop is StopResult.UNANSWERED)
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


async def batch_reply(adapter: TaskAdapter, batch: BatchOutcome, note: str = "") -> str:
    """The call's reply: the adapter's summary, hand-off text or error (raised). ``note`` (a
    line starting with a newline) follows the summary, before a ``Not started:`` line."""
    last = batch.last
    if last is None:
        text = await adapter.summary(batch)
        rest = batch.not_started()
        if rest and batch.out_of_time is not None:
            # Done before the call's wait ran out; nothing is running (#496). The first line is
            # what gets read, so it says the call is partial (pre-QA r2).
            ran = len(batch.items) - len(rest)
            them = "it" if len(rest) == 1 else "them"
            text = (
                f"[!] This call's wait ({batch.out_of_time:g} s, {_setting(adapter)}) ran out "
                f"after {ran:,} of "
                f"{len(batch.items):,} item(s); the rest weren't started (listed last).\n"
                + text
                + note
                + "\n"
                # One a line when two or more, the advice in the heading (#693).
                + (
                    f"Not started: {shown(rest[0].source)}. Run it in another call."
                    if len(rest) == 1
                    else path_list(
                        f"Not started (run {them} in another call)", [shown(x.source) for x in rest]
                    )
                )
            )
            return text
        return text + note
    if last.state == "handed_off":
        return adapter.handoff_text(last, batch) + note
    raise failure_error(adapter, last, batch)


async def bounded_summary(
    make: Callable[[bool], Awaitable[str]], wait: float | None, label: str
) -> str:
    """A summary within ``wait`` (#512 PR 3): ``make(True)`` may read the NAS (the recycle bin's
    setting, what a skipped source is), and those reads are what's bounded. Out of time, it's
    ``make(False)``, the same summary from what's known already, worded as for a check DSM
    didn't answer: the tasks are done, and a slow check mustn't take the call past its wait."""
    if wait is None:
        return await make(True)
    try:
        return await asyncio.wait_for(make(True), wait)
    except TimeoutError:
        # A WARNING: the reply says a check got no answer in time, and the cause is ours.
        logger.warning(
            "%s: the summary's checks didn't finish within what was left of the call's wait "
            "(%.1f s); replying without them",
            label,
            wait,
        )
    return await make(False)


def unstop(message: str) -> str:
    """``message`` without its final period, and only that one: a path can end in dots."""
    return message[:-1] if message.endswith(".") else message


# Advice that already says to check with list_files (a start with no answer, a path not
# found when the delete ran), not advice that only mentions it (408's "Use list_files or
# search_files to find …").
CHECKS_WITH_LIST_FILES = re.compile(r"\bcheck\b[^.]*\bwith list_files\b", re.IGNORECASE)


def names_path(message: str, path: str) -> bool:
    """Whether ``message`` names ``path`` itself: after the start, a space, ":" or "(",
    and followed by the end, a sentence's period, or " (" (as in
    ``/video/b (DSM error 900)``); not a path ending in it (``/video/b/x/video/b``)."""
    # As messages show it: quoted when padded (`shown`, #489).
    pattern = r"(?<![^\s:(])" + re.escape(shown(path)) + r"(?=$|\.$|\.\s|\s\()"
    return re.search(pattern, message) is not None


def failure_error(adapter: TaskAdapter, outcome: ItemOutcome, batch: BatchOutcome) -> ToolError:
    """The one failure formatter of the batch tools (#482): the item's cause, then which item
    failed, what the call did before it, what wasn't started, the adapter's note and the stop
    sent for its task, the same for every tool. One retryable rule (#452, #490; the maintainer's
    Q4 and Q5 on #512): only when nothing ran, nothing may have, and the cause's own flag says
    so: not once an earlier item was done or this item's own task started. Advice is added to
    the cause's own, never in place of it (QA round 3 on #480)."""
    try:
        adapter.cause(outcome)
    except ToolError as e:
        error = e
    item = outcome.item
    done = adapter.progress_text(outcome)
    rest = [x.source for x in batch.not_started()]
    refused = _refused_path(error)
    if refused is not None and refused != item.source and refused in rest:
        # A refusal of a later item, checked before this one's start (a landing clash, #587):
        # that's the item it names; this one wasn't started either.
        rest = [item.source, *(r for r in rest if r != refused)]
        item = Item(refused, item.dest, item.group)
    # Its own task ran or may have (a start with no answer): it may be partly done. A refusal
    # of ours (a ToolError before its start) did nothing to it, so there's nothing to check.
    ran = outcome.started or outcome.state == "may_have_started"
    ours = isinstance(outcome.error, ToolError) and not outcome.started
    check = None if ours else adapter.check_clause(item)
    if done or rest:
        error = _with_progress(error, item.source, done, adapter.retry_advice(), check)
        if done:
            error = adapter.amend(error)
        error = _with_not_started(error, rest)
    if ran:
        # Not retryable, whatever the cause's flag (402 and 421 say to wait and retry, #490), as
        # for a call where earlier items were done (Q5). The advice says why, unless an earlier
        # item's note already says to check (pre-QA r2).
        error = _not_retryable(error, None if done else check)
    return _with_stop(error, outcome)


def _add_advice(details: dict[str, Any], note: str) -> None:
    """``note`` added to an error's own advice, never in place of it (QA round 3 on #480): after
    it, or alone; first when the advice ends in a list (418's characters to avoid), so no
    punctuation is read as part of the list."""
    original = str(details.get("suggestion") or "").strip()
    if not original:
        details["suggestion"] = note
    elif original[-1] in ".!?)":
        details["suggestion"] = f"{original} {note}"
    else:
        details["suggestion"] = f"{note} {original}"


def _checks_already(advice: str, check: str) -> bool:
    """The advice already says to check with list_files: in general words, or as ``check``
    itself (a cause that names where to look, whose paths may hold the dots the general pattern
    stops at: pre-QA r4 on #687)."""
    return bool(CHECKS_WITH_LIST_FILES.search(advice)) or check.lower() in advice.lower()


def _not_retryable(error: ToolError, check: str | None) -> ToolError:
    """``error`` not retryable; with ``check`` (an adapter's `check_clause`), advice to check
    first, since its task may be partly done."""
    try:
        body = json.loads(str(error))
        details = body["error"]
    except (ValueError, KeyError, TypeError):
        return error
    details["retryable"] = False
    advice = str(details.get("suggestion") or "")
    if check is not None and not _checks_already(advice, check):
        _add_advice(details, f"It may be partly done: {check} before running it again.")
    return rewrapped_error(body)


def _refused_path(error: ToolError) -> str | None:
    """The one path a refusal gives as its value, if it gives one."""
    try:
        details = json.loads(str(error))["error"]
    except (ValueError, KeyError, TypeError):
        return None
    value = details.get("value")
    return value if details.get("param") == "paths" and isinstance(value, str) else None


def _with_progress(
    error: ToolError, failed: str, done: str, leave_out: str, check: str | None
) -> ToolError:
    """``error`` with which item failed and what was done before it (``done``), so the user
    can tell what's left (QA round 1 on #480)."""
    try:
        body = json.loads(str(error))
        details = body["error"]
        message = str(details["message"])
    except (ValueError, KeyError, TypeError):
        return error
    # A DSM error on a start or a status request names no path (QA round 5 on #480); one
    # that names a path inside the item, or a sibling starting with the same characters
    # (/video/a.txt, /video/a b), doesn't name the item either. A refusal quotes it as
    # ``_escaped_repr`` writes it.
    names_it = names_path(message, failed) or _escaped_repr(failed) in message
    if not names_it:
        message = then(sentence(message), f"Failed on: {shown(failed)}.")
    details["param"] = "paths"
    details["value"] = failed
    if done:
        message = then(sentence(message), done)
        # Something ran: the same call again would repeat or meet it, so only what's left can
        # be retried, after a check (#482; the maintainer's Q5 on #512).
        details["retryable"] = False
        # A note that reads after any advice, or alone (QA round 4 on #480): no "Then", no
        # second "retry", and a check first, since the failed item may be done already, unless
        # the advice already says to check with list_files (QA round 5), or the item is one this
        # server refused before its start (pre-QA r3 on #687: nothing of it can be done).
        retry = f"When retrying, leave out {leave_out}"
        if check is not None and not _checks_already(str(details.get("suggestion") or ""), check):
            retry += f", and {check} first"
        _add_advice(details, retry + ".")
    details["message"] = message
    return rewrapped_error(body)


def _with_not_started(error: ToolError, rest: list[str]) -> ToolError:
    """``error`` ending with the items that weren't started, last as the reply's first line of
    a cut-short call says (#482, #496)."""
    if not rest:
        return error
    try:
        body = json.loads(str(error))
        message = str(body["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return error
    # One a line when two or more (#693).
    body["error"]["message"] = then(
        sentence(message), path_list("Not started", [shown(p) for p in rest])
    )
    return rewrapped_error(body)


# What a failure says of the stop sent after a status read's DSM error (#497): only a confirmed
# stop is called one; anything else leaves the task possibly running, with its handle.
_STOP_SAYS = {
    StopResult.CONFIRMED: "DSM confirmed the stop of {}'s task",
    StopResult.UNANSWERED: "a stop was sent for {}'s task, and DSM hasn't confirmed it",
    StopResult.REFUSED: "DSM refused the stop of {}'s task, so it may still be running",
    StopResult.NOT_SENT: "the stop for {}'s task couldn't be sent, so it may still be running",
}


def _setting(adapter: TaskAdapter) -> str:
    """The setting that sets this call's wait, as the reply names it."""
    return "delete_timeout" if adapter.kind == "delete" else "copy_move_timeout"


def _with_stop(error: ToolError, outcome: ItemOutcome) -> ToolError:
    """``error`` with what became of the stop of ``outcome``'s task, and its handle when it may
    still be running (#497)."""
    if outcome.stop is None:
        return error
    try:
        body = json.loads(str(error))
        details = body["error"]
        message = str(details["message"])
    except (ValueError, KeyError, TypeError):
        return error
    says = _STOP_SAYS[outcome.stop].format(shown(outcome.item.source))
    if ends_in_list(message):
        # After a list of lines, a sentence of its own (#693).
        details["message"] = f"{message}\n{says[0].upper()}{says[1:]}."
    else:
        # Only the message's own period goes: a path ending in a dot keeps it (`/s/report.`).
        details["message"] = f"{message.removesuffix('.')}; {says}."
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
        details["suggestion"] = f"{advice.removesuffix('.')}. {follow}" if advice else follow
    return rewrapped_error(body)


@contextlib.contextmanager
def no_scope() -> Any:
    """A `batch_scope` that holds nothing."""
    yield None
