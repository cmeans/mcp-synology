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
  no dot, `*.local`, `*.internal`, `*.home.arpa`;
- the NAS's own host from the config (`connection.host`), however it's reached;
- the same inside a magnet link (its trackers, web seeds, sources and peers: `tr`, `ws`,
  `xs`, `as`, `x.pe`), an ed2k link's servers and sources, and a `thunder://`,
  `flashget://` or `qqdl://` link's wrapped URL, which Download Station unwraps;
- a URI with a `//` but no host this can read, or a wrapped link that doesn't decode:
  refused rather than guessed at.

Not stoppable here (the docs say so): a public name whose DNS answers with an internal
address, and a public server that redirects to one. Only the NAS's own firewall covers
those. Scheme-less text and schemes Download Station doesn't take (`file:`, `gopher:`,
`smb:`, `data:`) are left to DSM, which refuses them with 1901.

A refusal never quotes the URI or its host: create_download's ``uri`` is exempt from the
check for characters that could forge a reply line (#604), so it's never echoed. It
names the URI by its position and says what kind of address it is.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
import unicodedata
from typing import NoReturn
from urllib.parse import parse_qsl, unquote, urlsplit

from mcp_synology.core.errors import ErrorCode
from mcp_synology.core.formatting import error_response

# Names only a local network answers (RFC 6761 `localhost`, RFC 6762 `.local`, RFC 8375
# `.home.arpa`, ICANN's reserved `.internal`); a name with no dot is refused too.
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")

# Magnet parameters naming a server the NAS would contact (BEP 9, BEP 19 and the magnet
# URI scheme's `xs`/`as`/`x.pe`), each also with a `.N` suffix (`tr.1`).
_MAGNET_SOURCE_KEYS = frozenset({"tr", "ws", "xs", "as", "x.pe"})

_WRAPPED = ("thunder", "flashget", "qqdl")
_FLASHGET_MARK = "[FLASHGET]"

# ed2k: `ed2k://|server|<host>|<port>|/` and `|sources,<host>:<port>,...|` in a file link.
_ED2K_SERVER = re.compile(r"\|server\|([^|]*)\|", re.IGNORECASE)
_ED2K_SOURCES = re.compile(r"\|sources,([^|]*)", re.IGNORECASE)

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

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


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
    if not ip.is_global:
        return _PRIVATE
    return None


def _normalized_host(host: str) -> str:
    """``host`` as a resolver would compare it: percent-decoded, NFKC, lowercase, no
    brackets, IPv6 zone or trailing dots."""
    host = unicodedata.normalize("NFKC", unquote(host)).lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    host = host.split("%", 1)[0] if ":" in host else host
    return host.rstrip(".")


def _check_host(host: str, nas: tuple[str, object] | None) -> None:
    """Refuse ``host`` when it's local, or the NAS's own host."""
    name = _normalized_host(host)
    if not name:
        raise _RefusedError("has no host this server can read")
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
        raise _RefusedError("has no host this server can read")
    if name == "localhost" or name.endswith(_LOCAL_SUFFIXES):
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
        raise _RefusedError("is a wrapped link whose URL can't be read") from e
    if scheme == "thunder" and text.startswith("AA") and text.endswith("ZZ"):
        text = text[2:-2]
    elif scheme == "flashget" and text.startswith(_FLASHGET_MARK) and text.endswith(_FLASHGET_MARK):
        text = text[len(_FLASHGET_MARK) : -len(_FLASHGET_MARK)]
    return text


def _check_uri(uri: str, nas: tuple[str, object] | None, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise _RefusedError("wraps links too deeply to read")
    stripped = uri.strip()
    scheme, sep, rest = stripped.partition(":")
    scheme = scheme.lower()
    if not sep:
        if "%" in stripped and unquote(stripped) != stripped:
            # `thunder%3A%2F%2F…`: a percent-encoded link is checked as it decodes.
            _check_uri(unquote(stripped), nas, depth + 1)
        return  # no scheme: DSM refuses it (1901)
    if scheme == "magnet":
        query = rest.split("?", 1)[1] if "?" in rest else ""
        for key, value in parse_qsl(query, keep_blank_values=True):
            base = key.lower()
            base = re.sub(r"\.\d+$", "", base)
            if base not in _MAGNET_SOURCE_KEYS or not value:
                continue
            try:
                if base == "x.pe":
                    _host_port(value, nas)
                else:
                    _check_uri(value, nas, depth + 1)
            except _RefusedError as e:
                raise _RefusedError(f"has a tracker, seed or peer that {e.what}") from e
        return
    if scheme in _WRAPPED:
        body = rest[2:] if rest.startswith("//") else rest
        try:
            _check_uri(_unwrap(scheme, body), nas, depth + 1)
        except _RefusedError as e:
            if e.what.startswith("is a wrapped link") or e.what.startswith("wraps"):
                raise
            raise _RefusedError(f"wraps a URL that {e.what}") from e
        return
    if scheme == "ed2k":
        for host in _ED2K_SERVER.findall(rest):
            _check_host(host, nas)
        for group in _ED2K_SOURCES.findall(rest):
            for source in group.split(","):
                if source.strip():
                    _host_port(source, nas)
        return
    if not rest.startswith("//"):
        return  # `mailto:`-style: no host to fetch; DSM refuses what it can't use
    try:
        parts = urlsplit(stripped)
        host = parts.hostname
    except ValueError as e:  # an unclosed `[` or a bad IPv6 literal
        raise _RefusedError("has no host this server can read") from e
    _check_host(host or "", nas)
    # A `\` reads as `/` to many URL parsers (WHATWG): check what such a parser sees too.
    if "\\" in parts.netloc:
        _check_host(parts.netloc.replace("\\", "/").split("/", 1)[0].rsplit("@", 1)[-1], nas)


def nas_identity(nas_host: str | None) -> tuple[str, object] | None:
    """The NAS's own host, as the check compares it: its name, and its address if it is one."""
    if not nas_host:
        return None
    name = _normalized_host(nas_host)
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
        try:
            _check_uri(uri, nas)
        except _RefusedError as e:
            _refuse(index, len(uris), e.what)


def _refuse(index: int, total: int, what: str) -> NoReturn:
    which = "The URI" if total == 1 else f"URI {index} of {total}"
    error_response(
        ErrorCode.INVALID_PARAMETER,
        f"Create download refused: {which} {what}. Download Station fetches each source "
        "from the NAS, so a source on the NAS itself or its local network isn't sent. "
        "Nothing was created.",
        retryable=False,
        param="uri",
        suggestion=_SETTING_HINT,
    )
