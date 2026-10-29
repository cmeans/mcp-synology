"""list_disks and get_disk against the real-NAS and vdsm captures."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_synology.modules.storage.disks import get_disk, list_disks, trend_lines
from tests.modules.storage.dsm import VDSM, Replay, flat, load_info_with, ok, run, run_error


async def test_real_nas_list() -> None:
    text, replay = await run(list_disks)
    assert replay.unmatched == []
    assert text.count("\nDrive ") + text.startswith("Drive ") == 6
    assert flat("Serial number: FAKE0003") in flat(text)
    assert flat("Model: WDC WD142KFGX-68AFPN0") in flat(text)
    assert flat("Used for: hot spare") in flat(text)
    assert flat("Remaining life: 67%") in flat(text)
    assert flat("Bad sectors: —") in flat(text)  # the SSD doesn't report it
    assert flat("Type: SSD SATA") in flat(text)
    assert "i18nNamingInfo" not in text


@pytest.mark.parametrize("fixture", VDSM)
async def test_vdsm_has_no_disks(fixture: str) -> None:
    text, _ = await run(list_disks, fixture)
    assert text == "No disks reported. Virtual DSM has no physical disks."


async def test_no_disks_on_a_real_model() -> None:
    def setup(replay: Replay, _: Any) -> None:
        replay.answer({"method": "load_info"}, ok({"disks": [], "env": {"model_name": "DS920+"}}))

    text, _ = await run(list_disks, setup=setup)
    assert text == "No disks reported."


@pytest.mark.parametrize("name", ["Drive 6", "sdf", "/dev/sdf", "drive 6"])
async def test_get_ssd(name: str) -> None:
    text, replay = await run(get_disk, disk=name, history_limit=20)
    assert replay.unmatched == []
    assert text.startswith("Drive 6 (sdf)")
    assert flat("Write cache: off") in flat(text)
    assert flat(
        "Last quick test:                            2026/10/28 04:00:03 (NAS time)"
    ) in flat(text)
    assert flat(
        "Remaining life (%):                       90, 90, 88, 85, 81, 79, 76, 74, 73, 71, 69, 67"
    ) in flat(text)
    assert flat("Bad sectors: not reported by this drive") in flat(text)
    assert "S.M.A.R.T. attributes" in text
    assert "Wear_Leveling_Count" in text
    assert "Recent tests" in text
    log = replay.calls("SYNO.Core.Storage.Disk", "disk_test_log_get")[0]
    assert (log["device"], log["limit"], log["sort_by"]) == ("/dev/sdf", "20", "time")


@pytest.mark.parametrize("history_limit", [0, 101, -1])
async def test_history_limit_out_of_range_is_refused_first(history_limit: int) -> None:
    replays: list[Replay] = []
    error = await run_error(get_disk, disk="sda", history_limit=history_limit, replay_out=replays)
    assert error["code"] == "invalid_parameter"
    assert error["param"] == "history_limit"
    assert replays[0].seen == []  # no DSM call


async def test_empty_disk_name_matches_nothing() -> None:
    def setup(replay: Replay, _: Any) -> None:
        load_info_with(replay, lambda data: data["disks"][0].pop("longName"))

    error = await run_error(get_disk, disk="", setup=setup)
    assert error["code"] == "not_found"


async def test_location_shows_the_enclosure() -> None:
    text, _ = await run(get_disk, disk="sda", history_limit=20)
    expected = "Location: DS1618+ (internal), bay 1, order: 0, supportPwrBtnDisable: no"
    assert flat(expected) in flat(text)


async def test_unknown_disk() -> None:
    error = await run_error(get_disk, disk="sdz")
    assert error["code"] == "not_found"
    assert "sda (Drive 1)" in error["valid"]


def test_trend_without_history() -> None:
    assert trend_lines(None) == []
    assert trend_lines({"healthInfo": {"history": {"incremental": {"unc": []}}}}) == []
