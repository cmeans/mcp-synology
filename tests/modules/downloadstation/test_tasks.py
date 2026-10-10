"""Tests for modules/downloadstation/tasks.py.

Payloads are real DSM captures (``dsm.captured``) and every route matches on
the DSM ``api`` / ``method`` (and ``version`` where pinned), so a wrong API
name, method, or version fails with an unmatched request — see #123.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from mcp.server.fastmcp.exceptions import ToolError

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


class TestCreateDownloadUri:
    """URI create goes to DS2 Task.create v2 as a form POST (#123 items 9/9a)."""

    @respx.mock
    async def test_ds2_post_returns_task_ids(self, mock_client: DsmClient) -> None:
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
        route = post_route(TASK2, "create", version=2).respond(
            json=ok({"list_id": [], "task_id": ["dbid_1", "dbid_2"]})
        )
        result = await create_download(
            mock_client, uri="http://a.example/1.iso,http://b.example/2.iso"
        )
        form = _form(route.calls.last.request)
        assert json.loads(form["url"]) == ["http://a.example/1.iso", "http://b.example/2.iso"]
        assert "destination" not in form
        assert "dbid_1" in result and "dbid_2" in result

    @respx.mock
    async def test_long_uri_list_is_sent_in_body_not_query(self, mock_client: DsmClient) -> None:
        """#123 bug 14: a GET URL past ~8 KB gets HTTP 414 from DSM."""
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
        post_route(TASK2, "create", version=2).respond(json=err(code))
        v1 = post_route(TASK, "create", version=1).respond(json=captured("v1_task_create_url"))
        result = await create_download(mock_client, uri="magnet:?xt=a", destination="writable")
        form = _form(v1.calls.last.request)
        assert form["uri"] == "magnet:?xt=a"
        assert form["destination"] == "writable"
        assert "did not return task IDs" in result

    @respx.mock
    async def test_falls_back_to_v1_when_ds2_absent(self, mock_client: DsmClient) -> None:
        del mock_client.api_cache[TASK2]
        v1 = post_route(TASK, "create", version=1).respond(json=captured("v1_task_create_url"))
        await create_download(mock_client, uri="magnet:?xt=a")
        assert v1.called

    @respx.mock
    async def test_falls_back_to_v1_when_ds2_task_is_v1_only(self, mock_client: DsmClient) -> None:
        mock_client.api_cache[TASK2] = mock_client.api_cache[TASK2].model_copy(
            update={"max_version": 1}
        )
        v1 = post_route(TASK, "create", version=1).respond(json=captured("v1_task_create_url"))
        await create_download(mock_client, uri="magnet:?xt=a")
        assert v1.called

    @respx.mock
    async def test_other_ds2_error_is_surfaced_not_retried(self, mock_client: DsmClient) -> None:
        """Retrying after a post-commit failure could create duplicate downloads."""
        post_route(TASK2, "create", version=2).respond(json=err(401))
        v1 = post_route(TASK, "create", version=1).respond(json=ok())
        with pytest.raises(ToolError, match="Max number of tasks"):
            await create_download(mock_client, uri="magnet:?xt=a")
        assert not v1.called

    @respx.mock
    async def test_ds2_http_5xx_is_surfaced_not_retried(self, mock_client: DsmClient) -> None:
        post_route(TASK2, "create", version=2).respond(status_code=502)
        v1 = post_route(TASK, "create", version=1).respond(json=ok())
        with pytest.raises(ToolError):
            await create_download(mock_client, uri="magnet:?xt=a")
        assert not v1.called

    @respx.mock
    async def test_ds2_timeout_is_surfaced_not_retried(self, mock_client: DsmClient) -> None:
        post_route(TASK2, "create", version=2).mock(side_effect=httpx.ReadTimeout("slow"))
        v1 = post_route(TASK, "create", version=1).respond(json=ok())
        with pytest.raises(ToolError) as exc:
            await create_download(mock_client, uri="magnet:?xt=a")
        assert _envelope(exc.value)["code"] == "timeout"
        assert not v1.called

    @respx.mock
    async def test_credentials_go_to_v1_in_post_body(self, mock_client: DsmClient) -> None:
        """HTTP-auth sources use v1 (DS2 url-create credential fields are
        unverified); credentials travel in the POST body, never the URL."""
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


class TestCreateDownloadFile:
    """File create goes to DS2 Task.create (multipart, part named 'torrent')."""

    @respx.mock
    async def test_torrent_upload_uses_ds2_multipart(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
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

    async def test_ds2_absent_gives_clear_error(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """v1 multipart upload fails with 101 on DSM 7.2.2 in every variant
        (#123 bug 6), so there is deliberately no v1 fallback for files."""
        del mock_client.api_cache[TASK2]
        torrent = tmp_path / "x.torrent"
        torrent.write_bytes(b"x")
        with pytest.raises(ToolError) as exc:
            await create_download(mock_client, torrent_file_path=str(torrent))
        assert _envelope(exc.value)["code"] == "api_not_found"


class TestDeleteDownload:
    """Delete semantics verified live (#123 bug 7): finished files are kept;
    unfinished tasks' partial data is discarded unless force_complete moves it.
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
        assert "moved into the destination" in result

    @respx.mock
    async def test_per_task_error_rendered(self, mock_client: DsmClient) -> None:
        get_route(TASK, "delete", version=1).respond(
            json=ok([{"error": 0, "id": "dbid_1"}, {"error": 405, "id": "dbid_2"}])
        )
        result = await delete_download(mock_client, task_ids=["dbid_1", "dbid_2"])
        assert "error 405" in result

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
