"""get_storage_settings against the real-NAS and vdsm captures."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_synology.modules.storage.settings import get_storage_settings
from tests.modules.storage.dsm import VDSM, Replay, err, flat, run


def _no_drive_db(replay: Replay, _: Any) -> None:
    # DSM 7.1 has no get_latest_online_drive_db_info: 103 (method doesn't exist).
    replay.answer({"method": "get_latest_online_drive_db_info"}, err(103))


async def test_real_nas() -> None:
    text, replay = await run(get_storage_settings, setup=_no_drive_db)
    assert replay.unmatched == []
    for line in (
        "Mode: lower impact on the system",
        "Limits: 10 to 600",
        "Fast repair: yes",
        "Automatic repair: no",
        "Warn on SSD remaining life: yes",
        "below (%): 5",
        "below (months): 6",
        "Check email settings: yes",
        "Drive database updated: about 2026-06-05 12:54:07 UTC",
        "Replace a failed disk with a hot spare automatically: no",
        "Block USB storage: no",
        "Drive database: not available on this DSM version",
    ):
        assert flat(line) in flat(text)


@pytest.mark.parametrize("fixture", VDSM)
async def test_vdsm(fixture: str) -> None:
    setup = _no_drive_db if fixture == "dsm-7.1-vdsm.json" else None
    text, replay = await run(get_storage_settings, fixture, setup=setup)
    assert replay.unmatched == []
    assert ("Latest online drive database" in text) == (setup is None)
