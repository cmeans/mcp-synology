"""Logins against a real DSM: one re-login for parallel calls (#359), a refused login
sent once (#447) and reported as itself after a session error (#448).

Run: ``uv run pytest tests/vdsm -m vdsm --dsm-version 7.2.2 -k relogin``
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from mcp_synology.core.auth import AuthManager
from mcp_synology.core.errors import AuthenticationError
from tests.personas import reauth_spy_on

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

pytestmark = pytest.mark.vdsm


def _count_logins(client: DsmClient) -> list[str]:
    """Record each login the client sends to DSM (not those answered without sending)."""
    sent: list[str] = []
    request = client.request

    async def counting(api: str, method: str, *args: Any, **kwargs: Any) -> Any:
        if (api, method) == ("SYNO.API.Auth", "login"):
            sent.append(method)
        return await request(api, method, *args, **kwargs)

    client.request = counting  # type: ignore[method-assign]
    return sent


async def test_relogin_parallel_calls_on_an_ended_session_log_in_once(nas_client: Any) -> None:
    """Four calls sent together with a session DSM has ended: one login, all succeed."""
    client, auth, _config, _paths = nas_client
    ended = client.sid
    assert ended
    await auth.logout()  # DSM ends the session; the client then holds it again
    client.sid = ended
    sent = _count_logins(client)
    with reauth_spy_on(auth) as spy:
        replies = await asyncio.gather(
            *(client.request("SYNO.FileStation.List", "list_share", version=2) for _ in range(4))
        )
    assert all("shares" in r for r in replies)
    assert len(sent) == 1, spy
    assert spy.reauths == 1, spy
    assert spy.codes and set(spy.codes) == {119}, spy
    assert client.sid and client.sid != ended


async def test_relogin_refused_login_is_sent_once(nas_client: Any) -> None:
    """A wrong password reaches DSM once; later logins get the same answer unsent, and a
    session error's re-login reports the refusal, not the session error."""
    client, _auth, config, _paths = nas_client
    wrong = config.model_copy(
        update={"auth": config.auth.model_copy(update={"password": "not-the-password"})}
    )
    auth = AuthManager(wrong, client)  # the client's re-login now uses the wrong password
    sent = _count_logins(client)
    good_sid = client.sid
    for _ in range(3):
        with pytest.raises(AuthenticationError) as exc:
            await auth.login()
        assert exc.value.code == 400
        assert str(exc.value).startswith("DSM refused the login: ")
    assert len(sent) == 1
    assert client.sid == good_sid  # a refused login leaves the session as it was

    client.sid = "ended-" + (good_sid or "")[6:]
    with pytest.raises(AuthenticationError) as exc:
        await client.request("SYNO.FileStation.List", "list_share", version=2)
    assert exc.value.code == 400
    assert "mcp-synology setup" in (exc.value.suggestion or "")
    assert len(sent) == 1
    client.sid = good_sid  # so the fixture's logout ends the real session
