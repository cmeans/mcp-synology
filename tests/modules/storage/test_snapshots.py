"""list_snapshots against the real-NAS and vdsm captures."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mcp_synology.modules.storage.snapshots import list_snapshots, snapshot_time
from tests.modules.storage.dsm import REALNAS, Replay, err, flat, ok, run, run_error

SHARE_LIST = {"api": "SYNO.Core.Share", "method": "list"}


def _shares_with_counts(replay: Replay, _: Any) -> None:
    """vdsm has only the bare Core.Share list: add the snapshot count the tool asks for."""
    bare = next(r for q, r in replay.routes if q.get("api") == "SYNO.Core.Share")
    data = json.loads(json.dumps(bare["data"]))
    for share in data["shares"]:
        share["snapshot_info"] = {"snapshot_count": 1 if share["name"] == "writable" else 0}
    replay.answer(SHARE_LIST, ok(data))


async def test_counts_per_share() -> None:
    text, replay = await run(list_snapshots)
    assert "share01" in text and "share04" in text
    assert replay.calls("SYNO.Core.Share", "list")[0]["additional"] == (
        '["vol_path", "enable_snapshot", "snapshot_count"]'
    )
    # The real NAS's capture has a schedule for four shares only (share04, 06, 09, 17).
    assert {q["method"] for q in replay.unmatched} == {"get_schedule"}
    row = next(line for line in text.splitlines() if line.strip().startswith("share04"))
    assert row.split()[1:] == ["/volume1", "0", "no"]


# Each image's own snapshot of writable: its name is in the NAS's zone, the tool gives UTC.
_TAKEN = {
    "7.1": "2026-10-28 15:04:27 UTC",  # GMT-05-2026.10.28-10.04.27
    "7.2.2": "2026-10-28 15:04:00 UTC",  # GMT+00-2026.10.28-15.04.00
    "7.3.2": "2026-10-28 15:04:28 UTC",  # GMT+0530-2026.10.28-20.34.28
    "7.4.1": "2026-10-28 15:04:28 UTC",  # GMT+01-2026.10.28-16.04.28
}


@pytest.mark.parametrize("version", ["7.1", "7.2.2", "7.3.2", "7.4.1"])
async def test_one_share(version: str) -> None:
    text, replay = await run(
        list_snapshots, f"dsm-{version}-vdsm.json", setup=_shares_with_counts, share="writable"
    )
    assert replay.unmatched == []
    assert flat("Snapshots: 1") in flat(text)
    assert flat("Schedule: off") in flat(text)
    assert _TAKEN[version] in text
    row = next(line for line in text.splitlines() if "mcp capture" in line)
    cells = [c for c in row.split("  ") if c.strip()]
    # Taken, Name, Description, Locked, Immutable, Made
    assert cells[3].strip() == "no"
    assert cells[4].strip() == ("—" if version == "7.1" else "no")  # 7.1 sends no worm_lock
    assert cells[5].strip() == "manual"
    request = replay.calls("SYNO.Core.Share.Snapshot", "list")[0]
    assert (request["version"], request["name"]) == ("2", '"writable"')


async def test_unknown_share_is_not_found_before_any_snapshot_call() -> None:
    replays: list[Replay] = []
    error = await run_error(list_snapshots, share="no-such-share", replay_out=replays)
    assert error["code"] == "not_found"
    assert "share01" in error["valid"]
    assert replays[0].calls("SYNO.Core.Share.Snapshot", "list") == []
    assert replays[0].calls("SYNO.Core.Share.Snapshot", "get_schedule") == []


async def test_snapshot_list_v2_missing() -> None:
    error = await run_error(list_snapshots, cache={"snapshot_max": 1})
    assert error["code"] == "not_supported"
    assert "Snapshot Replication" in error["message"]


async def test_snapshot_replication_missing() -> None:
    def setup(_: Replay, client: Any) -> None:
        del client._api_cache["SYNO.Core.Share.Snapshot"]

    error = await run_error(list_snapshots, setup=setup)
    assert error["code"] == "api_not_found"
    assert "Snapshot Replication" in error["message"]


async def test_paging_newest_first() -> None:
    snaps = [
        {"time": f"GMT+00-2026.10.{d:02d}-12.00.00", "desc": f"desc-{d}", "lock": False,
         "schedule_snapshot": True}
        for d in range(1, 6)
    ]  # fmt: skip

    def setup(replay: Replay, _: Any) -> None:
        replay.answer(
            {"api": "SYNO.Core.Share.Snapshot", "method": "list"},
            ok({"snapshots": snaps, "total": 5}),
        )

    text, _ = await run(list_snapshots, REALNAS, setup=setup, share="share01", offset=1, limit=2)
    assert text.index("desc-4") < text.index("desc-3")
    assert "desc-5" not in text and "desc-2" not in text
    assert "Showing 2–3 of 5, newest first. Use offset=3 for more." in text
    assert "scheduled" in text


async def test_offset_past_the_end() -> None:
    def setup(replay: Replay, _: Any) -> None:
        snap = {"time": "GMT+00-2026.10.01-12.00.00"}
        replay.answer(
            {"api": "SYNO.Core.Share.Snapshot", "method": "list"}, ok({"snapshots": [snap]})
        )

    text, _ = await run(list_snapshots, REALNAS, setup=setup, share="share01", offset=5)
    assert "No snapshots at offset 5 (there are 1)." in text


async def test_schedule_on() -> None:
    def setup(replay: Replay, _: Any) -> None:
        replay.answer(
            {"method": "get_schedule"},
            ok(
                {
                    "enable_snapshot_schedule": True,
                    "schedule": {"next_trigger_time": "2026-10-29 01:00", "hour": 1},
                }
            ),
        )

    text, _ = await run(list_snapshots, REALNAS, setup=setup, share="share04")
    assert flat("Schedule: on, next 2026-10-29 01:00 (NAS time)") in flat(text)


async def test_non_admin() -> None:
    def setup(replay: Replay, _: Any) -> None:
        replay.answer(SHARE_LIST, err(105))

    error = await run_error(list_snapshots, setup=setup)
    assert error["code"] == "permission_denied"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("GMT+00-2026.10.28-15.04.00", "2026-10-28 15:04:00 UTC"),
        ("GMT-05-2026.10.28-10.04.00", "2026-10-28 15:04:00 UTC"),
        ("GMT+05:30-2026.10.28-20.34.00", "2026-10-28 15:04:00 UTC"),
        ("GMT+0530-2026.10.28-20.34.00", "2026-10-28 15:04:00 UTC"),
        ("weird", "weird"),
    ],
)
def test_snapshot_time(name: str, expected: str) -> None:
    assert snapshot_time(name) == expected


@pytest.mark.parametrize(
    ("kwargs", "param"),
    [({"limit": 0}, "limit"), ({"limit": 501}, "limit"), ({"offset": -1}, "offset")],
)
async def test_paging_out_of_range_is_refused_first(kwargs: dict[str, int], param: str) -> None:
    replays: list[Replay] = []
    error = await run_error(list_snapshots, share="share01", replay_out=replays, **kwargs)
    assert error["code"] == "invalid_parameter"
    assert error["param"] == param
    assert replays[0].seen == []


async def test_share_name_in_any_case() -> None:
    text, replay = await run(list_snapshots, share="SHARE04/")
    assert text.startswith("Snapshots of share04")
    assert replay.calls("SYNO.Core.Share.Snapshot", "list")[0]["name"] == '"share04"'
