"""get_storage_overview against the real-NAS and vdsm captures."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_synology.modules.storage.overview import get_storage_overview, headline
from tests.modules.storage.dsm import REALNAS, VDSM, Replay, err, ok, run, run_error

LOAD_INFO = {"api": "SYNO.Storage.CGI.Storage", "method": "load_info"}


class TestRealNas:
    async def test_summary_lines(self) -> None:
        text, replay = await run(get_storage_overview)
        assert replay.unmatched == []
        assert text.startswith("Storage Overview — attention")
        assert "System health:  storage needs attention (storage_is_attention)" in text
        assert "Model:          DS1618+, 6 bays, no expansion units" in text
        assert "- Storage pool 1 (reuse_1): attention, fs_almost_full" in text
        assert "- Volume 1 (/volume1): attention, fs_almost_full, 96.5% used" in text
        assert "Pool 1 (reuse_1)   RAID 5, 4 disks + 1 hot spare   attention   38.2 TiB" in text
        assert "Volume 1 (/volume1)   Btrfs   attention   35.4 TiB of 36.6 TiB used (96.5%)" in text
        assert "Cache 1 (shared_cache_1)   read-only, 1 SSD   for volume 1   hit rate 83%" in text

    async def test_disk_lines(self) -> None:
        text, _ = await run(get_storage_overview)
        assert (
            "Drive 2 (sdb)   WD142KFGX-68AFPN0   12.7 TiB   41°C   normal   pool 1   "
            "extended S.M.A.R.T. test 90%, not on Synology's compatibility list"
        ) in text
        assert "Drive 5 (sde)   WD142KFGX-68AFPN0   12.7 TiB   39°C   normal   hot spare" in text
        assert "SSD cache 1 (shared_cache_1)   67% life left" in text
        # A running test and an off-list disk aren't problems: no disk is under attention.
        attention = text.split("Needs attention", 1)[1].split("Storage pools", 1)[0]
        assert "Drive" not in attention

    async def test_tasks_use_the_nas_clock_where_dsm_gives_it(self) -> None:
        text, _ = await run(get_storage_overview)
        assert "Running:  S.M.A.R.T. extended test (Drive 2 at 90%, Drive 4 at 90%)" in text
        assert "Next:     S.M.A.R.T. quick test of 6 disks, 2026-10-29 04:00 (NAS time)" in text
        assert "Data scrubbing of pool 1, about 2026-12-18 06:00:00 UTC" in text

    async def test_bad_disks_are_attention_items(self) -> None:
        """load_bad_disks names disks by display name, as every vdsm capture does."""

        def setup(replay: Replay, _: Any) -> None:
            replay.answer(
                {"method": "load_bad_disks"},
                ok({"unhealthy": ["Drive 1"], "not_compatible": ["Drive 3", "Drive 9"]}),
            )

        text, _ = await run(get_storage_overview, setup=setup)
        assert "- Drive 1 (sda): unhealthy" in text
        assert "- Drive 3 (sdc): not compatible" in text
        assert "Drive 9" not in text  # matches no disk

    async def test_an_optional_section_that_fails_is_noted(self) -> None:
        def setup(replay: Replay, _: Any) -> None:
            replay.answer({"api": "SYNO.Core.System.SystemHealth"}, err(121))
            replay.answer({"method": "get_sche_task_list"}, err(117))

        text, _ = await run(get_storage_overview, setup=setup)
        assert "System health:  " not in text
        assert "  - System health: not available on this NAS" in text
        assert "  - Storage tasks: couldn't be read (DSM error 117)" in text


@pytest.mark.parametrize("fixture", VDSM)
async def test_vdsm(fixture: str) -> None:
    text, replay = await run(get_storage_overview, fixture)
    assert replay.unmatched == []
    assert text.startswith("Storage Overview — normal")
    assert "System health:  healthy (system_health)" in text
    assert "none (Virtual DSM has no physical disks)" in text
    assert "Storage pools\n  none" in text
    assert "Running:  nothing" in text


async def test_non_admin_is_refused() -> None:
    error = await run_error(get_storage_overview, "dsm-7.2.2-vdsm.json", persona="test")
    assert error["code"] == "permission_denied"
    assert "administrator" in error["message"]


async def test_storage_manager_missing() -> None:
    def setup(_: Replay, client: Any) -> None:
        del client._api_cache["SYNO.Storage.CGI.Storage"]

    error = await run_error(get_storage_overview, setup=setup)
    assert error["code"] == "api_not_found"


async def test_a_load_info_error_fails_the_tool() -> None:
    def setup(replay: Replay, _: Any) -> None:
        replay.answer(LOAD_INFO, err(121))

    error = await run_error(get_storage_overview, setup=setup)
    assert error["code"] == "not_supported"


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("normal", "normal"),
        ("status_level_normal", "normal"),
        ("status_level_abnormal", "attention"),
        ("status_level_danger", "danger"),
        ("", "unknown"),
    ],
)
def test_headline(level: str, expected: str) -> None:
    assert headline({"overview_data": {"status_level": level}}) == expected


async def test_pool_progress_and_build_are_running_tasks() -> None:
    def setup(replay: Replay, _: Any) -> None:
        import json

        info = json.loads(
            json.dumps(next(r for q, r in replay.routes if q.get("method") == "load_info"))
        )
        pool = info["data"]["storagePools"][0]
        pool["progress"] = {"step": "resync", "percent": "45", "cur_step": 1, "total_step": 2}
        replay.answer(LOAD_INFO, info)
        replay.answer({"method": "is_building"}, ok({"isBuilding": True}))

    text, _ = await run(get_storage_overview, setup=setup)
    assert "Pool 1 (reuse_1): resync 45%" in text


async def test_running_scrubbing_without_a_task() -> None:
    def setup(replay: Replay, _: Any) -> None:
        replay.answer({"method": "is_data_scrubbing"}, ok({"isBuilding": True}))
        replay.answer({"method": "get_sche_task_list"}, ok({}))

    text, _ = await run(get_storage_overview, REALNAS, setup=setup)
    assert "Running:  Data scrubbing" in text


async def test_an_absent_optional_api_is_noted() -> None:
    def setup(_: Replay, client: Any) -> None:
        del client._api_cache["SYNO.Core.System.SystemHealth"]

    text, _ = await run(get_storage_overview, setup=setup)
    assert "  - System health: not available on this NAS" in text


async def test_a_proxy_error_names_the_http_status() -> None:
    def setup(replay: Replay, _: Any) -> None:
        replay.answer({"method": "load_bad_disks"}, 502)

    text, _ = await run(get_storage_overview, setup=setup)
    assert "  - Bad-disk list: couldn't be read (HTTP 502)" in text
