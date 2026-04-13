"""Virtual-dsm Docker container lifecycle management using testcontainers."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

from tests.vdsm.config import (
    CONTAINER_CPU_CORES,
    CONTAINER_DISK_SIZE,
    CONTAINER_RAM,
    DSM_API_POLL_INTERVAL,
    DSM_BOOT_TIMEOUT,
    DSM_VERSIONS,
    DsmVersionInfo,
)

try:
    from testcontainers.core.container import DockerContainer
except ImportError:
    DockerContainer = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# Common non-default Docker socket locations (Rancher Desktop, Docker Desktop,
# Podman, rootless Docker, Colima, etc.)
_DOCKER_SOCKET_CANDIDATES = [
    Path.home() / ".docker/desktop/docker.sock",  # Rancher Desktop / Docker Desktop
    Path.home() / ".docker/run/docker.sock",  # Docker Desktop alt
    Path(f"/run/user/{os.getuid()}/docker.sock"),  # Rootless Docker
    Path(f"/run/user/{os.getuid()}/podman/podman.sock"),  # Podman
    Path.home() / ".colima/default/docker.sock",  # Colima (macOS)
]


def _ensure_docker_host() -> None:
    """Set DOCKER_HOST if not already set, preferring Podman for KVM access.

    virtual-dsm needs /dev/kvm passthrough. Docker Desktop and Rancher Desktop
    run containers inside a VM that typically lacks nested virtualization, so
    /dev/kvm isn't available. Podman runs containers natively on the host and
    can access /dev/kvm directly.

    Priority order:
    1. DOCKER_HOST already set — respect it
    2. Podman socket — preferred for KVM passthrough
    3. Default Docker socket (/var/run/docker.sock)
    4. Other Docker socket locations (Rancher Desktop, Docker Desktop, etc.)
    """
    if os.environ.get("DOCKER_HOST"):
        return

    # Prefer Podman — it runs natively on the host with direct KVM access.
    # Docker Desktop/Rancher Desktop run containers in a VM without KVM.
    podman_socket = Path(f"/run/user/{os.getuid()}/podman/podman.sock")
    if podman_socket.exists():
        docker_host = f"unix://{podman_socket}"
        os.environ["DOCKER_HOST"] = docker_host
        # Testcontainers also needs this to skip Docker-specific API features
        os.environ.setdefault("TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", str(podman_socket))
        logger.info("Using Podman socket for KVM passthrough: %s", docker_host)
        return

    default_socket = Path("/var/run/docker.sock")
    if default_socket.exists():
        return

    for candidate in _DOCKER_SOCKET_CANDIDATES:
        if candidate.exists():
            docker_host = f"unix://{candidate}"
            os.environ["DOCKER_HOST"] = docker_host
            logger.info("Auto-detected Docker socket: %s", docker_host)
            return

    logger.warning("No Docker/Podman socket found. Set DOCKER_HOST or start Docker/Podman.")


def _is_podman() -> bool:
    """Detect if we're using Podman instead of Docker."""
    docker_host = os.environ.get("DOCKER_HOST", "")
    return "podman" in docker_host


class VirtualDsmContainer:
    """Manages a virtual-dsm Docker container for integration testing.

    Requires testcontainers-python (install with `uv sync --extra vdsm`)
    and KVM support on the host.
    """

    def __init__(self, version: str, storage_dir: Path) -> None:
        if DockerContainer is None:
            msg = "testcontainers is not installed. Install with: uv sync --extra vdsm"
            raise ImportError(msg)

        if version not in DSM_VERSIONS:
            msg = f"Unknown DSM version: {version!r}. Available: {list(DSM_VERSIONS)}"
            raise ValueError(msg)

        self.version = version
        self.version_info: DsmVersionInfo = DSM_VERSIONS[version]
        self.storage_dir = storage_dir
        self._container: DockerContainer | None = None

    def start(self) -> None:
        """Create and start the virtual-dsm container, waiting for DSM to boot."""
        _ensure_docker_host()
        # Disable the Ryuk reaper — it fails on non-standard Docker setups
        # (Rancher Desktop, rootless, etc.). We handle cleanup in stop().
        os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        container = DockerContainer("vdsm/virtual-dsm")
        container.with_env("DISK_SIZE", CONTAINER_DISK_SIZE)
        container.with_env("URL", self.version_info.pat_url)
        container.with_env("RAM_SIZE", CONTAINER_RAM)
        container.with_env("CPU_CORES", CONTAINER_CPU_CORES)
        container.with_exposed_ports(5000, 22)
        # :Z flag is required on SELinux-enforcing systems (Fedora, RHEL)
        # so the container process can write to the bind mount.
        container.with_volume_mapping(str(self.storage_dir), "/storage", mode="Z")

        kwargs: dict[str, Any] = {
            "devices": ["/dev/kvm:/dev/kvm", "/dev/net/tun:/dev/net/tun"],
            "cap_add": ["NET_ADMIN"],
        }
        # Podman's default passt networking doesn't forward ports properly
        # for QEMU VMs. Use slirp4netns instead.
        if _is_podman():
            kwargs["network_mode"] = "slirp4netns:port_handler=slirp4netns"

        container.with_kwargs(**kwargs)

        logger.info(
            "Starting virtual-dsm container (DSM %s, build %s)",
            self.version,
            self.version_info.build,
        )
        container.start()
        self._container = container

        self._wait_for_dsm()

    def _wait_for_dsm(self) -> None:
        """Poll the DSM API info endpoint until DSM is ready."""
        url = f"{self.base_url}/webapi/query.cgi"
        params = {"api": "SYNO.API.Info", "version": "1", "method": "query", "query": "ALL"}
        logger.info("Polling DSM at %s (timeout: %ds)", self.base_url, DSM_BOOT_TIMEOUT)

        start = time.monotonic()
        deadline = start + DSM_BOOT_TIMEOUT
        last_status = ""

        while time.monotonic() < deadline:
            elapsed = int(time.monotonic() - start)
            try:
                resp = httpx.get(url, params=params, timeout=10, verify=False)  # noqa: S501
                status = f"HTTP {resp.status_code}"
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if data.get("success"):
                            logger.info("DSM %s ready after %ds", self.version, elapsed)
                            return
                        status = "HTTP 200 but success=false"
                    except Exception:
                        status = "HTTP 200 but non-JSON response"
                if status != last_status:
                    logger.info("DSM boot: %s (%ds)", status, elapsed)
                    last_status = status
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.ConnectTimeout,
                httpx.ReadError,
                httpx.RemoteProtocolError,
            ) as e:
                status = type(e).__name__
                if status != last_status:
                    logger.info("DSM boot: %s (%ds)", status, elapsed)
                    last_status = status

            time.sleep(DSM_API_POLL_INTERVAL)

        elapsed = int(time.monotonic() - start)
        msg = f"DSM {self.version} did not become ready within {elapsed}s"
        raise TimeoutError(msg)

    @property
    def host(self) -> str:
        """Container host IP address."""
        if self._container is None:
            msg = "Container is not started"
            raise RuntimeError(msg)
        return self._container.get_container_host_ip()  # type: ignore[no-any-return]

    @property
    def ssh_port(self) -> int:
        """Mapped SSH port for the running DSM instance."""
        if self._container is None:
            msg = "Container is not started"
            raise RuntimeError(msg)
        return int(self._container.get_exposed_port(22))

    @property
    def base_url(self) -> str:
        """HTTP base URL for the running DSM instance."""
        if self._container is None:
            msg = "Container is not started"
            raise RuntimeError(msg)
        port = self._container.get_exposed_port(5000)
        return f"http://{self.host}:{port}"

    def stop(self) -> None:
        """Stop the container if it is running."""
        if self._container is not None:
            logger.info("Stopping virtual-dsm container (DSM %s)", self.version)
            try:
                self._container.stop()
            except Exception:
                logger.warning(
                    "Failed to stop virtual-dsm container cleanly",
                    exc_info=True,
                )
            finally:
                self._container = None
