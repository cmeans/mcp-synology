"""File Station task handles and the in-memory records behind them."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
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
from mcp_synology.modules.filestation.helpers import describe_transport_error, dsm_str

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)

HandleKind = Literal["delete", "copy", "move", "restore"]

_KINDS: tuple[str, ...] = get_args(HandleKind)
_TASKID_RE = re.compile(r"^[A-Za-z0-9_]{1,80}$")
_DECORATION = " \t\r\n`'\"*.,;:()[]<>"
# DSM 7.1 sometimes answers a Delete stop only after ~60 s, although the stop takes
# effect at once; the default read timeout would report a working stop as unanswered.
# Read timeout only: connect, write and pool keep the client's default.
STOP_TIMEOUT = 90.0


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
        self._items.pop(key, None)
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
            api, "stop", version=version, params={"taskid": taskid}, read_timeout=STOP_TIMEOUT
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
    """

    status: dict[str, Any]
    outcome: Literal["finished", "handoff", "poll_error", "stalled"]
    error: Exception | None = None
    stopped: bool = False


@dataclass
class Handoff:
    """A task left running on the NAS: its handle, last status, and the stall if any.

    ``stall`` is the status request's error: a ``TransportError`` (no answer) or any
    other exception (an answer that couldn't be read).
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
    decision 2026-10-17). Only a DSM error code on a status request stops the task; so
    does cancellation, which then propagates. The deadline is wall-clock (#237).
    """
    quoted = dsm_str(taskid)
    deadline = time.monotonic() + wait
    status: dict[str, Any] = {}
    last_progress: float | None = None
    last_numbers: tuple[int, int] | None = None
    report = MonotonicProgress.wrap(on_progress)

    async def stop() -> bool:
        return await _stop_background_task(client, api, quoted, version, logger)

    try:
        while time.monotonic() < deadline:
            try:
                reply: Any = await client.request(
                    api, "status", version=version, params={"taskid": quoted}
                )
                if not isinstance(reply, dict):
                    raise TypeError("status data is not an object")
                status = reply
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
                return PollResult(status, "poll_error", e, await stop())
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
    return PollResult(status, "handoff")


_PAST = {
    "deleting": "deleted",
    "copying": "copied",
    "moving": "moved",
    "restoring": "restored",
}


def handoff_reply(
    *,
    verb: str,
    path: str,
    handle: Handle,
    status: dict[str, Any],
    done: list[str],
    not_started: list[str],
    stall: Exception | None = None,
) -> str:
    """The reply for a task handed off after the wait: the handle and what to do next.

    ``verb`` is "Still deleting", "Still copying", "Still moving" or "Still restoring".
    ``stall``: the status request's error, for a task handed off because the NAS stopped
    answering, or its answer couldn't be read (#245, maintainer decision 2026-10-17).
    The error's text is never shown, since it may quote the body: a transport error or
    any other exception shows its type, an HTTP error its status, and a non-DSM reply
    a fixed phrase.
    """
    gerund = verb.split()[-1].lower()
    past = _PAST.get(gerund, "done")
    if stall is None:
        first = (
            f"{verb} on the NAS: {path} ({progress_text(status)}). This operation keeps "
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
            f"{what} while {gerund} {path} ({why}{last}). It's probably still running "
            "there; don't start it again."
        )
    lines = [
        first,
        f"Handle: `{handle}`",
        "To check on it, call file_task with this handle. To stop it, call file_task with "
        'action="stop" (it may then be partly done).',
    ]
    if done:
        lines.append(f"Already {past}: {', '.join(done)}.")
    if not_started:
        lines.append(
            f"Not started: {', '.join(not_started)}. Run them again once this one finishes."
        )
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
        return dest if isinstance(dest, str) else None

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


def _in_recycle_bin(path: str) -> bool:
    parts = path.split("/")
    return len(parts) > 2 and parts[2] == "#recycle"


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
    also ``dest_folder_path``, ``overwrite``, ``remove_src`` as JSON booleans; vdsm 7.1 and
    7.2.2). An unexpected reply shape counts as nothing running. Raises DSM and transport
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
        paths = [p for p in paths if isinstance(p, str)]
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
    for task in tasks:
        if task.handle.api != api or path not in task.paths:
            continue
        if params and any(task.params.get(k) != v for k, v in params.items()):
            continue
        return task.task
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
_UNAVAILABLE_CODES = (102, 103, 104)


def _describe(kind: HandleKind, paths: list[str], dest: str | None) -> str:
    """``deleting /s/a`` or ``moving /s/b to /s/d/``."""
    where = ", ".join(paths) or "(path unknown)"
    to = f" to {dest}/" if dest and kind != "delete" else ""
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
        to = f" -> {task.dest}/" if task.dest and task.handle.kind != "delete" else ""
        rows.append(
            [
                f"`{task.handle}`",
                task.handle.kind,
                ", ".join(task.paths) + to,
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
            size, skipped = _copy_move_result(status, path, operation, dest=record.dest)
            text = await _copy_move_summary(
                client,
                record.paths,
                record.dest,
                handle.kind != "copy",
                size,
                [path] if skipped else [],
            )
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
    failed: list[tuple[str, int]] = []
    error = status.get("error")
    if error:
        code = error.get("code", 0) if isinstance(error, dict) else error
        failed.append((str(status.get("path") or "task"), int(code)))
    errors = status.get("errors")
    if isinstance(errors, list):
        failed.extend(
            (str(e.get("path") or "item"), int(e.get("code", 0)))
            for e in errors
            if isinstance(e, dict)
        )
    if failed:
        text = "; ".join(f"{p} ({_code_text(c)})" for p, c in failed)
        if handle.kind != "delete":
            # No record of the source here, so a 408 can't be told apart: say it anyway.
            text += _partial_note(status, failed, handle.kind.capitalize(), None, None)
        lines.append("DSM reported errors: " + text + ".")
    skip = status.get("skipstatus")
    skip_value = skip.get("status") if isinstance(skip, dict) else skip
    if skip_value not in (None, "", "none"):
        lines.append("Some items were skipped (already at the destination, overwrite=false).")
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
    first and stops only a task that is still running: CopyMove ``stop`` answers success
    for any taskid, so its answer alone can't say whether anything was stopped.
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

    async def status_or_none() -> dict[str, Any] | None:
        """The task's status, or None when DSM doesn't have it (599)."""
        try:
            data: dict[str, Any] = await client.request(
                parsed.api, "status", version=version, params={"taskid": taskid}
            )
        except (DsmHttpError, DsmReplyError, RequestTooLongError) as e:
            stopping = action == "stop"
            _unreadable(
                parsed,
                e,
                operation="Stop task" if stopping else "Check task",
                note=" Nothing was stopped." if stopping else "",
            )
        except SynologyError as e:
            if e.code != 599:
                synology_error_response("Stop task" if action == "stop" else "Check task", e)
            return None
        except httpx.TransportError as e:
            stopping = action == "stop"
            _unanswered(
                parsed,
                e,
                operation="Stop task" if stopping else "Check task",
                request="status",
                note=" Nothing was stopped." if stopping else "",
            )
        return data

    def gone() -> str:
        # A stop request must hear that this call stopped nothing ("or was stopped"
        # alone reads as "your stop worked").
        nothing = " Nothing was stopped." if action == "stop" else ""
        if paths:
            # Ours, but DSM has let it go: a finished Delete is forgotten unless a status
            # read caught it finishing, so there's no result left to give.
            return (
                f"The {parsed.kind} of {', '.join(paths)} is no longer running on the NAS: "
                f"it finished or was stopped, and DSM no longer has its result.{nothing} "
                f"{_LIST_HINT}"
            )
        return (
            "No running task with this handle. It finished, was stopped, or was started by "
            f"a different DSM user.{nothing} {_LIST_HINT}"
        )

    status = await status_or_none()

    if action == "stop":
        # Read the status first: CopyMove stop answers success for any taskid, finished,
        # forgotten or another user's (vdsm 7.1 and 7.2.2), so stop alone can't tell.
        if status is None:
            if record is not None and record.final_status is not None:
                final = await _final_text(client, parsed, record.final_status, record)
                return f"{_NOT_STOPPED}\n{final}"
            return gone()
        logger.debug("%s status before stop (%s): %s", parsed.api, parsed, status)
        if status.get("finished"):
            records.set_final(parsed, status)
            final = await _final_text(client, parsed, status, record)
            return f"{_NOT_STOPPED}\n{final}"
        try:
            await client.request(
                parsed.api,
                "stop",
                version=version,
                params={"taskid": taskid},
                read_timeout=STOP_TIMEOUT,
            )
        except (DsmHttpError, DsmReplyError, RequestTooLongError) as e:
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
                return f"That task had already finished or isn't running. {_LIST_HINT}"
            synology_error_response("Stop task", e)
        except httpx.TransportError as e:
            _unanswered(
                parsed,
                e,
                operation="Stop task",
                request="stop",
                note=" The stop may or may not have taken effect.",
            )
        what = ", ".join(paths) if paths else f"`{parsed}`"
        return f"Stopped {parsed.kind} of {what}; it may be partly done. {_LIST_HINT}"

    if status is None:
        if record is not None and record.final_status is not None:
            return await _final_text(client, parsed, record.final_status, record)
        return gone()
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
        'action="stop" (it may then be partly done).'
    )
