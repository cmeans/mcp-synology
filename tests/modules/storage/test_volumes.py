"""list_volumes and get_volume against the real-NAS and vdsm captures."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_synology.core.formatting import format_timestamp
from mcp_synology.modules.storage.helpers import EPOCH_NOTE
from mcp_synology.modules.storage.volumes import get_volume, list_volumes
from tests.modules.storage.dsm import VDSM, Replay, err, flat, load_info_with, ok, run, run_error


async def test_real_nas_list() -> None:
    text, replay = await run(list_volumes)
    assert replay.unmatched == []
    assert "Volume 1 (/volume1)\n" in text
    assert flat("Size: 35.4 TiB of 36.6 TiB used (96.5%)") in flat(text)
    assert flat("Free: 1.3 TiB") in flat(text)
    assert flat("Status: attention, fs_almost_full") in flat(text)
    assert flat("Storage pool: pool 1 (reuse_1)") in flat(text)
    # DSM 7.1 sends no encryption fields.
    assert "Encrypted" not in text
    assert "vspace_can_do" not in text


@pytest.mark.parametrize("fixture", VDSM)
async def test_vdsm_list(fixture: str) -> None:
    text, replay = await run(list_volumes, fixture)
    assert replay.unmatched == []
    assert flat("Storage pool: none (not in a storage pool)") in flat(text)
    assert flat("RAID type: Basic") in flat(text)
    assert ("Encrypted:" in text) == (fixture != "dsm-7.1-vdsm.json")


async def test_real_nas_get() -> None:
    text, replay = await run(get_volume, volume="volume_1")
    assert replay.unmatched == []
    assert flat("Space by category: 36.6 TiB total, 1.3 TiB free") in flat(text)
    assert flat("Shared folders: 35.3 TiB (96.3%)") in flat(text)
    assert flat("iSCSI LUNs: 31.9 GiB (0.1%)") in flat(text)
    assert flat("SSD TRIM: support: not support") in flat(text)
    assert flat("Space reclamation: idle") in flat(text)
    assert flat(
        "SSD cache: Cache 1 (shared_cache_1, volume cache alloc_cache_1_1): read-only"
    ) in flat(text)
    assert flat("shared cache: can_modify: yes") in flat(text)
    assert "  - Deduplication: not available on this NAS" in text
    # get_* shows the UI-only fields raw.
    assert "vspace_can_do:" in text
    # The list goes as one JSON string; a plain list answers 101.
    dedupe = replay.calls("SYNO.Storage.CGI.BtrfsDedupe", "get_volume_info")
    assert dedupe[0]["vol_paths"] == '["/volume1"]'
    shared = replay.calls("SYNO.Storage.CGI.Flashcache", "shared_cache_config_get")
    assert shared[0]["shared_cache"] == '"shared_cache_1"'


async def test_vdsm_741_get() -> None:
    text, replay = await run(get_volume, "dsm-7.4.1-vdsm.json", volume="/volume1")
    assert replay.unmatched == []
    assert "off for this volume (turn on space usage details" in text
    assert flat("Deduplication: supported") in flat(text)
    assert flat("has_snapshot: yes") in flat(text)


@pytest.mark.parametrize("name", ["/volume1", "volume1", "volume_1"])
async def test_volume_names(name: str) -> None:
    text, _ = await run(get_volume, volume=name)
    assert text.startswith("Volume 1 (/volume1)")


async def test_unknown_volume_lists_the_valid_ones() -> None:
    error = await run_error(get_volume, volume="/volume9")
    assert error["code"] == "not_found"
    assert error["valid"] == ["/volume1"]
    assert error["param"] == "volume"


async def test_space_usage_error_is_noted() -> None:
    def setup(replay: Replay, _: Any) -> None:
        replay.answer({"method": "get_space_usage"}, err(48640))

    text, _ = await run(get_volume, volume="/volume1", setup=setup)
    assert "Space by category: couldn't be read (DSM error 48640)" in text


async def test_encrypted_locked_volume() -> None:
    def setup(replay: Replay, _: Any) -> None:
        def change(data: dict[str, Any]) -> None:
            data["volumes"][0].update(is_encrypted=True, is_locked=True, is_vault_contain_key=False)

        load_info_with(replay, change)

    text, _ = await run(list_volumes, setup=setup)
    assert flat("Encrypted: yes") in flat(text)
    assert flat("Locked: yes") in flat(text)
    assert flat("Key in the key vault: no") in flat(text)


async def test_epoch_fields_are_times() -> None:
    text, _ = await run(list_volumes)
    assert flat("last_done_time: —") in flat(text)
    assert flat("next_schedule_time: —") in flat(text)


async def test_space_usage_off_on_the_real_nas_shape() -> None:
    def setup(replay: Replay, _: Any) -> None:
        reply = next(
            r for q, r in replay.routes
            if q.get("method") == "get_space_usage" and q.get("volume_path") == "/volume1"
        )  # fmt: skip
        off = {**reply, "data": {**reply["data"], "status": "disabled"}}
        replay.answer({"method": "get_space_usage"}, off)

    text, _ = await run(get_volume, volume="/volume1", setup=setup)
    assert "off for this volume" in text
    assert "Shared folders" not in text


@pytest.mark.parametrize("name", ["VOLUME1", "/volume1/", "Volume_1"])
async def test_volume_names_ignore_case_and_a_trailing_slash(name: str) -> None:
    text, _ = await run(get_volume, volume=name)
    assert text.startswith("Volume 1 (/volume1)")


async def test_epoch_times_shown_as_times() -> None:
    """QA R1 #1: deduplication, space reclamation and ``dedup_info`` times are epochs with
    no local counterpart, shown as "about … UTC" like every other (spec: Formatting). Every
    capture has them at 0, so they're set here."""
    t = 1_790_000_000
    shown = f"about {format_timestamp(t)}"

    def setup(replay: Replay, _: Any) -> None:
        dedupe = {
            "vol_path": "/volume1",
            "support_dedupe": True,
            "last_manual_dedupe_time": t,
            "last_dry_run_time": t,
            "schedule_disable_time": t,
        }
        replay.answer({"method": "get_volume_info"}, ok({"volumes": [dedupe]}))
        replay.answer(
            {"method": "get_space_reclaim_status"}, ok({"state": "running", "delay_end_time": t})
        )
        load_info_with(
            replay, lambda data: data["volumes"][0]["dedup_info"].update(last_run_time=t)
        )

    text, _ = await run(get_volume, "dsm-7.3.2-vdsm.json", setup=setup, volume="/volume1")
    assert str(t) not in text
    for key in ("last_manual_dedupe_time", "last_dry_run_time", "schedule_disable_time"):
        assert flat(f"{key}: {shown}") in flat(text)
    assert flat(f"delay_end_time: {shown}") in flat(text)
    assert f'"last_run_time": "{shown}"' in text  # inside dedup_info
    assert EPOCH_NOTE in text


async def test_ssd_trim_reply_without_its_key_falls_back() -> None:
    """QA R1 #6: an ssd_trim_get reply without ``ssd_trim`` uses the volume's own."""

    def setup(replay: Replay, _: Any) -> None:
        replay.answer({"method": "ssd_trim_get"}, ok({}))
        load_info_with(replay, lambda data: data["volumes"][0].update(ssd_trim={"support": "x1"}))

    text, _ = await run(get_volume, setup=setup, volume="volume_1")
    assert flat("SSD TRIM: support: x1") in flat(text)
