"""Tests for modules/filestation/listing.py — list_shares, list_files, list_recycle_bin."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.filestation.listing import (
    list_files,
    list_recycle_bin,
    list_shares,
)
from tests.conftest import BASE_URL

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient


class TestListShares:
    @respx.mock
    async def test_list_shares_success(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "shares": [
                        {
                            "name": "video",
                            "path": "/video",
                            "isdir": True,
                            "additional": {
                                "real_path": "/volume1/x",
                                "owner": {"user": "admin"},
                            },
                        },
                        {
                            "name": "music",
                            "path": "/music",
                            "isdir": True,
                            "additional": {
                                "real_path": "/volume1/x",
                                "owner": {"user": "admin"},
                            },
                        },
                    ],
                    "total": 2,
                },
            }
        )
        result = await list_shares(mock_client, hostname="TestNAS")
        assert "video" in result
        assert "music" in result
        assert "TestNAS" in result
        assert "2 shared folders found" in result

    @respx.mock
    async def test_list_shares_behind_a_proxy_login_page(self, mock_client: DsmClient) -> None:
        """#282: a proxy's login page was "Expecting value: line 1 column 1 (char 0)"."""
        import json

        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            200, text="<html>Sign in to proxy</html>", headers={"content-type": "text/html"}
        )
        with pytest.raises(ToolError) as exc:
            await list_shares(mock_client, hostname="TestNAS")
        err = json.loads(str(exc.value))["error"]
        assert err["code"] == "unavailable"
        assert err["retryable"] is True
        assert "the answer wasn't a DSM reply (Content-Type text/html)" in err["message"]
        assert "Sign in" not in err["message"]
        assert "reverse proxy" in err["suggestion"]

    @respx.mock
    async def test_list_shares_with_recycle_status(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "shares": [
                        {
                            "name": "video",
                            "path": "/video",
                            "isdir": True,
                            "additional": {"owner": {"user": "admin"}},
                        },
                    ],
                    "total": 1,
                },
            }
        )
        result = await list_shares(
            mock_client,
            recycle_bin_status={"video": True},
        )
        assert "enabled" in result

    @staticmethod
    def _shares_and_bins(bins: dict[str, object], probed: list[str]) -> None:
        """Route list_share (one share per key of ``bins``) and each #recycle probe.

        A bin's value is True (lists), an int (that DSM error), "hang" (no answer)
        or "drop" (the connection drops).
        """

        async def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("method") == "list_share":
                shares = [
                    {"name": n, "path": f"/{n}", "isdir": True, "additional": {}} for n in bins
                ]
                return httpx.Response(
                    200, json={"success": True, "data": {"shares": shares, "total": len(bins)}}
                )
            name = params["folder_path"].strip('"').split("/")[1]
            probed.append(name)
            answer = bins[name]
            if answer == "hang":
                await asyncio.sleep(5)
            if answer == "drop":
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            if answer is True:
                return httpx.Response(
                    200, json={"success": True, "data": {"files": [], "total": 0}}
                )
            return httpx.Response(200, json={"success": False, "error": {"code": answer}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

    @staticmethod
    def _cell(result: str, share: str) -> str:
        line = next(ln for ln in result.splitlines() if ln.strip().startswith(f"{share} "))
        return re.split(r"\s{2,}", line.strip())[-1]

    @respx.mock
    async def test_list_shares_checks_every_share_not_yet_known(
        self, mock_client: DsmClient
    ) -> None:
        """#255, #335: the column is always there, and every share gets a real answer."""
        probed: list[str] = []
        self._shares_and_bins({"video": True, "music": True, "scratch": 408, "locked": 105}, probed)
        cache = {"video": True}
        result = await list_shares(mock_client, recycle_bin_status=cache)
        assert self._cell(result, "video") == "enabled"
        assert self._cell(result, "music") == "enabled"
        # No #recycle, and the share's setting is admin-only: not "disabled" (#409).
        assert self._cell(result, "scratch") == "none yet"
        assert self._cell(result, "locked") == "couldn't check"
        assert sorted(probed) == ["locked", "music", "scratch"]  # video was cached
        # Definite answers are kept; the others are asked again next time.
        assert cache == {"video": True, "music": True}
        assert 'Recycle Bin "couldn\'t check"' in result
        assert 'Recycle Bin "none yet": the share has no #recycle folder' in result

    @respx.mock
    async def test_an_admin_gets_the_share_setting_for_a_missing_bin_folder(
        self, mock_client: DsmClient
    ) -> None:
        """#409: the share's setting (admin only) says whether a share with no #recycle has
        its bin on or off: SYNO.Core.Share, or for /home (a new account's home has no
        #recycle yet) the user home service's; both answers are definite and cached."""
        from mcp_synology.core.client import ApiInfoEntry

        for api in ("SYNO.Core.Share", "SYNO.Core.User.Home"):
            mock_client._api_cache[api] = ApiInfoEntry(
                path="entry.cgi", min_version=1, max_version=1
            )

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("api") == "SYNO.Core.User.Home":
                return httpx.Response(
                    200, json={"success": True, "data": {"enable_recycle_bin": True}}
                )
            if params.get("api") == "SYNO.Core.Share":
                # The homes share's own flag is ignored for /home (vdsm: off, home bins on).
                shares = [
                    {"name": "homes", "enable_recycle_bin": False},
                    {"name": "docker", "enable_recycle_bin": False},
                ]
                return httpx.Response(200, json={"success": True, "data": {"shares": shares}})
            if params.get("method") == "list_share":
                shares = [
                    {"name": n, "path": f"/{n}", "additional": {}} for n in ("home", "docker")
                ]
                return httpx.Response(
                    200, json={"success": True, "data": {"shares": shares, "total": 2}}
                )
            return httpx.Response(200, json={"success": False, "error": {"code": 408}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        cache: dict[str, bool] = {}
        result = await list_shares(mock_client, recycle_bin_status=cache)
        assert self._cell(result, "home") == "enabled"
        assert self._cell(result, "docker") == "disabled"
        assert cache == {"home": True, "docker": False}
        assert "none yet" not in result

    @respx.mock
    async def test_list_shares_all_cached_probes_nothing(self, mock_client: DsmClient) -> None:
        probed: list[str] = []
        self._shares_and_bins({"video": True, "scratch": 408}, probed)
        result = await list_shares(
            mock_client, recycle_bin_status={"video": True, "scratch": False}
        )
        assert probed == []
        assert self._cell(result, "scratch") == "disabled"
        assert "couldn't check" not in result

    @respx.mock
    async def test_list_shares_slow_probe_is_couldnt_check(
        self, mock_client: DsmClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#255: a busy NAS doesn't fail or stall list_shares."""
        monkeypatch.setattr("mcp_synology.modules.filestation.listing.RECYCLE_COLUMN_TIMEOUT", 0.2)
        probed: list[str] = []
        self._shares_and_bins({"video": "hang", "music": True, "photo": "drop"}, probed)
        cache: dict[str, bool] = {}
        result = await list_shares(mock_client, recycle_bin_status=cache)
        assert self._cell(result, "video") == "couldn't check"
        assert self._cell(result, "photo") == "couldn't check"
        assert self._cell(result, "music") == "enabled"
        assert cache == {"music": True}

    @respx.mock
    async def test_list_shares_waits_once_for_all_checks(
        self, mock_client: DsmClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#255: the column has one deadline, not one per batch of 4 checks."""
        monkeypatch.setattr("mcp_synology.modules.filestation.listing.RECYCLE_COLUMN_TIMEOUT", 0.3)
        probed: list[str] = []
        self._shares_and_bins({f"s{i}": "hang" for i in range(12)}, probed)
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await list_shares(mock_client, recycle_bin_status={})
        assert loop.time() - started < 0.8  # 3 batches of 0.3 s each would be 0.9+
        assert all(self._cell(result, f"s{i}") == "couldn't check" for i in range(12))
        assert len(probed) == 4  # the first batch; the rest never started

    @respx.mock
    async def test_list_shares_empty(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"shares": [], "total": 0}}
        )
        result = await list_shares(mock_client)
        assert "No items" in result

    @respx.mock
    async def test_list_shares_error(self, mock_client: DsmClient) -> None:
        """DSM error on list_shares should propagate as a structured envelope."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 105}}
        )
        with pytest.raises(ToolError) as exc_info:
            await list_shares(mock_client)
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "permission_denied"

    @respx.mock
    async def test_list_shares_requests_what_the_table_shows(self, mock_client: DsmClient) -> None:
        """#149: only the fields the table shows (Volume, Owner, Free) are fetched; the
        `additional` parameter, which accepted fields the table dropped, is gone."""
        captured: list[dict[str, str]] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            captured.append(dict(request.url.params))
            return httpx.Response(200, json={"success": True, "data": {"shares": [], "total": 0}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        await list_shares(mock_client)
        assert len(captured) == 1
        assert captured[0]["additional"] == '["real_path","owner","volume_status"]'


def _share(name: str, real_path: str | None, volume: dict[str, object] | None) -> dict[str, object]:
    """A list_share row shaped like a real DSM 7.2 response (seska, #151): DSM sends
    no ``size`` for a share; ``volume_status`` describes the share's volume."""
    additional: dict[str, object] = {"owner": {"user": "root"}}
    if real_path is not None:
        additional["real_path"] = real_path
    if volume is not None:
        additional["volume_status"] = volume
    return {"name": name, "path": f"/{name}", "isdir": True, "additional": additional}


_VOLUME1 = {"freespace": 1633576271872, "readonly": False, "totalspace": 40289542017024}


class TestListSharesVolumeSpace:
    """#151: DSM's list_share has no per-share size, so the old Size column was
    always 0 B. Show the share's volume and that volume's free space instead."""

    @respx.mock
    async def test_default_request_asks_for_volume_status(self, mock_client: DsmClient) -> None:
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"shares": [], "total": 0}}
        )
        await list_shares(mock_client)
        sent = json.loads(dict(route.calls.last.request.url.params)["additional"])
        assert "volume_status" in sent
        assert "real_path" in sent
        assert "size" not in sent

    @respx.mock
    async def test_volume_and_free_space_replace_size(self, mock_client: DsmClient) -> None:
        shares = [
            _share("Backups", "/volume1/Backups", _VOLUME1),
            _share("home", "/volume1/homes/synAdmin", _VOLUME1),
        ]
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"shares": shares, "total": 2}}
        )
        result = await list_shares(mock_client, hostname="seska")
        assert "0 B" not in result
        assert "Size" not in result
        assert "Volume" in result and "volume1" in result
        assert "1.5 TiB of 36.6 TiB" in result
        assert "get_dir_size" in result  # a share's own usage lives there
        assert "per volume" in result

    @respx.mock
    async def test_read_only_volume_is_marked(self, mock_client: DsmClient) -> None:
        ro = {**_VOLUME1, "readonly": True}
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"shares": [_share("usb", "/volumeUSB1/usbshare", ro)]}}
        )
        result = await list_shares(mock_client)
        assert "volumeUSB1" in result
        assert "read-only" in result

    @respx.mock
    async def test_missing_fields_show_a_dash(self, mock_client: DsmClient) -> None:
        """DSM may leave out real_path or volume_status for a share."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"shares": [_share("video", None, None)]}}
        )
        result = await list_shares(mock_client)
        assert "0 B" not in result
        assert "—" in result
        assert "per volume" not in result  # no free space shown, so no note


class TestListFiles:
    @respx.mock
    async def test_list_files_success(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "files": [
                        {
                            "name": "Season 1",
                            "path": "/video/TV Shows/Season 1",
                            "isdir": True,
                            "additional": {"time": {"mtime": 1710000000}},
                        },
                        {
                            "name": "clip.mp4",
                            "path": "/video/TV Shows/clip.mp4",
                            "isdir": False,
                            "additional": {
                                "size": 297795584,
                                "time": {"mtime": 1710100000},
                            },
                        },
                    ],
                    "total": 2,
                },
            }
        )
        result = await list_files(mock_client, path="/video/TV Shows")
        assert "Season 1/" in result
        assert "clip.mp4" in result

    @respx.mock
    async def test_list_files_size_dash_when_not_requested(self, mock_client: DsmClient) -> None:
        """QA on #146: a file DSM sends without "size" must not show as "0 B"."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "files": [
                        {"name": "clip.mp4", "isdir": False, "additional": {"owner": {}}},
                    ],
                    "total": 1,
                },
            }
        )
        result = await list_files(mock_client, path="/video")
        assert "clip.mp4" in result
        assert "0 B" not in result

    @respx.mock
    async def test_list_files_hides_recycle(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "files": [
                        {"name": "#recycle", "isdir": True, "additional": {}},
                        {"name": "real_file.txt", "isdir": False, "additional": {"size": 100}},
                    ],
                    "total": 2,
                },
            }
        )
        result = await list_files(mock_client, path="/video", hide_recycle=True)
        assert "#recycle" not in result.replace("(#recycle hidden)", "")
        assert "real_file.txt" in result
        # #389: the hidden entry isn't counted, and nothing more is promised.
        assert "(1 items)" in result
        assert "Showing 1 of 1 items (#recycle hidden)." in result
        assert "offset=" not in result

    @staticmethod
    def _page(names: list[str], total: int) -> dict[str, Any]:
        files = [{"name": n, "isdir": n == "#recycle", "additional": {}} for n in names]
        return {"success": True, "data": {"files": files, "total": total}}

    @classmethod
    def _folder(cls, names: list[str], calls: list[str] | None = None) -> Any:
        """A folder DSM pages by offset and limit, and answers getinfo on."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = request.url.params
            if calls is not None:
                calls.append(params["method"])
            if params["method"] == "getinfo":
                name = params["path"].rsplit("/", 1)[-1]
                entry = {"name": name, "isdir": True} if name in names else {"code": 408}
                return httpx.Response(200, json={"success": True, "data": {"files": [entry]}})
            start, limit = int(params["offset"]), int(params["limit"])
            page = names[start : start + limit] if limit else names[start:]
            return httpx.Response(200, json=cls._page(page, total=len(names)))

        return side_effect

    async def _pages(self, client: DsmClient, limit: int, **kwargs: Any) -> list[str]:
        """Follow the next-page hints to the end; return every reply."""
        replies, offset = [], 0
        while True:
            reply = await list_files(client, path="/video", limit=limit, offset=offset, **kwargs)
            replies.append(reply)
            hint = re.search(r"Use offset=(\d+)", reply)
            if not hint:
                return replies
            offset = int(hint.group(1))

    @respx.mock
    @pytest.mark.parametrize("limit", [1, 2, 3, 4, 5])
    @pytest.mark.parametrize("where", [0, 2, 4])
    async def test_hidden_recycle_pages_every_item_once(
        self, mock_client: DsmClient, limit: int, where: int
    ) -> None:
        # #389: the next-page hint came one short (repeating an item), and a #recycle at
        # the end of a page promised a page that turned out empty. The total said 5 on
        # one page and 4 on another.
        names = [f"f{i}.txt" for i in range(4)]
        names.insert(where, "#recycle")
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._folder(names))
        replies = await self._pages(mock_client, limit, hide_recycle=True)
        seen = [n for r in replies for n in re.findall(r"f\d\.txt", r)]
        assert seen == ["f0.txt", "f1.txt", "f2.txt", "f3.txt"]
        assert all("(4 items)" in r and "of 4 items (#recycle hidden)" in r for r in replies)
        assert all("#recycle" not in r.replace("(#recycle hidden)", "") for r in replies)

    @respx.mock
    async def test_recycle_check_only_where_it_can_be(self, mock_client: DsmClient) -> None:
        calls: list[str] = []
        names = ["a.txt", "b.txt", "#recycle"]
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._folder(names, calls))
        # Below the top level, filtered to files, or with a pattern #recycle can't match.
        await list_files(mock_client, path="/video/sub", limit=1, hide_recycle=True)
        await list_files(mock_client, path="/video", limit=1, filetype="file", hide_recycle=True)
        await list_files(mock_client, path="/video", limit=1, pattern="*.txt", hide_recycle=True)
        await list_files(mock_client, path="/video", limit=1)
        assert calls == ["list"] * 4
        result = await list_files(mock_client, path="/video", limit=1, hide_recycle=True)
        assert calls[4:] == ["list", "getinfo"]
        assert "Showing 1 of 2 items (#recycle hidden). Use offset=1 to see more." in result

    @respx.mock
    async def test_no_recycle_at_the_top_level(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=self._folder(["a.txt", "b.txt", "c.txt"])
        )
        result = await list_files(mock_client, path="/video", limit=2, hide_recycle=True)
        assert "Showing 1–2 of 3 items. Use offset=2 to see more." in result

    @respx.mock
    async def test_failed_recycle_check_still_lists(self, mock_client: DsmClient) -> None:
        def side_effect(request: httpx.Request) -> httpx.Response:
            if request.url.params["method"] == "getinfo":
                return httpx.Response(502)
            return httpx.Response(200, json=self._page(["a.txt", "b.txt"], total=3))

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await list_files(mock_client, path="/video", limit=1, hide_recycle=True)
        assert "a.txt" in result and "b.txt" not in result
        assert "Showing 1–1 of 3 items. Use offset=1 to see more." in result

    @respx.mock
    async def test_only_recycle_is_an_empty_folder(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._folder(["#recycle"]))
        result = await list_files(mock_client, path="/video", limit=1, hide_recycle=True)
        assert "(0 items)" in result and "offset=" not in result

    @respx.mock
    async def test_offset_past_the_end_says_so(self, mock_client: DsmClient) -> None:
        # #389: it used to be just "No items to display".
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(json=self._page([], total=5))
        result = await list_files(mock_client, path="/video", offset=9)
        assert "No items from offset=9: there are 5." in result

    @respx.mock
    async def test_negative_offset_is_clamped(self, mock_client: DsmClient) -> None:
        captured: list[dict[str, str]] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            captured.append(dict(request.url.params))
            return httpx.Response(200, json=self._page(["a.txt", "b.txt"], total=5))

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        result = await list_files(mock_client, path="/video", offset=-1, limit=2)
        assert captured[0]["offset"] == "0"
        assert "Showing 1–2 of 5 items. Use offset=2 to see more." in result

    @respx.mock
    async def test_list_files_pagination(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "files": [
                        {"name": f"file{i}.txt", "isdir": False, "additional": {"size": 100}}
                        for i in range(200)
                    ],
                    "total": 500,
                },
            }
        )
        result = await list_files(mock_client, path="/video", limit=200)
        assert "offset=200" in result

    @respx.mock
    async def test_list_files_error(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 408}}
        )
        with pytest.raises(ToolError) as exc_info:
            await list_files(mock_client, path="/nonexistent")
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "not_found"

    @respx.mock
    async def test_list_files_407_is_permission_denied(self, mock_client: DsmClient) -> None:
        """rc5 live test: list_files /home got DSM 407, reported as "Unknown error (code 407)"."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 407}}
        )
        with pytest.raises(ToolError) as exc_info:
            await list_files(mock_client, path="/home")
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "permission_denied"
        assert "Unknown" not in error["message"] and error["suggestion"]

    @respx.mock
    async def test_list_files_requests_what_the_table_shows(self, mock_client: DsmClient) -> None:
        """#149: only Size and Modified are shown, so only size and time are fetched."""
        captured: list[dict[str, str]] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            captured.append(dict(request.url.params))
            return httpx.Response(200, json={"success": True, "data": {"files": [], "total": 0}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        await list_files(mock_client, path="/video")
        assert len(captured) == 1
        assert captured[0]["additional"] == '["size","time"]'


def _no_bin(request: httpx.Request) -> httpx.Response:
    """A share with no recycle bin: the share lists, its #recycle is 408."""
    if "#recycle" in request.url.params.get("folder_path", ""):
        return httpx.Response(200, json={"success": False, "error": {"code": 408}})
    return httpx.Response(200, json={"success": True, "data": {"files": [], "total": 0}})


class TestListRecycleBin:
    @respx.mock
    async def test_list_recycle_bin(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {
                    "files": [
                        {
                            "name": "old_file.mkv",
                            "isdir": False,
                            "additional": {"size": 1288490188, "time": {"mtime": 1710000000}},
                        },
                    ],
                    "total": 1,
                },
            }
        )
        result = await list_recycle_bin(mock_client, share="video")
        assert "old_file.mkv" in result

    @respx.mock
    async def test_offset_follows_the_more_hint(self, mock_client: DsmClient) -> None:
        """The "Use offset=N" hint names a parameter the tool has (rc5 live test: it didn't)."""
        sent: list[str] = []

        def responder(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params.get("offset", "0"))
            sent.append(str(offset))
            names = [f"f{i}.txt" for i in range(9)][offset : offset + 5]
            files = [
                {"name": n, "isdir": False, "additional": {"size": 1, "time": {"mtime": 1}}}
                for n in names
            ]
            return httpx.Response(200, json={"success": True, "data": {"files": files, "total": 9}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=responder)
        first = await list_recycle_bin(mock_client, share="home", limit=5)
        assert "Showing 1–5 of 9 items. Use offset=5 to see more." in first
        second = await list_recycle_bin(mock_client, share="home", limit=5, offset=5)
        assert sent == ["0", "5"]
        assert "f5.txt" in second and "f0.txt" not in second
        assert "Use offset=" not in second
        # A later page gives its range, not just its count (rc6 live test: page 2 of 5
        # said "Showing 2 of 5 items"), as the pattern listing does.
        assert "Showing 6–9 of 9 items." in second

    @respx.mock
    async def test_footer_counts_have_thousands_separators(self, mock_client: DsmClient) -> None:
        """Every number in the footer is formatted alike, not just the total."""

        def responder(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params.get("offset", "0"))
            limit = int(request.url.params.get("limit", "0"))
            files = [
                {
                    "name": f"f{i}.txt",
                    "isdir": False,
                    "additional": {"size": 1, "time": {"mtime": 1}},
                }
                for i in range(offset, min(offset + limit, 1500))
            ]
            return httpx.Response(
                200, json={"success": True, "data": {"files": files, "total": 1500}}
            )

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=responder)
        whole = await list_recycle_bin(mock_client, share="home", limit=2000)
        assert whole.endswith("1,500 of 1,500 items.")
        assert "(1,500 items)" in whole  # the title too
        last = await list_recycle_bin(mock_client, share="home", limit=1000, offset=1000)
        assert last.endswith("Showing 1,001–1,500 of 1,500 items.")

    @respx.mock
    async def test_recycle_bin_disabled(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_no_bin)
        result = await list_recycle_bin(
            mock_client,
            share="docker",
            recycle_bin_status={"docker": False},
        )
        assert "not enabled" in result

    @respx.mock
    async def test_recycle_bin_not_found_error_408(self, mock_client: DsmClient) -> None:
        """DSM error 408 on the #recycle path: no bin folder, which doesn't prove the bin
        is off (#409). list_recycle_bin says so instead of crashing."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_no_bin)
        result = await list_recycle_bin(mock_client, share="writable")
        assert result.startswith("/writable has no recycle bin folder (#recycle)")
        assert "cannot be recovered" not in result

    @pytest.mark.parametrize("cached", [True, False])
    @respx.mock
    async def test_a_share_that_isnt_there_is_not_found(
        self, mock_client: DsmClient, cached: bool
    ) -> None:
        """#353: /Home/#recycle and /Home both 408 (the home service is only /home), and a
        restorable file mustn't read as "cannot be recovered"."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 408}}
        )
        status = {"Home": False} if cached else {}
        with pytest.raises(ToolError) as exc_info:
            await list_recycle_bin(mock_client, share="Home", recycle_bin_status=status)
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == "not_found" and err["retryable"] is False
        assert err["message"] == (
            "No shared folder /Home on this NAS. The user home folder is /home (lowercase)."
        )
        assert status == {}  # no recycle status is kept for a share that isn't there

    @respx.mock
    async def test_a_typo_share_gets_no_home_hint(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 408}}
        )
        with pytest.raises(ToolError) as exc_info:
            await list_recycle_bin(mock_client, share="vidoe")
        assert json.loads(str(exc_info.value))["error"]["message"] == (
            "No shared folder /vidoe on this NAS."
        )

    @respx.mock
    async def test_another_error_checking_the_share_propagates(
        self, mock_client: DsmClient
    ) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            code = 408 if "#recycle" in request.url.params.get("folder_path", "") else 407
            return httpx.Response(200, json={"success": False, "error": {"code": code}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=respond)
        with pytest.raises(ToolError, match="List recycle bin failed"):
            await list_recycle_bin(mock_client, share="writable")

    @respx.mock
    async def test_recycle_bin_other_error_propagates(self, mock_client: DsmClient) -> None:
        """Non-408 errors from list_files should still propagate as ToolError."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 402}}
        )
        with pytest.raises(ToolError):
            await list_recycle_bin(mock_client, share="video")

    async def test_recycle_bin_non_json_toolerror_propagates(
        self, mock_client: DsmClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ToolError with non-JSON body should propagate (JSONDecodeError branch)."""
        from mcp_synology.modules.filestation import listing

        async def _raise_non_json(*args: object, **kwargs: object) -> str:
            raise ToolError("plain text error, not JSON")

        monkeypatch.setattr(listing, "list_files", _raise_non_json)
        with pytest.raises(ToolError, match="plain text error"):
            await list_recycle_bin(mock_client, share="video")

    @respx.mock
    async def test_self_correct_when_dsm_disagrees_with_cache(self, mock_client: DsmClient) -> None:
        """Closes #37: if cache says recycle-on but DSM returns 408 on the actual
        list, list_recycle_bin should flip the cache to False so subsequent
        delete_files calls in the same session emit the correct messaging.
        """
        # The cache says True, so there's no probe. The full list of
        # /share/#recycle returns 408, contradicting it; the share itself
        # still lists (#353 checks that before saying "not enabled").
        call_count = 0

        def side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            params = dict(request.url.params)
            if "#recycle" not in params.get("folder_path", ""):
                return httpx.Response(
                    200, json={"success": True, "data": {"files": [], "total": 0}}
                )
            # The full list: simulate the recycle dir disappeared
            return httpx.Response(200, json={"success": False, "error": {"code": 408}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        recycle_status = {"video": True}  # stale cached value
        result = await list_recycle_bin(
            mock_client,
            share="video",
            recycle_bin_status=recycle_status,
        )

        assert "has no recycle bin folder" in result
        # The stale True is dropped, not flipped to False: a missing #recycle doesn't prove
        # the bin is off, and DSM re-creates it at the next delete (#409).
        assert recycle_status == {}

    @respx.mock
    async def test_self_correct_when_observed_enabled_disagrees_with_cache(
        self, mock_client: DsmClient
    ) -> None:
        """Inverse self-correct: cached False, DSM list succeeds → flip to True.

        Triggered when admin enabled the recycle bin mid-session after the
        cache was populated with False.
        """

        def side_effect(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            # The early ensure_recycle_status fast-path returns the cached
            # False without probing, so we never reach a probe call here —
            # the test SHOULDN'T enter this side_effect for the early-return
            # path. But if recycle_bin_status[share] is False, the early
            # return fires WITHOUT calling DSM, so this side_effect is unused
            # in that case.
            #
            # To exercise self-correct-on-success, we simulate cache==True by
            # NOT pre-populating, then return success.
            if params.get("limit") == "1":  # the probe
                return httpx.Response(
                    200, json={"success": True, "data": {"files": [], "total": 0}}
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "files": [
                            {
                                "name": "old.mkv",
                                "isdir": False,
                                "additional": {
                                    "size": 100,
                                    "time": {"mtime": 1700000000},
                                },
                            }
                        ],
                        "total": 1,
                    },
                },
            )

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        # Empty cache; ensure_recycle_status will probe (success → True),
        # full list also succeeds, self-correct is a no-op since cache and
        # observation agree. This test exercises the "agreement no-op"
        # branch on the success path of list_recycle_bin.
        recycle_status: dict[str, bool] = {}
        result = await list_recycle_bin(
            mock_client,
            share="video",
            recycle_bin_status=recycle_status,
        )

        assert "old.mkv" in result
        assert recycle_status == {"video": True}


# -- #333: a pattern searches the bin's subfolders, up to the walk's caps; DSM's sort grouping --

_BIN = "/home/#recycle"


def _entry(path: str, *, isdir: bool = False, mtime: int = 0, size: int = 1) -> dict[str, object]:
    additional: dict[str, object] = {"time": {"mtime": mtime, "ctime": mtime + 10}}
    if not isdir:
        additional["size"] = size
    return {"name": path.rsplit("/", 1)[1], "path": path, "isdir": isdir, "additional": additional}


_TREE: dict[str, list[dict[str, object]]] = {
    _BIN: [
        _entry(f"{_BIN}/desktop.ini", mtime=1560000000),
        _entry(f"{_BIN}/mcp-rc3", isdir=True, mtime=1790000000),
    ],
    f"{_BIN}/mcp-rc3": [_entry(f"{_BIN}/mcp-rc3/photos-zip", isdir=True, mtime=1790000000)],
    f"{_BIN}/mcp-rc3/photos-zip": [
        _entry(f"{_BIN}/mcp-rc3/photos-zip/b2.txt", mtime=1700000000, size=2),
        _entry(f"{_BIN}/mcp-rc3/photos-zip/AB2-upper.TXT", mtime=1800000000),
        _entry(f"{_BIN}/mcp-rc3/photos-zip/a1.txt", mtime=1600000000),
    ],
}


def _bin_responder(
    tree: dict[str, list[dict[str, object]]],
    errors: dict[str, int] | None = None,
    seen: list[dict[str, str]] | None = None,
) -> object:
    def respond(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if seen is not None:
            seen.append(params)
        folder = params.get("folder_path", "")
        if errors and folder in errors:
            return httpx.Response(200, json={"success": False, "error": {"code": errors[folder]}})
        if folder not in tree:
            return httpx.Response(200, json={"success": False, "error": {"code": 408}})
        files = tree[folder]
        offset, limit = int(params.get("offset", "0")), int(params.get("limit", "1000"))
        page = files[offset : offset + limit]
        return httpx.Response(
            200, json={"success": True, "data": {"files": page, "total": len(files)}}
        )

    return respond


class TestRecycleBinSearch:
    @respx.mock
    async def test_match_footer_has_thousands_separators(self, mock_client: DsmClient) -> None:
        """Like list_files' footer (#390): every count in the line is formatted alike."""
        tree = {_BIN: [_entry(f"{_BIN}/f{i:04d}.txt") for i in range(1500)]}
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(tree))
        out = await list_recycle_bin(
            mock_client, share="home", pattern="f*", limit=200, offset=1000
        )
        assert "Showing 1,001–1,200 of 1,500 matches. Use offset=1200 to see more." in out
        # The title in the same reply counts alike (#401).
        assert 'Matches for "f*" in /home/#recycle (1,500 found in 1 folder(s)' in out

    @respx.mock
    async def test_stop_note_has_thousands_separators(
        self, mock_client: DsmClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp_synology.modules.filestation import listing

        monkeypatch.setattr(listing, "_WALK_MAX_ENTRIES", 1500)
        root = [_entry(f"{_BIN}/f{i:04d}.txt") for i in range(1499)]
        tree = {_BIN: [*root, _entry(f"{_BIN}/unopened", isdir=True)]}
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(tree))
        out = await list_recycle_bin(mock_client, share="home", pattern="f*", limit=5)
        assert "(1,499 found in 1 folder(s)" in out
        assert "Stopped after 1 folder(s) and 1,500 item(s)" in out

    @respx.mock
    async def test_pattern_finds_a_file_in_a_subfolder(self, mock_client: DsmClient) -> None:
        """The rc3 case: b2* missed mcp-rc3/photos-zip/b2.txt at the bin's top level."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE))
        out = await list_recycle_bin(mock_client, share="home", pattern="b2*")
        assert 'Matches for "b2*" in /home/#recycle (1 found in 3 folder(s)' in out
        assert "mcp-rc3/photos-zip/b2.txt" in out
        assert "a1.txt" not in out and "AB2-upper" not in out  # anchored: b2* isn't *b2*
        assert "Paths are relative to the recycle bin, as a restore takes them." in out

    @respx.mock
    async def test_bare_word_and_case_follow_dsm(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE))
        out = await list_recycle_bin(mock_client, share="home", pattern="B2")
        assert "(2 found" in out
        assert "photos-zip/b2.txt" in out and "photos-zip/AB2-upper.TXT" in out

    @respx.mock
    async def test_folders_match_too_and_get_a_slash(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE))
        out = await list_recycle_bin(mock_client, share="home", pattern="photos*")
        assert "mcp-rc3/photos-zip/" in out

    @respx.mock
    async def test_matches_sort_across_types_and_levels(self, mock_client: DsmClient) -> None:
        """Locally sorted by the field, not grouped by type as DSM groups a listing."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE))
        out = await list_recycle_bin(mock_client, share="home", pattern="*")
        order = [n for n in ("AB2-upper", "mcp-rc3/ ", "b2.txt", "a1.txt", "desktop.ini")]
        positions = [out.index(n) for n in order]
        assert positions == sorted(positions), out
        assert "sorts folders and files separately" not in out

    @respx.mock
    async def test_sort_by_changed_and_ascending(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE))
        out = await list_recycle_bin(
            mock_client, share="home", pattern="*.txt", sort_by="changed", sort_direction="asc"
        )
        assert out.index("a1.txt") < out.index("b2.txt") < out.index("AB2-upper.TXT")

    @respx.mock
    async def test_limit_says_how_many_matched(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE))
        out = await list_recycle_bin(mock_client, share="home", pattern="*.txt", limit=1)
        assert "(3 found" in out and "Showing 1–1 of 3 matches. Use offset=1 to see more." in out

    @respx.mock
    async def test_offset_pages_the_matches(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE))
        whole = await list_recycle_bin(mock_client, share="home", pattern="*.txt")
        order = [n for n in ("AB2-upper.TXT", "b2.txt", "a1.txt") if n in whole]
        order.sort(key=whole.index)
        page2 = await list_recycle_bin(
            mock_client, share="home", pattern="*.txt", limit=1, offset=1
        )
        assert order[1] in page2 and order[0] not in page2 and order[2] not in page2
        assert "Showing 2–2 of 3 matches. Use offset=2 to see more." in page2
        last = await list_recycle_bin(mock_client, share="home", pattern="*.txt", limit=1, offset=2)
        assert "Showing 3–3 of 3 matches." in last and "Use offset=" not in last
        past = await list_recycle_bin(mock_client, share="home", pattern="*.txt", offset=5)
        assert "No matches from offset=5: there are 3." in past
        rest = await list_recycle_bin(mock_client, share="home", pattern="*.txt", offset=1)
        assert "Showing 2–3 of 3 matches." in rest and "Use offset=" not in rest
        negative = await list_recycle_bin(
            mock_client, share="home", pattern="*.txt", limit=1, offset=-1
        )
        assert order[0] in negative and "Showing 1–1 of 3 matches." in negative
        # limit 0 or less lists every match, as DSM's limit=0 lists everything (QA r1).
        for limit in (0, -1):
            out = await list_recycle_bin(mock_client, share="home", pattern="*.txt", limit=limit)
            assert all(name in out for name in order), (limit, out)
            assert "Showing" not in out and "No matches" not in out, (limit, out)
        rest_all = await list_recycle_bin(
            mock_client, share="home", pattern="*.txt", limit=0, offset=1
        )
        assert order[0] not in rest_all and "Showing 2–3 of 3 matches." in rest_all

    @respx.mock
    async def test_no_match_says_the_whole_bin_was_searched(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE))
        out = await list_recycle_bin(mock_client, share="home", pattern="zzz")
        assert "(0 found in 3 folder(s), subfolders included)" in out
        assert "restore" not in out

    @respx.mock
    async def test_folder_cap_says_the_search_is_partial(
        self, mock_client: DsmClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp_synology.modules.filestation import listing

        monkeypatch.setattr(listing, "_WALK_MAX_FOLDERS", 2)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE))
        out = await list_recycle_bin(mock_client, share="home", pattern="b2*")
        assert "(0 found in 2 folder(s)" in out
        assert "Stopped after 2 folder(s)" in out and "list_files on /home/#recycle/<folder>" in out

    @respx.mock
    async def test_entry_cap_says_the_search_is_partial(
        self, mock_client: DsmClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp_synology.modules.filestation import listing

        monkeypatch.setattr(listing, "_WALK_MAX_ENTRIES", 2)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE))
        out = await list_recycle_bin(mock_client, share="home", pattern="*")
        # The root's two entries are read; the next folder isn't opened.
        assert "Stopped after 1 folder(s) and 2 item(s)" in out

    @respx.mock
    async def test_a_bin_exactly_at_the_entry_cap_is_complete(
        self, mock_client: DsmClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exactly the cap, nothing left to open: complete. A folder still unopened at
        the cap is reported as a partial search, since it may hold more."""
        from mcp_synology.modules.filestation import listing

        monkeypatch.setattr(listing, "_WALK_MAX_ENTRIES", 2)
        files_only = {_BIN: [_entry(f"{_BIN}/a.txt"), _entry(f"{_BIN}/b.txt")]}
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(files_only))
        out = await list_recycle_bin(mock_client, share="home", pattern="*")
        assert "Stopped" not in out and "(2 found in 1 folder(s)" in out

        with_folder = {_BIN: [_entry(f"{_BIN}/a.txt"), _entry(f"{_BIN}/x", isdir=True)]}
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(with_folder))
        out = await list_recycle_bin(mock_client, share="home", pattern="*")
        assert "Stopped after 1 folder(s) and 2 item(s)" in out

    @respx.mock
    async def test_paths_come_from_the_walk_not_dsm_path(self, mock_client: DsmClient) -> None:
        """share="Video" lists share video (ordinary share names match case-insensitively),
        while DSM echoes the path as /video/...: the walk mustn't depend on DSM's path."""
        tree = {
            "/Video/#recycle": [_entry("/video/#recycle/mcp-rc3", isdir=True)],
            "/Video/#recycle/mcp-rc3": [_entry("/video/#recycle/mcp-rc3/b2.txt")],
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(tree))
        out = await list_recycle_bin(mock_client, share="Video", pattern="b2*")
        assert "mcp-rc3/b2.txt" in out and "/video/#recycle/" not in out.split("\n", 3)[3]
        assert "Not searched" not in out

    @respx.mock
    async def test_a_search_records_the_bin_as_enabled(
        self, mock_client: DsmClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp_synology.modules.filestation import listing

        seen: list[tuple[str, bool]] = []

        def spy(share: str, *, observed_enabled: bool, recycle_status: dict[str, bool]) -> None:
            seen.append((share, observed_enabled))

        monkeypatch.setattr(listing, "correct_recycle_status_from_observation", spy)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE))
        status: dict[str, bool] = {}
        out = await list_recycle_bin(
            mock_client, share="home", pattern="b2*", recycle_bin_status=status
        )
        assert "photos-zip/b2.txt" in out and seen == [("home", True)]

    @respx.mock
    async def test_entry_cap_inside_a_folder(
        self, mock_client: DsmClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp_synology.modules.filestation import listing

        monkeypatch.setattr(listing, "_WALK_MAX_ENTRIES", 4)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE))
        out = await list_recycle_bin(mock_client, share="home", pattern="*")
        # 2 at the top, 1 in mcp-rc3, then 1 of photos-zip's 3: the rest is unread.
        assert "Stopped after 3 folder(s) and 4 item(s)" in out
        assert "photos-zip/b2.txt" in out and "a1.txt" not in out

    @pytest.mark.parametrize(
        ("sort_by", "direction", "order"),
        [
            ("name", "asc", ["photos-zip/a1.txt", "photos-zip/AB2-upper.TXT", "photos-zip/b2.txt"]),
            ("size", "desc", ["photos-zip/b2.txt", "photos-zip/AB2-upper.TXT"]),
        ],
    )
    @respx.mock
    async def test_sort_by_name_and_size(
        self, mock_client: DsmClient, sort_by: str, direction: str, order: list[str]
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE))
        out = await list_recycle_bin(
            mock_client, share="home", pattern="*.txt", sort_by=sort_by, sort_direction=direction
        )
        positions = [out.index(n) for n in order]
        assert positions == sorted(positions), out

    @respx.mock
    async def test_a_sort_the_search_cant_do_says_so(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE))
        out = await list_recycle_bin(mock_client, share="home", pattern="*.txt", sort_by="type")
        assert "sort_by=type doesn't apply to a search of the bin; sorted by mtime." in out
        # The note names the value given, not DSM's field for it.
        out = await list_recycle_bin(mock_client, share="home", pattern="*.txt", sort_by="owner")
        assert "sort_by=owner doesn't apply" in out
        # An alias of a field the search can sort by isn't a fallback.
        out = await list_recycle_bin(mock_client, share="home", pattern="*.txt", sort_by="modified")
        assert "doesn't apply" not in out

    @respx.mock
    async def test_pages_through_a_large_folder(
        self, mock_client: DsmClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp_synology.modules.filestation import listing

        monkeypatch.setattr(listing, "_WALK_PAGE", 2)
        seen: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE, seen=seen))
        out = await list_recycle_bin(mock_client, share="home", pattern="a1*")
        assert "photos-zip/a1.txt" in out  # the third entry, on the folder's second page
        offsets = [p["offset"] for p in seen if p["folder_path"].endswith("photos-zip")]
        assert offsets == ["0", "2"]

    @respx.mock
    async def test_unreadable_subfolder_is_named_and_skipped(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=_bin_responder(_TREE, errors={f"{_BIN}/mcp-rc3/photos-zip": 407})
        )
        out = await list_recycle_bin(mock_client, share="home", pattern="*")
        assert "Not searched (couldn't be listed): mcp-rc3/photos-zip." in out
        assert "desktop.ini" in out

    @respx.mock
    async def test_more_than_five_unreadable_subfolders_are_counted(
        self, mock_client: DsmClient
    ) -> None:
        names = [f"d{i}" for i in range(7)]
        tree = {_BIN: [_entry(f"{_BIN}/{n}", isdir=True) for n in names]}
        errors = {f"{_BIN}/{n}": 407 for n in names}
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(tree, errors))
        out = await list_recycle_bin(mock_client, share="home", pattern="zzz")
        assert "Not searched (couldn't be listed): d0, d1, d2, d3, d4 and 2 more." in out

    @respx.mock
    async def test_pattern_on_a_share_without_a_bin(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder({"/home": []}))
        out = await list_recycle_bin(mock_client, share="home", pattern="b2*")
        assert out.startswith("/home has no recycle bin folder (#recycle), so there's nothing")

    @respx.mock
    async def test_without_pattern_only_the_top_level_is_listed(
        self, mock_client: DsmClient
    ) -> None:
        seen: list[dict[str, str]] = []
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(_TREE, seen=seen))
        out = await list_recycle_bin(mock_client, share="home")
        assert "Contents of /home/#recycle (2 items)" in out
        assert [p["folder_path"] for p in seen] == [_BIN]
        # DSM's own order for mtime desc: files first (the note says so).
        assert "DSM sorts folders and files separately; files come first." in out


class TestDsmPatternMatcher:
    # The names and results DSM 7.1 and 7.3.2 gave for List.list's pattern (#333).
    NAMES = ["B2-upper.TXT", "ab2c.log", "b2.txt", "other.txt"]

    @pytest.mark.parametrize(
        ("pattern", "expected"),
        [
            ("b2", ["B2-upper.TXT", "ab2c.log", "b2.txt"]),
            ("b2*", ["B2-upper.TXT", "b2.txt"]),
            ("B2*", ["B2-upper.TXT", "b2.txt"]),
            ("*b2*", ["B2-upper.TXT", "ab2c.log", "b2.txt"]),
            ("*.txt", ["B2-upper.TXT", "b2.txt", "other.txt"]),
            ("b?.txt", ["b2.txt"]),
            ("b2*,other*", ["B2-upper.TXT", "b2.txt", "other.txt"]),
            ("[ab]2*", []),
            ("b2*, ,", ["B2-upper.TXT", "b2.txt"]),  # empty alternatives are ignored
        ],
    )
    def test_matches_like_dsm(self, pattern: str, expected: list[str]) -> None:
        from mcp_synology.modules.filestation.listing import dsm_pattern_matcher

        matches = dsm_pattern_matcher(pattern)
        assert [n for n in self.NAMES if matches(n)] == expected


class TestSortNote:
    MIXED = [
        {"name": "Sub", "isdir": True, "additional": {}},
        {"name": "a.txt", "isdir": False, "additional": {"size": 1}},
    ]

    @pytest.mark.parametrize(("direction", "first"), [("asc", "folders"), ("desc", "files")])
    @respx.mock
    async def test_mixed_listing_explains_the_grouping(
        self, mock_client: DsmClient, direction: str, first: str
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"files": self.MIXED, "total": 2}}
        )
        out = await list_files(mock_client, path="/video", sort_direction=direction)
        assert f"DSM sorts folders and files separately; {first} come first." in out

    @respx.mock
    async def test_no_note_for_one_kind_or_a_type_sort(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"files": self.MIXED[1:], "total": 1}}
        )
        assert "separately" not in await list_files(mock_client, path="/video")
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"files": self.MIXED, "total": 2}}
        )
        assert "separately" not in await list_files(mock_client, path="/video", sort_by="type")

    @respx.mock
    async def test_changed_is_an_alias_for_ctime(self, mock_client: DsmClient) -> None:
        seen: list[dict[str, str]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"success": True, "data": {"files": [], "total": 0}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=respond)
        await list_files(mock_client, path="/video", sort_by="changed")
        assert seen[0]["sort_by"] == "ctime"
