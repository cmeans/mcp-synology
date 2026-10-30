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

# A URL or magnet inside free text. A comma ends a match only where another URI
# starts after it, as create_download splits its list (#397): a comma inside a URL
# (`/a,b.iso?token=…`) is part of it, and v1 Task.create's list is comma-separated.
# Quotes end a match for DS2's JSON-encoded list; a comma at the end of a match (a
# URL in prose: "see https://…, then") is left out of it.
_URL_CHAR = (
    r"""(?:[^\s,"'<>]"""
    r"""|,(?!\s*(?:[A-Za-z][A-Za-z0-9+.-]*://|(?:magnet|ed2k|thunder|flashget|qqdl):)))"""
)
_URL_IN_TEXT = re.compile(
    rf"""(?:magnet:\?|[A-Za-z][A-Za-z0-9+.-]*://){_URL_CHAR}+(?<!,)""", re.IGNORECASE
)


# A query item or path parameter glued to a title: ``?key=value``, ``&key=value``
# or ``;key=value``, with no spaces. "Tom & Jerry" doesn't match, because the
# ``&`` must be followed by ``key=``.
_QUERY_ITEM = re.compile(r"([?&;])([^\s=&?;#]+)=[^\s&;#]*")


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
    for item in magnet[len("magnet:?") :].split("&"):
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
    return "magnet:?" + "&".join(params)


def redact_url(url: str) -> str:
    """Return ``url`` with secrets masked; anything that isn't a URL is returned as-is."""
    if url.startswith("magnet:?"):
        return _redact_magnet(url)
    if "://" not in url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return MASK
    if not parts.scheme or not parts.netloc:
        return url

    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{MASK}@{netloc.rpartition('@')[2]}"
    path = "/".join(_redact_segment(seg) for seg in parts.path.split("/"))
    query = _redact_query(parts.query) if parts.query else ""
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def redact_urls(text: str) -> str:
    """Redact every URL or magnet found in ``text`` (lists, JSON, prose)."""
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
