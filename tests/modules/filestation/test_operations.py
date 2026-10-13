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

    @respx.mock
    async def test_multipath_uses_per_path_serial_calls(self, mock_client: DsmClient) -> None:
        """Closes #95: DSM 7.x's `SYNO.FileStation.CreateFolder create` does
        not honor the documented comma-joined multi-path format on v2 — a
        request with `name=a,b` is treated as a single literal name and
        creates one folder named `a,b` while the tool reports
        `Created 1 folder(s)`. Same root-cause family as #68 (delete +
        getinfo) and #84 (move/copy); fix matches that pattern: one DSM
        CreateFolder call per input path. Test asserts (a) N create
        requests for N paths, (b) each request carries a single bare
        `name` (no commas) and a single `folder_path` parent, and
        (c) the aggregated response names every input folder.
        """
        creates: list[dict[str, str]] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if (
                params.get("api") == "SYNO.FileStation.CreateFolder"
                and params.get("method") == "create"
            ):
                creates.append(params)
                folder_path = params["folder_path"]
                name = json.loads(params["name"])  # JSON-quoted (#121)
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "folders": [{"path": f"{folder_path.rstrip('/')}/{name}", "name": name}]
                        },
                    },
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        result = await create_folder(
            mock_client,
            paths=["/video/Show/Season 1", "/video/Show/Season 2"],
        )

        assert len(creates) == 2, (
            f"expected two CreateFolder calls (one per path), got {len(creates)}"
        )
        for params in creates:
            assert "," not in params["name"], (
                f"each request must carry a single bare name (no comma-joined multipath), "
                f"got name={params['name']!r}"
            )
            assert "," not in params["folder_path"], (
                f"each request must carry a single folder_path parent, "
                f"got folder_path={params['folder_path']!r}"
            )
        sent_names = sorted(json.loads(p["name"]) for p in creates)
        assert sent_names == ["Season 1", "Season 2"]
        sent_parents = sorted(p["folder_path"] for p in creates)
        assert sent_parents == ["/video/Show", "/video/Show"]
        assert "Season 1" in result
        assert "Season 2" in result
        assert "Created 2 folder(s)" in result


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

    @respx.mock
    async def test_multipath_uses_per_path_serial_calls(self, mock_client: DsmClient) -> None:
        """Closes #84 (the copy_files half via _copy_move): DSM 7.x's
        `SYNO.FileStation.CopyMove start` doesn't honor the documented
        comma-joined multi-path format on v2 — a request with
        `path=/a,/b` is treated as a single literal path and silently
        no-ops. Production therefore issues ONE DSM CopyMove task per
        input path. Test asserts (a) N start requests for N paths,
        (b) each start request carries a single path (no commas),
        (c) all CopyMove calls pinned to v2, (d) the aggregated
        response names every input file.
        """
        starts: list[dict[str, str]] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("api") == "SYNO.FileStation.CopyMove" and params.get("method") == "start":
                starts.append(params)
                return httpx.Response(
                    200, json={"success": True, "data": {"taskid": f"task-{len(starts)}"}}
                )
            if params.get("method") == "status":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"finished": True, "processed_size": 100}},
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        result = await copy_files(
            mock_client,
            paths=["/video/a.mkv", "/video/b.srt"],
            dest_folder="/video/dest",
        )

        # Two paths → two CopyMove start requests.
        assert len(starts) == 2, (
            f"expected two CopyMove start calls (one per path), got {len(starts)}"
        )
        for params in starts:
            assert params["api"] == "SYNO.FileStation.CopyMove"
            assert params["version"] == "2", f"expected version=2, got {params['version']!r}"
            assert "," not in params["path"], (
                f"each request must carry a single path (no comma-joined multipath), "
                f"got path={params['path']!r}"
            )
        sent_paths = sorted(p["path"] for p in starts)
        assert sent_paths == ["/video/a.mkv", "/video/b.srt"]
        assert "a.mkv" in result
        assert "b.srt" in result
        # Per-path sizes summed: 2 paths × 100 bytes = 200 → "200 B".
        assert "200 B" in result, result

    async def test_copy_empty_paths_returns_not_found(self, mock_client: DsmClient) -> None:
        """Empty paths list short-circuits before any DSM call. Without this
        guard the per-path loop simply does nothing and returns a misleading
        success message claiming '0 item(s)' were copied.
        """
        with pytest.raises(ToolError) as exc_info:
            await copy_files(mock_client, paths=[], dest_folder="/video/dest")
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "not_found"
        assert "No paths provided" in body["error"]["message"]


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

    @respx.mock
    async def test_multipath_uses_per_path_serial_calls(self, mock_client: DsmClient) -> None:
        """Closes #84: DSM 7.x's `SYNO.FileStation.CopyMove start` silently
        no-ops on comma-joined multi-path input — `move_files` returns
        `Moved N item(s) ... Source files have been removed` while no
        files actually move on disk. Production now issues ONE CopyMove
        task per source path so the comma-join is never constructed.
        """
        starts: list[dict[str, str]] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("api") == "SYNO.FileStation.CopyMove" and params.get("method") == "start":
                starts.append(params)
                return httpx.Response(
                    200, json={"success": True, "data": {"taskid": f"task-{len(starts)}"}}
                )
            if params.get("method") == "status":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"finished": True, "processed_size": 0}},
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        result = await move_files(
            mock_client,
            paths=[
                "/video/Bellevue.S01E07.mkv",
                "/video/Bellevue.S01E06.mkv",
            ],
            dest_folder="/video/__movetest__",
        )

        # Two paths → two CopyMove start requests, each with remove_src=true.
        assert len(starts) == 2, (
            f"expected two CopyMove start calls (one per path), got {len(starts)}"
        )
        for params in starts:
            assert params["remove_src"] == "true"
            assert "," not in params["path"], (
                f"each request must carry a single path (no comma-joined multipath), "
                f"got path={params['path']!r}"
            )
        sent_paths = sorted(p["path"] for p in starts)
        assert sent_paths == [
            "/video/Bellevue.S01E06.mkv",
            "/video/Bellevue.S01E07.mkv",
        ]
        assert "Moved" in result
        assert "Source files have been removed" in result

    async def test_move_empty_paths_returns_not_found(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError) as exc_info:
            await move_files(mock_client, paths=[], dest_folder="/video/dest")
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "not_found"


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

    async def test_delete_empty_paths_list_returns_not_found(self, mock_client: DsmClient) -> None:
        """Empty paths list short-circuits to a not_found error before any
        DSM call is attempted. Defensive guard added with the per-path
        serial refactor — without it, the per-path for-loop would simply
        do nothing and return an empty success message.
        """
        with pytest.raises(ToolError) as exc_info:
            await delete_files(mock_client, paths=[])
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "not_found"
        assert "No paths provided" in body["error"]["message"]
        assert body["error"]["param"] == "paths"
        assert body["error"]["value"] == []

    @respx.mock
    async def test_delete_lazily_probes_when_share_missing_from_cache(
        self, mock_client: DsmClient
    ) -> None:
        """Closes #37: an empty recycle_bin_status dict triggers the per-share
        probe via ensure_recycle_status. Pre-#37 the dict was always empty AND
        nothing populated it, so every delete reported recycle-on. Now the
        delete path probes lazily — a 408 on `/share/#recycle` flips messaging
        to the permanent-delete variant for that share.
        """

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            api = params.get("api")
            method = params.get("method")
            # Probe call: SYNO.FileStation.List on `/scratch/#recycle` → 408
            if api == "SYNO.FileStation.List" and method == "list":
                if params.get("folder_path") == "/scratch/#recycle":
                    return httpx.Response(200, json={"success": False, "error": {"code": 408}})
                return httpx.Response(
                    200, json={"success": True, "data": {"files": [], "total": 0}}
                )
            # Otherwise fall through to the standard async-task fixture.
            return _async_task_side_effect()(request)

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        recycle_status: dict[str, bool] = {}
        result = await delete_files(
            mock_client,
            paths=["/scratch/temp.bin"],
            recycle_bin_status=recycle_status,
        )

        assert "Permanently deleted" in result
        assert "NOT enabled" in result
        # Probe result was cached so a subsequent delete in the same share
        # would not re-probe.
        assert recycle_status == {"scratch": False}


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
        assert body["error"]["param"] == "timeout"
        assert body["error"]["value"] == 1.0

    @respx.mock
    async def test_copy_task_completes_with_error(self, mock_client: DsmClient) -> None:
        """Copy task finishes with FileStation code 1100 → filestation_error.

        Asserts the envelope is routed through ``error_from_code`` so callers
        see the specific envelope code (``filestation_error``) and the per-code
        suggestion from FILESTATION_ERROR_CODES instead of the old generic
        ``dsm_error`` fallback.
        """

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
        assert body["error"]["code"] == "filestation_error"
        assert "1100" in body["error"]["message"]
        assert "/video/restricted/file.mkv" in body["error"]["message"]
        # Suggestion now comes from the per-code mapping, not the generic fallback.
        assert "shared folder" in body["error"]["suggestion"].lower()

    @respx.mock
    async def test_copy_task_error_maps_408_to_not_found(self, mock_client: DsmClient) -> None:
        """Copy task error 408 → ``not_found`` envelope (not ``dsm_error``)."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "cm-408"}})
            if method == "status":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "finished": True,
                            "error": {"code": 408},
                            "path": "/video/missing/file.mkv",
                        },
                    },
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        with pytest.raises(ToolError) as exc_info:
            await copy_files(
                mock_client,
                paths=["/video/missing/file.mkv"],
                dest_folder="/video/Archive",
            )
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "not_found"

    @respx.mock
    async def test_copy_task_error_maps_416_to_disk_full_retryable(
        self, mock_client: DsmClient
    ) -> None:
        """Copy task error 416 → ``disk_full`` envelope, retryable=True."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "cm-416"}})
            if method == "status":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "finished": True,
                            "error": {"code": 416},
                            "path": "/video/big/file.mkv",
                        },
                    },
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        with pytest.raises(ToolError) as exc_info:
            await copy_files(
                mock_client,
                paths=["/video/big/file.mkv"],
                dest_folder="/video/Archive",
            )
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "disk_full"
        assert body["error"]["retryable"] is True

    @respx.mock
    async def test_copy_task_error_unknown_code_falls_back(self, mock_client: DsmClient) -> None:
        """Unknown/unmapped error code still yields ``dsm_error`` + generic suggestion."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "cm-999"}})
            if method == "status":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "finished": True,
                            "error": {"code": 9999},
                            "path": "/video/file.mkv",
                        },
                    },
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        with pytest.raises(ToolError) as exc_info:
            await copy_files(
                mock_client,
                paths=["/video/file.mkv"],
                dest_folder="/video/Archive",
            )
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "dsm_error"
        assert "9999" in body["error"]["message"]
        assert "source paths exist" in body["error"]["suggestion"]

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
            methods.append(method)
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "del-1"}})
            if method == "status":
                return httpx.Response(200, json={"success": True, "data": {"finished": False}})
            return httpx.Response(200, json={"success": True, "data": {}})

        methods: list[str] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        with pytest.raises(ToolError) as exc_info:
            await delete_files(
                mock_client,
                paths=["/video/huge_dir"],
                timeout=1.0,
            )
        # An unfinished task must still be stopped (#162 skips stop only when finished).
        assert "stop" in methods
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "timeout"
        assert body["error"]["retryable"] is True
        assert "Delete files" in body["error"]["message"]
        assert body["error"]["param"] == "timeout"
        assert body["error"]["value"] == 1.0

    @respx.mock
    async def test_delete_poll_error_mid_operation(self, mock_client: DsmClient) -> None:
        """DSM fails on status call during delete polling.

        Covers the ``poll_error = e; break`` branch in delete_files,
        mirroring the copymove version above. Previously uncovered in
        the patch.
        """

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            methods.append(method)
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "del-err"}})
            if method == "status":
                return httpx.Response(200, json={"success": False, "error": {"code": 402}})
            return httpx.Response(200, json={"success": True, "data": {}})

        methods: list[str] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        with pytest.raises(ToolError) as exc_info:
            await delete_files(mock_client, paths=["/video/file.mkv"])
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "filestation_error"
        # The poll failed, so the task never reported finished: it must still be stopped.
        assert "stop" in methods

    @respx.mock
    async def test_delete_task_completes_with_error(self, mock_client: DsmClient) -> None:
        """Delete task error 1100 → ``filestation_error`` via error_from_code."""

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
        assert body["error"]["code"] == "filestation_error"
        assert "1100" in body["error"]["message"]

    @respx.mock
    async def test_delete_task_error_maps_105_to_permission_denied(
        self, mock_client: DsmClient
    ) -> None:
        """Delete task error 105 (common) → ``permission_denied`` envelope."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "del-105"}})
            if method == "status":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "finished": True,
                            "error": {"code": 105},
                            "path": "/video/locked/file.mkv",
                        },
                    },
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        with pytest.raises(ToolError) as exc_info:
            await delete_files(mock_client, paths=["/video/locked/file.mkv"])
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "permission_denied"
        assert "105" in body["error"]["message"]


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


class TestDeleteMissingPaths:
    """#162: a missing path must never be reported as deleted.

    Shapes captured on vdsm DSM 7.1 and 7.2.2 (2026-10-13):
    - getinfo on a missing path: {"files": [{"code": 408, "path": ...}]}
    - Delete status for a missing path: {"finished": true, "path": "", "processed_num": 0,
      "processing_path": "", "progress": 1, "total": -1}
    - Delete stop on any finished task: error 401
    """

    MISSING_STATUS = {
        "finished": True,
        "path": "",
        "processed_num": 0,
        "processing_path": "",
        "progress": 1,
        "total": -1,
    }

    @staticmethod
    def _dsm(
        missing: set[str], status: dict[str, object] | None = None
    ) -> tuple[object, list[str]]:
        calls: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            api, method = params.get("api", ""), params.get("method", "")
            calls.append(f"{api}.{method}")
            if method == "getinfo":
                path = params["path"]
                entry = (
                    {"code": 408, "path": path}
                    if path in missing
                    else {"isdir": False, "name": path.rsplit("/", 1)[-1], "path": path}
                )
                return httpx.Response(200, json={"success": True, "data": {"files": [entry]}})
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "t1"}})
            if method == "status":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": status or {"finished": True, "total": 1, "path": "/a/x"},
                    },
                )
            if method == "stop":
                return httpx.Response(200, json={"success": False, "error": {"code": 401}})
            return httpx.Response(200, json={"success": True, "data": {}})

        return side_effect, calls

    @respx.mock
    async def test_missing_path_refused_and_nothing_started(self, mock_client: DsmClient) -> None:
        side_effect, calls = self._dsm(missing={"/a/gone"})
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        with pytest.raises(ToolError) as exc_info:
            await delete_files(mock_client, paths=["/a/gone"], recycle_bin_status={"a": True})
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "not_found"
        assert body["error"]["retryable"] is False
        assert "/a/gone" in body["error"]["message"]
        assert "Nothing was deleted" in body["error"]["message"]
        assert not any(c.endswith(".start") for c in calls), calls

    @respx.mock
    async def test_one_missing_path_refuses_whole_call(self, mock_client: DsmClient) -> None:
        side_effect, calls = self._dsm(missing={"/a/gone"})
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        with pytest.raises(ToolError) as exc_info:
            await delete_files(
                mock_client, paths=["/a/real.txt", "/a/gone"], recycle_bin_status={"a": True}
            )
        message = json.loads(str(exc_info.value))["error"]["message"]
        assert "/a/gone" in message and "/a/real.txt" not in message
        assert not any(c.endswith(".start") for c in calls), calls

    @respx.mock
    async def test_glob_path_gets_a_hint(self, mock_client: DsmClient) -> None:
        side_effect, _ = self._dsm(missing={"/a/downloads/*"})
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        with pytest.raises(ToolError) as exc_info:
            await delete_files(mock_client, paths=["/a/downloads/*"], recycle_bin_status={})
        error = json.loads(str(exc_info.value))["error"]
        assert "search_files" in (error.get("suggestion") or "")
        assert "wildcard" in (error.get("suggestion") or "")

    @respx.mock
    async def test_status_total_minus_one_is_not_found(self, mock_client: DsmClient) -> None:
        # getinfo saw the path, but it was gone by the time the task ran.
        side_effect, _ = self._dsm(missing=set(), status=self.MISSING_STATUS)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        with pytest.raises(ToolError) as exc_info:
            await delete_files(mock_client, paths=["/a/x"], recycle_bin_status={"a": True})
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "not_found"
        assert "/a/x" in body["error"]["message"]

    @respx.mock
    async def test_delete_errors_list_is_an_error_not_success(self, mock_client: DsmClient) -> None:
        """QA R1 (#173): a non-recursive delete of a non-empty folder finishes with
        {"errors": [{"code": 900, "path": ...}], "finished": true} and the folder stays."""
        side_effect, _ = self._dsm(
            missing=set(),
            status={"errors": [{"code": 900, "path": "/a/dir"}], "finished": True},
        )
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        with pytest.raises(ToolError) as exc_info:
            await delete_files(
                mock_client, paths=["/a/dir"], recursive=False, recycle_bin_status={"a": True}
            )
        error = json.loads(str(exc_info.value))["error"]
        assert "/a/dir" in error["message"] and "900" in error["message"]
        assert "recursive=true" in (error.get("suggestion") or "")

    @respx.mock
    async def test_finished_task_is_not_stopped(
        self, mock_client: DsmClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        side_effect, calls = self._dsm(missing=set())
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await delete_files(mock_client, paths=["/a/x"], recycle_bin_status={"a": True})
        assert "Deleted" in result
        assert "SYNO.FileStation.Delete.stop" not in calls, calls
        assert "cleanup failed" not in caplog.text


class TestCopyMoveMissingSource:
    """Same bug class as #162: CopyMove reports a missing source in an `errors` list.

    Status captured on vdsm DSM 7.1 and 7.2.2 (2026-10-13) for copying a missing file:
    {"dest_folder_path": "/writable", "errors": [{"code": 408, "path": ...}],
     "finished": true, "found_dir_num": 0, "found_file_num": 0, "path": "", ...}
    """

    @staticmethod
    def _dsm(missing_src: str) -> object:
        def side_effect(request: httpx.Request) -> httpx.Response:
            method = dict(request.url.params).get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "c1"}})
            if method == "status":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "dest_folder_path": "/writable",
                            "errors": [{"code": 408, "path": missing_src}],
                            "finished": True,
                            "found_dir_num": 0,
                            "found_file_num": 0,
                            "found_file_size": 0,
                            "path": "",
                            "processed_size": 0,
                        },
                    },
                )
            return httpx.Response(200, json={"success": True, "data": {}})

        return side_effect

    @respx.mock
    async def test_copy_missing_source_is_not_found(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm("/writable/gone"))
        with pytest.raises(ToolError) as exc_info:
            await copy_files(mock_client, paths=["/writable/gone"], dest_folder="/writable/dst")
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "not_found"
        assert "/writable/gone" in body["error"]["message"]

    @respx.mock
    async def test_move_missing_source_is_not_found(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm("/writable/gone"))
        with pytest.raises(ToolError) as exc_info:
            await move_files(mock_client, paths=["/writable/gone"], dest_folder="/writable/dst")
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "not_found"
        assert "/writable/gone" in body["error"]["message"]


class TestCopyMoveRestorePreCheck:
    """Tier 1 review: refuse the whole copy/move/restore if any source is missing, before
    anything moves (same policy as delete, #162), and name completed items on a later failure."""

    @staticmethod
    def _dsm(missing: set[str], fail_status_for: str | None = None) -> tuple[object, list[str]]:
        calls: list[str] = []
        started: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            calls.append(method)
            if method == "getinfo":
                path = params["path"]
                entry = (
                    {"code": 408, "path": path}
                    if path in missing
                    else {"isdir": False, "name": path.rsplit("/", 1)[-1], "path": path}
                )
                return httpx.Response(200, json={"success": True, "data": {"files": [entry]}})
            if method == "start":
                started.append(params.get("path", ""))
                return httpx.Response(
                    200, json={"success": True, "data": {"taskid": f"t{len(started)}"}}
                )
            if method == "status":
                current = started[-1]
                if fail_status_for and current == fail_status_for:
                    data = {
                        "finished": True,
                        "errors": [{"code": 408, "path": current}],
                        "path": "",
                    }
                else:
                    data = {"finished": True, "processed_size": 10, "path": current}
                return httpx.Response(200, json={"success": True, "data": data})
            return httpx.Response(200, json={"success": True, "data": {}})

        return side_effect, calls

    @respx.mock
    async def test_copy_refused_when_any_source_missing(self, mock_client: DsmClient) -> None:
        side_effect, calls = self._dsm(missing={"/w/gone"})
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        with pytest.raises(ToolError) as exc_info:
            await copy_files(mock_client, paths=["/w/a", "/w/gone"], dest_folder="/w/dst")
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "not_found"
        assert "/w/gone" in error["message"] and "Nothing was copied" in error["message"]
        assert "start" not in calls, calls

    @respx.mock
    async def test_move_refused_when_any_source_missing(self, mock_client: DsmClient) -> None:
        side_effect, calls = self._dsm(missing={"/w/gone"})
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        with pytest.raises(ToolError) as exc_info:
            await move_files(mock_client, paths=["/w/a", "/w/gone"], dest_folder="/w/dst")
        error = json.loads(str(exc_info.value))["error"]
        assert "Nothing was moved" in error["message"]
        assert "start" not in calls, calls

    @respx.mock
    async def test_restore_refused_when_any_item_missing(self, mock_client: DsmClient) -> None:
        side_effect, calls = self._dsm(missing={"/video/#recycle/gone.mkv"})
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        with pytest.raises(ToolError) as exc_info:
            await restore_from_recycle_bin(
                mock_client, share="video", paths=["Shows/a.mkv", "gone.mkv"]
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "not_found"
        assert "/video/#recycle/gone.mkv" in error["message"]
        assert "Nothing was restored" in error["message"]
        assert "start" not in calls, calls

    @respx.mock
    async def test_multi_destination_restore_names_items_already_restored(
        self, mock_client: DsmClient
    ) -> None:
        # Items from different folders restore one at a time to their own folders; a later
        # failure must name what was already restored.
        side_effect, calls = self._dsm(
            missing=set(), fail_status_for="/video/#recycle/Movies/b.mkv"
        )
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        with pytest.raises(ToolError) as exc_info:
            await restore_from_recycle_bin(
                mock_client, share="video", paths=["Shows/a.mkv", "Movies/b.mkv"]
            )
        message = json.loads(str(exc_info.value))["error"]["message"]
        assert "/video/#recycle/Movies/b.mkv" in message
        assert "Already restored before this failure: /video/#recycle/Shows/a.mkv" in message
        assert calls.count("start") == 2

    @respx.mock
    async def test_later_failure_names_items_already_moved(self, mock_client: DsmClient) -> None:
        # Both sources exist at the check; the second fails while moving (e.g. removed meanwhile).
        side_effect, _ = self._dsm(missing=set(), fail_status_for="/w/b")
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        with pytest.raises(ToolError) as exc_info:
            await move_files(mock_client, paths=["/w/a", "/w/b"], dest_folder="/w/dst")
        message = json.loads(str(exc_info.value))["error"]["message"]
        assert "/w/b" in message
        assert "Already moved before this failure: /w/a" in message


class TestOverlappingPaths:
    """QA R1 (#173): a path listed twice, or a path inside another listed path, ran a second
    task after the first had already removed it; its total -1 then read as "not found" after
    a successful delete. Overlaps are collapsed before anything runs."""

    @staticmethod
    def _dsm(missing_at_status: set[str] | None = None) -> tuple[object, list[str]]:
        started: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "getinfo":
                path = params["path"]
                entry = {"isdir": True, "name": path.rsplit("/", 1)[-1], "path": path}
                return httpx.Response(200, json={"success": True, "data": {"files": [entry]}})
            if method == "start":
                started.append(params.get("path", ""))
                return httpx.Response(200, json={"success": True, "data": {"taskid": "t"}})
            if method == "status":
                cur = started[-1]
                if missing_at_status and cur in missing_at_status:
                    data: dict[str, object] = {"finished": True, "path": "", "total": -1}
                else:
                    data = {"finished": True, "path": cur, "total": 1, "processed_size": 0}
                return httpx.Response(200, json={"success": True, "data": data})
            return httpx.Response(200, json={"success": True, "data": {}})

        return side_effect, started

    @respx.mock
    async def test_delete_drops_child_of_listed_parent(self, mock_client: DsmClient) -> None:
        side_effect, started = self._dsm()
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await delete_files(
            mock_client, paths=["/a/n", "/a/n/child"], recycle_bin_status={"a": True}
        )
        assert started == ["/a/n"]
        assert "Deleted 1 item(s)" in result

    @respx.mock
    async def test_delete_drops_duplicate_after_normalization(self, mock_client: DsmClient) -> None:
        side_effect, started = self._dsm()
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        await delete_files(mock_client, paths=["/a/dup/", "a/dup"], recycle_bin_status={"a": True})
        assert started == ["/a/dup"]

    @respx.mock
    async def test_move_drops_child_of_listed_parent(self, mock_client: DsmClient) -> None:
        side_effect, started = self._dsm()
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        await move_files(mock_client, paths=["/a/n/child", "/a/n"], dest_folder="/b")
        assert started == ["/a/n"]

    @respx.mock
    async def test_copy_keeps_child_of_listed_parent(self, mock_client: DsmClient) -> None:
        """QA R2 (#173): copying /a and /a/b is two copies (dest/a and dest/b); the child is
        still there after the parent's copy, so it must not be dropped."""
        side_effect, started = self._dsm()
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await copy_files(mock_client, paths=["/a/n", "/a/n/child"], dest_folder="/b")
        assert started == ["/a/n", "/a/n/child"]
        assert "Copied 2 item(s)" in result

    @respx.mock
    async def test_copy_drops_duplicate_after_normalization(self, mock_client: DsmClient) -> None:
        side_effect, started = self._dsm()
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        await copy_files(mock_client, paths=["/a/dup/", "a/dup"], dest_folder="/b")
        assert started == ["/a/dup"]

    @respx.mock
    async def test_later_delete_failure_names_paths_already_deleted(
        self, mock_client: DsmClient
    ) -> None:
        side_effect, _ = self._dsm(missing_at_status={"/a/y"})
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        with pytest.raises(ToolError) as exc_info:
            await delete_files(mock_client, paths=["/a/x", "/a/y"], recycle_bin_status={"a": True})
        message = json.loads(str(exc_info.value))["error"]["message"]
        assert "/a/y" in message
        assert "Already deleted before this failure: /a/x" in message


class TestCopyMoveSkippedItems:
    """#171: with overwrite=false DSM skips items that already exist at the destination and
    reports it only in `skipstatus`. Captured on vdsm DSM 7.1 and 7.2.2 (2026-10-13):
    {"status": "none"} when nothing was skipped, {"status": "all"} when anything was (a
    whole file, or some children of a folder), null with overwrite=true. A skipped move
    leaves its source in place."""

    @staticmethod
    def _dsm(skipstatus_by_path: dict[str, object]) -> object:
        started: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "getinfo":
                path = params["path"]
                entry = {"isdir": False, "name": path.rsplit("/", 1)[-1], "path": path}
                return httpx.Response(200, json={"success": True, "data": {"files": [entry]}})
            if method == "start":
                started.append(params.get("path", ""))
                return httpx.Response(200, json={"success": True, "data": {"taskid": "t"}})
            if method == "status":
                data: dict[str, object] = {
                    "finished": True,
                    "path": "",
                    "processed_size": 0,
                    "status": "PROCESSING",
                }
                skip = skipstatus_by_path.get(started[-1])
                if skip is not None:
                    data["skipstatus"] = skip
                return httpx.Response(200, json={"success": True, "data": data})
            return httpx.Response(200, json={"success": True, "data": {}})

        return side_effect

    NONE = {"status": "none"}
    ALL = {"status": "all"}

    @respx.mock
    async def test_copy_without_skips_is_plain_success(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm({"/w/a": self.NONE}))
        result = await copy_files(mock_client, paths=["/w/a"], dest_folder="/w/dst")
        assert result.startswith("[+] Copied 1 item(s)")
        assert "skipped" not in result

    @respx.mock
    async def test_copy_with_skips_says_so(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm({"/w/a": self.ALL}))
        result = await copy_files(mock_client, paths=["/w/a"], dest_folder="/w/dst")
        assert result.startswith("[!]"), result
        assert "/w/a" in result and "already existed" in result and "skipped" in result
        assert "overwrite=true" in result

    @respx.mock
    async def test_move_with_skips_does_not_claim_source_removed(
        self, mock_client: DsmClient
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm({"/w/a": self.ALL}))
        result = await move_files(mock_client, paths=["/w/a"], dest_folder="/w/dst")
        assert result.startswith("[!]"), result
        assert "have been removed" not in result
        assert "still in /w" in result

    @respx.mock
    async def test_mixed_names_only_the_skipped_path(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm({"/w/a": self.NONE, "/v/b": self.ALL})
        )
        result = await move_files(mock_client, paths=["/w/a", "/v/b"], dest_folder="/w/dst")
        assert result.startswith("[!]"), result
        skipped_section = result[result.index("skipped") :]
        assert "/v/b" in skipped_section and "/w/a" not in skipped_section
        assert "Source files have been removed from /w/." in result
        assert "removed from /v/" not in result

    @respx.mock
    async def test_restore_with_skips_says_items_stay_in_recycle_bin(
        self, mock_client: DsmClient
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm({"/video/#recycle/a.mkv": self.ALL})
        )
        result = await restore_from_recycle_bin(mock_client, share="video", paths=["a.mkv"])
        assert result.startswith("[!]"), result
        assert "recycle bin" in result and "skipped" in result

    @respx.mock
    async def test_overwrite_true_has_no_skipstatus(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm({}))
        result = await copy_files(mock_client, paths=["/w/a"], dest_folder="/w/dst", overwrite=True)
        assert result.startswith("[+] Copied 1 item(s)")
