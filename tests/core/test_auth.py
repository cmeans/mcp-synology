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
            pytest.raises(AuthenticationError, match="No credentials"),
        ):
            auth._resolve_credentials()

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
            if params.get("method") == "login":
                return httpx.Response(200, json={"success": True, "data": {"sid": "new-sid"}})
            return httpx.Response(200, json={"success": True, "data": {"shares": []}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

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
            if params.get("method") == "login":
                return httpx.Response(200, json={"success": True, "data": {"sid": "new-sid"}})
            return httpx.Response(200, json={"success": True, "data": {"shares": []}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

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
