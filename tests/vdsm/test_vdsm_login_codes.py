"""What DSM answers a login with an expired or must-change password (#460), as Synology's Login Web
API Guide says: 408 expired and can't be changed, 409 expired, 410 must be changed. A wrong password
gets 400 in each, so these codes also prove the password was right. 407, a blocked IP address, was
seen by hand on all four DSM versions; blocking this computer's address would stop the rest of the
session, so it isn't a test.

Run: ``uv run pytest tests/vdsm -m vdsm --no-cov --dsm-version 7.2.2 -k login_codes``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mcp_synology.core.errors import AuthenticationError, error_from_code
from tests.vdsm.config import DEFAULT_FRESH_PASSWORD, DEFAULT_FRESH_USER
from tests.vdsm.conftest import _ssh, load_golden_meta

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.vdsm

_EXPIRY = "SYNO.Core.User.PasswordExpiry"
_OLD = "15000"  # days since 1970: long past any policy


def _login(base_url: str, password: str) -> int | None:
    reply = httpx.post(
        f"{base_url}/webapi/entry.cgi",
        data={
            "api": "SYNO.API.Auth",
            "version": "6",
            "method": "login",
            "account": DEFAULT_FRESH_USER,
            "passwd": password,
            "format": "sid",
        },
        timeout=30,
    ).json()
    return None if reply.get("success") else reply["error"]["code"]


class _Account:
    """The fresh user's last-password-change day in /etc/shadow, and the expiry policy."""

    def __init__(self, container: Any, admin_password: str, client: Any) -> None:
        self.container = container
        self.admin_password = admin_password
        self.client = client
        self.base_url = container.base_url

    def last_changed(self, day: str) -> None:
        rc, out = _ssh(
            self.container,
            self.admin_password,
            f"sed -i 's/^\\\\({DEFAULT_FRESH_USER}:[^:]*\\\\):[0-9]*:/\\\\1:{day}:/' /etc/shadow",
            timeout=60,
        )
        assert rc == 0, out

    def read_day(self) -> str:
        rc, out = _ssh(
            self.container,
            self.admin_password,
            f"grep '^{DEFAULT_FRESH_USER}:' /etc/shadow | cut -d: -f3",
            timeout=60,
        )
        assert rc == 0, out
        return out.strip().splitlines()[-1].strip()

    async def policy(self, *, enabled: bool, reset: bool = True) -> None:
        params = {
            "password_expire_enable": str(enabled).lower(),
            "min_age_enable": "false",
            "enable_login_prompt": "false",
            "enable_mail_notification": "false",
            "allow_reset_after_expired": str(reset).lower(),
        }
        if enabled:
            params["max_age"] = "1"
        await self.client.request(_EXPIRY, "set", version=1, params=params)


@pytest.fixture
async def account(
    vdsm_container: Any, admin_client: Any, dsm_version: str
) -> AsyncIterator[_Account]:
    meta = load_golden_meta(dsm_version)
    state = _Account(vdsm_container, str(meta["admin_password"]), admin_client[0])
    day = state.read_day()
    yield state
    # The policy first: it expires the admin's own password too, which stops SSH.
    await state.policy(enabled=False)
    state.last_changed(day)
    assert _login(state.base_url, DEFAULT_FRESH_PASSWORD) is None


def _is_a_refusal(code: int, message: str) -> None:
    error = error_from_code(code, "SYNO.API.Auth")
    assert isinstance(error, AuthenticationError)
    assert str(error) == message


async def test_a_password_that_must_change_is_410(account: _Account) -> None:
    account.last_changed("0")
    assert _login(account.base_url, DEFAULT_FRESH_PASSWORD) == 410
    assert _login(account.base_url, "not-the-password") == 400
    _is_a_refusal(410, "The password must be changed")


async def test_an_expired_password_is_409_or_408(account: _Account) -> None:
    account.last_changed(_OLD)
    assert _login(account.base_url, DEFAULT_FRESH_PASSWORD) is None  # no policy, so not expired
    await account.policy(enabled=True, reset=True)
    assert _login(account.base_url, DEFAULT_FRESH_PASSWORD) == 409
    assert _login(account.base_url, "not-the-password") == 400
    await account.policy(enabled=True, reset=False)
    assert _login(account.base_url, DEFAULT_FRESH_PASSWORD) == 408
    assert _login(account.base_url, "not-the-password") == 400
    _is_a_refusal(409, "The password has expired")
    _is_a_refusal(408, "The password has expired, and DSM doesn't let this account change it")
