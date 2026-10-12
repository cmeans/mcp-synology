"""Tests for core/redaction.py (#143): secrets in URLs never reach tool output or logs.

The passkeys here are made up; they only have the shape of real ones.
"""

from __future__ import annotations

import json

import pytest

from mcp_synology.core.redaction import redact_url, redact_urls

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
