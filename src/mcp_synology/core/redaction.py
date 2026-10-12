"""Redact secrets embedded in URLs before they reach tool output or logs (#143).

Private BitTorrent trackers put a personal passkey in the announce URL
(``/announce/<passkey>`` or ``?passkey=``), and download sources can carry
``user:pass@`` or tokens in the query. DSM hands these back verbatim, so
anything that shows a tracker URL or a task's source URI must pass it through
here first.

Rules, chosen so the host stays visible and nothing secret-shaped survives:

- userinfo becomes ``***@``
- every query value becomes ``***`` (keys are kept); a bare query item becomes ``***``
- a path segment that looks like a token (16+ of ``[A-Za-z0-9_-]`` with both a
  letter and a digit) becomes ``***``; names like ``announce`` or
  ``ubuntu-24.04.iso`` are kept
- the fragment is dropped
- a magnet keeps ``xt`` / ``dn``; its URL-valued parameters are redacted in turn

Task titles get their own, narrower pass (``redact_title``): DSM titles an HTTP
task with the tail of its URL, query string included (``file.bin&_sid=...``).
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote_plus, urlsplit, urlunsplit

MASK = "***"

_TOKEN_SEGMENT = re.compile(r"[A-Za-z0-9_-]{16,}")
_HAS_LETTER = re.compile(r"[A-Za-z]")
_HAS_DIGIT = re.compile(r"[0-9]")

# Magnet parameters whose values are URLs (trackers, web seeds, sources).
_MAGNET_URL_KEYS = frozenset({"tr", "ws", "as", "xs"})

# A URL or magnet inside free text. Commas end a match because v1 Task.create
# takes a comma-separated URI list; quotes end it for DS2's JSON-encoded list.
_URL_IN_TEXT = re.compile(r"""magnet:\?[^\s,"'<>]+|[A-Za-z][A-Za-z0-9+.-]*://[^\s,"'<>]+""")


# A query item glued to a title: ``?key=value`` or ``&key=value`` with no spaces.
# "Tom & Jerry" doesn't match, because the ``&`` must be followed by ``key=``.
_QUERY_ITEM = re.compile(r"([?&])([^\s=&?#]+)=[^\s&#]*")


def _is_token(segment: str) -> bool:
    return bool(
        _TOKEN_SEGMENT.fullmatch(segment)
        and _HAS_LETTER.search(segment)
        and _HAS_DIGIT.search(segment)
    )


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
        if sep and key in _MAGNET_URL_KEYS:
            inner = unquote_plus(value)
            redacted = redact_url(inner)
            if redacted != inner:
                item = f"{key}={quote(redacted, safe=':/*@')}"
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
    path = "/".join(MASK if _is_token(seg) else seg for seg in parts.path.split("/"))
    query = _redact_query(parts.query) if parts.query else ""
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def redact_urls(text: str) -> str:
    """Redact every URL or magnet found in ``text`` (lists, JSON, prose)."""
    return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), text)


def redact_title(title: str) -> str:
    """Mask query values (and any whole URL) in a task title; other text is kept.

    Unlike ``redact_url``, token-shaped words are left alone: a title like
    ``Show_Series_1_2026_1080p`` is a name, not a secret.
    """
    return _QUERY_ITEM.sub(lambda m: f"{m.group(1)}{m.group(2)}={MASK}", redact_urls(title))
