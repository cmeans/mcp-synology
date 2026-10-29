"""get_storage_settings against the real-NAS and vdsm captures."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_synology.modules.storage.settings import get_storage_settings
from tests.modules.storage.dsm import VDSM, Replay, err, flat, run, run_error


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


async def test_storage_environment() -> None:
    text, _ = await run(get_storage_settings, setup=_no_drive_db)
    env = text.split("Storage environment", 1)[1]
    for line in ("ram_size: 4", "max_fs_bytes: 118747255799808", "multipathSupport: no",
                 "overview: status_level: status_level_abnormal", "ports: []"):  # fmt: skip
        assert flat(line) in flat(env)


@pytest.mark.parametrize("fixture", ["dsm-7.2.2-vdsm.json"])
async def test_non_admin_settings_and_schedules(fixture: str) -> None:
    """Neither tool calls load_info first: the 105 comes from an optional section."""
    from mcp_synology.modules.storage.schedules import get_storage_schedules

    for tool in (get_storage_settings, get_storage_schedules):
        error = await run_error(tool, fixture, persona="test")
        assert error["code"] == "permission_denied", tool
