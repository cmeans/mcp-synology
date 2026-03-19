"""Auth manager: session lifecycle, 2FA, credential resolution.

Strategy chain auto-detects 2FA vs non-2FA:
1. If device_id available -> attempt login with device token (2FA remembered device)
2. If no device_id and login succeeds -> simple login (no 2FA)
3. If no device_id and error 403 -> 2FA required, user must run setup CLI
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

import keyring as kr

from synology_mcp.core.errors import AuthenticationError, SynologyError

if TYPE_CHECKING:
    from synology_mcp.core.client import DsmClient
    from synology_mcp.core.config import AppConfig

logger = logging.getLogger(__name__)

# DSM Auth API error code for 2FA required
_ERROR_2FA_REQUIRED = 403


class AuthManager:
    """Manages DSM authentication and session lifecycle."""

    def __init__(self, config: AppConfig, client: DsmClient) -> None:
        self._config = config
        self._client = client
        self._lock = asyncio.Lock()
        self._session_name = self._build_session_name()

        # Register ourselves as the re-auth callback
        self._client.set_re_auth_callback(self._re_authenticate)
        logger.debug("AuthManager initialized, session name: %s", self._session_name)

    def _build_session_name(self) -> str:
        """Build a unique DSM session name: SynologyMCP_{instance_id}_{uuid}."""
        instance_id = self._config.instance_id or "default"
        unique_id = uuid.uuid4().hex[:8]
        return f"SynologyMCP_{instance_id}_{unique_id}"

    def _resolve_credentials(self) -> tuple[str, str, str | None]:
        """Resolve credentials from the storage hierarchy.

        Order: env vars -> config file -> keyring.
        Explicit sources (env, config) override the implicit default (keyring).
        Returns: (username, password, device_id or None)
        """
        import os
        import sys
        from pathlib import Path

        username: str | None = None
        password: str | None = None
        device_id: str | None = None

        # 1. Environment variables (highest priority — explicit override)
        username = os.environ.get("SYNOLOGY_USERNAME")
        if username:
            logger.debug("Username from env var SYNOLOGY_USERNAME: %s", username)
        password = os.environ.get("SYNOLOGY_PASSWORD")
        if password:
            logger.debug("Password from env var SYNOLOGY_PASSWORD")
        device_id = os.environ.get("SYNOLOGY_DEVICE_ID")
        if device_id:
            logger.debug("Device ID from env var SYNOLOGY_DEVICE_ID")

        # 2. Config file (explicit, if present)
        if not username and self._config.auth.username:
            username = self._config.auth.username
            logger.debug("Username from config file: %s", username)
        if not password and self._config.auth.password:
            password = self._config.auth.password
            logger.debug("Password from config file (plaintext)")
        if not device_id and self._config.auth.device_id:
            device_id = self._config.auth.device_id
            logger.debug("Device ID from config file")

        # 3. OS keyring (implicit default — set by 'synology-mcp setup')
        # Check keyring if we're missing username, password, OR device_id
        if not username or not password or not device_id:
            # On Linux, ensure D-Bus session address is available for keyring access.
            # When launched by Claude Desktop, the env var may not be inherited.
            # The keyring library reads DBUS_SESSION_BUS_ADDRESS from os.environ.
            if sys.platform == "linux" and not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
                uid = os.getuid()
                socket_path = Path(f"/run/user/{uid}/bus")
                if socket_path.exists():
                    dbus_addr = f"unix:path=/run/user/{uid}/bus"
                    os.environ["DBUS_SESSION_BUS_ADDRESS"] = dbus_addr
                    logger.debug("Set DBUS_SESSION_BUS_ADDRESS=%s for keyring access", dbus_addr)
                else:
                    logger.debug("D-Bus socket not found at %s; keyring may not work", socket_path)

            try:
                service = f"synology-mcp/{self._config.instance_id or 'default'}"
                logger.debug("Trying keyring service: %s", service)
                kr_user = kr.get_password(service, "username")
                kr_pass = kr.get_password(service, "password")
                kr_device = kr.get_password(service, "device_id")
                if kr_user and not username:
                    username = kr_user
                    logger.debug("Username from keyring: %s", username)
                if kr_pass and not password:
                    password = kr_pass
                    logger.debug("Password from keyring")
                if kr_device and not device_id:
                    device_id = kr_device
                    logger.debug("Device ID from keyring")
            except Exception:
                logger.debug("Keyring not available.")

        if not username or not password:
            msg = (
                "No credentials found. Run 'synology-mcp setup' to store credentials "
                "in the OS keyring, or set SYNOLOGY_USERNAME and SYNOLOGY_PASSWORD "
                "environment variables."
            )
            raise AuthenticationError(msg)

        logger.debug(
            "Credentials resolved: user=%s, has_password=yes, has_device_id=%s",
            username,
            "yes" if device_id else "no",
        )
        return username, password, device_id

    async def login(self) -> str:
        """Authenticate with the NAS and return a session ID.

        Uses the strategy chain to handle 2FA automatically.
        """
        username, password, device_id = self._resolve_credentials()

        params: dict[str, Any] = {
            "account": username,
            "passwd": password,
            "format": "sid",
        }

        # Path B: 2FA with remembered device
        if device_id:
            logger.debug("Login path: 2FA with device token")
            params["device_name"] = "SynologyMCP"
            params["device_id"] = device_id
        else:
            logger.debug("Login path: simple (no device token)")

        try:
            data = await self._client.request("SYNO.API.Auth", "login", version=6, params=params)
        except SynologyError as e:
            if e.code == _ERROR_2FA_REQUIRED:
                logger.debug("Login failed: 2FA required but no device token available")
                raise AuthenticationError(
                    "2FA is required but no device token is available. "
                    "Run 'synology-mcp setup' to complete 2FA bootstrap.",
                    code=_ERROR_2FA_REQUIRED,
                    suggestion="Run: synology-mcp setup --config <your-config>",
                ) from e
            raise

        sid: str | None = data.get("sid")
        if not sid:
            raise AuthenticationError("Login succeeded but no session ID returned.")

        self._client.sid = sid
        logger.info("Authenticated as '%s' (session: %s)", username, self._session_name)
        return sid

    async def logout(self) -> None:
        """Log out the current session."""
        if not self._client.sid:
            return

        logger.debug("Logging out session '%s'", self._session_name)
        try:
            await self._client.request(
                "SYNO.API.Auth",
                "logout",
                version=6,
                params={"session": self._session_name},
            )
            logger.debug("Logout successful")
        except SynologyError:
            logger.debug("Logout failed (session may already be expired).")
        finally:
            self._client.sid = None

    async def get_session(self) -> str:
        """Get a valid session ID, logging in if needed."""
        if self._client.sid:
            return self._client.sid
        logger.debug("No active session, logging in")
        return await self.login()

    async def _re_authenticate(self) -> None:
        """Re-authenticate transparently (called by DsmClient on session errors).

        Uses asyncio.Lock to prevent concurrent re-auth from multiple requests.
        """
        async with self._lock:
            # Another coroutine may have already re-authenticated
            logger.info("Re-authenticating session '%s'.", self._session_name)
            self._client.sid = None
            await self.login()
