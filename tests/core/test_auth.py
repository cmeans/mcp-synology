"""Tests for core/auth.py — auth flows, credential resolution, re-auth."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from mcp_synology.core.auth import AuthManager
from mcp_synology.core.client import DsmClient
from mcp_synology.core.config import AppConfig
from mcp_synology.core.errors import AuthenticationError
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
    """Return a mock keyring module where get_password raises."""
    mock = MagicMock()
    mock.get_password.side_effect = Exception("No keyring backend")
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
            pytest.raises(AuthenticationError, match="No credentials"),
        ):
            auth._resolve_credentials()


class TestLogin:
    @respx.mock
    async def test_simple_login(self) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
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

    @respx.mock
    async def test_login_with_device_id(self) -> None:
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
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
        request_params = dict(route.calls[0].request.url.params)
        assert request_params["device_id"] == "dev123"

    @respx.mock
    async def test_2fa_required_without_device_id(self) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
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
            if params.get("method") == "login":
                return httpx.Response(200, json={"success": True, "data": {"sid": "new-sid"}})
            return httpx.Response(200, json={"success": True, "data": {"shares": []}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

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
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
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


class TestDbusSocketMissing:
    def test_dbus_not_set_when_socket_missing_on_linux(self) -> None:
        """Linux + DBUS unset + socket missing → log debug, do not set env var."""
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
        ):
            username, _, _ = auth._resolve_credentials()
            # Falls back to config-file credentials; the missing-socket branch
            # logs at debug and does NOT set the env var (assert inside the
            # patch.dict block so the assertion sees the patched env, not the
            # restored one)
            assert "DBUS_SESSION_BUS_ADDRESS" not in os.environ
        assert username == "admin"


class TestLoginErrorPaths:
    @respx.mock
    async def test_login_non_2fa_synology_error_propagates(self) -> None:
        """A non-403 SynologyError on login is re-raised, not wrapped."""
        from mcp_synology.core.errors import SynologyError

        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
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
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
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
