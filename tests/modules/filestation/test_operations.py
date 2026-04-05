"""Tests for modules/filestation/operations.py — WRITE tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import respx

from mcp_synology.modules.filestation.operations import (
    copy_files,
    create_folder,
    delete_files,
    move_files,
    rename,
    restore_from_recycle_bin,
)
from tests.conftest import BASE_URL

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient


def _async_task_side_effect(
    start_response: dict[str, object] | None = None,
) -> object:
    """Create a side_effect that handles start/status/stop pattern."""
    call_count = 0

    def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        params = dict(request.url.params)
        method = params.get("method", "")

        if method == "start":
            data = start_response or {"taskid": "task-1"}
            return httpx.Response(200, json={"success": True, "data": data})
        if method in ("status",):
            return httpx.Response(200, json={"success": True, "data": {"finished": True}})
        if method in ("stop", "clean"):
            return httpx.Response(200, json={"success": True, "data": {}})
        return httpx.Response(200, json={"success": True, "data": {}})

    return side_effect


class TestCreateFolder:
    @respx.mock
    async def test_create_folder_success(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "folders": [{"path": "/video/TV Shows/New Show/Season 1", "name": "Season 1"}]
                },
            }
        )
        result = await create_folder(mock_client, paths=["/video/TV Shows/New Show/Season 1"])
        assert "[+]" in result
        assert "Season 1" in result

    @respx.mock
    async def test_create_folder_error(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 418}}
        )
        result = await create_folder(mock_client, paths=["/video/bad<name"])
        assert "[!]" in result


class TestRename:
    @respx.mock
    async def test_rename_success(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "files": [
                        {
                            "name": "Severance",
                            "path": "/video/TV Shows/Severance",
                        }
                    ]
                },
            }
        )
        result = await rename(mock_client, path="/video/TV Shows/Severence", new_name="Severance")
        assert "[+]" in result
        assert "Severance" in result

    async def test_rename_rejects_path_in_name(self, mock_client: DsmClient) -> None:
        result = await rename(mock_client, path="/video/test", new_name="some/path/name")
        assert "[!]" in result
        assert "just a filename" in result

    @respx.mock
    async def test_rename_error(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 419}}
        )
        result = await rename(mock_client, path="/video/test", new_name="bad<name")
        assert "[!]" in result


class TestCopyFiles:
    @respx.mock
    async def test_copy_success(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_async_task_side_effect())
        result = await copy_files(
            mock_client,
            paths=["/video/Downloads/file.mkv"],
            dest_folder="/video/Archive",
        )
        assert "[+]" in result
        assert "Copied" in result
        assert "file.mkv" in result

    @respx.mock
    async def test_copy_error(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 414}}
        )
        result = await copy_files(
            mock_client,
            paths=["/video/file.mkv"],
            dest_folder="/video/dest",
        )
        assert "[!]" in result


class TestMoveFiles:
    @respx.mock
    async def test_move_success(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_async_task_side_effect())
        result = await move_files(
            mock_client,
            paths=["/video/Downloads/ep.mkv", "/video/Downloads/ep.srt"],
            dest_folder="/video/TV Shows/Show/Season 1",
        )
        assert "[+]" in result
        assert "Moved" in result
        assert "Source files have been removed" in result

    @respx.mock
    async def test_move_conflict(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 414}}
        )
        result = await move_files(
            mock_client,
            paths=["/video/file.mkv"],
            dest_folder="/video/dest",
        )
        assert "[!]" in result
        assert "exists" in result.lower()


class TestDeleteFiles:
    @respx.mock
    async def test_delete_with_recycle_bin(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_async_task_side_effect())
        result = await delete_files(
            mock_client,
            paths=["/video/Downloads/old.mkv"],
            recycle_bin_status={"video": True},
        )
        assert "[+]" in result
        assert "Deleted" in result
        assert "Recycle bin is enabled" in result

    @respx.mock
    async def test_delete_without_recycle_bin(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_async_task_side_effect())
        result = await delete_files(
            mock_client,
            paths=["/docker/temp/old.json"],
            recycle_bin_status={"docker": False},
        )
        assert "Permanently deleted" in result
        assert "NOT enabled" in result

    @respx.mock
    async def test_delete_error(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 408}}
        )
        result = await delete_files(mock_client, paths=["/nonexistent/file"])
        assert "[!]" in result

    @respx.mock
    async def test_delete_multiple_shares(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_async_task_side_effect())
        result = await delete_files(
            mock_client,
            paths=["/video/file.mkv", "/docker/file.json"],
            recycle_bin_status={"video": True, "docker": False},
        )
        assert "enabled" in result
        assert "NOT enabled" in result


class TestRestoreFromRecycleBin:
    @respx.mock
    async def test_restore_success(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_async_task_side_effect())
        result = await restore_from_recycle_bin(
            mock_client,
            share="video",
            paths=["Shows/old_ep.mkv"],
        )
        assert "[+]" in result

    @respx.mock
    async def test_restore_to_custom_dest(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_async_task_side_effect())
        result = await restore_from_recycle_bin(
            mock_client,
            share="video",
            paths=["old_ep.mkv"],
            dest_folder="/video/Restored",
        )
        assert "[+]" in result

    @respx.mock
    async def test_restore_full_path(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_async_task_side_effect())
        result = await restore_from_recycle_bin(
            mock_client,
            share="video",
            paths=["/video/#recycle/Shows/ep.mkv"],
        )
        assert "[+]" in result

    @respx.mock
    async def test_restore_error(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 408}}
        )
        result = await restore_from_recycle_bin(
            mock_client,
            share="video",
            paths=["nonexistent.mkv"],
        )
        assert "[!]" in result
