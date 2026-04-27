"""Pytest fixtures for virtual-dsm integration tests.

Provides a session-scoped container fixture and a function-scoped nas_client
fixture that matches the shape of the real-NAS fixture in test_integration.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from mcp_synology.core.auth import AuthManager
from mcp_synology.core.client import DsmClient
from mcp_synology.core.config import AppConfig
from tests.vdsm.config import DEFAULT_DSM_VERSION, DSM_VERSIONS
from tests.vdsm.container import VirtualDsmContainer
from tests.vdsm.golden_image import has_golden_image, load_golden_meta, restore_golden_image

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

    storage_dir = restore_golden_image(dsm_version)
    logger.info("Restored golden image for DSM %s to %s", dsm_version, storage_dir)

    container = VirtualDsmContainer(dsm_version, storage_dir)
    try:
        container.start()
        yield container
    finally:
        container.stop()


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
            "username": meta.get("admin_user", "admin"),
            "password": meta.get("admin_password", ""),
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


@pytest.fixture
async def nas_client(
    vdsm_config: tuple[AppConfig, dict[str, str]],
    dsm_version: str,
) -> Any:
    """Provide an authenticated DsmClient connected to virtual-dsm.

    Function-scoped async generator — same shape as the real-NAS fixture
    in test_integration.py: yields (client, auth, config, test_paths).
    """
    config, test_paths = vdsm_config
    conn = config.connection
    assert conn is not None

    base_url = f"http://{conn.host}:{conn.port}"

    client = DsmClient(
        base_url=base_url,
        verify_ssl=False,
        timeout=30,
    )

    async with client:
        cache = await client.query_api_info()
        logger.info("vDSM API cache: %d APIs discovered (DSM %s)", len(cache), dsm_version)

        auth = AuthManager(config, client)
        sid = await auth.login()
        logger.info("vDSM authenticated, SID=%s... (DSM %s)", sid[:8], dsm_version)

        yield client, auth, config, test_paths

        await auth.logout()


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
