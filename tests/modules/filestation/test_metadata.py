"""Tests for modules/filestation/metadata.py — get_file_info, get_dir_size."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import error_from_code
from mcp_synology.core.formatting import ESCAPED_NAME_ADVICE
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

    async def test_empty_paths_list_returns_not_found(self, mock_client: DsmClient) -> None:
        """Empty paths list short-circuits to a not_found error before any
        DSM call is attempted. Defensive guard added with the per-path
        serial refactor — without it, the for-loop would simply do nothing
        and the downstream `if not files:` branch would fire with a less
        precise message about missing file information.
        """
        with pytest.raises(ToolError) as exc_info:
            await get_file_info(mock_client, paths=[])
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "not_found"
        assert "No paths provided" in body["error"]["message"]
        assert body["error"]["param"] == "paths"
        assert body["error"]["value"] == []

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


class TestGetFileInfoMissingPaths:
    """#161: getinfo reports a missing path as a per-file `code`, not a request error.

    Shape captured on vdsm DSM 7.1 and 7.2.2 (2026-10-13), for a missing file in a real
    share and for an unknown share alike.
    """

    @staticmethod
    def _found(path: str) -> dict[str, object]:
        return {"isdir": False, "name": path.rsplit("/", 1)[-1], "path": path}

    @staticmethod
    def _missing(path: str) -> dict[str, object]:
        return {"code": 408, "path": path}

    def _route(self, by_path: dict[str, dict[str, object]]) -> None:
        def side_effect(request: httpx.Request) -> httpx.Response:
            path = request.url.params["path"]
            return httpx.Response(200, json={"success": True, "data": {"files": [by_path[path]]}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

    @respx.mock
    async def test_single_missing_path_is_not_found(self, mock_client: DsmClient) -> None:
        self._route({"/writable/nope": self._missing("/writable/nope")})
        with pytest.raises(ToolError) as exc_info:
            await get_file_info(mock_client, paths=["/writable/nope"])
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "not_found"
        assert body["error"]["retryable"] is False
        assert "/writable/nope" in body["error"]["message"]
        assert body["error"]["param"] == "paths"

    @respx.mock
    async def test_all_missing_paths_named_in_error(self, mock_client: DsmClient) -> None:
        self._route({"/a/x": self._missing("/a/x"), "/a/y": self._missing("/a/y")})
        with pytest.raises(ToolError) as exc_info:
            await get_file_info(mock_client, paths=["/a/x", "/a/y"])
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "not_found"
        assert "/a/x" in body["error"]["message"]
        assert "/a/y" in body["error"]["message"]

    @respx.mock
    @pytest.mark.parametrize(
        ("path", "escaped"), [("/a/invoice\\u202efdp.exe", True), ("/a/plain.txt", False)]
    )
    async def test_all_missing_gets_the_shared_suggestion(
        self, mock_client: DsmClient, path: str, escaped: bool
    ) -> None:
        """Pre-QA r3 on #606: DSM's 408 advice starts with "check with list_files", which loops
        for a name copied escaped; every path missing gets the shared suggestion instead."""
        self._route({path: self._missing(path)})
        with pytest.raises(ToolError) as exc_info:
            await get_file_info(mock_client, paths=[path])
        suggestion = json.loads(str(exc_info.value))["error"]["suggestion"]
        if escaped:
            assert suggestion == ESCAPED_NAME_ADVICE, suggestion
        else:
            assert suggestion.startswith("Check the paths with list_files or search_files")
            assert ESCAPED_NAME_ADVICE not in suggestion

    @respx.mock
    @pytest.mark.parametrize(
        ("codes", "mapped_code"), [((407, 407), 407), ((408, 407), 408)], ids=["all-407", "mixed"]
    )
    async def test_not_every_path_408_keeps_the_codes_own_advice(
        self, mock_client: DsmClient, codes: tuple[int, int], mapped_code: int
    ) -> None:
        """QA round 3 on #606: only every path missing (408) gets the shared suggestion. A
        path DSM won't let the user read (407), alone or beside a missing one, keeps its
        code's own advice; beside a missing one, after 408's (pre-QA r3 on #623)."""
        paths = ["/a/x", "/a/y"]
        self._route({p: {"code": c, "path": p} for p, c in zip(paths, codes, strict=True)})
        with pytest.raises(ToolError) as exc_info:
            await get_file_info(mock_client, paths=paths)
        suggestion = json.loads(str(exc_info.value))["error"]["suggestion"]
        err = json.loads(str(exc_info.value))["error"]
        expected = error_from_code(mapped_code, "SYNO.FileStation.List").suggestion
        assert expected
        perm = error_from_code(407, "SYNO.FileStation.List").suggestion
        if mapped_code == 407:
            assert (err["code"], suggestion) == ("permission_denied", expected), err
        else:
            assert err["code"] == "not_found", err
            # No escape in the paths, so no escaped-name advice (QA round 1 on #623).
            assert suggestion == (
                "Use list_files or search_files to find the correct path. "
                f"For one with no permission: {perm}"
            ), suggestion
        assert not suggestion.startswith("Check the paths with list_files or search_files")

    @respx.mock
    @pytest.mark.parametrize(
        ("code", "words"),
        [
            (408, "doesn't exist"),
            (407, "no permission"),
            (105, "no permission"),
            (599, "can't be read"),
        ],
    )
    async def test_every_path_missing_says_why_in_words(
        self, mock_client: DsmClient, code: int, words: str
    ) -> None:
        """#620 (0.11.0rc3 live test): "Get file info failed: /x (DSM error 408)" never said
        the file doesn't exist."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"files": [{"code": code, "path": "/a/gone"}]}}
        )
        with pytest.raises(ToolError) as exc:
            await get_file_info(mock_client, paths=["/a/gone"])  # type: ignore[list-item]
        assert f"Get file info failed: /a/gone: {words} (DSM error {code})." in str(exc.value)

    @respx.mock
    async def test_mixed_shows_found_and_lists_missing(self, mock_client: DsmClient) -> None:
        self._route(
            {"/a/real.txt": self._found("/a/real.txt"), "/a/gone": self._missing("/a/gone")}
        )
        result = await get_file_info(mock_client, paths=["/a/real.txt", "/a/gone"])
        assert "real.txt" in result
        assert "/a/gone: doesn't exist (DSM error 408)" in result  # #620: in words
        assert "Name:  \n" not in result  # no blank card for the missing path

    @respx.mock
    @pytest.mark.parametrize(("gone", "advised"), [("/a/inv\\u202efdp", True), ("/a/gone", False)])
    async def test_mixed_with_an_escaped_missing_path_gives_the_advice(
        self, mock_client: DsmClient, gone: str, advised: bool
    ) -> None:
        """Pre-QA r4 on #606: a found path beside an escaped missing one got only a "Not found"
        note, and listing again shows the same escape; the note carries the advice once."""
        self._route({"/a/real.txt": self._found("/a/real.txt"), gone: self._missing(gone)})
        result = await get_file_info(mock_client, paths=["/a/real.txt", gone])
        assert "real.txt" in result
        assert result.count(ESCAPED_NAME_ADVICE) == (1 if advised else 0), result

    @respx.mock
    async def test_missing_path_never_rendered_as_a_file(self, mock_client: DsmClient) -> None:
        self._route(
            {"/a/real.txt": self._found("/a/real.txt"), "/a/gone": self._missing("/a/gone")}
        )
        result = await get_file_info(mock_client, paths=["/a/real.txt", "/a/gone"])
        rows = [line for line in result.splitlines() if "/a/gone" in line]
        assert all("File" not in line and "Directory" not in line for line in rows), rows


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
        assert "42.6 GiB" in result
        assert "186" in result
        assert "12" in result

    @respx.mock
    async def test_dir_size_counts_have_thousands_separators(self, mock_client: DsmClient) -> None:
        """Counts of 1,000 or more get separators, like every other count (rc8 live test)."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            method = dict(request.url.params).get("method")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "ds-1"}})
            if method == "status":
                data = {"finished": True, "total_size": 1, "num_file": 10341, "num_dir": 1206}
                return httpx.Response(200, json={"success": True, "data": data})
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        result = await get_dir_size(mock_client, path="/photo")
        assert "10,341" in result
        assert "1,206" in result

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

    @staticmethod
    def _getinfo_then_dirsize(
        entry: dict[str, Any] | None,
        calls: list[str],
        status: dict[str, Any] | None = None,
        *,
        transport_error: bool = False,
        proxy: str | None = None,
    ) -> None:
        """getinfo answers with ``entry`` (or a DSM error code, if None, which can't tell;
        ``proxy``: a 502 or a page that isn't DSM's instead); DirSize finishes with ``status``
        (default 0 B, 0 files)."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            method = params.get("method", "")
            calls.append(method)
            if method == "getinfo":
                assert params.get("additional") == '["perm"]'
                if transport_error:
                    raise httpx.ConnectError("connection refused", request=request)
                if proxy == "502":
                    return httpx.Response(502, text="<html>Bad Gateway</html>")
                if proxy == "page":
                    return httpx.Response(200, text="<html>Sign in</html>")
                if entry is None:
                    return httpx.Response(200, json={"success": False, "error": {"code": 400}})
                files = entry.get("_files", [entry])
                return httpx.Response(200, json={"success": True, "data": {"files": files}})
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "ds-1"}})
            if method == "status":
                done = status or {"finished": True, "total_size": 0, "num_file": 0, "num_dir": 0}
                return httpx.Response(200, json={"success": True, "data": done})
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

    @respx.mock
    @pytest.mark.parametrize(
        ("code", "error", "message"),
        [
            (408, "not_found", "Get directory size failed: /video/nope doesn't exist on the NAS."),
            (
                407,
                "permission_denied",
                "Get directory size failed: no permission to read /video/nope on the NAS.",
            ),
        ],
    )
    async def test_dir_size_missing_path_is_refused(
        self, mock_client: DsmClient, code: int, error: str, message: str
    ) -> None:
        """DSM's DirSize on a missing path finishes with 0 B, 0 files (seska, DSM 7.1.1),
        which read as "empty". getinfo is checked first; no task is started."""
        calls: list[str] = []
        self._getinfo_then_dirsize({"code": code, "path": "/video/nope"}, calls)
        with pytest.raises(ToolError) as exc_info:
            await get_dir_size(mock_client, path="video/nope")
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == error
        assert err["message"] == message
        if code == 407:
            assert err["suggestion"] == "Check the DSM user's permissions for that folder."
        assert calls == ["getinfo"]

    @respx.mock
    async def test_dir_size_folder_the_user_cant_read_is_refused(
        self, mock_client: DsmClient
    ) -> None:
        """A folder denied by an ACL or POSIX mode: getinfo reports it with read: false,
        and DirSize would answer 0 B (vdsm 7.1, 7.2.2; #336). No task is started."""
        calls: list[str] = []
        perm = {"acl": {"read": False, "del": True}, "is_acl_mode": True, "posix": 0}
        entry = {"path": "/video/locked", "isdir": True, "additional": {"perm": perm}}
        self._getinfo_then_dirsize(entry, calls)
        with pytest.raises(ToolError) as exc_info:
            await get_dir_size(mock_client, path="/video/locked")
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == "permission_denied"
        assert err["message"] == (
            "Get directory size failed: no permission to read /video/locked on the NAS."
        )
        assert calls == ["getinfo"]

    @respx.mock
    @pytest.mark.parametrize(
        "entry",
        [
            None,
            {"path": "/video", "name": "video", "isdir": True},
            {"path": "/video", "isdir": True, "additional": {"perm": {"acl": {"read": True}}}},
            {"path": "/video", "isdir": True, "additional": {"perm": "garbled"}},
            {"_files": {"a": 1}},
            {"code": "abc", "path": "/video"},
        ],
        ids=["getinfo-failed", "exists", "readable", "garbled-perm", "garbled-files", "odd-code"],
    )
    async def test_dir_size_runs_when_the_path_exists_or_the_check_fails(
        self, mock_client: DsmClient, entry: dict[str, Any] | None
    ) -> None:
        calls: list[str] = []
        self._getinfo_then_dirsize(entry, calls)
        result = await get_dir_size(mock_client, path="/video")
        assert "Total size" in result
        assert calls[:2] == ["getinfo", "start"]

    @respx.mock
    @pytest.mark.parametrize("code", [105, 407])
    async def test_dir_size_refuses_a_path_getinfo_denies(
        self, mock_client: DsmClient, code: int
    ) -> None:
        """QA r1 on #716: a per-path 105 was measured as 0 B while search_files refused it;
        both read `NO_PERMISSION_CODES`."""
        calls: list[str] = []
        self._getinfo_then_dirsize({"code": code, "path": "/video"}, calls)
        with pytest.raises(ToolError) as exc_info:
            await get_dir_size(mock_client, path="/video")
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == "permission_denied", err
        assert calls == ["getinfo"]

    @respx.mock
    @pytest.mark.parametrize("code", [[407], {"a": 1}, "407", True])
    async def test_dir_size_runs_when_the_check_code_is_malformed(
        self, mock_client: DsmClient, code: Any
    ) -> None:
        """QA r3 on #716: get_dir_size reads the code through entry_code; a malformed one
        can't tell, so the task runs (it raised TypeError on a list or dict)."""
        calls: list[str] = []
        self._getinfo_then_dirsize({"code": code, "path": "/video"}, calls)
        result = await get_dir_size(mock_client, path="/video")
        assert "Total size" in result
        assert calls[:2] == ["getinfo", "start"]

    @respx.mock
    async def test_dir_size_runs_when_getinfo_reports_another_code(
        self, mock_client: DsmClient
    ) -> None:
        calls: list[str] = []
        self._getinfo_then_dirsize({"code": 418, "path": "/video"}, calls)
        result = await get_dir_size(mock_client, path="/video")
        assert "Total size" in result
        assert calls[:2] == ["getinfo", "start"]

    @respx.mock
    @pytest.mark.parametrize("proxy", ["502", "page"])
    async def test_dir_size_a_proxys_answer_to_the_check_ends_the_call(
        self, mock_client: DsmClient, proxy: str
    ) -> None:
        """#705: after a proxy's 502 (or a page that isn't DSM's) on the check, DirSize
        started and reported a size; a NAS that doesn't answer ends the call (#704's rule)."""
        calls: list[str] = []
        self._getinfo_then_dirsize(None, calls, proxy=proxy)
        with pytest.raises(ToolError) as exc_info:
            await get_dir_size(mock_client, path="/video")
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == "unavailable", err
        assert err["message"].startswith("Get directory size failed"), err
        assert calls == ["getinfo"]

    @respx.mock
    async def test_dir_size_network_error_on_the_check_ends_the_call(
        self, mock_client: DsmClient
    ) -> None:
        calls: list[str] = []
        self._getinfo_then_dirsize(None, calls, transport_error=True)
        with pytest.raises(httpx.TransportError):
            await get_dir_size(mock_client, path="/video")
        assert calls == ["getinfo"]

    @respx.mock
    @pytest.mark.parametrize(
        ("errors", "error", "message"),
        [
            (
                [{"code": 407, "path": "/homes/admin"}],
                "permission_denied",
                "Get directory size failed: no permission to read /homes/admin on the NAS.",
            ),
            (
                [{"code": 105, "path": "/homes/admin"}],
                "permission_denied",
                "Get directory size failed: no permission to read /homes/admin on the NAS.",
            ),
            (
                [{"code": 401, "path": "/homes"}, {"code": 408, "path": "/homes/admin"}],
                "not_found",
                "Get directory size failed: /homes/admin doesn't exist on the NAS.",
            ),
            ({"code": 401, "path": "/homes/admin/x"}, "filestation_error", None),
            (
                [{"code": 407, "path": "/homes/admin/sub"}],
                "permission_denied",
                "Get directory size failed: no permission to read /homes/admin/sub on the NAS.",
            ),
            (
                [None, {"code": "407"}, {"code": True}],
                "filestation_error",
                "Get directory size failed: DSM reported errors without saying what they were.",
            ),
        ],
        ids=[
            "unreadable",
            "unreadable-105",
            "entry-for-the-path",
            "other-code",
            "subfolder",
            "no-usable-code",
        ],
    )
    async def test_dir_size_status_errors_are_not_an_empty_folder(
        self,
        mock_client: DsmClient,
        errors: object,
        error: str,
        message: str | None,
    ) -> None:
        """A path the user can't read finishes with errors and no totals (vdsm 7.1). When
        the getinfo check fails, that status must not read as 0 B; the task is stopped."""
        calls: list[str] = []
        status = {"finished": True, "errors": errors}
        self._getinfo_then_dirsize(None, calls, status)
        with pytest.raises(ToolError) as exc_info:
            await get_dir_size(mock_client, path="homes/admin")
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == error, err
        if message:
            assert err["message"] == message
        else:
            assert "DSM error 401" in err["message"], err
        assert "0 B" not in str(exc_info.value)
        assert calls == ["getinfo", "start", "status", "stop"]

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


class TestGetFileInfoAdditionalValidation:
    async def test_unknown_additional_is_rejected_before_dsm(self, mock_client: DsmClient) -> None:
        """Same up-front check as list_files/search_files: DSM ignores unknown fields."""
        with pytest.raises(ToolError, match="unknown 'additional'"):
            await get_file_info(mock_client, paths=["/video/a.mkv"], additional=["sizee"])

    @respx.mock
    async def test_size_line_omitted_when_size_not_requested(self, mock_client: DsmClient) -> None:
        """QA on #146: additional=["owner"] gives no size, so don't print "Size: 0 B"."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "files": [
                        {
                            "name": "a.iso",
                            "path": "/video/a.iso",
                            "isdir": False,
                            "additional": {"owner": {"user": "admin", "group": "users"}},
                        }
                    ]
                },
            }
        )
        result = await get_file_info(mock_client, paths=["/video/a.iso"], additional=["owner"])
        assert "Size" not in result
        assert "0 B" not in result
        assert "admin (users)" in result

    @respx.mock
    async def test_table_shows_dash_when_size_not_requested(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "files": [
                        {
                            "name": "a.iso",
                            "path": "/video/a.iso",
                            "isdir": False,
                            "additional": {"owner": {"user": "admin"}},
                        }
                    ]
                },
            }
        )
        result = await get_file_info(
            mock_client, paths=["/video/a.iso", "/video/b.iso"], additional=["owner"]
        )
        assert "0 B" not in result

    @respx.mock
    async def test_type_and_mount_point_type_are_rendered(self, mock_client: DsmClient) -> None:
        """Shapes from vdsm DSM 7.2.2: ``type`` is the extension type ("ISO", "" for a
        folder); ``mount_point_type`` is "" unless the path is a mount point."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "files": [
                        {
                            "name": "a.iso",
                            "path": "/video/a.iso",
                            "isdir": False,
                            "additional": {"type": "ISO", "mount_point_type": "remote"},
                        }
                    ]
                },
            }
        )
        result = await get_file_info(
            mock_client, paths=["/video/a.iso"], additional=["type", "mount_point_type"]
        )
        assert "File type" in result and "ISO" in result
        assert "Mount point" in result and "remote" in result

    @respx.mock
    async def test_empty_type_and_mount_point_type_are_omitted(
        self, mock_client: DsmClient
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "files": [
                        {
                            "name": "dir",
                            "path": "/video/dir",
                            "isdir": True,
                            "additional": {"type": "", "mount_point_type": ""},
                        }
                    ]
                },
            }
        )
        result = await get_file_info(
            mock_client, paths=["/video/dir"], additional=["type", "mount_point_type"]
        )
        assert "File type" not in result
        assert "Mount point" not in result


class TestGetDirSizeUnansweredStatus:
    """#237: a status request DSM doesn't answer is a structured error, not an empty one."""

    @respx.mock
    async def test_read_timeout(self, mock_client: DsmClient) -> None:
        methods: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            method = dict(request.url.params).get("method", "")
            methods.append(method)
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "ds-9"}})
            if method == "status":
                raise httpx.ReadTimeout("")
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        with pytest.raises(ToolError) as exc_info:
            await get_dir_size(mock_client, path="/homes/cmeans")
        assert "stop" in methods
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == "timeout"
        assert err["message"] == (
            "Get directory size failed: DSM didn't answer a status request on path: "
            "/homes/cmeans (ReadTimeout)."
        )
        assert err["suggestion"].startswith("Try a subdirectory")

    @pytest.mark.parametrize(
        ("exc", "code", "what"),
        [
            (httpx.ConnectError("refused"), "unavailable", "couldn't be reached for"),
            (httpx.ConnectTimeout("t"), "timeout", "couldn't be reached for"),
            (httpx.RemoteProtocolError("dropped"), "unavailable", "lost the connection during"),
        ],
    )
    @respx.mock
    async def test_a_status_read_that_never_left(
        self, mock_client: DsmClient, exc: Exception, code: str, what: str
    ) -> None:
        """Pre-QA r3 on #660: a connection that couldn't be opened never reached DSM."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            method = dict(request.url.params).get("method", "")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "ds-9"}})
            if method == "status":
                raise exc
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        with pytest.raises(ToolError) as exc_info:
            await get_dir_size(mock_client, path="/homes/cmeans")
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == code
        assert err["message"].startswith(f"Get directory size failed: DSM {what} a status request")
        # Pre-QA r5 on #660: a longer timeout doesn't help a NAS that can't be reached.
        reached = what != "couldn't be reached for"
        assert ("connection.timeout" in err["suggestion"]) is reached
        assert ("reachable" in err["suggestion"]) is not reached


class TestGetDirSizeCancellation:
    @respx.mock
    async def test_canceled_dir_size_still_stops(
        self, mock_client: DsmClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A canceled get_dir_size still stops its DirSize task (see search_files)."""
        import asyncio

        import anyio

        import mcp_synology.modules.filestation.metadata as metadata

        calls: list[str] = []
        blocked = asyncio.Event()

        async def side_effect(request: httpx.Request) -> httpx.Response:
            method = dict(request.url.params).get("method", "")
            calls.append(method)
            if method == "stop":
                await anyio.sleep(0)  # a checkpoint: re-canceled here unless shielded
                calls.append("stop-done")
            data: dict[str, object] = {"taskid": "ds-1"} if method == "start" else {}
            if method == "status":
                data = {"finished": False}
            return httpx.Response(200, json={"success": True, "data": data})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        async def blocked_sleep(seconds: float) -> None:
            blocked.set()
            await anyio.sleep_forever()

        monkeypatch.setattr(metadata.asyncio, "sleep", blocked_sleep)
        async with anyio.create_task_group() as tg:

            async def run() -> None:
                await get_dir_size(mock_client, path="/video/TV")

            tg.start_soon(run)
            await blocked.wait()
            tg.cancel_scope.cancel()
        assert "stop-done" in calls
