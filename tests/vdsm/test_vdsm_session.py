"""The session reaches DSM as the ``id`` cookie, so every session ID works (#176).

DSM parses JSON-format parameter values as JSON first (#121), ``_sid`` included. Some
session IDs (about 1 in 75 on DSM 7.2.2 and 1 in 170 on 7.1, measured on vdsm) begin with
a shape that parser misreads (``-8bV…``, ``6E5h…``, ``02-P…``); sent as ``_sid`` in the
query string, such a session matches nothing and every call gets 119, on both versions.
That was #176's "flaky 119 on the first call after login". The client sends the session
as the ``id`` cookie instead.

Run: ``uv run pytest tests/vdsm -m vdsm --dsm-version 7.2.2 -k session``
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.personas import reauth_spy_on

pytestmark = pytest.mark.vdsm

# Enough logins that a misread session ID turns up. At 7.1's rate (6 in 1,000) the
# chance of none in 3,000 is about 1e-8 (0.994 ** 3000); a run usually needs a few hundred.
_MAX_LOGINS = 3000


async def _sid_in_query_works(client: Any, sid: str) -> bool:
    """Send one call with ``_sid`` in the query string, as the client used to."""
    path = client.api_cache["SYNO.FileStation.List"].path
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(
            f"{client._base_url}/webapi/{path}",
            params={
                "api": "SYNO.FileStation.List",
                "method": "list_share",
                "version": "2",
                "_sid": sid,
            },
        )
    body = resp.json()
    if body.get("success"):
        return True
    assert body["error"]["code"] == 119, body
    return False


async def test_a_session_id_dsm_misreads_as_sid_still_works(nas_client: Any) -> None:
    """Log in until DSM hands out a session ID that fails as ``_sid``; the client's
    calls with that same session work, with no re-auth."""
    client, auth, _config, _paths = nas_client
    tried = 0
    for tried in range(1, _MAX_LOGINS + 1):  # noqa: B007 — reported below
        await auth.logout()
        sid = await auth.login()
        if not await _sid_in_query_works(client, sid):
            break
    else:
        pytest.fail(f"no session ID failed as _sid in {_MAX_LOGINS} logins")

    with reauth_spy_on(auth) as spy:
        shares = await client.request("SYNO.FileStation.List", "list_share", version=2)
        info = await client.request("SYNO.DSM.Info", "getinfo", version=2)
    assert "shares" in shares
    assert "version_string" in info or "version" in info
    assert client.sid == sid, "the client logged in again, so the misread session wasn't tested"
    assert (spy.logins, spy.reauths, spy.codes) == (0, 0, []), spy
    print(f"session ID {sid[:6]}… failed as _sid after {tried} logins; works as the cookie")
