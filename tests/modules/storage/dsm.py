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
    cache = {
        api: ApiInfoEntry(path="entry.cgi", min_version=1, max_version=1, request_format="JSON")
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
        self.overrides: list[tuple[dict[str, str], dict[str, Any]]] = []
        self.unmatched: list[dict[str, str]] = []
        self.seen: list[dict[str, str]] = []

    def answer(self, match: dict[str, Any], reply: dict[str, Any]) -> None:
        """Answer requests carrying ``match`` with ``reply`` (checked before captures)."""
        self.overrides.append(({k: _as_query(v) for k, v in match.items()}, reply))

    def _reply(self, query: dict[str, str]) -> dict[str, Any]:
        for match, reply in self.overrides:
            if all(query.get(k) == v for k, v in match.items()):
                return copy.deepcopy(reply)
        for request, reply in self.routes:
            if request == query:
                return copy.deepcopy(reply)
        self.unmatched.append(query)
        return {"success": False, "error": {"code": 599}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params)
        self.seen.append(query)
        return httpx.Response(200, json=self._reply(query))

    def calls(self, api: str, method: str) -> list[dict[str, str]]:
        return [q for q in self.seen if q.get("api") == api and q.get("method") == method]


async def make_client(replay: Replay, **cache_kwargs: Any) -> DsmClient:
    client = DsmClient(base_url=BASE_URL)
    client._api_cache = api_cache(**cache_kwargs)
    respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=replay.handler)
    return client
