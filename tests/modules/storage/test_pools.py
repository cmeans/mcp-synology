"""list_storage_pools against the real-NAS and vdsm captures."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_synology.modules.storage.pools import list_storage_pools, progress_text, when
from tests.modules.storage.dsm import VDSM, Replay, err, flat, load_info_with, ok, run


async def test_real_nas_pool() -> None:
    text, replay = await run(list_storage_pools)
    assert replay.unmatched == []
    assert "Pool 1 (reuse_1)\n" in text
    assert flat("RAID type: RAID 5") in flat(text)
    assert flat(
        "Disks:                       sdd (normal), sdc (normal), sdb (normal), sda (normal)"
    ) in flat(text)
    assert flat("Hot spares: sde") in flat(text)
    assert flat("Volumes: volume 1 (/volume1)") in flat(text)
    assert flat("next run: about 2026-12-18 06:00:00 UTC") in flat(text)
    assert flat("Setting: stripe_cache_size: default") in flat(text)
    # Pool get_setting takes the space path, not the pool_path field.
    assert replay.calls("SYNO.Storage.CGI.Pool", "get_setting")[0]["pool_path"] == "/dev/vg1"
    # Every other field DSM sends is listed by name; UI-only ones aren't.
    assert "minimal_spare_size:" in text
    assert "can_do" not in text
    assert "show_assemble_btn" not in text


async def test_real_nas_spares() -> None:
    text, _ = await run(list_storage_pools)
    assert "Automatic replacement of a failed disk: no" in text
    assert (
        "sde   WD142KFGX-68AFPN0   serial FAKE0001   12.7 TiB   standby   protects Pool 1 (reuse_1)"
        in text
    )
    assert "Pool 1 (reuse_1): no disk qualifies (needs at least 12.7 TiB)" in text
    assert "/dev/sdf: too small; /dev/sdf: a different drive type" in text
    assert "Missing pools: none" in text
    assert "can be an hour off while the NAS is on daylight saving time" in text


@pytest.mark.parametrize("fixture", VDSM)
async def test_vdsm_has_no_pools(fixture: str) -> None:
    text, replay = await run(list_storage_pools, fixture)
    assert replay.unmatched == []
    assert text == (
        "No storage pools. Volumes on this NAS aren't in a storage pool (see get_volume)."
        "\n\nMissing pools: none\n\nDetected pools: none"
    )


async def test_failed_spare_calls_are_noted() -> None:
    def setup(replay: Replay, _: Any) -> None:
        replay.answer({"api": "SYNO.Storage.CGI.Spare", "method": "list"}, err(117))
        replay.answer({"method": "potential_spares_dump"}, ok({}))
        replay.answer({"api": "SYNO.Storage.CGI.Spare.Conf"}, ok({"auto_replacement": True}))

    text, _ = await run(list_storage_pools, setup=setup)
    assert "Automatic replacement of a failed disk: yes" in text
    assert "Hot spares: couldn't be read (DSM error 117)" in text
    assert "Disks that could be hot spares" not in text


def test_progress_text() -> None:
    assert progress_text({"step": "none"}) == "none"
    assert progress_text({"step": "expanding", "percent": "-1"}) == "expanding"
    assert (
        progress_text({"step": "resync", "percent": "12", "cur_step": 1, "total_step": 3})
        == "resync 12% (step 1 of 3)"
    )


def test_when() -> None:
    assert when(0) == "—"
    assert when(None) == "—"
    assert when(1797573600) == "about 2026-12-18 06:00:00 UTC"


async def test_missing_pool_without_assembled_pools() -> None:
    """A crashed pool: no storagePools, but missing_pools says so. Never 'no pools'."""

    def setup(replay: Replay, _: Any) -> None:
        def change(data: dict[str, Any]) -> None:
            data["storagePools"] = []
            data["missing_pools"] = [{"id": "reuse_1", "status": "crashed"}]

        load_info_with(replay, change)

    text, _ = await run(list_storage_pools, setup=setup)
    assert text.startswith("No assembled storage pools.")
    assert 'Missing pools: {"id": "reuse_1", "status": "crashed"}' in text
    assert "aren't in a storage pool" not in text


async def test_degraded_raid() -> None:
    def setup(replay: Replay, _: Any) -> None:
        def change(data: dict[str, Any]) -> None:
            raid = data["storagePools"][0]["raids"][0]
            raid["normalDevCount"] = 3
            raid["devices"][0]["status"] = "crashed"

        load_info_with(replay, change)

    text, _ = await run(list_storage_pools, setup=setup)
    assert flat("array /dev/md2: 3 of 4 disks working (degraded)") in flat(text)
    assert "sdd (crashed)" in text


async def test_allocation_not_fullness() -> None:
    text, _ = await run(list_storage_pools)
    assert flat("Size: 38.2 TiB, of which 38.2 TiB (100.0%) is allocated to volumes") in flat(text)
