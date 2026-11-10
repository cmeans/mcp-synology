"""list_disks and get_disk against the real-NAS and vdsm captures."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from mcp_synology.modules.storage.disks import get_disk, list_disks, smart_table, trend_lines
from mcp_synology.modules.storage.overview import get_storage_overview, low_performance
from tests.modules.storage.dsm import (
    VDSM,
    Replay,
    err,
    flat,
    load_info_with,
    ok,
    run,
    run_error,
)


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


async def test_real_nas_low_performance_in_raid() -> None:
    """#659: DSM flags sdb as lagging its RAID only in sdb's own get_health_info. list_disks
    reads each disk's and shows the flag on that disk's status, and only there."""
    text, replay = await run(list_disks)
    assert replay.unmatched == []
    flagged = [line for line in text.splitlines() if "low performance in RAID" in line]
    assert [line.split(":", 1)[1].strip() for line in flagged] == [
        "normal (low performance in RAID: now)"
    ]
    sdb = text.split("Drive 2 (sdb)", 1)[1].split("\n\nDrive ", 1)[0]
    assert flat("Status: normal (low performance in RAID: now)") in flat(sdb)
    devices = [c["device"] for c in replay.calls("SYNO.Storage.CGI.Smart", "get_health_info")]
    assert devices == [f"/dev/sd{x}" for x in "abcdef"]


async def test_real_nas_get_disk_low_performance_in_raid() -> None:
    # The capture has only get_health_info for sdb, sdc and sdd (#659), so their other sections
    # are unmatched here and read as notes; the status line is what's checked.
    text, _ = await run(get_disk, disk="sdb")
    assert flat("Status: normal (low performance in RAID: now)") in flat(text)
    other, _ = await run(get_disk, disk="sdc")
    assert "low performance" not in other


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
    # valid holds what get_disk takes back; the message keeps the bay names (QA R1 #5).
    assert error["valid"][:2] == ["sda", "sdb"]
    assert "sda (Drive 1)" in error["message"]
    text, _ = await run(get_disk, disk=error["valid"][0], history_limit=20)
    assert text.startswith("Drive 1")


@pytest.mark.parametrize("name", ["Drive 1", "drive 1"])
async def test_a_bay_name_two_disks_share_picks_neither(name: str) -> None:
    """#614 class (pre-QA on #615): a bay in the NAS and one in an expansion unit can both be
    "Drive 1". The first match could be the other disk, so neither is shown; each is listed
    with its ID, which is unique, and its ID picks it."""

    def setup(replay: Replay, _: Any) -> None:
        def twin(data: dict[str, Any]) -> None:
            data["disks"][1]["name"] = data["disks"][0]["name"]

        load_info_with(replay, twin)

    error = await run_error(get_disk, disk=name, setup=setup)
    assert error["code"] == "invalid_parameter"
    assert (error["param"], error["valid"]) == ("disk", ["sda", "sdb"])
    assert "2 disks match" in error["message"]
    assert "sda (" in error["message"] and "sdb (" in error["message"]
    assert error["suggestion"] == "Ask the user which one they mean, then pass its ID."
    text, _ = await run(get_disk, disk="sdb", history_limit=20, setup=setup)
    assert "(sdb)" in text.splitlines()[0]


def test_trend_without_history() -> None:
    assert trend_lines(None) == []
    assert trend_lines({"healthInfo": {"history": {"incremental": {"unc": []}}}}) == []


async def test_missing_size_is_a_dash() -> None:
    """QA R1 #3: a disk without size_total showed "Size: None"."""

    def setup(replay: Replay, _: Any) -> None:
        load_info_with(replay, lambda data: data["disks"][0].pop("size_total"))

    text, _ = await run(list_disks, setup=setup)
    assert "None" not in text


def test_smart_table_missing_cell_is_a_dash() -> None:
    """Pre-QA sibling of QA R1 #3: a S.M.A.R.T. attribute without ``raw`` showed None."""
    table = smart_table({"smartInfo": [{"id": 5, "name": "Reallocated", "current": 100}]})
    assert "None" not in table and "—" in table


@pytest.mark.parametrize(
    ("health", "expected"),
    [
        ({"healthInfo": {"overview": {"low_performance_in_raid": "now"}}}, "now"),
        ({"healthInfo": {"overview": {"low_performance_in_raid": "normal"}}}, None),
        ({"healthInfo": {"overview": {"low_performance_in_raid": ""}}}, None),
        ({"healthInfo": {"overview": {"low_performance_in_raid": False}}}, None),
        ({"healthInfo": {"overview": {"low_performance_in_raid": {"x": 1}}}}, None),
        ({"healthInfo": {"overview": {}}}, None),
        ({"healthInfo": {}}, None),
        ({"healthInfo": None}, None),
        (None, None),
        ([], None),
    ],
)
def test_low_performance(health: Any, expected: str | None) -> None:
    assert low_performance(health) == expected


@pytest.mark.parametrize("reply", [500, err(117)], ids=["http-500", "dsm-117"])
@pytest.mark.parametrize("tool", [list_disks, get_storage_overview], ids=["list", "overview"])
async def test_a_disk_whose_flag_cant_be_read_is_a_note(tool: Any, reply: Any) -> None:
    """Pre-QA on #661: one disk's get_health_info failing leaves a note naming the flag, not
    the disk's health, and the tool still answers."""

    def setup(replay: Replay, _: Any) -> None:
        replay.answer(
            {"api": "SYNO.Storage.CGI.Smart", "method": "get_health_info", "device": "/dev/sda"},
            reply,
        )

    text, _ = await run(tool, setup=setup)
    assert "Drive 1 (sda) low-performance-in-RAID flag:" in text
    assert "Drive 1 (sda) health" not in text
    assert "low performance in RAID: now" in text or "low performance in RAID (now)" in text


@pytest.mark.parametrize("tool", [list_disks, get_storage_overview], ids=["list", "overview"])
async def test_without_the_smart_api_one_note(tool: Any) -> None:
    def setup(replay: Replay, client: Any) -> None:
        client._api_cache.pop("SYNO.Storage.CGI.Smart", None)

    text, replay = await run(tool, setup=setup)
    assert text.count("Low-performance-in-RAID flags: not available on this NAS") == 1
    assert "low-performance-in-RAID flag:" not in text
    assert replay.calls("SYNO.Storage.CGI.Smart", "get_health_info") == []


@pytest.mark.parametrize(
    "exc",
    [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("dropped")],
    ids=["timeout", "drop"],
)
@pytest.mark.parametrize("tool", [list_disks, get_storage_overview], ids=["list", "overview"])
async def test_a_disk_whose_flag_gets_no_answer_is_a_note(tool: Any, exc: Exception) -> None:
    """Pre-QA r2 on #661: a transport error on one disk's flag failed the whole tool."""

    def setup(replay: Replay, _: Any) -> None:
        original = replay._reply

        def reply(query: dict[str, str]) -> Any:
            if query.get("method") == "get_health_info" and query.get("device") == "/dev/sda":
                raise exc
            return original(query)

        replay._reply = reply  # type: ignore[method-assign]

    text, _ = await run(tool, setup=setup)
    assert (
        f"Drive 1 (sda) low-performance-in-RAID flag: couldn't be read "
        f"(no answer: {type(exc).__name__})"
    ) in text
    assert "slow" not in text and "dropped" not in text


@pytest.mark.parametrize("fixture", VDSM)
async def test_no_disks_no_flag_note(fixture: str) -> None:
    """Without disks there's nothing to flag: no note even when the API is missing."""

    def setup(replay: Replay, client: Any) -> None:
        client._api_cache.pop("SYNO.Storage.CGI.Smart", None)

    text, _ = await run(get_storage_overview, fixture, setup=setup)
    assert "Low-performance-in-RAID" not in text


@pytest.mark.parametrize("tool", [list_disks, get_storage_overview], ids=["list", "overview"])
async def test_after_one_unanswered_request_nothing_more_is_sent(tool: Any) -> None:
    """Pre-QA r3 on #661: on a NAS that stops answering, each section waited out its own
    timeout (12 x 30 s for the overview, past claude.ai's 60 s). After the first, the rest
    aren't sent."""
    sent_after: list[str] = []

    def setup(replay: Replay, _: Any) -> None:
        original = replay._reply
        stalled = {"on": False}

        def reply(query: dict[str, str]) -> Any:
            if query.get("method") == "load_info":
                return original(query)
            if stalled["on"]:
                sent_after.append(query.get("method", ""))
            stalled["on"] = True
            raise httpx.ReadTimeout("busy")

        replay._reply = reply  # type: ignore[method-assign]

    text, _ = await run(tool, setup=setup)
    assert sent_after == []
    assert text.count("couldn't be read (no answer: ReadTimeout)") == 1
    assert "not read (DSM didn't answer an earlier request)" in text
    assert text.count("Low-performance-in-RAID flags") == 1


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("refused"), httpx.ConnectTimeout("t"), httpx.PoolTimeout("t")],
    ids=["connect", "connect-timeout", "pool"],
)
async def test_a_connection_that_couldnt_be_opened_isnt_called_no_answer(
    exc: Exception,
) -> None:
    def setup(replay: Replay, _: Any) -> None:
        original = replay._reply

        def reply(query: dict[str, str]) -> Any:
            if query.get("method") == "get_health_info":
                raise exc
            return original(query)

        replay._reply = reply  # type: ignore[method-assign]

    name = type(exc).__name__
    text, _ = await run(list_disks, setup=setup)
    assert f"Drive 1 (sda) low-performance-in-RAID flag: couldn't be read ({name})" in text
    assert f"no answer: {name}" not in text


@pytest.mark.parametrize("reply", [502, 503, 504], ids=["502", "503", "504"])
async def test_a_proxy_error_stalls_too(reply: int) -> None:
    """Pre-QA r4 on #661: a proxy's 5xx is how a proxy reports a stall (#282); each later
    section waited out the proxy's own timeout."""
    sent_after: list[str] = []

    def setup(replay: Replay, _: Any) -> None:
        original = replay._reply
        seen = {"first": False}

        def answer(query: dict[str, str]) -> Any:
            if query.get("method") == "load_info":
                return original(query)
            if seen["first"]:
                sent_after.append(query.get("method", ""))
            seen["first"] = True
            return reply

        replay._reply = answer  # type: ignore[method-assign]

    text, _ = await run(get_storage_overview, setup=setup)
    assert sent_after == []
    assert f"couldn't be read (HTTP {reply})" in text


@pytest.mark.parametrize("stall", ["usb", "esata"])
async def test_external_storage_sends_one_unanswered_request(stall: str) -> None:
    """Pre-QA r4 on #661: the eSATA probe had its own Sections, so a stall on USB still sent
    eSATA, and a stall on the probe resent eSATA without `additional`."""
    from mcp_synology.modules.storage.external import list_external_storage

    unanswered: list[str] = []

    def setup(replay: Replay, _: Any) -> None:
        original = replay._reply

        def answer(query: dict[str, str]) -> Any:
            api = query.get("api", "")
            if (stall == "usb" and "USB" in api) or "eSATA" in api or unanswered:
                unanswered.append(api)
                raise httpx.ReadTimeout("busy")
            return original(query)

        replay._reply = answer  # type: ignore[method-assign]

    text, _ = await run(list_external_storage, setup=setup)
    assert len(unanswered) == 1, unanswered
    assert text.count("no answer: ReadTimeout") == 1
