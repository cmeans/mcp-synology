"""Unit tests for the re-auth spy used by the integration/vdsm suites (#124, #133)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest

from tests.personas import reauth_spy_on

if TYPE_CHECKING:
    from collections.abc import Callable

_client_log = logging.getLogger("mcp_synology.core.client")


class _FakeAuth:
    def __init__(self) -> None:
        self.callbacks: list[Callable[[], None]] = []

    def add_on_reauth_callback(self, cb: Callable[[], None]) -> None:
        self.callbacks.append(cb)

    async def login(self) -> str:
        return "sid"


async def _reauth(auth: Any, code: int) -> None:
    """What DsmClient + AuthManager do on a session error: log, login, fire callbacks."""
    _client_log.info("Session error %d on %s/%s, attempting re-auth.", code, "SYNO.X", "get")
    await auth.login()
    for cb in auth.callbacks:
        cb()


async def test_session_code_reauth_is_allowed() -> None:
    auth = _FakeAuth()
    with reauth_spy_on(auth) as spy:  # type: ignore[arg-type]
        await _reauth(auth, 119)
    assert spy.codes == [119]
    assert (spy.reauths, spy.logins) == (1, 1)
    spy.assert_105_never_reauthed()


async def test_reauth_on_105_fails_the_check() -> None:
    auth = _FakeAuth()
    with reauth_spy_on(auth) as spy:  # type: ignore[arg-type]
        await _reauth(auth, 105)
    with pytest.raises(AssertionError, match="105 triggered a re-auth"):
        spy.assert_105_never_reauthed()


async def test_failed_reauth_attempt_fails_the_check() -> None:
    auth = _FakeAuth()
    with reauth_spy_on(auth) as spy:  # type: ignore[arg-type]
        _client_log.info("Session error %d on %s/%s, attempting re-auth.", 106, "SYNO.X", "get")
        await auth.login()  # login ran, but the re-auth never completed
    with pytest.raises(AssertionError, match="differ"):
        spy.assert_105_never_reauthed()


async def test_handler_is_removed_and_level_restored() -> None:
    level = _client_log.level
    auth = _FakeAuth()
    with reauth_spy_on(auth) as spy:  # type: ignore[arg-type]
        pass
    _client_log.info("Session error %d on %s/%s, attempting re-auth.", 119, "SYNO.X", "get")
    assert spy.codes == []
    assert _client_log.level == level
