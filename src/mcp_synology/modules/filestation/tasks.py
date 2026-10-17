"""File Station task handles and the in-memory records behind them."""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, get_args

from mcp_synology.core.errors import ErrorCode
from mcp_synology.core.formatting import error_response

if TYPE_CHECKING:
    from collections.abc import Callable

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
