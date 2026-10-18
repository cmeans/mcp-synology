"""Test-only httpx transport that resends DSM requests another way (#263).

The project sends DSM calls as GET with ``_sid`` in the query string. #263 checks the
old claim that POST fails silently on DSM 7.1 by running the real module code with
every request rewritten underneath it, so the vdsm suites' own state checks (a copy
that exists, a delete that removed the files) catch any call DSM accepts but ignores.

Modes:

- ``get``: no change.
- ``post``: a GET becomes a form POST: ``api``/``method``/``version`` and the other
  parameters go in the body, ``_sid`` stays in the query string.
- ``post_sid_body``: as ``post``, with ``_sid`` in the body too (also for requests that
  were already form POSTs).
- ``cookie_get``: ``_sid`` leaves the query string; the session goes in the ``id``
  cookie, as DSM's own web clients send it.
- ``cookie_post``: ``post`` plus the cookie, so the URL carries no parameters at all.

Multipart uploads keep their body (DSM doesn't read ``_sid`` from multipart form
data, ``DsmClient.upload``); in the cookie modes their ``_sid`` moves to the cookie.
"""

from __future__ import annotations

from collections import Counter
from urllib.parse import parse_qsl, urlencode

import httpx

MODES = ("get", "post", "post_sid_body", "cookie_get", "cookie_post")

_FORM = "application/x-www-form-urlencoded"


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
        ctype = request.headers.get("content-type", "")
        multipart = ctype.startswith("multipart/")
        cookie = self.mode in ("cookie_get", "cookie_post") and sid is not None
        to_post = self.mode in ("post", "post_sid_body", "cookie_post")

        url = request.url.copy_with(query=None)

        if multipart or (request.method == "GET" and not to_post):
            # Body untouched (multipart, or a GET that stays a GET): only _sid moves.
            # Edited in place: a multipart body is a stream that can't be re-read.
            keep = query if cookie or sid is None else [*query, ("_sid", sid)]
            request.url = url.copy_with(query=urlencode(keep).encode() or None)
            if cookie:
                request.headers["cookie"] = f"id={sid}"
            self.sent[(request.method, "cookie" if cookie else "query")] += 1
            return request

        headers = httpx.Headers(request.headers)
        if cookie:
            headers["cookie"] = f"id={sid}"

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
