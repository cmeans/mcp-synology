"""Version registry and constants for virtual-dsm test infrastructure."""

from __future__ import annotations

import dataclasses
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class DsmVersionInfo:
    """Metadata for a specific DSM release."""

    version: str
    build: str
    pat_url: str


DSM_VERSIONS: dict[str, DsmVersionInfo] = {
    "7.0.1": DsmVersionInfo(
        version="7.0.1",
        build="42218",
        pat_url=(
            "https://global.synologydownload.com/download/DSM/release"
            "/7.0.1/42218/DSM_VirtualDSM_42218.pat"
        ),
    ),
    "7.1": DsmVersionInfo(
        version="7.1",
        build="42661",
        pat_url=(
            "https://global.synologydownload.com/download/DSM/release"
            "/7.1/42661/DSM_VirtualDSM_42661.pat"
        ),
    ),
    "7.2.1": DsmVersionInfo(
        version="7.2.1",
        build="69057",
        pat_url=(
            "https://global.synologydownload.com/download/DSM/release"
            "/7.2.1/69057/DSM_VirtualDSM_69057.pat"
        ),
    ),
    "7.2.2": DsmVersionInfo(
        version="7.2.2",
        build="72806",
        pat_url=(
            "https://global.synologydownload.com/download/DSM/release"
            "/7.2.2/72806/DSM_VirtualDSM_72806.pat"
        ),
    ),
    "7.3.2": DsmVersionInfo(
        version="7.3.2",
        build="86009",
        pat_url=(
            "https://global.synologydownload.com/download/DSM/release"
            "/7.3.2/86009/DSM_VirtualDSM_86009.pat"
        ),
    ),
}

DEFAULT_DSM_VERSION: str = "7.2.2"

# Project root / .vdsm directory for all virtual-dsm artifacts
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
VDSM_ROOT: Path = _PROJECT_ROOT / ".vdsm"


def golden_image_path(version: str) -> Path:
    """Path to the compressed golden image tarball for a DSM version."""
    return VDSM_ROOT / "golden" / f"dsm-{version}.tar.gz"


def golden_meta_path(version: str) -> Path:
    """Path to the metadata sidecar JSON for a golden image."""
    return VDSM_ROOT / "golden" / f"dsm-{version}.meta.json"


def storage_path(version: str) -> Path:
    """Path to the live storage directory for a DSM version container."""
    return VDSM_ROOT / "storage" / f"dsm-{version}"


# Default credentials for test DSM instances
DEFAULT_ADMIN_USER: str = "mcpadmin"
DEFAULT_TEST_USER: str = "mcptest"
DEFAULT_TEST_PASSWORD: str = "Mcp#Test9!xK27zQ"

# Container resource limits
CONTAINER_DISK_SIZE: str = "16G"
CONTAINER_RAM: str = "2G"
CONTAINER_CPU_CORES: str = "2"

# Timing
DSM_BOOT_TIMEOUT: int = 600  # seconds
DSM_API_POLL_INTERVAL: int = 10  # seconds
