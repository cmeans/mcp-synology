"""Tests for modules/filestation/search.py — search_files."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.filestation.search import search_files
from tests.conftest import BASE_URL

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient


class TestSearchFiles:
    @respx.mock
    async def test_search_success(self, mock_client: DsmClient) -> None:
        call_count = 0

        def side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            params = dict(request.url.params)
            if params.get("method") == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "search-1"}})
            if params.get("method") == "list":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "files": [
                                {
                                    "name": "Severance.S02E10.mkv",
                                    "path": "/video/Downloads/Severance.S02E10.mkv",
                                    "isdir": False,
                                    "additional": {
                                        "size": 1932735283,
                                        "time": {"mtime": 1710000000},
                                    },
                                },
                            ],
                            "finished": True,
                            "total": 1,
                        },
                    },
                )
            # stop/clean
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        result = await search_files(mock_client, folder_path="/video", pattern="*Severance*")
        assert "Severance.S02E10.mkv" in result
        assert "1 results found" in result

    @respx.mock
    async def test_search_with_exclude_pattern(self, mock_client: DsmClient) -> None:
        call_count = 0

        def side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            params = dict(request.url.params)
            if params.get("method") == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "search-2"}})
            if params.get("method") == "list":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "files": [
                                {
                                    "name": "Severance.S02E10.mkv",
                                    "path": "/video/Severance.S02E10.mkv",
                                    "isdir": False,
                                    "additional": {"size": 1932735283, "time": {"mtime": 0}},
                                },
                                {
                                    "name": "Severance.S02E10.torrent",
                                    "path": "/video/Severance.S02E10.torrent",
                                    "isdir": False,
                                    "additional": {"size": 1024, "time": {"mtime": 0}},
                                },
                            ],
                            "finished": True,
                            "total": 2,
                        },
                    },
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        result = await search_files(
            mock_client,
            folder_path="/video",
            pattern="*Severance*",
            exclude_pattern="*.torrent",
        )
        assert "Severance.S02E10.mkv" in result
        assert "torrent" not in result.split("results")[0]  # not in the data rows
        assert "1 excluded by filter" in result

    @respx.mock
    async def test_search_zero_results(self, mock_client: DsmClient) -> None:
        call_count = 0

        def side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            params = dict(request.url.params)
            if params.get("method") == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "search-3"}})
            if params.get("method") == "list":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {"files": [], "finished": True, "total": 0},
                    },
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        result = await search_files(mock_client, folder_path="/video", pattern="nonexistent")
        assert "0 results" in result

    @respx.mock
    async def test_search_error(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 408}}
        )
        with pytest.raises(ToolError) as exc_info:
            await search_files(mock_client, folder_path="/nonexistent")
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "not_found"

    @respx.mock
    async def test_search_poll_error_mid_operation(self, mock_client: DsmClient) -> None:
        """DSM fails on the status/list call after search task was started.

        Exercises the ``poll_error = e`` branch and the subsequent
        ``synology_error_response("Search files", poll_error)`` after
        the try/finally cleanup runs. Prior to this test, those lines
        were uncovered in the patch.
        """

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "search-err"}})
            if method == "list":
                # First list call fails — DSM busy
                return httpx.Response(200, json={"success": False, "error": {"code": 402}})
            # stop/clean
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        with pytest.raises(ToolError) as exc_info:
            await search_files(mock_client, folder_path="/video", pattern="test")
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        # Code 402 "System too busy" is not specifically typed → filestation_error
        assert body["error"]["code"] == "filestation_error"

    @respx.mock
    async def test_search_with_size_filter(self, mock_client: DsmClient) -> None:
        call_count = 0

        def side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            params = dict(request.url.params)
            if params.get("method") == "start":
                # Verify size params were passed
                assert "size_from" in params
                return httpx.Response(200, json={"success": True, "data": {"taskid": "search-4"}})
            if params.get("method") == "list":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {"files": [], "finished": True, "total": 0},
                    },
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        await search_files(mock_client, folder_path="/video", size_from="500MB")

    @respx.mock
    async def test_search_with_mtime_filter(self, mock_client: DsmClient) -> None:
        captured: dict[str, str] = {}

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("method") == "start":
                # Capture so the assertions can verify epoch conversion.
                captured.update(params)
                return httpx.Response(200, json={"success": True, "data": {"taskid": "search-5"}})
            if params.get("method") == "list":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {"files": [], "finished": True, "total": 0},
                    },
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        # Calendar date for mtime_from, full ISO 8601 with offset for mtime_to.
        await search_files(
            mock_client,
            folder_path="/video",
            mtime_from="2026-04-01",
            mtime_to="2026-04-01T12:00:00+00:00",
        )

        # Both lands as Unix-epoch-seconds strings in DSM start params.
        assert captured.get("mtime_from") == "1775001600"  # 2026-04-01 00:00 UTC
        assert captured.get("mtime_to") == "1775044800"  # 2026-04-01 12:00 UTC

    @staticmethod
    def _capture_start(captured: dict[str, str]) -> None:
        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("method") == "start":
                captured.update(params)
                return httpx.Response(200, json={"success": True, "data": {"taskid": "search-6"}})
            if params.get("method") == "list":
                return httpx.Response(
                    200, json={"success": True, "data": {"files": [], "finished": True, "total": 0}}
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

    @respx.mock
    @pytest.mark.parametrize(
        ("kwargs", "pattern"),
        [
            # #306: with filetype=all and nothing to match on, DSM answers "Search folder
            # access denied" (DSM 7.2.2 and 7.3.2), so "*" is sent.
            ({}, json.dumps("*")),
            # filetype=file or dir alone works on DSM, so nothing is added.
            ({"filetype": "file"}, None),
            ({"filetype": "dir"}, None),
            # Any filter is enough for DSM.
            ({"mtime_from": "2026-04-01"}, None),
            ({"mtime_to": "2026-04-01"}, None),
            ({"size_from": "0"}, None),
            ({"size_to": "1GiB"}, None),
            # An empty pattern is no filter: DSM refuses pattern="" too.
            ({"pattern": ""}, json.dumps("*")),
            ({"extension": "mkv"}, None),
            ({"pattern": "*.mkv"}, None),  # becomes the extension filter
            ({"pattern": "Bambu"}, json.dumps("*Bambu*")),
            # exclude_pattern is applied here, not by DSM, so it doesn't count.
            ({"exclude_pattern": "*.torrent"}, json.dumps("*")),
        ],
    )
    async def test_pattern_star_only_when_nothing_narrows_an_all_search(
        self, mock_client: DsmClient, kwargs: dict[str, str], pattern: str | None
    ) -> None:
        captured: dict[str, str] = {}
        self._capture_start(captured)
        await search_files(mock_client, folder_path="/video", **kwargs)  # type: ignore[arg-type]
        assert captured.get("pattern") == pattern

    @respx.mock
    async def test_heading_names_only_the_callers_filters(self, mock_client: DsmClient) -> None:
        # exclude_pattern alone used to be refused by DSM (600); with "*" sent it works, and
        # the heading shows what the caller asked for, not the "*".
        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("method") == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "s-7"}})
            if params.get("method") == "list":
                files = [
                    {"name": "a.mkv", "path": "/video/a.mkv", "isdir": False},
                    {"name": "a.torrent", "path": "/video/a.torrent", "isdir": False},
                ]
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"files": files, "finished": True, "total": 2}},
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await search_files(mock_client, folder_path="/video", exclude_pattern="*.torrent")
        assert result.splitlines()[0] == "Search results in /video (excluding: *.torrent)"
        assert "a.mkv" in result and "a.torrent" not in result

    @respx.mock
    async def test_search_size_dash_when_not_requested(self, mock_client: DsmClient) -> None:
        """QA on #146: a file DSM sends without "size" must not show as "0 B"."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            method = dict(request.url.params).get("method")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "s-sz"}})
            if method == "list":
                found = {"name": "a.iso", "path": "/video/a.iso", "isdir": False}
                found["additional"] = {"owner": {"user": "admin"}}
                data = {"files": [found], "finished": True, "total": 1}
                return httpx.Response(200, json={"success": True, "data": data})
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await search_files(mock_client, folder_path="/video", pattern="a")
        assert "a.iso" in result
        assert "0 B" not in result

    @respx.mock
    async def test_search_requests_what_the_table_shows(self, mock_client: DsmClient) -> None:
        """#149: only Size and Modified are shown, so only size and time are fetched.

        For Search, `additional` is sent on the `list` (poll) request, not on
        `start` — DSM returns the full file metadata at poll time.
        """
        captured_list: dict[str, str] = {}

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("method") == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "s-add"}})
            if params.get("method") == "list":
                captured_list.update(params)
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"files": [], "finished": True, "total": 0}},
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        await search_files(mock_client, folder_path="/video")
        assert captured_list.get("additional") == '["size","time"]'
