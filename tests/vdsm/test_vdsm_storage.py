"""The Storage module (#49) on virtual-dsm, every DSM version.

vdsm has no disks and no storage pools, only one basic volume, so these cover what it
does have: the overview, volumes, schedules, settings, external storage, snapshots (bake
v8 installs Snapshot Replication), the empty disk and pool paths, unknown IDs, and the
non-admin refusal. Disk, S.M.A.R.T., RAID and SSD-cache replies are tested against the
real NAS's capture in ``tests/modules/storage/``.

Run with: uv run pytest -m vdsm -v tests/vdsm/test_vdsm_storage.py --dsm-version 7.2.2
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.storage.caches import list_ssd_caches
from mcp_synology.modules.storage.disks import get_disk, list_disks
from mcp_synology.modules.storage.external import list_external_storage
from mcp_synology.modules.storage.overview import get_storage_overview
from mcp_synology.modules.storage.pools import list_storage_pools
from mcp_synology.modules.storage.schedules import get_storage_schedules
from mcp_synology.modules.storage.settings import get_storage_settings
from mcp_synology.modules.storage.snapshots import list_snapshots
from mcp_synology.modules.storage.volumes import get_volume, list_volumes

if TYPE_CHECKING:
    from tests.personas import ReauthSpy

pytestmark = pytest.mark.vdsm

SIMPLE = [
    get_storage_overview, list_storage_pools, list_volumes, list_disks, list_ssd_caches,
    get_storage_schedules, get_storage_settings, list_external_storage,
]  # fmt: skip


def _error(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    error: dict[str, Any] = json.loads(str(exc.value))["error"]
    return error


async def test_overview(admin_client: Any) -> None:
    text = await get_storage_overview(admin_client[0])
    assert text.startswith("Storage Overview — ")
    assert "VirtualDSM" in text
    assert "none (Virtual DSM has no physical disks)" in text
    assert "Volume 1 (/volume1)   Btrfs" in text
    assert "Not shown:" not in text, text


async def test_no_pools_disks_or_caches(admin_client: Any) -> None:
    client = admin_client[0]
    assert (await list_storage_pools(client)).startswith("No storage pools.")
    assert await list_disks(client) == "No disks reported. Virtual DSM has no physical disks."
    assert (await list_ssd_caches(client)).startswith("No SSD caches.")


async def test_volumes(admin_client: Any) -> None:
    client = admin_client[0]
    listed = await list_volumes(client)
    assert "Volume 1 (/volume1)" in listed
    detail = await get_volume(client, "volume1")
    assert "Space by category" in detail
    assert "Space reclamation" in detail
    with pytest.raises(ToolError) as exc:
        await get_volume(client, "/volume9")
    assert _error(exc)["code"] == "not_found"
    assert _error(exc)["valid"] == ["/volume1"]


async def test_schedules_settings_and_external(admin_client: Any) -> None:
    client = admin_client[0]
    schedules = await get_storage_schedules(client)
    assert "Data scrubbing" in schedules
    assert "S.M.A.R.T. tests" in schedules
    settings = await get_storage_settings(client)
    assert "RAID resync speed" in settings
    assert "Disk health" in settings
    external = await list_external_storage(client)
    assert "USB drives\n  none" in external
    assert "Expansion units\n  none" in external


async def test_unknown_disk(admin_client: Any) -> None:
    with pytest.raises(ToolError) as exc:
        await get_disk(admin_client[0], "sda")
    assert _error(exc)["code"] == "not_found"
    assert _error(exc)["valid"] == []


async def test_snapshots(admin_client: Any) -> None:
    """A snapshot taken on writable shows up in list_snapshots (which pins list v2)."""
    client = admin_client[0]
    taken = await client.request(
        "SYNO.Core.Share.Snapshot",
        "create",
        version=1,
        params={"name": '"writable"', "snapinfo": '{"desc": "storage tool check", "lock": false}'},
    )
    try:
        one = await list_snapshots(client, "writable")
        assert taken in one
        assert "storage tool check" in one
        assert "manual" in one
        counts = await list_snapshots(client)
        row = next(line for line in counts.splitlines() if line.strip().startswith("writable"))
        assert row.split()[2] == "1", counts
    finally:
        # Never leave a snapshot on writable for later tests to count.
        await client.request(
            "SYNO.Core.Share.Snapshot",
            "delete",
            version=1,
            params={"name": '"writable"', "snapshots": f'["{taken}"]'},
        )
    with pytest.raises(ToolError) as exc:
        await list_snapshots(client, "no-such-share")
    assert _error(exc)["code"] == "not_found"


@pytest.mark.parametrize("tool", [*SIMPLE, list_snapshots], ids=lambda t: t.__name__)
async def test_non_admin_is_refused(tool: Any, nas_client: Any, reauth_spy: ReauthSpy) -> None:
    with pytest.raises(ToolError) as exc:
        await tool(nas_client[0])
    error = _error(exc)
    assert error["code"] == "permission_denied", error
    assert "administrator" in error["message"]
    reauth_spy.assert_105_never_reauthed()
