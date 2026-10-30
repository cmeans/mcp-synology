"""Tests for modules/filestation/helpers.py — path, size, polling, icons."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import SynologyError
from mcp_synology.core.formatting import format_size
from mcp_synology.modules.filestation.helpers import (
    correct_recycle_status_from_observation,
    ensure_known_shares,
    ensure_recycle_status,
    escape_multi_path,
    file_type_icon,
    matches_pattern,
    normalize_path,
    parse_human_size,
    parse_mtime,
    validate_additional,
    validate_share_path,
)
from tests.conftest import BASE_URL

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient


class TestNormalizePath:
    def test_adds_leading_slash(self) -> None:
        assert normalize_path("video/test") == "/video/test"

    def test_preserves_leading_slash(self) -> None:
        assert normalize_path("/video/test") == "/video/test"

    def test_strips_trailing_slash(self) -> None:
        assert normalize_path("/video/test/") == "/video/test"

    def test_root_path(self) -> None:
        assert normalize_path("/") == "/"

    def test_strips_whitespace(self) -> None:
        assert normalize_path("  /video  ") == "/video"


class TestValidateSharePath:
    def test_valid_share(self) -> None:
        result = validate_share_path("/video/test", {"video", "music"})
        assert result is None

    def test_unknown_share(self) -> None:
        result = validate_share_path("/unknown/test", {"video", "music"})
        assert result is not None
        assert "Unknown" in result
        assert "video" in result

    def test_empty_path(self) -> None:
        result = validate_share_path("/", {"video"})
        assert result is not None

    def test_recycle_path_rejected(self) -> None:
        result = validate_share_path("/#recycle/test", {"video"})
        assert result is not None
        assert "#recycle" in result

    def test_no_shares(self) -> None:
        result = validate_share_path("/video/test", set())
        assert result is not None
        assert "none" in result


class TestParseHumanSize:
    def test_integer_passthrough(self) -> None:
        assert parse_human_size(1024) == 1024

    def test_string_integer(self) -> None:
        assert parse_human_size("1024") == 1024

    def test_bytes(self) -> None:
        assert parse_human_size("500B") == 500

    def test_kilobytes(self) -> None:
        assert parse_human_size("1KB") == 1024

    def test_megabytes(self) -> None:
        assert parse_human_size("500MB") == 500 * 1024**2

    def test_gigabytes(self) -> None:
        assert parse_human_size("2GB") == 2 * 1024**3

    def test_terabytes(self) -> None:
        assert parse_human_size("1.5TB") == int(1.5 * 1024**4)

    def test_case_insensitive(self) -> None:
        assert parse_human_size("500mb") == 500 * 1024**2
        assert parse_human_size("500Mb") == 500 * 1024**2

    def test_decimal_values(self) -> None:
        assert parse_human_size("1.5GB") == int(1.5 * 1024**3)

    def test_with_spaces(self) -> None:
        assert parse_human_size("  500 MB  ") == 500 * 1024**2

    def test_binary_unit_names(self) -> None:
        assert parse_human_size("1KiB") == 1024
        assert parse_human_size("500MiB") == 500 * 1024**2
        assert parse_human_size("2GiB") == 2 * 1024**3
        assert parse_human_size("1.5TiB") == int(1.5 * 1024**4)
        assert parse_human_size("500mib") == 500 * 1024**2

    def test_kb_and_kib_mean_the_same(self) -> None:
        assert parse_human_size("500MB") == parse_human_size("500MiB")

    def test_formatted_size_parses_back(self) -> None:
        # What the tools print (format_size) is accepted as input unchanged.
        for size in (0, 1023, 1536 * 1024**2, int(1.5 * 1024**4), 1024**5, 3 * 1024**5, 1024**6):
            assert parse_human_size(format_size(size)) == size

    def test_invalid_binary_unit(self) -> None:
        for bad in ("500iB", "500KiiB", "500KBi"):
            with pytest.raises(ValueError, match="Invalid size"):
                parse_human_size(bad)

    def test_invalid_input(self) -> None:
        with pytest.raises(ValueError, match="Invalid size"):
            parse_human_size("not_a_size")

    def test_invalid_unit(self) -> None:
        with pytest.raises(ValueError, match="Invalid size"):
            parse_human_size("500EB")


class TestParseMtime:
    def test_calendar_date_treated_as_utc(self) -> None:
        # 2026-04-01 00:00:00 UTC = epoch 1775001600
        assert parse_mtime("2026-04-01") == 1775001600

    def test_iso_8601_with_offset(self) -> None:
        # 2026-04-01 12:00:00 UTC = epoch 1775044800
        assert parse_mtime("2026-04-01T12:00:00+00:00") == 1775044800

    def test_iso_8601_with_non_utc_offset(self) -> None:
        # 2026-04-01 12:00:00 UTC-05:00 = 17:00:00 UTC = epoch 1775062800
        assert parse_mtime("2026-04-01T12:00:00-05:00") == 1775062800

    def test_naive_iso_8601_treated_as_utc(self) -> None:
        # No tzinfo → UTC. Same epoch as the +00:00 case.
        assert parse_mtime("2026-04-01T12:00:00") == 1775044800

    def test_numeric_epoch_string_passthrough(self) -> None:
        assert parse_mtime("1775044800") == 1775044800

    def test_strips_whitespace(self) -> None:
        assert parse_mtime("  2026-04-01  ") == 1775001600

    def test_invalid_input(self) -> None:
        with pytest.raises(ValueError, match="Invalid mtime"):
            parse_mtime("not_a_date")

    def test_empty_string(self) -> None:
        with pytest.raises(ValueError, match="Invalid mtime"):
            parse_mtime("")


class TestFileTypeIcon:
    def test_directory_emoji(self) -> None:
        assert file_type_icon(True) == "\U0001f4c1"

    def test_directory_text(self) -> None:
        assert file_type_icon(True, style="text") == "[DIR]"

    def test_video_emoji(self) -> None:
        assert file_type_icon(False, "movie.mkv") == "\U0001f3ac"

    def test_video_text(self) -> None:
        assert file_type_icon(False, "movie.mp4", style="text") == "[VIDEO]"

    def test_generic_file_emoji(self) -> None:
        assert file_type_icon(False, "readme.txt") == "\U0001f4c4"

    def test_generic_file_text(self) -> None:
        assert file_type_icon(False, "readme.txt", style="text") == "[FILE]"


class TestEscapeMultiPath:
    def test_single(self) -> None:
        assert escape_multi_path(["/video/test"]) == "/video/test"

    def test_multiple(self) -> None:
        result = escape_multi_path(["/video/a", "/music/b"])
        assert result == "/video/a,/music/b"

    def test_comma_escape(self) -> None:
        result = escape_multi_path(["/video/a,b"])
        assert result == "/video/a\\,b"


class TestValidateAdditional:
    """Closes #41: `additional` field whitelist enforcement.

    DSM silently accepts unknown values (the field just doesn't appear in the
    response), so a typo like `"sze"` would never produce a visible error.
    `validate_additional` rejects unknown values up-front with a clear
    ToolError naming the bad value and listing the supported set.
    """

    @pytest.mark.parametrize(
        "value",
        [
            None,
            [],
            ["size"],
            ["size", "time"],
            ["real_path", "size", "owner", "perm"],
            ["mount_point_type"],
            ["type"],
        ],
    )
    def test_accepts_valid_or_empty(self, value: list[str] | None) -> None:
        validate_additional(value, tool_name="Get file info")  # no exception

    @pytest.mark.parametrize(
        "value,bad",
        [
            (["sze"], "sze"),
            (["size", "tme"], "tme"),
            (["mount_point_type", "junk"], "junk"),
            (["", "size"], ""),
            # get_file_info never shows volume_status (only list_share returns it),
            # so it's rejected rather than silently dropped (QA R1 on #240).
            (["volume_status"], "volume_status"),
        ],
    )
    def test_rejects_unknown(self, value: list[str], bad: str) -> None:
        with pytest.raises(ToolError) as exc:
            validate_additional(value, tool_name="Get file info")
        body = str(exc.value)
        assert "Get file info failed" in body
        assert bad in body

    def test_suggestion_lists_exactly_what_get_file_info_shows(self) -> None:
        import json

        with pytest.raises(ToolError) as exc:
            validate_additional(["bogus"], tool_name="Get file info")
        suggestion = json.loads(str(exc.value))["error"]["suggestion"]
        assert suggestion == (
            "Supported fields: mount_point_type, owner, perm, real_path, size, time, type."
        )


class TestMatchesPattern:
    def test_glob_match(self) -> None:
        assert matches_pattern("Severance.S02E10.mkv", "*.mkv")

    def test_glob_no_match(self) -> None:
        assert not matches_pattern("Severance.S02E10.mkv", "*.srt")

    def test_case_insensitive(self) -> None:
        assert matches_pattern("FILE.MKV", "*.mkv")

    def test_wildcard(self) -> None:
        assert matches_pattern("Severance.S02E10.mkv", "*Severance*")


# ---------- Recycle-bin probe + self-correct (closes #37) ----------


class TestEnsureRecycleStatus:
    """Lazy per-share recycle-bin probe with caching."""

    @pytest.mark.asyncio
    async def test_returns_cached_value_without_probing(self) -> None:
        client = AsyncMock()
        recycle_status = {"video": True, "music": False}

        result_video = await ensure_recycle_status(client, "video", recycle_status)
        result_music = await ensure_recycle_status(client, "music", recycle_status)

        assert result_video is True
        assert result_music is False
        client.request.assert_not_called()  # cache hit; no probe

    @pytest.mark.asyncio
    async def test_probe_success_caches_true(self) -> None:
        client = AsyncMock()
        client.request.return_value = {"files": []}  # any non-error response
        recycle_status: dict[str, bool] = {}

        result = await ensure_recycle_status(client, "video", recycle_status)

        assert result is True
        assert recycle_status == {"video": True}
        client.request.assert_awaited_once_with(
            "SYNO.FileStation.List",
            "list",
            params={"folder_path": "/video/#recycle", "limit": 1},
        )

    @pytest.mark.asyncio
    async def test_missing_bin_folder_is_off_but_not_cached(self) -> None:
        """#409: no #recycle, and the share's setting is admin-only (105): off for this
        call, checked again next time, since DSM re-creates #recycle at the next delete."""
        client = AsyncMock()
        client.request.side_effect = [
            SynologyError("not found", code=408),
            SynologyError("permission denied", code=105),
        ]
        recycle_status: dict[str, bool] = {}

        result = await ensure_recycle_status(client, "scratch", recycle_status)

        assert result is False
        assert recycle_status == {}
        assert client.request.await_args_list[1].args == ("SYNO.Core.Share", "list")
        assert client.request.await_args_list[1].kwargs == {
            "params": {"shareType": "all", "additional": '["recyclebin"]'}
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled", [True, False])
    async def test_missing_bin_folder_uses_the_share_setting(self, enabled: bool) -> None:
        """An admin reads the share's own setting: a definite answer, cached."""
        client = AsyncMock()
        shares = {
            "shares": [
                {"name": "other", "enable_recycle_bin": not enabled},
                {"name": "scratch", "enable_recycle_bin": enabled},
            ]
        }
        client.request.side_effect = [SynologyError("not found", code=408), shares]
        recycle_status: dict[str, bool] = {}

        assert await ensure_recycle_status(client, "scratch", recycle_status) is enabled
        assert recycle_status == {"scratch": enabled}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled", [True, False])
    async def test_home_uses_the_user_home_setting(self, enabled: bool) -> None:
        """/home's bin is the user home service's setting, which the `homes` share's own
        flag can contradict (vdsm). A new account's home has no #recycle yet."""
        client = AsyncMock()
        home = {"enable": True, "enable_recycle_bin": enabled}
        client.request.side_effect = [SynologyError("not found", code=408), home]
        recycle_status: dict[str, bool] = {}

        assert await ensure_recycle_status(client, "home", recycle_status) is enabled
        assert recycle_status == {"home": enabled}
        assert client.request.await_args_list[1].args == ("SYNO.Core.User.Home", "get")

    @pytest.mark.asyncio
    async def test_share_not_in_the_setting_list_is_not_cached(self) -> None:
        client = AsyncMock()
        client.request.side_effect = [
            SynologyError("not found", code=408),
            {"shares": [{"name": "other", "enable_recycle_bin": True}]},
        ]
        recycle_status: dict[str, bool] = {}

        assert await ensure_recycle_status(client, "scratch", recycle_status) is False
        assert recycle_status == {}

    @pytest.mark.asyncio
    async def test_probe_permission_denied_answers_true_uncached_with_warning(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("mcp_synology.modules.filestation.helpers._warned_recycle", set())
        client = AsyncMock()
        client.request.side_effect = SynologyError("permission denied", code=105)
        recycle_status: dict[str, bool] = {}

        with caplog.at_level(logging.WARNING, logger="mcp_synology.modules.filestation.helpers"):
            result = await ensure_recycle_status(client, "admin_only", recycle_status)

        assert result is True
        assert recycle_status == {}  # can't tell: checked again next time (#255)
        assert any(
            "Recycle-bin probe on /admin_only" in r.getMessage()
            and "DSM 105 (permission denied)" in r.getMessage()
            and "Grant the MCP service account" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_probe_unknown_error_answers_true_uncached_with_warning(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("mcp_synology.modules.filestation.helpers._warned_recycle", set())
        client = AsyncMock()
        client.request.side_effect = SynologyError("server confused", code=500)
        recycle_status: dict[str, bool] = {}

        with caplog.at_level(logging.WARNING, logger="mcp_synology.modules.filestation.helpers"):
            result = await ensure_recycle_status(client, "weird", recycle_status)

        assert result is True
        assert recycle_status == {}
        assert any("DSM error 500" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_cant_tell_warns_once_per_share(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#255: an uncached share is probed on every use, so it warns only the first time."""
        monkeypatch.setattr("mcp_synology.modules.filestation.helpers._warned_recycle", set())
        client = AsyncMock()
        client.request.side_effect = SynologyError("permission denied", code=105)
        with caplog.at_level(logging.DEBUG, logger="mcp_synology.modules.filestation.helpers"):
            for _ in range(3):
                await ensure_recycle_status(client, "locked", {})
        probes = [r for r in caplog.records if "Recycle-bin probe on /locked" in r.getMessage()]
        assert [r.levelno for r in probes] == [logging.WARNING, logging.DEBUG, logging.DEBUG]
        assert client.request.await_count == 3

    @pytest.mark.asyncio
    async def test_memoization_second_call_skips_probe(self) -> None:
        """Second call against the same share must not re-issue the List request."""
        client = AsyncMock()
        client.request.return_value = {"files": []}
        recycle_status: dict[str, bool] = {}

        await ensure_recycle_status(client, "video", recycle_status)
        await ensure_recycle_status(client, "video", recycle_status)

        # Probe ran exactly once despite two ensure calls.
        client.request.assert_awaited_once()


class TestCorrectRecycleStatusFromObservation:
    """In-place cache update when DSM behavior contradicts the cached bool."""

    def test_bin_folder_missing_drops_a_cached_true_and_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#409: a missing #recycle doesn't prove the bin is off, so the share is checked
        again on next use instead of being cached as off."""
        recycle_status = {"video": True}
        with caplog.at_level(logging.INFO, logger="mcp_synology.modules.filestation.helpers"):
            correct_recycle_status_from_observation(
                "video", observed_enabled=False, recycle_status=recycle_status
            )
        assert recycle_status == {}
        assert any(
            "Self-correcting recycle-bin cache on /video" in r.getMessage() for r in caplog.records
        )

    def test_bin_folder_seen_turns_a_cached_false_on(self) -> None:
        recycle_status = {"video": False}
        correct_recycle_status_from_observation(
            "video", observed_enabled=True, recycle_status=recycle_status
        )
        assert recycle_status == {"video": True}

    def test_agreement_is_no_op(self, caplog: pytest.LogCaptureFixture) -> None:
        recycle_status = {"video": True}
        with caplog.at_level(logging.INFO, logger="mcp_synology.modules.filestation.helpers"):
            correct_recycle_status_from_observation(
                "video", observed_enabled=True, recycle_status=recycle_status
            )
        assert recycle_status == {"video": True}
        # No "Self-correcting" log when cache agrees.
        assert not any(
            "Self-correcting" in r.getMessage()
            for r in caplog.records
            if r.name == "mcp_synology.modules.filestation.helpers"
        )

    @pytest.mark.parametrize(("observed", "cached"), [(False, {}), (True, {"scratch": True})])
    def test_missing_key(self, observed: bool, cached: dict[str, bool]) -> None:
        recycle_status: dict[str, bool] = {}
        correct_recycle_status_from_observation(
            "scratch", observed_enabled=observed, recycle_status=recycle_status
        )
        assert recycle_status == cached


class TestWithListedShareName:
    """#353: the share is renamed to its listed spelling; anything else is left alone."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/home/a", "/home/a"),  # already listed
            ("/Home/a/b", "/home/a/b"),  # another case
            ("HOME", "/home"),  # no leading slash
            ("/other/a", "/other/a"),  # not a listed share: left for the caller
            ("/", "/"),
        ],
    )
    def test_listed_spelling(self, path: str, expected: str) -> None:
        from mcp_synology.modules.filestation.helpers import _with_listed_share_name

        assert _with_listed_share_name(path, {"home", "video"}) == expected


class TestEnsureKnownShares:
    """#160: validate the share name against the NAS's share list before any DSM call."""

    @staticmethod
    def _route(share_lists: list[list[str]], calls: list[str]) -> None:
        answers = iter(share_lists)

        def side_effect(request: httpx.Request) -> httpx.Response:
            method = dict(request.url.params).get("method", "")
            calls.append(method)
            if method == "list_share":
                names = next(answers)
                shares = [{"name": n, "path": f"/{n}"} for n in names]
                return httpx.Response(200, json={"success": True, "data": {"shares": shares}})
            return httpx.Response(200, json={"success": True, "data": {}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

    @respx.mock
    async def test_unknown_share_is_not_found_with_available_shares(
        self, mock_client: DsmClient
    ) -> None:
        calls: list[str] = []
        self._route([["testshare", "writable"], ["testshare", "writable"]], calls)
        with pytest.raises(ToolError) as exc_info:
            await ensure_known_shares(mock_client, ["/video/downloads/*"], set(), "path")
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "not_found"
        assert body["error"]["retryable"] is False
        assert "video" in body["error"]["message"]
        assert "testshare, writable" in body["error"]["message"]
        assert "list_shares" in body["error"]["message"]

    @respx.mock
    @pytest.mark.parametrize(
        "path",
        [
            "/writable/..",
            "/writable/../testshare/x",
            "/writable/.",
            "/writable/./",
            "writable/sub/../x",
            "/..",
            "/./writable",
        ],
    )
    async def test_dot_parts_are_refused_before_dsm(
        self, mock_client: DsmClient, path: str
    ) -> None:
        # #449: DSM refuses them too (418), but only after the share check, which read
        # just the first part. Nothing is sent, even when the share list is cached.
        calls: list[str] = []
        self._route([["testshare", "writable"]], calls)
        with pytest.raises(ToolError) as exc_info:
            await ensure_known_shares(mock_client, ["/testshare/ok", path], set(), "paths")
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "invalid_parameter"
        assert error["param"] == "paths"
        assert error["value"] == path
        assert error["message"] == f"Invalid path '{path}': '.' and '..' parts aren't allowed."
        assert calls == []

    @respx.mock
    @pytest.mark.parametrize(
        "path", ["/writable/.hidden", "/writable/a..b", "/writable/...", "/writable/x."]
    )
    async def test_dots_inside_a_name_pass(self, mock_client: DsmClient, path: str) -> None:
        calls: list[str] = []
        self._route([["testshare", "writable"]], calls)
        assert await ensure_known_shares(mock_client, [path], set(), "path") == [path]

    @respx.mock
    async def test_errors_name_each_paths_argument(self, mock_client: DsmClient) -> None:
        # copy_files and move_files check paths and dest_folder together: an error names
        # the one at fault (not_found used to say "path" for every argument).
        calls: list[str] = []
        self._route([["testshare", "writable"], ["testshare", "writable"]], calls)
        params = ["paths", "paths", "dest_folder"]
        with pytest.raises(ToolError) as exc_info:
            await ensure_known_shares(
                mock_client, ["/testshare/a", "/writable/b", "/writable/../x"], set(), params
            )
        assert json.loads(str(exc_info.value))["error"]["param"] == "dest_folder"
        with pytest.raises(ToolError) as exc_info:
            await ensure_known_shares(
                mock_client, ["/testshare/a", "/writable/b", "/nope/x"], set(), params
            )
        error = json.loads(str(exc_info.value))["error"]
        assert (error["code"], error["param"]) == ("not_found", "dest_folder")

    @respx.mock
    async def test_dot_parts_refused_when_shares_cannot_be_listed(
        self, mock_client: DsmClient
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 105}}
        )
        with pytest.raises(ToolError, match="'.' and '..' parts"):
            await ensure_known_shares(mock_client, ["/video/.."], set(), "path")

    @respx.mock
    async def test_known_share_passes_and_cache_is_reused(self, mock_client: DsmClient) -> None:
        calls: list[str] = []
        self._route([["testshare", "writable"]], calls)
        cache: set[str] = set()
        await ensure_known_shares(mock_client, ["/writable/a", "writable/b"], cache, "path")
        await ensure_known_shares(mock_client, ["/testshare/c"], cache, "path")
        assert cache == {"testshare", "writable"}
        assert calls.count("list_share") == 1

    @respx.mock
    async def test_new_share_found_after_one_refresh(self, mock_client: DsmClient) -> None:
        calls: list[str] = []
        self._route([["writable", "media"]], calls)
        cache = {"writable"}  # filled before "media" existed
        await ensure_known_shares(mock_client, ["/media/x"], cache, "path")
        assert "media" in cache
        assert calls.count("list_share") == 1

    @respx.mock
    async def test_share_list_failure_does_not_block(self, mock_client: DsmClient) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 105}}
        )
        cache: set[str] = set()
        await ensure_known_shares(
            mock_client, ["/whatever/x"], cache, "path"
        )  # DSM decides instead
        assert cache == set()

    @respx.mock
    async def test_no_paths_makes_no_request(self, mock_client: DsmClient) -> None:
        # e.g. upload_file with no dest_folder and no default_upload_dir configured
        calls: list[str] = []
        self._route([["writable"]], calls)
        cache: set[str] = set()
        assert await ensure_known_shares(mock_client, [], cache, "path") == []
        assert calls == [] and cache == set()

    @respx.mock
    async def test_share_names_match_case_insensitively(self, mock_client: DsmClient) -> None:
        """QA R1 (#173): DSM accepts /TESTSHARE/... for share "testshare" (DSM 7.2.2), so the
        check must too, without reloading the share list for a case difference."""
        calls: list[str] = []
        self._route([["testshare", "writable"]], calls)
        cache = {"testshare", "writable"}
        out = await ensure_known_shares(
            mock_client, ["/TESTSHARE/Documents", "/Writable/x"], cache, "path"
        )
        assert calls == []
        # Sent as listed (#353): DSM takes /TESTSHARE, but not /Home for the home service.
        assert out == ["/testshare/Documents", "/writable/x"]

    @respx.mock
    async def test_paths_come_back_with_the_listed_share_name(self, mock_client: DsmClient) -> None:
        """#353: /Home and /HOME get 408 where /home lists (vdsm 7.1 and 7.4.1, seska 7.1.1)."""
        calls: list[str] = []
        self._route([["home", "homes", "Backups"]], calls)
        out = await ensure_known_shares(
            mock_client,
            ["Home", "/HOME/docs/a.txt", "homes/x/", "/backups", "/Backups/y", "home/z"],
            set(),
            "paths",
        )
        # Changed only where the share name differs; anything else is left as given.
        assert out == ["/home", "/home/docs/a.txt", "homes/x/", "/Backups", "/Backups/y", "home/z"]

    @respx.mock
    async def test_paths_come_back_unchanged_when_dsm_wont_list(
        self, mock_client: DsmClient
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 105}}
        )
        assert await ensure_known_shares(mock_client, ["/Home/x"], set(), "path") == ["/Home/x"]

    @respx.mock
    async def test_recycle_as_share_is_rejected(self, mock_client: DsmClient) -> None:
        calls: list[str] = []
        self._route([["writable"]], calls)
        with pytest.raises(ToolError):
            await ensure_known_shares(mock_client, ["/#recycle/x"], {"writable"}, "path")
