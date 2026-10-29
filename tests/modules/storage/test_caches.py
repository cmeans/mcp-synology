"""list_ssd_caches against the real-NAS and vdsm captures."""

from __future__ import annotations

import pytest

from mcp_synology.modules.storage.caches import list_ssd_caches
from tests.modules.storage.dsm import VDSM, flat, run


async def test_real_nas() -> None:
    text, replay = await run(list_ssd_caches)
    assert replay.unmatched == []
    for line in (
        "For volume: volume 1 (/volume1)",
        "Mode: read-only",
        "Read hit rate: 83%",
        "Shared cache: shared_cache_1 (Basic)",
        "setting: can_modify: yes",
        "Current I/O: 114222 hits of 125817 cacheable",
        "volume 1 (/volume1): finished about 2023-12-22 05:03:43 UTC, suggests 960 GiB "
        "(1.4 TiB with metadata pinned), meet_criteria",
    ):
        assert flat(line) in flat(text)


@pytest.mark.parametrize("fixture", VDSM)
async def test_vdsm(fixture: str) -> None:
    text, _ = await run(list_ssd_caches, fixture)
    assert text.startswith("No SSD caches.")
