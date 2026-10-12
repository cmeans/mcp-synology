"""Tests for core/redaction.py (#143): secrets in URLs never reach tool output or logs.

The passkeys here are made up; they only have the shape of real ones.
"""

from __future__ import annotations

import json

import pytest

from mcp_synology.core.redaction import redact_title, redact_url, redact_urls

PASSKEY = "a1B2c3D4e5F6g7H8i9J0"


class TestRedactUrl:
    def test_passkey_path_segment_is_masked(self) -> None:
        url = f"https://tracker.example/announce/{PASSKEY}"
        assert redact_url(url) == "https://tracker.example/announce/***"

    def test_passkey_before_announce_is_masked(self) -> None:
        url = f"http://tracker.example:2710/{PASSKEY}/announce"
        assert redact_url(url) == "http://tracker.example:2710/***/announce"

    def test_query_values_are_masked_and_keys_kept(self) -> None:
        url = f"https://tracker.example/announce.php?passkey={PASSKEY}&uid=42"
        assert redact_url(url) == "https://tracker.example/announce.php?passkey=***&uid=***"

    def test_userinfo_is_masked(self) -> None:
        url = "ftp://alice:s3cret@files.example/pub/ubuntu-24.04.iso"
        assert redact_url(url) == "ftp://***@files.example/pub/ubuntu-24.04.iso"

    def test_fragment_is_dropped(self) -> None:
        assert redact_url("https://h.example/f.iso#token=abc") == "https://h.example/f.iso"

    @pytest.mark.parametrize(
        "url",
        [
            "udp://tracker.opentrackr.org:1337/announce",
            "https://releases.example/ubuntu-24.04.1-desktop-amd64.iso",
            "http://h.example/a/b/some_long_file_name.mkv",
            "https://h.example/abcdefghijklmnopqrstuvwxyz",  # long but no digits
            "https://h.example/12345678901234567890",  # long but no letters
        ],
    )
    def test_ordinary_urls_are_unchanged(self, url: str) -> None:
        assert redact_url(url) == url

    def test_magnet_keeps_hash_and_name_and_redacts_trackers(self) -> None:
        magnet = (
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Some+Show"
            f"&tr=https%3A%2F%2Ftracker.example%2Fannounce%2F{PASSKEY}"
            "&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
        )
        result = redact_url(magnet)
        assert PASSKEY not in result
        assert "xt=urn:btih:0123456789abcdef0123456789abcdef01234567" in result
        assert "dn=Some+Show" in result
        assert "tracker.example" in result
        assert "tracker.opentrackr.org" in result

    def test_magnet_web_seed_is_redacted(self) -> None:
        magnet = f"magnet:?xt=urn:btih:{'0' * 40}&ws=https%3A%2F%2Fu%3Ap%40seed.example%2Ff"
        result = redact_url(magnet)
        assert "u%3Ap" not in result and "u:p@" not in result

    def test_empty_and_bare_query_items(self) -> None:
        assert redact_url("https://h.example/a?x=1&&token") == "https://h.example/a?x=***&***"

    def test_unparseable_url_is_fully_masked(self) -> None:
        assert redact_url("http://[::1/announce") == "***"

    def test_url_without_host_is_unchanged(self) -> None:
        assert redact_url("file:///volume1/x.torrent") == "file:///volume1/x.torrent"

    @pytest.mark.parametrize("text", ["Some Show (2024) [1080p]", "", "—", "not a url"])
    def test_non_urls_are_unchanged(self, text: str) -> None:
        assert redact_url(text) == text

    def test_unencoded_tracker_query_in_magnet_is_masked(self) -> None:
        # #144 QA: split on "&", "passkey=" became a top-level magnet key and leaked.
        magnet = (
            f"magnet:?xt=urn:btih:{'0' * 40}&dn=Show"
            f"&tr=https://t.example/announce.php?uid=12&passkey={PASSKEY}"
        )
        result = redact_url(magnet)
        assert PASSKEY not in result
        assert "passkey=***" in result
        assert f"xt=urn:btih:{'0' * 40}" in result and "dn=Show" in result

    def test_magnet_keeps_only_known_safe_keys(self) -> None:
        magnet = f"magnet:?xt=urn:btih:{'0' * 40}&xl=1024&kt=show&x.pe=198.51.100.7:6881&so=0"
        assert redact_url(magnet) == (
            f"magnet:?xt=urn:btih:{'0' * 40}&xl=1024&kt=show&x.pe=***&so=***"
        )

    def test_token_inside_a_path_segment_is_masked(self) -> None:
        url = f"https://t.example/rss/{PASSKEY}.torrent"
        assert redact_url(url) == "https://t.example/rss/***.torrent"

    def test_path_parameter_values_are_masked(self) -> None:
        url = f"https://h.example/dl/file.iso;jsessionid={PASSKEY}"
        assert redact_url(url) == "https://h.example/dl/file.iso;jsessionid=***"

    def test_separated_token_is_masked(self) -> None:
        # base64url tokens carry - and _, but keep long alphanumeric runs.
        assert redact_url("https://h.example/dl/Ab3dE5fG7hI9jK1lM_x") == "https://h.example/dl/***"

    def test_uuid_segment_is_masked(self) -> None:
        url = "https://h.example/dl/550e8400-e29b-41d4-a716-446655440000/file.iso"
        assert redact_url(url) == "https://h.example/dl/***/file.iso"

    @pytest.mark.parametrize(
        "url",
        [
            "https://h.example/ubuntu-24-04-desktop-amd64",
            "https://h.example/v/Cleddau_Series_1_2026_1080p_WEB.mkv",
        ],
    )
    def test_names_made_of_short_words_are_kept(self, url: str) -> None:
        assert redact_url(url) == url


class TestRedactUrls:
    def test_comma_separated_v1_uri_list(self) -> None:
        text = f"https://a.example/x.iso,https://t.example/announce/{PASSKEY}"
        assert redact_urls(text) == "https://a.example/x.iso,https://t.example/announce/***"

    def test_json_encoded_ds2_url_list(self) -> None:
        text = json.dumps([f"https://t.example/dl?key={PASSKEY}", "magnet:?xt=urn:btih:abc"])
        result = redact_urls(text)
        assert PASSKEY not in result
        assert json.loads(result) == ["https://t.example/dl?key=***", "magnet:?xt=urn:btih:abc"]

    def test_text_without_urls_is_unchanged(self) -> None:
        assert redact_urls("Created 2 task(s).") == "Created 2 task(s)."


class TestRedactTitle:
    """DSM titles an HTTP task with the URL's tail, query string included (#144)."""

    def test_query_tail_from_a_url_title_is_masked(self) -> None:
        # Seen live on vdsm: a FileStation download link titled with its session id.
        assert redact_title(f"ds-source.bin&_sid={PASSKEY}") == "ds-source.bin&_sid=***"

    def test_question_mark_query_is_masked(self) -> None:
        assert redact_title(f"file.iso?token={PASSKEY}&x=1") == "file.iso?token=***&x=***"

    def test_path_parameter_in_title_is_masked(self) -> None:
        assert redact_title(f"file.bin;jsessionid={PASSKEY}") == "file.bin;jsessionid=***"

    def test_full_url_title_is_redacted(self) -> None:
        title = f"https://t.example/dl/{PASSKEY}/file.iso"
        assert redact_title(title) == "https://t.example/dl/***/file.iso"

    @pytest.mark.parametrize(
        "title",
        [
            "Cleddau Series 1 (2026) 1080p",
            "Tom & Jerry (1940)",
            "Mrs Biggs  (2012) 720p",
            "Cleddau_Series_1_2026_1080p_WEB",  # token-shaped, but a name: kept
            "a=b c",
        ],
    )
    def test_ordinary_titles_are_unchanged(self, title: str) -> None:
        assert redact_title(title) == title
