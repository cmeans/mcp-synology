#!/usr/bin/env python3
"""CLI tool for creating virtual-dsm golden images for testing.

This script automates the process of setting up a virtual-dsm container,
configuring DSM for integration testing, and saving a golden image that
can be restored for fast test runs.

Usage:
    uv run python scripts/vdsm_setup.py --version 7.2.2
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Add project root to sys.path so tests.vdsm is importable
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import click  # noqa: E402

from tests.vdsm.config import DEFAULT_DSM_VERSION, DSM_VERSIONS, storage_path  # noqa: E402
from tests.vdsm.container import VirtualDsmContainer  # noqa: E402
from tests.vdsm.golden_image import has_golden_image, save_golden_image  # noqa: E402
from tests.vdsm.setup_dsm import complete_wizard, setup_dsm_for_testing, wait_for_api  # noqa: E402


def _check_prerequisites() -> None:
    """Check that KVM and Docker are available."""
    if not os.path.exists("/dev/kvm"):
        click.echo("Error: /dev/kvm not found. KVM support is required for virtual-dsm.")
        click.echo("  On Linux: ensure the kvm kernel module is loaded (modprobe kvm)")
        sys.exit(1)

    import shutil

    if shutil.which("docker") is None:
        click.echo("Error: Docker is not installed or not in PATH.")
        click.echo("  Install Docker: https://docs.docker.com/engine/install/")
        sys.exit(1)

    # Quick check that Docker daemon is responsive
    result = os.system("docker info > /dev/null 2>&1")  # noqa: S605
    if result != 0:
        click.echo("Error: Docker daemon is not running or not accessible.")
        click.echo("  Start Docker: sudo systemctl start docker")
        click.echo("  Or add your user to the docker group: sudo usermod -aG docker $USER")
        sys.exit(1)


@click.command()
@click.option(
    "--version",
    "dsm_version",
    default=DEFAULT_DSM_VERSION,
    type=click.Choice(list(DSM_VERSIONS.keys())),
    help="DSM version to set up.",
)
@click.option(
    "--admin-user",
    prompt="Admin username (use during wizard)",
    default="admin",
    help="Admin username created during DSM setup wizard.",
)
@click.option(
    "--admin-password",
    prompt=True,
    hide_input=True,
    confirmation_prompt=True,
    help="Admin password for DSM setup wizard.",
)
def setup(dsm_version: str, admin_user: str, admin_password: str) -> None:
    """Create a golden image for virtual-dsm testing.

    This command starts a virtual-dsm container, waits for you to complete
    the DSM setup wizard in a browser, then configures the instance for
    integration testing and saves a golden image.
    """
    # 1. Check prerequisites
    click.echo("Checking prerequisites...")
    _check_prerequisites()
    click.echo("  KVM: OK")
    click.echo("  Docker: OK")

    # 2. Check if golden image already exists
    if has_golden_image(dsm_version) and not click.confirm(
        f"\nGolden image for DSM {dsm_version} already exists. Overwrite?"
    ):
        click.echo("Aborted.")
        sys.exit(0)

    # 3. Create clean storage directory (must be empty for fresh DSM wizard)
    import shutil as _shutil

    store = storage_path(dsm_version)
    if store.exists():
        _shutil.rmtree(store)
        click.echo(f"\nCleared existing storage: {store}")
    store.mkdir(parents=True, exist_ok=True)
    click.echo(f"Storage directory: {store}")

    # 4. Start virtual-dsm container
    click.echo(f"\nStarting virtual-dsm container (DSM {dsm_version})...")
    click.echo("  This may take several minutes on first run (downloading DSM image).")
    container = VirtualDsmContainer(version=dsm_version, storage_dir=store)

    try:
        container.start()
        base_url = container.base_url
        click.echo(f"\n  Container started: {base_url}")

        # 5. Wait for DSM web UI to become accessible
        click.echo("\nWaiting for DSM to finish booting...")
        wait_for_api(base_url)

        # 6. Automate setup wizard with Playwright (fully headless)
        click.echo("\nRunning DSM setup wizard (automated)...")
        complete_wizard(base_url, admin_user, admin_password)

        # 7. Wait for DSM services to initialize after wizard
        click.echo("\nWaiting 30s for DSM services to initialize after wizard...")
        import time

        time.sleep(30)

        # 8. Run post-wizard API configuration
        click.echo("\nConfiguring DSM for integration testing...")
        metadata = setup_dsm_for_testing(
            base_url,
            admin_password,
            admin_user=admin_user,
            container_id=container.container_id,
        )

        # 8. Stop container gracefully
        click.echo("\nStopping container...")
        container.stop()
        container = None  # type: ignore[assignment]

        # 9. Save golden image
        click.echo(f"\nSaving golden image for DSM {dsm_version}...")
        image_path = save_golden_image(dsm_version, metadata=metadata)
        image_size_mb = image_path.stat().st_size / (1024 * 1024)

        # 10. Print success
        click.echo(f"\n{'=' * 60}")
        click.echo("Golden image created successfully!")
        click.echo(f"{'=' * 60}")
        click.echo(f"  Image: {image_path}")
        click.echo(f"  Size:  {image_size_mb:.1f} MB")
        click.echo(f"  Version: DSM {dsm_version}")
        click.echo(f"\n  Test user: {metadata['test_user']}")
        click.echo(f"  Test paths: {metadata['test_paths']}")
        click.echo("\nTo run integration tests:")
        click.echo("  uv run pytest -m integration -v")

    except Exception:
        click.echo("\nError during setup. Stopping container...", err=True)
        if container is not None:
            container.stop()
        raise


if __name__ == "__main__":
    setup()
