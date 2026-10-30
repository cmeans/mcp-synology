"""Tests for core/auth.py — auth flows, credential resolution, re-auth."""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qsl

import httpx
import pytest
import respx

from mcp_synology.core.auth import AuthManager, LoginRefusals
from mcp_synology.core.client import DsmClient
from mcp_synology.core.config import AppConfig
from mcp_synology.core.errors import AuthenticationError, SynologyError
from mcp_synology.core.state import ApiInfoEntry

BASE_URL = "http://nas:5000"


def _make_config(**overrides: Any) -> AppConfig:
    raw: dict[str, Any] = {
        "schema_version": 1,
        "connection": {"host": "nas", "port": 5000},
        "modules": {"filestation": {"enabled": True}},
    }
    raw.update(overrides)
    return AppConfig(**raw)


def _make_client() -> DsmClient:
    client = DsmClient(base_url=BASE_URL)
    client._api_cache = {
        "SYNO.API.Auth": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=7),
        "SYNO.FileStation.List": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2),
    }
    return client


def _no_keyring() -> MagicMock:
    """Return a mock keyring module where get_password raises a realistic
    `keyring.errors.NoKeyringError`.

    Pre-#38 this used bare `Exception("No keyring backend")` and the
    production code's bare `except Exception` swallowed it. After #38 the
    handler narrowed to `KeyringError`/`OSError`, so the realistic typed
    error is also what the tests should mock — keeps the production-shaped
    error path exercised everywhere this fixture is used.
    """
    from keyring.errors import NoKeyringError

    mock = MagicMock()
    mock.get_password.side_effect = NoKeyringError("No keyring backend")
    return mock


def _keyring_with(
    username: str | None = None,
    password: str | None = None,
    device_id: str | None = None,
) -> MagicMock:
    """Return a mock keyring module that returns specific values."""
    mock = MagicMock()
    mock.get_password.side_effect = lambda _svc, key: {
        "username": username,
        "password": password,
        "device_id": device_id,
    }.get(key)
    return mock


def _clean_env() -> dict[str, str]:
    """Return env dict with all SYNOLOGY_ vars removed."""
    return {k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")}


class TestCredentialResolution:
    def test_credentials_from_config(self) -> None:
        config = _make_config(auth={"username": "admin", "password": "secret"})
        client = _make_client()
        auth = AuthManager(config, client)

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch("mcp_synology.core.auth.kr", _no_keyring()),
        ):
            username, password, device_id = auth._resolve_credentials()

        assert username == "admin"
        assert password == "secret"
        assert device_id is None

    def test_credentials_from_env(self) -> None:
        config = _make_config()
        client = _make_client()
        auth = AuthManager(config, client)

        env = {
            **_clean_env(),
            "SYNOLOGY_USERNAME": "env_user",
            "SYNOLOGY_PASSWORD": "env_pass",
            "SYNOLOGY_DEVICE_ID": "env_device",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("mcp_synology.core.auth.kr", _no_keyring()),
        ):
            username, password, device_id = auth._resolve_credentials()

        assert username == "env_user"
        assert password == "env_pass"
        assert device_id == "env_device"

    def test_credentials_from_keyring(self) -> None:
        config = _make_config()
        client = _make_client()
        auth = AuthManager(config, client)

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "mcp_synology.core.auth.kr",
                _keyring_with("kr_user", "kr_pass", "kr_device"),
            ),
        ):
            username, password, device_id = auth._resolve_credentials()

        assert username == "kr_user"
        assert password == "kr_pass"
        assert device_id == "kr_device"

    def test_no_credentials_raises(self) -> None:
        config = _make_config()
        client = _make_client()
        auth = AuthManager(config, client)

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch("mcp_synology.core.auth.kr", _no_keyring()),
            pytest.raises(AuthenticationError, match="No credentials") as exc,
        ):
            auth._resolve_credentials()
        # The hint is a suggestion, so the structured auth_failed error carries it (#232).
        assert str(exc.value) == "No credentials found."
        assert "mcp-synology setup" in (exc.value.suggestion or "")
        assert "SYNOLOGY_PASSWORD" in (exc.value.suggestion or "")

    # --- Empty / whitespace-only credentials at each strategy level (closes #35) ---
    #
    # The bug was that whitespace-only credentials (e.g. `auth: {username: "   "}`
    # mid-edit) flowed straight into login() as bogus values, surfacing as a
    # generic DSM 400. Empty strings already fell through correctly; whitespace
    # did not. The fix normalizes empty + whitespace at every read site
    # (env / config / keyring) via _present_or_none.

    def test_whitespace_config_credentials_fall_through(self) -> None:
        """Whitespace-only plaintext config credentials fall through, raise clean error."""
        config = _make_config(auth={"username": "   ", "password": "\t"})
        client = _make_client()
        auth = AuthManager(config, client)

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch("mcp_synology.core.auth.kr", _no_keyring()),
            pytest.raises(AuthenticationError, match="No credentials"),
        ):
            auth._resolve_credentials()

    def test_whitespace_env_credentials_fall_through(self) -> None:
        """Whitespace-only env credentials are ignored, fall through to next strategy."""
        config = _make_config()
        client = _make_client()
        auth = AuthManager(config, client)

        env = {
            **_clean_env(),
            "SYNOLOGY_USERNAME": "   ",
            "SYNOLOGY_PASSWORD": "\t\n",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("mcp_synology.core.auth.kr", _no_keyring()),
            pytest.raises(AuthenticationError, match="No credentials"),
        ):
            auth._resolve_credentials()

    def test_whitespace_keyring_credentials_fall_through(self) -> None:
        """Whitespace-only keyring values are ignored, raise clean error."""
        config = _make_config()
        client = _make_client()
        auth = AuthManager(config, client)

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch("mcp_synology.core.auth.kr", _keyring_with("   ", "\t", "  ")),
            pytest.raises(AuthenticationError, match="No credentials"),
        ):
            auth._resolve_credentials()

    def test_empty_string_env_credentials_fall_through(self) -> None:
        """Empty-string env credentials fall through (regression coverage)."""
        config = _make_config(auth={"username": "", "password": ""})
        client = _make_client()
        auth = AuthManager(config, client)

        env = {
            **_clean_env(),
            "SYNOLOGY_USERNAME": "",
            "SYNOLOGY_PASSWORD": "",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("mcp_synology.core.auth.kr", _no_keyring()),
            pytest.raises(AuthenticationError, match="No credentials"),
        ):
            auth._resolve_credentials()

    def test_whitespace_env_falls_through_to_valid_keyring(self) -> None:
        """Whitespace-only env doesn't shadow a valid keyring entry."""
        config = _make_config()
        client = _make_client()
        auth = AuthManager(config, client)

        env = {
            **_clean_env(),
            "SYNOLOGY_USERNAME": "   ",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "mcp_synology.core.auth.kr",
                _keyring_with("kr_user", "kr_pass", "kr_device"),
            ),
        ):
            username, password, device_id = auth._resolve_credentials()

        assert username == "kr_user"
        assert password == "kr_pass"
        assert device_id == "kr_device"

    def test_valid_credentials_with_internal_padding_preserved(self) -> None:
        """Credentials with leading/trailing spaces but real content are NOT stripped."""
        # If a user's actual password has padding, _present_or_none keeps it.
        config = _make_config(auth={"username": "  alice  ", "password": "  pwd123  "})
        client = _make_client()
        auth = AuthManager(config, client)

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch("mcp_synology.core.auth.kr", _no_keyring()),
        ):
            username, password, _ = auth._resolve_credentials()

        # Original padding is preserved — we filter out empty/whitespace-only,
        # we don't strip meaningful values.
        assert username == "  alice  "
        assert password == "  pwd123  "


class TestLogin:
    @respx.mock
    async def test_simple_login(self) -> None:
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"sid": "test-sid-123"}}
        )

        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
            ):
                sid = await auth.login()

        assert sid == "test-sid-123"
        (call,) = respx.calls
        assert call.request.method == "POST"
        assert dict(call.request.url.params) == {}  # no passwd in the URL (#235)
        assert dict(parse_qsl(call.request.content.decode()))["passwd"] == "secret"

    @respx.mock
    async def test_login_with_device_id(self) -> None:
        route = respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"sid": "2fa-sid-456"}}
        )

        config = _make_config(
            auth={"username": "admin", "password": "secret", "device_id": "dev123"}
        )
        async with _make_client() as client:
            auth = AuthManager(config, client)
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
            ):
                sid = await auth.login()

        assert sid == "2fa-sid-456"
        request = route.calls[0].request
        form = dict(parse_qsl(request.content.decode()))
        assert form["device_id"] == "dev123"
        assert form["passwd"] == "secret"
        # The secrets travel in the POST body, never in the URL (#235).
        assert dict(request.url.params) == {}

    @respx.mock
    async def test_rejected_device_token_says_so(self) -> None:
        """A stored device token DSM no longer trusts also gets 403. The error
        used to say "no device token is available", which sent users looking
        for a token that was there."""
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 403}}
        )
        config = _make_config(
            auth={"username": "admin", "password": "secret", "device_id": "dev123"}
        )
        async with _make_client() as client:
            auth = AuthManager(config, client)
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
                pytest.raises(AuthenticationError) as exc,
            ):
                await auth.login()
        message = str(exc.value)
        assert "rejected the device token" in message
        assert "no device token" not in message
        # The fix depends on where the token is set (here, the config file): the
        # suggestion says it, the message doesn't contradict it (QA round 2 on #461).
        assert "mcp-synology setup" not in message
        assert "auth.device_id in the config file" in (exc.value.suggestion or "")

    @respx.mock
    async def test_2fa_required_without_device_id(self) -> None:
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 403}}
        )

        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
                pytest.raises(AuthenticationError, match="2FA"),
            ):
                await auth.login()


class TestReAuth:
    @respx.mock
    async def test_re_auth_on_session_expired(self) -> None:
        call_count = 0

        def side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            params = dict(request.url.params)
            if params.get("method") == "list_share" and call_count == 1:
                return httpx.Response(200, json={"success": False, "error": {"code": 106}})
            return httpx.Response(200, json={"success": True, "data": {"shares": []}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"sid": "new-sid"}}
        )

        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            AuthManager(config, client)
            client.sid = "old-sid"

            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
            ):
                data = await client.request("SYNO.FileStation.List", "list_share", version=2)
        assert "shares" in data

    @respx.mock
    async def test_get_session_returns_existing(self) -> None:
        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            client.sid = "existing-sid"
            sid = await auth.get_session()
        assert sid == "existing-sid"

    @respx.mock
    async def test_get_session_logs_in_when_no_sid(self) -> None:
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"sid": "fresh-sid"}}
        )
        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
            ):
                sid = await auth.get_session()
        assert sid == "fresh-sid"


class TestOnReauthCallbacks:
    """`add_on_reauth_callback` + dispatch from `_re_authenticate` (closes #37)."""

    @respx.mock
    async def test_callback_fires_after_reauth(self) -> None:
        """A registered callback runs once after a successful re-auth."""
        call_count = 0

        def side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            params = dict(request.url.params)
            if params.get("method") == "list_share" and call_count == 1:
                return httpx.Response(200, json={"success": False, "error": {"code": 106}})
            return httpx.Response(200, json={"success": True, "data": {"shares": []}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"sid": "new-sid"}}
        )

        invocations: list[str] = []

        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            auth.add_on_reauth_callback(lambda: invocations.append("fired"))
            client.sid = "old-sid"

            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
            ):
                await client.request("SYNO.FileStation.List", "list_share", version=2)

        assert invocations == ["fired"]

    @respx.mock
    async def test_callback_exception_is_logged_and_does_not_block_others(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A raising callback must not stop later callbacks from firing."""
        call_count = 0

        def side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            params = dict(request.url.params)
            if params.get("method") == "list_share" and call_count == 1:
                return httpx.Response(200, json={"success": False, "error": {"code": 106}})
            return httpx.Response(200, json={"success": True, "data": {"shares": []}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"sid": "new-sid"}}
        )

        invocations: list[str] = []

        def boom() -> None:
            raise RuntimeError("simulated callback failure")

        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            auth.add_on_reauth_callback(boom)
            auth.add_on_reauth_callback(lambda: invocations.append("after-boom"))
            client.sid = "old-sid"

            import logging

            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
                caplog.at_level(logging.WARNING, logger="mcp_synology.core.auth"),
            ):
                await client.request("SYNO.FileStation.List", "list_share", version=2)

        # Second callback fired even though the first raised.
        assert invocations == ["after-boom"]
        # Warning was logged about the callback failure.
        warning_messages = [r.getMessage() for r in caplog.records]
        assert any(
            "on_reauth callback" in msg and "simulated callback failure" in msg
            for msg in warning_messages
        )

    def test_callback_can_be_added_pre_reauth(self) -> None:
        """Just verifies the registration API is non-coroutine and accepts a callable."""
        config = _make_config(auth={"username": "admin", "password": "secret"})
        client = _make_client()
        auth = AuthManager(config, client)
        auth.add_on_reauth_callback(lambda: None)
        # No exception, internal list now has one entry.
        assert len(auth._on_reauth_callbacks) == 1


class TestCredentialPriority:
    """Test that credential resolution follows: env > config > keyring."""

    def test_env_overrides_keyring(self) -> None:
        """Env vars should take priority over keyring."""
        config = _make_config()
        client = _make_client()
        auth = AuthManager(config, client)

        env = {
            **_clean_env(),
            "SYNOLOGY_USERNAME": "env_user",
            "SYNOLOGY_PASSWORD": "env_pass",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "mcp_synology.core.auth.kr",
                _keyring_with("kr_user", "kr_pass"),
            ),
        ):
            username, password, device_id = auth._resolve_credentials()

        assert username == "env_user"
        assert password == "env_pass"

    def test_config_overrides_keyring(self) -> None:
        """Config file creds should take priority over keyring."""
        config = _make_config(auth={"username": "cfg_user", "password": "cfg_pass"})
        client = _make_client()
        auth = AuthManager(config, client)

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "mcp_synology.core.auth.kr",
                _keyring_with("kr_user", "kr_pass"),
            ),
        ):
            username, password, device_id = auth._resolve_credentials()

        assert username == "cfg_user"
        assert password == "cfg_pass"

    def test_env_overrides_config(self) -> None:
        """Env vars should take priority over config file creds."""
        config = _make_config(auth={"username": "cfg_user", "password": "cfg_pass"})
        client = _make_client()
        auth = AuthManager(config, client)

        env = {
            **_clean_env(),
            "SYNOLOGY_USERNAME": "env_user",
            "SYNOLOGY_PASSWORD": "env_pass",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("mcp_synology.core.auth.kr", _no_keyring()),
        ):
            username, password, device_id = auth._resolve_credentials()

        assert username == "env_user"
        assert password == "env_pass"

    def test_partial_env_falls_through(self) -> None:
        """If env has username but not password, password comes from keyring."""
        config = _make_config()
        client = _make_client()
        auth = AuthManager(config, client)

        env = {
            **_clean_env(),
            "SYNOLOGY_USERNAME": "env_user",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "mcp_synology.core.auth.kr",
                _keyring_with("kr_user", "kr_pass"),
            ),
        ):
            username, password, device_id = auth._resolve_credentials()

        assert username == "env_user"
        assert password == "kr_pass"

    def test_device_id_from_keyring_when_not_in_env(self) -> None:
        """Device ID from keyring is used even when creds come from env."""
        config = _make_config()
        client = _make_client()
        auth = AuthManager(config, client)

        env = {**_clean_env(), "SYNOLOGY_USERNAME": "env_user", "SYNOLOGY_PASSWORD": "env_pass"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "mcp_synology.core.auth.kr",
                _keyring_with(device_id="kr_device"),
            ),
        ):
            username, password, device_id = auth._resolve_credentials()

        assert username == "env_user"
        assert password == "env_pass"
        assert device_id == "kr_device"


class TestDbusAutoDetect:
    def test_dbus_set_when_missing_and_socket_exists(self) -> None:
        """On Linux, auto-set DBUS_SESSION_BUS_ADDRESS if socket exists."""
        config = _make_config()
        client = _make_client()
        auth = AuthManager(config, client)

        clean = _clean_env()
        # Ensure DBUS is not set
        clean.pop("DBUS_SESSION_BUS_ADDRESS", None)

        with (
            patch.dict(os.environ, clean, clear=True),
            patch("mcp_synology.core.auth.kr", _keyring_with("user", "pass")),
            patch("sys.platform", "linux"),
            patch("pathlib.Path.exists", return_value=True),
            patch("os.getuid", return_value=1000),
        ):
            auth._resolve_credentials()

        # After resolution, DBUS should have been set (if we're on linux)
        # This test validates the code path runs without error

    def test_dbus_not_set_on_macos(self) -> None:
        """On macOS, don't set DBUS_SESSION_BUS_ADDRESS."""
        config = _make_config(auth={"username": "admin", "password": "secret"})
        client = _make_client()
        auth = AuthManager(config, client)

        clean = _clean_env()
        clean.pop("DBUS_SESSION_BUS_ADDRESS", None)

        with (
            patch.dict(os.environ, clean, clear=True),
            patch("mcp_synology.core.auth.kr", _no_keyring()),
            patch("sys.platform", "darwin"),
        ):
            username, password, _ = auth._resolve_credentials()

        assert username == "admin"


class TestSessionNaming:
    def test_session_name_format(self) -> None:
        config = _make_config(instance_id="test-nas")
        client = _make_client()
        auth = AuthManager(config, client)
        assert auth._session_name.startswith("MCPSynology_test-nas_")
        uuid_part = auth._session_name.split("_")[-1]
        assert len(uuid_part) == 8

    def test_build_session_name_without_key_uses_uuid(self) -> None:
        """Helper called with no key keeps the current uuid-suffix shape.

        Pinning the no-arg behavior so the deferred-stub argument addition
        (ADR 0001) cannot silently change the default session-name format.
        """
        config = _make_config(instance_id="nas-a")
        client = _make_client()
        auth = AuthManager(config, client)
        name = auth._build_session_name()
        assert name.startswith("MCPSynology_nas-a_")
        suffix = name.rsplit("_", 1)[-1]
        assert len(suffix) == 8
        # uuid4().hex is lowercase hex
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_build_session_name_with_key_uses_key_as_suffix(self) -> None:
        """Helper called with an explicit key derives the per-key form.

        This is the deferred-stub surface for the Streamable HTTP per-client
        session model (ADR 0001). No production code path passes a key today;
        the test pins the helper's contract so a future per-client session
        pool can build on it without re-deriving the name format.
        """
        config = _make_config(instance_id="nas-b")
        client = _make_client()
        auth = AuthManager(config, client)
        name = auth._build_session_name(session_key="mcp-client-xyz")
        assert name == "MCPSynology_nas-b_mcp-client-xyz"

    def test_build_session_name_with_empty_key_uses_empty_suffix(self) -> None:
        """An explicit empty-string key is honored as-is, not treated as None.

        Pins the None-vs-empty distinction so a future pool that uses sentinel
        values cannot accidentally collide with the no-key default. Empty-string
        is a degenerate case but the contract treats `None` (use uuid) and
        `""` (use the empty string literally) as different.
        """
        config = _make_config(instance_id="nas-c")
        client = _make_client()
        auth = AuthManager(config, client)
        name = auth._build_session_name(session_key="")
        assert name == "MCPSynology_nas-c_"


class TestDbusSocketMissing:
    def test_dbus_not_set_when_socket_missing_on_linux(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Linux + DBUS unset + socket missing → log INFO with remediation hint
        and do not set env var.

        Closes #38 (the operator-actionable hint half). Pre-fix this branch
        only logged at DEBUG ("D-Bus socket not found at %s; keyring may not
        work"), so an operator running `mcp-synology check` (without -v) saw
        a generic "no credentials" error with no clue that the missing D-Bus
        socket was the root cause. INFO-level message names the socket path
        AND points at three concrete remediations: run setup from a real
        session, wrap with `dbus-run-session`, or use SYNOLOGY_* env vars.
        """
        import logging

        config = _make_config(auth={"username": "admin", "password": "secret"})
        client = _make_client()
        auth = AuthManager(config, client)

        clean = _clean_env()
        clean.pop("DBUS_SESSION_BUS_ADDRESS", None)

        with (
            patch.dict(os.environ, clean, clear=True),
            patch("mcp_synology.core.auth.kr", _no_keyring()),
            patch("sys.platform", "linux"),
            patch("pathlib.Path.exists", return_value=False),
            patch("os.getuid", return_value=1000),
            caplog.at_level(logging.INFO, logger="mcp_synology.core.auth"),
        ):
            username, _, _ = auth._resolve_credentials()
            assert "DBUS_SESSION_BUS_ADDRESS" not in os.environ
        assert username == "admin"
        # INFO record carries actionable remediation, not just a description.
        info_messages = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
        assert any(
            "D-Bus socket not found" in msg
            and "/run/user/1000/bus" in msg
            and "mcp-synology setup" in msg
            and "SYNOLOGY_USERNAME" in msg
            for msg in info_messages
        ), f"expected actionable INFO hint, got: {info_messages}"


class TestKeyringErrorHandling:
    """Closes #38 — narrow keyring exception handler + log root cause.

    Pre-fix `core/auth.py:147-148` caught bare `except Exception` with a flat
    `logger.debug("Keyring not available.")`, hiding the actual failure
    (locked macOS keychain, NoKeyringError on a headless host, OSError on
    D-Bus socket reach issues, library bugs). Now narrows to
    `keyring.errors.KeyringError` + `OSError` and logs each at DEBUG with
    `exc_info=True` so `mcp-synology check -v` surfaces the real cause.
    """

    def _make_keyring_that_raises(self, exc: Exception) -> MagicMock:
        mock = MagicMock()
        mock.get_password.side_effect = exc
        return mock

    def test_keyring_error_logged_with_exc_info_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Typed `KeyringError` (e.g. macOS locked keychain) is logged with
        the exception message and traceback at DEBUG, NOT swallowed.
        """
        import logging

        from keyring.errors import KeyringLocked

        config = _make_config(auth={"username": "admin", "password": "secret"})
        client = _make_client()
        auth = AuthManager(config, client)

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "mcp_synology.core.auth.kr",
                self._make_keyring_that_raises(KeyringLocked("Keychain is locked")),
            ),
            caplog.at_level(logging.DEBUG, logger="mcp_synology.core.auth"),
        ):
            auth._resolve_credentials()

        debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
        # Failure log carries the exception text and exc_info traceback.
        matching = [
            r
            for r in debug_records
            if "Keyring access failed" in r.getMessage() and "Keychain is locked" in r.getMessage()
        ]
        assert matching, (
            f"expected DEBUG log with KeyringError message, got: "
            f"{[r.getMessage() for r in debug_records]}"
        )
        assert matching[0].exc_info is not None, (
            "expected exc_info traceback on the keyring failure log"
        )
        # The pre-fix flat "Keyring not available." string must NOT appear.
        assert not any(r.getMessage() == "Keyring not available." for r in caplog.records), (
            "regression: legacy flat 'Keyring not available.' message reappeared"
        )

    def test_keyring_oserror_logged_with_exc_info_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """OS-level keyring failure (D-Bus reach errors, permission denied
        on macOS keychain DB, etc.) is logged with the exception message
        and traceback at DEBUG, separate from the typed-error branch.
        """
        import logging

        config = _make_config(auth={"username": "admin", "password": "secret"})
        client = _make_client()
        auth = AuthManager(config, client)

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "mcp_synology.core.auth.kr",
                self._make_keyring_that_raises(OSError("Connection refused: /run/user/1000/bus")),
            ),
            caplog.at_level(logging.DEBUG, logger="mcp_synology.core.auth"),
        ):
            auth._resolve_credentials()

        debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
        matching = [
            r
            for r in debug_records
            if "Keyring OS-level error" in r.getMessage() and "Connection refused" in r.getMessage()
        ]
        assert matching, (
            f"expected DEBUG log with OSError message, got: "
            f"{[r.getMessage() for r in debug_records]}"
        )
        assert matching[0].exc_info is not None

    def test_keyring_failure_does_not_block_config_credentials(self) -> None:
        """A keyring blow-up must not prevent the resolver from returning
        credentials sourced from the config file (or env). Defense in depth.
        """
        from keyring.errors import NoKeyringError

        config = _make_config(auth={"username": "admin", "password": "secret"})
        client = _make_client()
        auth = AuthManager(config, client)

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "mcp_synology.core.auth.kr",
                self._make_keyring_that_raises(NoKeyringError("No backend")),
            ),
        ):
            username, password, _ = auth._resolve_credentials()
        assert username == "admin"
        assert password == "secret"


class TestLoginErrorPaths:
    @respx.mock
    async def test_login_non_2fa_synology_error_propagates(self) -> None:
        """A non-403 SynologyError on login is re-raised, not wrapped."""
        from mcp_synology.core.errors import SynologyError

        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 400}}
        )

        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
                pytest.raises(SynologyError),
            ):
                await auth.login()

    @respx.mock
    async def test_login_succeeds_but_no_sid_raises(self) -> None:
        """DSM returns success=True but no sid in data → AuthenticationError."""
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {}}  # no sid
        )

        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
                pytest.raises(AuthenticationError, match="no session ID"),
            ):
                await auth.login()


class TestLogout:
    async def test_logout_no_sid_is_noop(self) -> None:
        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            client.sid = None
            # Should not raise; should not call request
            await auth.logout()

    @respx.mock
    async def test_logout_success_clears_sid(self) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(json={"success": True})

        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            client.sid = "active-sid"
            await auth.logout()
            assert client.sid is None

    @respx.mock
    async def test_logout_synology_error_still_clears_sid(self) -> None:
        """Logout failure (e.g., already-expired session) is swallowed; sid cleared."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 105}}
        )

        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            client.sid = "expired-sid"
            await auth.logout()  # must not raise
            assert client.sid is None


def _auth_routes(
    login: list[httpx.Response] | Any, logins: list[httpx.Request] | None = None
) -> None:
    """Route the login POSTs to ``login`` (a list of replies, in order, or a side effect)
    and answer File Station reads by session: 119 for ``old-sid``, success otherwise."""

    def on_post(request: httpx.Request) -> httpx.Response:
        if logins is not None:
            logins.append(request)
        reply = login(request) if callable(login) else login.pop(0)
        assert isinstance(reply, httpx.Response)
        return reply

    def on_get(request: httpx.Request) -> httpx.Response:
        if "id=old-sid" in request.headers.get("cookie", ""):
            return httpx.Response(200, json={"success": False, "error": {"code": 119}})
        return httpx.Response(200, json={"success": True, "data": {"shares": []}})

    respx.post(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=on_post)
    respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=on_get)


def _refused(code: int = 400) -> httpx.Response:
    return httpx.Response(200, json={"success": False, "error": {"code": code}})


def _granted(sid: str = "new-sid") -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": {"sid": sid}})


class TestLoginRefusals:
    """#447: a login DSM refused isn't sent again, so DSM's Auto Block isn't tripped."""

    @respx.mock
    @pytest.mark.parametrize("code", [400, 401, 403, 404, 406])
    async def test_refused_login_is_sent_once(self, code: int) -> None:
        logins: list[httpx.Request] = []
        _auth_routes([_refused(code)], logins)
        config = _make_config(auth={"username": "admin", "password": "wrong"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
            ):
                errors = []
                for _ in range(5):
                    with pytest.raises(AuthenticationError) as exc:
                        await auth.login()
                    errors.append(exc.value)
        assert len(logins) == 1
        assert {e.code for e in errors} == {code}
        assert len({str(e) for e in errors}) == 1  # the same answer every time
        message = str(errors[0])
        assert message.startswith("DSM refused the login: ")
        assert "Auto Block" in message and ".." not in message
        if code != 401:  # a disabled account: no credential change helps
            assert "mcp-synology setup" in (errors[0].suggestion or "")
        assert ".." not in (errors[0].suggestion or "")

    @respx.mock
    @pytest.mark.parametrize("code", [402, 407, 408])
    async def test_other_login_errors_are_not_remembered(self, code: int) -> None:
        # Not a verdict on the credentials (no permission for the service, a temporary
        # lock or an IP block): the next call tries again.
        logins: list[httpx.Request] = []
        _auth_routes([_refused(code), _granted()], logins)
        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
            ):
                with pytest.raises(SynologyError) as exc:
                    await auth.login()
                assert not isinstance(exc.value, AuthenticationError)
                assert await auth.login() == "new-sid"
        assert len(logins) == 2

    @respx.mock
    async def test_transport_error_is_not_remembered(self) -> None:
        logins: list[httpx.Request] = []
        replies: list[Any] = [httpx.ConnectError("refused"), _granted()]

        def login(request: httpx.Request) -> httpx.Response:
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            assert isinstance(reply, httpx.Response)
            return reply

        _auth_routes(login, logins)
        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
            ):
                with pytest.raises(httpx.ConnectError):
                    await auth.login()
                assert await auth.login() == "new-sid"
        assert len(logins) == 2

    @respx.mock
    @pytest.mark.parametrize(
        ("changed", "value"),
        [("password", "fixed"), ("username", "other"), ("device_id", "dev-new")],
    )
    async def test_changed_credentials_are_tried(self, changed: str, value: str) -> None:
        # `mcp-synology setup` writes new credentials to the keyring while the server runs.
        logins: list[httpx.Request] = []
        _auth_routes([_refused(), _granted()], logins)
        stored = {"username": "admin", "password": "wrong", "device_id": "dev-old"}
        config = _make_config()
        async with _make_client() as client:
            auth = AuthManager(config, client)
            with patch.dict(os.environ, _clean_env(), clear=True):
                with (
                    patch("mcp_synology.core.auth.kr", _keyring_with(**stored)),
                    pytest.raises(AuthenticationError),
                ):
                    await auth.login()
                with (
                    patch("mcp_synology.core.auth.kr", _keyring_with(**stored)),
                    pytest.raises(AuthenticationError),
                ):
                    await auth.login()
                assert len(logins) == 1
                stored[changed] = value
                with patch("mcp_synology.core.auth.kr", _keyring_with(**stored)):
                    assert await auth.login() == "new-sid"
        assert len(logins) == 2

    @respx.mock
    async def test_a_new_server_tries_again(self) -> None:
        # The refusal lives in memory: a restart (a new LoginRefusals) sends the login.
        logins: list[httpx.Request] = []
        _auth_routes([_refused(), _refused()], logins)
        config = _make_config(auth={"username": "admin", "password": "wrong"})
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch("mcp_synology.core.auth.kr", _no_keyring()),
        ):
            for _ in range(2):
                async with _make_client() as client:
                    with pytest.raises(AuthenticationError):
                        await AuthManager(config, client, LoginRefusals()).login()
        assert len(logins) == 2

    @respx.mock
    async def test_relogin_after_a_session_error_uses_it(self) -> None:
        # #447 and #448: a 119 whose re-login is refused says the login was refused, with
        # its suggestion, and the next session error doesn't send the login again.
        logins: list[httpx.Request] = []
        _auth_routes([_refused()], logins)
        config = _make_config(auth={"username": "admin", "password": "wrong"})
        async with _make_client() as client:
            AuthManager(config, client)
            client.sid = "old-sid"
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
            ):
                for _ in range(3):
                    with pytest.raises(AuthenticationError) as exc:
                        await client.request("SYNO.FileStation.List", "list_share", version=2)
                    assert exc.value.code == 400
                    assert str(exc.value).startswith("DSM refused the login: ")
                    assert "mcp-synology setup" in (exc.value.suggestion or "")
            assert client.sid == "old-sid"
        assert len(logins) == 1


class TestConcurrentReauth:
    """#359: requests that meet the same expired session log in once, not once each."""

    @respx.mock
    @pytest.mark.parametrize("n", [2, 5])
    async def test_parallel_session_errors_log_in_once(self, n: int) -> None:
        logins: list[httpx.Request] = []

        async def slow_login(request: httpx.Request) -> httpx.Response:
            logins.append(request)
            await asyncio.sleep(0.01)  # the other requests meet 119 meanwhile
            return _granted()

        respx.post(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=slow_login)

        def on_get(request: httpx.Request) -> httpx.Response:
            if "id=new-sid" in request.headers.get("cookie", ""):
                return httpx.Response(200, json={"success": True, "data": {"shares": []}})
            return httpx.Response(200, json={"success": False, "error": {"code": 119}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=on_get)
        fired: list[int] = []
        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            auth.add_on_reauth_callback(lambda: fired.append(1))
            client.sid = "old-sid"
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
            ):
                results = await asyncio.gather(
                    *(
                        client.request("SYNO.FileStation.List", "list_share", version=2)
                        for _ in range(n)
                    )
                )
        assert results == [{"shares": []}] * n
        assert len(logins) == 1
        assert fired == [1]
        # The login itself never carries the session it replaces.
        assert "cookie" not in logins[0].headers

    @respx.mock
    async def test_session_stays_until_the_new_one_exists(self) -> None:
        # The expired session used to be cleared before the login, so a request sent
        # meanwhile went with no session at all and queued its own re-login.
        seen: list[str | None] = []
        client = _make_client()

        def login(request: httpx.Request) -> httpx.Response:
            seen.append(client.sid)
            return _granted()

        _auth_routes(login)
        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with client:
            AuthManager(config, client)
            client.sid = "old-sid"
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
            ):
                await client.request("SYNO.FileStation.List", "list_share", version=2)
            assert client.sid == "new-sid"
        assert seen == ["old-sid"]


class TestRefusalEdges:
    """Guards the pre-QA review found untested."""

    @respx.mock
    async def test_a_successful_login_forgets_the_refusal(self) -> None:
        # Refused with A, then logged in with B: A is tried again, not answered unsent.
        logins: list[httpx.Request] = []
        _auth_routes([_refused(), _granted(), _refused()], logins)
        stored = {"username": "admin", "password": "a"}
        async with _make_client() as client:
            auth = AuthManager(_make_config(), client)
            with patch.dict(os.environ, _clean_env(), clear=True):
                with (
                    patch("mcp_synology.core.auth.kr", _keyring_with(**stored)),
                    pytest.raises(AuthenticationError),
                ):
                    await auth.login()
                stored["password"] = "b"
                with patch("mcp_synology.core.auth.kr", _keyring_with(**stored)):
                    await auth.login()
                stored["password"] = "a"
                with (
                    patch("mcp_synology.core.auth.kr", _keyring_with(**stored)),
                    pytest.raises(AuthenticationError),
                ):
                    await auth.login()
        assert len(logins) == 3

    @respx.mock
    async def test_an_error_without_a_dsm_code_is_not_remembered(self) -> None:
        # Only DSM's verdict is remembered; an AuthenticationError with no code isn't one.
        config = _make_config(auth={"username": "admin", "password": "secret"})
        async with _make_client() as client:
            auth = AuthManager(config, client)
            sent = AsyncMock(side_effect=[AuthenticationError("odd"), {"sid": "s"}])
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
                patch.object(auth, "_send_login", sent),
            ):
                with pytest.raises(AuthenticationError, match="odd"):
                    await auth.login()
                assert await auth.login() == "s"
        assert sent.await_count == 2

    async def test_a_session_error_with_no_session_left_logs_in(self) -> None:
        # The session was cleared (a logout) after the request went out: log in, rather
        # than read "the session changed" as another request's re-login.
        calls: list[int] = []

        async def reauth() -> None:
            calls.append(1)

        async with _make_client() as client:
            client.set_re_auth_callback(reauth)
            client.sid = None
            await client._re_authenticate(119, "SYNO.FileStation.List", "sent-sid")
        assert calls == [1]


class TestRefusalSuggestion:
    """QA rounds 1-2 on #461: `setup` writes only the keyring, so a refusal names the fix
    for the credential DSM refused: the password for 400, the device token for 2FA codes,
    none for a disabled account; `setup` where it's in the keyring, the environment or
    the config file where it's set there."""

    ENV = "in the server's environment (the MCP client's config)"
    SETUP_PW = "Run 'mcp-synology setup' to store the right password; the running server"
    SETUP_2FA = "Run 'mcp-synology setup' to sign in with a new 2FA code; the running server"

    @respx.mock
    @pytest.mark.parametrize(
        ("code", "auth", "env", "expected", "absent"),
        [
            # Everything from the keyring.
            (400, {}, {}, SETUP_PW + " picks it up. After the fix", "restart"),
            # The recommended setup: a username in the config, the password in the keyring.
            (
                400,
                {"username": "admin"},
                {},
                SETUP_PW + " picks it up. If the username is wrong, it comes from auth.username"
                " in the config file: correct it there and restart the server.",
                "overridden",
            ),
            # Both in the config file: setup can't help.
            (
                400,
                {"username": "admin", "password": "wrong"},
                {},
                "The password comes from auth.password in the config file: correct it there "
                "and restart the server ('mcp-synology setup' writes only the keyring). If the "
                "username is wrong, it comes from auth.username in the config file: correct it "
                "there.",
                SETUP_PW,
            ),
            # The environment wins over the config for the same key: only it is named.
            (
                400,
                {"username": "admin", "password": "unused"},
                {"SYNOLOGY_PASSWORD": "wrong"},
                "The password comes from SYNOLOGY_PASSWORD " + ENV,
                "auth.password",
            ),
            # QA round 2: env credentials, the device token in the keyring, 403.
            (
                403,
                {},
                {"SYNOLOGY_USERNAME": "admin", "SYNOLOGY_PASSWORD": "wrong"},
                SETUP_2FA + " picks it up. After the fix",
                "SYNOLOGY_USERNAME",
            ),
            # A device token set outside the keyring overrides what setup writes.
            (
                404,
                {"device_id": "old"},
                {},
                "The device token comes from auth.device_id in the config file, which "
                "overrides the keyring: remove it there, run 'mcp-synology setup' to sign in "
                "with a new 2FA code, and restart the server.",
                "picks it up",
            ),
            (
                406,
                {},
                {},
                "Admin requires 2FA. Enable 2FA in DSM > Personal > Security. Then: run "
                "'mcp-synology setup' to sign in with a new 2FA code; the running server picks "
                "it up.",
                "password",
            ),
            # A disabled account: no credential change helps.
            (
                401,
                {"username": "admin", "password": "wrong"},
                {},
                "This account is disabled in DSM. Contact your NAS administrator. Once it's "
                "enabled again, restart the server.",
                "setup",
            ),
        ],
        ids=[
            "400-keyring",
            "400-config-username",
            "400-config-both",
            "400-env-over-config",
            "403-env-creds-keyring-token",
            "404-config-token",
            "406",
            "401",
        ],
    )
    async def test_suggestion_names_the_fix_for_the_refused_credential(
        self,
        code: int,
        auth: dict[str, str],
        env: dict[str, str],
        expected: str,
        absent: str,
    ) -> None:
        _auth_routes([_refused(code)])
        stored = _keyring_with(username="admin", password="wrong", device_id="d")
        async with _make_client() as client:
            refusal_auth = AuthManager(_make_config(auth=auth), client)
            with (
                patch.dict(os.environ, {**_clean_env(), **env}, clear=True),
                patch("mcp_synology.core.auth.kr", stored),
                pytest.raises(AuthenticationError) as exc,
            ):
                await refusal_auth.login()
        suggestion = exc.value.suggestion or ""
        assert expected in suggestion
        assert absent not in suggestion

    @pytest.mark.parametrize(
        ("code", "first", "expected"),
        [
            (401, "", "Once the account is enabled again, restart the server"),
            (
                402,
                "Not allowed",
                "Not allowed. mcp-synology tries again once the credentials change "
                "('mcp-synology setup' stores new ones in the keyring; the running server picks "
                "it up); auth.password in the config file overrides the keyring: change it "
                "there and restart the server",
            ),
            (
                None,
                "",
                "mcp-synology tries again once the credentials change ('mcp-synology setup' "
                "stores new ones in the keyring; the running server picks it up); "
                "auth.password in the config file overrides the keyring: change it there and "
                "restart the server",
            ),
        ],
        ids=["401-without-suggestion", "unmapped-code", "no-code"],
    )
    def test_advice_for_other_codes(self, code: int | None, first: str, expected: str) -> None:
        """Codes the five above don't cover (#460 decides 402 and 407-410) get the
        general advice, naming credentials set outside the keyring."""
        from mcp_synology.core.auth import KEYRING, refusal_advice

        sources = {"username": KEYRING, "password": "auth.password in the config file"}
        assert refusal_advice(code, first, {**sources, "device_id": None}) == expected

    def test_check_tests_the_login_once(self) -> None:
        """A model with a shell could loop on `check`, which has no remembered refusal:
        the suggestion says once, and why (pre-QA on QA round 2, #461)."""
        refusal = LoginRefusals().refuse(
            "fp", AuthenticationError("No such account", code=400, suggestion="Check.")
        )
        assert (refusal.suggestion or "").endswith(
            "After the fix, 'mcp-synology check' tests the login once (each failed check "
            "counts toward Auto Block)."
        )

    @respx.mock
    async def test_a_remembered_refusal_logs_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Only a login actually sent and refused is a WARNING; answering one from memory
        # on every later session error would flood the log (pre-QA on QA round 1).
        import logging

        _auth_routes([_refused()])
        config = _make_config(auth={"username": "admin", "password": "wrong"})
        async with _make_client() as client:
            AuthManager(config, client)
            client.sid = "old-sid"
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch("mcp_synology.core.auth.kr", _no_keyring()),
                caplog.at_level(logging.INFO),
            ):
                for _ in range(3):
                    with pytest.raises(AuthenticationError):
                        await client.request("SYNO.FileStation.List", "list_share", version=2)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert not any("Re-authenticating" in r.getMessage() for r in caplog.records)

    def test_the_remembered_refusal_keeps_no_frames(self) -> None:
        # The raised refusal's traceback holds the login's frames, and its password.
        refusals = LoginRefusals()
        try:
            try:
                raise AuthenticationError("No such account", code=400, suggestion="x")
            except AuthenticationError as e:
                raise refusals.refuse("fp", e) from e
        except AuthenticationError:
            pass
        stored = refusals._refused[1] if refusals._refused else None
        assert stored is not None
        assert stored.__traceback__ is None and stored.__cause__ is None


class TestWaitersShareAFailedLogin:
    """QA round 1 on #461: calls waiting on a re-login that fails get its error instead of
    each sending a login of their own; a later call tries again."""

    async def test_waiters_share_the_failure(self) -> None:
        calls: list[int] = []

        async def failing_reauth() -> None:
            calls.append(1)
            await asyncio.sleep(0.01)
            raise httpx.ConnectError("refused")

        async with _make_client() as client:
            client.set_re_auth_callback(failing_reauth)
            client.sid = "old-sid"
            results = await asyncio.gather(
                *(
                    client._re_authenticate(119, "SYNO.FileStation.List", "old-sid")
                    for _ in range(5)
                ),
                return_exceptions=True,
            )
            assert all(isinstance(r, httpx.ConnectError) for r in results)
            assert calls == [1]
            # QA round 2: the kept failure holds no frames (the login's password), and each
            # waiter gets its own copy rather than one object every raise adds to.
            stored = client._login_failure
            assert stored is not None and stored.__traceback__ is None
            assert len({id(r) for r in results}) == 5
            with pytest.raises(httpx.ConnectError):  # a later call tries afresh
                await client._re_authenticate(119, "SYNO.FileStation.List", "old-sid")
            assert calls == [1, 1]

    async def test_a_kept_transport_error_holds_no_password(self) -> None:
        """An httpx error carries its request, and the login POST's body holds the
        password; the kept copy's request has none (pre-QA on QA round 2, #461)."""
        login = httpx.Request("POST", "https://nas:5001/webapi/entry.cgi", data={"passwd": "Hx7q"})

        async def failing_reauth() -> None:
            raise httpx.ConnectError("refused", request=login)

        async with _make_client() as client:
            client.set_re_auth_callback(failing_reauth)
            client.sid = "old-sid"
            with pytest.raises(httpx.ConnectError):
                await client._re_authenticate(119, "SYNO.FileStation.List", "old-sid")
            kept = client._login_failure
            assert isinstance(kept, httpx.ConnectError)
            assert b"Hx7q" not in kept.request.read()
            assert (kept.request.method, kept.request.url) == (login.method, login.url)

    async def test_a_canceled_login_leaves_waiters_to_try(self) -> None:
        # An earlier failure is not handed to requests that waited on a later, canceled
        # attempt: they log in themselves.
        calls: list[str] = []
        started = asyncio.Event()

        async def reauth() -> None:
            calls.append("try")
            if len(calls) == 1:
                raise httpx.ConnectError("refused")
            if len(calls) == 2:
                started.set()
                await asyncio.sleep(10)

        async with _make_client() as client:
            client.set_re_auth_callback(reauth)
            client.sid = "old-sid"
            with pytest.raises(httpx.ConnectError):
                await client._re_authenticate(119, "X", "old-sid")
            second = asyncio.create_task(client._re_authenticate(119, "X", "old-sid"))
            await started.wait()
            third = asyncio.create_task(client._re_authenticate(119, "X", "old-sid"))
            await asyncio.sleep(0)
            second.cancel()
            await third  # not the earlier ConnectError: it logs in itself
        assert calls == ["try", "try", "try"]
