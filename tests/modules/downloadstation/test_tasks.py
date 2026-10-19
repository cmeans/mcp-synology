"""Tests for modules/downloadstation/tasks.py.

Payloads are real DSM captures (``dsm.captured``) and every route matches on
the DSM ``api`` / ``method`` (and ``version`` where pinned), so a wrong API
name, method, or version fails with an unmatched request — see #123.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.formatting import format_timestamp
from mcp_synology.modules.downloadstation.tasks import (
    create_download,
    delete_download,
    edit_download,
    get_download_info,
    list_downloads,
    pause_download,
    resume_download,
)
from tests.modules.downloadstation.dsm import captured, err, get_route, ok, post_route

if TYPE_CHECKING:
    from pathlib import Path

    from mcp_synology.core.client import DsmClient

TASK = "SYNO.DownloadStation.Task"
TASK2 = "SYNO.DownloadStation2.Task"
INFO = "SYNO.DownloadStation.Info"


def _envelope(exc: ToolError) -> dict[str, Any]:
    return json.loads(str(exc))["error"]  # type: ignore[no-any-return]


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def _task(task_id: str, status: str, *, title: str = "t", size: int = 100) -> dict[str, Any]:
    """A task row shaped like the captured Task.list v1 rows (string status)."""
    row = captured("task_list_v1")["data"]["tasks"][0]
    row.update({"id": task_id, "status": status, "title": title, "size": size})
    return row


def _manager(is_manager: bool = True) -> None:
    key = "info_getinfo_admin" if is_manager else "info_getinfo_user"
    get_route(INFO, "getinfo", version=1).respond(json=captured(key))


def _v1_list(*, limit: str | None = None) -> respx.Route:
    """Route v1 ``Task.list``; with ``limit``, only requests carrying that limit."""
    return get_route(TASK, "list", version=1, params={"limit": limit} if limit else None)


def _page(n: int, *, total: int | None = None, status: str = "downloading") -> dict[str, Any]:
    """A v1 Task.list page of ``n`` tasks. DSM sets ``total`` to the page size (#140)."""
    tasks = [_task(f"dbid_{i}", status, title=f"task-{i}") for i in range(1, n + 1)]
    return ok({"offset": 0, "total": n if total is None else total, "tasks": tasks})


def _ds2_total(total: int) -> respx.Route:
    """Route DS2 ``Task.list`` v2, which reports the true queue size (#140)."""
    return get_route(TASK2, "list", version=2).respond(
        json=ok({"offset": 0, "total": total, "task": []})
    )


class TestListDownloads:
    @respx.mock
    async def test_captured_list_renders_string_status(self, mock_client: DsmClient) -> None:
        """Regression for #123 bug 1: v1 status is a string, not an int."""
        _manager()
        get_route(TASK, "list", version=1).respond(json=captured("task_list_v1"))
        result = await list_downloads(mock_client)
        assert "capture-magnet" in result
        assert "unknown(" not in result
        assert "waiting" in result or "downloading" in result or "finished" in result

    @respx.mock
    @pytest.mark.parametrize(
        ("status_filter", "kept", "dropped"),
        [
            ("downloading", "waiting", "finished"),
            ("downloading", "downloading", "paused"),
            ("finished", "seeding", "downloading"),
            ("paused", "paused", "waiting"),
            ("error", "error", "finished"),
        ],
    )
    async def test_status_filter_matches_string_statuses(
        self, mock_client: DsmClient, status_filter: str, kept: str, dropped: str
    ) -> None:
        """Regression for #123 bug 1: every filter except 'all' used to return empty."""
        _manager()
        get_route(TASK, "list", version=1).respond(
            json=ok(
                {
                    "offset": 0,
                    "total": 2,
                    "tasks": [
                        _task("dbid_1", kept, title="keep-me"),
                        _task("dbid_2", dropped, title="drop-me"),
                    ],
                }
            )
        )
        result = await list_downloads(mock_client, status_filter=status_filter)
        assert "keep-me" in result
        assert "drop-me" not in result

    @respx.mock
    async def test_error_task_shows_dsm_error_detail(self, mock_client: DsmClient) -> None:
        _manager()
        row = _task("dbid_12", "error", title="missing-one.iso")
        row["status_extra"] = {"error_detail": "broken_link"}
        get_route(TASK, "list", version=1).respond(
            json=ok({"offset": 0, "total": 1, "tasks": [row]})
        )
        result = await list_downloads(mock_client)
        assert "error (broken_link)" in result

    @respx.mock
    async def test_info_shows_dsm_error_detail(self, mock_client: DsmClient) -> None:
        body = captured("task_getinfo_v1")
        task = body["data"]["tasks"][0]
        task["status"] = "error"
        task["status_extra"] = {"error_detail": "broken_link"}
        get_route(TASK, "getinfo", version=1).respond(json=body)
        result = await get_download_info(mock_client, task_id="dbid_1")
        assert "error (broken_link)" in result

    @respx.mock
    async def test_empty_queue(self, mock_client: DsmClient) -> None:
        _manager()
        get_route(TASK, "list", version=1).respond(json=ok({"offset": 0, "total": 0, "tasks": []}))
        result = await list_downloads(mock_client)
        assert "No items to display" in result

    @respx.mock
    async def test_non_manager_output_says_own_tasks_only(self, mock_client: DsmClient) -> None:
        """#123 bug 13: non-managers only see their own tasks (captured: total 0)."""
        _manager(is_manager=False)
        get_route(TASK, "list", version=1).respond(json=captured("task_list_user"))
        result = await list_downloads(mock_client)
        assert "your tasks only" in result

    @respx.mock
    async def test_manager_output_has_no_own_tasks_note(self, mock_client: DsmClient) -> None:
        _manager()
        get_route(TASK, "list", version=1).respond(json=captured("task_list_v1"))
        result = await list_downloads(mock_client)
        assert "your tasks only" not in result

    @respx.mock
    async def test_info_api_absent_lists_without_note(self, mock_client: DsmClient) -> None:
        del mock_client.api_cache[INFO]
        get_route(TASK, "list", version=1).respond(json=captured("task_list_v1"))
        result = await list_downloads(mock_client)
        assert "capture-magnet" in result
        assert "your tasks only" not in result

    @respx.mock
    async def test_manager_lookup_failure_does_not_fail_listing(
        self, mock_client: DsmClient
    ) -> None:
        get_route(INFO, "getinfo", version=1).respond(json=err(105))
        get_route(TASK, "list", version=1).respond(json=captured("task_list_v1"))
        result = await list_downloads(mock_client)
        assert "capture-magnet" in result
        assert "your tasks only" not in result

    @respx.mock
    async def test_dsm_error_propagates_as_tool_error(self, mock_client: DsmClient) -> None:
        _manager()
        get_route(TASK, "list", version=1).respond(json=err(105))
        with pytest.raises(ToolError) as exc:
            await list_downloads(mock_client)
        assert _envelope(exc.value)["code"] == "permission_denied"

    async def test_unknown_status_filter_raises_tool_error(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError, match="status_filter"):
            await list_downloads(mock_client, status_filter="not_a_status")


class TestListDownloadsPaging:
    """#140, found on a real NAS with 1,214 tasks: v1 ``total`` is the page size."""

    @respx.mock
    async def test_heading_is_the_queue_not_the_last_task(self, mock_client: DsmClient) -> None:
        _manager()
        _v1_list().respond(json=_page(2))
        result = await list_downloads(mock_client)
        assert result.splitlines()[0] == "Download Station queue (all)"

    @respx.mock
    async def test_full_page_takes_total_from_ds2(self, mock_client: DsmClient) -> None:
        _manager()
        _v1_list(limit="2").respond(json=_page(2))
        ds2 = _ds2_total(1214)
        result = await list_downloads(mock_client, limit=2)
        assert ds2.called
        assert "1214 task(s) total; showing 1–2." in result
        assert "offset=2" in result

    @respx.mock
    async def test_short_page_needs_no_count(self, mock_client: DsmClient) -> None:
        _manager()
        _v1_list(limit="25").respond(json=_page(3))
        ds2 = _ds2_total(999)
        result = await list_downloads(mock_client)
        assert not ds2.called
        assert "3 task(s) total; showing 1–3." in result
        assert "offset=" not in result

    @respx.mock
    async def test_short_later_page_counts_from_offset(self, mock_client: DsmClient) -> None:
        _manager()
        _v1_list(limit="2").respond(json=_page(1))
        ds2 = _ds2_total(999)
        result = await list_downloads(mock_client, offset=4, limit=2)
        assert not ds2.called
        assert "5 task(s) total; showing 5–5." in result

    @respx.mock
    async def test_ds2_absent_counts_with_v1(self, mock_client: DsmClient) -> None:
        del mock_client.api_cache[TASK2]
        _manager()
        _v1_list(limit="2").respond(json=_page(2))
        count = _v1_list(limit="-1").respond(json=_page(7))
        result = await list_downloads(mock_client, limit=2)
        assert count.called
        assert "additional" not in count.calls.last.request.url.params
        assert "7 task(s) total; showing 1–2." in result

    @respx.mock
    async def test_ds2_refusal_counts_with_v1(self, mock_client: DsmClient) -> None:
        _manager()
        _v1_list(limit="2").respond(json=_page(2))
        get_route(TASK2, "list", version=2).respond(json=err(105))
        _v1_list(limit="-1").respond(json=_page(7))
        result = await list_downloads(mock_client, limit=2)
        assert "7 task(s) total; showing 1–2." in result

    @respx.mock
    async def test_offset_past_the_end(self, mock_client: DsmClient) -> None:
        _manager()
        _v1_list(limit="25").respond(json=ok({"offset": 500, "total": 0, "tasks": []}))
        _ds2_total(12)
        result = await list_downloads(mock_client, offset=500)
        assert "No items to display" in result
        assert "12 task(s) total; offset 500 is past the end." in result

    @respx.mock
    async def test_status_filter_fetches_every_task_then_pages(
        self, mock_client: DsmClient
    ) -> None:
        _manager()
        tasks = [_task(f"dbid_{i}", "finished", title=f"done-{i}") for i in range(1, 151)]
        tasks.append(_task("dbid_151", "paused", title="the-paused-one"))
        everything = _v1_list(limit="-1").respond(
            json=ok({"offset": 0, "total": 151, "tasks": tasks})
        )
        result = await list_downloads(mock_client, status_filter="paused", limit=100)
        assert "additional" in everything.calls.last.request.url.params
        assert "the-paused-one" in result
        assert "1 task(s) total; showing 1–1." in result

    @respx.mock
    async def test_status_filter_pages_the_filtered_list(self, mock_client: DsmClient) -> None:
        _manager()
        tasks = [_task(f"dbid_{i}", "finished", title=f"done-{i}") for i in range(1, 6)]
        _v1_list(limit="-1").respond(json=ok({"offset": 0, "total": 5, "tasks": tasks}))
        result = await list_downloads(mock_client, status_filter="finished", offset=2, limit=2)
        assert _ids(result) == ["dbid_3", "dbid_4"]
        assert "5 task(s) total; showing 3–4." in result
        assert "offset=4" in result


def _timed(
    task_id: str, *, created: int, completed: int = 0, down: int = 0, up: int = 0
) -> dict[str, Any]:
    """A task row with creation/completion times and transfer speeds set."""
    row = _task(task_id, "seeding" if completed else "downloading", title="timed")
    row["additional"]["detail"].update({"create_time": created, "completed_time": completed})
    row["additional"]["transfer"].update({"speed_download": down, "speed_upload": up})
    return row


class TestListDownloadsColumnsAndSort:
    """#145: upload speed, completion time, sort_by, and a smaller default page."""

    @respx.mock
    async def test_down_and_up_speed_columns(self, mock_client: DsmClient) -> None:
        _manager()
        _v1_list().respond(
            json=ok({"offset": 0, "total": 1, "tasks": [_timed("dbid_1", created=1, up=25088)]})
        )
        result = await list_downloads(mock_client)
        header = result.splitlines()[2]
        assert "Down" in header and "Up" in header and "Speed" not in header
        assert "24.5 KiB/s" in result

    @respx.mock
    async def test_completed_column(self, mock_client: DsmClient) -> None:
        _manager()
        tasks = [
            _timed("dbid_1", created=1_791_700_000, completed=1_791_771_855),
            _timed("dbid_2", created=1_791_700_100),
        ]
        _v1_list().respond(json=ok({"offset": 0, "total": 2, "tasks": tasks}))
        result = await list_downloads(mock_client)
        assert "Completed" in result.splitlines()[2]
        assert format_timestamp(1_791_771_855) in result
        row_2 = next(line for line in result.splitlines() if "dbid_2" in line)
        assert row_2.rstrip().endswith("—") or " — " in row_2

    @respx.mock
    async def test_sort_by_completed_finds_the_latest_across_the_queue(
        self, mock_client: DsmClient
    ) -> None:
        """Creation order isn't completion order: the newest task finished first."""
        _manager()
        tasks = [_timed(f"dbid_{i}", created=i, completed=1000 + i) for i in range(1, 60)]
        tasks.append(_timed("dbid_60", created=60, completed=1001))  # newest, finished early
        tasks.append(_timed("dbid_61", created=61))  # still downloading
        _v1_list(limit="-1").respond(json=ok({"offset": 0, "total": 61, "tasks": tasks}))
        result = await list_downloads(mock_client, sort_by="completed", limit=1)
        assert _ids(result) == ["dbid_59"]
        assert "61 task(s) total; showing 1–1." in result

    @respx.mock
    async def test_sort_by_completed_puts_unfinished_last(self, mock_client: DsmClient) -> None:
        _manager()
        tasks = [_timed("dbid_1", created=1), _timed("dbid_2", created=2, completed=5)]
        _v1_list(limit="-1").respond(json=ok({"offset": 0, "total": 2, "tasks": tasks}))
        result = await list_downloads(mock_client, sort_by="completed")
        assert _ids(result) == ["dbid_2", "dbid_1"]

    @respx.mock
    async def test_sort_by_created_is_newest_first(self, mock_client: DsmClient) -> None:
        _manager()
        tasks = [_timed(f"dbid_{i}", created=i) for i in range(1, 4)]
        _v1_list(limit="-1").respond(json=ok({"offset": 0, "total": 3, "tasks": tasks}))
        result = await list_downloads(mock_client, sort_by="created")
        assert _ids(result) == ["dbid_3", "dbid_2", "dbid_1"]

    @respx.mock
    @pytest.mark.parametrize(
        ("sort_by", "field"), [("created", "created"), ("completed", "completed")]
    )
    async def test_tied_times_put_the_later_queued_task_first(
        self, mock_client: DsmClient, sort_by: str, field: str
    ) -> None:
        # DSM records times to the second, so a batch ties (seen on vdsm: ten tasks
        # with one create_time). Newest first must then mean last queued first.
        _manager()
        tasks = [_timed("dbid_1", created=5, completed=5)]
        tasks += [_timed(f"dbid_{i}", created=9, completed=9) for i in range(2, 6)]
        _v1_list(limit="-1").respond(json=ok({"offset": 0, "total": 5, "tasks": tasks}))
        result = await list_downloads(mock_client, sort_by=sort_by, limit=3)
        assert _ids(result) == ["dbid_5", "dbid_4", "dbid_3"]

    @respx.mock
    async def test_sort_and_filter_combine(self, mock_client: DsmClient) -> None:
        _manager()
        tasks = [
            _timed("dbid_1", created=1, completed=9),
            _timed("dbid_2", created=2),
            _timed("dbid_3", created=3, completed=7),
        ]
        _v1_list(limit="-1").respond(json=ok({"offset": 0, "total": 3, "tasks": tasks}))
        result = await list_downloads(mock_client, status_filter="finished", sort_by="completed")
        assert _ids(result) == ["dbid_1", "dbid_3"]

    async def test_unknown_sort_by_raises_tool_error(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError, match="sort_by"):
            await list_downloads(mock_client, sort_by="size")

    @pytest.mark.parametrize(
        ("kwargs", "param"),
        [
            ({"offset": -1}, "offset"),
            ({"offset": -2, "limit": 1}, "offset"),
            ({"limit": 0}, "limit"),
            ({"limit": -1}, "limit"),
        ],
    )
    @pytest.mark.parametrize(
        "path",
        [{}, {"status_filter": "finished"}, {"sort_by": "completed"}],
        ids=["queue", "filtered", "sorted"],
    )
    @respx.mock
    async def test_bad_offset_or_limit_is_rejected_before_any_dsm_call(
        self, mock_client: DsmClient, path: dict[str, str], kwargs: dict[str, int], param: str
    ) -> None:
        # The filtered/sorted path pages with a Python slice, where -1 drops a row
        # and a negative offset counts from the end; DSM clamps a negative offset
        # to 0. Reject both up front so every path agrees (#141 QA).
        list_route = _v1_list(limit=str(kwargs.get("limit", 25)))
        with pytest.raises(ToolError) as exc:
            await list_downloads(mock_client, **path, **kwargs)  # type: ignore[arg-type]
        error = _envelope(exc.value)
        assert error["code"] == "invalid_parameter"
        assert error["param"] == param
        assert error["value"] == kwargs[param]
        assert not list_route.called

    @respx.mock
    async def test_default_page_is_25(self, mock_client: DsmClient) -> None:
        _manager()
        route = _v1_list(limit="25").respond(json=_page(3))
        await list_downloads(mock_client)
        assert route.called


def _ids(text: str) -> list[str]:
    return re.findall(r"\bdbid_\d+\b", text)


class TestGetDownloadInfo:
    @respx.mock
    async def test_captured_getinfo_renders_sections(self, mock_client: DsmClient) -> None:
        route = get_route(TASK, "getinfo", version=1).respond(json=captured("task_getinfo_v1"))
        result = await get_download_info(mock_client, task_id="dbid_1")
        assert "capture-magnet" in result
        assert "unknown(" not in result
        assert "writable" in result  # destination
        sent = dict(route.calls.last.request.url.params)
        assert sent["additional"] == "detail,transfer,file,tracker,peer"

    @respx.mock
    async def test_captured_invalid_task_id_error(self, mock_client: DsmClient) -> None:
        get_route(TASK, "getinfo", version=1).respond(json=captured("task_getinfo_bogus"))
        with pytest.raises(ToolError, match="Invalid task id"):
            await get_download_info(mock_client, task_id="dbid_99999")

    @respx.mock
    async def test_empty_tasks_array_treated_as_not_found(self, mock_client: DsmClient) -> None:
        get_route(TASK, "getinfo", version=1).respond(json=ok({"tasks": []}))
        with pytest.raises(ToolError) as exc:
            await get_download_info(mock_client, task_id="dbid_1")
        assert _envelope(exc.value)["code"] == "not_found"


class TestGetDownloadInfoBtSections:
    @respx.mock
    async def test_file_tracker_peer_tables_render(self, mock_client: DsmClient) -> None:
        # The vdsm capture's magnet task has empty file/tracker/peer arrays, so
        # these rows follow the official DS Web API guide's Task_File /
        # Task_Tracker / Task_Peer definitions (note sizes are strings there).
        body = captured("task_getinfo_v1")
        add = body["data"]["tasks"][0]["additional"]
        add["file"] = [
            {
                "filename": "ubuntu.iso",
                "size": "1000",
                "size_downloaded": "500",
                "priority": "normal",
            }
        ]
        add["tracker"] = [
            {"url": "http://tracker.example/announce", "status": "Success", "seeds": 3, "peers": 9}
        ]
        add["peer"] = [
            {
                "address": "192.0.2.7",
                "agent": "Transmission",
                "progress": 0.25,
                "speed_download": 0,
                "speed_upload": 2048,
            }
        ]
        get_route(TASK, "getinfo", version=1).respond(json=body)
        result = await get_download_info(mock_client, task_id="dbid_1")
        assert "Files" in result and "ubuntu.iso" in result and "(50%)" in result
        assert "Trackers" in result and "tracker.example" in result
        assert "Peers" in result and "192.0.2.7" in result and "25%" in result


class TestGetDownloadInfoRedaction:
    """#143: tracker passkeys and source-URL credentials never reach tool output."""

    PASSKEY = "a1B2c3D4e5F6g7H8i9J0"

    @respx.mock
    async def test_tracker_passkey_is_masked(self, mock_client: DsmClient) -> None:
        body = captured("task_getinfo_v1")
        add = body["data"]["tasks"][0]["additional"]
        add["tracker"] = [
            {
                "url": f"https://tracker.example/announce/{self.PASSKEY}",
                "status": "Success",
                "seeds": 51,
                "peers": 3,
            }
        ]
        get_route(TASK, "getinfo", version=1).respond(json=body)
        result = await get_download_info(mock_client, task_id="dbid_1")
        assert self.PASSKEY not in result
        assert "https://tracker.example/announce/***" in result
        assert "51" in result

    @respx.mock
    async def test_source_uri_secrets_are_masked(self, mock_client: DsmClient) -> None:
        body = captured("task_getinfo_v1")
        detail = body["data"]["tasks"][0]["additional"]["detail"]
        detail["uri"] = (
            f"magnet:?xt=urn:btih:{'d' * 40}&dn=show"
            f"&tr=https%3A%2F%2Ftracker.example%2Fannounce%2F{self.PASSKEY}"
        )
        get_route(TASK, "getinfo", version=1).respond(json=body)
        result = await get_download_info(mock_client, task_id="dbid_1")
        assert self.PASSKEY not in result
        assert "dn=show" in result

    @respx.mock
    async def test_title_query_secrets_are_masked(self, mock_client: DsmClient) -> None:
        body = captured("task_getinfo_v1")
        body["data"]["tasks"][0]["title"] = f"file.iso&_sid={self.PASSKEY}"
        get_route(TASK, "getinfo", version=1).respond(json=body)
        result = await get_download_info(mock_client, task_id="dbid_1")
        assert self.PASSKEY not in result
        assert "file.iso&_sid=***" in result

    @respx.mock
    async def test_list_title_query_secrets_are_masked(self, mock_client: DsmClient) -> None:
        _manager()
        get_route(TASK, "list", version=1).respond(
            json=ok(
                {
                    "offset": 0,
                    "total": 1,
                    "tasks": [_task("dbid_1", "finished", title=f"f.bin&_sid={self.PASSKEY}")],
                }
            )
        )
        result = await list_downloads(mock_client)
        assert self.PASSKEY not in result
        assert "f.bin&_sid=***" in result

    @pytest.mark.parametrize("task_type", ["http", "ftp", "nzb", "emule", None])
    @respx.mock
    async def test_url_derived_title_token_is_masked(
        self, mock_client: DsmClient, task_type: str | None
    ) -> None:
        # QA on vdsm: DSM titles an HTTP task for .../<passkey>.torrent "<passkey>.torrent".
        body = captured("task_getinfo_v1")
        body["data"]["tasks"][0].update({"type": task_type, "title": f"{self.PASSKEY}.torrent"})
        get_route(TASK, "getinfo", version=1).respond(json=body)
        result = await get_download_info(mock_client, task_id="dbid_1")
        assert self.PASSKEY not in result
        assert "***.torrent" in result

    @respx.mock
    async def test_list_url_derived_title_token_is_masked(self, mock_client: DsmClient) -> None:
        _manager()
        http = _task("dbid_1", "error", title=f"{self.PASSKEY}.iso")
        http["type"] = "http"
        bt = _task("dbid_2", "finished", title="Show_Series_1_2026_1080p_WEB")
        bt["type"] = "bt"
        get_route(TASK, "list", version=1).respond(
            json=ok({"offset": 0, "total": 2, "tasks": [http, bt]})
        )
        result = await list_downloads(mock_client)
        assert self.PASSKEY not in result
        assert "***.iso" in result
        assert "Show_Series_1_2026_1080p_WEB" in result


class TestCreateDownloadUri:
    """URI create goes to DS2 Task.create v2 as a form POST (#123 items 9/9a)."""

    @respx.mock
    async def test_ds2_post_returns_task_ids(self, mock_client: DsmClient) -> None:
        _has_default()
        route = post_route(TASK2, "create", version=2).respond(json=captured("ds2_task_create_url"))
        result = await create_download(
            mock_client, uri="magnet:?xt=urn:btih:abc", destination="writable"
        )
        assert "dbid_1" in result
        form = _form(route.calls.last.request)
        assert form["type"] == '"url"'
        assert json.loads(form["url"]) == ["magnet:?xt=urn:btih:abc"]
        assert form["destination"] == '"writable"'
        assert form["create_list"] == "false"

    @respx.mock
    async def test_comma_list_becomes_json_array(self, mock_client: DsmClient) -> None:
        _has_default()
        route = post_route(TASK2, "create", version=2).respond(
            json=ok({"list_id": [], "task_id": ["dbid_1", "dbid_2"]})
        )
        result = await create_download(
            mock_client, uri="http://a.example/1.iso,http://b.example/2.iso"
        )
        form = _form(route.calls.last.request)
        assert json.loads(form["url"]) == ["http://a.example/1.iso", "http://b.example/2.iso"]
        assert form["destination"] == '"writable"'  # filled in from the default
        assert "dbid_1" in result and "dbid_2" in result

    @respx.mock
    async def test_long_uri_list_is_sent_in_body_not_query(self, mock_client: DsmClient) -> None:
        """#123 bug 14: a GET URL past ~8 KB gets HTTP 414 from DSM."""
        _has_default()
        long_magnet = "magnet:?xt=urn:btih:" + "a" * 40 + "&tr=" + "x" * 12_000
        route = post_route(TASK2, "create", version=2).respond(
            json=ok({"list_id": [], "task_id": ["dbid_9"]})
        )
        await create_download(mock_client, uri=long_magnet)
        request = route.calls.last.request
        assert long_magnet not in str(request.url)
        assert json.loads(_form(request)["url"]) == [long_magnet]

    @respx.mock
    @pytest.mark.parametrize("code", [102, 103, 104])
    async def test_falls_back_to_v1_post_on_not_available_codes(
        self, mock_client: DsmClient, code: int
    ) -> None:
        _has_default()
        post_route(TASK2, "create", version=2).respond(json=err(code))
        v1 = post_route(TASK, "create", version=1).respond(json=captured("v1_task_create_url"))
        result = await create_download(mock_client, uri="magnet:?xt=a", destination="writable")
        form = _form(v1.calls.last.request)
        assert form["uri"] == "magnet:?xt=a"
        assert form["destination"] == "writable"
        assert "did not return task IDs" in result

    @respx.mock
    async def test_falls_back_to_v1_when_ds2_absent(self, mock_client: DsmClient) -> None:
        _has_default()
        del mock_client.api_cache[TASK2]
        v1 = post_route(TASK, "create", version=1).respond(json=captured("v1_task_create_url"))
        await create_download(mock_client, uri="magnet:?xt=a")
        assert v1.called

    @respx.mock
    async def test_falls_back_to_v1_when_ds2_task_is_v1_only(self, mock_client: DsmClient) -> None:
        _has_default()
        mock_client.api_cache[TASK2] = mock_client.api_cache[TASK2].model_copy(
            update={"max_version": 1}
        )
        v1 = post_route(TASK, "create", version=1).respond(json=captured("v1_task_create_url"))
        await create_download(mock_client, uri="magnet:?xt=a")
        assert v1.called

    @respx.mock
    async def test_other_ds2_error_is_surfaced_not_retried(self, mock_client: DsmClient) -> None:
        """Retrying after a post-commit failure could create duplicate downloads."""
        _has_default()
        post_route(TASK2, "create", version=2).respond(json=err(401))
        v1 = post_route(TASK, "create", version=1).respond(json=ok())
        with pytest.raises(ToolError, match="Max number of tasks"):
            await create_download(mock_client, uri="magnet:?xt=a")
        assert not v1.called

    @respx.mock
    async def test_ds2_http_5xx_is_surfaced_not_retried(self, mock_client: DsmClient) -> None:
        _has_default()
        post_route(TASK2, "create", version=2).respond(status_code=502)
        v1 = post_route(TASK, "create", version=1).respond(json=ok())
        with pytest.raises(ToolError) as exc:
            await create_download(mock_client, uri="magnet:?xt=a")
        envelope = _envelope(exc.value)
        assert envelope["code"] == "unavailable"
        # DSM may already have created the task: a client retry could duplicate it.
        assert envelope["retryable"] is False
        assert not v1.called

    @respx.mock
    async def test_ds2_timeout_is_surfaced_not_retried(self, mock_client: DsmClient) -> None:
        _has_default()
        post_route(TASK2, "create", version=2).mock(side_effect=httpx.ReadTimeout("slow"))
        v1 = post_route(TASK, "create", version=1).respond(json=ok())
        with pytest.raises(ToolError) as exc:
            await create_download(mock_client, uri="magnet:?xt=a")
        envelope = _envelope(exc.value)
        assert envelope["code"] == "timeout"
        assert envelope["retryable"] is False
        assert not v1.called

    @respx.mock
    async def test_ds2_error_120_is_invalid_parameter(self, mock_client: DsmClient) -> None:
        _has_default()
        post_route(TASK2, "create", version=2).respond(json=err(120))
        with pytest.raises(ToolError) as exc:
            await create_download(mock_client, uri="magnet:?xt=a")
        envelope = _envelope(exc.value)
        assert envelope["code"] == "invalid_parameter"
        assert envelope["help_url"].endswith("#invalid_parameter")

    @respx.mock
    async def test_credentials_go_to_v1_in_post_body(self, mock_client: DsmClient) -> None:
        """HTTP-auth sources use v1 (DS2 url-create credential fields are
        unverified); credentials travel in the POST body, never the URL."""
        _has_default()
        ds2 = post_route(TASK2, "create").respond(json=ok())
        v1 = post_route(TASK, "create", version=1).respond(json=ok())
        await create_download(
            mock_client, uri="http://example.com/f.bin", username="u", password="s3cret"
        )
        assert not ds2.called
        request = v1.calls.last.request
        assert "s3cret" not in str(request.url)
        form = _form(request)
        assert form["username"] == "u"
        assert form["password"] == "s3cret"

    @respx.mock
    async def test_session_error_triggers_one_reauth_retry(self, mock_client: DsmClient) -> None:
        _has_default()
        reauths = 0

        async def fake_reauth() -> None:
            nonlocal reauths
            reauths += 1

        mock_client.set_re_auth_callback(fake_reauth)
        post_route(TASK2, "create", version=2).mock(
            side_effect=[
                httpx.Response(200, json=err(106)),
                httpx.Response(200, json=ok({"list_id": [], "task_id": ["dbid_3"]})),
            ]
        )
        result = await create_download(mock_client, uri="magnet:?xt=a")
        assert "dbid_3" in result
        assert reauths == 1

    @respx.mock
    async def test_v1_fallback_error_is_surfaced(self, mock_client: DsmClient) -> None:
        _has_default()
        del mock_client.api_cache[TASK2]
        post_route(TASK, "create", version=1).respond(json=err(403))
        with pytest.raises(ToolError, match="Destination doesn't exist"):
            await create_download(mock_client, uri="magnet:?xt=a", destination="nope")

    async def test_only_commas_is_an_empty_uri(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError, match="empty"):
            await create_download(mock_client, uri=",,")

    async def test_neither_uri_nor_torrent_path_raises(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError, match="uri"):
            await create_download(mock_client)

    async def test_both_uri_and_torrent_path_raises(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        torrent = tmp_path / "x.torrent"
        torrent.write_bytes(b"x")
        with pytest.raises(ToolError, match="exactly one"):
            await create_download(mock_client, uri="magnet:?xt=a", torrent_file_path=str(torrent))


def _default_destination(dest: str | None, *, is_manager: bool = True) -> respx.Route:
    """Route Info.getconfig with the user's default destination (None = never set)."""
    body = captured("info_getconfig_admin" if is_manager else "info_getconfig_user")
    body["data"]["default_destination"] = dest
    return get_route(INFO, "getconfig", version=1).respond(json=body)


def _has_default() -> None:
    """A manager whose default destination is set (the normal case)."""
    _default_destination("writable")


class TestCreateDownloadDestination:
    """#133: DS2 create answers 120 to any user who omits destination, and DS
    never starts a non-manager's tasks until their default destination is set
    (DS 3.9 and 4.1). create_download resolves the default itself."""

    @respx.mock
    async def test_omitted_destination_uses_the_users_default(self, mock_client: DsmClient) -> None:
        _default_destination("downloads")
        route = post_route(TASK2, "create", version=2).respond(json=captured("ds2_task_create_url"))
        await create_download(mock_client, uri="magnet:?xt=a")
        assert _form(route.calls.last.request)["destination"] == '"downloads"'

    @respx.mock
    async def test_explicit_destination_wins_over_default(self, mock_client: DsmClient) -> None:
        _default_destination("downloads")
        route = post_route(TASK2, "create", version=2).respond(json=captured("ds2_task_create_url"))
        await create_download(mock_client, uri="magnet:?xt=a", destination="writable")
        assert _form(route.calls.last.request)["destination"] == '"writable"'

    @respx.mock
    async def test_file_station_style_destination_is_made_share_relative(
        self, mock_client: DsmClient
    ) -> None:
        # DSM answers 403 "Destination doesn't exist" to "/writable" (vdsm 7.2.2).
        _has_default()
        route = post_route(TASK2, "create", version=2).respond(json=captured("ds2_task_create_url"))
        await create_download(mock_client, uri="magnet:?xt=a", destination="/writable")
        assert _form(route.calls.last.request)["destination"] == '"writable"'

    @respx.mock
    async def test_non_manager_without_default_and_destination_is_refused(
        self, mock_client: DsmClient
    ) -> None:
        _default_destination(None, is_manager=False)
        _manager(is_manager=False)
        route = post_route(TASK2, "create", version=2).respond(json=captured("ds2_task_create_url"))
        with pytest.raises(ToolError) as exc:
            await create_download(mock_client, uri="magnet:?xt=a")
        envelope = _envelope(exc.value)
        assert envelope["code"] == "invalid_parameter"
        assert "set_download_config" in envelope["suggestion"]
        assert not route.called

    @respx.mock
    async def test_non_manager_without_default_gets_a_not_started_note(
        self, mock_client: DsmClient
    ) -> None:
        _default_destination(None, is_manager=False)
        _manager(is_manager=False)
        post_route(TASK2, "create", version=2).respond(json=captured("ds2_task_create_url"))
        result = await create_download(mock_client, uri="magnet:?xt=a", destination="writable")
        assert "dbid_1" in result
        assert "won't start" in result
        assert "set_download_config(default_destination=" in result

    @respx.mock
    async def test_manager_without_default_or_destination_is_refused(
        self, mock_client: DsmClient
    ) -> None:
        """DS2 create answers 120 to any user who omits destination, even an admin
        with a default (DS 3.9/4.1), so with no default to fill in, nothing is sent."""
        _default_destination(None)
        _manager()
        route = post_route(TASK2, "create", version=2).respond(json=captured("ds2_task_create_url"))
        with pytest.raises(ToolError) as exc:
            await create_download(mock_client, uri="magnet:?xt=a")
        envelope = _envelope(exc.value)
        assert envelope["code"] == "invalid_parameter"
        assert "destination" in envelope["suggestion"]
        assert "set_download_config" not in envelope["suggestion"]
        assert not route.called

    @respx.mock
    async def test_manager_without_default_with_destination_has_no_note(
        self, mock_client: DsmClient
    ) -> None:
        _default_destination(None)
        _manager()
        route = post_route(TASK2, "create", version=2).respond(json=captured("ds2_task_create_url"))
        result = await create_download(mock_client, uri="magnet:?xt=a", destination="writable")
        assert _form(route.calls.last.request)["destination"] == '"writable"'
        assert "won't start" not in result

    @respx.mock
    async def test_info_api_absent_is_unchanged(self, mock_client: DsmClient) -> None:
        del mock_client.api_cache[INFO]
        route = post_route(TASK2, "create", version=2).respond(json=captured("ds2_task_create_url"))
        await create_download(mock_client, uri="magnet:?xt=a", destination="writable")
        assert _form(route.calls.last.request)["destination"] == '"writable"'

    @respx.mock
    async def test_unreadable_config_is_unchanged(self, mock_client: DsmClient) -> None:
        get_route(INFO, "getconfig", version=1).respond(json=err(105))
        route = post_route(TASK2, "create", version=2).respond(json=captured("ds2_task_create_url"))
        await create_download(mock_client, uri="magnet:?xt=a")
        assert "destination" not in _form(route.calls.last.request)


class TestCreateDownloadFile:
    """File create goes to DS2 Task.create (multipart, part named 'torrent')."""

    @respx.mock
    async def test_torrent_upload_uses_ds2_multipart(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        _has_default()
        torrent = tmp_path / "ubuntu.torrent"
        torrent.write_bytes(b"d4:infod6:lengthi100eee")
        route = respx.post("http://nas:5000/webapi/entry.cgi").respond(
            json=captured("ds2_task_create_url")
        )
        result = await create_download(
            mock_client, torrent_file_path=str(torrent), destination="writable"
        )
        assert "dbid_1" in result
        body = route.calls.last.request.content
        assert b'name="torrent"; filename="ubuntu.torrent"' in body
        assert b'name="api"\r\n\r\nSYNO.DownloadStation2.Task' in body
        assert b'name="type"\r\n\r\n"file"' in body
        assert b'name="file"\r\n\r\n["torrent"]' in body
        assert b'name="destination"\r\n\r\n"writable"' in body
        assert b'name="create_list"\r\n\r\nfalse' in body

    @respx.mock
    async def test_torrent_upload_timeout_is_a_non_retryable_timeout(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """#154: the tool reports a structured timeout and says to check before
        retrying, since DSM may already have created the task."""
        _has_default()
        torrent = tmp_path / "x.torrent"
        torrent.write_bytes(b"x")
        respx.post("http://nas:5000/webapi/entry.cgi").mock(side_effect=httpx.ReadTimeout(""))
        with pytest.raises(ToolError) as exc:
            await create_download(mock_client, torrent_file_path=str(torrent))
        envelope = _envelope(exc.value)
        assert envelope["code"] == "timeout"
        assert envelope["retryable"] is False
        assert "list_downloads" in envelope.get("suggestion", "") or "check" in envelope.get(
            "suggestion", ""
        )

    @respx.mock
    @pytest.mark.parametrize("name", ["id_rsa", "notes.txt", "x.torrent.sh"])
    async def test_torrent_wrong_extension_refused(
        self, mock_client: DsmClient, tmp_path: Path, name: str
    ) -> None:
        """#257: only .torrent and .nzb files, checked before anything is read or sent."""
        route = respx.post("http://nas:5000/webapi/entry.cgi")
        f = tmp_path / name
        f.write_bytes(b"secret")
        with pytest.raises(ToolError) as exc:
            await create_download(mock_client, torrent_file_path=str(f), local_roots=[tmp_path])
        envelope = _envelope(exc.value)
        assert envelope["code"] == "invalid_parameter"
        assert envelope["param"] == "torrent_file_path"
        assert ".torrent" in envelope["message"]
        assert ".nzb" in envelope["message"]
        assert not route.called

    @respx.mock
    @pytest.mark.parametrize("name", ["UPPER.TORRENT", "news.nzb", "a.Nzb"])
    async def test_torrent_extension_case_insensitive(
        self, mock_client: DsmClient, tmp_path: Path, name: str
    ) -> None:
        _has_default()
        respx.post("http://nas:5000/webapi/entry.cgi").respond(json=captured("ds2_task_create_url"))
        f = tmp_path / name
        f.write_bytes(b"d4:infod6:lengthi100eee")
        result = await create_download(
            mock_client, torrent_file_path=str(f), local_roots=[tmp_path]
        )
        assert "dbid_1" in result

    @respx.mock
    async def test_torrent_outside_roots_refused_before_existence(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """#257: a path outside the folders is refused whether or not it exists, so the
        reply never says what exists outside."""
        route = respx.post("http://nas:5000/webapi/entry.cgi")
        (tmp_path / "ok").mkdir()
        outside = tmp_path / "out.torrent"
        outside.write_bytes(b"x")
        for path in (str(outside), "/nonexistent/x.torrent"):
            with pytest.raises(ToolError) as exc:
                await create_download(
                    mock_client, torrent_file_path=path, local_roots=[tmp_path / "ok"]
                )
            envelope = _envelope(exc.value)
            assert envelope["code"] == "permission_denied", path
            assert "allowed_local_paths" in envelope["suggestion"]
            assert "downloadstation" in envelope["suggestion"]
        assert not route.called

    @respx.mock
    async def test_torrent_symlink_out_refused(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        route = respx.post("http://nas:5000/webapi/entry.cgi")
        ok = tmp_path / "ok"
        ok.mkdir()
        secret = tmp_path / "id_rsa"
        secret.write_bytes(b"secret")
        link = ok / "link.torrent"
        link.symlink_to(secret)
        with pytest.raises(ToolError) as exc:
            await create_download(mock_client, torrent_file_path=str(link), local_roots=[ok])
        envelope = _envelope(exc.value)
        assert envelope["code"] == "permission_denied"
        assert "resolves to" in envelope["message"]
        assert not route.called

    @respx.mock
    async def test_torrent_inside_roots_uploads(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        _has_default()
        route = respx.post("http://nas:5000/webapi/entry.cgi").respond(
            json=captured("ds2_task_create_url")
        )
        torrent = tmp_path / "ubuntu.torrent"
        torrent.write_bytes(b"d4:infod6:lengthi100eee")
        result = await create_download(
            mock_client, torrent_file_path=str(torrent), local_roots=[tmp_path]
        )
        assert "dbid_1" in result
        assert route.called

    @respx.mock
    async def test_torrent_unknown_home_refused_like_outside(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """Final review #257: an unknown ~user is the same refusal as any path outside, so
        the reply never reveals which local users exist."""
        route = respx.post("http://nas:5000/webapi/entry.cgi")
        envelopes = []
        for path in ("~nosuchuserxyz257/x.torrent", "~root/x.torrent"):
            with pytest.raises(ToolError) as exc:
                await create_download(mock_client, torrent_file_path=path, local_roots=[tmp_path])
            envelopes.append(_envelope(exc.value))
        assert [e["code"] for e in envelopes] == ["permission_denied", "permission_denied"]
        assert not route.called

    @respx.mock
    async def test_torrent_symlink_to_other_file_type_refused(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """Final review #257: the extension rule applies to the file a link points to."""
        route = respx.post("http://nas:5000/webapi/entry.cgi")
        secret = tmp_path / "secret.pdf"
        secret.write_bytes(b"%PDF")
        link = tmp_path / "inner.torrent"
        link.symlink_to(secret)
        with pytest.raises(ToolError) as exc:
            await create_download(mock_client, torrent_file_path=str(link), local_roots=[tmp_path])
        envelope = _envelope(exc.value)
        assert envelope["code"] == "invalid_parameter"
        assert envelope["param"] == "torrent_file_path"
        assert not route.called

    async def test_torrent_unknown_home_unconfined_is_invalid_parameter(
        self, mock_client: DsmClient
    ) -> None:
        """Direct (unconfined) callers get a structured error, not a crash."""
        with pytest.raises(ToolError) as exc:
            await create_download(mock_client, torrent_file_path="~nosuchuserxyz257/x.torrent")
        assert _envelope(exc.value)["code"] == "invalid_parameter"

    async def test_missing_file_raises_not_found(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError) as exc:
            await create_download(mock_client, torrent_file_path="/nonexistent/file.torrent")
        assert _envelope(exc.value)["code"] == "not_found"

    async def test_credentials_with_torrent_file_are_refused(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        torrent = tmp_path / "x.torrent"
        torrent.write_bytes(b"x")
        with pytest.raises(ToolError, match="only apply to `uri`"):
            await create_download(mock_client, torrent_file_path=str(torrent), username="u")

    @respx.mock
    async def test_ds2_absent_gives_clear_error(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """v1 multipart upload fails with 101 on DSM 7.2.2 in every variant
        (#123 bug 6), so there is deliberately no v1 fallback for files."""
        _has_default()
        del mock_client.api_cache[TASK2]
        torrent = tmp_path / "x.torrent"
        torrent.write_bytes(b"x")
        with pytest.raises(ToolError) as exc:
            await create_download(mock_client, torrent_file_path=str(torrent))
        assert _envelope(exc.value)["code"] == "api_not_found"


class TestDeleteDownload:
    """Delete semantics verified live (#123 bug 7, #128): finished files are
    kept; an unfinished HTTP task's partial data is discarded, and DS 3.9/4.1
    do so even with force_complete (documented as moving it).
    """

    @respx.mock
    async def test_delete_sends_ids_and_force_complete_false_by_default(
        self, mock_client: DsmClient
    ) -> None:
        route = get_route(TASK, "delete", version=1).respond(json=captured("task_delete_v1"))
        result = await delete_download(mock_client, task_ids=["dbid_1", "dbid_2"])
        sent = dict(route.calls.last.request.url.params)
        assert sent["id"] == "dbid_1,dbid_2"
        assert sent["force_complete"] == "false"
        assert "dbid_1" in result and "dbid_2" in result

    @respx.mock
    async def test_result_never_claims_files_were_removed(self, mock_client: DsmClient) -> None:
        get_route(TASK, "delete", version=1).respond(json=captured("task_delete_v1"))
        result = await delete_download(mock_client, task_ids=["dbid_1", "dbid_2"])
        assert "files removed" not in result
        assert "completed files kept" in result

    @respx.mock
    async def test_force_complete_true_is_passed_and_described(
        self, mock_client: DsmClient
    ) -> None:
        route = get_route(TASK, "delete", version=1).respond(
            json=ok([{"error": 0, "id": "dbid_1"}])
        )
        result = await delete_download(mock_client, task_ids=["dbid_1"], force_complete=True)
        assert dict(route.calls.last.request.url.params)["force_complete"] == "true"
        # #128: DSM discarded partial HTTP data despite force_complete, so the
        # result must not claim a move; it says to check the destination.
        assert "moved into" not in result
        assert "check the destination" in result

    def test_tool_description_does_not_promise_a_move(self) -> None:
        from mcp_synology.modules.downloadstation import MODULE_INFO

        tool = next(t for t in MODULE_INFO.tools if t.name == "delete_download")
        assert "moves" not in tool.description
        assert "force_complete" in tool.description
        # #130: DSM refuses force_complete on finished tasks (they stay listed).
        assert "refuses `force_complete` on finished tasks" in tool.description

    @respx.mock
    async def test_per_task_error_rendered(self, mock_client: DsmClient) -> None:
        get_route(TASK, "delete", version=1).respond(
            json=ok([{"error": 0, "id": "dbid_1"}, {"error": 405, "id": "dbid_2"}])
        )
        result = await delete_download(mock_client, task_ids=["dbid_1", "dbid_2"])
        assert "error 405" in result

    @respx.mock
    async def test_title_counts_only_removed_tasks(self, mock_client: DsmClient) -> None:
        get_route(TASK, "delete", version=1).respond(
            json=ok([{"error": 0, "id": "dbid_1"}, {"error": 405, "id": "dbid_2"}])
        )
        result = await delete_download(mock_client, task_ids=["dbid_1", "dbid_2"])
        assert "1 of 2 task(s) removed" in result

    @respx.mock
    async def test_force_complete_on_finished_task_405_says_retry_without(
        self, mock_client: DsmClient
    ) -> None:
        # Live on DS 3.9 and 4.1: force_complete=true on a finished task gets a
        # per-task 405 and the task stays listed; force_complete=false deletes it.
        get_route(TASK, "delete", version=1).respond(json=ok([{"error": 405, "id": "dbid_1"}]))
        result = await delete_download(mock_client, task_ids=["dbid_1"], force_complete=True)
        assert "0 of 1 task(s) removed" in result
        assert "error 405" in result
        assert "retry them with force_complete=false" in result

    @respx.mock
    async def test_405_without_force_complete_has_no_retry_hint(
        self, mock_client: DsmClient
    ) -> None:
        get_route(TASK, "delete", version=1).respond(json=ok([{"error": 405, "id": "dbid_1"}]))
        result = await delete_download(mock_client, task_ids=["dbid_1"])
        assert "0 of 1 task(s) removed" in result
        assert "force_complete=false" not in result

    async def test_empty_task_ids_raises(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError, match="task_ids"):
            await delete_download(mock_client, task_ids=[])

    @respx.mock
    async def test_dsm_error_propagates(self, mock_client: DsmClient) -> None:
        get_route(TASK, "delete", version=1).respond(json=err(105))
        with pytest.raises(ToolError) as exc:
            await delete_download(mock_client, task_ids=["dbid_1"])
        assert _envelope(exc.value)["code"] == "permission_denied"


class TestPauseResume:
    @respx.mock
    async def test_pause_uses_pause_method_and_renders_captured_results(
        self, mock_client: DsmClient
    ) -> None:
        route = get_route(TASK, "pause", version=1).respond(json=captured("task_pause_v1"))
        result = await pause_download(mock_client, task_ids=["dbid_1", "dbid_2"])
        assert dict(route.calls.last.request.url.params)["id"] == "dbid_1,dbid_2"
        assert "ok" in result
        assert "error 405" in result  # captured: dbid_2 could not be paused

    @respx.mock
    async def test_resume_uses_resume_method(self, mock_client: DsmClient) -> None:
        route = get_route(TASK, "resume", version=1).respond(json=captured("task_resume_v1"))
        await resume_download(mock_client, task_ids=["dbid_1", "dbid_2"])
        assert route.called

    async def test_empty_task_ids_raises(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError, match="task_ids"):
            await pause_download(mock_client, task_ids=[])

    @respx.mock
    async def test_dsm_error_propagates(self, mock_client: DsmClient) -> None:
        get_route(TASK, "resume", version=1).respond(json=err(105))
        with pytest.raises(ToolError) as exc:
            await resume_download(mock_client, task_ids=["dbid_1"])
        assert _envelope(exc.value)["code"] == "permission_denied"


class TestEditDownload:
    @respx.mock
    async def test_edit_is_pinned_to_task_v2(self, mock_client: DsmClient) -> None:
        """#123 bug 5: Task.edit is "2 and later"; v1 returns 103."""
        route = get_route(TASK, "edit", version=2).respond(json=captured("task_edit_v2"))
        result = await edit_download(mock_client, task_ids=["dbid_1"], destination="testshare")
        assert dict(route.calls.last.request.url.params)["destination"] == "testshare"
        assert "dbid_1" in result

    @respx.mock
    async def test_file_station_style_destination_is_made_share_relative(
        self, mock_client: DsmClient
    ) -> None:
        # DSM answers error 100 to "/testshare" (vdsm 7.2.2).
        route = get_route(TASK, "edit", version=2).respond(json=captured("task_edit_v2"))
        await edit_download(mock_client, task_ids=["dbid_1"], destination="/testshare/")
        assert dict(route.calls.last.request.url.params)["destination"] == "testshare"

    @respx.mock
    async def test_edit_100_hints_at_the_destination(self, mock_client: DsmClient) -> None:
        """QA on #147: a missing folder (e.g. "/volume1/testshare") makes Task.edit
        fail with a bare 100 on vdsm 7.1 and 7.2.2; point the model at the cause."""
        get_route(TASK, "edit", version=2).respond(json=err(100))
        with pytest.raises(ToolError) as exc:
            await edit_download(mock_client, task_ids=["dbid_1"], destination="/volume1/testshare")
        envelope = _envelope(exc.value)
        assert "DSM error 100" in envelope["message"]
        assert "share-relative" in envelope["suggestion"]
        assert "/volume1" in envelope["suggestion"]
        assert "list_files" in envelope["suggestion"]
        assert envelope["suggestion"].endswith(
            "Download Station doesn't create missing folders: create it first with "
            "create_folder, then retry."
        )

    @respx.mock
    async def test_create_403_mentions_the_volume_form(self, mock_client: DsmClient) -> None:
        _has_default()
        post_route(TASK2, "create", version=2).respond(json=err(403))
        with pytest.raises(ToolError) as exc:
            await create_download(
                mock_client, uri="magnet:?xt=urn:btih:abc", destination="/volume1/video"
            )
        suggestion = _envelope(exc.value)["suggestion"]
        assert "/volume1" in suggestion
        # Download Station doesn't create missing folders; name the fix (#296).
        assert "doesn't create missing folders: create it first with create_folder" in suggestion

    async def test_nas_with_task_v1_only_gets_clear_error(self, mock_client: DsmClient) -> None:
        mock_client.api_cache[TASK] = mock_client.api_cache[TASK].model_copy(
            update={"max_version": 1}
        )
        with pytest.raises(ToolError, match="requires a newer Download Station"):
            await edit_download(mock_client, task_ids=["dbid_1"], destination="testshare")

    async def test_no_destination_raises(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError, match="destination"):
            await edit_download(mock_client, task_ids=["dbid_1"])

    async def test_empty_task_ids_raises(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError, match="task_ids"):
            await edit_download(mock_client, task_ids=[], destination="writable")

    @respx.mock
    async def test_per_task_error_rendered(self, mock_client: DsmClient) -> None:
        get_route(TASK, "edit", version=2).respond(
            json=ok([{"error": 0, "id": "dbid_1"}, {"error": 407, "id": "dbid_2"}])
        )
        result = await edit_download(
            mock_client, task_ids=["dbid_1", "dbid_2"], destination="writable"
        )
        assert "error 407" in result

    @respx.mock
    async def test_dsm_error_propagates(self, mock_client: DsmClient) -> None:
        get_route(TASK, "edit", version=2).respond(json=err(105))
        with pytest.raises(ToolError) as exc:
            await edit_download(mock_client, task_ids=["dbid_1"], destination="writable")
        assert _envelope(exc.value)["code"] == "permission_denied"


class TestTaskNotFoundOrNotYours:
    """#136, probed on DS 3.9 and 4.1: another user's task and a task that
    doesn't exist look the same. resume/edit/delete answer per-task 544; pause
    answers per-task 404 in a mixed list and fails the whole call with 101 when
    no ID can be paused (missing, not yours, or the caller's own finished or
    already-paused task); getinfo fails with 404."""

    NOT_FOUND = "not found (no such task, or not yours)"

    @respx.mock
    async def test_resume_544_is_not_found_with_hint(self, mock_client: DsmClient) -> None:
        get_route(TASK, "resume", version=1).respond(
            json=ok([{"error": 544, "id": "dbid_9"}, {"error": 0, "id": "dbid_1"}])
        )
        result = await resume_download(mock_client, task_ids=["dbid_9", "dbid_1"])
        assert self.NOT_FOUND in result
        assert "error 544" not in result
        assert "1 task(s) not found" in result
        assert "list_downloads" in result

    @respx.mock
    async def test_pause_404_in_mixed_list_is_not_found(self, mock_client: DsmClient) -> None:
        get_route(TASK, "pause", version=1).respond(
            json=ok([{"error": 0, "id": "dbid_1"}, {"error": 404, "id": "dbid_9"}])
        )
        result = await pause_download(mock_client, task_ids=["dbid_1", "dbid_9"])
        assert self.NOT_FOUND in result

    @respx.mock
    async def test_pause_101_names_every_cause(self, mock_client: DsmClient) -> None:
        """QA on #146: pause's whole-call 101 also covers the caller's own task when it
        is finished or already paused, so the message can't say "not found"."""
        get_route(TASK, "pause", version=1).respond(json=err(101))
        with pytest.raises(ToolError) as exc:
            await pause_download(mock_client, task_ids=["dbid_9"])
        envelope = _envelope(exc.value)
        assert envelope["code"] == "invalid_parameter"
        assert "could be paused" in envelope["message"]
        assert "another user" in envelope["message"]
        assert "finished, or already paused" in envelope["message"]
        assert "were found" not in envelope["message"]
        assert "list_downloads" in envelope["suggestion"]
        assert "status" in envelope["suggestion"]

    @respx.mock
    async def test_resume_101_is_not_reinterpreted(self, mock_client: DsmClient) -> None:
        """Only pause answers 101 for unknown IDs; elsewhere 101 stays a 101."""
        get_route(TASK, "resume", version=1).respond(json=err(101))
        with pytest.raises(ToolError) as exc:
            await resume_download(mock_client, task_ids=["dbid_9"])
        assert _envelope(exc.value)["code"] != "not_found"

    @respx.mock
    async def test_edit_544_is_not_found(self, mock_client: DsmClient) -> None:
        get_route(TASK, "edit", version=2).respond(json=ok([{"error": 544, "id": "dbid_9"}]))
        result = await edit_download(mock_client, task_ids=["dbid_9"], destination="writable")
        assert self.NOT_FOUND in result

    @respx.mock
    async def test_delete_nothing_removed_says_so(self, mock_client: DsmClient) -> None:
        get_route(TASK, "delete", version=1).respond(json=ok([{"error": 544, "id": "dbid_9"}]))
        result = await delete_download(mock_client, task_ids=["dbid_9"])
        title = result.splitlines()[0]
        assert "0 of 1 task(s) removed" in title
        assert "nothing was deleted" in title
        assert "discarded" not in title
        assert self.NOT_FOUND in result

    @respx.mock
    async def test_getinfo_404_mentions_other_users(self, mock_client: DsmClient) -> None:
        get_route(TASK, "getinfo", version=1).respond(json=captured("task_getinfo_bogus"))
        with pytest.raises(ToolError) as exc:
            await get_download_info(mock_client, task_id="dbid_9")
        assert "another user" in _envelope(exc.value)["suggestion"]
