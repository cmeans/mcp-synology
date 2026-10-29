"""Replay DSM's captured storage replies (``fixtures/``) at the HTTP boundary.

Each request is answered with the capture whose request has the same ``api``,
``method``, ``version`` and other parameters, so a wrong method, version or
parameter encoding fails the test (an unmatched request answers 599 and is
recorded in ``unmatched``) instead of getting a canned body.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import respx

from mcp_synology.core.client import DsmClient
from mcp_synology.core.state import ApiInfoEntry
from tests.conftest import BASE_URL

FIXTURES = Path(__file__).parent / "fixtures"
REALNAS = "dsm-7.1.1-ds1618plus-realnas.json"
VDSM = ["dsm-7.1-vdsm.json", "dsm-7.2.2-vdsm.json", "dsm-7.3.2-vdsm.json", "dsm-7.4.1-vdsm.json"]

STORAGE_APIS = [
    "SYNO.Storage.CGI.Storage", "SYNO.Storage.CGI.Smart", "SYNO.Core.Storage.Disk",
    "SYNO.Storage.CGI.Volume", "SYNO.Storage.CGI.Pool", "SYNO.Storage.CGI.Smart.Scheduler",
    "SYNO.Storage.CGI.Spare", "SYNO.Storage.CGI.Spare.Conf", "SYNO.Storage.CGI.HddMan",
    "SYNO.Storage.CGI.Flashcache", "SYNO.Storage.CGI.BtrfsDedupe", "SYNO.Storage.CGI.Check",
    "SYNO.Core.ExternalDevice.Storage.USB", "SYNO.Core.ExternalDevice.Storage.eSATA",
    "SYNO.Core.ExternalDevice.Storage.EUnit", "SYNO.Core.ExternalDevice.Storage.Setting",
    "SYNO.Core.Share", "SYNO.Core.System.SystemHealth",
]  # fmt: skip


def api_cache(*, snapshot_max: int = 2) -> dict[str, ApiInfoEntry]:
    # Max version 3, above anything captured: a call that drops its v1 pin goes out as v3,
    # matches no capture, and fails the test.
    cache = {
        api: ApiInfoEntry(path="entry.cgi", min_version=1, max_version=3, request_format="JSON")
        for api in STORAGE_APIS
    }
    cache["SYNO.Core.Share.Snapshot"] = ApiInfoEntry(
        path="entry.cgi", min_version=1, max_version=snapshot_max, request_format="JSON"
    )
    return cache


def _as_query(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value)


class Replay:
    """Captured replies for one persona (``admin`` or ``test``) of one fixture."""

    def __init__(self, fixture: str, persona: str = "admin") -> None:
        captures = json.loads((FIXTURES / fixture).read_text())
        self.routes: list[tuple[dict[str, str], dict[str, Any]]] = []
        for label, capture in captures.items():
            if not label.startswith(f"{persona}:") or not isinstance(capture, dict):
                continue
            request = {k: _as_query(v) for k, v in capture["request"].items()}
            self.routes.append((request, capture["reply"]))
        self.overrides: list[tuple[dict[str, str], dict[str, Any] | int]] = []
        self.unmatched: list[dict[str, str]] = []
        self.seen: list[dict[str, str]] = []

    def answer(self, match: dict[str, Any], reply: dict[str, Any] | int) -> None:
        """Answer requests carrying ``match`` with ``reply`` (checked before captures).

        An int is an HTTP status with no DSM reply (a proxy's 502).
        """
        self.overrides.append(({k: _as_query(v) for k, v in match.items()}, reply))

    def _reply(self, query: dict[str, str]) -> dict[str, Any] | int:
        for match, reply in self.overrides:
            if all(query.get(k) == v for k, v in match.items()):
                return reply if isinstance(reply, int) else copy.deepcopy(reply)
        for request, reply in self.routes:
            if request == query:
                return copy.deepcopy(reply)
        self.unmatched.append(query)
        return {"success": False, "error": {"code": 599}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params)
        self.seen.append(query)
        reply = self._reply(query)
        if isinstance(reply, int):
            return httpx.Response(reply, text="<html>Bad Gateway</html>")
        return httpx.Response(200, json=reply)

    def calls(self, api: str, method: str) -> list[dict[str, str]]:
        return [q for q in self.seen if q.get("api") == api and q.get("method") == method]


async def make_client(replay: Replay, **cache_kwargs: Any) -> DsmClient:
    client = DsmClient(base_url=BASE_URL)
    client._api_cache = api_cache(**cache_kwargs)
    respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=replay.handler)
    return client


def ok(data: Any = None) -> dict[str, Any]:
    return {"success": True, "data": data if data is not None else {}}


def err(code: int) -> dict[str, Any]:
    return {"success": False, "error": {"code": code}}


async def run(
    fn: Any,
    fixture: str = REALNAS,
    *,
    persona: str = "admin",
    setup: Any = None,
    cache: dict[str, Any] | None = None,
    **kwargs: Any,
) -> tuple[str, Replay]:
    """Call a storage tool function against a fixture; returns its text and the replay.

    ``setup(replay, client)`` can add overrides or change the API cache first.
    """
    replay = Replay(fixture, persona)
    with respx.mock:
        client = await make_client(replay, **(cache or {}))
        async with client:
            if setup is not None:
                setup(replay, client)
            text = await fn(client, **kwargs)
    return text, replay


async def run_error(
    fn: Any, fixture: str = REALNAS, *, replay_out: list[Replay] | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Call a tool that must fail; returns its error envelope's ``error``.

    ``replay_out``, when given, receives the replay, to check which calls were made.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    setup = kwargs.pop("setup", None)

    def keep(replay: Replay, client: Any) -> None:
        if replay_out is not None:
            replay_out.append(replay)
        if setup is not None:
            setup(replay, client)

    try:
        await run(fn, fixture, setup=keep, **kwargs)
    except ToolError as e:
        error: dict[str, Any] = json.loads(str(e))["error"]
        return error
    raise AssertionError("the tool didn't fail")


def flat(text: str) -> str:
    """Text with runs of whitespace collapsed, so assertions don't depend on padding."""
    return " ".join(text.split())


def load_info_with(replay: Replay, change: Any) -> None:
    """Answer load_info with the fixture's own reply after ``change(data)`` edits it."""
    reply = next(r for q, r in replay.routes if q.get("method") == "load_info")
    reply = copy.deepcopy(reply)
    change(reply["data"])
    replay.answer({"api": "SYNO.Storage.CGI.Storage", "method": "load_info"}, reply)
