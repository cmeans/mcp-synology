"""Tests for modules/filestation/helpers.py — path, size, polling, icons."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_synology.core.errors import SynologyError
from mcp_synology.modules.filestation.helpers import (
    correct_recycle_status_from_observation,
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

    def test_invalid_input(self) -> None:
        with pytest.raises(ValueError, match="Invalid size"):
            parse_human_size("not_a_size")

    def test_invalid_unit(self) -> None:
        with pytest.raises(ValueError, match="Invalid size"):
            parse_human_size("500PB")


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
            ["mount_point_type", "volume_status"],
            ["type"],
        ],
    )
    def test_accepts_valid_or_empty(self, value: list[str] | None) -> None:
        validate_additional(value, tool_name="List shares")  # no exception

    @pytest.mark.parametrize(
        "value,bad",
        [
            (["sze"], "sze"),
            (["size", "tme"], "tme"),
            (["mount_point_type", "junk"], "junk"),
            (["", "size"], ""),
        ],
    )
    def test_rejects_unknown(self, value: list[str], bad: str) -> None:
        with pytest.raises(ToolError) as exc:
            validate_additional(value, tool_name="List shares")
        body = str(exc.value)
        assert "List shares failed" in body
        assert bad in body
        # Suggestion lists every supported field.
        for field in ("real_path", "size", "owner", "time", "perm", "type"):
            assert field in body

    def test_tool_name_appears_in_error(self) -> None:
        with pytest.raises(ToolError) as exc:
            validate_additional(["bogus"], tool_name="Search files")
        assert "Search files failed" in str(exc.value)


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
            params={"folder_path": "/video/#recycle", "limit": 0},
        )

    @pytest.mark.asyncio
    async def test_probe_dsm_408_caches_false(self) -> None:
        client = AsyncMock()
        client.request.side_effect = SynologyError("not found", code=408)
        recycle_status: dict[str, bool] = {}

        result = await ensure_recycle_status(client, "scratch", recycle_status)

        assert result is False
        assert recycle_status == {"scratch": False}

    @pytest.mark.asyncio
    async def test_probe_permission_denied_falls_back_to_true_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = AsyncMock()
        client.request.side_effect = SynologyError("permission denied", code=105)
        recycle_status: dict[str, bool] = {}

        with caplog.at_level(logging.WARNING, logger="mcp_synology.modules.filestation.helpers"):
            result = await ensure_recycle_status(client, "admin_only", recycle_status)

        assert result is True
        assert recycle_status == {"admin_only": True}
        assert any(
            "Recycle-bin probe on /admin_only" in r.getMessage()
            and "DSM 105 (permission denied)" in r.getMessage()
            and "Grant the MCP service account" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_probe_unknown_error_falls_back_to_true_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = AsyncMock()
        client.request.side_effect = SynologyError("server confused", code=500)
        recycle_status: dict[str, bool] = {}

        with caplog.at_level(logging.WARNING, logger="mcp_synology.modules.filestation.helpers"):
            result = await ensure_recycle_status(client, "weird", recycle_status)

        assert result is True
        assert recycle_status == {"weird": True}
        assert any("DSM error 500" in r.getMessage() for r in caplog.records)

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

    def test_disagreement_flips_cache_and_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        recycle_status = {"video": True}
        with caplog.at_level(logging.INFO, logger="mcp_synology.modules.filestation.helpers"):
            correct_recycle_status_from_observation(
                "video", observed_enabled=False, recycle_status=recycle_status
            )
        assert recycle_status["video"] is False
        assert any(
            "Self-correcting recycle-bin cache on /video" in r.getMessage() for r in caplog.records
        )

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

    def test_missing_key_sets_default_without_warning(self) -> None:
        recycle_status: dict[str, bool] = {}
        correct_recycle_status_from_observation(
            "scratch", observed_enabled=False, recycle_status=recycle_status
        )
        assert recycle_status == {"scratch": False}
