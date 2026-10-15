"""DSM parses File Station parameter values as JSON first (#121).

Measured on vdsm DSM 7.1/7.2.2 and a real DS1618+ (DSM 7.2): a free-text value
that is valid JSON is read as that type. Search/DirSize task IDs (a 10-digit
timestamp plus hex) are misread unless quoted whenever a digit follows the
timestamp, so ``list`` answers ``{"finished": true}`` (the unknown-task reply)
and DirSize ``status`` fails with 599; ``stop``/``clean`` then clean nothing.
Names like ``2024``/``true`` get error 400, and a ``list`` pattern of ``2024``
crashes the request (HTTP 502). Synology's API guide JSON-quotes every value.

``FakeDsm`` reproduces that parsing so these tests fail on unquoted code.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.filestation.listing import list_files
from mcp_synology.modules.filestation.metadata import get_dir_size
from mcp_synology.modules.filestation.operations import create_folder, rename
from mcp_synology.modules.filestation.search import search_files
from tests.conftest import BASE_URL

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

# The 11th character is a digit, so DSM misreads it unless it is quoted.
BAD_SHAPE_ID = "17918443788CE649B9"
_LOST = object()


def _dsm_value(raw: str) -> Any:
    """Parse a parameter the way DSM's JSON-format APIs do."""
    if re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", raw):
        return float(raw)  # DSM's parser is looser than JSON: it takes "007" too
    try:
        return json.loads(raw)
    except ValueError:
        # Not valid JSON: DSM still reads a leading number past the timestamp
        # (or a digit-E-digit exponent), so such a task ID matches nothing.
        if re.match(r"\d{11}", raw) or re.match(r"\d+E\d", raw):
            return _LOST
        return raw


def _envelope(exc: ToolError) -> dict[str, Any]:
    return json.loads(str(exc))["error"]  # type: ignore[no-any-return]


class FakeDsm:
    """Minimal Search / DirSize / CreateFolder / Rename / List over respx."""

    def __init__(self, taskid: str = BAD_SHAPE_ID, hits: int = 1) -> None:
        self.taskid = taskid
        self.hits = hits
        self.tasks: set[str] = set()
        self.calls: list[dict[str, str]] = []
        self.reject_quoted_taskid = False
        self.forget_tasks = False  # DSM drops the task right after start
        self.pending_polls = 0  # list replies with finished=false this many times

    def _task(self, params: dict[str, str]) -> Any:
        raw = params.get("taskid", "")
        if self.reject_quoted_taskid and raw.startswith('"'):
            return 600
        value = _dsm_value(raw)
        return value if isinstance(value, str) and value in self.tasks else None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p = dict(request.url.params)
        self.calls.append(p)
        api, method = p.get("api", ""), p.get("method", "")

        def ok(data: Any) -> httpx.Response:
            return httpx.Response(200, json={"success": True, "data": data})

        def err(code: int) -> httpx.Response:
            return httpx.Response(200, json={"success": False, "error": {"code": code}})

        if method == "start" and api in ("SYNO.FileStation.Search", "SYNO.FileStation.DirSize"):
            for key in ("pattern", "extension"):
                if key in p and not isinstance(_dsm_value(p[key]), str):
                    return err(600)
            if not self.forget_tasks:
                self.tasks.add(self.taskid)
            return ok({"taskid": self.taskid})
        if api == "SYNO.FileStation.Search":
            task = self._task(p)
            if task == 600:
                return err(600)
            if method == "list":
                if task is None:
                    return ok({"finished": True})  # DSM's unknown-task reply
                files = [
                    {"name": f"hit{i}", "path": f"/video/hit{i}", "isdir": True, "additional": {}}
                    for i in range(self.hits)
                ]
                if self.pending_polls:
                    self.pending_polls -= 1
                    return ok({"finished": False, "total": 0, "offset": 0, "files": []})
                return ok({"finished": True, "total": self.hits, "offset": 0, "files": files})
            if method in ("stop", "clean") and task is not None and method == "clean":
                self.tasks.discard(task)
            return ok({})
        if api == "SYNO.FileStation.DirSize":
            task = self._task(p)
            if task in (None, 600):
                return err(599 if task is None else 600)
            if method == "status":
                return ok({"finished": True, "total_size": 4399207, "num_file": 3, "num_dir": 2})
            self.tasks.discard(task)
            return ok({})
        if api in ("SYNO.FileStation.CreateFolder", "SYNO.FileStation.Rename"):
            name = _dsm_value(p["name"])
            if not isinstance(name, str):
                return err(400)
            if api == "SYNO.FileStation.CreateFolder":
                folder = _dsm_value(p["folder_path"])
                return ok({"folders": [{"path": f"{folder}/{name}", "name": name}]})
            return ok({"files": [{"path": f"/video/{name}", "name": name}]})
        if api == "SYNO.FileStation.List" and method == "list":
            if "pattern" in p and not isinstance(_dsm_value(p["pattern"]), str):
                return httpx.Response(502)
            files = [{"name": "2024", "path": "/video/2024", "isdir": True, "additional": {}}]
            return ok({"files": files, "total": 1, "offset": 0})
        return ok({})


@pytest.fixture
def fake() -> FakeDsm:
    return FakeDsm()


class TestSearchTaskId:
    @respx.mock
    async def test_bad_shape_taskid_still_finds_results(
        self, mock_client: DsmClient, fake: FakeDsm
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=fake)
        result = await search_files(mock_client, folder_path="/video", pattern="hit")
        assert "hit0" in result
        assert "1 results found" in result

    @respx.mock
    async def test_cleanup_really_removes_the_task(
        self, mock_client: DsmClient, fake: FakeDsm
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=fake)
        await search_files(mock_client, folder_path="/video", pattern="hit")
        assert fake.tasks == set(), "stop/clean must reach the task, or it stays on the NAS"

    @respx.mock
    async def test_genuine_empty_result_needs_one_poll(self, mock_client: DsmClient) -> None:
        fake = FakeDsm(hits=0)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=fake)
        result = await search_files(
            mock_client, folder_path="/video", pattern="none", poll_interval=0.01
        )
        assert "0 results found" in result
        lists = [c for c in fake.calls if c.get("method") == "list"]
        assert len(lists) == 1, "a full, finished, empty reply is a real empty result"

    @respx.mock
    async def test_still_running_search_is_polled_until_finished(
        self, mock_client: DsmClient
    ) -> None:
        fake = FakeDsm()
        fake.pending_polls = 2
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=fake)
        result = await search_files(
            mock_client, folder_path="/video", pattern="hit", poll_interval=0.01
        )
        assert "hit0" in result
        assert len([c for c in fake.calls if c.get("method") == "list"]) == 3

    @respx.mock
    async def test_lost_task_is_an_error_not_zero_results(self, mock_client: DsmClient) -> None:
        """A bare {"finished": true} means DSM doesn't know the task."""
        fake = FakeDsm()
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=fake)
        fake.forget_tasks = True
        with pytest.raises(ToolError) as exc:
            await search_files(mock_client, folder_path="/video", pattern="hit", poll_interval=0.01)
        envelope = _envelope(exc.value)
        assert envelope["retryable"] is True
        assert "0 results" not in envelope["message"]

    @respx.mock
    async def test_falls_back_to_unquoted_taskid_when_quoted_is_rejected(
        self, mock_client: DsmClient
    ) -> None:
        """One DSM 7.2.2 report (esausilva/synology-api-sdk#1) saw error 600 for a
        quoted taskid; then retry once unquoted."""
        fake = FakeDsm(taskid="1791844378C7E86B33")  # unquoted lookup works for this one
        fake.reject_quoted_taskid = True
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=fake)
        result = await search_files(mock_client, folder_path="/video", pattern="hit")
        assert "hit0" in result

    @respx.mock
    async def test_number_like_extension_is_quoted(
        self, mock_client: DsmClient, fake: FakeDsm
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=fake)
        result = await search_files(mock_client, folder_path="/video", extension="3")
        assert "results found" in result
        start = next(c for c in fake.calls if c.get("method") == "start")
        assert start["extension"] == '"3"'


class TestDirSizeTaskId:
    @respx.mock
    async def test_bad_shape_taskid_reports_the_real_size(
        self, mock_client: DsmClient, fake: FakeDsm
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=fake)
        result = await get_dir_size(mock_client, path="/video")
        assert "too fast" not in result
        assert "Files" in result and "3" in result

    @respx.mock
    async def test_unknown_task_is_an_error_not_a_guess(self, mock_client: DsmClient) -> None:
        """599 no longer means "finished too fast": quoted and unquoted both
        miss, so say so instead of reporting sizes as unknown."""
        fake = FakeDsm()
        fake.forget_tasks = True
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=fake)
        with pytest.raises(ToolError) as exc:
            await get_dir_size(mock_client, path="/video")
        assert "599" in _envelope(exc.value)["message"]


class TestFreeTextValues:
    @respx.mock
    @pytest.mark.parametrize("name", ["2024", "007", "1e5", "true", "null", "-5"])
    async def test_create_folder_with_json_like_name(
        self, mock_client: DsmClient, fake: FakeDsm, name: str
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=fake)
        result = await create_folder(mock_client, paths=[f"/video/{name}"])
        assert "Created 1 folder(s)" in result

    @respx.mock
    @pytest.mark.parametrize("name", ["2024", "true", "[1]"])
    async def test_rename_to_json_like_name(
        self, mock_client: DsmClient, fake: FakeDsm, name: str
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=fake)
        result = await rename(mock_client, path="/video/old", new_name=name)
        assert name in result

    @respx.mock
    async def test_list_files_number_like_pattern(
        self, mock_client: DsmClient, fake: FakeDsm
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=fake)
        result = await list_files(mock_client, path="/video", pattern="2024")
        assert "2024" in result
