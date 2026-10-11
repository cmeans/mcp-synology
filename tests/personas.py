"""Who an integration test runs as, and a spy on its session re-auths (#124)."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
    """Counts session re-auths and ``login()`` calls on one AuthManager.

    On vdsm a re-auth just succeeds, so a test can't see one from its result;
    this makes "error 105 never triggers re-auth" (CLAUDE.md) observable.
    The login count also catches a re-auth that was attempted but failed.
    """

    reauths: int = 0
    logins: int = 0


def attach_reauth_spy(auth: AuthManager) -> ReauthSpy:
    """Start counting ``auth``'s re-auths and logins from now on."""
    spy = ReauthSpy()

    def on_reauth() -> None:
        spy.reauths += 1

    auth.add_on_reauth_callback(on_reauth)
    login = auth.login

    async def counting_login() -> str:
        spy.logins += 1
        return await login()

    auth.login = counting_login  # type: ignore[method-assign]
    return spy
