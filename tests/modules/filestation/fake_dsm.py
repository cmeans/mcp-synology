"""A scripted File Station DSM for the batch runner's tests (#512).

Requests are routed by ``api`` and ``method`` (as ``tests/modules/downloadstation/dsm.py`` does),
and each source path has a script: whether it exists, how its task's start answers, the sequence
of status reads, and whether a task for it is already running. Every request is recorded, so a
test can say which were sent and in what order.

The task clock is fake: `FakeClock.install` swaps ``tasks.time`` and ``tasks.asyncio.sleep`` for
``poll_task`` only, so a wait of seconds takes none, and a sleep still yields to the event loop so
cancellation can reach it.
"""

from __future__ import annotations

import asyncio
import types
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from tests.conftest import BASE_URL

if TYPE_CHECKING:
    import pytest
    import respx

# A status read answers one of these: a status dict, a DSM error code, an exception to raise, an
# HTTP response, or CANCEL (the call is canceled during that read).
CANCEL = "cancel"

RUNNING: dict[str, Any] = {"finished": False, "processed_num": 1, "total": 10, "progress": 0.1}
FINISHED: dict[str, Any] = {"finished": True, "processed_size": 10, "progress": 1}


@dataclass
class PathScript:
    """How DSM answers for one source path."""

    # getinfo: whether the path is there before the call; `gone_after` once its task ran.
    exists: bool = True
    isdir: bool = False
    gone_after: bool = True
    # The start: "ok", a DSM error code, or an exception to raise.
    start: Any = "ok"
    # The status reads, in order; the last one repeats.
    statuses: list[Any] = field(default_factory=lambda: [dict(FINISHED)])
    # A task for this path already running (listed by BackgroundTask list).
    running: dict[str, Any] | None = None
    # Once DSM has lost its task (599), getinfo of the path answers this instead: a code for it
    # (407 settles nothing, leaving a lost delete's outcome unknown, #521), an HTTP response, or
    # an exception to raise.
    code_when_lost: Any = None


@dataclass
class Call:
    api: str
    method: str
    params: dict[str, str]


class FakeClock:
    """A monotonic clock that only sleeps move."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_800_000_000.0 + self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mcp_synology.modules.filestation import tasks

        fake_time = types.SimpleNamespace(monotonic=self.monotonic, time=self.time)
        fake_asyncio = types.SimpleNamespace(
            **{name: getattr(asyncio, name) for name in dir(asyncio) if not name.startswith("__")}
        )
        fake_asyncio.sleep = self.sleep
        monkeypatch.setattr(tasks, "time", fake_time)
        monkeypatch.setattr(tasks, "asyncio", fake_asyncio)


class FakeDsm:
    """The scripted DSM. ``scripts`` is keyed by source path; any other path getinfo asks for
    exists as a folder."""

    def __init__(self, scripts: dict[str, PathScript] | None = None) -> None:
        self.scripts: dict[str, PathScript] = dict(scripts or {})
        self.calls: list[Call] = []
        self.ran: set[str] = set()
        self.unclear: dict[str, Any] = {}
        self._tasks: dict[str, str] = {}  # taskid → path
        self._reads: dict[str, int] = {}
        self._next = 0
        # Tasks of other calls that DSM lists once this many starts were sent: (starts, task).
        self.later_tasks: list[tuple[int, dict[str, Any]]] = []

    def mount(self, router: respx.Router | Any) -> None:
        router.route(url__startswith=f"{BASE_URL}/webapi/").mock(side_effect=self._answer)

    def methods(self, api: str | None = None) -> list[str]:
        return [c.method for c in self.calls if api is None or c.api == api]

    def stops(self) -> list[Call]:
        return [c for c in self.calls if c.method == "stop"]

    def starts(self) -> list[Call]:
        return [c for c in self.calls if c.method == "start"]

    # -- answers --

    @staticmethod
    def _ok(data: Any) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": data})

    @staticmethod
    def _err(code: int) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "error": {"code": code}})

    def _answer(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.content:
            params.update(dict(httpx.QueryParams(request.content.decode())))
        api, method = params.get("api", ""), params.get("method", "")
        self.calls.append(Call(api, method, params))
        if api == "SYNO.FileStation.List" and method == "getinfo":
            return self._getinfo(params["path"])
        if api == "SYNO.FileStation.List" and method == "list":
            return self._ok({"files": [], "total": 0, "offset": 0})
        if api == "SYNO.FileStation.BackgroundTask" and method == "list":
            return self._ok({"tasks": self._listed(), "total": len(self._listed())})
        if api in ("SYNO.FileStation.Delete", "SYNO.FileStation.CopyMove"):
            if method == "start":
                return self._start(api, params)
            if method == "status":
                return self._status(params["taskid"].strip('"'))
            if method == "stop":
                return self._ok({})
        if api == "SYNO.FileStation.CreateFolder":
            return self._ok({"folders": [{"path": params.get("folder_path", "")}]})
        return self._ok({})

    def _getinfo(self, path: str) -> httpx.Response:
        if path in self.unclear:
            answer = self.unclear[path]
            if isinstance(answer, BaseException):
                raise answer
            if isinstance(answer, httpx.Response):
                return answer
            return self._ok({"files": [{"code": answer, "path": path}]})
        script = self.scripts.get(path)
        missing = script is not None and (
            not script.exists or (path in self.ran and script.gone_after)
        )
        if missing:
            return self._ok({"files": [{"code": 408, "path": path}]})
        isdir = script.isdir if script is not None else True
        name = path.rsplit("/", 1)[-1]
        return self._ok({"files": [{"path": path, "name": name, "isdir": isdir}]})

    def _listed(self) -> list[dict[str, Any]]:
        tasks = []
        for path, script in self.scripts.items():
            if script.running is not None:
                task = {"finished": False, "taskid": f"Running_{list(self.scripts).index(path)}"}
                task.update(script.running)
                task.setdefault("params", {}).setdefault("path", [path])
                tasks.append(task)
        started = len(self.starts())
        tasks.extend(dict(task) for after, task in self.later_tasks if started >= after)
        return tasks

    def _start(self, api: str, params: dict[str, str]) -> httpx.Response:
        path = params["path"]
        script = self.scripts.setdefault(path, PathScript())
        if isinstance(script.start, BaseException):
            raise script.start
        if isinstance(script.start, int):
            return self._err(script.start)
        self._next += 1
        prefix = "Delete" if api.endswith("Delete") else "CopyMove"
        taskid = f"FileStation_{prefix}_{self._next}"
        self._tasks[taskid] = path
        return self._ok({"taskid": taskid})

    def _status(self, taskid: str) -> httpx.Response:
        path = self._tasks.get(taskid, "")
        script = self.scripts.get(path, PathScript())
        n = self._reads.get(taskid, 0)
        self._reads[taskid] = n + 1
        answer = script.statuses[min(n, len(script.statuses) - 1)]
        if answer == CANCEL:
            raise asyncio.CancelledError
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        if isinstance(answer, int):
            if answer == 599 and script.code_when_lost is not None:
                self.unclear[path] = script.code_when_lost
            elif answer == 599 and script.gone_after:
                # DSM lost the task, and the path went: as when a delete finished unseen (#521).
                self.ran.add(path)
            return self._err(answer)
        if answer.get("finished"):
            self.ran.add(path)
        return self._ok({**answer, "path": path})
