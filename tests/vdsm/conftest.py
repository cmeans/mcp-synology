"""Pytest fixtures for virtual-dsm integration tests.

Provides a session-scoped container fixture and a function-scoped nas_client
fixture that matches the shape of the real-NAS fixture in test_integration.py.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
import pytest

from mcp_synology.core.auth import AuthManager
from mcp_synology.core.client import DsmClient
from mcp_synology.core.config import AppConfig
from tests.personas import Persona
from tests.vdsm.config import (
    DEFAULT_DSM_VERSION,
    DEFAULT_FRESH_USER,
    DEFAULT_TEST_USER,
    DSM_VERSIONS,
    ds_start_fallback_allowed,
)
from tests.vdsm.container import VirtualDsmContainer
from tests.vdsm.golden_image import (
    has_golden_image,
    load_golden_meta,
    restore_golden_image,
    stale_golden_image_reason,
)
from tests.vdsm.ssh import ssh_exec

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add --dsm-version command line option."""
    parser.addoption(
        "--dsm-version",
        default=DEFAULT_DSM_VERSION,
        choices=list(DSM_VERSIONS.keys()),
        help=f"DSM version to test against (default: {DEFAULT_DSM_VERSION})",
    )


@pytest.fixture(scope="session")
def dsm_version(request: pytest.FixtureRequest) -> str:
    """The DSM version to test, from --dsm-version CLI option."""
    return request.config.getoption("--dsm-version")  # type: ignore[no-any-return]


# DSM answers SYNO.API.Info before it has finished booting: on CI (#107) the
# logins right after that got DSM error 499 for a couple of seconds and a
# request was dropped mid-response. DSM's own boot state machine marks the
# end of boot: syno-bootup-done.service runs ``synobootseq --set-boot-done``
# and is ordered After=multi-user.target syno-login.target, so "boot done"
# means the login service is up. The suite waits for that over SSH.
_BOOT_DONE_TIMEOUT = 300
_BOOT_POLL_INTERVAL = 2
_SYNOBOOTSEQ = "/usr/syno/bin/synobootseq"


def _ssh(
    container: VirtualDsmContainer,
    password: str,
    cmd: str,
    timeout: int = 30,
    *,
    sudo: bool = True,
) -> tuple[int, str]:
    """ssh_exec that reports a hung or unreachable sshd as a failed command.

    Use ``sudo=False`` when the output is the answer: ``sudo -S`` prints its
    prompt without a newline, so the first output line arrives as
    ``Password:<output>`` and ssh_exec's prompt filter drops it.
    """
    try:
        return ssh_exec(
            container.host, container.ssh_port, password, cmd, sudo=sudo, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return -1, f"ssh timed out after {timeout}s"


def _login_problem(base_url: str, user: str, password: str) -> str | None:
    """Log in and out once; None on success, else what went wrong."""
    try:
        resp = httpx.get(
            f"{base_url}/webapi/auth.cgi",
            params={
                "api": "SYNO.API.Auth",
                "version": "3",
                "method": "login",
                "account": user,
                "passwd": password,
                "session": "MCPReadiness",
                "format": "sid",
            },
            timeout=15,
        ).json()
        if not resp.get("success"):
            return f"DSM error {resp.get('error', {}).get('code')}"
        httpx.get(
            f"{base_url}/webapi/auth.cgi",
            params={
                "api": "SYNO.API.Auth",
                "version": "1",
                "method": "logout",
                "session": "MCPReadiness",
                "_sid": resp["data"]["sid"],
            },
            timeout=15,
        )
    except (httpx.HTTPError, ValueError) as e:
        return f"{type(e).__name__}: {e}"
    return None


def _wait_for_boot_done(container: VirtualDsmContainer, user: str, password: str) -> None:
    """Block until DSM reports boot done, then require one successful login."""
    deadline = time.monotonic() + _BOOT_DONE_TIMEOUT
    rc, out = _ssh(container, password, f"{_SYNOBOOTSEQ} --is-ready")
    while rc != 0:
        if time.monotonic() > deadline:
            _, stage = _ssh(container, password, f"{_SYNOBOOTSEQ} --get", sudo=False)
            pytest.fail(
                f"DSM did not report boot done within {_BOOT_DONE_TIMEOUT}s "
                f"(synobootseq --get: {stage.strip()!r}; last --is-ready rc={rc}: "
                f"{out.strip()[-300:]!r})",
                pytrace=False,
            )
        time.sleep(_BOOT_POLL_INTERVAL)
        rc, out = _ssh(container, password, f"{_SYNOBOOTSEQ} --is-ready")
    waited = _BOOT_DONE_TIMEOUT - (deadline - time.monotonic())
    logger.info("DSM reports boot done %.0fs after its web API answered", waited)
    problem = _login_problem(container.base_url, user, password)
    if problem:
        pytest.fail(f"DSM reports boot done, but login failed: {problem}", pytrace=False)


@pytest.fixture(scope="session")
def vdsm_container(dsm_version: str) -> Any:
    """Boot a virtual-dsm container from golden image for the test session.

    Yields the VirtualDsmContainer instance. Skips if prerequisites are missing.
    """
    if not Path("/dev/kvm").exists():
        pytest.skip("virtual-dsm requires /dev/kvm (Linux with KVM support)")

    if not has_golden_image(dsm_version):
        pytest.skip(
            f"Golden image not found for DSM {dsm_version}. "
            f"Run: python scripts/vdsm_setup.py --version {dsm_version}"
        )

    # Fail rather than skip: a stale image would boot fine and then fail tests
    # with errors (e.g. a 105 from a missing grant) that hide the real cause.
    stale = stale_golden_image_reason(load_golden_meta(dsm_version), dsm_version)
    if stale:
        pytest.fail(stale)

    storage_dir = restore_golden_image(dsm_version)
    logger.info("Restored golden image for DSM %s to %s", dsm_version, storage_dir)

    container = VirtualDsmContainer(dsm_version, storage_dir)
    try:
        container.start()
        meta = load_golden_meta(dsm_version)
        _wait_for_boot_done(
            container,
            str(meta.get("admin_user", "admin")),
            str(meta.get("admin_password", "")),
        )
        yield container
    finally:
        container.stop()


# Download Station readiness. DSM starts the package at boot as the systemd
# unit pkgctl-DownloadStation.service (ordered after nginx and
# syno-login.target), so DS is ready when that unit is active and an
# authenticated Task.list succeeds. If not, the fixture collects diagnostics
# and starts DS with the package's own script (what the bake uses). Locally a
# DS that then answers is a logged workaround; on CI the session fails
# (config.ds_start_fallback_allowed), since there it means a broken bake. The
# case that prompted this (#107): DS's CGIs answered 502 because
# pkg-synodl-apid.service was masked, the image having been saved from a
# SIGKILLed VM. The bake now shuts DSM down cleanly (VirtualDsmContainer.shutdown).
_DS_UNIT = "pkgctl-DownloadStation.service"
_DS_PROBE_PATH = "/webapi/DownloadStation/task.cgi"
_DS_READY_WAIT = 90
_DS_KICK_WAIT = 120
_DS_START = "/var/packages/DownloadStation/scripts/start-stop-status start"
# (command, needs root). Root-less where possible: see _ssh on sudo -S.
_DS_DIAGNOSTICS = (
    ("systemctl is-enabled pkg-synodl-apid.service", False),  # "masked" on CI (#107)
    ("systemctl is-active pkg-synodl-apid.service", False),
    (f"systemctl status {_DS_UNIT} --no-pager", False),
    (f"journalctl -u {_DS_UNIT} -n 40 --no-pager", True),
    ("/usr/syno/bin/synopkg status DownloadStation", False),
    ("tail -n 30 /var/log/synopkg.log", True),
    ("ps -ef", False),
)


class _DsProbe:
    """Authenticated ``Task.list`` probe; logs in once and reuses the session.

    ``Task.list`` is the readiness signal because it needs the DS daemon:
    with the daemon stopped, unauthenticated calls still return JSON (105)
    and ``Info.getinfo`` still succeeds, while ``Task.list`` answers error 100.
    On CI the CGIs answered 502 instead. Either counts as not ready.
    """

    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.base_url = base_url
        self.user = user
        self.password = password
        self.sid: str | None = None

    def _login(self) -> str | None:
        resp = httpx.get(
            f"{self.base_url}/webapi/auth.cgi",
            params={
                "api": "SYNO.API.Auth",
                "version": "3",
                "method": "login",
                "account": self.user,
                "passwd": self.password,
                "session": "DownloadStation",
                "format": "sid",
            },
            timeout=15,
        ).json()
        if not resp.get("success"):
            return f"login failed: {resp.get('error')}"
        self.sid = resp["data"]["sid"]
        return None

    def problem(self) -> str | None:
        """None if DS lists tasks, else why not."""
        try:
            if self.sid is None and (failed := self._login()):
                return failed
            resp = httpx.get(
                f"{self.base_url}{_DS_PROBE_PATH}",
                params={
                    "api": "SYNO.DownloadStation.Task",
                    "version": "1",
                    "method": "list",
                    "_sid": self.sid or "",
                },
                timeout=15,
            )
            if resp.status_code >= 400:
                return f"HTTP {resp.status_code}"
            body = resp.json()
        except httpx.HTTPError as e:
            return f"{type(e).__name__}: {e}"
        except ValueError:
            return "non-JSON response"
        if body.get("success"):
            return None
        code = body.get("error", {}).get("code")
        if code in (106, 107, 119):
            self.sid = None  # session lost; log in again on the next poll
        return f"DSM error {code}"


def _poll(check: Callable[[], str | None], timeout: float) -> str | None:
    """Call ``check`` every 3 s until it returns None; return its last problem."""
    deadline = time.monotonic() + timeout
    problem = check()
    while problem and time.monotonic() < deadline:
        time.sleep(3)
        problem = check()
    return problem


def _ds_diagnostics(container: VirtualDsmContainer, password: str) -> str:
    out = []
    for cmd, sudo in _DS_DIAGNOSTICS:
        rc, text = _ssh(container, password, cmd, sudo=sudo)
        if cmd == "ps -ef":
            procs = [" ".join(ln.split()[7:])[:160] for ln in text.splitlines() if "ownload" in ln]
            text = "\n".join(sorted(set(procs))[:15])
        out.append(f"$ {cmd} (rc={rc})\n{text.strip()[-1500:]}")
    return "\n".join(out)


@pytest.fixture(scope="session")
def ds_backend_ready(vdsm_container: VirtualDsmContainer, dsm_version: str) -> None:
    """Wait once per session for Download Station; start it locally if it never comes up.

    If DS is still down after the wait, the fixture collects diagnostics and
    starts DS with its package script. Locally, a DS that then answers is a
    logged workaround; on CI it fails the session (see
    ``ds_start_fallback_allowed``), because there it means the bake is broken.

    Session-scoped so an unavailable DS costs one wait, not one per test:
    pytest caches a session fixture's failure and re-raises it at once for
    every later DS test.
    """
    base_url = vdsm_container.base_url
    meta = load_golden_meta(dsm_version)
    password = str(meta.get("admin_password", ""))
    probe = _DsProbe(base_url, str(meta.get("admin_user", "admin")), password)

    def unit_then_api() -> str | None:
        _, state = _ssh(vdsm_container, password, f"systemctl is-active {_DS_UNIT}", sudo=False)
        if state.strip() != "active":
            return f"{_DS_UNIT} is {state.strip() or 'unknown'}"
        return probe.problem()

    problem = _poll(unit_then_api, _DS_READY_WAIT)
    if problem is None:
        return

    before = _ds_diagnostics(vdsm_container, password)
    rc, out = _ssh(vdsm_container, password, _DS_START, timeout=180)
    # Started outside systemd, the unit may stay inactive; the API is the test.
    after_problem = _poll(probe.problem, _DS_KICK_WAIT)
    report = (
        f"Download Station did not answer at {base_url}{_DS_PROBE_PATH} within "
        f"{_DS_READY_WAIT}s of DSM boot done (last: {problem}).\n"
        f"Diagnostics before starting it:\n{before}\n"
        f"$ {_DS_START} (rc={rc})\n{out.strip()[-1500:]}"
    )
    if after_problem is None and not ds_start_fallback_allowed(os.environ):
        pytest.fail(
            f"{report}\nDS answered only after the conftest started it. On CI that means "
            "the bake is broken (DS does not start on boot of the golden image); the "
            "start-it-ourselves fallback runs only locally.",
            pytrace=False,
        )
    if after_problem is None:
        warnings.warn(
            f"{report}\nWORKAROUND: DS answered only after the conftest started it.",
            stacklevel=1,
        )
        return
    pytest.fail(
        f"{report}\nStill not answering {_DS_KICK_WAIT}s later (last: {after_problem}).\n"
        f"Diagnostics after:\n{_ds_diagnostics(vdsm_container, password)}",
        pytrace=False,
    )


@pytest.fixture(scope="session")
def vdsm_config(
    vdsm_container: VirtualDsmContainer,
    dsm_version: str,
) -> tuple[AppConfig, dict[str, str]]:
    """Build AppConfig and test_paths from the running virtual-dsm container.

    Session-scoped — config doesn't change between tests.
    """
    meta = load_golden_meta(dsm_version)
    test_paths: dict[str, str] = meta.get("test_paths", {})  # type: ignore[assignment]

    parsed = urlparse(vdsm_container.base_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5000

    config = AppConfig(
        schema_version=1,
        instance_id=f"vdsm-{dsm_version.replace('.', '-')}",
        connection={
            "host": host,
            "port": port,
            "https": False,
            "verify_ssl": False,
        },
        auth={
            "username": meta.get("test_user", DEFAULT_TEST_USER),
            "password": meta.get("test_password", ""),
        },
        modules={
            "filestation": {
                "enabled": True,
                "permission": "write",
            },
            "system": {
                "enabled": True,
            },
        },
    )
    return config, test_paths


@pytest.fixture
def integration_config(
    vdsm_config: tuple[AppConfig, dict[str, str]],
) -> tuple[AppConfig, dict[str, str]]:
    """Override integration_config to point at virtual-dsm.

    This shadows the integration_config fixture from test_integration.py
    for tests collected under tests/vdsm/.
    """
    return vdsm_config


@pytest.fixture
async def refresh_search_index(
    vdsm_container: VirtualDsmContainer,
    dsm_version: str,
) -> Any:
    """Override the no-op fixture from test_integration.py.

    Invokes `synoindex -A -d <path>` via SSH on the vdsm container so
    runtime-created directories are immediately discoverable by DSM's
    search service. Without this, DSM Universal Search on non-indexed
    shares takes several minutes (sometimes longer than any reasonable
    test retry budget) to crawl freshly-created subdirectories.

    Best-effort: a non-zero return from synoindex is logged but not
    raised — the test's existing retry loop is the safety net for
    indexer hiccups.
    """
    import asyncio

    from tests.vdsm.ssh import ssh_exec

    meta = load_golden_meta(dsm_version)
    admin_password = str(meta.get("admin_password", ""))
    host = vdsm_container.host
    port = vdsm_container.ssh_port

    async def _refresh(path: str) -> None:
        # Translate the share-relative path the test uses (e.g.
        # "/testshare/Documents/Bambu Studio") into the on-volume path
        # synoindex needs (e.g. "/volume1/testshare/Documents/Bambu Studio").
        on_volume = f"/volume1{path}"
        cmd = f"/usr/syno/bin/synoindex -A -d '{on_volume}'"
        rc, out = await asyncio.to_thread(
            ssh_exec, host, port, admin_password, cmd, sudo=True, timeout=20
        )
        if rc != 0:
            logger.warning(
                "synoindex -A -d %r returned rc=%d (output: %s) — relying on test retries",
                on_volume,
                rc,
                out,
            )
        else:
            logger.info("synoindex registered %s with DSM search index", on_volume)

    return _refresh


async def _user_session(
    config: AppConfig, test_paths: dict[str, str], dsm_version: str, label: str
) -> Any:
    """Yield ``(client, auth, config, test_paths)`` logged in with ``config``'s account."""
    conn = config.connection
    assert conn is not None
    client = DsmClient(base_url=f"http://{conn.host}:{conn.port}", verify_ssl=False, timeout=30)
    async with client:
        cache = await client.query_api_info()
        logger.info("vDSM API cache: %d APIs discovered (DSM %s)", len(cache), dsm_version)
        auth = AuthManager(config, client)
        sid = await auth.login()
        logger.info("vDSM %s authenticated, SID=%s... (DSM %s)", label, sid[:8], dsm_version)
        yield client, auth, config, test_paths
        await auth.logout()


@pytest.fixture
async def nas_client(
    vdsm_config: tuple[AppConfig, dict[str, str]],
    dsm_version: str,
) -> Any:
    """Provide a DsmClient logged in as the golden image's **non-admin** test user.

    Function-scoped async generator — same shape as the real-NAS fixture
    in test_integration.py: yields (client, auth, config, test_paths). Tests
    that need admin use ``admin_client``; ``nas_persona`` says who this is.
    """
    config, test_paths = vdsm_config
    async for session in _user_session(config, test_paths, dsm_version, "test user"):
        yield session


@pytest.fixture
def nas_persona(dsm_version: str) -> Persona:
    """vdsm's ``nas_client`` persona is known: the bake's non-admin test user."""
    meta = load_golden_meta(dsm_version)
    return Persona(username=str(meta.get("test_user", DEFAULT_TEST_USER)), is_admin=False)


@pytest.fixture
async def fresh_client(
    vdsm_config: tuple[AppConfig, dict[str, str]],
    dsm_version: str,
) -> Any:
    """A DS-granted non-admin that has never set a DS default destination (#124)."""
    config, test_paths = vdsm_config
    meta = load_golden_meta(dsm_version)
    fresh = config.model_copy(
        update={
            "instance_id": f"{config.instance_id}-fresh",
            "auth": config.auth.model_copy(
                update={
                    "username": meta.get("fresh_user", DEFAULT_FRESH_USER),
                    "password": meta.get("fresh_password", ""),
                }
            ),
        }
    )
    async for session in _user_session(fresh, test_paths, dsm_version, "fresh user"):
        yield session


@pytest.fixture
async def admin_client(
    vdsm_config: tuple[AppConfig, dict[str, str]],
    dsm_version: str,
) -> Any:
    """Provide an admin-authenticated DsmClient for virtual-dsm.

    Virtual-dsm typically runs with admin credentials, so this is the
    same as nas_client but authenticates as admin.
    """
    config, test_paths = vdsm_config
    conn = config.connection
    assert conn is not None

    base_url = f"http://{conn.host}:{conn.port}"
    meta = load_golden_meta(dsm_version)

    # Build admin config
    admin_config = AppConfig(
        schema_version=1,
        instance_id=f"vdsm-admin-{dsm_version.replace('.', '-')}",
        connection={
            "host": conn.host,
            "port": conn.port,
            "https": False,
            "verify_ssl": False,
        },
        auth={
            "username": meta.get("admin_user", "admin"),
            "password": meta.get("admin_password", ""),
        },
        modules={
            "filestation": {"enabled": True, "permission": "write"},
            "system": {"enabled": True},
        },
    )

    client = DsmClient(
        base_url=base_url,
        verify_ssl=False,
        timeout=30,
    )

    async with client:
        await client.query_api_info()
        auth = AuthManager(admin_config, client)
        sid = await auth.login()
        logger.info("vDSM admin authenticated, SID=%s... (DSM %s)", sid[:8], dsm_version)

        yield client, auth, admin_config, test_paths

        await auth.logout()
