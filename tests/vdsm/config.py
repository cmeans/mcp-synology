"""Version registry and constants for virtual-dsm test infrastructure."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclasses.dataclass(frozen=True)
class DsmVersionInfo:
    """Metadata for a specific DSM release."""

    version: str
    build: str
    pat_url: str
    # The DSM update the bake installs on top of the release (its smallfixnumber),
    # 0 for none. Synology's update packs are deltas for one build.
    update: int = 0

    @property
    def update_url(self) -> str:
        """The VirtualDSM update pack for ``update`` (only meaningful when it's set)."""
        return (
            "https://global.synologydownload.com/download/DSM/criticalupdate/update_pack"
            f"/{self.build}-{self.update}/synology_kvmx64_virtualdsm.pat"
        )


DSM_VERSIONS: dict[str, DsmVersionInfo] = {
    "7.0.1": DsmVersionInfo(
        version="7.0.1",
        build="42218",
        pat_url=(
            "https://global.synologydownload.com/download/DSM/release"
            "/7.0.1/42218/DSM_VirtualDSM_42218.pat"
        ),
    ),
    # 7.1.1 since bake v9 (7.1-42661 before): Synology's 7.1 updates are for 7.1.1, the
    # release seska runs. The key stays "7.1", as CI's matrix and the image names use it.
    "7.1": DsmVersionInfo(
        version="7.1",
        build="42962",
        pat_url=(
            "https://global.synologydownload.com/download/DSM/release"
            "/7.1.1/42962/DSM_VirtualDSM_42962.pat"
        ),
        update=9,
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
        update=10,
    ),
    "7.3.2": DsmVersionInfo(
        version="7.3.2",
        build="86009",
        pat_url=(
            "https://global.synologydownload.com/download/DSM/release"
            "/7.3.2/86009/DSM_VirtualDSM_86009.pat"
        ),
        update=5,
    ),
    "7.4.1": DsmVersionInfo(
        version="7.4.1",
        build="90080",
        pat_url=(
            "https://global.synologydownload.com/download/DSM/release"
            "/7.4.1/90080/DSM_VirtualDSM_90080.pat"
        ),
        update=1,
    ),
}

DEFAULT_DSM_VERSION: str = "7.2.2"

# Bumped on every change to what the bake puts into a golden image. The bake
# records it in meta.json, and the vdsm conftest refuses older images: a local
# .vdsm/golden/ image is reused indefinitely and would otherwise silently lack
# newer setup (e.g. the test user's Download Station grant, added in v2). CI's
# golden-image cache is keyed on it too (#132): a bake change without a bump
# is baked fresh on its own PR, but main would keep restoring the old image.
#   v3: the bake shuts DSM down cleanly before saving; v2 images were saved
#       from a SIGKILLed VM (on CI, DS's pkg-synodl-apid unit came up masked).
#   v4: the test user sets its own DS default destination (DS never starts a
#       non-manager's tasks until it is set, #124), and a second, unconfigured
#       DS user (DEFAULT_FRESH_USER) pins that case.
#   v5: Synology Photos (#258): the package and its dependencies, the user home
#       service (each user's personal space), the test user's Photos privilege,
#       and Shared Space with the test user as a manager. DEFAULT_FRESH_USER has
#       no Photos privilege.
#   v6: DSM 7.3.2 and 7.4.1 (#307): their Synology Photos package lists. The
#       7.1 and 7.2.2 bakes are unchanged.
#   v7: each DSM version runs in its own fixed timezone (DSM_TIMEZONES, #342).
#       Before, DSM kept the zone the wizard's browser had, i.e. the bake
#       host's: US Central for local bakes, UTC for CI's.
#   v8: Snapshot Replication (ReplicationService), for share snapshots in the coming
#       Storage module (#49): its vdsm tests take and list snapshots, as on a real NAS
#       with the package.
#   v9: the latest DSM update for each version (DsmVersionInfo.update), installed
#       before the packages, and 7.1 moves to 7.1.1-42962; the virtual-dsm container
#       image is pinned (VDSM_IMAGE). meta.json records the firmware version DSM reports.
BAKE_VERSION: int = 9

# The virtual-dsm container image, pinned so a new upstream release can't change the
# VM a bake or a test run boots without a bake change.
VDSM_IMAGE: str = "vdsm/virtual-dsm:7.66"

# The timezone each golden image runs in, as SYNO.Core.Region.NTP names it
# (listzone). Tests read the NAS's zone rather than assume one; between them
# these cover a negative offset, UTC, a half-hour offset, and a positive one
# with daylight saving (#342). 7.1 matches seska, the maintainer's real NAS.
DSM_TIMEZONES: dict[str, str] = {
    "7.1": "Central",  # America/Chicago, -06:00 / -05:00
    "7.2.2": "GMT",  # +00:00, no daylight saving
    "7.3.2": "Calcutta",  # Asia/Kolkata, +05:30
    "7.4.1": "Amsterdam",  # Europe/Amsterdam (Berlin's zone), +01:00 / +02:00
}

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
# A DS-granted non-admin that has never set a DS default destination (#124).
DEFAULT_FRESH_USER: str = "mcpfresh"
DEFAULT_FRESH_PASSWORD: str = "Mcp#Fresh7!pQ31vW"

# Container resource limits
CONTAINER_DISK_SIZE: str = "16G"
CONTAINER_RAM: str = "2G"
CONTAINER_CPU_CORES: str = "2"

# Timing
DSM_BOOT_TIMEOUT: int = 600  # seconds
DSM_API_POLL_INTERVAL: int = 10  # seconds


def ds_start_fallback_allowed(env: Mapping[str, str]) -> bool:
    """Whether the conftest may start Download Station itself when it never came up.

    Never on CI, where every golden image is baked: a DS that only answers after
    the conftest starts it means a broken bake, and that must turn the job red.
    Locally it stays a logged workaround so an older image is still usable.
    """
    return not any(env.get(var, "").lower() not in ("", "false", "0") for var in _CI_VARS)


_CI_VARS = ("CI", "GITHUB_ACTIONS")
