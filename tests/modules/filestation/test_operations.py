"""Tests for modules/filestation/operations.py — WRITE tools."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.filestation.operations import (
    copy_files,
    create_folder,
    delete_files,
    move_files,
    rename,
    restore_from_recycle_bin,
)
from mcp_synology.modules.filestation.tasks import Handle, TaskRecords
from tests.conftest import BASE_URL

if TYPE_CHECKING:
    from collections.abc import Callable

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

    @respx.mock
    async def test_delete_from_recycle_bin_is_permanent(self, mock_client: DsmClient) -> None:
        """Deleting inside /<share>/#recycle empties it for good (#220).

        DSM keeps no second copy, so the share's recycle-bin setting doesn't
        make these recoverable. Seen on seska (DSM 7.1.1): the old wording said
        "files can be recovered" after emptying the bin.
        """
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_async_task_side_effect())
        result = await delete_files(
            mock_client,
            paths=["/Test-Resources/#recycle/a.txt", "/Test-Resources/#recycle/old"],
            recycle_bin_status={"Test-Resources": True},
        )
        assert result.startswith("[+] Permanently deleted 2 item(s):")
        assert "can be recovered" not in result
        assert "recycle bin on /Test-Resources" in result
        assert "cannot be recovered" in result

    @respx.mock
    async def test_delete_the_recycle_folder_itself_is_permanent(
        self, mock_client: DsmClient
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_async_task_side_effect())
        result = await delete_files(
            mock_client, paths=["/video/#recycle"], recycle_bin_status={"video": True}
        )
        assert result.startswith("[+] Permanently deleted 1 item(s):")
        assert "can be recovered" not in result

    @respx.mock
    async def test_delete_mixed_bin_and_regular_paths(self, mock_client: DsmClient) -> None:
        """A call mixing bin and non-bin paths names the bin paths as permanent."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_async_task_side_effect())
        result = await delete_files(
            mock_client,
            paths=["/video/keep-recoverable.mkv", "/video/#recycle/gone.mkv"],
            recycle_bin_status={"video": True},
        )
        assert result.startswith("[+] Deleted 2 item(s):")
        recoverable, _, permanent = result.partition("⚠")
        assert "Recycle bin is enabled on /video" in recoverable
        assert "except the items deleted from the recycle bin itself" in recoverable
        assert "/video/#recycle/gone.mkv" in permanent
        assert "cannot be recovered" in permanent
        assert "/video/keep-recoverable.mkv" not in permanent

    @respx.mock
    async def test_delete_from_a_differently_cased_folder_is_not_the_bin(
        self, mock_client: DsmClient
    ) -> None:
        """Only DSM's exact `#recycle` is the bin, as in restore and listing.

        A user folder named `#Recycle` on a share with a recycle bin goes to the
        bin like any other path, so it stays recoverable.
        """
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_async_task_side_effect())
        result = await delete_files(
            mock_client, paths=["/video/#Recycle/x.mkv"], recycle_bin_status={"video": True}
        )
        assert result.startswith("[+] Deleted 1 item(s):")
        assert "Recycle bin is enabled on /video" in result
        assert "permanent" not in result

    @respx.mock
    async def test_delete_from_recycle_bin_skips_the_recycle_probe(
        self, mock_client: DsmClient
    ) -> None:
        """A share whose deleted paths are all in its bin needs no recycle probe."""
        recycle_status: dict[str, bool] = {}
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_async_task_side_effect()
        )
        await delete_files(
            mock_client,
            paths=["/scratch/#recycle/x.bin"],
            recycle_bin_status=recycle_status,
        )
        listed = [
            dict(c.request.url.params).get("folder_path")
            for c in route.calls
            if dict(c.request.url.params).get("method") == "list"
            # BackgroundTask list is the running-task check (#245), not a recycle probe.
            and dict(c.request.url.params).get("api") == "SYNO.FileStation.List"
        ]
        assert listed == []
        assert recycle_status == {}

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
        """Copy task that outlives the wait is handed off, not stopped (#245)."""
        methods: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            methods.append(method)
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "cm-1"}})
            if method == "status":
                return httpx.Response(200, json={"success": True, "data": {"finished": False}})
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        result = await copy_files(
            mock_client,
            paths=["/video/huge.mkv"],
            dest_folder="/video/Archive",
            timeout=1.0,
        )
        assert "stop" not in methods
        assert "Still copying on the NAS: /video/huge.mkv" in result
        assert "Handle: `copy:cm-1`" in result

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
        """Delete task that outlives the wait is handed off, not stopped (#245)."""

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

        result = await delete_files(mock_client, paths=["/video/huge_dir"], timeout=1.0)
        assert "stop" not in methods
        assert "Still deleting on the NAS: /video/huge_dir" in result
        assert "Handle: `delete:del-1`" in result

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
    def _dsm(
        skipstatus_by_path: dict[str, object],
        overwrite_sent: list[str] | None = None,
        *,
        dirs: frozenset[str] = frozenset(),
        no_type: frozenset[str] = frozenset(),
    ) -> object:
        started: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "getinfo":
                path = params["path"]
                entry: dict[str, object] = {"name": path.rsplit("/", 1)[-1], "path": path}
                if path not in no_type:
                    entry["isdir"] = path in dirs
                return httpx.Response(200, json={"success": True, "data": {"files": [entry]}})
            if method == "start":
                started.append(params.get("path", ""))
                if overwrite_sent is not None:
                    overwrite_sent.append(params.get("overwrite", ""))
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
    async def test_copy_with_a_skipped_file_says_skipped(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm({"/w/a": self.ALL}))
        result = await copy_files(mock_client, paths=["/w/a"], dest_folder="/w/dst")
        # #189: a skipped file was skipped, full stop; folder wording would only confuse.
        assert result.startswith(
            "[!] Copied 0 of 1 item(s) to /w/dst/; 1 skipped (already at the destination, "
            "overwrite=false):"
        ), result
        assert "Skipped:\n  /w/a" in result
        assert "in whole or in part" not in result
        assert "folder" not in result
        assert "Copied:" not in result  # nothing to list as fully copied
        assert "overwrite=true" in result

    @respx.mock
    async def test_copy_with_a_skipped_folder_says_items_skipped(
        self, mock_client: DsmClient
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm({"/w/d": self.ALL}, dirs=frozenset({"/w/d"}))
        )
        result = await copy_files(mock_client, paths=["/w/d"], dest_folder="/w/dst")
        # QA R1 (#175): "0 of 1" alone would claim nothing was copied, but DSM still copies
        # a skipped folder's children that weren't at the destination.
        assert result.startswith(
            "[!] Copied 0 of 1 item(s) in full to /w/dst/; 1 folder with items skipped "
            "(already at the destination, overwrite=false):"
        ), result
        # Not "the rest were copied": DSM doesn't say whether anything was left to copy.
        assert (
            "Folders with items skipped (anything not already at the destination was "
            "copied):\n  /w/d"
        ) in result
        assert "the rest" not in result
        assert "Skipped:" not in result
        assert "in whole or in part" not in result
        assert "overwrite=true" in result

    @respx.mock
    async def test_copy_skip_of_unknown_type_keeps_hedged_wording(
        self, mock_client: DsmClient
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm({"/w/a": self.ALL}, no_type=frozenset({"/w/a"}))
        )
        result = await copy_files(mock_client, paths=["/w/a"], dest_folder="/w/dst")
        assert result.startswith("[!] Copied 0 of 1 item(s) in full to /w/dst/"), result
        assert "Skipped in whole or in part:\n  /w/a" in result
        assert (
            "For a skipped folder, the parts not already at the destination were still copied"
            in result
        )

    @respx.mock
    async def test_copy_skip_type_lookup_failure_keeps_hedged_wording(
        self, mock_client: DsmClient
    ) -> None:
        inner = self._dsm({"/w/a": self.ALL})
        getinfo_calls: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("method") == "getinfo":
                getinfo_calls.append(request.url.params["path"])
                if len(getinfo_calls) > 1:  # the first is the up-front existence check
                    return httpx.Response(200, json={"success": False, "error": {"code": 401}})
            return inner(request)  # type: ignore[operator, no-any-return]

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await copy_files(mock_client, paths=["/w/a"], dest_folder="/w/dst")
        # The copy already happened: a failed type lookup must not turn it into an error.
        assert result.startswith("[!] Copied 0 of 1 item(s) in full to /w/dst/"), result
        assert "Skipped in whole or in part:\n  /w/a" in result

    @respx.mock
    async def test_copy_mixed_file_and_folder_skips(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm(
                {
                    "/w/a": self.ALL,
                    "/w/b": self.ALL,
                    "/w/d": self.ALL,
                    "/w/e": self.ALL,
                    "/w/c": self.NONE,
                },
                dirs=frozenset({"/w/d", "/w/e"}),
            )
        )
        result = await copy_files(
            mock_client, paths=["/w/a", "/w/d", "/w/b", "/w/e", "/w/c"], dest_folder="/x"
        )
        assert result.startswith(
            "[!] Copied 1 of 5 item(s) in full to /x/; 2 skipped, 2 folders with items skipped "
            "(already at the destination, overwrite=false):"
        ), result
        assert "Copied:\n  c\n" in result
        assert "Skipped:\n  /w/a\n  /w/b\n" in result
        assert "was copied):\n  /w/d\n  /w/e\n" in result

    @respx.mock
    async def test_move_with_skips_does_not_claim_source_removed(
        self, mock_client: DsmClient
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm({"/w/a": self.ALL}))
        result = await move_files(mock_client, paths=["/w/a"], dest_folder="/w/dst")
        assert result.startswith("[!]"), result
        assert "have been removed" not in result
        assert "still in /w" in result
        assert "Skipped:\n  /w/a (skipped items are still in /w/)" in result

    @respx.mock
    async def test_mixed_names_only_the_skipped_path(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm({"/w/a": self.NONE, "/v/b": self.ALL})
        )
        result = await move_files(mock_client, paths=["/w/a", "/v/b"], dest_folder="/w/dst")
        assert result.startswith("[!] Moved 1 of 2 item(s) to /w/dst/; 1 skipped"), result
        moved = result[result.index("Moved:\n") : result.index("Skipped:")]
        skipped = result[result.index("Skipped:") :]
        assert "  a" in moved and "b" not in moved
        assert "/v/b" in skipped and "/w/a" not in skipped
        assert "Moved sources removed from /w/: a" in result
        assert "removed from /v/" not in result

    @respx.mock
    async def test_restore_with_skips_says_items_stay_in_recycle_bin(
        self, mock_client: DsmClient
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm({"/video/#recycle/a.mkv": self.ALL})
        )
        result = await restore_from_recycle_bin(mock_client, share="video", paths=["a.mkv"])
        assert result.startswith(
            "[!] Restored from /video recycle bin: 0 of 1 item(s) to /video/; 1 skipped"
        ), result
        assert "Skipped:\n  /video/#recycle/a.mkv (skipped items are still in" in result
        assert "moved" not in result.lower()

    @respx.mock
    async def test_restore_of_a_skipped_folder_says_items_skipped(
        self, mock_client: DsmClient
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm(
                {"/video/#recycle/S": self.ALL}, dirs=frozenset({"/video/#recycle/S"})
            )
        )
        result = await restore_from_recycle_bin(mock_client, share="video", paths=["S"])
        assert result.startswith(
            "[!] Restored from /video recycle bin: 0 of 1 item(s) in full to /video/; "
            "1 folder with items skipped"
        ), result
        assert (
            "Folders with items skipped (anything not already at the destination was restored):"
        ) in result
        # The skipped items are the folder's own children, so point inside it (QA R1 on #190).
        assert "  /video/#recycle/S (skipped items are still in /video/#recycle/S/)" in result
        assert "moved" not in result.lower()

    @respx.mock
    async def test_overwrite_true_has_no_skipstatus(self, mock_client: DsmClient) -> None:
        sent: list[str] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm({}, sent))
        result = await copy_files(mock_client, paths=["/w/a"], dest_folder="/w/dst", overwrite=True)
        assert sent == ["true"]
        assert result.startswith("[+] Copied 1 item(s)")

    @respx.mock
    async def test_string_skipstatus_counts_as_skipped(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm({"/w/a": "all"}))
        result = await copy_files(mock_client, paths=["/w/a"], dest_folder="/w/dst")
        assert result.startswith("[!]"), result

    @respx.mock
    async def test_multi_destination_restore_with_a_skip_leads_with_warning(
        self, mock_client: DsmClient
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm(
                {"/video/#recycle/S1/a.mkv": self.NONE, "/video/#recycle/S2/b.mkv": self.ALL}
            )
        )
        result = await restore_from_recycle_bin(
            mock_client, share="video", paths=["S1/a.mkv", "S2/b.mkv"]
        )
        # The summary stays neutral and the per-destination result says what was skipped,
        # so the two never disagree (QA R1 on #190).
        assert result.startswith(
            "[!] Restored 1 of 2 item(s) from /video recycle bin; 1 with skips "
            "(already at the destination, overwrite=false; details below):"
        ), result
        assert "Skipped:\n  /video/#recycle/S2/b.mkv" in result
        assert "in whole or in part" not in result
        assert "[+] Moved" not in result and "[!] Moved" not in result


class TestUnansweredStatus:
    """#237: the wait's deadline is wall-clock time. (A network error on a status poll is
    a stall handoff since #245, 2026-10-17: see TestStallHandoff.)"""

    @staticmethod
    def _dsm(
        task_prefix: str, status_effect: Exception | None, methods: list[str]
    ) -> Callable[[httpx.Request], httpx.Response]:
        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            methods.append(method)
            if method == "start":
                n = methods.count("start")
                return httpx.Response(
                    200, json={"success": True, "data": {"taskid": f"{task_prefix}-{n}"}}
                )
            if method == "status":
                if status_effect is not None:
                    raise status_effect
                return httpx.Response(200, json={"success": True, "data": {"finished": False}})
            return httpx.Response(200, json={"success": True, "data": {}})

        return side_effect

    @respx.mock
    async def test_deadline_counts_slow_status_requests(
        self, mock_client: DsmClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each status request "takes" 10 s on a fake clock. With a 30 s timeout the old
        sleep-counting loop polled 60 times; the wall-clock deadline stops after 3."""
        import mcp_synology.modules.filestation.tasks as ops

        now = [0.0]
        monkeypatch.setattr(ops.time, "monotonic", lambda: now[0])
        monkeypatch.setattr(ops.asyncio, "sleep", AsyncMock())
        methods: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            method = dict(request.url.params).get("method", "")
            methods.append(method)
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "slow"}})
            if method == "status":
                now[0] += 10.0
                return httpx.Response(200, json={"success": True, "data": {"finished": False}})
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await delete_files(mock_client, paths=["/video/slow"], timeout=30.0)
        assert methods.count("status") == 3
        # Handed off at the deadline, not stopped (#245).
        assert "stop" not in methods
        assert "Handle: `delete:slow`" in result

    @respx.mock
    async def test_handed_off_copy_names_paths_already_done(
        self, mock_client: DsmClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mcp_synology.modules.filestation.tasks as ops

        now = [0.0]
        monkeypatch.setattr(ops.time, "monotonic", lambda: now[0])
        monkeypatch.setattr(ops.asyncio, "sleep", AsyncMock())
        methods: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            method = dict(request.url.params).get("method", "")
            methods.append(method)
            if method == "start":
                n = methods.count("start")
                return httpx.Response(200, json={"success": True, "data": {"taskid": f"c-{n}"}})
            if method == "status":
                if methods.count("start") == 1:
                    return httpx.Response(
                        200, json={"success": True, "data": {"finished": True, "processed_size": 1}}
                    )
                now[0] += 10.0
                return httpx.Response(200, json={"success": True, "data": {"finished": False}})
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await copy_files(
            mock_client, paths=["/video/a", "/video/b"], dest_folder="/x", timeout=30.0
        )
        # Handed off at the deadline, not stopped (#245).
        assert "stop" not in methods
        assert methods.count("status") == 4
        assert "Still copying on the NAS: /video/b" in result
        assert "Already copied: /video/a." in result


class TestStallHandoff:
    """#245, maintainer decision 2026-10-17: when DSM stops answering status requests
    during the wait (a transport error, not a DSM error code), the task is not stopped:
    it is handed off with its handle, and the reply says the NAS stopped answering."""

    @staticmethod
    def _dsm(
        calls: list[dict[str, str]],
        status_effect: Exception,
        *,
        finish: set[str] | None = None,
        answer_first: bool = False,
    ) -> Callable[[httpx.Request], httpx.Response]:
        finish = finish or set()

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            calls.append(params)
            if method == "start":
                name = params["path"].rsplit("/", 1)[-1]
                return httpx.Response(
                    200, json={"success": True, "data": {"taskid": f"FileStation_{name.upper()}"}}
                )
            if method == "status":
                tid = params["taskid"].strip('"')
                if tid.removeprefix("FileStation_").lower() in finish:
                    return httpx.Response(200, json={"success": True, "data": {"finished": True}})
                answered = [c for c in calls if c.get("method") == "status"]
                if answer_first and len(answered) == 1:
                    data = {"finished": False, "processed_num": 8, "total": 342, "progress": 0.02}
                    return httpx.Response(200, json={"success": True, "data": data})
                raise status_effect
            return httpx.Response(200, json={"success": True, "data": {}})

        return side_effect

    @staticmethod
    def _stops(calls: list[dict[str, str]]) -> list[dict[str, str]]:
        return [c for c in calls if c.get("method") == "stop"]

    @respx.mock
    async def test_delete_read_timeout_hands_off(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm(calls, httpx.ReadTimeout(""), answer_first=True)
        )
        records = TaskRecords()
        result = await delete_files(
            mock_client,
            paths=["/Backups/huge.sparsebundle"],
            recycle_bin_status={"Backups": True},
            records=records,
        )
        assert not self._stops(calls)
        assert result.startswith(
            "The NAS stopped answering while deleting /Backups/huge.sparsebundle "
            "(ReadTimeout; last progress: 8 of 342 items, 2%). It's probably still running "
            "there; don't start it again.\n"
            "Handle: `delete:FileStation_HUGE.SPARSEBUNDLE`\n"
        )
        assert "call file_task with this handle" in result
        record = records.get(Handle("delete", "FileStation_HUGE.SPARSEBUNDLE"))
        assert record is not None
        assert record.paths == ["/Backups/huge.sparsebundle"]
        assert record.started is not None  # our start request, for "running for N min"

    @respx.mock
    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("[Errno 111] Connection refused"),
            httpx.RemoteProtocolError("Server disconnected"),
            httpx.ReadError("reset"),
            httpx.ConnectTimeout(""),
            httpx.PoolTimeout(""),
        ],
    )
    async def test_any_transport_error_hands_off(
        self, mock_client: DsmClient, exc: Exception
    ) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm(calls, exc))
        records = TaskRecords()
        result = await delete_files(
            mock_client, paths=["/s/big"], recycle_bin_status={"s": True}, records=records
        )
        assert not self._stops(calls)
        # No answer at all: no progress to report.
        assert result.startswith(
            f"The NAS stopped answering while deleting /s/big ({type(exc).__name__}"
        )
        assert "last progress" not in result
        assert "Handle: `delete:FileStation_BIG`" in result
        assert records.get(Handle("delete", "FileStation_BIG")) is not None

    @respx.mock
    async def test_dsm_error_code_still_fails_and_stops(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            calls.append(params)
            if params.get("method") == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "T1"}})
            if params.get("method") == "status":
                return httpx.Response(200, json={"success": False, "error": {"code": 599}})
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        with pytest.raises(ToolError):
            await delete_files(mock_client, paths=["/s/big"], recycle_bin_status={"s": True})
        assert len(self._stops(calls)) == 1

    @respx.mock
    async def test_multi_path_lists_not_started(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm(calls, httpx.ReadTimeout(""), finish={"a"})
        )
        result = await delete_files(
            mock_client, paths=["/s/a", "/s/b", "/s/c"], recycle_bin_status={"s": True}
        )
        assert not self._stops(calls)
        assert result.startswith("The NAS stopped answering while deleting /s/b (ReadTimeout)")
        assert "Already deleted: /s/a." in result
        assert "Not started: /s/c. Run them again once this one finishes." in result
        assert [c["path"] for c in calls if c.get("method") == "start"] == ["/s/a", "/s/b"]

    @respx.mock
    @pytest.mark.parametrize(
        ("tool", "kind", "verb"), [(copy_files, "copy", "copying"), (move_files, "move", "moving")]
    )
    async def test_copy_move_hand_off(
        self, mock_client: DsmClient, tool: Callable[..., object], kind: str, verb: str
    ) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm(calls, httpx.ReadTimeout(""))
        )
        records = TaskRecords()
        result = await tool(  # type: ignore[misc]
            mock_client, paths=["/s/big"], dest_folder="/s/dst", records=records
        )
        assert not self._stops(calls)
        assert result.startswith(f"The NAS stopped answering while {verb} /s/big (ReadTimeout)")
        assert f"Handle: `{kind}:FileStation_BIG`" in result
        record = records.get(Handle(kind, "FileStation_BIG"))  # type: ignore[arg-type]
        assert record is not None
        assert record.dest == "/s/dst"
        assert record.started is not None

    @respx.mock
    async def test_restore_hands_off(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm(calls, httpx.ReadTimeout(""))
        )
        records = TaskRecords()
        result = await restore_from_recycle_bin(
            mock_client, share="video", paths=["big"], records=records
        )
        assert not self._stops(calls)
        assert result.startswith(
            "The NAS stopped answering while restoring /video/#recycle/big (ReadTimeout)"
        )
        assert "Handle: `restore:FileStation_BIG`" in result
        assert records.get(Handle("restore", "FileStation_BIG")) is not None

    @respx.mock
    async def test_unreadable_html_status_reply_hands_off(self, mock_client: DsmClient) -> None:
        """QA R1 on #251: a 200 text/html status reply (a captive portal or a reverse
        proxy's login page) can't be parsed. The task is handed off, never lost."""
        calls: list[dict[str, str]] = []
        dsm = self._dsm(calls, httpx.ReadTimeout(""))

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("method") == "status":
                calls.append(params)
                return httpx.Response(
                    200,
                    text="<html><body>Proxy login secret-token-123</body></html>",
                    headers={"content-type": "text/html"},
                )
            return dsm(request)

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        records = TaskRecords()
        result = await delete_files(
            mock_client, paths=["/s/big"], recycle_bin_status={"s": True}, records=records
        )
        assert not self._stops(calls)
        assert result.startswith(
            "The NAS's status reply couldn't be read while deleting /s/big (JSONDecodeError). "
            "It's probably still running there; don't start it again.\n"
            "Handle: `delete:FileStation_BIG`\n"
        )
        assert "secret-token" not in result
        assert "http" not in result.lower()
        assert records.get(Handle("delete", "FileStation_BIG")) is not None

    @respx.mock
    async def test_unexpected_exception_on_status_hands_off_copy(
        self, mock_client: DsmClient
    ) -> None:
        """A status reply that is JSON but not an object (here a list) breaks parsing in
        the client with an AttributeError: any unexpected exception hands off too."""
        calls: list[dict[str, str]] = []
        dsm = self._dsm(calls, httpx.ReadTimeout(""))

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("method") == "status":
                calls.append(params)
                return httpx.Response(200, json=["detail that must not leak"])
            return dsm(request)

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        records = TaskRecords()
        result = await copy_files(
            mock_client, paths=["/s/big"], dest_folder="/s/dst", records=records
        )
        assert not self._stops(calls)
        assert result.startswith(
            "The NAS's status reply couldn't be read while copying /s/big (AttributeError)."
        )
        assert "must not leak" not in result
        assert "Handle: `copy:FileStation_BIG`" in result
        assert records.get(Handle("copy", "FileStation_BIG")) is not None

    @respx.mock
    async def test_start_transport_error_unchanged(self, mock_client: DsmClient) -> None:
        """No task ID yet, so no handle: the start's network error surfaces as before."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            if dict(request.url.params).get("method") == "start":
                raise httpx.ReadTimeout("")
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        with pytest.raises(httpx.ReadTimeout):
            await delete_files(mock_client, paths=["/s/big"], recycle_bin_status={"s": True})


class TestDeleteHandoff:
    """#245: a delete that outlives the wait is handed off with a handle."""

    @staticmethod
    def _dsm(
        calls: list[dict[str, str]],
        *,
        finish: set[str] | None = None,
        status: dict[str, object] | None = None,
    ) -> Callable[[httpx.Request], httpx.Response]:
        finish = finish or set()

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            calls.append(params)
            if method == "start":
                name = params["path"].rsplit("/", 1)[-1]
                return httpx.Response(
                    200, json={"success": True, "data": {"taskid": f"FileStation_{name.upper()}"}}
                )
            if method == "status":
                tid = params["taskid"].strip('"')
                if tid.removeprefix("FileStation_").lower() in finish:
                    return httpx.Response(200, json={"success": True, "data": {"finished": True}})
                data = status or {
                    "finished": False,
                    "processed_num": 8,
                    "total": 342,
                    "progress": 0.02,
                }
                return httpx.Response(200, json={"success": True, "data": data})
            return httpx.Response(200, json={"success": True, "data": {}})

        return side_effect

    @respx.mock
    async def test_delete_small_unchanged(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_async_task_side_effect())
        result = await delete_files(
            mock_client, paths=["/video/Downloads/old.mkv"], recycle_bin_status={"video": True}
        )
        assert result == (
            "[+] Deleted 1 item(s):\n  /video/Downloads/old.mkv\n\n"
            "Recycle bin is enabled on /video \u2014 files can be recovered with "
            "list_recycle_bin and restore_from_recycle_bin."
        )

    @respx.mock
    async def test_delete_hands_off(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm(calls))
        records = TaskRecords()
        result = await delete_files(
            mock_client,
            paths=["/share/big"],
            timeout=1,
            recycle_bin_status={"share": True},
            records=records,
        )
        assert "Still deleting on the NAS: /share/big" in result
        assert "Handle: `delete:FileStation_BIG`" in result
        assert "8 of 342 items, 2%" in result
        assert not [c for c in calls if c.get("method") == "stop"]
        record = records.get(Handle("delete", "FileStation_BIG"))
        assert record is not None
        assert record.paths == ["/share/big"]
        assert record.recycle == {"share": True}

    @respx.mock
    async def test_delete_multi_path_handoff_not_started(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm(calls, finish={"a"}))
        result = await delete_files(
            mock_client,
            paths=["/s/a", "/s/b", "/s/c"],
            timeout=1,
            recycle_bin_status={"s": True},
        )
        assert "Still deleting on the NAS: /s/b" in result
        assert "Not started: /s/c" in result
        assert "/s/a" in result.split("Not started")[0].split("Handle")[1]
        started = [c["path"] for c in calls if c.get("method") == "start"]
        assert started == ["/s/a", "/s/b"]

    @respx.mock
    async def test_delete_cancel_stops(self, mock_client: DsmClient) -> None:
        import asyncio

        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm(calls))
        task = asyncio.create_task(
            delete_files(mock_client, paths=["/s/a"], timeout=30, recycle_bin_status={"s": True})
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len([c for c in calls if c.get("method") == "stop"]) == 1

    @respx.mock
    async def test_delete_start_sends_accurate_progress(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm(calls, finish={"a"}))
        await delete_files(mock_client, paths=["/s/a"], recycle_bin_status={"s": True})
        start = next(c for c in calls if c.get("method") == "start")
        assert start["accurate_progress"] == "true"

    @respx.mock
    async def test_delete_progress_callback_called(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm(calls))
        seen: list[tuple[float, float | None, str]] = []

        async def on_progress(progress: float, total: float | None, message: str) -> None:
            seen.append((progress, total, message))

        await delete_files(
            mock_client,
            paths=["/s/a"],
            timeout=1,
            recycle_bin_status={"s": True},
            on_progress=on_progress,
        )
        assert seen
        assert seen[0][:2] == (8, 342)

    @respx.mock
    async def test_delete_handoff_keeps_handle_when_recycle_probe_would_stall(
        self, mock_client: DsmClient
    ) -> None:
        """No recycle-bin probe at handoff: a stalled DSM must not lose the handle."""
        calls: list[dict[str, str]] = []
        dsm = self._dsm(calls)

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("method") == "list" and "#recycle" in params.get("folder_path", ""):
                raise httpx.ReadTimeout("DSM busy deleting")
            return dsm(request)

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        records = TaskRecords()
        result = await delete_files(
            mock_client,
            paths=["/share/big"],
            timeout=1,
            recycle_bin_status={},
            records=records,
        )
        assert "Handle: `delete:FileStation_BIG`" in result
        assert "Still deleting on the NAS: /share/big" in result
        record = records.get(Handle("delete", "FileStation_BIG"))
        assert record is not None
        assert record.recycle == {}


class TestProgressAcrossPaths:
    """#245 re-review: a multi-path call shares one MCP progress token, so the values sent
    must keep increasing from one path's task to the next."""

    @respx.mock
    async def test_delete_two_paths_progress_never_decreases(
        self, mock_client: DsmClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mcp_synology.modules.filestation.tasks as tasks

        monkeypatch.setattr(tasks.asyncio, "sleep", AsyncMock())
        statuses = {
            "FileStation_A": iter(
                [
                    {"finished": False, "processed_num": 50, "total": 100},
                    {"finished": True},
                ]
            ),
            "FileStation_B": iter(
                [
                    {"finished": False, "processed_num": 3, "total": 10},
                    {"finished": False, "processed_num": 7, "total": 10},
                    {"finished": True},
                ]
            ),
        }
        now = [0.0]
        monkeypatch.setattr(tasks.time, "monotonic", lambda: now[0])

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            if method == "start":
                name = params["path"].rsplit("/", 1)[-1].upper()
                return httpx.Response(
                    200, json={"success": True, "data": {"taskid": f"FileStation_{name}"}}
                )
            if method == "status":
                now[0] += 5.0
                data = next(statuses[params["taskid"].strip('"')])
                return httpx.Response(200, json={"success": True, "data": data})
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        sent: list[float] = []
        totals: list[float | None] = []

        async def on_progress(progress: float, total: float | None, message: str) -> None:
            sent.append(progress)
            totals.append(total)

        await delete_files(
            mock_client,
            paths=["/s/a", "/s/b"],
            recycle_bin_status={"s": True},
            on_progress=on_progress,
        )
        assert len(sent) == 3
        assert sent[0] == 50
        assert all(b > a for a, b in zip(sent, sent[1:], strict=False))
        # A raised value is sent without a total, so no client shows 50.001 of 10.
        assert totals == [100, None, None]


class TestDeleteSummaryRecycleProbe:
    """#245 re-review: the lazy recycle-bin probe in the final summary must not turn a
    finished delete into an error when DSM doesn't answer it."""

    @respx.mock
    async def test_unanswered_probe_still_returns_summary(self, mock_client: DsmClient) -> None:
        from mcp_synology.modules.filestation.operations import _delete_summary

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=httpx.ReadTimeout(""))
        recycle: dict[str, bool] = {}
        text = await _delete_summary(mock_client, ["/share/big"], recycle)
        assert text.startswith("[+] Deleted 1 item(s):\n  /share/big")
        assert "Permanently" not in text
        assert "Couldn't check whether /share has its recycle bin enabled" in text
        assert recycle == {}  # nothing cached, so a later call probes again

    @respx.mock
    async def test_file_task_final_result_survives_unanswered_probe(
        self, mock_client: DsmClient
    ) -> None:
        from mcp_synology.modules.filestation.tasks import TaskRecord, file_task

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("method") == "status":
                return httpx.Response(200, json={"success": True, "data": {"finished": True}})
            raise httpx.ReadTimeout("")

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        records = TaskRecords()
        h = Handle("delete", "FileStation_BIG")
        records.add(TaskRecord(handle=h, paths=["/share/big"], recursive=True))
        text = await file_task(mock_client, records=records, handle=str(h))
        assert text.startswith("[+] Deleted 1 item(s):\n  /share/big")


class TestCopyMoveHandoff:
    """#245: a copy, move or restore that outlives the wait is handed off with a handle."""

    @staticmethod
    def _dsm(
        calls: list[dict[str, str]],
        *,
        finish: set[str] | None = None,
    ) -> Callable[[httpx.Request], httpx.Response]:
        finish = finish or set()

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            calls.append(params)
            if params.get("api") == "SYNO.FileStation.CopyMove":
                if method == "start":
                    name = params["path"].rsplit("/", 1)[-1].removesuffix(".mkv")
                    return httpx.Response(
                        200,
                        json={"success": True, "data": {"taskid": f"FileStation_{name.upper()}"}},
                    )
                if method == "status":
                    tid = params["taskid"].strip('"')
                    if tid.removeprefix("FileStation_").lower() in finish:
                        return httpx.Response(
                            200, json={"success": True, "data": {"finished": True}}
                        )
                    # CopyMove reports bytes, not items (vdsm 7.1 and 7.2.2).
                    data = {
                        "finished": False,
                        "processed_size": 1 << 30,
                        "total": 4 << 30,
                        "progress": 0.25,
                    }
                    return httpx.Response(200, json={"success": True, "data": data})
            return httpx.Response(200, json={"success": True, "data": {}})

        return side_effect

    @respx.mock
    @pytest.mark.parametrize(
        ("tool", "kind", "verb"), [(copy_files, "copy", "copying"), (move_files, "move", "moving")]
    )
    async def test_copy_move_hands_off(
        self, mock_client: DsmClient, tool: Callable[..., object], kind: str, verb: str
    ) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm(calls))
        records = TaskRecords()
        result = await tool(  # type: ignore[misc]
            mock_client,
            paths=["/s/big"],
            dest_folder="/s/dst",
            overwrite=True,
            timeout=1,
            records=records,
        )
        assert f"Still {verb} on the NAS: /s/big (1 GiB of 4 GiB, 25%)" in result
        assert f"Handle: `{kind}:FileStation_BIG`" in result
        assert not [c for c in calls if c.get("method") == "stop"]
        record = records.get(Handle(kind, "FileStation_BIG"))  # type: ignore[arg-type]
        assert record is not None
        assert record.paths == ["/s/big"]
        assert record.dest == "/s/dst"
        assert record.overwrite is True

    @respx.mock
    async def test_copy_hands_off(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm(calls, finish={"a"}))
        result = await copy_files(
            mock_client, paths=["/s/a", "/s/b", "/s/c"], dest_folder="/s/dst", timeout=1
        )
        assert "Still copying on the NAS: /s/b" in result
        assert "Handle: `copy:FileStation_B`" in result
        assert "Already copied: /s/a." in result
        assert "Not started: /s/c." in result
        started = [c["path"] for c in calls if c.get("method") == "start"]
        assert started == ["/s/a", "/s/b"]

    @respx.mock
    async def test_move_hands_off(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm(calls))
        result = await move_files(mock_client, paths=["/s/big"], dest_folder="/s/dst", timeout=1)
        assert result.startswith("Still moving on the NAS: /s/big")
        assert "Handle: `move:FileStation_BIG`" in result
        assert not [c for c in calls if c.get("method") == "stop"]

    @respx.mock
    async def test_restore_hands_off(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm(calls))
        records = TaskRecords()
        result = await restore_from_recycle_bin(
            mock_client, share="video", paths=["Shows/big"], timeout=1, records=records
        )
        assert result.startswith("Still restoring on the NAS: /video/#recycle/Shows/big")
        assert "Handle: `restore:FileStation_BIG`" in result
        assert not [c for c in calls if c.get("method") == "stop"]
        record = records.get(Handle("restore", "FileStation_BIG"))
        assert record is not None
        assert record.paths == ["/video/#recycle/Shows/big"]
        assert record.dest == "/video/Shows"
        assert record.overwrite is False

    @respx.mock
    async def test_restore_multi_destination_not_started(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm(calls))
        result = await restore_from_recycle_bin(
            mock_client, share="video", paths=["Shows/a", "Movies/b"], timeout=1
        )
        assert "Still restoring on the NAS: /video/#recycle/Shows/a" in result
        assert "Not started: /video/#recycle/Movies/b." in result
        started = [c["path"] for c in calls if c.get("method") == "start"]
        assert started == ["/video/#recycle/Shows/a"]

    @respx.mock
    async def test_restore_multi_destination_hands_off_second(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm(calls, finish={"a"}))
        result = await restore_from_recycle_bin(
            mock_client, share="video", paths=["Shows/a", "Movies/b", "Docs/c"], timeout=1
        )
        assert result.startswith("[+] Restored from /video recycle bin")
        assert "Still restoring on the NAS: /video/#recycle/Movies/b" in result
        assert "Already restored: /video/#recycle/Shows/a." in result
        assert "Not started: /video/#recycle/Docs/c." in result

    @respx.mock
    async def test_copy_progress_callback_called(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm(calls))
        seen: list[tuple[float, float | None, str]] = []

        async def on_progress(progress: float, total: float | None, message: str) -> None:
            seen.append((progress, total, message))

        await copy_files(
            mock_client, paths=["/s/a"], dest_folder="/s/dst", timeout=1, on_progress=on_progress
        )
        assert seen[0] == (1 << 30, 4 << 30, "Copying /s/a: 1 GiB of 4 GiB")

    @respx.mock
    async def test_copy_cancel_stops(self, mock_client: DsmClient) -> None:
        import asyncio

        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dsm(calls))
        task = asyncio.create_task(
            copy_files(mock_client, paths=["/s/a"], dest_folder="/s/dst", timeout=30)
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len([c for c in calls if c.get("method") == "stop"]) == 1


class TestDuplicateStart:
    """#245: a path that already has a running task gets that task's handle, not a second
    task."""

    @staticmethod
    def _dsm(
        calls: list[dict[str, str]], listing: httpx.Response
    ) -> Callable[[httpx.Request], httpx.Response]:
        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            calls.append(params)
            if params.get("api") == "SYNO.FileStation.BackgroundTask":
                return listing
            method = params.get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "new-1"}})
            if method == "status":
                return httpx.Response(200, json={"success": True, "data": {"finished": True}})
            return httpx.Response(200, json={"success": True, "data": {}})

        return side_effect

    @staticmethod
    def _listing(*tasks: dict[str, object]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"offset": 0, "tasks": list(tasks), "total": len(tasks)},
            },
        )

    _RUNNING_DELETE = {
        "api": "SYNO.FileStation.Delete",
        "crtime": 1792191585,
        "finished": False,
        "method": "start",
        "params": {"accurate_progress": True, "path": ["/s/big"], "recursive": True},
        "processed_num": 124219,
        "progress": 0.62,
        "taskid": "FileStation_179219158016597225",
        "total": 200400,
        "version": 2,
    }

    @respx.mock
    async def test_delete_refuses_duplicate_start(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm(calls, self._listing(self._RUNNING_DELETE))
        )
        records = TaskRecords()
        result = await delete_files(
            mock_client, paths=["/s/big"], recycle_bin_status={"s": True}, records=records
        )
        assert not [c for c in calls if c.get("method") == "start"]
        assert "Handle: `delete:FileStation_179219158016597225`" in result
        assert "Still deleting on the NAS: /s/big (124,219 of 200,400 items, 62%)" in result
        assert "already running" in result
        record = records.get(Handle("delete", "FileStation_179219158016597225"))
        assert record is not None
        assert record.started == 1792191585  # DSM's crtime, not this call's time

    @respx.mock
    async def test_duplicate_check_tolerates_list_failure(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm(
                calls, httpx.Response(200, json={"success": False, "error": {"code": 599}})
            )
        )
        result = await delete_files(mock_client, paths=["/s/big"], recycle_bin_status={"s": True})
        assert [c["path"] for c in calls if c.get("method") == "start"] == ["/s/big"]
        assert result.startswith("[+] Deleted 1 item(s)")

    @respx.mock
    async def test_finished_or_other_path_is_not_a_duplicate(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        finished = {**self._RUNNING_DELETE, "finished": True}
        other = {**self._RUNNING_DELETE, "params": {"path": ["/s/big/sub"]}, "taskid": "x"}
        copy = {**self._RUNNING_DELETE, "api": "SYNO.FileStation.CopyMove", "taskid": "y"}
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm(calls, self._listing(finished, other, copy))
        )
        await delete_files(mock_client, paths=["/s/big"], recycle_bin_status={"s": True})
        assert [c["path"] for c in calls if c.get("method") == "start"] == ["/s/big"]

    @respx.mock
    async def test_copy_refuses_duplicate_start(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        running = {
            **self._RUNNING_DELETE,
            "api": "SYNO.FileStation.CopyMove",
            "params": {"path": ["/s/big"], "dest_folder_path": "/s/dst", "remove_src": False},
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm(calls, self._listing(running))
        )
        result = await copy_files(mock_client, paths=["/s/big"], dest_folder="/s/dst")
        assert not [c for c in calls if c.get("method") == "start"]
        assert "Still copying on the NAS: /s/big" in result
        assert "Handle: `copy:FileStation_179219158016597225`" in result

    @respx.mock
    async def test_copy_to_another_destination_is_not_a_duplicate(
        self, mock_client: DsmClient
    ) -> None:
        calls: list[dict[str, str]] = []
        running = {
            **self._RUNNING_DELETE,
            "api": "SYNO.FileStation.CopyMove",
            "params": {"path": ["/s/big"], "dest_folder_path": "/s/other", "remove_src": False},
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm(calls, self._listing(running))
        )
        await copy_files(mock_client, paths=["/s/big"], dest_folder="/s/dst")
        assert [c["path"] for c in calls if c.get("method") == "start"] == ["/s/big"]

    @respx.mock
    @pytest.mark.parametrize(("tool", "kind"), [(move_files, "move"), (copy_files, None)])
    async def test_move_of_a_moving_source_to_another_destination_is_a_duplicate(
        self, mock_client: DsmClient, tool: Callable[..., object], kind: str | None
    ) -> None:
        """A source already moving anywhere can't be moved again; a copy to another
        destination is still a different operation (fix round 1)."""
        calls: list[dict[str, str]] = []
        running = {
            **self._RUNNING_DELETE,
            "api": "SYNO.FileStation.CopyMove",
            "params": {
                "path": ["/s/big"],
                "dest_folder_path": "/s/other",
                "overwrite": True,
                "remove_src": True,
            },
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm(calls, self._listing(running))
        )
        records = TaskRecords()
        result = await tool(  # type: ignore[misc]
            mock_client, paths=["/s/big"], dest_folder="/s/dst", records=records
        )
        started = [c for c in calls if c.get("method") == "start"]
        if kind is None:
            # A copy never matches a running move.
            assert [c["path"] for c in started] == ["/s/big"]
            return
        assert not started
        assert result.startswith(
            "A move of /s/big (to /s/other/) was already running on the NAS, so no second one "
            "was started."
        )
        assert "Handle: `move:FileStation_179219158016597225`" in result
        record = records.get(Handle("move", "FileStation_179219158016597225"))
        assert record is not None
        # What the NAS is actually doing, not what this call asked for.
        assert record.dest == "/s/other"
        assert record.overwrite is True

    @respx.mock
    async def test_running_copy_record_falls_back_to_request(self, mock_client: DsmClient) -> None:
        calls: list[dict[str, str]] = []
        running = {
            **self._RUNNING_DELETE,
            "api": "SYNO.FileStation.CopyMove",
            "params": {"path": ["/s/big"], "dest_folder_path": "/s/dst", "remove_src": False},
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._dsm(calls, self._listing(running))
        )
        records = TaskRecords()
        result = await copy_files(
            mock_client, paths=["/s/big"], dest_folder="/s/dst", overwrite=True, records=records
        )
        assert result.startswith("A copy of /s/big was already running")
        record = records.get(Handle("copy", "FileStation_179219158016597225"))
        assert record is not None
        assert (record.dest, record.overwrite) == ("/s/dst", True)
        assert record.started == 1792191585
