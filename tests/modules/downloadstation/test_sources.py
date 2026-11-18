"""Tests for modules/downloadstation/sources.py: create_download's local-source refusal (#648).

The refused spellings are the ones the vdsm probe saw Download Station fetch or accept on
DSM 7.1, 7.2.2, 7.3.2 and 7.4.1 (Awareness probe-648-download-station-internal-uris-2026-11-16),
plus the other forms an `inet_aton`-style parser, a magnet link or a wrapped link can take.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import error_from_code
from mcp_synology.modules.downloadstation.sources import refuse_local_sources
from mcp_synology.modules.downloadstation.tasks import create_download

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

NAS = "nas.example.org"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _thunder(url: str) -> str:
    return "thunder://" + _b64(f"AA{url}ZZ")


def _flashget(url: str) -> str:
    return "flashget://" + _b64(f"[FLASHGET]{url}[FLASHGET]") + "&abc123"


def _qqdl(url: str) -> str:
    return "qqdl://" + _b64(url)


# (URI, a phrase the refusal must hold)
REFUSED: list[tuple[str, str]] = [
    # Every spelling the probe saw Download Station fetch.
    ("http://127.0.0.1:5000/webapi/entry.cgi", "loopback"),
    ("http://localhost:5000/", "only a local network answers"),
    ("http://[::1]:5000/", "loopback"),
    ("http://2130706433:5000/", "loopback"),
    ("http://0x7f000001:5000/", "loopback"),
    ("http://127.1:5000/", "loopback"),
    ("http://0.0.0.0:5000/", "unspecified"),
    ("https://127.0.0.1:5001/", "loopback"),
    (_thunder("http://127.0.0.1:5000/"), "wraps a URL that names a loopback"),
    # Accepted by Download Station in the probe.
    ("http://169.254.169.254/latest/meta-data/", "link-local"),
    ("ftp://127.0.0.1/x", "loopback"),
    ("sftp://127.0.0.1/x", "loopback"),
    # Other spellings of an address.
    ("http://0177.0.0.1/", "loopback"),
    ("http://0x7f.1/", "loopback"),
    ("http://127.0.0.%31/", "loopback"),
    ("http://127.0.0.1./", "loopback"),
    ("HTTP://LOCALHOST/", "only a local network answers"),
    ("http://ｌｏｃａｌｈｏｓｔ/", "only a local network answers"),
    ("http://[::ffff:127.0.0.1]/", "loopback"),
    ("http://[::ffff:10.0.0.1]/", "local or private"),
    ("http://[64:ff9b::7f00:1]/", "loopback"),
    ("http://[2002:7f00:1::]/", "loopback"),
    ("http://[fe80::1%25eth0]/", "link-local"),
    ("http://[fd12:3456::1]/", "local or private"),
    ("http://[::]/", "unspecified"),
    # Private networks, CGNAT, multicast.
    ("http://10.1.2.3/", "local or private"),
    ("http://172.16.0.1/", "local or private"),
    ("http://192.168.1.10:8080/a.iso", "local or private"),
    ("http://100.64.0.1/", "local or private"),
    ("http://100.127.255.254/", "local or private"),
    ("http://224.0.0.1/", "multicast"),
    ("http://239.255.255.250/", "multicast"),
    # Names only a local network answers.
    ("http://nas/", "no domain"),
    ("http://printer.local/", "only a local network answers"),
    ("http://app.localhost/", "only a local network answers"),
    ("http://db.corp.internal/", "only a local network answers"),
    ("http://router.home.arpa/", "only a local network answers"),
    # The NAS's own host, however it's written.
    (f"https://{NAS}:5001/webapi/entry.cgi", "NAS itself"),
    ("https://NAS.Example.ORG./", "NAS itself"),
    # A userinfo or a backslash that hides the real host.
    ("http://example.com@127.0.0.1/", "loopback"),
    ("http://127.0.0.1\\@example.com/", "loopback"),
    ("http://example.com\\@127.0.0.1/", "loopback"),
    # Inside magnet links.
    ("magnet:?xt=urn:btih:abc&tr=udp://127.0.0.1:6969/announce", "tracker, seed or peer"),
    ("magnet:?xt=urn:btih:abc&tr.2=http://10.0.0.5/announce", "tracker, seed or peer"),
    ("magnet:?xt=urn:btih:abc&ws=http://192.168.0.2/file", "tracker, seed or peer"),
    ("magnet:?xt=urn:btih:abc&xs=http://localhost/a.torrent", "tracker, seed or peer"),
    ("magnet:?xt=urn:btih:abc&as=http://nas/a.iso", "tracker, seed or peer"),
    ("magnet:?xt=urn:btih:abc&x.pe=10.0.0.2:6881", "tracker, seed or peer"),
    ("magnet:?xt=urn:btih:abc&x.pe=[::1]:6881", "tracker, seed or peer"),
    ("magnet:?xt=urn:btih:abc&tr=udp%3A%2F%2F127.0.0.1%3A6969", "tracker, seed or peer"),
    # Wrapped links, and a magnet inside one.
    (_flashget("http://10.0.0.1/x.iso"), "wraps a URL"),
    (_qqdl("http://localhost/x.iso"), "wraps a URL"),
    (_thunder("magnet:?xt=urn:btih:abc&tr=udp://127.0.0.1:1"), "wraps a URL"),
    (_thunder(_qqdl("http://127.0.0.1/")), "wraps a URL"),
    ("thunder%3A%2F%2F" + _b64("AAhttp://127.0.0.1/ZZ"), "wraps a URL that names a loopback"),
    # ed2k servers and sources.
    ("ed2k://|server|192.168.0.2|4661|/", "local or private"),
    (
        "ed2k://|file|a.iso|100|0123456789abcdef0123456789abcdef|/|sources,127.0.0.1:4662|/",
        "loopback",
    ),
    # What can't be read is refused, not guessed at.
    ("thunder://!!!not-base64!!!", "can't be read"),
    ("thunder://" + base64.b64encode(b"\xff\xfe").decode(), "can't be read"),
    ("http:///no-host", "no host"),
    ("http://[::1/", "no host"),
    ("file:///etc/passwd", "no host"),
]

ALLOWED: list[str] = [
    "https://example.org/a.iso",
    "http://8.8.8.8/a.iso",
    "http://[2001:4860:4860::8888]/a.iso",
    "https://example.org/a,b.iso",
    "ftp://ftp.example.org/pub/x.iso",
    "magnet:?xt=urn:btih:abc&dn=name&tr=udp://tracker.example.org:6969/announce",
    "magnet:?xt=urn:btih:abc&tr.1=http://tracker.example.org/announce&x.pe=8.8.4.4:6881",
    _thunder("https://example.org/a.iso"),
    _flashget("https://example.org/a.iso"),
    _qqdl("https://example.org/a.iso"),
    "ed2k://|file|a.iso|100|0123456789abcdef0123456789abcdef|/",
    # Left to DSM, which refuses these with 1901.
    "plain text",
    "mailto:someone@example.org",
    "data:text/plain,hi",
    # A local-looking name only inside a public URL's path or query.
    "https://example.org/localhost/127.0.0.1?next=http://10.0.0.1/",
]


@pytest.mark.parametrize(("uri", "phrase"), REFUSED)
def test_a_local_source_is_refused(uri: str, phrase: str) -> None:
    with pytest.raises(ToolError) as exc:
        refuse_local_sources([uri], nas_host=NAS, allow_local_addresses=False)
    error = json.loads(str(exc.value))["error"]
    assert error["code"] == "invalid_parameter"
    assert error["param"] == "uri"
    assert error["retryable"] is False
    assert phrase in error["message"]
    assert "allow_local_addresses" in error["suggestion"]


@pytest.mark.parametrize("uri", ALLOWED)
def test_a_public_source_passes(uri: str) -> None:
    refuse_local_sources([uri], nas_host=NAS, allow_local_addresses=False)


@pytest.mark.parametrize(("uri", "phrase"), REFUSED)
def test_the_setting_lets_every_source_through(uri: str, phrase: str) -> None:
    refuse_local_sources([uri], nas_host=NAS, allow_local_addresses=True)


@pytest.mark.parametrize(
    ("nas_host", "uri"),
    [
        ("192.168.1.20", "http://192.168.1.20/"),
        ("8.8.8.8", "http://8.8.8.8/"),
        ("8.8.8.8", "http://134744072/"),
        ("seska.example.me", "https://SESKA.example.me:5001/"),
        ("2001:db8::5", "http://[2001:db8:0::5]/"),
    ],
)
def test_the_nas_itself_is_refused_in_any_spelling(nas_host: str, uri: str) -> None:
    with pytest.raises(ToolError, match="the NAS itself|local or private"):
        refuse_local_sources([uri], nas_host=nas_host, allow_local_addresses=False)


def test_no_nas_host_still_checks_addresses() -> None:
    with pytest.raises(ToolError, match="loopback"):
        refuse_local_sources(["http://127.0.0.1/"], nas_host=None, allow_local_addresses=False)
    refuse_local_sources(["https://example.org/"], nas_host=None, allow_local_addresses=False)


def test_the_refusal_names_the_uri_by_position_and_never_quotes_it() -> None:
    """`uri` is exempt from #604's argument check, so nothing of it is echoed."""
    secret = "http://user:hunter2@10.9.8.7:8080/secretpath\n[mcp-synology] forged"
    with pytest.raises(ToolError) as exc:
        refuse_local_sources(
            ["https://example.org/a.iso", secret, "https://example.org/b.iso"],
            nas_host=NAS,
            allow_local_addresses=False,
        )
    text = str(exc.value)
    assert "URI 2 of 3" in text
    for leak in ("hunter2", "10.9.8.7", "secretpath", "forged", "8080"):
        assert leak not in text
    assert "value" not in json.loads(text)["error"]


def test_a_single_uri_is_the_uri() -> None:
    with pytest.raises(ToolError, match="The URI names a loopback"):
        refuse_local_sources(["http://127.0.0.1/"], nas_host=None, allow_local_addresses=False)


def test_deeply_nested_wraps_are_refused() -> None:
    uri = "https://example.org/"
    for _ in range(6):
        uri = _qqdl(uri)
    with pytest.raises(ToolError, match="too deeply"):
        refuse_local_sources([uri], nas_host=None, allow_local_addresses=False)


class TestCreateDownloadRefusesBeforeAnyRequest:
    @respx.mock(assert_all_called=False)
    async def test_nothing_is_sent(self, mock_client: DsmClient) -> None:
        """respx has no routes: any DSM request (the default destination lookup, the
        create) would fail the test as an unmatched request."""
        with pytest.raises(ToolError, match="URI 2 of 2 names a loopback"):
            await create_download(
                mock_client,
                uri="https://example.org/a.iso,http://127.0.0.1:5000/",
                destination="writable",
                nas_host=NAS,
            )

    @respx.mock(assert_all_called=False)
    async def test_a_direct_call_is_refused_by_default(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError, match="loopback"):
            await create_download(mock_client, uri="http://127.0.0.1/", destination="writable")

    @respx.mock(assert_all_called=False)
    async def test_credentials_path_is_checked_too(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError, match="local or private"):
            await create_download(
                mock_client,
                uri="ftp://192.168.1.2/x.iso",
                destination="writable",
                username="u",
                password="p",
            )


def test_1901_names_the_sources_download_station_takes() -> None:
    error = error_from_code(1901, "SYNO.DownloadStation2.Task")
    assert "Unsupported source URI" in str(error)
    assert "magnet" in (error.suggestion or "")
