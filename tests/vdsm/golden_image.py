"""Golden image save/restore for virtual-dsm test instances."""

from __future__ import annotations

import json
import logging
import shutil
import tarfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from tests.vdsm.config import (
    DEFAULT_ADMIN_USER,
    DEFAULT_TEST_PASSWORD,
    DEFAULT_TEST_USER,
    DSM_VERSIONS,
    golden_image_path,
    golden_meta_path,
    storage_path,
)

logger = logging.getLogger(__name__)


def save_golden_image(
    version: str,
    *,
    metadata: dict[str, object] | None = None,
) -> Path:
    """Tar the storage directory into a golden image.

    The storage directory must already exist and contain a configured DSM instance.
    A metadata sidecar JSON is saved alongside the tarball.

    If metadata is provided (from setup_dsm_for_testing), it's saved as-is.
    Otherwise, a default metadata dict is generated.

    Returns the path to the created golden image tarball.
    """
    source = storage_path(version)
    if not source.is_dir():
        msg = f"Storage directory does not exist: {source}"
        raise FileNotFoundError(msg)

    dest = golden_image_path(version)
    dest.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Saving golden image for DSM %s: %s -> %s", version, source, dest)
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(str(source), arcname=".")

    # Save metadata sidecar
    if metadata is None:
        version_info = DSM_VERSIONS.get(version)
        metadata = {
            "version": version,
            "build": version_info.build if version_info else "unknown",
            "admin_user": DEFAULT_ADMIN_USER,
            "test_user": DEFAULT_TEST_USER,
            "test_password": DEFAULT_TEST_PASSWORD,
            "test_paths": {
                "existing_share": "/testshare",
                "search_folder": "/testshare/Documents",
                "search_keyword": "Bambu",
                "writable_folder": "/writable",
            },
        }

    meta_path = golden_meta_path(version)
    meta_path.write_text(json.dumps(metadata, indent=2) + "\n")
    logger.info("Saved golden image metadata: %s", meta_path)

    return dest


def restore_golden_image(version: str) -> Path:
    """Extract golden image to storage directory.

    Deletes any existing storage directory for this version to ensure a clean
    slate, then extracts the golden image tarball.

    Returns the storage directory path.
    """
    source = golden_image_path(version)
    if not source.is_file():
        msg = f"Golden image not found: {source}"
        raise FileNotFoundError(msg)

    dest = storage_path(version)

    # Clean slate
    if dest.exists():
        logger.info("Removing existing storage directory: %s", dest)
        shutil.rmtree(dest)

    dest.mkdir(parents=True, exist_ok=True)

    logger.info("Restoring golden image for DSM %s: %s -> %s", version, source, dest)
    with tarfile.open(source, "r:gz") as tar:
        tar.extractall(path=str(dest))  # noqa: S202

    return dest


def has_golden_image(version: str) -> bool:
    """Check if a golden image exists for this version."""
    return golden_image_path(version).is_file()


def load_golden_meta(version: str) -> dict[str, object]:
    """Load the meta.json sidecar for a golden image."""
    meta_path = golden_meta_path(version)
    if not meta_path.is_file():
        msg = f"Golden image metadata not found: {meta_path}"
        raise FileNotFoundError(msg)

    return json.loads(meta_path.read_text())  # type: ignore[no-any-return]
