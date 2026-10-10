"""Real-DSM payloads and api/method-keyed routes for Download Station tests.

Payloads come from ``fixtures/dsm-7.2.2-ds-4.1-5012.json``, a live capture
(see its ``_provenance`` key). Routes match on the DSM ``api`` and ``method``
(and ``version`` when given) so a wrong API name, method, or pinned version
fails the test with an unmatched-request error instead of silently getting a
canned body — the gap that let #123's bugs through.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import respx

from tests.conftest import BASE_URL, make_api_cache

_CAPTURE: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "dsm-7.2.2-ds-4.1-5012.json").read_text()
)


def captured(key: str) -> dict[str, Any]:
    """Return a deep copy of one captured DSM response envelope."""
    return copy.deepcopy(_CAPTURE[key])


def ok(data: Any = None) -> dict[str, Any]:
    """A DSM success envelope; ``data`` omitted means no ``data`` key (like v1 create)."""
    body: dict[str, Any] = {"success": True}
    if data is not None:
        body["data"] = data
    return body


def err(code: int) -> dict[str, Any]:
    """A DSM error envelope."""
    return {"success": False, "error": {"code": code}}


def _url(api: str) -> str:
    return f"{BASE_URL}/webapi/{make_api_cache()[api].path}"


def get_route(api: str, method: str, *, version: int | None = None) -> respx.Route:
    """Route a GET for ``api``/``method`` (and ``version`` if given) at its real CGI path."""
    params = {"api": api, "method": method}
    if version is not None:
        params["version"] = str(version)
    return respx.get(_url(api), params__contains=params)


def post_route(api: str, method: str, *, version: int | None = None) -> respx.Route:
    """Route a form POST whose body carries ``api``/``method`` (and ``version``)."""
    data = {"api": api, "method": method}
    if version is not None:
        data["version"] = str(version)
    return respx.post(_url(api), data__contains=data)
