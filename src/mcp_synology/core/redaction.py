"""Redact secrets embedded in URLs before they reach tool output or logs (#143).

Private BitTorrent trackers put a personal passkey in the announce URL
(``/announce/<passkey>`` or ``?passkey=``), and download sources can carry
``user:pass@`` or tokens in the query. DSM hands these back verbatim, so
anything that shows a tracker URL or a task's source URI must pass it through
here first.

Rules, chosen so the host stays visible and nothing secret-shaped survives:

- userinfo becomes ``***@``: found by a scan inside each URL match (a 256-character window after
  its ``://``, ending at the next ``://``), ending at the first ``@`` followed by a whole host,
  else the last ``@`` before a host. So a password holding a ``/``, ``#`` or ``@`` is masked,
  unless an ``@`` in it is followed later by a ``/``, ``?`` or ``#`` (that reads as
  ``https://u:p@medium.com/@name``). A URL match ends at whitespace, a quote, ``<``, ``>`` or a
  comma before another URI, so a password with a space or a quote isn't masked
- ``&key=value`` and ``;key=value`` items glued to the host (``c&token=…``) or in a path are masked
  as a query's; nothing else after the host is (#466)
- every query value becomes ``***`` (keys are kept); a bare query item becomes ``***``
- path parameters (``;key=value``) are masked like query items
- a token inside a path segment becomes ``***``: a run of ``[A-Za-z0-9_-]`` holding
  an alphanumeric stretch of 16+ characters with both a letter and a digit
  (``<passkey>.torrent`` becomes ``***.torrent``), or a UUID. Names made of short
  words, like ``announce`` or ``ubuntu-24-04-desktop-amd64``, are kept
- the fragment is dropped
- a wrapped link (``thunder://``, ``flashget://``, ``qqdl://`` with a base64 body) is masked
  whole; an opaque link's query (``ed2k:foo?token=…``, ``ed2k:foo&token=…``) is masked
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
# hides a whole URL, credentials included). Wrapped links
# are masked whole (#458, #514 Q4).
_WRAPPED = ("thunder", "flashget", "qqdl")
# ed2k without `//` is a link only right after a comma (#466: `ftp://h/a,ED2K:foo?token=…`): a
# name like `My ed2k:Song#1.mp3` isn't one.
_OPAQUE_START = r"(?i:magnet):\?|(?:(?<=,)|(?<=,\s))(?i:ed2k):"

# A URL or magnet inside free text. A comma ends a match only where another URI
# starts after it (#397; create_download also splits at `thunder:`-style links): a comma
# inside a URL (`/a,b.iso?token=…`) is part of it, and v1 Task.create's list is comma-separated.
# Quotes end a match for DS2's JSON-encoded list; a comma at the end of a match (a
# URL in prose: "see https://…, then") is left out of it.
_URL_CHAR = (
    r"""(?:[^\s,"'<>]"""
    rf"""|,(?!\s{{0,16}}(?:{_SCHEME}://|(?i:magnet|ed2k):)))"""
)
# An ed2k link after such a comma is a URI of its own, so it's matched
# here too, or its query would stay as it is (#466: `ftp://h/a,ED2K:foo?token=…`).
_URL_IN_TEXT = re.compile(rf"""(?:{_OPAQUE_START}|{_SCHEME}://){_URL_CHAR}+(?<!,)""")

# A whole host after an `@`, as userinfo ends: a name (IDN too) or a bracketed address, maybe a
# port, then the end of the authority. An `@` later in the path or the prose isn't followed by
# one (`https://u:p@h/@name`, `… mail bob@c.example/x`: the first `@` is the one that counts).
_WHOLE_HOST = re.compile(r"""(?:\[[\w:.%]+\]|[\w.\-~%]+)(?::\d*)?(?=[/?#,\s"'<>]|$)""")
# `&key=value` in a URL's path, or in an opaque link before any `?`: a query that lost its `?`,
# as when a comma split a URL (#466).
_AMP_ITEM = re.compile(r"&([^\s=&?;#/]+)=[^\s&;#/]*")
# The same, in an authority: `&` or `;` (`b://c&token=…`, `https://h;sid=…`).
_NETLOC_ITEM = re.compile(r"([&;])([^\s=&?;#/]+)=[^\s&;#/]*")

# Wrapped links in free text, masked whole with their scheme kept.
# Real Thunder, FlashGet and QQDL links always have `//`: without it, `Thunder:Live` is a name.
# Started where a URL scheme can start (not after a scheme character, as `redact_urls` reads
# it), the body standard or URL-safe base64, ending at anything else.
_WRAPPED_IN_TEXT = re.compile(
    r"""(?i)(?<![A-Za-z0-9+])(thunder|flashget|qqdl)(://|%3A%2F%2F)"""
    r"""[A-Za-z0-9+=\-_%][A-Za-z0-9+/=\-_%]*"""
)
# A wrapped link's body: base64, not starting with `/` (so `//` is the separator, never body).
_WRAPPED_BODY = re.compile(r"//[A-Za-z0-9+=\-_%][A-Za-z0-9+/=\-_%]*")

# Credential parameters, as a query or form item after `?`, `&` or `;`, or as a JSON member.
# The keys DSM and this server use for a password, a session, a 2FA code or device, and the
# passkeys private trackers put in a URL. Case is ignored.
_CREDENTIAL_KEYS = (
    "passwd|password|pwd|_sid|sid|synotoken|otp_code|device_id|passkey|authkey|"
    "torrent_pass|token|access_token|api_key|api-key|apikey|did"
)
# A key may carry a prefix ending in `_` (`refresh_token`, `user_password`), never letters
# alone (`insid`, `passwordless` are other words).
# A paging cursor (`page_token`, `next_token`) is no credential: a model told it's `***` would
# page wrongly (QA round 1 on #680).
_PAGING = r"(?!(?:page|next|continuation|cursor)[_-])"
_KEY = rf"{_PAGING}(?:[A-Za-z0-9]{{1,32}}[_-]){{0,3}}(?:{_CREDENTIAL_KEYS})"
# Only where query or form syntax puts an item: after `?`, `&` or `;`. A bare `sid=1.pdf` or
# `/share/token=abc/file.txt` is a name (decided in pre-QA r6 on #514); a logged form body is
# masked by parameter name (`client._log_safe`). A prefix joins with `_` or `-` (`access-token`).
_CREDENTIAL_PARAM = re.compile(rf"""(?i)(?<=[?&;])({_KEY}=)("[^"\n]*"?|'[^'\n]*'?|[^\s&;#"'<>]+)""")
# DSM's CSRF header, as a log line or an error quotes it.
_CREDENTIAL_HEADER = re.compile(r"(?i)(?<![A-Za-z0-9_-])(x-syno-token:[ \t]{0,64})\S+")
# A cookie header, as a log line, a dict repr or JSON writes it (`Cookie: …`, `{'Cookie': '…'}`,
# `"cookie": "…"`): DSM's session is its `id` cookie (#176), and every credential key among the
# `;`-separated items is masked (QA round 1 on #680).
_COOKIE = re.compile(r"""(?i)(cookie['"]?\s{0,64}[:=]\s{0,64}['"]?)([^\n'"]{0,4096})""")
_COOKIE_ITEM = re.compile(rf"(?i)(^|;\s{{0,8}})((?:id|{_KEY})=)([^;\s]*)")
# A JSON member inside a JSON string, its quotes escaped (`{\\"passwd\\":\\"x\\"}`).
_CREDENTIAL_JSON_ESCAPED = re.compile(
    rf"""(?i)(\\"{_KEY}\\"\s{{0,64}}:\s{{0,64}}\\")((?:[^"\\\n]|\\[^"\n])*)"""
)
# A JSON member or a Python repr item (`'passwd': 'x'`), to its closing quote or the line's end:
# a value longer than any cap is masked whole too.
# A number is masked too (`{"otp_code": 123456}`). Not a bare `key: value` in YAML or prose:
# `password: try again` is a sentence as often as a secret.
_CREDENTIAL_JSON = re.compile(
    rf"""(?i)("{_KEY}"\s{{0,64}}:\s{{0,64}})("(?:[^"\\\n]|\\.)*"?|-?\d[\d.eE+-]*)"""
)
_CREDENTIAL_REPR = re.compile(
    rf"""(?i)('{_KEY}'\s{{0,64}}:\s{{0,64}}b?)('(?:[^'\\\n]|\\.)*'?|"(?:[^"\\\n]|\\.)*"?|-?\d[\d.eE+-]*)"""
)
# How far past a `://` the userinfo scan looks for the `@` that ends it.
_USERINFO_WINDOW = 256
# How many `@`s in one window the scan tries, last first: a password holds few, and a cap keeps
# a run of `://@` from costing a window's worth of checks per `@`.
_USERINFO_TRIES = 8
# A character a host can start with, after the `@` that ends userinfo.
_HOST_START = re.compile(r"[A-Za-z0-9\[]")
# An authority with userinfo left in it: `://`, then anything but a path, query, fragment or
# backslash, to its last `@` (the greedy class stops at the first of those).
_AUTHORITY = re.compile(r"(://)([^/?#\s]*)")
# What JSON writes for one character: `\uXXXX`, `\\`, `\/` (`json.dumps` of a URI list writes
# `jörg` as `j\u00f6rg`). Any other backslash before an `@` makes the host ambiguous.
_JSON_ESCAPE = re.compile(r"\\(?:u[0-9A-Fa-f]{4}|[\\/])")
_PORT_THEN_PATH = re.compile(r"\d*[/\\]")


# A query item or path parameter glued to a title: ``?key=value``, ``&key=value``
# or ``;key=value``, with no spaces. "Tom & Jerry" doesn't match, because the
# ``&`` must be followed by ``key=``.
_QUERY_ITEM = re.compile(r"([?&;])([^\s=&?;#]+)=[^\s&;#]*")


def _is_userinfo(span: str) -> bool:
    """Whether ``span``, the text between a ``://`` and an ``@``, reads as userinfo.

    ``user:password`` with a ``/``, ``#`` or ``@`` in the password (#458), as long as the user
    part holds no ``/``, ``?``, ``#`` or bracket (``https://site/a:x@y`` has a path before the
    colon; ``[::1]:5001`` is an IPv6 host), the colon isn't a port before a path
    (``h:8080/@x``) and the user isn't a Windows drive (``file://C:/Users/me@corp``). Without a
    colon, a plain ``user`` with no ``/``, ``?``, ``#`` or bracket (``https://site/@name`` is a
    path). The scan passes a non-empty span with no whitespace.
    """
    user, colon, rest = span.partition(":")
    if not colon:
        return not any(c in "/?#[]" for c in span)
    if any(c in "/?#[]" for c in user) or (len(user) == 1 and rest[:1] in ("/", "\\")):
        return False
    return _PORT_THEN_PATH.match(rest) is None


def _mask_userinfo(text: str) -> str:
    """``text`` with the userinfo of every ``scheme://user:password@host`` masked, found by a
    scan rather than ``urlsplit``, so a password holding a ``/``, ``#`` or ``@`` is masked too
    (#458). The scan runs inside each URL that `_URL_IN_TEXT` matches, so it ends where that
    match does: at whitespace, a quote, ``<`` or ``>``, or a comma before another URI. Prose,
    JSON and URI lists after a host are never read as its userinfo, and a password holding a
    space or a quote isn't masked (#458 keeps the space open). Linear in ``text``."""
    if "@" not in text or "://" not in text:
        return text
    return _URL_IN_TEXT.sub(lambda m: _userinfo_masked(m.group(0)), text)


def _userinfo_masked(url: str) -> str:
    """One matched URL with the userinfo after each of its ``://`` masked (a URL nested in a
    query, `?next=https://u:p@h`, has its own), where `_userinfo_end` finds any."""
    if "@" not in url:
        return url
    out: list[str] = []
    pos = 0
    start = url.find("://")
    while start >= 0:
        begin = start + 3
        # A window of `_USERINFO_WINDOW` characters, ending before the next `://`.
        window = url[begin : begin + _USERINFO_WINDOW].partition("://")[0]
        authority = _AUTHORITY.match(url, start)
        if authority and _ambiguous(authority.group(2)):
            # A backslash before the `@`: a browser reads it as `/` (WHATWG), so which part is the
            # host isn't clear. The whole authority is masked, never left as it is (QA round 1
            # on #680: `CORP\\jdoe:pw@ftp.example` was shown).
            out.append(url[pos:begin])
            out.append(MASK)
            pos = authority.end()
            start = url.find("://", pos)
            continue
        end = _userinfo_end(window)
        if end is not None:
            out.append(url[pos:begin])
            out.append(MASK)
            pos = begin + end
        start = url.find("://", max(begin, pos))
    out.append(url[pos:])
    return "".join(out)


def _userinfo_end(window: str) -> int | None:
    """Where userinfo ends in ``window`` (the text after a ``://``): the first ``@`` that ends
    userinfo and is followed by a whole host; failing that, the last ``@`` that ends userinfo and
    is followed by a host's first character (a password holding ``@``, or a host this can't
    read). At most ``_USERINFO_TRIES`` of each."""
    ats: list[int] = []
    at = window.find("@", 1)
    while at > 0 and len(ats) < _USERINFO_TRIES:
        ats.append(at)
        at = window.find("@", at + 1)
    for at in ats:
        if _WHOLE_HOST.match(window, at + 1) and _is_userinfo(window[:at]):
            return at
    at = window.rfind("@")
    for _ in range(_USERINFO_TRIES):
        if at <= 0:
            return None
        if _HOST_START.match(window, at + 1) and _is_userinfo(window[:at]):
            return at
        at = window.rfind("@", 0, at)
    return None


def _ambiguous(authority: str) -> bool:
    """Whether a backslash that isn't a JSON escape comes before ``authority``'s last ``@``."""
    user, at, _ = authority.rpartition("@")
    return bool(at) and "\\" in _JSON_ESCAPE.sub("", user)


def _mask_wrapped(text: str) -> str:
    """``text`` with every ``thunder://``, ``flashget://`` or ``qqdl://`` link masked whole."""
    return _WRAPPED_IN_TEXT.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)


def redact_secrets(text: str) -> str:
    """``text`` with credentials masked and everything else kept (#514).

    URL userinfo (``scheme://***@host``), the value of a credential parameter (``passwd``,
    ``_sid``, ``SynoToken``, ``otp_code``, ``device_id``, a tracker passkey; as ``key=value`` or
    a JSON member), and wrapped download links (``thunder://***``). Unlike ``redact_urls``,
    query values, path tokens and other URL parts are kept: a Photos share link's passphrase is
    what the user asked for.
    """
    text = _mask_userinfo(_mask_wrapped(text))
    # Userinfo longer than the scan's window (a long token as the password): each URL's own
    # authority, up to its first `/`, `?` or `#`, is masked to its last `@`.
    text = _URL_IN_TEXT.sub(lambda m: _mask_authority(m.group(0)), text)
    text = _CREDENTIAL_PARAM.sub(
        lambda m: m.group(1) + _masked_value(m.group(2), m.group(2)[:1]), text
    )
    text = _CREDENTIAL_HEADER.sub(lambda m: f"{m.group(1)}{MASK}", text)
    text = _COOKIE.sub(
        lambda m: (
            m.group(1) + _COOKIE_ITEM.sub(lambda i: f"{i.group(1)}{i.group(2)}{MASK}", m.group(2))
        ),
        text,
    )
    text = _CREDENTIAL_JSON_ESCAPED.sub(lambda m: f"{m.group(1)}{MASK}", text)
    text = _CREDENTIAL_JSON.sub(lambda m: m.group(1) + _masked_value(m.group(2), '"'), text)
    return _CREDENTIAL_REPR.sub(
        lambda m: m.group(1) + _masked_value(m.group(2), m.group(2)[:1]), text
    )


def _mask_authority(url: str) -> str:
    """``url`` with any userinfo still in an authority masked: each ``://``'s authority up to its
    first ``/``, ``?`` or ``#``, to its last ``@`` (a URL nested in a query too). An ambiguous
    authority (`_ambiguous`) was masked whole by `_mask_userinfo` already."""

    def masked(m: re.Match[str]) -> str:
        authority = m.group(2)
        # Even after an earlier mask (`***@ss,1@nas`): only what follows the last `@` stays.
        if "@" not in authority:
            return m.group(0)
        return f"{m.group(1)}{MASK}@{authority.rpartition('@')[2]}"

    return _AUTHORITY.sub(masked, url)


# A value that is only a placeholder in angle brackets.
# The placeholders this server prints for the user to fill in (`setup`'s client snippet); a
# real value in angle brackets (`<hunter2>`) is still masked.
_PLACEHOLDERS = frozenset({"<your password>"})


def _masked_value(value: str, quote: str) -> str:
    """A JSON or repr value masked, keeping its quotes (one if it had only one), or a number.
    A placeholder (`"<your password>"`, as `setup`'s client snippet prints) is kept: it holds no
    secret, and the user fills it in."""
    if value.strip("'\"") in _PLACEHOLDERS:
        return value
    if quote not in ("'", '"') or not value.startswith(quote):
        return MASK
    closed = len(value) > 1 and value.endswith(quote) and not value.endswith("\\" + quote)
    return f"{quote}{MASK}{quote}" if closed else f"{quote}{MASK}"


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
    """Return ``url`` with secrets masked. Text with no ``://`` is returned as it is, except an
    ed2k link without ``//`` (``ed2k:foo?token=…``), whose query is masked and fragment dropped."""
    if url[: len("magnet:?")].lower() == "magnet:?":  # any case, as DSM may accept it
        return _redact_magnet(url)
    scheme, colon, rest = url.partition(":")
    if colon and scheme.lower() in _WRAPPED:
        body = _WRAPPED_BODY.match(rest)
        # The body ends at punctuation (`thunder://…=.`); without `//` it isn't a link.
        if body:
            return f"{scheme}://{MASK}{rest[body.end() :]}"
    if "://" not in url:
        if colon and scheme.lower() == "ed2k" and any(c in rest for c in "?&#"):
            # An opaque link such as `ed2k:foo?token=…` or `ed2k:foo&token=…`: its query is
            # masked as a URL's (#466).
            before, q, query = url.partition("#")[0].partition("?")
            before = _AMP_ITEM.sub(lambda m: f"&{m.group(1)}={MASK}", before)
            return f"{before}?{_redact_query(query.partition('#')[0])}" if q else before
        return url
    url = _mask_userinfo(url)
    try:
        parts = urlsplit(url)
    except ValueError:
        return MASK
    if not parts.scheme or not parts.netloc:
        return url

    netloc = parts.netloc
    # Not when a backslash comes before the `@`: a browser reads `https://evil\\@nas` as evil's
    # host, so masking it would show `nas` as the host (WHATWG).
    if "@" in netloc:
        netloc = MASK if _ambiguous(netloc) else f"{MASK}@{netloc.rpartition('@')[2]}"
    # A query glued to the host once a comma split the URL (`b://c&token=…`, #466): its items
    # are masked as a query's. Only `&key=value`: an ed2k link's `|file|name|…|` netloc, or a
    # host before prose punctuation (`https://nas.local).`), is left as it is.
    netloc = _NETLOC_ITEM.sub(lambda m: f"{m.group(1)}{m.group(2)}={MASK}", netloc)
    path = "/".join(_redact_segment(seg) for seg in parts.path.split("/"))
    path = _AMP_ITEM.sub(lambda m: f"&{m.group(1)}={MASK}", path)
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
