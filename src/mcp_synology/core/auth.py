"""Auth manager: session lifecycle, 2FA, credential resolution.

Strategy chain auto-detects 2FA vs non-2FA:
1. If device_id available -> attempt login with device token (2FA remembered device)
2. If no device_id and login succeeds -> simple login (no 2FA)
3. If no device_id and error 403 -> 2FA required, user must run setup CLI
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from typing import TYPE_CHECKING, Any

import keyring as kr
from keyring.errors import KeyringError

from mcp_synology.core.errors import AuthenticationError, SynologyError

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp_synology.core.client import DsmClient
    from mcp_synology.core.config import AppConfig

logger = logging.getLogger(__name__)

# DSM Auth API error code for 2FA required
_ERROR_2FA_REQUIRED = 403


def _present_or_none(value: str | None) -> str | None:
    """Return value unchanged if it contains non-whitespace content; else None.

    Used to filter empty / whitespace-only credentials at every read site
    (env vars, plaintext config, OS keyring) so they fall through the
    resolution chain instead of flowing into `login()` as bogus credentials
    that surface as a generic DSM 400.
    """
    return value if (value and value.strip()) else None


def resolve_credentials(config: AppConfig) -> tuple[str, str, str | None]:
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
    # _present_or_none normalizes empty / whitespace-only to None so they
    # fall through to the next strategy instead of becoming bogus creds.
    username = _present_or_none(os.environ.get("SYNOLOGY_USERNAME"))
    if username:
        logger.debug("Username from env var SYNOLOGY_USERNAME: %s", username)
    password = _present_or_none(os.environ.get("SYNOLOGY_PASSWORD"))
    if password:
        logger.debug("Password from env var SYNOLOGY_PASSWORD")
    device_id = _present_or_none(os.environ.get("SYNOLOGY_DEVICE_ID"))
    if device_id:
        logger.debug("Device ID from env var SYNOLOGY_DEVICE_ID")

    # 2. Config file (explicit, if present)
    if not username and (cfg_user := _present_or_none(config.auth.username)):
        username = cfg_user
        logger.debug("Username from config file: %s", username)
    if not password and (cfg_pass := _present_or_none(config.auth.password)):
        password = cfg_pass
        logger.debug("Password from config file (plaintext)")
    if not device_id and (cfg_dev := _present_or_none(config.auth.device_id)):
        device_id = cfg_dev
        logger.debug("Device ID from config file")

    # 3. OS keyring (implicit default — set by 'mcp-synology setup')
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
                # INFO not DEBUG — without D-Bus the keyring path is dead
                # for this session; the operator needs to know how to
                # recover. Closes #38 (the operator-actionable hint).
                logger.info(
                    "D-Bus socket not found at %s; OS keyring is unavailable. "
                    "Run 'mcp-synology setup' from a real desktop session "
                    "(or under 'dbus-run-session'), or set credentials via the "
                    "SYNOLOGY_USERNAME / SYNOLOGY_PASSWORD / SYNOLOGY_DEVICE_ID "
                    "env vars to bypass the keyring entirely.",
                    socket_path,
                )

        try:
            service = f"mcp-synology/{config.instance_id or 'default'}"
            logger.debug("Trying keyring service: %s", service)
            kr_user = _present_or_none(kr.get_password(service, "username"))
            kr_pass = _present_or_none(kr.get_password(service, "password"))
            kr_device = _present_or_none(kr.get_password(service, "device_id"))
            if kr_user and not username:
                username = kr_user
                logger.debug("Username from keyring: %s", username)
            if kr_pass and not password:
                password = kr_pass
                logger.debug("Password from keyring")
            if kr_device and not device_id:
                device_id = kr_device
                logger.debug("Device ID from keyring")
        except KeyringError as e:
            # Typed keyring failure (NoKeyringError, InitError,
            # KeyringLocked, PasswordSetError, etc.). Log at DEBUG with
            # `exc_info` so `mcp-synology check -v` surfaces the actual
            # cause (locked keychain on macOS is operator-actionable;
            # NoKeyringError on a headless host signals a config issue).
            # Closes #38 — previously caught by a bare `except Exception`
            # with a flat "Keyring not available." line that hid all of
            # these. Falls through to the no-credentials check below;
            # the resolver chain still works without keyring.
            logger.debug("Keyring access failed: %s", e, exc_info=True)
        except OSError as e:
            # D-Bus socket present but unreachable, permission errors on
            # the OS keychain backend, etc. Same DEBUG-with-exc_info
            # treatment as the typed-error case above. The D-Bus pre-check above
            # line 123-131 already emitted an INFO hint if the D-Bus
            # socket was missing entirely; this branch covers the
            # reach-but-fail variant.
            logger.debug("Keyring OS-level error: %s", e, exc_info=True)

    if not username or not password:
        raise AuthenticationError(
            "No credentials found.",
            suggestion=(
                "Run 'mcp-synology setup' to store credentials in the OS keyring, "
                "or set SYNOLOGY_USERNAME and SYNOLOGY_PASSWORD environment variables."
            ),
        )

    logger.debug(
        "Credentials resolved: user=%s, has_password=yes, has_device_id=%s",
        username,
        "yes" if device_id else "no",
    )
    return username, password, device_id


def explicit_credential_sources(config: AppConfig) -> list[str]:
    """Where each credential that overrides the keyring comes from, in
    `resolve_credentials`' order: `setup` writes only the keyring, so these must be
    changed where they are (#447, QA round 1 on #461)."""
    import os

    env: list[str] = []
    file: list[str] = []
    for key, var in (
        ("username", "SYNOLOGY_USERNAME"),
        ("password", "SYNOLOGY_PASSWORD"),
        ("device_id", "SYNOLOGY_DEVICE_ID"),
    ):
        if _present_or_none(os.environ.get(var)):
            env.append(var)
        elif _present_or_none(getattr(config.auth, key)):
            file.append(f"auth.{key}")
    found: list[str] = []
    if env:
        found.append(f"{' and '.join(env)} in the server's environment (the MCP client's config)")
    if file:
        found.append(f"{' and '.join(file)} in the config file")
    return found


def credentials_fingerprint(username: str, password: str, device_id: str | None) -> str:
    """A digest of the credentials, so a refused login can be recognized without
    keeping a second copy of the password."""
    joined = "\0".join((username, password, device_id or ""))
    return hashlib.sha256(joined.encode()).hexdigest()


class LoginRefusals:
    """Remembers the credentials DSM refused, so they aren't sent again (#447).

    DSM's Auto Block (on by default: 10 failed logins within 5 minutes) blocks the
    computer's IP address, for the user's browser and every other client too. A refused
    login retried on every tool call, or on every session error, would trip it. The
    refusal holds until the credentials change (``mcp-synology setup`` writes new ones
    to the keyring) or the server restarts. A transport error or any other failure is
    never remembered: it says nothing about the credentials.
    """

    def __init__(self) -> None:
        self._refused: tuple[str, AuthenticationError] | None = None

    @property
    def any(self) -> bool:
        return self._refused is not None

    def check(self, fingerprint: str) -> None:
        """Raise the remembered refusal if these are the credentials DSM refused."""
        if self._refused is not None and self._refused[0] == fingerprint:
            error = self._refused[1]
            logger.debug("Not sending a login DSM already refused (error %s)", error.code)
            raise AuthenticationError(str(error), code=error.code, suggestion=error.suggestion)

    def refuse(
        self, fingerprint: str, error: AuthenticationError, explicit: list[str] | None = None
    ) -> AuthenticationError:
        """Remember DSM's refusal; return the error to raise, which says what happens next.

        ``explicit`` lists credentials set outside the keyring (environment or config
        file): `setup` can't change those, so the suggestion says where they are.
        """
        reason = str(error).rstrip(".")
        first = (error.suggestion or "").rstrip(".")
        if explicit:
            where = (
                "These credentials come from "
                + "; ".join(explicit)
                + ": change them there and restart the server ('mcp-synology setup' "
                "writes only the keyring, which those override)"
            )
        else:
            where = (
                "mcp-synology tries again once the stored credentials change "
                "('mcp-synology setup' stores new ones; the running server picks them up) "
                "or the server restarts"
            )
        refusal = AuthenticationError(
            f"DSM refused the login: {reason}. mcp-synology won't send these credentials "
            "again until they change or the server restarts, because repeated failed "
            "logins can trip DSM's Auto Block, which blocks this computer's IP address "
            "from the NAS.",
            code=error.code,
            suggestion=(
                (f"{first}. " if first else "") + f"{where}; 'mcp-synology check' tests the login."
            ),
        )
        self._refused = (fingerprint, refusal)
        return refusal

    def clear(self) -> None:
        self._refused = None


class AuthManager:
    """Manages DSM authentication and session lifecycle."""

    def __init__(
        self, config: AppConfig, client: DsmClient, refusals: LoginRefusals | None = None
    ) -> None:
        self._config = config
        self._client = client
        # Shared with the server's client manager, so a refusal outlives this
        # AuthManager when the first login fails (#447).
        self._refusals = refusals if refusals is not None else LoginRefusals()
        self._lock = asyncio.Lock()
        self._session_name = self._build_session_name()
        # Subscriber list — fired after a successful re-auth so module-owned
        # caches (e.g. filestation's `recycle_status`) can invalidate any
        # state that may have changed on the NAS between sessions. See #37.
        self._on_reauth_callbacks: list[Callable[[], None]] = []

        # Register ourselves as the re-auth callback
        self._client.set_re_auth_callback(self._re_authenticate)
        logger.debug("AuthManager initialized, session name: %s", self._session_name)

    def add_on_reauth_callback(self, cb: Callable[[], None]) -> None:
        """Register a callback to fire after each successful session re-auth.

        Modules that cache NAS state which can drift mid-session (the canonical
        case is filestation's recycle-bin probe cache) subscribe here so they
        can clear the cache and let the next operation re-probe against fresh
        DSM state. Callbacks must be cheap and synchronous; an exception
        from one callback is logged and does not prevent the rest from firing.
        """
        self._on_reauth_callbacks.append(cb)

    def _build_session_name(self, session_key: str | None = None) -> str:
        """Build a DSM session name.

        ``session_key=None`` (current default) produces
        ``MCPSynology_{instance_id}_{uuid8}`` — one shared session per
        ``AuthManager`` instance, which is the only mode exercised today
        under the stdio transport.

        ``session_key=<str>`` produces ``MCPSynology_{instance_id}_{session_key}``
        for the planned per-client session model under Streamable HTTP. The
        helper accepts a key now so a future per-client session pool can derive
        names without changing this method's contract; no production code path
        invokes it with a non-None key today. See ADR 0001.
        """
        instance_id = self._config.instance_id or "default"
        suffix = session_key if session_key is not None else uuid.uuid4().hex[:8]
        return f"MCPSynology_{instance_id}_{suffix}"

    def _resolve_credentials(self) -> tuple[str, str, str | None]:
        """Resolve credentials from the storage hierarchy (``resolve_credentials``)."""
        return resolve_credentials(self._config)

    async def login(self) -> str:
        """Authenticate with the NAS and return a session ID.

        Uses the strategy chain to handle 2FA automatically. Credentials DSM already
        refused aren't sent again (#447): the refusal is raised without contacting DSM.
        """
        username, password, device_id = self._resolve_credentials()
        fingerprint = credentials_fingerprint(username, password, device_id)
        self._refusals.check(fingerprint)

        params: dict[str, Any] = {
            "account": username,
            "passwd": password,
            "format": "sid",
        }

        # Path B: 2FA with remembered device
        if device_id:
            logger.debug("Login path: 2FA with device token")
            params["device_name"] = "MCPSynology"
            params["device_id"] = device_id
        else:
            logger.debug("Login path: simple (no device token)")

        try:
            data = await self._send_login(params, device_id)
        except AuthenticationError as e:
            if e.code is None:
                raise
            explicit = explicit_credential_sources(self._config)
            raise self._refusals.refuse(fingerprint, e, explicit) from e

        sid: str | None = data.get("sid")
        if not sid:
            raise AuthenticationError("Login succeeded but no session ID returned.")

        self._refusals.clear()
        self._client.sid = sid
        logger.info("Authenticated as '%s' (session: %s)", username, self._session_name)
        return sid

    async def _send_login(self, params: dict[str, Any], device_id: str | None) -> dict[str, Any]:
        """Send the login; a 403 becomes the 2FA error that says what to do."""
        try:
            # A POST keeps the password out of the URL (#235).
            return await self._client.request(
                "SYNO.API.Auth", "login", version=6, params=params, post_form=True
            )
        except SynologyError as e:
            if e.code == _ERROR_2FA_REQUIRED:
                if device_id:
                    # DSM no longer trusts the stored token, e.g. the trusted
                    # device was removed in DSM's security settings.
                    logger.debug("Login failed: DSM rejected the stored 2FA device token")
                    message = (
                        "2FA is required and DSM rejected the stored device token "
                        "(it may have been removed or invalidated on the NAS). "
                        "Run 'mcp-synology setup' to sign in with a new 2FA code."
                    )
                else:
                    logger.debug("Login failed: 2FA required but no device token available")
                    message = (
                        "2FA is required but no device token is available. "
                        "Run 'mcp-synology setup' to complete 2FA bootstrap."
                    )
                raise AuthenticationError(
                    message,
                    code=_ERROR_2FA_REQUIRED,
                    suggestion="Run: mcp-synology setup --config <your-config>",
                ) from e
            raise

    async def logout(self, *, raise_errors: bool = False) -> None:
        """Log out the current session.

        A DSM error is logged at DEBUG and swallowed (the session may already be
        expired), unless ``raise_errors`` is set: shutdown uses that to warn (#402).
        """
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
            if raise_errors:
                raise
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

        DsmClient runs one re-login at a time and skips it when another request's
        re-login already replaced the session (#359). The expired session stays in
        place until the new one exists: cleared first, every request sent during the
        login would go without a session and queue its own re-login. A refused login
        isn't sent again (#447).

        Fires every registered ``on_reauth`` callback after the new session is
        live so module-owned caches (filestation's recycle-bin probe, etc.)
        can drop entries that may have drifted while the session was down.
        """
        async with self._lock:
            logger.info("Re-authenticating session '%s'.", self._session_name)
            await self.login()
            # NOTE: callbacks fire INSIDE `self._lock`. Per `add_on_reauth_callback`'s
            # contract they must be cheap and synchronous — anything heavy here would
            # block every other coroutine waiting to re-auth. Today's only subscriber
            # is `recycle_status.clear` (O(n_shares)); keep new callbacks similarly
            # trivial or move the work to a deferred task.
            for cb in self._on_reauth_callbacks:
                try:
                    cb()
                except Exception as e:  # noqa: BLE001 — best-effort dispatch
                    logger.warning("on_reauth callback %r raised: %s", cb, e)
