"""Tests for task handles and the in-memory task records."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import DsmHttpError, DsmReplyError
from mcp_synology.modules.filestation.tasks import (
    DiscoveryNote,
    Handle,
    TaskRecord,
    TaskRecords,
    parse_handle,
    poll_task,
    progress_text,
    running_task_for,
)
from tests.conftest import BASE_URL

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient


def test_parse_handle_roundtrip() -> None:
    text = "delete:FileStation_179219158016597225"
    assert str(parse_handle(text)) == text
    for kind in ("copy", "move", "restore"):
        assert parse_handle(f"{kind}:X1").kind == kind
    assert parse_handle("move:X").api == "SYNO.FileStation.CopyMove"
    assert parse_handle("delete:X").api == "SYNO.FileStation.Delete"


def test_parse_handle_strips_decoration() -> None:
    h = parse_handle(" `delete:FileStation_6AD2AEEDA63770CD`. ")
    assert str(h) == "delete:FileStation_6AD2AEEDA63770CD"


@pytest.mark.parametrize("bad", ["search:X", "delete:", "delete:a/b", "FileStation_1", ""])
def test_parse_handle_rejects(bad: str) -> None:
    with pytest.raises(ToolError) as exc_info:
        parse_handle(bad)
    body = json.loads(str(exc_info.value))
    assert body["error"]["code"] == "invalid_parameter"
    assert body["error"]["param"] == "handle"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(20, "under a minute"), (14 * 60, "14 min"), (89 * 60, "89 min"), (125 * 60, "2 h 5 min")],
)
def test_elapsed_text(seconds: float, text: str) -> None:
    from mcp_synology.modules.filestation.tasks import _elapsed_text

    assert _elapsed_text(seconds) == text


NOW = 1792250200.0


@pytest.mark.parametrize(
    ("taskid", "started"),
    [
        ("FileStation_179224848373CDB12", 1792248483.0),  # real NAS, 2026-10-17
        ("FileStation_179219158016597225", 1792191580.0),
        ("FileStation_6AD3911EB4E9B2BB", 1792250142.0),  # vdsm 7.1: hex
        ("FileStation_6ad3911e", 1792250142.0),
        ("FileStation_17922484", None),  # 8 decimal digits read as hex: 1982
        ("FileStation_1792250250", 1792250250.0),  # a clock a little ahead
        ("FileStation_1792250500", None),  # future
        (f"FileStation_{int(NOW) - 31 * 86400}", None),  # over 30 days old
        ("FileStation_D1", None),
        ("FileStation_179224", None),
        ("Other_1792248483", None),
    ],
)
def test_taskid_started(taskid: str, started: float | None) -> None:
    from mcp_synology.modules.filestation.tasks import taskid_started

    assert taskid_started(taskid, now=NOW) == started


def test_taskid_started_defaults_to_the_clock() -> None:
    from mcp_synology.modules.filestation.tasks import taskid_started

    started = int(time.time()) - 60
    assert taskid_started(f"FileStation_{started}ABC") == started


def _rec(n: int) -> TaskRecord:
    return TaskRecord(handle=Handle("delete", f"T{n}"), paths=[f"/s/{n}"])


def test_records_evict_oldest_beyond_max() -> None:
    records = TaskRecords(max_entries=2)
    for n in range(3):
        records.add(_rec(n))
    assert records.get(Handle("delete", "T0")) is None
    assert records.get(Handle("delete", "T2")) is not None


def test_records_expire_after_ttl() -> None:
    now = [0.0]
    records = TaskRecords(ttl=10.0, clock=lambda: now[0])
    records.add(_rec(1))
    assert records.get(Handle("delete", "T1")) is not None
    now[0] = 11.0
    assert records.get(Handle("delete", "T1")) is None


def test_set_final_without_record() -> None:
    records = TaskRecords()
    h = Handle("copy", "Z9")
    status: dict[str, Any] = {"finished": True}
    records.set_final(h, status)
    rec = records.get(h)
    assert rec is not None
    assert rec.final_status == status


API = "SYNO.FileStation.Delete"
RUNNING = {"finished": False, "processed_num": 8, "total": 342, "progress": 0.02}


def _ok(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data})


def _method(request: httpx.Request) -> str:
    return dict(request.url.params).get("method", "")


def _read_timeout(request: httpx.Request) -> float:
    timeout: dict[str, float] = request.extensions["timeout"]
    return timeout["read"]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Fake clock; each fake sleep advances it by its argument."""
    import mcp_synology.modules.filestation.tasks as tasks

    now = [0.0]

    async def fake_sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(tasks.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(tasks.asyncio, "sleep", fake_sleep)
    return now


class TestPollTask:
    @respx.mock
    async def test_poll_finished_does_not_stop(
        self, mock_client: DsmClient, clock: list[float]
    ) -> None:
        calls: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            calls.append(_method(request))
            return _ok({"finished": True})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await poll_task(mock_client, api=API, version=2, taskid="t1", wait=30, label="x")
        assert result.outcome == "finished"
        assert calls == ["status"]

    @respx.mock
    async def test_poll_sends_quoted_taskid(
        self, mock_client: DsmClient, clock: list[float]
    ) -> None:
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(return_value=_ok({"finished": True}))
        await poll_task(mock_client, api=API, version=2, taskid="17918443788", wait=30, label="x")
        assert dict(route.calls.last.request.url.params)["taskid"] == '"17918443788"'

    @respx.mock
    async def test_poll_handoff_does_not_stop(
        self, mock_client: DsmClient, clock: list[float]
    ) -> None:
        calls: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            calls.append(_method(request))
            return _ok(RUNNING)

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await poll_task(mock_client, api=API, version=2, taskid="t1", wait=1, label="x")
        assert result.outcome == "handoff"
        assert "stop" not in calls
        assert result.stopped is False
        assert result.status["processed_num"] == 8

    @respx.mock
    async def test_poll_cancel_stops(
        self, mock_client: DsmClient, clock: list[float], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mcp_synology.modules.filestation.tasks as tasks

        calls: list[str] = []
        timeouts: dict[str, float] = {}
        polled = asyncio.Event()

        def side_effect(request: httpx.Request) -> httpx.Response:
            calls.append(_method(request))
            timeouts[_method(request)] = _read_timeout(request)
            return _ok(RUNNING if _method(request) == "status" else {})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        never = asyncio.Event()

        async def blocked_sleep(seconds: float) -> None:
            polled.set()
            await never.wait()

        monkeypatch.setattr(tasks.asyncio, "sleep", blocked_sleep)
        task = asyncio.ensure_future(
            poll_task(mock_client, api=API, version=2, taskid="t1", wait=30, label="x")
        )
        await polled.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls.count("stop") == 1
        assert timeouts == {"status": 30, "stop": 90}

    @pytest.mark.parametrize(
        "exc",
        [httpx.ReadTimeout("slow"), httpx.ConnectError("refused"), httpx.RemoteProtocolError("x")],
    )
    @respx.mock
    async def test_poll_transport_error_is_a_stall_handoff(
        self, mock_client: DsmClient, clock: list[float], exc: Exception
    ) -> None:
        """#245, maintainer decision 2026-10-17: a NAS that stops answering during the
        wait hands the task off instead of stopping it."""
        calls: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            calls.append(_method(request))
            if _method(request) == "status" and calls.count("status") > 1:
                raise exc
            return _ok(RUNNING)

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await poll_task(mock_client, api=API, version=2, taskid="t1", wait=30, label="x")
        assert result.outcome == "stalled"
        assert result.stopped is False
        assert result.error is exc
        assert result.status["processed_num"] == 8  # the last answered status
        assert "stop" not in calls

    @respx.mock
    async def test_poll_unexpected_exception_is_a_handoff_without_stop(
        self, mock_client: DsmClient, clock: list[float]
    ) -> None:
        """QA R1 on #251: an unreadable status reply hands off like a stall."""
        calls: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            calls.append(_method(request))
            return httpx.Response(200, text="<html>login</html>")

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await poll_task(mock_client, api=API, version=2, taskid="t1", wait=30, label="x")
        assert result.outcome == "stalled"
        assert isinstance(result.error, DsmReplyError)
        assert result.stopped is False
        assert calls == ["status"]

    @pytest.mark.parametrize("status_code", [502, 504])
    @respx.mock
    async def test_poll_proxy_http_error_is_a_handoff_without_stop(
        self, mock_client: DsmClient, clock: list[float], status_code: int
    ) -> None:
        """#282: a reverse proxy's 502/504 is how a DSM stalled by a huge delete shows up
        behind a proxy. It used to stop the task (DsmHttpError is a SynologyError)."""
        calls: list[str] = []
        replies = iter([_ok(RUNNING), httpx.Response(status_code, text="<html>Bad Gateway</html>")])

        def side_effect(request: httpx.Request) -> httpx.Response:
            calls.append(_method(request))
            return next(replies)

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await poll_task(mock_client, api=API, version=2, taskid="t1", wait=30, label="x")
        assert result.outcome == "stalled"
        assert isinstance(result.error, DsmHttpError)
        assert result.error.status == status_code
        assert result.stopped is False
        assert result.status == RUNNING
        assert calls == ["status", "status"]

    @respx.mock
    async def test_poll_status_data_not_an_object_is_a_handoff(
        self, mock_client: DsmClient, clock: list[float]
    ) -> None:
        """``success: true`` with a non-object ``data`` can't be read as a status."""
        calls: list[str] = []
        replies = iter([_ok(RUNNING), httpx.Response(200, json={"success": True, "data": [1]})])

        def side_effect(request: httpx.Request) -> httpx.Response:
            calls.append(_method(request))
            return next(replies)

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await poll_task(mock_client, api=API, version=2, taskid="t1", wait=30, label="x")
        assert result.outcome == "stalled"
        assert isinstance(result.error, TypeError)
        assert result.status == RUNNING  # the last readable status
        assert calls == ["status", "status"]

    @pytest.mark.parametrize(
        "stop_reply",
        [
            httpx.Response(200, json={"success": False, "error": {"code": 401}}),
            httpx.ReadTimeout(""),
        ],
    )
    @respx.mock
    async def test_poll_error_stop_not_confirmed(
        self, mock_client: DsmClient, clock: list[float], stop_reply: Any
    ) -> None:
        """A DSM error stops the task; a refused or unanswered stop is reported as such."""
        calls: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            calls.append(_method(request))
            if _method(request) == "status":
                return httpx.Response(200, json={"success": False, "error": {"code": 599}})
            if isinstance(stop_reply, Exception):
                raise stop_reply
            return stop_reply

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await poll_task(mock_client, api=API, version=2, taskid="t1", wait=30, label="x")
        assert result.outcome == "poll_error"
        assert result.stopped is False
        assert calls == ["status", "stop"]

    @respx.mock
    async def test_poll_error_stops(self, mock_client: DsmClient, clock: list[float]) -> None:
        timeouts: dict[str, float] = {}
        connects: dict[str, float] = {}

        def side_effect(request: httpx.Request) -> httpx.Response:
            timeouts[_method(request)] = _read_timeout(request)
            connects[_method(request)] = request.extensions["timeout"]["connect"]
            if _method(request) == "status":
                return httpx.Response(200, json={"success": False, "error": {"code": 599}})
            return _ok({})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await poll_task(mock_client, api=API, version=2, taskid="t1", wait=30, label="x")
        assert result.outcome == "poll_error"
        assert result.stopped is True
        assert result.error is not None
        # DSM 7.1 can take ~60 s to answer a Delete stop that worked at once (#245).
        assert timeouts == {"status": 30, "stop": 90}
        assert connects == {"status": 30, "stop": 30}

    @respx.mock
    async def test_poll_progress_counting_then_numbers(
        self, mock_client: DsmClient, clock: list[float]
    ) -> None:
        statuses = iter(
            [
                {"finished": False, "progress": -1, "total": 0},
                {"finished": False, "processed_num": 5, "total": 10, "progress": 0.5},
                {"finished": True},
            ]
        )
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=lambda request: _ok(next(statuses))
        )
        on_progress = AsyncMock()
        # progress_every=0.5 == the poll interval, so both updates get through
        await poll_task(
            mock_client,
            api=API,
            version=2,
            taskid="t1",
            wait=30,
            label="Deleting /x",
            on_progress=on_progress,
            progress_every=0.5,
        )
        assert [c.args for c in on_progress.await_args_list] == [
            (0, None, "Deleting /x: counting items"),
            (5, 10, "Deleting /x: 5 of 10 items"),
        ]

    @respx.mock
    async def test_poll_progress_always_increases(
        self, mock_client: DsmClient, clock: list[float]
    ) -> None:
        """MCP: progress must increase with each notification, heartbeats included."""
        statuses = iter(
            [
                {"finished": False, "progress": -1, "total": 0},
                {"finished": False, "progress": -1, "total": 0},
                {"finished": False, "progress": -1, "total": 0},
                {"finished": False, "processed_num": 0, "total": 342, "progress": 0},
                {"finished": False, "processed_num": 9, "total": 342, "progress": 0.03},
                {"finished": True},
            ]
        )
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=lambda request: _ok(next(statuses))
        )
        on_progress = AsyncMock()
        await poll_task(
            mock_client,
            api=API,
            version=2,
            taskid="t1",
            wait=30,
            label="x",
            on_progress=on_progress,
            progress_every=0.5,
        )
        sent = [c.args for c in on_progress.await_args_list]
        assert len(sent) == 5
        values = [s[0] for s in sent]
        assert all(b > a for a, b in zip(values, values[1:], strict=False))
        assert values[0] == 0
        assert values[-1] == 9
        # The 0 of 342 after the heartbeats is raised, so it goes without a total.
        assert [s[1] for s in sent] == [None, None, None, None, 342]
        assert sent[3][2] == "x: 0 of 342 items"

    @respx.mock
    async def test_poll_progress_callback_error_keeps_polling(
        self, mock_client: DsmClient, clock: list[float]
    ) -> None:
        """Progress is best-effort: a failing callback neither ends the wait nor stops."""
        calls: list[str] = []
        statuses = iter(
            [
                {"finished": False, "processed_num": 5, "total": 10},
                {"finished": False, "processed_num": 6, "total": 10},
                {"finished": True},
            ]
        )

        def side_effect(request: httpx.Request) -> httpx.Response:
            calls.append(_method(request))
            return _ok(next(statuses))

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        on_progress = AsyncMock(side_effect=RuntimeError("stream closed"))
        result = await poll_task(
            mock_client,
            api=API,
            version=2,
            taskid="t1",
            wait=30,
            label="x",
            on_progress=on_progress,
            progress_every=0.5,
        )
        assert result.outcome == "finished"
        assert on_progress.await_count == 2
        assert calls == ["status", "status", "status"]

    @respx.mock
    async def test_poll_progress_throttled(
        self, mock_client: DsmClient, clock: list[float]
    ) -> None:
        count = 0

        def side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal count
            count += 1
            return _ok({"finished": count >= 10, "processed_num": count, "total": 20})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        on_progress = AsyncMock()
        result = await poll_task(
            mock_client,
            api=API,
            version=2,
            taskid="t1",
            wait=30,
            label="x",
            on_progress=on_progress,
            interval=0.1,
        )
        assert result.outcome == "finished"
        assert on_progress.await_count == 1

    @respx.mock
    async def test_poll_progress_skips_unchanged_numbers(
        self, mock_client: DsmClient, clock: list[float]
    ) -> None:
        statuses = iter(
            [
                {"finished": False, "processed_num": 5, "total": 10},
                {"finished": False, "processed_num": 5, "total": 10},
                {"finished": False, "processed_num": 6, "total": 10},
                {"finished": True},
            ]
        )
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=lambda request: _ok(next(statuses))
        )
        on_progress = AsyncMock()
        await poll_task(
            mock_client,
            api=API,
            version=2,
            taskid="t1",
            wait=30,
            label="x",
            on_progress=on_progress,
            progress_every=0.5,
        )
        assert [c.args[0] for c in on_progress.await_args_list] == [5, 6]

    @respx.mock
    async def test_poll_anyio_cancel_scope_still_stops(
        self, mock_client: DsmClient, clock: list[float], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The MCP SDK cancels via level-triggered anyio scopes; stop must be shielded."""
        import anyio

        import mcp_synology.modules.filestation.tasks as tasks

        calls: list[str] = []
        blocked = asyncio.Event()

        async def side_effect(request: httpx.Request) -> httpx.Response:
            calls.append(_method(request))
            if _method(request) == "stop":
                await anyio.sleep(0)  # a checkpoint: re-canceled here unless shielded
                calls.append("stop-done")
            return _ok(RUNNING if _method(request) == "status" else {})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        async def blocked_sleep(seconds: float) -> None:
            blocked.set()
            await anyio.sleep_forever()

        monkeypatch.setattr(tasks.asyncio, "sleep", blocked_sleep)
        async with anyio.create_task_group() as tg:

            async def run() -> None:
                await poll_task(mock_client, api=API, version=2, taskid="t1", wait=30, label="x")

            tg.start_soon(run)
            await blocked.wait()
            tg.cancel_scope.cancel()
        assert calls.count("stop") == 1
        assert "stop-done" in calls

    @respx.mock
    async def test_poll_reauth_mid_wait(self, mock_client: DsmClient, clock: list[float]) -> None:
        reauths = 0

        async def fake_reauth() -> None:
            nonlocal reauths
            reauths += 1
            mock_client.sid = "new-sid"

        mock_client.set_re_auth_callback(fake_reauth)
        responses = iter(
            [
                httpx.Response(200, json={"success": False, "error": {"code": 119}}),
                _ok({"finished": True}),
            ]
        )
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=lambda request: next(responses)
        )
        result = await poll_task(mock_client, api=API, version=2, taskid="t1", wait=30, label="x")
        assert result.outcome == "finished"
        assert reauths == 1
        sent = [dict(c.request.url.params)["taskid"] for c in route.calls]
        assert sent == ['"t1"', '"t1"']


def test_progress_text() -> None:
    assert progress_text({"processed_num": 8, "total": 342, "progress": 0.0234}) == (
        "8 of 342 items, 2%"
    )
    assert progress_text({"progress": -1, "total": 0}) == "counting items"
    # CopyMove reports bytes (processed_size), not items.
    assert progress_text({"processed_size": 1 << 30, "total": 4 << 30, "progress": 0.25}) == (
        "1 GiB of 4 GiB, 25%"
    )
    assert progress_text({"processed_size": 0, "progress": -1, "total": 0}) == (
        "no progress figures from DSM yet"
    )


@respx.mock
async def test_poll_progress_in_bytes_for_copymove(
    mock_client: DsmClient, clock: list[float]
) -> None:
    statuses = iter(
        [
            {"finished": False, "processed_size": 1 << 30, "total": 4 << 30, "progress": 0.25},
            {"finished": True},
        ]
    )
    respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=lambda request: _ok(next(statuses)))
    on_progress = AsyncMock()
    await poll_task(
        mock_client,
        api="SYNO.FileStation.CopyMove",
        version=2,
        taskid="t1",
        wait=30,
        label="Copying /x",
        on_progress=on_progress,
    )
    assert [c.args for c in on_progress.await_args_list] == [
        (1 << 30, 4 << 30, "Copying /x: 1 GiB of 4 GiB")
    ]


class TestRunningTaskFor:
    TASKS = [
        {
            "api": "SYNO.FileStation.CopyMove",
            "finished": False,
            "params": {"path": ["/s/a"], "dest_folder_path": "/s/d", "remove_src": False},
            "taskid": "FileStation_C",
        },
        {
            "api": "SYNO.FileStation.Delete",
            "finished": True,
            "params": {"path": ["/s/a"], "recursive": True},
            "taskid": "FileStation_DONE",
        },
        {
            "api": "SYNO.FileStation.Delete",
            "finished": False,
            "params": {"path": "/s/a", "recursive": True},
            "taskid": "FileStation_D",
        },
    ]

    @respx.mock
    async def test_finds_running_task_of_api_for_path(self, mock_client: DsmClient) -> None:
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            return_value=_ok({"offset": 0, "tasks": self.TASKS, "total": 3})
        )
        api = "SYNO.FileStation.Delete"
        assert await running_task_for(mock_client, api=api, path="/s/a") == "FileStation_D"
        assert await running_task_for(mock_client, api=api, path="/s/b") is None
        assert (
            await running_task_for(mock_client, api="SYNO.FileStation.CopyMove", path="/s/a")
            == "FileStation_C"
        )
        params = dict(route.calls[0].request.url.params)
        assert params["api"] == "SYNO.FileStation.BackgroundTask"
        assert params["method"] == "list"
        assert params["version"] == "2"

    @respx.mock
    async def test_list_failure_is_none(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            return_value=httpx.Response(200, json={"success": False, "error": {"code": 599}})
        )
        api = "SYNO.FileStation.Delete"
        assert await running_task_for(mock_client, api=api, path="/s/a") is None

    @respx.mock
    async def test_unanswered_list_is_none(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=httpx.ReadTimeout(""))
        api = "SYNO.FileStation.Delete"
        assert await running_task_for(mock_client, api=api, path="/s/a") is None

    @respx.mock
    @pytest.mark.parametrize("data", [[], {"tasks": None}, {"tasks": "x"}, {}])
    async def test_unexpected_reply_shape_is_none(self, mock_client: DsmClient, data: Any) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            return_value=httpx.Response(200, json={"success": True, "data": data})
        )
        api = "SYNO.FileStation.Delete"
        assert await running_task_for(mock_client, api=api, path="/s/a") is None


# Shapes from the vdsm probe (BackgroundTask.list v2, DSM 7.1 and 7.2.2). crtime isn't
# the start time (taskid_started), so nothing uses it.
CRTIME = 1792197001
LIST_DELETE = {
    "api": "SYNO.FileStation.Delete",
    "crtime": CRTIME,
    "finished": False,
    "method": "start",
    "params": {"accurate_progress": True, "path": ["/s/a"], "recursive": True},
    "path": "",
    "processed_num": 8,
    "processing_path": "",
    "progress": 0.02,
    "taskid": "FileStation_D1",
    "total": 342,
    "version": 2,
}
LIST_MOVE = {
    "api": "SYNO.FileStation.CopyMove",
    "crtime": CRTIME,
    "finished": False,
    "method": "start",
    "params": {
        "dest_folder_path": "/s/d",
        "overwrite": False,
        "path": ["/s/b"],
        "remove_src": True,
    },
    "path": "",
    "processed_size": 0,
    "processing_path": "",
    "progress": -1,
    "taskid": "FileStation_M1",
    "total": 0,
    "version": 2,
}
LIST_DONE = {**LIST_DELETE, "finished": True, "taskid": "FileStation_OLD"}
DELETE_FINISHED = {
    "finished": True,
    "found_dir_num": 400,
    "found_file_num": 200000,
    "has_dir": True,
    "path": "",
    "processed_num": 200400,
    "processing_path": "/s/a/d248/472",
    "progress": 1,
    "total": 200400,
}
MOVE_FINISHED = {
    "dest_folder_path": "/s/d",
    "finished": True,
    "path": "",
    "processed_size": 2048,
    "progress": 1,
    "skipstatus": {"status": "none"},
    "status": "PROCESSING",
    "total": 2048,
}


def _list_reply(*tasks: dict[str, Any]) -> httpx.Response:
    return _ok({"offset": 0, "tasks": list(tasks), "total": len(tasks)})


def _err(code: int) -> httpx.Response:
    return httpx.Response(200, json={"success": False, "error": {"code": code}})


def _router(replies: dict[str, Any], calls: list[tuple[str, str]] | None = None) -> Any:
    """Route by DSM method; a value that is an exception is raised."""

    def side_effect(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if calls is not None:
            calls.append((params.get("api", ""), params.get("method", "")))
        reply = replies[params.get("method", "")]
        if isinstance(reply, Exception):
            raise reply
        assert isinstance(reply, httpx.Response)
        return reply

    return side_effect


class TestDiscover:
    @respx.mock
    async def test_discover_seeds_records_and_returns_note(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import discover_running_tasks

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"list": _list_reply(LIST_DELETE, LIST_MOVE, LIST_DONE)})
        )
        records = TaskRecords()
        found = await discover_running_tasks(mock_client, records)

        delete = records.get(Handle("delete", "FileStation_D1"))
        assert delete is not None
        assert delete.paths == ["/s/a"]
        assert delete.recursive is True
        move = records.get(Handle("move", "FileStation_M1"))
        assert move is not None
        assert move.paths == ["/s/b"]
        assert move.dest == "/s/d"
        assert move.overwrite is False
        assert records.get(Handle("delete", "FileStation_OLD")) is None

        assert found is not None
        note = found.text()
        assert note is not None
        assert note.startswith("Note: 2 operation(s) still running on the NAS:")
        assert "deleting /s/a (8 of 342 items, 2%), handle `delete:FileStation_D1`" in note
        assert "moving /s/b" in note
        assert "handle `move:FileStation_M1`" in note
        assert note.endswith("Use file_task to check on them or stop them.")
        # A file_task reply about one of them leaves that one out.
        rest = found.text(skip="delete:FileStation_D1")
        assert rest is not None
        assert rest.startswith("Note: 1 operation(s) still running on the NAS:")
        assert "FileStation_D1" not in rest
        assert "handle `move:FileStation_M1`" in rest
        assert DiscoveryNote({"delete:X": "  x"}).text(skip="delete:X") is None

    @respx.mock
    async def test_discover_restore_and_copy_kinds(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import discover_running_tasks

        restore = {
            **LIST_MOVE,
            "taskid": "FileStation_R1",
            "params": {**LIST_MOVE["params"], "path": ["/s/#recycle/x"]},  # type: ignore[dict-item]
        }
        copy = {
            **LIST_MOVE,
            "taskid": "FileStation_C1",
            "params": {**LIST_MOVE["params"], "remove_src": False},  # type: ignore[dict-item]
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"list": _list_reply(restore, copy)})
        )
        records = TaskRecords()
        found = await discover_running_tasks(mock_client, records)
        assert records.get(Handle("restore", "FileStation_R1")) is not None
        assert records.get(Handle("copy", "FileStation_C1")) is not None
        assert found is not None
        note = found.text()
        assert note is not None
        assert "restoring /s/#recycle/x" in note
        assert "copying /s/b" in note

    @respx.mock
    async def test_discover_nothing_running_returns_none(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import discover_running_tasks

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"list": _list_reply(LIST_DONE)})
        )
        records = TaskRecords()
        assert await discover_running_tasks(mock_client, records) is None
        assert records.get(Handle("delete", "FileStation_OLD")) is None

    @respx.mock
    @pytest.mark.parametrize("reply", [_err(599), httpx.ReadTimeout("")])
    async def test_discover_list_error_returns_none(
        self, mock_client: DsmClient, reply: Any
    ) -> None:
        from mcp_synology.modules.filestation.tasks import discover_running_tasks

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_router({"list": reply}))
        assert await discover_running_tasks(mock_client, TaskRecords()) is None

    @respx.mock
    async def test_discover_malformed_task_never_raises(self, mock_client: DsmClient) -> None:
        """A task whose numbers aren't numbers must not break the first tool call."""
        from mcp_synology.modules.filestation.tasks import discover_running_tasks

        bad = {**LIST_DELETE, "processed_num": "lots", "total": 10}
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"list": _list_reply(bad)})
        )
        assert await discover_running_tasks(mock_client, TaskRecords()) is None


class TestFileTask:
    @respx.mock
    async def test_file_task_status_after_discover_gives_full_result(
        self, mock_client: DsmClient
    ) -> None:
        from mcp_synology.modules.filestation.operations import _copy_move_summary
        from mcp_synology.modules.filestation.tasks import discover_running_tasks, file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"list": _list_reply(LIST_MOVE), "status": _ok(MOVE_FINISHED)})
        )
        records = TaskRecords()
        await discover_running_tasks(mock_client, records)
        text = await file_task(mock_client, records=records, handle="move:FileStation_M1")
        expected = await _copy_move_summary(mock_client, ["/s/b"], "/s/d", True, 2048, [])
        assert text == expected

    @respx.mock
    async def test_file_task_list(self, mock_client: DsmClient) -> None:
        from mcp_synology.core.formatting import format_timestamp
        from mcp_synology.modules.filestation.tasks import file_task

        calls: list[tuple[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"list": _list_reply(LIST_DELETE, LIST_MOVE, LIST_DONE)}, calls)
        )
        text = await file_task(mock_client, records=TaskRecords())
        assert calls == [("SYNO.FileStation.BackgroundTask", "list")]
        assert "delete:FileStation_D1" in text
        assert "move:FileStation_M1" in text
        assert "FileStation_OLD" not in text
        assert "/s/a" in text
        assert "/s/b" in text
        assert format_timestamp(CRTIME) not in text
        assert "unknown" in text  # FileStation_D1 carries no start time
        assert "8 of 342 items, 2%" in text

    @respx.mock
    async def test_file_task_list_empty(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_router({"list": _list_reply()}))
        text = await file_task(mock_client, records=TaskRecords())
        assert text == "No File Station delete, copy or move tasks are running for this user."

    @respx.mock
    async def test_file_task_list_unavailable(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_router({"list": _err(103)}))
        text = await file_task(mock_client, records=TaskRecords())
        assert "isn't available on this DSM version" in text

    @respx.mock
    async def test_file_task_status_running(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        calls: list[tuple[str, str]] = []
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _ok(RUNNING)}, calls)
        )
        records = TaskRecords()
        records.add(TaskRecord(handle=Handle("delete", "FileStation_D1"), paths=["/s/a"]))
        text = await file_task(mock_client, records=records, handle="delete:FileStation_D1")
        assert calls == [("SYNO.FileStation.Delete", "status")]
        assert dict(route.calls.last.request.url.params)["taskid"] == '"FileStation_D1"'
        assert "/s/a" in text
        assert "8 of 342 items, 2%" in text
        assert "delete:FileStation_D1" in text
        rec = records.get(Handle("delete", "FileStation_D1"))
        assert rec is not None
        assert rec.final_status is None
        assert "running for" not in text  # no start time known
        assert "a count that hasn't moved doesn't mean it's stuck" in text
        assert "stopping and starting it again doesn't make it faster" in text

    @respx.mock
    async def test_file_task_status_running_says_how_long(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _ok(RUNNING)})
        )
        h = Handle("delete", f"FileStation_{int(time.time()) - 14 * 60}ABC")
        text = await file_task(mock_client, records=TaskRecords(), handle=str(h))
        assert "(8 of 342 items, 2%; running for 14 min)" in text

    @respx.mock
    async def test_file_task_list_started_from_taskid(self, mock_client: DsmClient) -> None:
        from mcp_synology.core.formatting import format_timestamp
        from mcp_synology.modules.filestation.tasks import file_task

        started = int(time.time()) - 600
        task = {**LIST_DELETE, "taskid": f"FileStation_{started}ABC"}
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"list": _list_reply(task)})
        )
        text = await file_task(mock_client, records=TaskRecords())
        assert format_timestamp(started) in text
        assert format_timestamp(CRTIME) not in text

    @respx.mock
    async def test_file_task_status_running_copy_wording(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _ok(RUNNING)})
        )
        text = await file_task(mock_client, records=TaskRecords(), handle="copy:FileStation_C1")
        assert "A busy NAS can go minutes without visible progress" in text
        assert "counts finished items" not in text

    @respx.mock
    async def test_file_task_status_finished_with_record(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.operations import _delete_summary
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _ok(DELETE_FINISHED)})
        )
        h = Handle("delete", "FileStation_D1")
        records = TaskRecords()
        records.add(TaskRecord(handle=h, paths=["/s/a"], recursive=True, recycle={"s": False}))
        text = await file_task(mock_client, records=records, handle=str(h))
        assert text == await _delete_summary(mock_client, ["/s/a"], {"s": False})
        rec = records.get(h)
        assert rec is not None
        assert rec.final_status == DELETE_FINISHED

    @respx.mock
    async def test_file_task_status_finished_restore_wording(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _ok(MOVE_FINISHED)})
        )
        h = Handle("restore", "FileStation_R1")
        records = TaskRecords()
        records.add(TaskRecord(handle=h, paths=["/s/#recycle/x"], dest="/s", overwrite=False))
        text = await file_task(mock_client, records=records, handle=str(h))
        assert "Restored from /s recycle bin:" in text

    @respx.mock
    async def test_file_task_status_finished_without_record(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        finished = {**DELETE_FINISHED, "errors": [{"code": 408, "path": "/s/a/x"}]}
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _ok(finished)})
        )
        records = TaskRecords()
        text = await file_task(mock_client, records=records, handle="delete:FileStation_D9")
        assert text.startswith("Delete finished: 200,400 items processed.")
        assert "/s/a/x (DSM error 408)" in text
        rec = records.get(Handle("delete", "FileStation_D9"))
        assert rec is not None
        assert rec.final_status == finished

    @respx.mock
    async def test_file_task_status_599_uses_cached_final(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.operations import _delete_summary
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_router({"status": _err(599)}))
        h = Handle("delete", "FileStation_D1")
        records = TaskRecords()
        records.add(TaskRecord(handle=h, paths=["/s/a"], recursive=True, recycle={"s": True}))
        records.set_final(h, DELETE_FINISHED)
        text = await file_task(mock_client, records=records, handle=str(h))
        assert text == await _delete_summary(mock_client, ["/s/a"], {"s": True})

    @respx.mock
    async def test_file_task_status_599_unknown(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_router({"status": _err(599)}))
        text = await file_task(mock_client, records=TaskRecords(), handle="copy:FileStation_X")
        assert text == (
            "No running task with this handle. It finished, was stopped, or was started by a "
            "different DSM user. List the path to see what's left."
        )

    @respx.mock
    async def test_file_task_status_599_with_record_names_the_path(
        self, mock_client: DsmClient
    ) -> None:
        """Finished or stopped with no status read to catch it: DSM has forgotten it, the record
        hasn't."""
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_router({"status": _err(599)}))
        h = Handle("delete", "FileStation_D1")
        records = TaskRecords()
        records.add(TaskRecord(handle=h, paths=["/Backups/X"]))
        text = await file_task(mock_client, records=records, handle=str(h))
        assert text == (
            "The delete of /Backups/X is no longer running on the NAS: it finished or was "
            "stopped, and DSM no longer has its result. List the path to see what's left."
        )

    @respx.mock
    async def test_file_task_stop(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        calls: list[tuple[str, str]] = []
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _ok(RUNNING), "stop": _ok({})}, calls)
        )
        h = Handle("move", "FileStation_M1")
        records = TaskRecords()
        records.add(TaskRecord(handle=h, paths=["/s/b"], dest="/s/d"))
        text = await file_task(mock_client, records=records, handle=str(h), action="stop")
        assert calls == [
            ("SYNO.FileStation.CopyMove", "status"),
            ("SYNO.FileStation.CopyMove", "stop"),
        ]
        assert dict(route.calls.last.request.url.params)["taskid"] == '"FileStation_M1"'
        assert [_read_timeout(c.request) for c in route.calls] == [30, 90]
        # Only the read timeout is raised for the stop (#245); connect keeps the default.
        assert [c.request.extensions["timeout"]["connect"] for c in route.calls] == [30, 30]
        assert text == (
            "Stopped move of /s/b; it may be partly done. List the path to see what's left."
        )

    @pytest.mark.parametrize("action", ["status", "stop"])
    @pytest.mark.parametrize(
        ("reply", "shown", "retryable"),
        [
            (httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"}),
             "the answer wasn't a DSM reply (Content-Type text/html)", True),
            (httpx.Response(502, text="<html>Bad Gateway</html>"), "DSM answered HTTP 502", True),
            # A 4xx isn't retryable, as on every other call (DsmHttpError.retryable).
            (httpx.Response(403, text="<html>Forbidden</html>"), "DSM answered HTTP 403", False),
        ],
    )  # fmt: skip
    @respx.mock
    async def test_file_task_unreadable_status_is_structured(
        self,
        mock_client: DsmClient,
        action: str,
        reply: httpx.Response,
        shown: str,
        retryable: bool,
    ) -> None:
        """#282: a proxy page or 502 on the status read was a raw JSONDecodeError crash (or,
        for a 502 on stop, didn't say nothing was stopped)."""
        from mcp_synology.modules.filestation.tasks import file_task

        calls: list[tuple[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": reply, "stop": _ok({})}, calls)
        )
        with pytest.raises(ToolError) as exc:
            await file_task(
                mock_client,
                records=TaskRecords(),
                handle="delete:FileStation_D1",
                action=action,  # type: ignore[arg-type]
            )
        err = json.loads(str(exc.value))["error"]
        assert err["code"] == "unavailable"
        assert err["retryable"] is retryable
        assert err["param"] == "handle"
        op = "Stop task" if action == "stop" else "Check task"
        assert err["message"].startswith(f"{op} failed for `delete:FileStation_D1`: ")
        assert shown in err["message"]
        assert err["message"].endswith("Nothing was stopped.") is (action == "stop")
        assert not any(w in err["message"] for w in ("login", "Bad Gateway", "Forbidden"))
        assert "file_task with this handle" in err["suggestion"]
        assert ("SYNO.FileStation.Delete", "stop") not in calls

    @respx.mock
    async def test_file_task_stop_request_proxy_error_may_have_taken_effect(
        self, mock_client: DsmClient
    ) -> None:
        """#282: a 502 on the stop request itself: the stop may or may not have reached DSM."""
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router(
                {"status": _ok(RUNNING), "stop": httpx.Response(502, text="Bad Gateway")}
            )
        )
        with pytest.raises(ToolError) as exc:
            await file_task(
                mock_client, records=TaskRecords(), handle="delete:FileStation_D1", action="stop"
            )
        err = json.loads(str(exc.value))["error"]
        assert err["code"] == "unavailable"
        assert err["message"].endswith(
            "DSM answered HTTP 502. The stop may or may not have taken effect."
        )

    @respx.mock
    @pytest.mark.parametrize("code", [401, 599])
    async def test_file_task_stop_already_finished(self, mock_client: DsmClient, code: int) -> None:
        """Running at the status read, gone by the stop (a race): not "Stopped"."""
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _ok(RUNNING), "stop": _err(code)})
        )
        text = await file_task(
            mock_client, records=TaskRecords(), handle="delete:FileStation_D1", action="stop"
        )
        assert text.startswith("That task had already finished or isn't running.")

    @respx.mock
    async def test_file_task_stop_finished_unread_copy_is_not_stopped(
        self, mock_client: DsmClient
    ) -> None:
        """CopyMove stop answers success for a finished task; status first says so."""
        from mcp_synology.modules.filestation.tasks import file_task

        calls: list[tuple[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _ok(MOVE_FINISHED), "stop": _ok({})}, calls)
        )
        h = Handle("copy", "FileStation_C1")
        records = TaskRecords()
        text = await file_task(mock_client, records=records, handle=str(h), action="stop")
        assert ("SYNO.FileStation.CopyMove", "stop") not in calls
        assert text.startswith(
            "That task had already finished, so nothing was stopped.\nCopy finished: "
        ), text
        rec = records.get(h)
        assert rec is not None
        assert rec.final_status == MOVE_FINISHED

    @respx.mock
    async def test_file_task_stop_finished_with_record(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.operations import _delete_summary
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _ok(DELETE_FINISHED), "stop": _err(401)})
        )
        h = Handle("delete", "FileStation_D1")
        records = TaskRecords()
        records.add(TaskRecord(handle=h, paths=["/s/a"], recursive=True, recycle={"s": False}))
        text = await file_task(mock_client, records=records, handle=str(h), action="stop")
        summary = await _delete_summary(mock_client, ["/s/a"], {"s": False})
        assert text == f"That task had already finished, so nothing was stopped.\n{summary}"

    @respx.mock
    @pytest.mark.parametrize("kind", ["copy", "delete"])
    async def test_file_task_stop_unknown_is_not_stopped(
        self, mock_client: DsmClient, kind: str
    ) -> None:
        """Unknown, forgotten or another user's task (599): no stop, the status reply."""
        from mcp_synology.modules.filestation.tasks import file_task

        calls: list[tuple[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _err(599), "stop": _ok({})}, calls)
        )
        text = await file_task(
            mock_client, records=TaskRecords(), handle=f"{kind}:FileStation_X", action="stop"
        )
        assert [m for _, m in calls] == ["status"]
        assert text == (
            "No running task with this handle. It finished, was stopped, or was started by a "
            "different DSM user. Nothing was stopped. List the path to see what's left."
        )

    @respx.mock
    async def test_file_task_stop_forgotten_with_record_says_nothing_stopped(
        self, mock_client: DsmClient
    ) -> None:
        """QA round 3: "or was stopped" alone reads as "your stop worked"."""
        from mcp_synology.modules.filestation.tasks import file_task

        calls: list[tuple[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _err(599), "stop": _ok({})}, calls)
        )
        h = Handle("delete", "FileStation_D1")
        records = TaskRecords()
        records.add(TaskRecord(handle=h, paths=["/qa/vanish-y"]))
        text = await file_task(mock_client, records=records, handle=str(h), action="stop")
        assert [m for _, m in calls] == ["status"]
        assert text == (
            "The delete of /qa/vanish-y is no longer running on the NAS: it finished or was "
            "stopped, and DSM no longer has its result. Nothing was stopped. List the path to "
            "see what's left."
        )

    @respx.mock
    async def test_file_task_stop_599_with_cached_final(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _err(599), "stop": _ok({})})
        )
        h = Handle("copy", "FileStation_C1")
        records = TaskRecords()
        records.set_final(h, MOVE_FINISHED)
        text = await file_task(mock_client, records=records, handle=str(h), action="stop")
        assert text.startswith(
            "That task had already finished, so nothing was stopped.\nCopy finished: "
        ), text

    @respx.mock
    @pytest.mark.parametrize(
        ("exc", "code"),
        [(httpx.ReadTimeout("t"), "timeout"), (httpx.ConnectError("refused"), "unavailable")],
    )
    async def test_file_task_stop_unanswered_stop(
        self, mock_client: DsmClient, exc: Exception, code: str
    ) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _ok(RUNNING), "stop": exc})
        )
        with pytest.raises(ToolError) as exc_info:
            await file_task(
                mock_client, records=TaskRecords(), handle="delete:FileStation_D1", action="stop"
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == code
        assert error["message"].startswith("Stop task failed: DSM ")
        assert "the stop request for `delete:FileStation_D1`" in error["message"]
        assert "The stop may or may not have taken effect." in error["message"]
        assert "file_task" in error["suggestion"]

    @respx.mock
    async def test_file_task_stop_unanswered_status(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        calls: list[tuple[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": httpx.ReadTimeout("t"), "stop": _ok({})}, calls)
        )
        with pytest.raises(ToolError) as exc_info:
            await file_task(
                mock_client, records=TaskRecords(), handle="copy:FileStation_C1", action="stop"
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "timeout"
        assert "didn't answer the status request" in error["message"]
        assert "(ReadTimeout: t)" in error["message"]
        assert error["message"].endswith("Nothing was stopped.")
        assert [m for _, m in calls] == ["status"]

    @respx.mock
    async def test_file_task_status_unanswered(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": httpx.ConnectError("refused")})
        )
        with pytest.raises(ToolError) as exc_info:
            await file_task(mock_client, records=TaskRecords(), handle="copy:FileStation_C1")
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "unavailable"
        assert error["message"].startswith(
            "Check task failed: DSM lost the connection during the status request"
        )

    @pytest.mark.parametrize("handle", [None, "", "  "])
    @respx.mock
    async def test_file_task_stop_without_handle_is_invalid(
        self, mock_client: DsmClient, handle: str | None
    ) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(return_value=_ok({}))
        with pytest.raises(ToolError) as exc_info:
            await file_task(mock_client, records=TaskRecords(), handle=handle, action="stop")
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "invalid_parameter"
        assert error["param"] == "handle"
        assert "Stopping needs a handle" in error["message"]
        assert "file_task without a handle" in error["suggestion"]
        assert not route.called

    @respx.mock
    async def test_file_task_bad_handle_no_dsm_call(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(return_value=_ok({}))
        with pytest.raises(ToolError) as exc_info:
            await file_task(mock_client, records=TaskRecords(), handle="search:X")
        assert json.loads(str(exc_info.value))["error"]["code"] == "invalid_parameter"
        assert not route.called

    @respx.mock
    async def test_file_task_generic_copy_with_skips_and_error(
        self, mock_client: DsmClient
    ) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        finished = {
            **MOVE_FINISHED,
            "skipstatus": {"status": "all"},
            "error": {"code": 1002},
            "path": "/s/b/x",
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _ok(finished)})
        )
        text = await file_task(mock_client, records=TaskRecords(), handle="copy:FileStation_C9")
        assert text.startswith("Copy finished: 2 KiB processed.")
        assert "/s/b/x (DSM error 1002)" in text
        assert "skipped" in text

    @respx.mock
    @pytest.mark.parametrize(
        ("method", "action"), [("status", "status"), ("stop", "stop"), ("list", "status")]
    )
    async def test_file_task_other_dsm_errors_raise(
        self, mock_client: DsmClient, method: str, action: Any
    ) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _ok(RUNNING), method: _err(105)})
        )
        handle = None if method == "list" else "delete:FileStation_D1"
        with pytest.raises(ToolError):
            await file_task(mock_client, records=TaskRecords(), handle=handle, action=action)

    @respx.mock
    async def test_file_task_list_skips_malformed_entries(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        bad = [
            "x",
            {**LIST_DELETE, "params": None},
            {**LIST_DELETE, "params": {"path": 5}},
            {**LIST_DELETE, "taskid": "bad id!"},
            {**LIST_DELETE, "api": "SYNO.FileStation.Search", "taskid": "S1"},
        ]
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"list": _ok({"tasks": bad})})
        )
        text = await file_task(mock_client, records=TaskRecords())
        assert text == "No File Station delete, copy or move tasks are running for this user."

    @respx.mock
    async def test_file_task_finished_with_record_errors_like_delete_files(
        self, mock_client: DsmClient
    ) -> None:
        from mcp_synology.modules.filestation.tasks import file_task

        finished = {**DELETE_FINISHED, "errors": [{"code": 900, "path": "/s/a"}]}
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_router({"status": _ok(finished)})
        )
        h = Handle("delete", "FileStation_D1")
        records = TaskRecords()
        records.add(TaskRecord(handle=h, paths=["/s/a"], recursive=False))
        with pytest.raises(ToolError) as exc_info:
            await file_task(mock_client, records=records, handle=str(h))
        assert "/s/a (DSM error 900)" in str(exc_info.value)
        rec = records.get(h)
        assert rec is not None
        assert rec.final_status == finished
