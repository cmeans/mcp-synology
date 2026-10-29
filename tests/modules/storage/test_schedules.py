"""get_storage_schedules against the real-NAS and vdsm captures."""

from __future__ import annotations

import pytest

from mcp_synology.modules.storage.schedules import get_storage_schedules
from tests.modules.storage.dsm import VDSM, flat, run


async def test_real_nas() -> None:
    text, replay = await run(get_storage_schedules)
    assert replay.unmatched == []
    for line in (
        "On: yes",
        "Every: 3 months",
        "Next run: about 2026-12-18 06:00:00 UTC",
        "Runs: only in the hours below",
        "Disk Test: quick test of all disks, on, next 2026-10-29 04:00 (NAS time)",
        "Extended Tests: extended test of all disks, on, next 2026-11-01 02:00 (NAS time)",
        "Running now: extended test, started on sda, sdb, sdc, sdd, sde, sdf",
        "First run: about 2022-12-18 06:00:00 UTC",
        "Legend: # = may run . = doesn't run",
        "Deduplication: not available on this NAS",
        "Snapshot schedules are per shared folder: use list_snapshots.",
    ):
        assert flat(line) in flat(text)
    assert "    Sun  #  #  #" in text
    # Reclamation runs at any time, so it has no grid.
    reclaim = text.split("Space reclamation", 1)[1].split("SSD TRIM", 1)[0]
    assert "Sun" not in reclaim


async def test_741_dedupe_window() -> None:
    text, replay = await run(get_storage_schedules, "dsm-7.4.1-vdsm.json")
    assert replay.unmatched == []
    dedupe = text.split("Btrfs deduplication", 1)[1]
    assert "Mon  #  #  #  #  #  #  #  .  ." in dedupe


@pytest.mark.parametrize("fixture", VDSM)
async def test_vdsm(fixture: str) -> None:
    text, replay = await run(get_storage_schedules, fixture)
    assert replay.unmatched == []
    assert flat("Tasks: none") in flat(text)
    assert flat("On: no") in flat(text)
