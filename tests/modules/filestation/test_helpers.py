"""Tests for modules/filestation/helpers.py — path, size, polling, icons."""

from __future__ import annotations

import pytest

from mcp_synology.modules.filestation.helpers import (
    escape_multi_path,
    file_type_icon,
    matches_pattern,
    normalize_path,
    parse_human_size,
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


class TestMatchesPattern:
    def test_glob_match(self) -> None:
        assert matches_pattern("Severance.S02E10.mkv", "*.mkv")

    def test_glob_no_match(self) -> None:
        assert not matches_pattern("Severance.S02E10.mkv", "*.srt")

    def test_case_insensitive(self) -> None:
        assert matches_pattern("FILE.MKV", "*.mkv")

    def test_wildcard(self) -> None:
        assert matches_pattern("Severance.S02E10.mkv", "*Severance*")
