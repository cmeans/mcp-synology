"""GET vs POST vs session cookie on DSM's File Station and Download Station APIs (#263).

The project once recorded that "POST causes silent failures on DSM 7.1" for File
Station, without saying which call failed. #121 later traced many silent failures of
that time to unquoted JSON values. These tests settle the claim:

- here, the same calls are sent in every mode of ``tests/vdsm/transport.py`` and DSM's
  replies must match the GET reply;
- the whole File Station and Download Station vdsm suite can also run with every
  request rewritten (``--dsm-transport post``, ``cookie_get``, …), so its own state
  checks catch a write DSM accepts but doesn't do.

Run: ``uv run pytest tests/vdsm -m vdsm --dsm-version 7.1 -k transport``
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mcp_synology.modules.filestation.helpers import dsm_str
from tests.vdsm.golden_image import download_station_skip_reason, load_golden_meta
from tests.vdsm.transport import MODES, RewritingTransport

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

pytestmark = pytest.mark.vdsm


async def _send(
    client: DsmClient,
    mode: str,
    api: str,
    method: str,
    version: int,
    params: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any] | None, RewritingTransport]:
    """Send one call as GET with ``_sid`` in the query, rewritten by ``mode``."""
    info = client.api_cache[api]
    transport = RewritingTransport(httpx.AsyncHTTPTransport(), mode)
    query = {"api": api, "method": method, "version": str(version), **(params or {})}
    query["_sid"] = client.sid or ""
    async with httpx.AsyncClient(transport=transport, timeout=60) as http:
        resp = await http.get(f"{client._base_url}/webapi/{info.path}", params=query)
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body, transport


@pytest.fixture
async def sid_client(nas_client: Any) -> Any:
    """``nas_client``, with a session ID DSM reads correctly as ``_sid`` (#176).

    These tests send the session as ``_sid`` themselves. DSM misreads some session IDs
    there (about 1 login in 75 on 7.2.2), and such a session gets 119 in every mode
    but the cookie, so a test drawing one would fail for that reason alone. Log in
    again until the ID works.
    """
    client, auth, _config, _paths = nas_client
    for _ in range(20):
        _, body, _ = await _send(client, "get", "SYNO.FileStation.Info", "get", 2)
        if body and body.get("success"):
            return nas_client
        assert body and (body.get("error") or {}).get("code") == 119, body
        await auth.logout()
        await auth.login()
    pytest.fail("20 logins in a row drew a session ID DSM misreads as _sid")


def _shape(body: dict[str, Any] | None) -> Any:
    """What must match across modes: success, the error code, and the data's keys."""
    if body is None:
        return None
    data = body.get("data")
    return (
        body.get("success"),
        (body.get("error") or {}).get("code"),
        sorted(data) if isinstance(data, dict) else type(data).__name__,
    )


# Reads every persona can make: (api, method, version, params).
_READS: list[tuple[str, str, int, dict[str, str]]] = [
    ("SYNO.FileStation.Info", "get", 2, {}),
    ("SYNO.FileStation.List", "list_share", 2, {"additional": '["real_path"]'}),
    ("SYNO.FileStation.List", "list", 2, {"folder_path": "/testshare", "limit": "5"}),
    ("SYNO.FileStation.List", "getinfo", 2, {"path": "/testshare"}),
    ("SYNO.FileStation.List", "list", 2, {"folder_path": "/testshare", "pattern": dsm_str("2024")}),
    ("SYNO.DSM.Info", "getinfo", 2, {}),
]


@pytest.mark.parametrize("mode", [m for m in MODES if m != "get"])
async def test_reads_match_get(sid_client: Any, mode: str) -> None:
    """Every mode gives GET's reply for the same read, and really is rewritten."""
    client, _auth, _config, _paths = sid_client
    assert not client._get_http().cookies, "login set a cookie, so cookie modes prove nothing"
    for api, method, version, params in _READS:
        if api not in client.api_cache:
            continue
        get_status, get_body, _ = await _send(client, "get", api, method, version, params)
        status, body, transport = await _send(client, mode, api, method, version, params)
        label = f"{api}/{method} via {mode}"
        assert (status, _shape(body)) == (get_status, _shape(get_body)), label
        assert transport.sent, f"{label}: nothing was rewritten"
        if mode.startswith("cookie"):
            assert set(transport.sent) <= {("GET", "cookie"), ("POST", "cookie")}, label
        else:
            assert set(transport.sent) == {("POST", "body" if mode == "post_sid_body" else "query")}


@pytest.mark.parametrize("mode", [m for m in MODES if m != "get"])
async def test_write_takes_effect(sid_client: Any, mode: str) -> None:
    """A CreateFolder, Rename and Delete sent in ``mode`` change the NAS (read back by GET)."""
    client, _auth, _config, paths = sid_client
    parent = paths.get("writable_folder", "/writable")
    name = f"t263-{mode}-{uuid.uuid4().hex[:6]}"

    _, body, _ = await _send(
        client, mode, "SYNO.FileStation.CreateFolder", "create", 2,
        {"folder_path": parent, "name": dsm_str(name)},  # as create_folder sends them
    )  # fmt: skip
    assert body and body.get("success"), body
    _, seen, _ = await _send(
        client, "get", "SYNO.FileStation.List", "getinfo", 2, {"path": f"{parent}/{name}"}
    )
    assert seen and seen.get("success"), f"read-back after create via {mode}: {seen}"
    assert "code" not in seen["data"]["files"][0], f"create via {mode} had no effect"

    renamed = f"{name}-r"
    _, body, _ = await _send(
        client, mode, "SYNO.FileStation.Rename", "rename", 2,
        {"path": f"{parent}/{name}", "name": dsm_str(renamed)},
    )  # fmt: skip
    assert body and body.get("success"), body
    _, seen, _ = await _send(
        client, "get", "SYNO.FileStation.List", "getinfo", 2, {"path": f"{parent}/{renamed}"}
    )
    assert seen and seen.get("success"), f"read-back after rename via {mode}: {seen}"
    assert "code" not in seen["data"]["files"][0], f"rename via {mode} had no effect"

    _, body, _ = await _send(
        client, mode, "SYNO.FileStation.Delete", "delete", 2,
        {"path": f"{parent}/{renamed}", "recursive": "true"},
    )  # fmt: skip
    assert body and body.get("success"), body
    _, seen, _ = await _send(
        client, "get", "SYNO.FileStation.List", "getinfo", 2, {"path": f"{parent}/{renamed}"}
    )
    assert seen and seen.get("success"), f"read-back after delete via {mode}: {seen}"
    assert seen["data"]["files"][0].get("code") == 408, f"delete via {mode} had no effect"


async def test_unquoted_values_fail_the_same_over_post(sid_client: Any) -> None:
    """An unquoted JSON-looking value fails alike over GET and POST (#121's cause)."""
    client, _auth, _config, paths = sid_client
    parent = paths.get("writable_folder", "/writable")
    params = {"folder_path": parent, "name": "2024"}  # 2024 unquoted: DSM reads a number
    results = {}
    for mode in ("get", "post"):
        status, body, _ = await _send(
            client, mode, "SYNO.FileStation.CreateFolder", "create", 2, params
        )
        results[mode] = (status, _shape(body))
    assert results["post"] == results["get"], results
    if results["get"][1] and results["get"][1][0]:  # created after all: clean up
        await _send(
            client, "get", "SYNO.FileStation.Delete", "delete", 2,
            {"path": f"{parent}/2024", "recursive": "true"},
        )  # fmt: skip


async def test_post_needs_api_method_version_in_body(sid_client: Any) -> None:
    """A form POST with api/method/version only in the query string, empty body: 101.

    The rewrite puts them in the body; this pins why (also seen on Photos, #258).
    """
    client, _auth, _config, _paths = sid_client
    info = client.api_cache["SYNO.FileStation.Info"]
    query = {"api": "SYNO.FileStation.Info", "method": "get", "version": "2", "_sid": client.sid}
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(f"{client._base_url}/webapi/{info.path}", params=query, data={})
    body = resp.json()
    assert body.get("success") is False and body["error"]["code"] == 101, body


@pytest.mark.parametrize(
    ("mode", "ok"), [("post", False), ("post_sid_body", True), ("cookie_post", True)]
)
async def test_download_station_v1_post_needs_sid_out_of_the_query(
    sid_client: Any,
    dsm_version: str,
    request: pytest.FixtureRequest,
    live_clients: set[DsmClient],
    mode: str,
    ok: bool,
) -> None:
    """Download Station's v1 CGIs ignore ``_sid`` in the query string of a POST (#263).

    They answer 105 (permission denied), which never triggers a re-auth, so it reads
    as a permissions problem; File Station's ``entry.cgi`` reads it fine. In the body
    or as the ``id`` cookie, the same POST succeeds. This is the real failure behind
    the old "POST fails on DSM 7.1" note.
    """
    client, _auth, _config, _paths = sid_client
    # As test_vdsm_downloadstation's _ds_available fixture: skip without DS, wait for it.
    reason = download_station_skip_reason(
        load_golden_meta(dsm_version), await client.query_api_info()
    )
    if reason is not None:
        pytest.skip(reason)
    request.getfixturevalue("ds_backend_ready")
    for open_client in list(live_clients):
        await open_client.query_api_info()
    _, get_body, _ = await _send(client, "get", "SYNO.DownloadStation.Info", "getinfo", 1)
    assert get_body and get_body.get("success"), get_body
    _, body, _ = await _send(client, mode, "SYNO.DownloadStation.Info", "getinfo", 1)
    if ok:
        assert _shape(body) == _shape(get_body), body
    else:
        assert body and body.get("success") is False and body["error"]["code"] == 105, body
