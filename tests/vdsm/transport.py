"""Test-only httpx transport that resends DSM requests another way (#263).

The project sends File Station and Download Station calls as GET. #263 checks the old
claim that POST fails silently on DSM 7.1 by running the real module code with every
request rewritten underneath it, so the vdsm suites' own state checks (a copy that
exists, a delete that removed the files) catch any call DSM accepts but ignores.

The session is taken from wherever the request carries it, ``_sid`` in the query
string or the ``id`` cookie (the client's way since #176), and each mode puts it in one
place:

- ``get``: no change.
- ``post``: a GET becomes a form POST: ``api``/``method``/``version`` and the other
  parameters go in the body, the session as ``_sid`` in the query string.
- ``post_sid_body``: as ``post``, with ``_sid`` in the body instead (also for requests
  that were already form POSTs).
- ``cookie_get``: the session as the ``id`` cookie, as DSM's own web clients and, since
  #176, the client send it. The same as ``get`` for today's client.
- ``cookie_post``: ``post`` with the cookie, so the URL carries no parameters at all.

``post`` and ``post_sid_body`` send the session as ``_sid``, so on JSON-format APIs they
meet #176's misread session IDs (about 1 login in 75 on DSM 7.2.2): a 119, then a
re-auth, which fails a test that asserts none.

Multipart uploads keep their body (DSM doesn't read ``_sid`` from multipart form
data); outside the cookie modes their session goes as ``_sid`` in the query string.
Other cookies are kept.
"""

from __future__ import annotations

from collections import Counter
from urllib.parse import parse_qsl, urlencode

import httpx

MODES = ("get", "post", "post_sid_body", "cookie_get", "cookie_post")

_FORM = "application/x-www-form-urlencoded"


def _set_cookie(headers: httpx.Headers, value: str) -> None:
    """Replace the ``Cookie`` header, or drop it when nothing is left."""
    if value:
        headers["cookie"] = value
    elif "cookie" in headers:
        del headers["cookie"]


class RewritingTransport(httpx.AsyncBaseTransport):
    """Wrap a transport and rewrite each ``/webapi/`` request according to ``mode``."""

    def __init__(self, inner: httpx.AsyncBaseTransport, mode: str) -> None:
        if mode not in MODES:
            msg = f"unknown transport mode {mode!r}; expected one of {MODES}"
            raise ValueError(msg)
        self._inner = inner
        self.mode = mode
        # How many requests went out in each final shape, e.g. ("POST", "cookie"): a
        # test asserts the rewrite really happened, so a no-op can't pass for a result.
        self.sent: Counter[tuple[str, str]] = Counter()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        rewritten = self._rewrite(request)
        return await self._inner.handle_async_request(rewritten)

    async def aclose(self) -> None:
        await self._inner.aclose()

    def _rewrite(self, request: httpx.Request) -> httpx.Request:
        if self.mode == "get" or not request.url.path.startswith("/webapi/"):
            return request
        query = list(request.url.params.multi_items())
        sid = next((v for k, v in query if k == "_sid"), None)
        query = [(k, v) for k, v in query if k != "_sid"]
        others: list[str] = []
        for part in request.headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "id":
                sid = sid or value
            elif name:
                others.append(f"{name}={value}")
        ctype = request.headers.get("content-type", "")
        multipart = ctype.startswith("multipart/")
        cookie = self.mode in ("cookie_get", "cookie_post") and sid is not None
        to_post = self.mode in ("post", "post_sid_body", "cookie_post")
        jar = "; ".join([f"id={sid}", *others] if cookie else others)

        url = request.url.copy_with(query=None)

        if multipart or (request.method == "GET" and not to_post):
            # Body untouched (multipart, or a GET that stays a GET): only the session
            # moves. Edited in place: a multipart body is a stream that can't be re-read.
            keep = query if cookie or sid is None else [*query, ("_sid", sid)]
            request.url = url.copy_with(query=urlencode(keep).encode() or None)
            _set_cookie(request.headers, jar)
            self.sent[(request.method, "cookie" if cookie else "query")] += 1
            return request

        headers = httpx.Headers(request.headers)
        _set_cookie(headers, jar)

        # Form POST: what was in the query (and in any existing form body) goes in the
        # body, except _sid, which stays in the query unless the mode says otherwise.
        body = query
        if request.method == "POST" and ctype.startswith(_FORM):
            body = [*parse_qsl(request.content.decode(), keep_blank_values=True), *query]
        url_query: list[tuple[str, str]] = []
        if sid is not None and not cookie:
            if self.mode == "post_sid_body":
                body = [*body, ("_sid", sid)]
            else:
                url_query = [("_sid", sid)]
        content = urlencode(body).encode()
        headers["content-type"] = _FORM
        headers["content-length"] = str(len(content))
        where = "cookie" if cookie else ("body" if self.mode == "post_sid_body" else "query")
        self.sent[("POST", where if sid is not None else "none")] += 1
        return httpx.Request(
            "POST",
            url.copy_with(query=urlencode(url_query).encode() or None),
            headers=headers,
            content=content,
            extensions=request.extensions,
        )
