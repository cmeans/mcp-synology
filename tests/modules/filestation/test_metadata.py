"""Tests for modules/filestation/metadata.py — get_file_info, get_dir_size."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest
import respx
from mcp.server.fastmcp.exceptions import ToolError

from mcp_synology.modules.filestation.metadata import get_dir_size, get_file_info
from tests.conftest import BASE_URL

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient


class TestGetFileInfo:
    @respx.mock
    async def test_single_file(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "files": [
                        {
                            "name": "movie.mkv",
                            "path": "/video/Movies/movie.mkv",
                            "isdir": False,
                            "additional": {
                                "real_path": "/volume1/video/Movies/movie.mkv",
                                "size": 19755850547,
                                "owner": {"user": "admin", "group": "users"},
                                "time": {
                                    "mtime": 1708266125,
                                    "crtime": 1708266012,
                                    "atime": 1710540600,
                                },
                                "perm": {"posix": 755},
                            },
                        }
                    ]
                },
            }
        )
        result = await get_file_info(mock_client, paths=["/video/Movies/movie.mkv"])
        assert "movie.mkv" in result
        assert "File Info:" in result
        assert "admin" in result

    @respx.mock
    async def test_multiple_files(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "files": [
                        {
                            "name": "a.mkv",
                            "path": "/video/a.mkv",
                            "isdir": False,
                            "additional": {"size": 1000, "time": {"mtime": 1710000000}},
                        },
                        {
                            "name": "b.srt",
                            "path": "/video/b.srt",
                            "isdir": False,
                            "additional": {"size": 500, "time": {"mtime": 1710000000}},
                        },
                    ]
                },
            }
        )
        result = await get_file_info(mock_client, paths=["/video/a.mkv", "/video/b.srt"])
        assert "a.mkv" in result
        assert "b.srt" in result

    @respx.mock
    async def test_error(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 408}}
        )
        with pytest.raises(ToolError) as exc_info:
            await get_file_info(mock_client, paths=["/nonexistent"])
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "not_found"

    @respx.mock
    async def test_multipath_uses_per_path_serial_calls(self, mock_client: DsmClient) -> None:
        """Closes #68 (the get_file_info half): DSM 7.x's
        `SYNO.FileStation.List getinfo` doesn't honor the documented
        comma-joined multi-path format on v2 (vdsm 7.2.2 verified — the
        comma-joined string lands as a literal single path, returning one
        synthetic record). Production therefore issues ONE DSM call per
        input path. Test asserts (a) N requests for N paths, (b) each
        request carries a single path (no commas), (c) all pinned to
        v2, (d) results aggregate into the table-format response.
        """
        captured: list[dict[str, str]] = []
        per_path_files = {
            "/video/a.mkv": {
                "name": "a.mkv",
                "path": "/video/a.mkv",
                "isdir": False,
                "additional": {"size": 1, "time": {"mtime": 1710000000}},
            },
            "/video/b.srt": {
                "name": "b.srt",
                "path": "/video/b.srt",
                "isdir": False,
                "additional": {"size": 1, "time": {"mtime": 1710000000}},
            },
        }

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            captured.append(params)
            requested_path = params.get("path", "")
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"files": [per_path_files[requested_path]]},
                },
            )

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await get_file_info(mock_client, paths=["/video/a.mkv", "/video/b.srt"])

        # Two paths → two DSM calls.
        assert len(captured) == 2, f"expected two DSM calls (one per path), got {len(captured)}"
        for params in captured:
            assert params["api"] == "SYNO.FileStation.List"
            assert params["method"] == "getinfo"
            assert params["version"] == "2", (
                f"expected version=2, got version={params['version']!r}"
            )
            assert "," not in params["path"], (
                f"each request must carry a single path (no comma-joined multipath), "
                f"got path={params['path']!r}"
            )
        # Each input path appears in exactly one request.
        sent_paths = sorted(p["path"] for p in captured)
        assert sent_paths == ["/video/a.mkv", "/video/b.srt"]
        # And the aggregated response renders both files in tabular form.
        assert "a.mkv" in result
        assert "b.srt" in result
        # No comma-joined string in the result — that would indicate a regression
        # back to the single-call form that #68 surfaced.
        assert "/video/a.mkv,/video/b.srt" not in result

    @respx.mock
    async def test_empty_files_list_returns_not_found(self, mock_client: DsmClient) -> None:
        """getinfo succeeds but returns no files → not_found.

        DSM returns ``success=true, files=[]`` for multi-path requests where
        every path is missing or unreadable. The tool converts this to
        not_found so clients don't have to special-case empty success.
        """
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"files": []}}
        )
        with pytest.raises(ToolError) as exc_info:
            await get_file_info(
                mock_client,
                paths=["/video/missing1", "/video/missing2"],
            )
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "not_found"
        assert body["error"]["retryable"] is False
        # F17: envelope names the offending argument so clients can dispatch
        assert body["error"]["param"] == "paths"
        assert body["error"]["value"] == ["/video/missing1", "/video/missing2"]


class TestGetDirSize:
    @respx.mock
    async def test_dir_size_success(self, mock_client: DsmClient) -> None:
        call_count = 0

        def side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            params = dict(request.url.params)
            if params.get("method") == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "ds-1"}})
            if params.get("method") == "status":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "finished": True,
                            "total_size": 45742428160,
                            "num_file": 186,
                            "num_dir": 12,
                        },
                    },
                )
            # stop
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        result = await get_dir_size(mock_client, path="/video/TV Shows/The Bear")
        assert "42.6 GB" in result
        assert "186" in result
        assert "12" in result

    @respx.mock
    async def test_dir_size_start_error(self, mock_client: DsmClient) -> None:
        """DSM error on the start call should propagate as a structured error."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 408}}
        )
        with pytest.raises(ToolError) as exc_info:
            await get_dir_size(mock_client, path="/nonexistent")
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "not_found"

    @respx.mock
    async def test_dir_size_poll_error(self, mock_client: DsmClient) -> None:
        """DSM error mid-poll should still clean up the task and raise.

        The task is started successfully but the first status call fails.
        The ``try/finally`` must still invoke stop/clean, and the error
        must be surfaced as a structured envelope (filestation_error for
        code 402, "System too busy").
        """

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "ds-2"}})
            if method == "status":
                return httpx.Response(200, json={"success": False, "error": {"code": 402}})
            # stop/clean
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        with pytest.raises(ToolError) as exc_info:
            await get_dir_size(mock_client, path="/video/busy")
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "filestation_error"

    @respx.mock
    async def test_dir_size_error_599_instant_completion(self, mock_client: DsmClient) -> None:
        """Error 599 on status poll means the task completed before we could read it.

        Common on Virtual DSM where tiny directories finish instantly.
        Should return a best-effort result instead of raising.
        """

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "ds-fast"}})
            if method == "status":
                return httpx.Response(200, json={"success": False, "error": {"code": 599}})
            # stop/clean
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        result = await get_dir_size(mock_client, path="/small/dir")
        assert "Total size" in result
        assert "completed" in result

    @respx.mock
    async def test_dir_size_timeout(self, mock_client: DsmClient) -> None:
        """Polling never returns finished → timeout error, retryable=True."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "ds-3"}})
            if method == "status":
                # Never mark finished — force the polling loop to exhaust
                # its budget.
                return httpx.Response(200, json={"success": True, "data": {"finished": False}})
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        with pytest.raises(ToolError) as exc_info:
            # Tight timeout so the test runs fast.
            await get_dir_size(mock_client, path="/video/huge", timeout=1.0)
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "timeout"
        assert body["error"]["retryable"] is True
        assert body["error"]["param"] == "timeout"
        assert body["error"]["value"] == 1.0
