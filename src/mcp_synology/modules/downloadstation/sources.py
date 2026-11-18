"""create_download's sources: refuse those on the NAS itself or its local network (#648).

Download Station fetches a URI from the NAS, so a URI naming the NAS's own address, its
LAN or a cloud metadata address makes the NAS fetch it: a request the model's user may
never have meant, written by text the model read (a prompt injection). The vdsm probe
(DSM 7.1, 7.2.2, 7.3.2, 7.4.1; admin and non-admin) found Download Station fetches every
loopback spelling (`127.0.0.1`, `localhost`, `[::1]`, `2130706433`, `0x7f000001`,
`127.1`, `0.0.0.0`, DSM's own `https://127.0.0.1:5001`) and a `thunder://` link wrapping
one, and accepts `169.254.169.254` and `ftp://` / `sftp://` to loopback.

So, unless the module's ``allow_local_addresses`` setting is on, every source is checked
before anything is sent (OWASP's SSRF cheat sheet; GitLab and Nextcloud refuse local
addresses by default the same way):

- an IP address, in any spelling an `inet_aton`-style parser takes (`127.1`, `0x7f.1`,
  `2130706433`, `0177.0.0.1`) or IPv6 (an IPv4 address inside one too), that isn't
  globally routable: loopback, private, link-local, CGNAT `100.64/10`, unspecified,
  reserved, multicast, IPv6 unique-local;
- a name that only a local network answers: `localhost` and `*.localhost`, a name with
  no dot, `*.local`, `*.internal`, `*.home.arpa`, and the private names RFC 6762
  Appendix G lists (`*.lan`, `*.home`, `*.corp`, `*.private`, `*.intranet`) with
  `*.localdomain`;
- the NAS's own host from the config (`connection.host`), by that name or address;
- the same inside a magnet link (its trackers, web seeds, sources and peers: `tr`, `ws`,
  `xs`, `as`, `x.pe`, each with any `.` suffix), an ed2k link's servers, sources and
  every URL in it, and a `thunder://`, `flashget://` or `qqdl://` link's wrapped URL;
- a fetching scheme (`http`, `https`, `ftp`, `ftps`, `sftp`) written with one slash, none
  or backslashes (`http:/127.0.0.1`, `http:\\127.0.0.1`), read as curl and WHATWG
  parsers read it, as `scheme://`;
- each whitespace-separated piece of a URI, in case DSM splits on it, and a tracker or
  seed written `udp:/host`, `udp:host` or a schemeless `host:port`;
- a URI holding a control character (a NUL can end a host where DSM reads it), and a
  host name IDNA can't prepare: refused as unreadable. Host names are compared as IDNA
  reads them (nameprep: NFKC, case folding, invisible characters such as U+200B dropped;
  the ideographic, fullwidth and halfwidth full stops read as `.`);
- a URI with no host this can read, or a wrapped link that doesn't decode: refused as
  one that can't be checked, rather than guessed at.

Not stoppable here (the docs say so): a public name whose DNS answers with an internal
address, a public server that redirects to one, and the trackers and web seeds inside a
`.torrent` file (`torrent_file_path`). Scheme-less text and schemes Download Station
doesn't take (`file:`, `gopher:`, `smb:`, `data:`) are left to DSM, which refuses them
with 1901.

A refusal never quotes the URI or its host: create_download's ``uri`` is exempt from the
check for characters that could forge a reply line (#604), so it's never echoed. It
names the URI by its position and says what kind of address it is.
"""

from __future__ import annotations

import base64
import binascii
import encodings.idna
import ipaddress
import re
import unicodedata
from typing import NoReturn
from urllib.parse import parse_qsl, unquote, urlsplit

from mcp_synology.core.errors import ErrorCode
from mcp_synology.core.formatting import error_response

# Names only a local network answers (RFC 6761 `localhost`, RFC 6762 `.local`, RFC 8375
# `.home.arpa`, ICANN's reserved `.internal`); a name with no dot is refused too.
# RFC 6762 Appendix G's private names and `.localdomain` (`localhost.localdomain`) too.
_LOCAL_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".home.arpa",
    ".localdomain",
    ".lan",
    ".home",
    ".corp",
    ".private",
    ".intranet",
)
_LOCAL_NAMES = frozenset({"localhost", *(s[1:] for s in _LOCAL_SUFFIXES)})

# Schemes Download Station fetches by host: written with one slash, none or backslashes,
# they're still read as `scheme://host` (curl rewrites `http:/h` to `http://h`).
_FETCH_SCHEMES = frozenset({"http", "https", "ftp", "ftps", "sftp"})

# Magnet parameters naming a server the NAS would contact (BEP 9, BEP 19 and the magnet
# URI scheme's `xs`/`as`/`x.pe`), each also with any `.` suffix (`tr.1`, `tr.x`).
_MAGNET_SOURCE_KEYS = frozenset({"tr", "ws", "xs", "as", "x.pe"})

_WRAPPED = ("thunder", "flashget", "qqdl")
_FLASHGET_MARK = "[FLASHGET]"

# ed2k: `ed2k://|server|<host>|<port>|/` and `|sources,<host>:<port>,...|` in a file link.
_ED2K_SERVER = re.compile(r"\|server\|([^|]*)\|", re.IGNORECASE)
_ED2K_SOURCES = re.compile(r"\|sources,([^|]*)", re.IGNORECASE)
# Any URL inside an ed2k link (`|serverlist|<url>|`, `|nodeslist|<url>|`).
# Anchored at a scheme's start, so a long run of letters is scanned once (linear).
_ED2K_URL = re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*:[^|]*", re.IGNORECASE)

# The dots IDNA (UTS #46, RFC 3490) reads as `.`: full stop, ideographic, fullwidth and
# halfwidth ideographic full stops.
_IDNA_DOTS = re.compile("[.\u3002\uff0e\uff61]")

# A tracker or seed's scheme inside a magnet link, read by a BitTorrent client with one
# slash, none or backslashes too, and a schemeless `host:port`.
_SOURCE_SCHEME = re.compile(r"(?i)(?:udp|https?|wss?|ftps?|sftp):[/\\]*")
_HOST_PORT = re.compile(r"[^:/?#\s]+:\d+(?:[/?#]|$)")

# A deprecated IPv6 site-local range (RFC 3879) that Python counts as global.
_SITE_LOCAL = ipaddress.IPv6Network("fec0::/10")


# How deep wrapped links may nest (`thunder://` of a `qqdl://` of a URL) before the URI is
# refused as unreadable.
_MAX_DEPTH = 4

_SETTING_HINT = (
    "If the user means to download from their own network, they can turn on "
    "`allow_local_addresses` in the downloadstation module's settings of the mcp-synology "
    "config. Otherwise use a public source."
)


class _RefusedError(Exception):
    """A source the check refuses: ``what`` says why, without quoting it."""

    def __init__(self, what: str, *, unreadable: bool = False) -> None:
        super().__init__(what)
        self.what = what
        self.unreadable = unreadable


def _cant_read(what: str) -> _RefusedError:
    return _RefusedError(what, unreadable=True)


_PRIVATE = "an address on a local or private network"


def _ipv4_like(host: str) -> ipaddress.IPv4Address | None:
    """``host`` as an IPv4 address the way `inet_aton` reads it, or None if it isn't one.

    One to four parts, each decimal, `0x` hex or `0`-leading octal, the last filling the
    bytes left (`127.1` is 127.0.0.1, `2130706433` is 127.0.0.1). A part that doesn't
    parse, or a value too large for its bytes, isn't an address.
    """
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    values: list[int] = []
    for part in parts:
        if not part:
            return None
        try:
            if part[:2].lower() == "0x":
                value = int(part[2:] or "0", 16)
            elif len(part) > 1 and part[0] == "0":
                value = int(part[1:], 8)
            else:
                value = int(part, 10)
        except ValueError:
            return None
        values.append(value)
    *head, last = values
    if any(v > 255 for v in head) or last >= 256 ** (4 - len(head)):
        return None
    number = 0
    for v in head:
        number = number * 256 + v
    number = number * 256 ** (4 - len(head)) + last
    return ipaddress.IPv4Address(number)


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``host`` (brackets and an IPv6 zone already off) as an IP address, or None."""
    if ":" in host:
        try:
            return ipaddress.IPv6Address(host)
        except ValueError:
            return None
    return _ipv4_like(host)


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 one carries: mapped, compatible, 6to4, Teredo, NAT64."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    packed = ip.packed
    if packed[:12] == bytes(12) or packed[:12] == b"\x00\x64\xff\x9b" + bytes(8):
        return ipaddress.IPv4Address(packed[12:])
    return None


def _ip_kind(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """What kind of local address ``ip`` is, or None if it's a public one."""
    kind = _own_kind(ip)
    # The IPv4 address an IPv6 one carries is what's reached (6to4 `2002:7f00:1::` is
    # 127.0.0.1), so it names the kind, unless the IPv6 address has a kind of its own
    # (`::1` is loopback, not the compatible form of 0.0.0.1).
    if isinstance(ip, ipaddress.IPv6Address) and kind in (None, _PRIVATE):
        inner = _embedded_v4(ip)
        inner_kind = _own_kind(inner) if inner is not None else None
        if inner_kind is not None:
            return inner_kind
    return kind


def _own_kind(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if ip.is_loopback:
        return "a loopback address (the NAS itself)"
    if ip.is_unspecified:
        return "the unspecified address, which reaches the NAS itself"
    if ip.is_link_local:
        return "a link-local address (such as a cloud metadata service)"
    if ip.is_multicast:
        return "a multicast address"
    if isinstance(ip, ipaddress.IPv6Address) and ip in _SITE_LOCAL:
        return _PRIVATE
    if not ip.is_global:
        return _PRIVATE
    return None


def _normalized_host(host: str) -> str:
    """``host`` as an IDNA resolver would compare it: percent-decoded, each label through
    IDNA's nameprep (NFKC, case folding, invisible characters such as U+200B and the soft
    hyphen dropped), every IDNA dot (`。`) a `.`, no brackets, IPv6 zone or trailing dots.

    A label nameprep refuses, or a control character, can't be checked (``_cant_read``).
    """
    host = unquote(host)
    if any(unicodedata.category(c) == "Cc" for c in host):
        raise _cant_read("holds a control character, so its host can't be read")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if ":" in host:  # IPv6: no labels to prepare
        return unicodedata.normalize("NFKC", host).lower().split("%", 1)[0]
    try:
        labels = [encodings.idna.nameprep(label) for label in _IDNA_DOTS.split(host)]
    except UnicodeError as e:
        raise _cant_read("has a host name that can't be read") from e
    return ".".join(labels).lower().rstrip(".")


def _check_host(host: str, nas: tuple[str, object] | None) -> None:
    """Refuse ``host`` when it's local, or the NAS's own host."""
    name = _normalized_host(host)
    if not name:
        raise _cant_read("has no host this server can read")
    ip = _ip(name)
    if nas is not None:
        nas_name, nas_ip = nas
        if name == nas_name or (ip is not None and ip == nas_ip):
            raise _RefusedError("names the NAS itself")
    if ip is not None:
        kind = _ip_kind(ip)
        if kind is not None:
            raise _RefusedError(f"names {kind}")
        return
    if ":" in name:  # an IPv6-looking host that didn't parse
        raise _cant_read("has no host this server can read")
    if name in _LOCAL_NAMES or name.endswith(_LOCAL_SUFFIXES):
        raise _RefusedError("names a host only a local network answers")
    if "." not in name:
        raise _RefusedError("names a host with no domain, which only a local network answers")


def _host_port(text: str, nas: tuple[str, object] | None) -> None:
    """Check a `host:port` (or `[v6]:port`, or a bare host) from a magnet or ed2k link."""
    text = text.strip()
    if text.startswith("["):
        host = text[1 : text.find("]")] if "]" in text else ""
    elif text.count(":") == 1:
        host = text.split(":", 1)[0]
    else:
        host = text
    _check_host(host, nas)


def _unwrap(scheme: str, body: str) -> str:
    """The URL inside a `thunder://`, `flashget://` or `qqdl://` link's base64 body."""
    body = unquote(body).split("&", 1)[0].strip().rstrip("/")
    try:
        padded = body + "=" * (-len(body) % 4)
        decoded = base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True)
        text = decoded.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as e:
        raise _cant_read("is a wrapped link whose URL can't be read") from e
    if scheme == "thunder" and text.startswith("AA") and text.endswith("ZZ"):
        text = text[2:-2]
    elif scheme == "flashget" and text.startswith(_FLASHGET_MARK) and text.endswith(_FLASHGET_MARK):
        text = text[len(_FLASHGET_MARK) : -len(_FLASHGET_MARK)]
    return text


def _check_uri(uri: str, nas: tuple[str, object] | None, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise _cant_read("wraps links too deeply to read")
    stripped = uri.strip()
    scheme, sep, rest = stripped.partition(":")
    scheme = scheme.lower()
    if not sep:
        if "%" in stripped and unquote(stripped) != stripped:
            # `thunder%3A%2F%2F…`: a percent-encoded link is checked as it decodes.
            _check_uri(unquote(stripped), nas, depth + 1)
        return  # no scheme: DSM refuses it (1901)
    if scheme == "magnet":
        # With or without the `?`, `&` or `;` between parameters.
        query = rest.split("?", 1)[1] if "?" in rest else rest
        for key, value in parse_qsl(query.replace(";", "&"), keep_blank_values=True):
            name = key.strip().lower()
            base = next(
                (k for k in _MAGNET_SOURCE_KEYS if name == k or name.startswith(k + ".")),
                None,
            )
            if base is None or not value:
                continue
            try:
                if base == "x.pe":
                    _host_port(value, nas)
                else:
                    _check_uri(value, nas, depth + 1)
                    # As a BitTorrent client may read it: `udp:/h`, `udp:h`, or `h:6969`.
                    if m := _SOURCE_SCHEME.match(value):
                        _check_uri("http://" + value[m.end() :], nas, depth + 1)
                    elif _HOST_PORT.match(value):
                        _host_port(value.split("/", 1)[0], nas)
            except _RefusedError as e:
                raise _RefusedError(
                    f"has a tracker, seed or peer that {e.what}", unreadable=e.unreadable
                ) from e
        return
    if scheme in _WRAPPED:
        body = rest[2:] if rest.startswith("//") else rest
        try:
            _check_uri(_unwrap(scheme, body), nas, depth + 1)
        except _RefusedError as e:
            if e.what.startswith("is a wrapped link") or e.what.startswith("wraps"):
                raise
            raise _RefusedError(f"wraps a URL that {e.what}", unreadable=e.unreadable) from e
        return
    if scheme == "ed2k":
        for host in _ED2K_SERVER.findall(rest):
            _check_host(host, nas)
        for group in _ED2K_SOURCES.findall(rest):
            for source in group.split(","):
                if source.strip():
                    _host_port(source, nas)
        for url in _ED2K_URL.findall(rest):
            _check_uri(url, nas, depth + 1)
        return
    if scheme in _FETCH_SCHEMES:
        # `http:/h`, `http:h` and `http:\\h` are fetched as `http://h` (curl rewrites them;
        # WHATWG reads `\\` as `/`). Both readings of a `\\` are checked: RFC 3986's, where
        # `http://a\\@b/` is host b, and WHATWG's, where it's host a.
        readings = [
            f"{scheme}://" + rest.lstrip("/\\"),
            f"{scheme}://" + rest.replace("\\", "/").lstrip("/"),
        ]
    elif rest.startswith(("//", "\\")):
        # WHATWG's reading first: RFC 3986 finds no host in `ws:\\h`.
        readings = [stripped.replace("\\", "/"), stripped]
    else:
        return  # `mailto:`-style: no host to fetch; DSM refuses what it can't use
    for reading in dict.fromkeys(readings):
        try:
            host = urlsplit(reading).hostname
        except ValueError as e:  # an unclosed `[` or a bad IPv6 literal
            raise _cant_read("has no host this server can read") from e
        _check_host(host or "", nas)


def nas_identity(nas_host: str | None) -> tuple[str, object] | None:
    """The NAS's own host, as the check compares it: its name, and its address if it is one."""
    if not nas_host:
        return None
    try:
        name = _normalized_host(nas_host)
    except _RefusedError:  # config validation refuses such a host; compare it as written
        name = nas_host.lower()
    return name, _ip(name)


def refuse_local_sources(
    uris: list[str], *, nas_host: str | None, allow_local_addresses: bool
) -> None:
    """Refuse create_download's ``uris`` when any names the NAS or its local network (#648).

    Every URI create_download sends goes through this first. With
    ``allow_local_addresses`` on (the module setting), nothing is checked.
    """
    if allow_local_addresses:
        return
    nas = nas_identity(nas_host)
    for index, uri in enumerate(uris, start=1):
        # A NUL or other control character (not the whitespace split on below) can end a
        # host where DSM reads it and not here: `http://127.0.0.1<NUL>.example.com/`.
        if any(unicodedata.category(c) == "Cc" and not c.isspace() for c in uri):
            _refuse(index, len(uris), _cant_read("holds a control character"))
        # The whole URI, then each whitespace-separated piece of it, in case DSM splits a
        # URI on a newline or space as its own paste box does.
        pieces = uri.split()
        for piece in [uri, *(pieces if len(pieces) > 1 else [])]:
            try:
                _check_uri(piece, nas)
            except _RefusedError as e:
                _refuse(index, len(uris), e)


def _refuse(index: int, total: int, e: _RefusedError) -> NoReturn:
    which = "the URI" if total == 1 else f"URI {index} of {total}"
    if e.unreadable:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"Create download refused: {which} {e.what}, so it can't be checked for an "
            "address on the NAS or its local network and isn't sent. Nothing was created.",
            retryable=False,
            param="uri",
            suggestion=(
                "Check the link: write it as scheme://host/path (or a magnet link). A "
                "wrapped thunder://, flashget:// or qqdl:// link can be passed as the URL "
                "it wraps."
            ),
        )
    error_response(
        ErrorCode.INVALID_PARAMETER,
        f"Create download refused: {which} {e.what}. Download Station fetches each URI "
        "from the NAS, so a URI on the NAS itself or its local network isn't sent. "
        "Nothing was created.",
        retryable=False,
        param="uri",
        suggestion=_SETTING_HINT,
    )
