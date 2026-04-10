"""Tests for modules/filestation/operations.py — WRITE tools."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest
import respx
from mcp.server.fastmcp.exceptions import ToolError

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
        with pytest.raises(ToolError) as exc_info:
            await create_folder(mock_client, paths=["/video/bad<name"])
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "invalid_parameter"


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
        with pytest.raises(ToolError) as exc_info:
            await rename(mock_client, path="/video/test", new_name="some/path/name")
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "new_name"

    @respx.mock
    async def test_rename_error(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 419}}
        )
        with pytest.raises(ToolError) as exc_info:
            await rename(mock_client, path="/video/test", new_name="bad<name")
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "invalid_parameter"


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
        with pytest.raises(ToolError) as exc_info:
            await copy_files(
                mock_client,
                paths=["/video/file.mkv"],
                dest_folder="/video/dest",
            )
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "already_exists"


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
        with pytest.raises(ToolError) as exc_info:
            await move_files(
                mock_client,
                paths=["/video/file.mkv"],
                dest_folder="/video/dest",
            )
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "already_exists"


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
        with pytest.raises(ToolError) as exc_info:
            await delete_files(mock_client, paths=["/nonexistent/file"])
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "not_found"

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


class TestBackgroundTaskErrors:
    """Error paths shared across CopyMove and Delete background tasks.

    These exercise the polling-loop branches that previously had no
    coverage: mid-poll errors, timeouts, and task-completion error dicts.
    """

    @respx.mock
    async def test_copy_timeout(self, mock_client: DsmClient) -> None:
        """Copy task that never finishes within timeout → timeout error."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "cm-1"}})
            if method == "status":
                return httpx.Response(200, json={"success": True, "data": {"finished": False}})
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        with pytest.raises(ToolError) as exc_info:
            await copy_files(
                mock_client,
                paths=["/video/huge.mkv"],
                dest_folder="/video/Archive",
                timeout=1.0,
            )
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "timeout"
        assert body["error"]["retryable"] is True
        assert "Copy files" in body["error"]["message"]

    @respx.mock
    async def test_copy_task_completes_with_error(self, mock_client: DsmClient) -> None:
        """Copy task finishes but status has an ``error`` key → dsm_error."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "cm-2"}})
            if method == "status":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "finished": True,
                            "error": {"code": 1100},
                            "path": "/video/restricted/file.mkv",
                        },
                    },
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        with pytest.raises(ToolError) as exc_info:
            await copy_files(
                mock_client,
                paths=["/video/restricted/file.mkv"],
                dest_folder="/video/Archive",
            )
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "dsm_error"
        assert "1100" in body["error"]["message"]
        assert "/video/restricted/file.mkv" in body["error"]["message"]

    @respx.mock
    async def test_copy_poll_error_mid_operation(self, mock_client: DsmClient) -> None:
        """DSM fails mid-poll → error propagates via synology_error_response."""
        state = {"calls": 0}

        def side_effect(request: httpx.Request) -> httpx.Response:
            state["calls"] += 1
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "cm-3"}})
            if method == "status":
                # Fail on the first status call
                return httpx.Response(200, json={"success": False, "error": {"code": 402}})
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        with pytest.raises(ToolError) as exc_info:
            await copy_files(
                mock_client,
                paths=["/video/file.mkv"],
                dest_folder="/video/dest",
            )
        body = json.loads(str(exc_info.value))
        # Code 402 "System too busy" is not specifically typed → filestation_error
        assert body["error"]["code"] == "filestation_error"

    @respx.mock
    async def test_delete_timeout(self, mock_client: DsmClient) -> None:
        """Delete task that never finishes → timeout error."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "del-1"}})
            if method == "status":
                return httpx.Response(200, json={"success": True, "data": {"finished": False}})
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        with pytest.raises(ToolError) as exc_info:
            await delete_files(
                mock_client,
                paths=["/video/huge_dir"],
                timeout=1.0,
            )
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "timeout"
        assert body["error"]["retryable"] is True
        assert "Delete files" in body["error"]["message"]

    @respx.mock
    async def test_delete_task_completes_with_error(self, mock_client: DsmClient) -> None:
        """Delete task finishes with an error dict → dsm_error."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "del-2"}})
            if method == "status":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "finished": True,
                            "error": {"code": 1100},
                            "path": "/video/locked/file.mkv",
                        },
                    },
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        with pytest.raises(ToolError) as exc_info:
            await delete_files(mock_client, paths=["/video/locked/file.mkv"])
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "dsm_error"
        assert "1100" in body["error"]["message"]


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
        with pytest.raises(ToolError) as exc_info:
            await restore_from_recycle_bin(
                mock_client,
                share="video",
                paths=["nonexistent.mkv"],
            )
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "not_found"
