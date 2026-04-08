"""Tests for modules/filestation/search.py — search_files."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest
import respx
from mcp.server.fastmcp.exceptions import ToolError

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
