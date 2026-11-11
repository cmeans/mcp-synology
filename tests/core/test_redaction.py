"""Tests for core/redaction.py (#143): secrets in URLs never reach tool output or logs.

The passkeys here are made up; they only have the shape of real ones.
"""

from __future__ import annotations

import json
import re
import time

import pytest

from mcp_synology.core.redaction import redact_secrets, redact_title, redact_url, redact_urls

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

    def test_bare_magnet_item_is_masked(self) -> None:
        # A tracker URL pasted unencoded can leave a bare "&<passkey>" item with no "=".
        magnet = f"magnet:?xt=urn:btih:{'0' * 40}&dn=Show&{PASSKEY}"
        result = redact_url(magnet)
        assert PASSKEY not in result
        assert result == f"magnet:?xt=urn:btih:{'0' * 40}&dn=Show&***"

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

    def test_comma_inside_a_url_is_part_of_it(self) -> None:
        # #397: create_download keeps such a comma, so redaction must too; it used to
        # end the match and leave the query unmasked.
        assert redact_urls(f"https://h.example/a,b.iso?token={PASSKEY}") == (
            "https://h.example/a,b.iso?token=***"
        )
        assert json.loads(redact_urls(json.dumps([f"https://h/a,b?k={PASSKEY}"]))) == [
            "https://h/a,b?k=***"
        ]

    def test_v1_list_with_an_escaped_comma(self) -> None:
        text = f"https://h/a\\,b.iso?k={PASSKEY},magnet:?xt=urn:btih:abc"
        assert redact_urls(text) == "https://h/a\\,b.iso?k=***,magnet:?xt=urn:btih:abc"

    @pytest.mark.parametrize("prefix", ["MAGNET:?", "Magnet:?"])
    def test_uppercase_magnet_is_redacted(self, prefix: str) -> None:
        plain = f"{prefix}xt=urn:btih:abc&tr=http://t.example/announce/{PASSKEY}"
        encoded = f"{prefix}xt=urn:btih:abc&tr=http%3A%2F%2Ft.example%2Fa%2F{PASSKEY}"
        for text in (plain, encoded, f"see {plain}, then"):
            assert PASSKEY not in redact_urls(text), text
        assert redact_urls(plain).startswith(prefix)
        # A capitalized magnet after a comma starts a new URL, as in split_uris.
        assert redact_urls(f"https://h/a?k={PASSKEY},{prefix}xt=abc") == (
            f"https://h/a?k=***,{prefix}xt=abc"
        )

    def test_comma_after_a_url_in_prose_stays_outside(self) -> None:
        assert redact_urls(f"see https://h/x?k={PASSKEY}, then") == "see https://h/x?k=***, then"


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

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            (f"{PASSKEY}.torrent", "***.torrent"),  # private tracker's .torrent link (QA, #144)
            (f"{PASSKEY}.iso", "***.iso"),
            (f"file-{PASSKEY}.bin&_sid={PASSKEY}", "***.bin&_sid=***"),  # whole run, as in a path
            ("ubuntu-24.04-desktop-amd64.iso", "ubuntu-24.04-desktop-amd64.iso"),
        ],
    )
    def test_url_derived_title_masks_tokens(self, title: str, expected: str) -> None:
        # An HTTP/FTP/NZB task's title is the URL's last segment, so a token in it is
        # the same secret redact_url masks in the URI.
        assert redact_title(title, url_derived=True) == expected

    def test_bt_title_keeps_token_shaped_names(self) -> None:
        assert redact_title(f"{PASSKEY}.torrent") == f"{PASSKEY}.torrent"


# --- #514 PR 1: the shared adversarial table ---
#
# Each row is text holding a credential and that credential. Both
# `redact_urls` (the Download Station sites) and `redact_secrets` (the narrow pass for every
# sink) must mask it; `redact_urls` masks more, so only the credential's absence is shared.

CREDENTIAL_ROWS = [
    # URL userinfo with a character urlsplit or the URL match stops at (#458).
    pytest.param("https://user:CAN/ARY02@h.example/x", "CAN/ARY02", id="userinfo-slash"),
    pytest.param("https://user:CANARY#03@h.example/x", "CANARY#03", id="userinfo-hash"),
    pytest.param("https://user%3ACANARY04@h.example/x", "CANARY04", id="userinfo-percent-encoded"),
    pytest.param("https://user:p@ss:CANARY05@h.example/x", "p@ss:CANARY05", id="userinfo-with-at"),
    pytest.param(
        "error for http://admin:CANARY06@10.0.0.2:5000/webapi", "CANARY06", id="userinfo-in-prose"
    ),
    # Wrapped download links hide a whole URL (#458).
    pytest.param(
        "thunder://QUFodHRwOi8vdTpDQU5BUlkwN0BoL3haWg==",
        "QUFodHRwOi8vdTpDQU5BUlkwN0BoL3haWg",
        id="thunder",
    ),
    pytest.param(
        "Flashget://W0ZMQVNIR0VUXUNBTkFSWTA4W0ZMQVNIR0VUXQ==&1",
        "W0ZMQVNIR0VUXUNBTkFSWTA4W0ZMQVNIR0VUXQ",
        id="flashget",
    ),
    pytest.param("qqdl://Q0FOQVJZMDk=", "Q0FOQVJZMDk", id="qqdl"),
    # A comma followed by a scheme-like word splits a URL (#466).
    pytest.param("https://h/dl?files=a,b://c&token=CANARY10", "CANARY10", id="comma-scheme-split"),
    pytest.param("ftp://h/a,ED2K:foo?token=CANARY11", "CANARY11", id="comma-ed2k-split"),
    # Known credential parameters.
    pytest.param("https://h/entry.cgi?api=x&passwd=CANARY12", "CANARY12", id="passwd-param"),
    pytest.param("https://h/entry.cgi?_sid=CANARY13&api=x", "CANARY13", id="sid-param"),
    pytest.param("https://h/x?SynoToken=CANARY14", "CANARY14", id="synotoken-param"),
    pytest.param(
        "https://tracker.example/announce?passkey=CANARY15", "CANARY15", id="passkey-param"
    ),
    # Userinfo longer than the scan's 256-character window: a long token as the password.
    pytest.param(
        "https://user:eyJ" + "a" * 300 + "CANARY16@nas.example/x",
        "aaaaCANARY16",
        id="long-userinfo",
    ),
]


@pytest.mark.parametrize(("text", "secret"), CREDENTIAL_ROWS)
@pytest.mark.parametrize("redact", [redact_urls, redact_secrets], ids=["urls", "secrets"])
def test_a_credential_never_survives(redact: object, text: str, secret: str) -> None:
    assert callable(redact)
    assert secret in text
    out = redact(text)
    assert secret not in out, out
    # Nor any part of a password split by a character the pass might stop at.
    assert not any(part in out for part in re.split(r"[\s/#@:]", secret) if len(part) > 3), out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://user:CAN/ARY02@h.example/x", "https://***@h.example/x"),
        ("thunder://QUFodHRw", "thunder://***"),
        ("qqdl://Q0FOQVJZMDk=", "qqdl://***"),
        ("POST entry.cgi?passwd=CANARY12&account=me", "POST entry.cgi?passwd=***&account=me"),
        ('{"passwd": "CANARY", "account": "me"}', '{"passwd": "***", "account": "me"}'),
        ('{"SynoToken":"CA\\"NARY"}', '{"SynoToken":"***"}'),
        ("&_sid=CANARY&x=1", "&_sid=***&x=1"),
        ("https://h/x?token=CANARY&access_token=C2", "https://h/x?token=***&access_token=***"),
    ],
)
def test_redact_secrets_masks_only_the_credential(text: str, expected: str) -> None:
    assert redact_secrets(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # A Photos share link: its passphrase is what the user asked for (#514 Q1).
        "https://nas.example:5001/mo/sharing/aB3xY9kQ2mN7pL4r",
        # File names, task IDs, plain paths and other replies' text.
        "/video/Season.2024.1080p.WEBRip.x265-ABCDEFGH12345678.mkv",
        "Handle: `delete:FileStation_Delete_1a2b3c4d5e6f7a8b`",
        "dbid_4127 finished",
        # An `@` in a URL's path, a port before a path, an email address in prose.
        "see https://medium.com/@someone/post and https://h.example:8080/@x",
        "contact bob@example.com or http://a.example/x and mail me: bob@c.example",
        # Query values that aren't credentials, and a word that ends in a key's name.
        "https://h.example/dl?file=a.iso&insid=1&passwordless=true",
        "Tom & Jerry: sid and nancy",
        # A key that starts a path segment is a file name (#514 PR 1, decided in pre-QA r3).
        "/share/token=abc/sub/file.txt",
        "| /docs/sid=1/report.pdf |",
        "/share/my-token=abc/file.txt",
        "/share/v1.token=abc/file.txt",
        "/home/x/.token=abc/y",
        "",
    ],
)
def test_redact_secrets_keeps_what_isnt_a_credential(text: str) -> None:
    assert redact_secrets(text) == text


def test_redact_url_masks_a_query_glued_to_the_host() -> None:
    """After a comma split a URL, its query can land after the host (#466)."""
    assert redact_url("b://c&token=CANARY") == "b://c&token=***"
    assert redact_url("https://h;sid=CANARY/a") == "https://h;sid=***/a"
    assert redact_url("https://h.example:5001/x") == "https://h.example:5001/x"
    assert redact_url("http://[::1]:5000/x") == "http://[::1]:5000/x"


def test_an_opaque_link_has_its_query_masked() -> None:
    assert redact_url("ed2k:foo?token=CANARY&x=1") == "ed2k:foo?token=***&x=***"
    # A fragment is dropped, as a `://` URL's is.
    assert redact_url("ed2k:foo#token=CANARY") == "ed2k:foo"
    assert redact_url("not a url?x=1") == "not a url?x=1"
    # Only ed2k is opaque: other text with a colon is returned as it is.
    assert redact_url("Note:Track#1") == "Note:Track#1"
    assert redact_url("C:\\a?b") == "C:\\a?b"


# Inputs that took seconds with unbounded patterns (#465: 60,000 letters before a `://` took
# 2.9 s), or that could if a pattern were unbounded: each must finish well inside a bound that
# allows for a loaded CI runner.
LONG_INPUTS = [
    pytest.param("a" * 60_000 + "://", id="letters-then-scheme"),
    pytest.param("a+" * 30_000, id="scheme-characters"),
    pytest.param("://@" * 200_000, id="scheme-at-runs"),
    pytest.param("cookie: " * 7_500, id="cookie-runs"),
    pytest.param("x://a" + ",a" * 30_000, id="commas"),
    pytest.param("x://a" + (", " + " " * 40) * 1_500, id="spaces-after-commas"),
    pytest.param('"passwd": "' + "a" * 60_000, id="unterminated-json"),
    pytest.param("?passwd=" + "a" * 60_000, id="long-parameter"),
    pytest.param("https://u:p@h/x?a=1 " * 5_000, id="many-urls"),
    pytest.param("https://h/" + "@" * 60_000, id="at-run"),
    pytest.param(("https://a@" + "b@" * 60 + " ") * 500, id="ats-after-userinfo"),
    pytest.param("https://u:" + "p@" * 60_000 + "h/x", id="one-long-password-of-ats"),
]


@pytest.mark.parametrize("text", LONG_INPUTS)
@pytest.mark.parametrize(
    "redact", [redact_urls, redact_secrets, redact_title], ids=["urls", "secrets", "title"]
)
def test_long_inputs_take_linear_time(redact: object, text: str) -> None:
    assert callable(redact)
    started = time.perf_counter()
    redact(text)
    assert time.perf_counter() - started < 1.0


# --- #514 PR 1, pre-QA: userinfo next to other `@`s, false positives, key forms ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The first `@` followed by a whole host ends userinfo; a later `@` keeps the host.
        ("https://u:pw@medium.com/@someone/post", "https://***@medium.com/@someone/post"),
        ("https://u:pw@h.example:8080/@x", "https://***@h.example:8080/@x"),
        (
            "see https://user:pw@h.example/a and mail bob@c.example",
            "see https://***@h.example/a and mail bob@c.example",
        ),
        # A password holding `@` ends at the `@` before the host.
        ("https://user:p@ss:CANARY05@h/x", "https://***@h/x"),
        # A URL holds no whitespace (RFC 3986): the scan ends at it, so prose after a host, a
        # port included, is never userinfo.
        ("https://us er:pw@h/x", "https://us er:pw@h/x"),
        (
            "Cannot reach https://nas.local:5001 now, ask admin@example.com",
            "Cannot reach https://nas.local:5001 now, ask admin@example.com",
        ),
        (
            "http://10.0.0.2:5000 timed out; mail it@corp.com",
            "http://10.0.0.2:5000 timed out; mail it@corp.com",
        ),
        ("https://h:5001, see a@b.c", "https://h:5001, see a@b.c"),
        # The scan ends where the URL does: a quote, `<`, `>`, or a comma before another URI
        # (pre-QA r5 on #514), so JSON and URI lists after a host aren't its userinfo.
        (
            '{"url":"https://nas.local:5001","owner":"admin@example.com"}',
            '{"url":"https://nas.local:5001","owner":"admin@example.com"}',
        ),
        (
            "https://mirror.example,magnet:?xt=urn:btih:abc&dn=Show@Group",
            "https://mirror.example,magnet:?xt=urn:btih:abc&dn=Show@Group",
        ),
        ("https://nas,ftp://u:p@h/x", "https://nas,ftp://***@h/x"),
        # A backslash before `@` reads as `/` to a browser: not userinfo (WHATWG).
        # A backslash before `@` (not a JSON escape) makes the host ambiguous (WHATWG reads it as
        # `/`): the whole authority is masked, never left as it is (QA round 1 on #680).
        ("https://evil.example\\@nas.local/x", "https://***/x"),
        ("https://evil.com:443\\@nas.local/", "https://***/"),
        ("https://evil.com:\\@nas.local/", "https://***/"),
        ("file://C:\\Users\\me@corp\\x", "file://***"),
        ("ftp://CORP\\jdoe:S3cr3tPw@ftp.example/x", "ftp://***/x"),
        # A JSON escape is one character of the user name (`json.dumps` of a URI list).
        (
            '["https://j\\u00f6rg:S3cr3tPw@nas.example/file.iso"]',
            '["https://***@nas.example/file.iso"]',
        ),
        # A port that's only a colon.
        ("https://u:p@h:/x", "https://***@h:/x"),
        # A comma inside a password is part of the URL.
        ("https://u:p,w@h/x", "https://***@h/x"),
        ("https://admin:p@ss,1@nas/x", "https://***@nas/x"),
        # A password holding `@` before an `@` in the path: the first whole host counts.
        ("https://u:p@x@h.example/@name", "https://***@h.example/@name"),
        (
            "See https://example.com. Note: write to bob@example.org",
            "See https://example.com. Note: write to bob@example.org",
        ),
        (
            "Cannot reach https://nas.local now: ask admin@example.com",
            "Cannot reach https://nas.local now: ask admin@example.com",
        ),
        # An IPv6 host's colons aren't userinfo's; a password before one is.
        ("https://[::1]:5001/photo/@name", "https://[::1]:5001/photo/@name"),
        ("https://u:pw@[::1]:5001/x", "https://***@[::1]:5001/x"),
        # A Windows drive isn't a user name.
        ("file://C:/Users/me@corp/x", "file://C:/Users/me@corp/x"),
        # Prose after a bare host: neither "nas.local, contact" nor another URL is userinfo.
        (
            "Failed to reach https://nas.local, contact: admin@example.com",
            "Failed to reach https://nas.local, contact: admin@example.com",
        ),
        ("http://nas.local then ftp://u:p@h/x", "http://nas.local then ftp://***@h/x"),
        # The scan stays on one line: a password can't span a line break.
        ("https://user:pw\nd@h/x", "https://user:pw\nd@h/x"),
    ],
)
def test_userinfo_ends_at_the_right_at(text: str, expected: str) -> None:
    assert redact_secrets(text) == expected
    assert redact_urls(text) == expected


def test_a_password_with_a_space_isnt_masked() -> None:
    """A known limit, kept open on #458: a URL can't hold a space (RFC 3986), and reading past
    one read prose as userinfo and showed the wrong host (pre-QA rounds 2 to 4 on #514)."""
    assert redact_secrets("https://user:pw d@h/x") == "https://user:pw d@h/x"


def test_a_password_holding_many_ats_is_still_masked() -> None:
    """The forward scan gives up after `_USERINFO_TRIES` `@`s; the fallback from the last `@`
    still finds the host."""
    assert redact_secrets("https://u:" + "p@" * 9 + "q@h/x") == "https://***@h/x"


@pytest.mark.parametrize(
    "text",
    [
        "https://h/dl/a,b://c/x&token=CANARY3",
        "ftp://h/a,ED2K:foo&token=CANARY2",
        # A scheme longer than the 32 characters a URL match takes: no split, the item is in
        # the path, and masked there.
        "ftp://h/a," + "b" * 40 + "://c&token=CANARY1",
    ],
)
def test_a_query_that_lost_its_question_mark_is_masked(text: str) -> None:
    """#466: after a comma split, `&key=value` lands in a path or an opaque link."""
    assert "CANARY" not in redact_urls(text)
    assert "CANARY" not in redact_secrets(text)


@pytest.mark.parametrize(
    "text",
    [
        # A file or prose that names a wrapped scheme isn't a link (pre-QA on #514).
        "/music/AC-DC/Thunder:Struck.mp3",
        "Weather: thunder: storms",
        "xthunder://QUFB",
        "AC_thunder:Struck",
        "Thunder:Live",
        "qqdl:Mix2024",
        "Rename failed: 'Thunder:Live' already exists",
        # `key=value` outside query or form syntax is a name (pre-QA r6 on #514).
        "sid=1.pdf",
        "Report password=draft.docx",
        "token=2024",
        "x-api-key=S",
        # A paging cursor and Dev's fixed device label aren't credentials (QA round 1 on #680).
        "?page_token=abc&next_token=d",
        "?device_name=MCPSynology",
        # ed2k without `//` is a link only right after a comma (pre-QA r7 on #514).
        "Not found: /music/My ed2k:Song#1.mp3",
        "/x/Best of ed2k:Hits?vol=2.mp3",
        # A cookie's `uid` isn't its `id`.
        "Cookie: lang=en; uid=1; x=1",
        "thunder:///x",
        # An IDN host is a host.
        "https://bücher.example/announce",
        # An ed2k link's `|file|name|size|hash|` is public, like a magnet's `xt`.
        "ed2k://|file|ubuntu-24.04.iso|5000000000|31D6CFE0D16AE931B73C59D7E0C089C0|/",
        # A bare host before prose punctuation.
        "(see https://nas.local)",
        "https://nas.local).",
        "https://nas.local:5001)",
        # An opaque scheme after a `/` is part of a path (a file name), not a link.
        "/music/ed2k:foo?x=1.mp3",
        # IPvFuture brackets hold no colon, and still aren't userinfo.
        "https://[v1.fe80]/@name",
    ],
)
def test_names_that_look_like_links_are_kept(text: str) -> None:
    assert redact_secrets(text) == text
    assert redact_urls(text) == text


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Without `//` it isn't a link (real Thunder, FlashGet and QQDL links always have one).
        ("qqdl:Q0FOQVJZMDk=", "qqdl:Q0FOQVJZMDk="),
        ("thunder://QUFodHRw", "thunder://***"),
        ("thunder://***", "thunder://***"),
        ("Thunder:Struck.mp3", "Thunder:Struck.mp3"),
        # Punctuation after a `//` link ends it (a sentence, a Markdown link).
        ("thunder://QUFodHRw=.", "thunder://***."),
        ("thunder://QUFodHRw=)", "thunder://***)"),
    ],
)
def test_redact_url_masks_a_wrapped_link_once(url: str, expected: str) -> None:
    assert redact_url(url) == expected
    assert redact_urls(f"uri={url}") == f"uri={expected}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{'passwd': 'CANARY'}", "{'passwd': '***'}"),
        ("?refresh_token=CANARY&x=1", "?refresh_token=***&x=1"),
        ("&user_password=CANARY", "&user_password=***"),
        ("X-SYNO-Token: CANARY", "X-SYNO-Token: ***"),
        # Longer than any cap: masked whole, not left as it is.
        ('{"token": "' + "A" * 5000 + 'TAIL"}', '{"token": "***"}'),
        # Unterminated, to the line's end.
        ('{"passwd": "CANARY', '{"passwd": "***'),
        # A number, decimals and exponents too.
        ('{"otp_code": 123456}', '{"otp_code": ***}'),
        ('{"passwd": 1.5e3}', '{"passwd": ***}'),
        # An unterminated value ending in an escaped quote keeps one quote.
        ('{"passwd": "a\\"', '{"passwd": "***'),
        # DSM's 2FA device token in a login reply, and the session as its `id` cookie.
        ('{"data": {"did": "DEVTOKEN", "sid": "S1"}}', '{"data": {"did": "***", "sid": "***"}}'),
        ("Cookie: lang=en; id=SESSION; x=1", "Cookie: lang=en; id=***; x=1"),
        # Every credential item of a cookie, in a header line, a dict repr or JSON (QA r1 on #680).
        ("Cookie: id=a; did=D1d", "Cookie: id=***; did=***"),
        ("{'Cookie': 'id=SEC'}", "{'Cookie': 'id=***'}"),
        ('{"Cookie": "id=SEC; did=D1d"}', '{"Cookie": "id=***; did=***"}'),
        ("Headers({'cookie': 'id=SEC'})", "Headers({'cookie': 'id=***'})"),
        # A quoted value however long, escaped JSON, wide spacing.
        ('?passwd="' + "a" * 5000 + 'TAIL"', '?passwd="***"'),
        ('{\\"passwd\\":\\"SEC\\"}', '{\\"passwd\\":\\"***\\"}'),
        ('{"passwd":      "SEC"}', '{"passwd":      "***"}'),
        ("X-SYNO-Token:      SEC", "X-SYNO-Token:      ***"),
        # A percent-encoded wrapped link, inside a URL's query too.
        ("thunder://QUFodHRw%2BOi8vdTpDQU5BUlk=", "thunder://***"),
        ("?u=thunder%3A%2F%2FQUFodHRw", "?u=thunder%3A%2F%2F***"),
        ("candid=1", "candid=1"),
        # A prefix joined by `-`.
        ("x?access-token=CANARY", "x?access-token=***"),
        ("?x-api-key=CANARY", "?x-api-key=***"),
        # A prefix of several parts (an STS session token in a presigned URL).
        ("?X-Amz-Security-Token=CANARY", "?X-Amz-Security-Token=***"),
        ("?user_refresh_token=CANARY", "?user_refresh_token=***"),
        # A header key with a prefix isn't DSM's header.
        ("my-x-syno-token: v", "my-x-syno-token: v"),
        # A password within the scan's window holding `/`, which the authority fallback can't
        # see past.
        ("https://u:" + "p/" * 50 + "@h/x", "https://***@h/x"),
        ("{'otp_code': 123456, 'x': 1}", "{'otp_code': ***, 'x': 1}"),
        # Quoted `key=value`, a repr of a password holding `'` (double quotes), bytes.
        ('&passwd="CANARY"', '&passwd="***"'),
        (";token='CANARY'", ";token='***'"),
        ('?password="CANARY"&x=1', '?password="***"&x=1'),
        ("""{'passwd': "it's"}""", """{'passwd': "***"}"""),
        ("{'passwd': b'x'}", "{'passwd': b'***'}"),
        # A wrapped link after punctuation that isn't part of a word.
        ("uri:thunder://QUFodHRw", "uri:thunder://***"),
        ("a;thunder://QUFodHRw", "a;thunder://***"),
        ("{thunder://QUFodHRw}", "{thunder://***}"),
        # A wrapped link in a sentence or a Markdown link.
        ("see thunder://QUFodHRw=.", "see thunder://***."),
        ("[link](thunder://QUFodHRw=)", "[link](thunder://***)"),
    ],
)
def test_more_credential_forms(text: str, expected: str) -> None:
    assert redact_secrets(text) == expected


def test_a_url_nested_in_a_query_has_its_own_userinfo_masked() -> None:
    """Each `://` in a match is scanned (pre-QA r6 on #514); `redact_urls` masks the whole
    query value anyway."""
    nested = "https://x/?next=https://u:CANARY@h/x"
    assert redact_secrets(nested) == "https://x/?next=https://***@h/x"
    assert "CANARY" not in redact_urls(nested)


def test_a_nested_url_with_long_userinfo_is_masked() -> None:
    """Past the scan's window, each `://`'s authority is masked to its last `@`, a nested URL's
    too (pre-QA r7 on #514)."""
    nested = "https://x/?next=https://user:eyJ" + "a" * 300 + "CANARY@h/x"
    assert redact_secrets(nested) == "https://x/?next=https://***@h/x"
