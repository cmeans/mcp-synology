"""A fake Synology Photos built from the live captures in ``fixtures/`` (#258).

``FakePhotos`` answers each DSM request by its ``api``/``method``/``version``: a
request nothing was registered for fails the test (``AssertionError``), so a wrong
API, method or pinned version can't pass by getting a canned body, the gap behind
#123. Requests are recorded with their values JSON-decoded, which also checks the
Photos rule that every value is JSON-encoded (an unquoted string fails to decode).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import respx

from mcp_synology.core.client import DsmClient
from mcp_synology.core.state import ApiInfoEntry
from tests.conftest import BASE_URL

_DIR = Path(__file__).parent / "fixtures"
CAPTURE_134: dict[str, Any] = json.loads((_DIR / "dsm-7.1-photos-1.3.4.json").read_text())
CAPTURE_191: dict[str, Any] = json.loads((_DIR / "dsm-7.2.2-photos-1.9.1.json").read_text())
PEOPLE: dict[str, Any] = json.loads((_DIR / "dsm-7.1.1-photos-1.3.4-people.json").read_text())


def captured(key: str, capture: dict[str, Any] = CAPTURE_134) -> Any:
    """The ``data`` of one captured reply (deep copy)."""
    return copy.deepcopy(capture[key]["response"].get("data", {}))


def api_cache(capture: dict[str, Any] = CAPTURE_134) -> dict[str, ApiInfoEntry]:
    """The NAS's real ``SYNO.API.Info`` for the captured Photos version."""
    return {
        name: ApiInfoEntry(
            path=info["path"],
            min_version=info["minVersion"],
            max_version=info["maxVersion"],
            request_format=info.get("requestFormat"),
        )
        for name, info in capture["api_info"].items()
    }


Reply = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any] | httpx.Response]


@dataclass
class Call:
    api: str
    method: str
    version: int
    params: dict[str, Any]
    http: str


@dataclass
class FakePhotos:
    """Registered replies by (api, method, version); every request recorded."""

    routes: dict[tuple[str, str, int], list[Reply]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def on(self, api: str, method: str, version: int, *replies: Reply) -> FakePhotos:
        """Answer with ``replies`` in turn (the last one repeats). A dict is the ``data``."""
        self.routes[(api, method, version)] = list(replies)
        return self

    def error(self, api: str, method: str, version: int, code: int, **extra: Any) -> FakePhotos:
        return self.on(api, method, version, lambda _p: _err(code, **extra))

    def of(self, api: str, method: str | None = None) -> list[Call]:
        return [c for c in self.calls if c.api == api and (method is None or c.method == method)]

    def one(self, api: str, method: str) -> Call:
        found = self.of(api, method)
        assert len(found) == 1, f"{api}/{method} sent {len(found)} times"
        return found[0]

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            ctype = request.headers.get("content-type", "")
            assert ctype.startswith("application/x-www-form-urlencoded"), ctype
            raw = dict(parse_qsl(request.content.decode(), keep_blank_values=True))
            assert not request.url.params, "a form POST has no query string"
        else:
            raw = dict(request.url.params)
        # The session goes as the id cookie, never as _sid (#176).
        assert "_sid" not in raw, "the session went as _sid"
        api, method, version = raw.pop("api"), raw.pop("method"), int(raw.pop("version"))
        params = {k: json.loads(v) for k, v in raw.items()}
        self.calls.append(Call(api, method, version, params, request.method))
        replies = self.routes.get((api, method, version))
        assert replies, f"unexpected DSM request {api}/{method} v{version} {params}"
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        result = (
            reply(params) if callable(reply) else {"success": True, "data": copy.deepcopy(reply)}
        )
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=result)


def _err(code: int, **extra: Any) -> dict[str, Any]:
    return {"success": False, "error": {"code": code, **extra}}


def ok(data: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A reply that ignores the request."""
    return lambda _p: {"success": True, "data": copy.deepcopy(data)}


def binary(content: bytes, ctype: str = "image/jpeg") -> Callable[[dict[str, Any]], httpx.Response]:
    return lambda _p: httpx.Response(200, content=content, headers={"content-type": ctype})


def make_client(capture: dict[str, Any] = CAPTURE_134) -> DsmClient:
    client = DsmClient(base_url=BASE_URL)
    client._api_cache = api_cache(capture)
    client.sid = "sid"
    return client


def install(fake: FakePhotos) -> None:
    """Route every /webapi/ request of the active ``respx`` mock to ``fake``."""
    respx.route(url__startswith=f"{BASE_URL}/webapi/").mock(side_effect=fake.handle)
