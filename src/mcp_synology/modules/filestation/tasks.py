"""File Station task handles and the in-memory records behind them."""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import logging
import re
import time
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NoReturn, get_args

import anyio
import httpx

from mcp_synology.core.errors import (
    DsmHttpError,
    DsmReplyError,
    ErrorCode,
    RequestTooLongError,
    SynologyError,
)
from mcp_synology.core.formatting import (
    error_response,
    format_size,
    format_table,
    format_timestamp,
    synology_error_response,
)
from mcp_synology.modules.filestation.helpers import (
    describe_transport_error,
    dsm_str,
    name_key,
    shown,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)

HandleKind = Literal["delete", "copy", "move", "restore"]

_KINDS: tuple[str, ...] = get_args(HandleKind)
_TASKID_RE = re.compile(r"^[A-Za-z0-9_]{1,80}$")
_DECORATION = " \t\r\n`'\"*.,;:()[]<>"
# How long a stop waits for DSM's answer before the task's status decides (#630). DSM 7.1
# sometimes answers a stop only after ~60 s (a Delete on vdsm; a CopyMove to a USB drive on a
# real NAS: 60.10 s), although the stop takes effect at once, and claude.ai stops waiting for a
# tool call after 60 s. Write and pool keep the client's default; connect is capped at this.
STOP_WAIT = 15.0
# The whole of file_task's stop (status reads before and after the stop, and the stop itself)
# ends by this many seconds from its start, under that 60 s limit with time to spare (#630).
STOP_DEADLINE = 45.0


@dataclass(frozen=True)
class Handle:
    """A follow-up handle for a File Station background task: ``kind:taskid``."""

    kind: HandleKind
    taskid: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.taskid}"

    @property
    def api(self) -> str:
        """The DSM API that owns this task."""
        return "SYNO.FileStation.Delete" if self.kind == "delete" else "SYNO.FileStation.CopyMove"


def parse_handle(text: str) -> Handle:
    """Parse a handle string, tolerating quoting or trailing punctuation around it."""
    cleaned = text.strip(_DECORATION)
    kind, sep, taskid = cleaned.partition(":")
    if not sep or kind not in _KINDS or not _TASKID_RE.match(taskid):
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"{text!r} isn't a task handle.",
            retryable=False,
            param="handle",
            value=text,
            suggestion="Pass the handle exactly as returned, e.g. delete:FileStation_1234.",
        )
    return Handle(kind=kind, taskid=taskid)  # type: ignore[arg-type]


@dataclass
class TaskRecord:
    """What the server remembers about a task it started."""

    handle: Handle
    paths: list[str]
    dest: str | None = None
    overwrite: bool | None = None
    recursive: bool | None = None
    recycle: dict[str, bool] = field(default_factory=dict)
    final_status: dict[str, Any] | None = None
    # The sources that landed whole before this one in its call, at the same destination (#580).
    earlier: list[str] = field(default_factory=list)
    # The sources DSM skipped before this one in its call, so the final text can say so (#585).
    skipped_earlier: list[str] = field(default_factory=list)
    # What's reported as already done before this task.
    done_earlier: list[str] = field(default_factory=list)
    # What landed, or is landing, at the call's destination; ``earlier`` isn't that when the task
    # goes elsewhere. A skipped source named like one of these isn't offered overwrite=true.
    landed_earlier: list[str] = field(default_factory=list)
    # Where each of those sources went, when a call has several destinations or a later call met
    # this task: a name only clashes at one destination, and a reply says which it means. One per
    # source: a source skipped at two destinations by two calls keeps the later one.
    dest_of: dict[str, str] = field(default_factory=dict)


class TaskRecords:
    """Bounded, expiring in-memory store of task records."""

    def __init__(
        self,
        *,
        max_entries: int = 50,
        ttl: float = 86400.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_entries
        self._ttl = ttl
        self._clock = clock
        self._items: OrderedDict[str, tuple[float, TaskRecord]] = OrderedDict()

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, (created, _) in self._items.items() if now - created > self._ttl]:
            del self._items[key]

    def add(self, record: TaskRecord) -> None:
        self._purge()
        key = str(record.handle)
        known = self._items.pop(key, None)
        if known is not None:
            # A later call that meets the same running task knows less about the first's sources.
            old = known[1]
            record.earlier = list(dict.fromkeys([*old.earlier, *record.earlier]))
            # The later call knows better: a source skipped then may be done now, and the reverse.
            new_skipped, new_done = record.skipped_earlier, record.done_earlier
            record.skipped_earlier = [
                p for p in dict.fromkeys([*old.skipped_earlier, *new_skipped]) if p not in new_done
            ]
            record.done_earlier = [
                p for p in dict.fromkeys([*old.done_earlier, *new_done]) if p not in new_skipped
            ]
            record.landed_earlier = list(
                dict.fromkeys([*old.landed_earlier, *record.landed_earlier])
            )
            record.dest_of = {**old.dest_of, **record.dest_of}
        self._items[key] = (self._clock(), record)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def get(self, handle: Handle) -> TaskRecord | None:
        self._purge()
        entry = self._items.get(str(handle))
        return entry[1] if entry else None

    def set_final(self, handle: Handle, status: dict[str, Any]) -> None:
        record = self.get(handle)
        if record is None:
            record = TaskRecord(handle=handle, paths=[])
            self.add(record)
        record.final_status = status


async def _stop_background_task(
    client: DsmClient,
    api: str,
    taskid: str,
    version: int,
    task_logger: logging.Logger,
) -> bool:
    """Stop a background task (CopyMove, Delete). Logs warnings on failure.

    Returns whether DSM confirmed the stop, so an error message never claims a task
    was stopped when DSM, stalled, didn't answer the stop either (QA R1 on #239).
    """
    try:
        await client.request(
            api, "stop", version=version, params={"taskid": taskid}, read_timeout=STOP_WAIT
        )
    except SynologyError as e:
        task_logger.warning("%s task cleanup failed for %s: %s", api, taskid, e)
    except Exception:
        task_logger.warning("%s task cleanup failed for %s", api, taskid, exc_info=True)
    else:
        return True
    return False


ProgressCallback = Callable[[float, float | None, str], Awaitable[None]]
# How much a progress value that wouldn't otherwise increase is raised by.
_PROGRESS_STEP = 0.001


class MonotonicProgress:
    """A progress callback whose values always increase, as MCP requires.

    One tool call has one progress token, so a multi-path call wraps its callback once
    and every path's task shares it: a heartbeat, or a count that doesn't exceed the
    last value sent (e.g. path 2 starting near 0), is raised by ``_PROGRESS_STEP`` and
    sent without a total, like a heartbeat, so a client never shows it against the
    wrong total (50.001 of 10); the message still has the real figures.
    Progress is best-effort: a failing callback is logged at DEBUG, never raised.
    """

    def __init__(self, callback: ProgressCallback) -> None:
        self._callback = callback
        self._last: float | None = None

    @classmethod
    def wrap(cls, callback: ProgressCallback | None) -> MonotonicProgress | None:
        """``callback`` as a MonotonicProgress (itself if it already is one)."""
        if callback is None or isinstance(callback, cls):
            return callback
        return cls(callback)

    async def __call__(self, value: float, total: float | None, message: str) -> None:
        if self._last is not None and value <= self._last:
            value = self._last + _PROGRESS_STEP
            total = None
        self._last = value
        try:
            await self._callback(value, total, message)
        except Exception:
            logger.debug("Progress notification failed (%s), ignoring", message, exc_info=True)


@dataclass
class PollResult:
    """How a poll of a background task ended.

    ``stalled``: a status request got no answer (a transport error), an HTTP error
    status (a proxy's 502/504), or an answer that couldn't be read; the task was left
    running, like ``handoff`` (#245, maintainer decision 2026-10-17; #282).

    ``lost``: DSM kept answering that it has no such task (599), so it can't say how the task
    ended. Not stopped: the call asked for this work, and it may still be going, so stopping it is
    the caller's choice, with ``file_task``, unless the call itself is canceled (#521). Handed off
    like ``stalled``, so the call keeps its handle and what it did and didn't do.
    """

    status: dict[str, Any]
    outcome: Literal["finished", "handoff", "poll_error", "stalled", "lost"]
    error: Exception | None = None
    stopped: bool = False


@dataclass
class Handoff:
    """A task left to run on the NAS: its handle, last status, and the stall if any.

    ``stall`` is the status request's error: a ``TransportError`` (no answer), a 599 (DSM has no
    such task, #521) or any other exception (an answer that couldn't be read).
    """

    handle: Handle
    status: dict[str, Any]
    stall: Exception | None = None


def _progress_numbers(status: dict[str, Any]) -> tuple[int, int, str] | None:
    """``(processed, total, "x of y")`` from a status, or None while DSM has no total.

    Delete counts items (``processed_num``); CopyMove counts bytes (``processed_size``;
    vdsm 7.1 and 7.2.2), so a copy of empty files never gets a total.
    """
    total = status.get("total") or 0
    if not isinstance(total, int | float) or total <= 0:
        return None
    if "processed_num" not in status and "processed_size" in status:
        size = int(status.get("processed_size") or 0)
        return size, int(total), f"{format_size(size)} of {format_size(int(total))}"
    processed = int(status.get("processed_num") or 0)
    return processed, int(total), f"{processed:,} of {int(total):,} items"


def _no_total_text(status: dict[str, Any]) -> str:
    """Progress wording while DSM reports no total (``progress: -1``)."""
    if "processed_num" not in status and "processed_size" in status:
        # CopyMove: no byte total yet, though it may already be copying (vdsm).
        return "no progress figures from DSM yet"
    return "counting items"


# A task ID begins with its start time in epoch seconds: in decimal on the real NAS
# (FileStation_1792248483...), in hex on vdsm 7.1 (FileStation_6AD3911E...).
_TASKID_EPOCH = (
    (re.compile(r"FileStation_(\d{10})"), 10),
    (re.compile(r"FileStation_([0-9A-Fa-f]{8})"), 16),
)
_MAX_TASK_AGE = 30 * 86400.0


def taskid_started(taskid: str, *, now: float | None = None) -> float | None:
    """When a task started, from the epoch seconds its ID begins with (decimal or hex).

    None for any other shape, or a time in the future or over 30 days old, so a
    decimal ID never passes as hex or the other way round. Not ``crtime``:
    ``BackgroundTask.list`` reported a 1 TB delete's ``crtime`` about 9 minutes after
    its start (real NAS, 2026-10-17).
    """
    now = time.time() if now is None else now
    for pattern, base in _TASKID_EPOCH:
        match = pattern.match(taskid)
        if match is not None:
            started = float(int(match.group(1), base))
            if now - _MAX_TASK_AGE <= started <= now + 60:
                return started
    return None


def _elapsed_text(seconds: float) -> str:
    """``under a minute``, ``14 min`` or ``2 h 5 min``."""
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def progress_text(status: dict[str, Any]) -> str:
    """Human-readable progress of a task status, e.g. ``8 of 342 items, 2%``."""
    numbers = _progress_numbers(status)
    if numbers is None:
        return _no_total_text(status)
    text = numbers[2]
    progress = status.get("progress")
    if isinstance(progress, int | float) and progress >= 0:
        text += f", {round(progress * 100)}%"
    return text


# How many 599s in a row before a task counts as lost (#521): about a second at the usual interval.
_LOST_TASK_READS = 3
_LOST_TASK_WAIT = 0.5
# Past this, no more reads, so slow 599s are less likely to outlast a client's reply limit, which
# cancels. It's checked after each read, so a slow read can still run past it.
_LOST_TASK_BOUND = 10.0


async def poll_task(
    client: DsmClient,
    *,
    api: str,
    version: int,
    taskid: str,
    wait: float,
    label: str,
    on_progress: ProgressCallback | None = None,
    interval: float = 0.5,
    progress_every: float = 2.0,
) -> PollResult:
    """Poll a File Station task until it finishes or ``wait`` seconds pass.

    A task still running at the deadline is handed off (left running, not stopped),
    and so is one whose status request gets no answer or an answer that can't be read
    (``stalled``: a transport error, since a DSM stalled by a huge delete is still
    working; an HTTP error status, which is how a reverse proxy reports that same stall
    (504) or a restart (502), #282; or an answer that can't be read; maintainer
    decision 2026-10-17). A DSM error code on a status request stops the task; so does
    cancellation, which then propagates. The exception is 599, "no such task", which isn't a
    verdict on a task this call just started: it's read again a few times, then reported as
    ``lost`` (#521). The deadline is wall-clock (#237).
    """
    quoted = dsm_str(taskid)
    deadline = time.monotonic() + wait
    status: dict[str, Any] = {}
    last_progress: float | None = None
    last_numbers: tuple[int, int] | None = None
    report = MonotonicProgress.wrap(on_progress)
    missing_reads = 0
    missing_since = 0.0
    last_missing: SynologyError | None = None

    async def stop() -> bool:
        return await _stop_background_task(client, api, quoted, version, logger)

    try:
        while time.monotonic() < deadline:
            asked = time.monotonic()
            try:
                reply: Any = await client.request(
                    api, "status", version=version, params={"taskid": quoted}
                )
                if not isinstance(reply, dict):
                    raise TypeError("status data is not an object")
                status = reply
                missing_reads = 0
            except (DsmHttpError, DsmReplyError, RequestTooLongError) as e:
                # Not DSM's verdict on the task: a proxy's 502/504 or login page, or a
                # DSM too busy to answer properly. Hand it off like a stall, never stop
                # it; the stop would go through the same broken path anyway (#282). A
                # status URL is short, so a 414 comes from a proxy too (#325).
                logger.warning(
                    "%s status (%s) failed, handing off: %s", api, label, type(e).__name__
                )
                return PollResult(status, "stalled", e)
            except SynologyError as e:
                if e.code != 599:
                    return PollResult(status, "poll_error", e, await stop())
                missing_reads += 1
                last_missing = e
                # At WARNING so CI's logs keep what's needed to find the cause (#521).
                logger.warning(
                    "%s status (%s) answered 599 for %s, read %d of %d, %s",
                    api,
                    label,
                    taskid,
                    missing_reads,
                    _LOST_TASK_READS,
                    "after a status" if status else "with no status yet",
                )
                if missing_reads == 1:
                    missing_since = asked
                if (
                    missing_reads >= _LOST_TASK_READS
                    or time.monotonic() - missing_since >= _LOST_TASK_BOUND
                ):
                    return PollResult(status, "lost", e)
                await asyncio.sleep(interval)
                continue
            except httpx.TransportError as e:
                # No answer: DSM is probably busy with this very task. Leave it running
                # and hand it off; never stop it (#245, maintainer decision 2026-10-17).
                logger.debug("%s status (%s) unanswered, handing off: %r", api, label, e)
                return PollResult(status, "stalled", e)
            except Exception as e:
                # Any other unexpected answer (a proxy's page is a DsmReplyError, above):
                # the task may well still be running, so hand it off like a stall rather
                # than lose it (QA R1 on #251).
                logger.warning(
                    "%s status (%s) unreadable, handing off: %s", api, label, type(e).__name__
                )
                return PollResult(status, "stalled", e)

            logger.debug("%s status (%s): %s", api, label, status)

            if status.get("finished", False):
                return PollResult(status, "finished")

            if report is not None:
                now = time.monotonic()
                if last_progress is None or now - last_progress >= progress_every:
                    numbers = _progress_numbers(status)
                    if numbers is not None:
                        processed, total, text = numbers
                        if (processed, total) != last_numbers:
                            last_progress = now
                            last_numbers = (processed, total)
                            await report(processed, total, f"{label}: {text}")
                    elif status.get("progress") == -1:
                        last_progress = now
                        await report(0, None, f"{label}: {_no_total_text(status)}")

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        # anyio cancel scopes (the MCP SDK's) are level-triggered: shield the stop or
        # it is canceled again at once and the NAS task is orphaned.
        with anyio.CancelScope(shield=True):
            await stop()
        raise
    # The wait ended during the re-reads: the path check and the reply follow what DSM last said.
    if missing_reads:
        return PollResult(status, "lost", last_missing)
    return PollResult(status, "handoff")


_PAST = {
    "deleting": "deleted",
    "copying": "copied",
    "moving": "moved",
    "restoring": "restored",
}


# This doesn't know what a finished call put there (#580), so this is never said flat.
SKIP_CAUTION = (
    "unless what's there came from this or an earlier call of yours: that would replace it, "
    "possibly with no copy in the recycle bin."
)


def where_it_went(path: str, dest_of: dict[str, str] | None, dest: str | None) -> str:
    """A source as a reply names it, with its destination if that isn't the task's own (#585)."""
    went = (dest_of or {}).get(path, dest)
    return f"{shown(path)} (to {shown(went)})" if went != dest and went else shown(path)


def skipped_line(
    removes_source: bool,
    skipped: list[str],
    landed: list[str] | None = None,
    *,
    dest_of: dict[str, str] | None = None,
    dest: str | None = None,
) -> str:
    """A skipped source isn't done, so it's no leftover from a move that worked (#585). DSM's skip
    on a folder means only some of its items: the rest land. So one named like what this call
    landed earlier at that destination, or like a skipped source before it, gets no advice to
    replace it (#580). ``dest_of`` says where each went, ``dest`` where the rest did."""

    def key(path: str) -> tuple[str | None, str]:
        return (dest_of or {}).get(path, dest), name_key(path)

    def named(path: str) -> str:
        return where_it_went(path, dest_of, dest)

    line = (
        "Skipped in whole or in part, as an item of that name is already at the destination "
        f"(not compared): {', '.join(map(named, dict.fromkeys(skipped)))}."
    )
    if removes_source:
        line += " A skipped file is still at the source; a skipped folder keeps the items skipped."
    ran = {key(path) for path in landed or []}
    own: list[str] = []
    others: list[str] = []
    for path in dict.fromkeys(skipped):
        (own if key(path) in ran else others).append(path)
        ran.add(key(path))
    if own:
        line += (
            f" Don't retry {', '.join(map(named, own))} with overwrite=true unless the user asked "
            "to replace that item: what's there may be what this call put there."
        )
    if others:
        line += f" {'For the others, pass' if own else 'Pass'} overwrite=true to replace them, "
        line += SKIP_CAUTION
    return line


def handoff_reply(
    *,
    verb: str,
    path: str,
    handle: Handle,
    status: dict[str, Any],
    done: list[str],
    not_started: list[str],
    stall: Exception | None = None,
    skipped: list[str] | None = None,
    landed: list[str] | None = None,
    dest: str | None = None,
    dest_of: dict[str, str] | None = None,
) -> str:
    """The reply for a task handed off after the wait: the handle and what to do next.

    ``verb`` is "Still deleting", "Still copying", "Still moving" or "Still restoring".
    ``stall``: the status request's error, for a task handed off because the NAS stopped
    answering, or its answer couldn't be read (#245, maintainer decision 2026-10-17), or it
    kept answering 599 (#521).
    ``dest``: where a copy, move or restore puts it, for a lost task's next step.
    The error's text is never shown, since it may quote the body: a transport error or
    any other exception shows its type, an HTTP error its status, and a non-DSM reply
    a fixed phrase.
    """
    gerund = verb.split()[-1].lower()
    past = _PAST.get(gerund, "done")
    lost = isinstance(stall, SynologyError) and stall.code == 599
    if lost:
        last = f" Last progress: {progress_text(status)}." if status else ""
        first = (
            f"DSM answered that it has no task {gerund} {shown(path)} (error 599), so how it ended "
            f"isn't known: it may have finished, failed or still be running.{last} It wasn't "
            f"stopped, so check before starting it again. {_RUNNING_HINT}"
        )
    elif stall is None:
        first = (
            f"{verb} on the NAS: {shown(path)} ({progress_text(status)}). This operation keeps "
            "running after this reply, so nothing more needs to be done to finish it."
        )
    else:
        last = f"; last progress: {progress_text(status)}" if status else ""
        if isinstance(stall, httpx.TransportError):
            what, why = "The NAS stopped answering", describe_transport_error(stall)
        elif isinstance(stall, (DsmHttpError, RequestTooLongError)):
            what = "The NAS, or a proxy in front of it, answered a status request with an error"
            why = f"HTTP {stall.status if isinstance(stall, DsmHttpError) else 414}"
        elif isinstance(stall, DsmReplyError):
            what = "The NAS's status reply couldn't be read"
            why = "not a DSM reply; a proxy or captive portal may be answering"
        else:
            what, why = "The NAS's status reply couldn't be read", type(stall).__name__
        first = (
            f"{what} while {gerund} {shown(path)} ({why}{last}). It's probably still running "
            "there; don't start it again."
        )
    lines = [
        first,
        f"Handle: `{handle}`",
        "To check on it, call file_task with this handle. To stop it, call file_task with "
        'action="stop" (it may then be partly done).',
    ]
    if done:
        named = ", ".join(where_it_went(p, dest_of, dest) for p in done)
        lines.append(f"Already {past}: {named}.")
    if skipped:
        lines.append(
            skipped_line(
                gerund in ("moving", "restoring"), skipped, landed, dest_of=dest_of, dest=dest
            )
        )
    if not_started:
        # A lost task never reports finishing, so "once this one finishes" couldn't be met.
        then = "Run them again once this one finishes."
        if lost and dest:
            then = f"First: {_where_to_look(gerund, dest, [path])} Then run them."
        elif lost:
            then = "Check this one first by listing its path, then run them."
        lines.append(f"Not started: {', '.join(map(shown, not_started))}. {then}")
    return "\n".join(lines)


@dataclass
class RunningTask:
    """A running Delete/CopyMove task of this user, as ``BackgroundTask.list`` shows it."""

    handle: Handle
    paths: list[str]
    params: dict[str, Any]
    task: dict[str, Any]

    @property
    def dest(self) -> str | None:
        dest = self.params.get("dest_folder_path")
        return _no_trailing_slash(dest) if isinstance(dest, str) else None

    def record(self) -> TaskRecord:
        """A task record from the task's start ``params``."""
        overwrite = self.params.get("overwrite")
        recursive = self.params.get("recursive")
        return TaskRecord(
            handle=self.handle,
            paths=list(self.paths),
            dest=self.dest,
            overwrite=overwrite if isinstance(overwrite, bool) else None,
            recursive=recursive if isinstance(recursive, bool) else None,
        )


def _no_trailing_slash(path: str) -> str:
    """A path as a task's start params may spell it, for comparing with a normalized one. A second
    guard: ``_list_running`` already strips the slash, and this server's own records are
    normalized, but a record built another way still compares right."""
    return path.rstrip("/") or "/"


def _in_recycle_bin(path: str) -> bool:
    # The one bin rule (#510): /homes/<user>/#recycle is a bin too.
    from mcp_synology.modules.filestation.paths import in_bin

    return in_bin(path)


def _task_kind(api: Any, params: dict[str, Any], paths: list[str]) -> HandleKind | None:
    """The handle kind of a listed task: from its API, ``remove_src`` and source paths."""
    if api == "SYNO.FileStation.Delete":
        return "delete"
    if api != "SYNO.FileStation.CopyMove":
        return None
    if params.get("remove_src") is not True:
        return "copy"
    return "restore" if any(_in_recycle_bin(p) for p in paths) else "move"


async def _list_running(client: DsmClient) -> list[RunningTask]:
    """This user's running Delete/CopyMove tasks, from ``SYNO.FileStation.BackgroundTask``
    ``list`` v2, which shows each task's start ``params`` (``path`` as a list; CopyMove
    also ``dest_folder_path``, ``overwrite``, ``remove_src`` as JSON booleans; vdsm 7.1, 7.2.2,
    7.3.2 and 7.4.1). An unexpected reply shape counts as nothing running. Raises DSM and transport
    errors.
    """
    data = await client.request("SYNO.FileStation.BackgroundTask", "list", version=2)
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        logger.debug("BackgroundTask list reply has no task list: %r", data)
        return []
    running: list[RunningTask] = []
    for task in tasks:
        if not isinstance(task, dict) or task.get("finished"):
            continue
        params = task.get("params")
        taskid = task.get("taskid")
        if not isinstance(params, dict) or not isinstance(taskid, str):
            continue
        paths = params.get("path")
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list) or not _TASKID_RE.match(taskid):
            continue
        # Another client may send /s/b/, which is the source this server calls /s/b.
        paths = [p.rstrip("/") or "/" for p in paths if isinstance(p, str)]
        dest = params.get("dest_folder_path")
        if isinstance(dest, str):
            # Another client may send /d/, which is the folder this server calls /d.
            params = {**params, "dest_folder_path": dest.rstrip("/") or "/"}
            task = {**task, "params": params}
        kind = _task_kind(task.get("api"), params, paths)
        if kind is None:
            continue
        running.append(RunningTask(Handle(kind, taskid), paths, params, task))
    return running


async def running_task(
    client: DsmClient,
    *,
    api: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """This user's running (unfinished) task of ``api`` on ``path``, as DSM lists it.

    ``params``, if given, must match the task's start params too. None if there is
    none, or if ``list`` fails: the check is a guard, never a reason to refuse an
    operation.
    """
    try:
        tasks = await _list_running(client)
    except (SynologyError, httpx.TransportError) as e:
        logger.debug("BackgroundTask list failed, skipping the running-task check: %s", e)
        return None
    return find_running(tasks, api=api, path=path, params=params)


def find_running(
    tasks: list[RunningTask],
    *,
    api: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The task of ``api`` on ``path`` among ``tasks``, as ``running_task`` looks for it."""
    for task in tasks:
        if task.handle.api != api or path not in task.paths:
            continue
        if params and any(task.params.get(k) != v for k, v in params.items()):
            continue
        return task.task
    return None


_LANDINGS: weakref.WeakKeyDictionary[DsmClient, dict[str, RunningTask]] = (
    weakref.WeakKeyDictionary()
)


# The tool call this task runs in: its tracker and the taskids it started, so nested calls
# share them and its own tasks aren't a clash for its own later sources.
_TRACKING: contextvars.ContextVar[tuple[Callable[[RunningTask], None], list[str]] | None] = (
    contextvars.ContextVar("tracking", default=None)
)


@contextmanager
def tracked_landings(client: DsmClient) -> Iterator[Callable[[RunningTask], None]]:
    """Keeps the tasks one copy, move or restore call starts where a parallel call on this client
    finds them, until this call returns (#597). DSM's list can't serve for this: a small move has
    usually finished by the time a parallel call reads it (probe on vdsm, 38 of 40 parallel moves
    both ran, on all four versions), and a finished task isn't listed as running. Yields the
    function that records a task, to call with the destination's lock held."""
    outer = _TRACKING.get()
    if outer is not None:
        # Inside a call that tracks already (a restore to several places runs one per item):
        # its tasks stay until that call returns.
        yield outer[0]
        return
    landings = _LANDINGS.setdefault(client, {})
    mine: list[str] = []

    def track(task: RunningTask) -> None:
        landings[task.handle.taskid] = task
        mine.append(task.handle.taskid)

    token = _TRACKING.set((track, mine))
    try:
        yield track
    finally:
        _TRACKING.reset(token)
        records = [landings.pop(taskid, None) for taskid in mine]
        # A source this call never started won't land: a parallel call that read the record
        # mid-list mustn't be refused for it (QA round 3 on #597). A started one keeps its
        # record in that snapshot, as its task may have finished just now.
        started = {
            path
            for task in records
            if task is not None and not task.task.get("pending")
            for path in task.paths
        }
        for task in records:
            if task is not None and task.task.get("pending") and not set(task.paths) & started:
                task.task["withdrawn"] = True


_PENDING_IDS = itertools.count(1)


def pending_landing(
    kind: HandleKind, path: str, dest: str, *, overwrite: bool, remove_src: bool
) -> RunningTask:
    """What a call is about to land, recorded before it starts any task (QA round 2 on #597):
    a parallel call that starts first must not land the same name where this one will. Its
    handle names no DSM task."""
    return RunningTask(
        Handle(kind, f"pending-{next(_PENDING_IDS)}"),
        [path],
        {"dest_folder_path": dest, "overwrite": overwrite, "remove_src": remove_src},
        {"local": True, "pending": True},
    )


async def tasks_for_landing_check(client: DsmClient) -> tuple[list[RunningTask], list[RunningTask]]:
    """This user's running tasks as DSM lists them, and every task the check should look at: those
    plus the ones parallel calls on this client have started. If ``list`` fails DSM's part is
    empty: a guard never refuses the call (#587), but it did not run on them, so this logs a
    WARNING, and the tool descriptions say it can happen. The parallel calls' tasks are
    read first: one can finish while ``list`` is answered."""
    # This call's own tasks aren't a clash for its own sources: those are refuse_same_names'.
    current = _TRACKING.get()
    own = set(current[1]) if current is not None else set()
    local = [t for t in _LANDINGS.get(client, {}).values() if t.handle.taskid not in own]
    try:
        listed = await _list_running(client)
    except (SynologyError, httpx.TransportError) as e:
        logger.warning(
            "BackgroundTask list failed, leaving DSM's tasks out of the same-landing check "
            "(this server's own calls are still checked): %s",
            e,
        )
        listed = []
    known = {task.handle.taskid for task in listed}
    local = [task for task in local if not task.task.get("withdrawn")]
    return listed, listed + [task for task in local if task.handle.taskid not in known]


def running_landing(
    tasks: list[RunningTask], *, sources: list[str], dest: str, overwrite: bool, remove_src: bool
) -> tuple[str, str, RunningTask] | None:
    """A source of this call, the path it clashes with and this user's running copy, move or
    restore that would land both in ``dest`` under one name, where either one replaces (#587): the
    later task would replace what the earlier landed, and for a move the original is already gone.
    Names compare as ``refuse_same_names`` does. The same source under the same operation is left
    out: that's a second run of it, which ``find_running`` hands back. A delete has no ``dest``, so
    it never matches. What a finished call put there can't be known here."""
    for task in tasks:
        if task.dest != dest:
            continue
        if not overwrite and task.params.get("overwrite") is not True:
            continue
        same_operation = task.params.get("remove_src") is remove_src
        for source in sources:
            key = name_key(source)
            for path in task.paths:
                if name_key(path) == key and (path != source or not same_operation):
                    return source, path, task
    return None


async def running_task_for(client: DsmClient, *, api: str, path: str) -> str | None:
    """The taskid of this user's running task of ``api`` on ``path``, or None."""
    task = await running_task(client, api=api, path=path)
    return str(task["taskid"]) if task else None


_GERUND: dict[HandleKind, str] = {
    "delete": "deleting",
    "copy": "copying",
    "move": "moving",
    "restore": "restoring",
}
_LIST_HINT = "List the path to see what's left."
_RUNNING_HINT = "Call file_task with no handle to see what's running."
_UNAVAILABLE_CODES = (102, 103, 104)


def _where_to_look(gerund: str, dest: str, sources: list[str]) -> str:
    """Where a lost copy, move or restore can be checked: a folder appears at the destination as
    soon as it starts, so what's there may not be all of it (QA round 3 on #590)."""
    moved = gerund in ("moving", "restoring")
    source = f"{', '.join(map(shown, sources))} and " if moved else ""
    return (
        f"List {source}{shown(dest)} to see what's there; a folder there may be only partly "
        f"{_PAST.get(gerund, 'done')}."
    )


def _describe(kind: HandleKind, paths: list[str], dest: str | None) -> str:
    """``deleting /s/a`` or ``moving /s/b to /s/d/``."""
    where = ", ".join(map(shown, paths)) or "(path unknown)"
    to = f" to {shown(f'{dest}/')}" if dest and kind != "delete" else ""
    return f"{_GERUND[kind]} {where}{to}"


@dataclass
class DiscoveryNote:
    """The one-time note about tasks found running at a process's first connection."""

    lines: dict[str, str]  # handle -> "  deleting /s/a (8 of 342 items, 2%), handle `...`"

    def text(self, *, skip: str | None = None) -> str | None:
        """The note, leaving out ``skip`` (the handle a file_task reply is about); None
        when nothing is left."""
        rest = [line for handle, line in self.lines.items() if handle != skip]
        if not rest:
            return None
        return "\n".join(
            [
                f"Note: {len(rest)} operation(s) still running on the NAS:",
                *rest,
                "Use file_task to check on them or stop them.",
            ]
        )


async def discover_running_tasks(client: DsmClient, records: TaskRecords) -> DiscoveryNote | None:
    """Seed ``records`` from this user's running tasks; the one-time note, or None.

    Run once at the first connection of a server process, so a later file_task status
    after a restart gives the full result. Any failure is logged at DEBUG and ignored.
    """
    try:
        tasks = await _list_running(client)
        if not tasks:
            return None
        lines: dict[str, str] = {}
        for task in tasks:
            records.add(task.record())
            lines[str(task.handle)] = (
                f"  {_describe(task.handle.kind, task.paths, task.dest)} "
                f"({progress_text(task.task)}), handle `{task.handle}`"
            )
    except Exception as e:
        # Malformed task data included: discovery must never fail a tool call.
        logger.debug("Running-task discovery failed, ignoring: %s", e)
        return None
    return DiscoveryNote(lines)


async def _list_reply(client: DsmClient) -> str:
    try:
        tasks = await _list_running(client)
    except SynologyError as e:
        if e.code in _UNAVAILABLE_CODES:
            return (
                "Listing running tasks isn't available on this DSM version. Use the handle "
                "from the delete, copy, move or restore reply to check on a task."
            )
        synology_error_response("List running tasks", e)
    if not tasks:
        return "No File Station delete, copy or move tasks are running for this user."
    rows = []
    for task in tasks:
        start = taskid_started(task.handle.taskid)
        started = format_timestamp(start) if start is not None else "unknown"
        to = f" -> {shown(f'{task.dest}/')}" if task.dest and task.handle.kind != "delete" else ""
        rows.append(
            [
                f"`{task.handle}`",
                task.handle.kind,
                ", ".join(map(shown, task.paths)) + to,
                started,
                progress_text(task.task),
            ]
        )
    return format_table(
        ["Handle", "Operation", "Path(s)", "Started", "Progress"],
        rows,
        title=f"Running File Station tasks ({len(rows)})",
    )


async def _final_text(
    client: DsmClient, handle: Handle, status: dict[str, Any], record: TaskRecord | None
) -> str:
    """A finished task's result: the original tool's text when the record allows it."""
    from mcp_synology.modules.filestation.operations import (  # local: operations imports tasks
        _as_restore,
        _copy_move_result,
        _copy_move_summary,
        _delete_result,
        _delete_summary,
    )

    if record is not None and len(record.paths) == 1:
        path = record.paths[0]
        if handle.kind == "delete":
            recursive = record.recursive if record.recursive is not None else True
            _delete_result(status, path, recursive=recursive)
            return await _delete_summary(client, record.paths, record.recycle)
        if record.dest is not None:
            operation = handle.kind.capitalize()
            size, skipped = _copy_move_result(
                status,
                path,
                operation,
                record.done_earlier,
                dest=record.dest,
                skipped_so_far=record.skipped_earlier,
                dest_of=record.dest_of,
                landed=record.landed_earlier,
            )
            text = await _copy_move_summary(
                client,
                record.paths,
                record.dest,
                handle.kind != "copy",
                size,
                [path] if skipped else [],
                # A skipped folder lands its other items, so it warns like one that landed (#585).
                earlier=[
                    *record.earlier,
                    *(
                        p
                        for p in [*record.done_earlier, *record.skipped_earlier]
                        if record.dest_of.get(p, record.dest) == record.dest
                    ),
                ],
                # A record can't know what else the call left in that folder: a source skipped,
                # one not started, or everything before a restart.
                only_named_sources_removed=True,
            )
            if record.done_earlier:
                earlier_done = ", ".join(
                    where_it_went(p, record.dest_of, record.dest) for p in record.done_earlier
                )
                past = _PAST[_GERUND[handle.kind]]
                text += f"\n\nAlready {past} before this task: {earlier_done}."
            if record.skipped_earlier:
                line = skipped_line(
                    handle.kind != "copy",
                    record.skipped_earlier,
                    record.landed_earlier,
                    dest_of=record.dest_of,
                    dest=record.dest,
                )
                text += f"\n\nBefore this task: {line}"
                # A result with skips starts with [!], as the first line is what gets read.
                if text.startswith("[+]"):
                    text = "[!]" + text.removeprefix("[+]")
            if handle.kind == "restore":
                text = _as_restore(text, path.split("/")[1])
            return text
    return _generic_final(handle, status)


def _generic_final(handle: Handle, status: dict[str, Any]) -> str:
    """A finished task's result without a record: what DSM's final status says."""
    if handle.kind == "delete":
        done = f"{int(status.get('processed_num') or 0):,} items processed"
    else:
        done = f"{format_size(int(status.get('processed_size') or 0))} processed"
    from mcp_synology.modules.filestation.operations import (  # local: operations imports tasks
        _code_text,
        _partial_note,
    )

    lines = [f"{handle.kind.capitalize()} finished: {done}."]
    # Codes kept as DSM sent them: this report has to survive any status (QA r1 on #366).
    failed: list[tuple[str, object]] = []
    error = status.get("error")
    if error:
        code = error.get("code", 0) if isinstance(error, dict) else error
        failed.append((str(status.get("path") or "task"), code))
    errors = status.get("errors")
    if isinstance(errors, list):
        failed.extend(
            (str(e.get("path") or "item"), e.get("code", 0)) for e in errors if isinstance(e, dict)
        )
    if failed:
        text = "; ".join(f"{shown(p)} ({_code_text(c)})" for p, c in failed)
        if handle.kind != "delete":
            # No record of the source here, so a 408 can't be told apart: say it anyway.
            text += _partial_note(status, failed, handle.kind.capitalize(), None, None)
        lines.append("DSM reported errors: " + text + ".")
    skip = status.get("skipstatus")
    skip_value = (skip.get("status") or "unseen") if isinstance(skip, dict) else skip
    if skip_value not in (None, "", "none"):
        lines.append(
            "Some items were skipped (that name is already at the destination, overwrite=false)."
        )
    lines.append("List the path to see the details.")
    return "\n".join(lines)


_NOT_STOPPED = "That task had already finished, so nothing was stopped."


def _unanswered(
    handle: Handle, exc: httpx.TransportError, *, operation: str, request: str, note: str
) -> NoReturn:
    """Raise the structured error for a status or stop request DSM didn't answer (#237)."""
    timed_out = isinstance(exc, httpx.TimeoutException)
    what = "didn't answer" if timed_out else "lost the connection during"
    error_response(
        ErrorCode.TIMEOUT if timed_out else ErrorCode.UNAVAILABLE,
        f"{operation} failed: DSM {what} the {request} request for `{handle}` "
        f"({describe_transport_error(exc)}).{note}",
        retryable=True,
        param="handle",
        value=str(handle),
        suggestion=(
            "Call file_task with this handle to see whether the task is still running, "
            "or list the path to see what's left."
        ),
    )


def _unreadable(
    handle: Handle,
    exc: DsmHttpError | DsmReplyError | RequestTooLongError,
    *,
    operation: str,
    note: str,
) -> NoReturn:
    """Raise the structured error for a status or stop request answered with an HTTP error
    status or something that isn't a DSM reply (a proxy's 502 or login page, #282). A 414
    comes from a proxy too: a status or stop URL is short, so its own suggestion (make the
    request smaller) doesn't apply (#325)."""
    follow_up = (
        "Call file_task with this handle to see whether the task is still running, or list "
        "the path to see what's left."
    )
    hint = None if isinstance(exc, RequestTooLongError) else exc.suggestion
    error_response(
        ErrorCode.UNAVAILABLE,
        f"{operation} failed for `{handle}`: {exc}{note}",
        retryable=exc.retryable,
        param="handle",
        value=str(handle),
        suggestion=f"{hint} {follow_up}" if hint else follow_up,
    )


async def file_task(
    client: DsmClient,
    *,
    records: TaskRecords,
    handle: str | None = None,
    action: Literal["status", "stop"] = "status",
) -> str:
    """List, follow up on, or stop a handed-off delete/copy/move/restore (#245).

    Without a handle: this user's running Delete/CopyMove tasks. With one: its
    progress or final result (``status``), or stop it (``stop``). The handle is
    validated before any DSM call; its prefix picks the API. ``stop`` without a handle
    is ``invalid_parameter`` (it never lists or stops anything). ``stop`` reads the status
    first and stops only a task that is still running, or one DSM keeps answering 599 for,
    which can't be confirmed: CopyMove ``stop`` answers success for any taskid, so its answer
    alone can't say whether anything was stopped.
    """
    if action == "stop" and (handle is None or not handle.strip()):
        error_response(
            ErrorCode.INVALID_PARAMETER,
            "Stopping needs a handle; nothing was stopped.",
            retryable=False,
            param="handle",
            value=handle,
            suggestion=(
                "Call file_task without a handle to list the running operations and their "
                'handles, then call it again with the handle and action="stop".'
            ),
        )
    if handle is None:
        return await _list_reply(client)
    parsed = parse_handle(handle)
    record = records.get(parsed)
    version = min(2, client.negotiate_version(parsed.api, max_version=2))
    taskid = dsm_str(parsed.taskid)
    paths = record.paths if record is not None else []

    # A stop ends by STOP_DEADLINE from here, however slow DSM is (#630); a status check
    # keeps the client's own timeout.
    deadline = time.monotonic() + STOP_DEADLINE if action == "stop" else None

    def time_left() -> float:
        return float("inf") if deadline is None else deadline - time.monotonic()

    def wait_left() -> float:
        """A request's read timeout during a stop: STOP_WAIT, or less as the deadline nears."""
        return max(1.0, min(STOP_WAIT, time_left()))

    async def final_in_time(status: dict[str, Any]) -> str:
        """``_final_text`` within what's left of a stop's deadline (QA round 1 on #632): its
        summary can read the destination or the recycle bin, at the client's default timeout.
        Out of time, the reply is what the status says, and a later file_task gives the rest
        from the record."""
        if deadline is None or record is None or len(record.paths) != 1:
            return await _final_text(client, parsed, status, record)  # no request without one
        left = time_left()
        if left >= 1:
            try:
                return await asyncio.wait_for(_final_text(client, parsed, status, record), left)
            except TimeoutError:
                logger.debug("Final result of %s not read within the stop's deadline", parsed)
        return (
            f"{_generic_final(parsed, status)}\n(DSM was too slow to check the details, such as "
            "what's at the destination or in the recycle bin: file_task with this handle shows "
            "them.)"
        )

    async def status_or_none(stop_note: str = " Nothing was stopped.") -> dict[str, Any] | None:
        """The task's status, or None when DSM keeps saying it doesn't have it (599). One 599 isn't
        a verdict (#521): a stop dropped on it would leave a running delete going. Once a final
        result is remembered it is: DSM forgets a task after one read, and the result can't change.
        """
        reads = 1 if record is not None and record.final_status is not None else _LOST_TASK_READS
        started = time.monotonic()
        for read in range(reads):
            try:
                data: dict[str, Any] = await client.request(
                    parsed.api,
                    "status",
                    version=version,
                    params={"taskid": taskid},
                    read_timeout=wait_left() if deadline is not None else None,
                )
                if not isinstance(data, dict):
                    # Not "no such task", which sends a stop: it's a reply that can't be read.
                    raise DsmReplyError(f"{parsed.api} status: the reply's data isn't an object.")
            except (DsmHttpError, DsmReplyError, RequestTooLongError) as e:
                stopping = action == "stop"
                _unreadable(
                    parsed,
                    e,
                    operation="Stop task" if stopping else "Check task",
                    note=stop_note if stopping else "",
                )
            except SynologyError as e:
                if e.code != 599:
                    synology_error_response("Stop task" if action == "stop" else "Check task", e)
                if (
                    read + 1 >= reads
                    or time.monotonic() - started >= _LOST_TASK_BOUND
                    or time_left() < _LOST_TASK_WAIT + 1
                ):
                    return None
                await asyncio.sleep(_LOST_TASK_WAIT)
                continue
            except httpx.TransportError as e:
                stopping = action == "stop"
                _unanswered(
                    parsed,
                    e,
                    operation="Stop task" if stopping else "Check task",
                    request="status",
                    note=stop_note if stopping else "",
                )
            return data
        return None

    def gone(*, stop_sent: bool = False) -> str:
        # A stop request must hear that this call can't say it worked ("or been stopped"
        # alone reads as "your stop worked").
        sent = " A stop was sent, but DSM can't confirm it." if stop_sent else ""
        if paths:
            # Ours, but DSM says it doesn't have it: a finished Delete is forgotten unless a status
            # read caught it finishing, and a 599 can pass (#521), so it may still be running.
            hint = _LIST_HINT
            if record is not None and record.dest and parsed.kind != "delete":
                hint = _where_to_look(_GERUND[parsed.kind], record.dest, paths)
            return (
                f"DSM has no task for the {parsed.kind} of {', '.join(map(shown, paths))}: it "
                f"may have finished, failed, been stopped or still be running.{sent} "
                f"{_RUNNING_HINT} {hint}"
            )
        return (
            "DSM has no task with this handle: it may have finished, failed, been stopped or still "
            f"be running, or have been started by a different DSM user.{sent} {_RUNNING_HINT} "
            f"{_LIST_HINT}"
        )

    def earlier_skip() -> str:
        if record is None or not (record.done_earlier or record.skipped_earlier):
            return ""
        lines = []
        if record.done_earlier:
            done = ", ".join(
                where_it_went(p, record.dest_of, record.dest) for p in record.done_earlier
            )
            lines.append(f"Already {_PAST[_GERUND[parsed.kind]]} before this task: {done}.")
        if record.skipped_earlier:
            lines.append(
                skipped_line(
                    parsed.kind != "copy",
                    record.skipped_earlier,
                    record.landed_earlier,
                    dest_of=record.dest_of,
                    dest=record.dest,
                )
            )
        return "\n" + "\n".join(lines)

    async def send_stop(*, seen_running: bool) -> int | str | None:
        """Send the stop. DSM's code when it says it has no such task (401, 599), the reply
        when DSM didn't answer within ``STOP_WAIT`` (the status decides it, #630), or None."""
        try:
            await client.request(
                parsed.api,
                "stop",
                version=version,
                params={"taskid": taskid},
                read_timeout=wait_left(),
            )
        except httpx.ReadTimeout:
            return await unanswered_stop(seen_running)
        except DsmHttpError as e:
            if e.status in (502, 504):
                # A proxy giving up on the stop before DSM answered: the same slow stop (#630).
                return await unanswered_stop(seen_running)
            _unreadable(
                parsed, e, operation="Stop task", note=" The stop may or may not have taken effect."
            )
        except (DsmReplyError, RequestTooLongError) as e:
            # A 414 is rejected before anything acts on it: nothing was stopped.
            sent = not isinstance(e, RequestTooLongError)
            _unreadable(
                parsed,
                e,
                operation="Stop task",
                note=" The stop may or may not have taken effect."
                if sent
                else " Nothing was stopped.",
            )
        except SynologyError as e:
            if e.code in (401, 599):
                return e.code
            synology_error_response("Stop task", e)
        except httpx.TransportError as e:
            _unanswered(
                parsed,
                e,
                operation="Stop task",
                request="stop",
                note=" The stop may or may not have taken effect.",
            )
        return None

    async def unanswered_stop(seen_running: bool) -> str:
        """DSM didn't answer the stop in time; the stop usually took effect at once (#630), so the
        task's status says what happened. ``seen_running``: a read just before the stop showed it
        running, so its being gone now is the stop working; otherwise it was gone before.

        As in ``status_or_none``, one 599 isn't a verdict (#521): it's read again, a few times
        within ``_LOST_TASK_BOUND``. Each read waits at most ``STOP_WAIT``, so the whole stop
        stays under an MCP client's 60 s limit."""
        what = ", ".join(map(shown, paths)) if paths else f"`{parsed}`"
        now: dict[str, Any] | None = None
        unknown = ""  # why the status couldn't be read, when it couldn't
        started = time.monotonic()
        for read in range(_LOST_TASK_READS):
            if time_left() < 1:
                unknown = "there was no time left to check its status"
                break
            try:
                data = await client.request(
                    parsed.api,
                    "status",
                    version=version,
                    params={"taskid": taskid},
                    read_timeout=wait_left(),
                )
                if not isinstance(data, dict):
                    raise DsmReplyError(f"{parsed.api} status: the reply's data isn't an object.")
                now = data
                break
            except (DsmHttpError, DsmReplyError, RequestTooLongError) as e:
                logger.debug("Status after an unanswered stop of %s: %s", parsed, e)
                unknown = "DSM's answer to a status check couldn't be read"
                break
            except SynologyError as e:
                if e.code != 599:
                    logger.debug("Status after an unanswered stop of %s: %s", parsed, e)
                    unknown = f"a status check got DSM error {e.code}"
                    break
                # The deadline ending counts like the reread bound: no time for another read.
                if (
                    read + 1 >= _LOST_TASK_READS
                    or time.monotonic() - started >= _LOST_TASK_BOUND
                    or time_left() < _LOST_TASK_WAIT + 1
                ):
                    break
                await asyncio.sleep(_LOST_TASK_WAIT)
            except httpx.TransportError as e:
                logger.debug("Status after an unanswered stop of %s: %s", parsed, e)
                unknown = "DSM didn't answer a status check either"
                break
        if now is None and not unknown:
            # DSM keeps saying it has no such task.
            if not seen_running:
                return gone(stop_sent=True) + earlier_skip()
            return (
                f"Stopped {parsed.kind} of {what}; it may be partly done. DSM was slow to answer "
                f"the stop, but the task is gone. {_LIST_HINT}{earlier_skip()}"
            )
        if now is None:
            return (
                f"A stop was sent for the {parsed.kind} of {what}, but DSM was slow to answer it "
                f"and {unknown}, so whether it stopped isn't known. Check with file_task and this "
                f"handle in a minute.{earlier_skip()}"
            )
        if now.get("finished"):
            records.set_final(parsed, now)
            final = await final_in_time(now)
            return (
                "A stop was sent and DSM was slow to answer it; the task ended meanwhile, so it "
                f"may have stopped partway: check what's left.\n{final}"
            )
        return (
            f"A stop was sent for the {parsed.kind} of {what}, but DSM hasn't confirmed it "
            f"yet: the task still shows as running ({progress_text(now)}). Check with "
            f"file_task and this handle in a minute before doing anything else with it; it "
            f"usually ends within a minute.{earlier_skip()}"
        )

    async def after_refusal(refused: int) -> str:
        """DSM says it has no such task, which can pass too, so a read says what's known."""
        now = await status_or_none(" The stop wasn't accepted.")
        if now is None:
            return gone(stop_sent=True) + earlier_skip()
        if now.get("finished"):
            records.set_final(parsed, now)
            final = await final_in_time(now)
            return f"{_NOT_STOPPED}\n{final}"
        return (
            f"DSM didn't accept the stop (error {refused}), and the task is still running "
            f"({progress_text(now)}). Try the stop again; if DSM keeps refusing it, stop it in "
            f"DSM's File Station.{earlier_skip()}"
        )

    status = await status_or_none()

    if action == "stop":
        # Read the status first: CopyMove stop answers success for any taskid, finished,
        # forgotten or another user's (vdsm 7.1 and 7.2.2), so stop alone can't tell.
        if status is None:
            if record is not None and record.final_status is not None:
                final = await final_in_time(record.final_status)
                return f"{_NOT_STOPPED}\n{final}"
            # The reads may be wrong, and a task ID is unique: a stop that's lost costs nothing
            # (another user's task isn't touched, on vdsm), and one that's not sent leaves a delete
            # running (QA round 2 on #590).
            refused = await send_stop(seen_running=False)
            if isinstance(refused, str):
                return refused
            if refused is None:
                return gone(stop_sent=True) + earlier_skip()
            return await after_refusal(refused)
        logger.debug("%s status before stop (%s): %s", parsed.api, parsed, status)
        if status.get("finished"):
            records.set_final(parsed, status)
            final = await final_in_time(status)
            return f"{_NOT_STOPPED}\n{final}"
        refused = await send_stop(seen_running=True)
        if isinstance(refused, str):
            return refused
        if refused is not None:
            return await after_refusal(refused)
        what = ", ".join(map(shown, paths)) if paths else f"`{parsed}`"
        stopped = f"Stopped {parsed.kind} of {what}; it may be partly done. {_LIST_HINT}"
        return stopped + earlier_skip()

    if status is None:
        if record is not None and record.final_status is not None:
            return await _final_text(client, parsed, record.final_status, record)
        return gone() + earlier_skip()
    logger.debug("%s status (%s): %s", parsed.api, parsed, status)

    if status.get("finished"):
        records.set_final(parsed, status)
        return await _final_text(client, parsed, status, record)

    dest = record.dest if record is not None else None
    where = _describe(parsed.kind, paths, dest).partition(" ")[2] if paths else ""
    what = f"{_GERUND[parsed.kind]} on the NAS" + (f": {where}" if where else "")
    progress = progress_text(status)
    started = taskid_started(parsed.taskid)
    if started is not None:
        progress += f"; running for {_elapsed_text(time.time() - started)}"
    slow = (
        "Progress counts finished items, and one large file can take many minutes on a "
        "busy NAS, so a count that hasn't moved doesn't mean it's stuck."
        if parsed.kind == "delete"
        else "A busy NAS can go minutes without visible progress; that doesn't mean it's stuck."
    )
    return (
        f"Still {what} ({progress}). Handle: `{parsed}`\n"
        f"{slow} It keeps running without anything more from you; stopping and starting it "
        "again doesn't make it faster. "
        "Check again later, or, if the user wants it canceled, call file_task with "
        f'action="stop" (it may then be partly done).{earlier_skip()}'
    )
