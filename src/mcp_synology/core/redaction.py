"""Redact secrets embedded in URLs before they reach tool output or logs (#143).

Private BitTorrent trackers put a personal passkey in the announce URL
(``/announce/<passkey>`` or ``?passkey=``), and download sources can carry
``user:pass@`` or tokens in the query. DSM hands these back verbatim, so
anything that shows a tracker URL or a task's source URI must pass it through
here first.

Rules, chosen so the host stays visible and nothing secret-shaped survives:

- userinfo becomes ``***@``
- every query value becomes ``***`` (keys are kept); a bare query item becomes ``***``
- path parameters (``;key=value``) are masked like query items
- a token inside a path segment becomes ``***``: a run of ``[A-Za-z0-9_-]`` holding
  an alphanumeric stretch of 16+ characters with both a letter and a digit
  (``<passkey>.torrent`` becomes ``***.torrent``), or a UUID. Names made of short
  words, like ``announce`` or ``ubuntu-24-04-desktop-amd64``, are kept
- the fragment is dropped
- a magnet keeps only ``xt`` / ``dn`` / ``xl`` / ``kt`` as they are, redacts its
  URL-valued parameters in turn, and masks every other value. A tracker URL
  pasted unencoded splits on ``&``, so its own query items land at the top level

Task titles get their own, narrower pass (``redact_title``): DSM titles an HTTP
task with the tail of its URL, query string included (``file.bin&_sid=...``).

``redact_secrets`` is narrower still: it masks credentials and nothing else (URL userinfo,
known credential parameters, wrapped download links), so it can run over any text the server
sends without hiding what a reply is for, such as a Photos share link (#514).

Every pattern here runs in linear time: schemes are at most 32 characters, lookaheads are
bounded, and the userinfo scan looks at a fixed window after each ``://`` (#465).
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote_plus, urlsplit, urlunsplit

MASK = "***"

_TOKEN_RUN = re.compile(r"[A-Za-z0-9_-]{16,}")
_ALNUM_CHUNK = re.compile(r"[A-Za-z0-9]{16,}")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_HAS_LETTER = re.compile(r"[A-Za-z]")
_HAS_DIGIT = re.compile(r"[0-9]")

# Magnet parameters whose values are URLs (trackers, web seeds, sources).
_MAGNET_URL_KEYS = frozenset({"tr", "ws", "as", "xs"})
# Magnet parameters shown as they are: info hash, name, length, keywords.
_MAGNET_SAFE_KEYS = frozenset({"xt", "dn", "xl", "kt"})

# A URL scheme, bounded so a long run of letters can't be rescanned from every position (#465:
# 60,000 letters before a `://` took 2.9 s unbounded).
_SCHEME = r"[A-Za-z][A-Za-z0-9+.-]{0,31}"
# Schemes whose links carry no host or query to keep: a wrapped link (`thunder://<base64>`, which
# hides a whole URL, credentials included), or an ed2k link written without `//`. Wrapped links
# are masked whole (#458, #514 Q4).
_WRAPPED = ("thunder", "flashget", "qqdl")
_OPAQUE_START = r"(?i:magnet):\?|(?i:ed2k|thunder|flashget|qqdl):"

# A URL or magnet inside free text. A comma ends a match only where another URI
# starts after it, as create_download splits its list (#397): a comma inside a URL
# (`/a,b.iso?token=…`) is part of it, and v1 Task.create's list is comma-separated.
# Quotes end a match for DS2's JSON-encoded list; a comma at the end of a match (a
# URL in prose: "see https://…, then") is left out of it.
_URL_CHAR = (
    r"""(?:[^\s,"'<>]"""
    rf"""|,(?!\s{{0,16}}(?:{_SCHEME}://|(?i:magnet|ed2k|thunder|flashget|qqdl):)))"""
)
# An ed2k, thunder, flashget or qqdl link after such a comma is a URI of its own, so it's matched
# here too, or its query would stay as it is (#466: `ftp://h/a,ED2K:foo?token=…`).
_URL_IN_TEXT = re.compile(rf"""(?:{_OPAQUE_START}|{_SCHEME}://){_URL_CHAR}+(?<!,)""")

# What a host may hold, once userinfo is gone: a name, an IPv4 or bracketed IPv6 address, a
# port. A netloc with anything else (`c&token=…`, a URL's query glued to its host after a comma
# split it, #466) keeps its host and masks the rest.
_HOST = re.compile(r"[A-Za-z0-9.\-_~%\[\]:]*")

# Wrapped links in free text, masked whole with their scheme kept.
_WRAPPED_IN_TEXT = re.compile(
    r"""(?i)(?<![A-Za-z0-9+.-])(thunder|flashget|qqdl):(//)?[^\s,"'<>]*"""
)

# Credential parameters, as `key=value` in a query, a form body or prose, or as a JSON member.
# The keys DSM and this server use for a password, a session, a 2FA code or device, and the
# passkeys private trackers put in a URL. Case is ignored.
_CREDENTIAL_KEYS = (
    "passwd|password|pwd|_sid|sid|synotoken|otp_code|device_id|device_name|passkey|authkey|"
    "torrent_pass|token|access_token|api_key|apikey"
)
_CREDENTIAL_PARAM = re.compile(rf"""(?i)(?<![A-Za-z0-9_])((?:{_CREDENTIAL_KEYS})=)[^\s&;#"'<>]+""")
_CREDENTIAL_JSON = re.compile(
    rf"""(?i)("(?:{_CREDENTIAL_KEYS})"\s{{0,4}}:\s{{0,4}}")(?:[^"\\]|\\.){{1,4096}}(")"""
)
# How far past a `://` the userinfo scan looks for the `@` that ends it.
_USERINFO_WINDOW = 256
# How many `@`s in one window the scan tries, last first: a password holds few, and a cap keeps
# a run of `://@` from costing a window's worth of checks per `@`.
_USERINFO_TRIES = 8
# A character a host can start with, after the `@` that ends userinfo.
_HOST_START = re.compile(r"[A-Za-z0-9\[]")
_PORT_THEN_PATH = re.compile(r"\d+/")


# A query item or path parameter glued to a title: ``?key=value``, ``&key=value``
# or ``;key=value``, with no spaces. "Tom & Jerry" doesn't match, because the
# ``&`` must be followed by ``key=``.
_QUERY_ITEM = re.compile(r"([?&;])([^\s=&?;#]+)=[^\s&;#]*")


def _is_userinfo(span: str) -> bool:
    """Whether ``span``, the text between a ``://`` and an ``@``, reads as userinfo.

    ``user:password`` with anything in the password (a space, ``/``, ``#``: #458), as long as
    the user part holds no ``/``, ``?`` or ``#`` (``https://site/a and me: x@y`` has a path
    before the colon) and the colon isn't a port before a path (``h:8080/@x``). Without a colon,
    a plain ``user`` with no whitespace, ``/``, ``?`` or ``#`` (``https://site/@name`` is a
    path). The scan passes a non-empty span from one line.
    """
    user, colon, rest = span.partition(":")
    if not colon:
        return not any(c.isspace() or c in "/?#" for c in span)
    if any(c in "/?#" for c in user):
        return False
    return _PORT_THEN_PATH.match(rest) is None


def _mask_userinfo(text: str) -> str:
    """``text`` with the userinfo of every ``scheme://user:password@host`` masked, found by a
    scan rather than ``urlsplit``, so a password holding a space, ``/`` or ``#`` is masked too
    (#458). Each ``://`` gets a window of ``_USERINFO_WINDOW`` characters on its line, so the
    scan is linear in ``text``."""
    if "@" not in text:
        return text
    out: list[str] = []
    pos = 0
    start = text.find("://")
    while start >= 0:
        begin = start + 3
        window = text[begin : begin + _USERINFO_WINDOW].partition("\n")[0]
        end = None
        at = window.rfind("@")
        for _ in range(_USERINFO_TRIES):
            if at <= 0:
                break
            if _HOST_START.match(window, at + 1) and _is_userinfo(window[:at]):
                end = at
                break
            at = window.rfind("@", 0, at)
        if end is not None and window[:end] != MASK:
            out.append(text[pos:begin])
            out.append(MASK)
            pos = begin + end
        start = text.find("://", max(begin, pos))
    out.append(text[pos:])
    return "".join(out)


def _mask_wrapped(text: str) -> str:
    """``text`` with every ``thunder:``, ``flashget:`` or ``qqdl:`` link masked whole."""
    return _WRAPPED_IN_TEXT.sub(lambda m: f"{m.group(1)}:{m.group(2) or ''}{MASK}", text)


def redact_secrets(text: str) -> str:
    """``text`` with credentials masked and everything else kept (#514).

    URL userinfo (``scheme://***@host``), the value of a credential parameter (``passwd``,
    ``_sid``, ``SynoToken``, ``otp_code``, ``device_id``, a tracker passkey; as ``key=value`` or
    a JSON member), and wrapped download links (``thunder://***``). Unlike ``redact_urls``,
    query values, path tokens and other URL parts are kept: a Photos share link's passphrase is
    what the user asked for.
    """
    text = _mask_userinfo(_mask_wrapped(text))
    text = _CREDENTIAL_PARAM.sub(lambda m: f"{m.group(1)}{MASK}", text)
    return _CREDENTIAL_JSON.sub(lambda m: f"{m.group(1)}{MASK}{m.group(2)}", text)


def _is_token(run: str) -> bool:
    if _UUID.fullmatch(run):
        return True
    return any(
        _HAS_LETTER.search(chunk) and _HAS_DIGIT.search(chunk)
        for chunk in _ALNUM_CHUNK.findall(run)
    )


def _redact_segment(segment: str) -> str:
    main, sep, params = segment.partition(";")
    main = _TOKEN_RUN.sub(lambda m: MASK if _is_token(m.group(0)) else m.group(0), main)
    if not sep:
        return main
    masked = [f"{p.partition('=')[0]}={MASK}" if "=" in p else MASK for p in params.split(";")]
    return main + ";" + ";".join(masked)


def _redact_query(query: str) -> str:
    items = []
    for item in query.split("&"):
        if not item:
            continue
        key, sep, _ = item.partition("=")
        items.append(f"{key}={MASK}" if sep else MASK)
    return "&".join(items)


def _redact_magnet(magnet: str) -> str:
    params = []
    prefix, rest = magnet[: len("magnet:?")], magnet[len("magnet:?") :]
    for item in rest.split("&"):
        key, sep, value = item.partition("=")
        if not sep:
            item = MASK
        elif key in _MAGNET_URL_KEYS:
            inner = unquote_plus(value)
            redacted = redact_url(inner)
            if redacted != inner:
                item = f"{key}={quote(redacted, safe=':/*@')}"
        elif key not in _MAGNET_SAFE_KEYS:
            item = f"{key}={MASK}"
        params.append(item)
    return prefix + "&".join(params)


def redact_url(url: str) -> str:
    """Return ``url`` with secrets masked; anything that isn't a URL is returned as-is."""
    if url[: len("magnet:?")].lower() == "magnet:?":  # any case, as DSM may accept it
        return _redact_magnet(url)
    scheme, colon, rest = url.partition(":")
    if colon and scheme.lower() in _WRAPPED:
        return _mask_wrapped(url)
    if "://" not in url:
        if colon and re.fullmatch(_SCHEME, scheme) and "?" in rest:
            # An opaque link such as `ed2k:foo?token=…`: its query is masked as a URL's (#466).
            before, _, query = url.partition("?")
            return f"{before}?{_redact_query(query.partition('#')[0])}"
        return url
    url = _mask_userinfo(url)
    try:
        parts = urlsplit(url)
    except ValueError:
        return MASK
    if not parts.scheme or not parts.netloc:
        return url

    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{MASK}@{netloc.rpartition('@')[2]}"
    user, at, host = netloc.rpartition("@")
    kept = _HOST.match(host)
    if kept is not None and kept.end() < len(host):
        # Not a host past this point: a query glued to it once a comma split the URL (#466).
        netloc = f"{user}{at}{host[: kept.end() + 1]}{MASK}"
    path = "/".join(_redact_segment(seg) for seg in parts.path.split("/"))
    query = _redact_query(parts.query) if parts.query else ""
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def redact_urls(text: str) -> str:
    """Redact every URL or magnet found in ``text`` (lists, JSON, prose)."""
    # Userinfo first: a password with a space or a `#` would end a URL match early (#458).
    text = _mask_userinfo(_mask_wrapped(text))
    return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), text)


def redact_title(title: str, *, url_derived: bool = False) -> str:
    """Mask query values (and any whole URL) in a task title; other text is kept.

    Token-shaped words are left alone by default: a BT title like
    ``Show_Series_1_2026_1080p`` is a name, not a secret. With ``url_derived``
    (an HTTP/FTP/NZB task, whose title is the URL's last segment), tokens are
    masked as ``redact_url`` masks them in a path, so ``<passkey>.torrent``
    becomes ``***.torrent``.
    """
    title = _QUERY_ITEM.sub(lambda m: f"{m.group(1)}{m.group(2)}={MASK}", redact_urls(title))
    if url_derived:
        title = _TOKEN_RUN.sub(lambda m: MASK if _is_token(m.group(0)) else m.group(0), title)
    return title
