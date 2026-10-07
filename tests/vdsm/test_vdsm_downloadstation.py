"""Integration tests for Download Station tools against virtual-dsm.

Requires DS installed in the golden image (see tests/vdsm/setup_dsm.py
_install_download_station_via_ui). Tests skip cleanly if the package isn't
available on the connected NAS (e.g., running against a real NAS without DS).

Run with: uv run pytest -m vdsm -v tests/vdsm/test_vdsm_downloadstation.py
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

pytestmark = pytest.mark.vdsm


# A magnet URI that's parseable but won't actually leech anything in CI
# (random hex hash with no real torrent behind it). DS accepts magnets at
# create time without verifying the swarm — the task will sit at "waiting"
# status indefinitely, which is fine for read-tool exercises.
_FAKE_MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test-file"


def _unpack(nas_client: Any) -> tuple[DsmClient, Any, Any, dict[str, str]]:
    """Unpack the nas_client tuple yielded by conftest."""
    return nas_client  # type: ignore[return-value]


@pytest.fixture
async def _ds_available(nas_client: Any) -> bool:
    """Skip if Download Station isn't installed on the connected NAS."""
    client, _, _, _ = _unpack(nas_client)
    info = await client.query_api_info()
    if "SYNO.DownloadStation.Task" not in info:
        pytest.skip("Download Station package not installed on this NAS")
    return True


class TestVdsmDownloadStationReads:
    """Exercise the 5 READ tools against a real DSM instance.

    Most tests run against the empty queue (default state after fresh install).
    """

    async def test_list_downloads_empty_queue(self, nas_client: Any, _ds_available: bool) -> None:
        from mcp_synology.modules.downloadstation.tasks import list_downloads

        client, _, _, _ = _unpack(nas_client)
        result = await list_downloads(client)
        # Empty queue renders the table header but no rows
        assert "Download Station queue" in result
        # The total-line should report 0 tasks OR the empty-table sentinel
        # depending on which path format_table takes for empty rows.
        assert "0 task" in result or "No items to display" in result

    async def test_get_download_stats_returns_speeds(
        self, nas_client: Any, _ds_available: bool
    ) -> None:
        from mcp_synology.modules.downloadstation.stats import get_download_stats

        client, _, _, _ = _unpack(nas_client)
        result = await get_download_stats(client)
        # Headline rows must be present whether or not transfers are active
        assert "Download (total)" in result
        assert "Upload (total)" in result

    async def test_get_download_config_returns_known_fields(
        self, nas_client: Any, _ds_available: bool
    ) -> None:
        from mcp_synology.modules.downloadstation.config import get_download_config

        client, _, _, _ = _unpack(nas_client)
        result = await get_download_config(client)
        # Expected fields rendered by the handler
        assert "Default destination" in result
        assert "BT max download" in result
        assert "eMule enabled" in result
        # The setup configured default_destination=writable — confirm it's set
        assert "writable" in result

    async def test_get_schedule_returns_grid(self, nas_client: Any, _ds_available: bool) -> None:
        from mcp_synology.modules.downloadstation.config import get_schedule

        client, _, _, _ = _unpack(nas_client)
        result = await get_schedule(client)
        # Header rendered
        assert "Enabled" in result
        # Grid rendered
        assert "Sun" in result
        assert "Sat" in result
        assert "Legend" in result


class TestVdsmDownloadStationLifecycle:
    """Create + read + delete lifecycle to exercise get_download_info against
    a real task (which the empty-queue tests can't cover).
    """

    async def test_create_read_delete_task(self, nas_client: Any, _ds_available: bool) -> None:
        from mcp_synology.modules.downloadstation.tasks import (
            create_download,
            delete_download,
            get_download_info,
            list_downloads,
        )

        client, _, _, _ = _unpack(nas_client)
        created_task_id: str | None = None
        try:
            # Create a magnet-URI task — won't actually transfer (no seeds),
            # but DS accepts it and creates a task record.
            create_result = await create_download(client, uri=_FAKE_MAGNET, destination="writable")
            assert "Created" in create_result

            # Give DS a moment to commit the new task (some DSM versions
            # need a tick before list reflects new entries).
            await asyncio.sleep(2)

            # Find the new task via list_downloads. The fake magnet uses dn=test-file,
            # which DS will surface as the task title.
            listed = await list_downloads(client)
            assert "test-file" in listed or "0123456789abcdef" in listed.lower(), (
                f"Newly-created task not found in list_downloads output:\n{listed}"
            )

            # Pull the task id out of the list output. The list table renders
            # the id in the first column; parse it by finding the row that
            # contains our magnet's identifying substring.
            created_task_id = _extract_task_id_from_list(listed)
            assert created_task_id, f"Could not parse task id from:\n{listed}"

            # Read full task info — exercises the additional=detail,transfer,file,tracker,peer path
            info = await get_download_info(client, task_id=created_task_id)
            assert created_task_id in info or "test-file" in info
            assert "Status" in info  # header block
            assert "Detail" in info  # detail block

        finally:
            # Cleanup — always attempt deletion, even on test failure
            if created_task_id is not None:
                try:
                    await delete_download(
                        client,
                        task_ids=[created_task_id],
                        delete_data=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    # Swallow cleanup errors — the test result has priority.
                    # Log via pytest's stderr capture so the failure shows up
                    # in CI logs.
                    print(f"WARNING: failed to delete test task {created_task_id}: {exc}")


def _extract_task_id_from_list(list_output: str) -> str | None:
    """Parse a task id from the list_downloads table output.

    Format is: ID | Title | Type | Status | Size | Progress | Speed | ETA
    with the columns space-separated and rendered by format_table. The first
    non-header, non-empty content line gives us the id in column 0.
    """
    lines = list_output.splitlines()
    in_table = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("ID "):
            in_table = True
            continue
        if in_table and not stripped.startswith("-") and not stripped.endswith("task(s) total"):
            # First column is the task id — split on multiple spaces
            parts = stripped.split()
            if parts:
                return parts[0]
    return None
