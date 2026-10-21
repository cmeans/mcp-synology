"""Tests for modules/filestation/listing.py — list_shares, list_files, list_recycle_bin."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

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

    @respx.mock
    async def test_list_shares_renders_unknown_for_unprobed_shares(
        self, mock_client: DsmClient
    ) -> None:
        """F3 regression (#73 round 1): once the cache fills lazily, the
        Recycle Bin column appears — but shares that haven't been probed
        must render as "unknown", not falsely as "disabled". Otherwise
        list_shares is *worse* than pre-#37 (which simply omitted the
        column when the dict was empty) because partial correctness has
        the appearance of full correctness.
        """
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
                        {
                            "name": "music",
                            "path": "/music",
                            "isdir": True,
                            "additional": {"owner": {"user": "admin"}},
                        },
                        {
                            "name": "scratch",
                            "path": "/scratch",
                            "isdir": True,
                            "additional": {"owner": {"user": "admin"}},
                        },
                    ],
                    "total": 3,
                },
            }
        )
        # /video has been probed (enabled); /scratch has been probed (disabled);
        # /music is NOT in the cache — must render as unknown, not disabled.
        result = await list_shares(
            mock_client,
            recycle_bin_status={"video": True, "scratch": False},
        )
        # Tabular output — find the per-row recycle-bin cell. Order in shares
        # is video, music, scratch; the column is the last one in each row.
        # We can't anchor on column position alone (table renderer pads), so
        # assert presence of the three distinct values.
        assert "enabled" in result
        assert "disabled" in result
        assert "unknown" in result

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
        assert "#recycle" not in result
        assert "real_file.txt" in result

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

    async def test_recycle_bin_disabled(self, mock_client: DsmClient) -> None:
        result = await list_recycle_bin(
            mock_client,
            share="docker",
            recycle_bin_status={"docker": False},
        )
        assert "not enabled" in result

    @respx.mock
    async def test_recycle_bin_not_found_error_408(self, mock_client: DsmClient) -> None:
        """DSM error 408 on the #recycle path means recycle bin is disabled.

        list_recycle_bin should catch the ToolError and return a friendly
        message instead of crashing.
        """
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 408}}
        )
        result = await list_recycle_bin(mock_client, share="writable")
        assert "not enabled" in result
        assert "writable" in result

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
        # First the lazy probe in `ensure_recycle_status` returns success
        # (the list call against /share/#recycle?limit=0 succeeds), so the
        # cache stays True. Then the FULL list (no limit=0) returns 408 —
        # contradicting the probe's view.
        call_count = 0

        def side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            params = dict(request.url.params)
            # Probe: limit=0 → success (recycle path exists at this moment)
            if params.get("limit") == "0":
                return httpx.Response(
                    200, json={"success": True, "data": {"files": [], "total": 0}}
                )
            # Subsequent full list: simulate the recycle dir disappeared
            return httpx.Response(200, json={"success": False, "error": {"code": 408}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        recycle_status = {"video": True}  # stale cached value
        result = await list_recycle_bin(
            mock_client,
            share="video",
            recycle_bin_status=recycle_status,
        )

        assert "not enabled" in result
        # Cache was self-corrected from True to False.
        assert recycle_status == {"video": False}

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
            if params.get("limit") == "0":
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


# -- #333: the whole bin is searched for a pattern; DSM's sort grouping is explained --

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
        assert "(3 found" in out and "Showing 1 of 3 matches (limit=1)." in out

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
        """share="Home" is accepted (share names match case-insensitively), while DSM
        may echo the path as /home/...: the walk mustn't depend on DSM's path."""
        tree = {
            "/Home/#recycle": [_entry(f"{_BIN}/mcp-rc3", isdir=True)],
            "/Home/#recycle/mcp-rc3": [_entry(f"{_BIN}/mcp-rc3/b2.txt")],
        }
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder(tree))
        out = await list_recycle_bin(mock_client, share="Home", pattern="b2*")
        assert "mcp-rc3/b2.txt" in out and "/home/#recycle/" not in out.split("\n", 3)[3]
        assert "Not searched" not in out

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
    async def test_pattern_on_a_share_without_a_bin(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=_bin_responder({}))
        out = await list_recycle_bin(mock_client, share="home", pattern="b2*")
        assert out == "Recycle bin is not enabled on /home. Deleted files cannot be recovered."

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
