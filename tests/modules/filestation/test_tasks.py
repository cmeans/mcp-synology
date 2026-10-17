"""Tests for task handles and the in-memory task records."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.filestation.tasks import (
    Handle,
    TaskRecord,
    TaskRecords,
    parse_handle,
    poll_task,
    progress_text,
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
        polled = asyncio.Event()

        def side_effect(request: httpx.Request) -> httpx.Response:
            calls.append(_method(request))
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

    @respx.mock
    async def test_poll_transport_error_stops(
        self, mock_client: DsmClient, clock: list[float]
    ) -> None:
        calls: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            calls.append(_method(request))
            if _method(request) == "status":
                raise httpx.ReadTimeout("slow")
            return _ok({})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await poll_task(mock_client, api=API, version=2, taskid="t1", wait=30, label="x")
        assert result.outcome == "unanswered"
        assert result.stopped is True
        assert isinstance(result.error, httpx.TransportError)
        assert calls.count("stop") == 1

    @respx.mock
    async def test_poll_error_stops(self, mock_client: DsmClient, clock: list[float]) -> None:
        def side_effect(request: httpx.Request) -> httpx.Response:
            if _method(request) == "status":
                return httpx.Response(200, json={"success": False, "error": {"code": 599}})
            return _ok({})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await poll_task(mock_client, api=API, version=2, taskid="t1", wait=30, label="x")
        assert result.outcome == "poll_error"
        assert result.stopped is True
        assert result.error is not None

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
                await anyio.sleep(0)  # a checkpoint: re-cancelled here unless shielded
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
