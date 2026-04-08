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
