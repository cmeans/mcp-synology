"""File Station task handles and the in-memory records behind them."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, get_args

import httpx

from mcp_synology.core.errors import ErrorCode, SynologyError
from mcp_synology.core.formatting import error_response
from mcp_synology.modules.filestation.helpers import dsm_str

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)

HandleKind = Literal["delete", "copy", "move", "restore"]

_KINDS: tuple[str, ...] = get_args(HandleKind)
_TASKID_RE = re.compile(r"^[A-Za-z0-9_]{1,80}$")
_DECORATION = " \t\r\n`'\"*.,;:()[]<>"


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
        await client.request(api, "stop", version=version, params={"taskid": taskid})
    except SynologyError as e:
        task_logger.warning("%s task cleanup failed for %s: %s", api, taskid, e)
    except Exception:
        task_logger.warning("%s task cleanup failed for %s", api, taskid, exc_info=True)
    else:
        return True
    return False


def _task_note(stopped: bool) -> str:
    """What happened to a background task mcp-synology gave up on (#237)."""
    if stopped:
        return "The task was stopped and may be partly done."
    # Not "DSM didn't answer": DSM may also have refused the stop (e.g. 401 for a task
    # that finished just before it), QA R2 on #239.
    return (
        "mcp-synology couldn't confirm the task stopped; it may still be running on the "
        "NAS, and may be partly done."
    )


ProgressCallback = Callable[[float, float | None, str], Awaitable[None]]


@dataclass
class PollResult:
    """How a poll of a background task ended."""

    status: dict[str, Any]
    outcome: Literal["finished", "handoff", "poll_error", "unanswered"]
    error: Exception | None = None
    stopped: bool = False


def progress_text(status: dict[str, Any]) -> str:
    """Human-readable progress of a task status, e.g. ``8 of 342 items, 2%``."""
    total = status.get("total") or 0
    if total > 0:
        processed = status.get("processed_num") or 0
        text = f"{processed:,} of {total:,} items"
        progress = status.get("progress")
        if isinstance(progress, int | float) and progress >= 0:
            text += f", {round(progress * 100)}%"
        return text
    return "counting items"


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

    A task still running at the deadline is handed off (left running, not stopped).
    A polling failure or an unanswered status request stops the task; so does
    cancellation, which then propagates. The deadline is wall-clock (#237).
    """
    quoted = dsm_str(taskid)
    deadline = time.monotonic() + wait
    status: dict[str, Any] = {}
    last_progress: float | None = None

    async def stop() -> bool:
        return await _stop_background_task(client, api, quoted, version, logger)

    try:
        while time.monotonic() < deadline:
            try:
                status = await client.request(
                    api, "status", version=version, params={"taskid": quoted}
                )
            except SynologyError as e:
                return PollResult(status, "poll_error", e, await stop())
            except httpx.TransportError as e:
                return PollResult(status, "unanswered", e, await stop())

            logger.debug("%s status (%s): %s", api, label, status)

            if status.get("finished", False):
                return PollResult(status, "finished")

            if on_progress is not None:
                now = time.monotonic()
                if last_progress is None or now - last_progress >= progress_every:
                    total = status.get("total") or 0
                    if total > 0:
                        last_progress = now
                        processed = status.get("processed_num") or 0
                        await on_progress(
                            processed, total, f"{label}: {processed:,} of {total:,} items"
                        )
                    elif status.get("progress") == -1:
                        last_progress = now
                        await on_progress(0, None, f"{label}: counting items")

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        await stop()
        raise
    return PollResult(status, "handoff")
