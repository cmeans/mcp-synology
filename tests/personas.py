"""Who an integration test runs as, and a spy on its session re-auths (#124)."""

from __future__ import annotations

import contextlib
import dataclasses
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mcp_synology.core.auth import AuthManager


@dataclasses.dataclass(frozen=True)
class Persona:
    """Who ``nas_client`` is logged in as.

    ``is_admin`` is None when unknown (a real NAS whose integration config
    doesn't set ``is_admin``): only then may a persona-dependent test branch
    or skip; otherwise it asserts the persona's behavior outright (#124).
    """

    username: str | None
    is_admin: bool | None


@dataclasses.dataclass
class ReauthSpy:
    """Records session re-auths on one AuthManager, with the DSM code behind each.

    On vdsm a re-auth just succeeds, so a test can't see one from its result;
    this makes "error 105 never triggers re-auth" (CLAUDE.md) observable.
    ``codes`` holds the DSM error code that triggered each re-auth, read from
    ``DsmClient``'s "Session error <code>" log record. A re-auth on a session
    code (106/107/119) is correct client behavior; DSM has been seen to answer
    a non-admin's admin-only call with 119 on a slow runner (#133). ``logins``
    counts ``login()`` calls, so an attempted-but-failed re-auth shows too.
    """

    reauths: int = 0
    logins: int = 0
    codes: list[int] = dataclasses.field(default_factory=list)

    def assert_105_never_reauthed(self) -> None:
        """No re-auth was triggered by 105, and every re-auth attempt succeeded."""
        assert 105 not in self.codes, f"error 105 triggered a re-auth: {self.codes}"
        assert self.logins == self.reauths == len(self.codes), (
            f"re-auth attempts and successes differ: logins={self.logins}, "
            f"reauths={self.reauths}, codes={self.codes}"
        )


class _SessionErrorHandler(logging.Handler):
    def __init__(self, spy: ReauthSpy) -> None:
        super().__init__(logging.INFO)
        self._spy = spy

    def emit(self, record: logging.LogRecord) -> None:
        if str(record.msg).startswith("Session error") and record.args:
            args = record.args if isinstance(record.args, tuple) else (record.args,)
            self._spy.codes.append(int(args[0]))  # type: ignore[call-overload]


@contextlib.contextmanager
def reauth_spy_on(auth: AuthManager) -> Iterator[ReauthSpy]:
    """Record ``auth``'s re-auths and logins from now until the block exits."""
    spy = ReauthSpy()

    def on_reauth() -> None:
        spy.reauths += 1

    auth.add_on_reauth_callback(on_reauth)
    login = auth.login

    async def counting_login() -> str:
        spy.logins += 1
        return await login()

    auth.login = counting_login  # type: ignore[method-assign]
    client_logger = logging.getLogger("mcp_synology.core.client")
    handler = _SessionErrorHandler(spy)
    level = client_logger.level
    client_logger.addHandler(handler)
    if client_logger.getEffectiveLevel() > logging.INFO:
        client_logger.setLevel(logging.INFO)
    try:
        yield spy
    finally:
        client_logger.removeHandler(handler)
        client_logger.setLevel(level)
