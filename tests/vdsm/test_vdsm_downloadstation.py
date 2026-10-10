"""Integration tests for Download Station tools against virtual-dsm.

Requires DS installed in the golden image (see tests/vdsm/setup_dsm.py
_install_download_station_via_ssh). Tests skip cleanly if the package isn't
available on the connected NAS (e.g., running against a real NAS without DS).

One test per tool, exercising the real call shapes verified in #123: string
task statuses, the DS2 weekly-plan API, ``setserverconfig``, ``Task.edit``
v2, DS2 create (URI as form POST, torrent as multipart), and DSM's actual
delete semantics. Run on DSM 7.2.2 and 7.1 (``--dsm-version 7.1``).

Run with: uv run pytest -m vdsm -v tests/vdsm/test_vdsm_downloadstation.py
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from mcp_synology.core.client import DsmClient

pytestmark = pytest.mark.vdsm


# A magnet URI that's parseable but won't actually leech anything in CI
# (random hex hash with no real torrent behind it). DS accepts magnets at
# create time without verifying the swarm — the task sits at "waiting" or
# "downloading" with 0 bytes, which is fine for exercising the tools.
_FAKE_MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test-file"

_TASK_ID = re.compile(r"\bdbid_\d+\b")

# A small file on an IPv4-only host. vdsm's QEMU user networking has no IPv6
# route, and DS 3.9 (what Package Center serves DSM 7.1) does not fall back
# from IPv6 the way curl does: a dual-stack URL such as http://example.com/
# sits at 0 bytes forever on 7.1 while the same guest's curl fetches it (#123).
_IPV4_ONLY_FILE = "http://ipv4.download.thinkbroadband.com/5MB.zip"

# How long to wait for the DS back end to answer after the container reports
# DSM ready. DSM's web server comes up before DS's CGIs, which answer
# 502 Bad Gateway until the package finishes starting (seen in CI on #107).
_DS_READY_TIMEOUT = 180


def _unpack(nas_client: Any) -> tuple[DsmClient, Any, Any, dict[str, str]]:
    """Unpack the nas_client tuple yielded by conftest."""
    return nas_client  # type: ignore[return-value]


@pytest.fixture
async def _ds_available(nas_client: Any) -> bool:
    """Skip if DS isn't installed; otherwise wait until its back end answers."""
    client, _, _, _ = _unpack(nas_client)
    info = await client.query_api_info()
    if "SYNO.DownloadStation.Task" not in info:
        pytest.skip("Download Station package not installed on this NAS")

    deadline = time.monotonic() + _DS_READY_TIMEOUT
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            await client.request("SYNO.DownloadStation.Info", "getinfo", version=1)
            return True
        except httpx.HTTPStatusError as e:  # 502 while the DS CGIs start
            last = e
            await asyncio.sleep(3)
    pytest.fail(
        f"Download Station back end did not answer within {_DS_READY_TIMEOUT}s "
        f"after DSM was ready (last error: {last})"
    )


async def _all_task_ids(client: DsmClient) -> list[str]:
    data = await client.request("SYNO.DownloadStation.Task", "list", version=1)
    return [str(t["id"]) for t in data.get("tasks", [])]


@pytest.fixture
async def _clean_queue(nas_client: Any, _ds_available: bool) -> Any:
    """Start and end each write test with an empty DS queue."""
    client, _, _, _ = _unpack(nas_client)

    async def _purge() -> None:
        ids = await _all_task_ids(client)
        if ids:
            await client.request(
                "SYNO.DownloadStation.Task",
                "delete",
                version=1,
                params={"id": ",".join(ids), "force_complete": "false"},
            )

    await _purge()
    yield
    await _purge()


def _ids_in(text: str) -> list[str]:
    return _TASK_ID.findall(text)


def _synthetic_torrent(tmp_path: Path) -> Path:
    """A minimal valid single-file .torrent (bencoded), no network needed."""

    def b(x: Any) -> bytes:
        if isinstance(x, int):
            return b"i%de" % x
        if isinstance(x, str):
            x = x.encode()
        if isinstance(x, bytes):
            return b"%d:%s" % (len(x), x)
        return b"d" + b"".join(b(k) + b(x[k]) for k in sorted(x)) + b"e"

    payload = b"mcp-synology vdsm torrent payload" * 64
    info = {
        "name": "vdsm-probe.bin",
        "length": len(payload),
        "piece length": 16384,
        "pieces": hashlib.sha1(payload).digest(),  # noqa: S324 — BitTorrent v1 uses SHA-1
    }
    path = tmp_path / "vdsm-probe.torrent"
    path.write_bytes(b({"announce": "http://127.0.0.1:1/announce", "info": info}))
    return path


class TestVdsmDownloadStationReads:
    async def test_list_downloads_empty_queue(self, nas_client: Any, _clean_queue: None) -> None:
        from mcp_synology.modules.downloadstation.tasks import list_downloads

        client, _, _, _ = _unpack(nas_client)
        result = await list_downloads(client)
        assert "Download Station queue" in result
        assert "No items to display" in result

    async def test_get_download_stats_returns_speeds(
        self, nas_client: Any, _ds_available: bool
    ) -> None:
        from mcp_synology.modules.downloadstation.stats import get_download_stats

        client, _, _, _ = _unpack(nas_client)
        result = await get_download_stats(client)
        assert "Download (total)" in result
        assert "Upload (total)" in result

    async def test_get_download_config_returns_known_fields(
        self, nas_client: Any, _ds_available: bool
    ) -> None:
        from mcp_synology.modules.downloadstation.config import get_download_config

        client, _, _, _ = _unpack(nas_client)
        result = await get_download_config(client)
        assert "Default destination" in result
        assert "BT max download" in result
        # The bake configured default_destination=writable, and nas_client is
        # admin, so this is the NAS-wide view rather than a per-user one.
        assert "writable" in result
        assert "your user view" not in result

    async def test_get_schedule_returns_flags_rates_and_grid(
        self, nas_client: Any, _ds_available: bool
    ) -> None:
        from mcp_synology.modules.downloadstation.config import get_schedule

        client, _, _, _ = _unpack(nas_client)
        result = await get_schedule(client)
        assert "Enabled" in result
        assert "Limited download rate" in result
        assert "Sun" in result and "Sat" in result
        assert "Legend" in result
        assert "malformed" not in result


class TestVdsmDownloadStationTasks:
    async def test_create_list_info_pause_resume_edit_delete(
        self, nas_client: Any, _clean_queue: None
    ) -> None:
        from mcp_synology.modules.downloadstation.tasks import (
            create_download,
            delete_download,
            edit_download,
            get_download_info,
            list_downloads,
            pause_download,
            resume_download,
        )

        client, _, _, _ = _unpack(nas_client)

        created = await create_download(client, uri=_FAKE_MAGNET, destination="writable")
        ids = _ids_in(created)
        assert len(ids) == 1, f"create_download should return the new task id:\n{created}"
        task_id = ids[0]

        listed = await list_downloads(client)
        assert task_id in listed
        assert "unknown(" not in listed  # string statuses render (#123 bug 1)
        in_flight = await list_downloads(client, status_filter="downloading")
        assert task_id in in_flight  # filters work on string statuses

        info = await get_download_info(client, task_id=task_id)
        assert "Detail" in info and "writable" in info

        paused = await pause_download(client, task_ids=[task_id])
        assert "ok" in paused
        assert task_id in await list_downloads(client, status_filter="paused")
        resumed = await resume_download(client, task_ids=[task_id])
        assert "ok" in resumed

        edited = await edit_download(client, task_ids=[task_id], destination="testshare")
        assert "ok" in edited  # Task.edit v2 (#123 bug 5)
        assert "testshare" in await get_download_info(client, task_id=task_id)

        deleted = await delete_download(client, task_ids=[task_id])
        assert "ok" in deleted
        assert task_id not in await _all_task_ids(client)

    async def test_create_from_magnet_longer_than_get_url_limit(
        self, nas_client: Any, _clean_queue: None
    ) -> None:
        """#123 bug 14: a GET URL past ~8 KB gets HTTP 414; create uses POST."""
        from mcp_synology.modules.downloadstation.tasks import create_download

        client, _, _, _ = _unpack(nas_client)
        tracker = "&tr=http%3A%2F%2Ftracker{}.invalid%3A6969%2Fannounce"
        trackers = "".join(tracker.format(i) for i in range(250))
        long_magnet = "magnet:?xt=urn:btih:" + "c" * 40 + "&dn=long-magnet" + trackers
        assert len(long_magnet) > 10_000
        created = await create_download(client, uri=long_magnet, destination="writable")
        assert len(_ids_in(created)) == 1, created

    async def test_create_from_torrent_file(
        self, nas_client: Any, _clean_queue: None, tmp_path: Path
    ) -> None:
        """#123 bug 6: v1 file upload is broken on DSM 7.2.2; DS2 multipart works."""
        from mcp_synology.modules.downloadstation.tasks import create_download

        client, _, _, _ = _unpack(nas_client)
        torrent = _synthetic_torrent(tmp_path)
        created = await create_download(
            client, torrent_file_path=str(torrent), destination="writable"
        )
        ids = _ids_in(created)
        assert len(ids) == 1, created
        assert ids[0] in await _all_task_ids(client)

    async def test_delete_keeps_finished_file(self, nas_client: Any, _clean_queue: None) -> None:
        """#123 bug 7: DSM never deletes a finished task's file."""
        from mcp_synology.modules.downloadstation.tasks import create_download, delete_download

        client, _, _, _ = _unpack(nas_client)
        created = await create_download(client, uri=_IPV4_ONLY_FILE, destination="writable")
        task_id = _ids_in(created)[0]
        status = ""
        for _ in range(30):
            await asyncio.sleep(2)
            data = await client.request(
                "SYNO.DownloadStation.Task", "getinfo", version=1, params={"id": task_id}
            )
            status = data["tasks"][0]["status"]
            if status in ("finished", "seeding", "error"):
                break
        if status != "finished":
            pytest.skip(f"vdsm guest could not complete an HTTP download (status={status!r})")
        title = data["tasks"][0]["title"]

        await delete_download(client, task_ids=[task_id])
        listing = await client.request(
            "SYNO.FileStation.List", "list", version=2, params={"folder_path": "/writable"}
        )
        names = [f["name"] for f in listing.get("files", [])]
        assert title in names, f"finished file {title!r} should be kept; /writable has {names}"


class TestVdsmDownloadStationSettings:
    async def test_set_download_config_round_trip(
        self, nas_client: Any, _ds_available: bool
    ) -> None:
        """#123 bug 4: config writes go through setserverconfig."""
        from mcp_synology.modules.downloadstation.config import set_download_config

        client, _, _, _ = _unpack(nas_client)
        before = await client.request("SYNO.DownloadStation.Info", "getconfig", version=1)
        try:
            await set_download_config(client, bt_max_download=123)
            after = await client.request("SYNO.DownloadStation.Info", "getconfig", version=1)
            assert after["bt_max_download"] == 123
        finally:
            await set_download_config(client, bt_max_download=int(before["bt_max_download"]))

    async def test_set_schedule_plan_and_rates_round_trip(
        self, nas_client: Any, _ds_available: bool
    ) -> None:
        """#123 bugs 2/3: the plan is read from and written to DS2."""
        from mcp_synology.modules.downloadstation.config import get_schedule, set_schedule

        client, _, _, _ = _unpack(nas_client)
        scheduler = "SYNO.DownloadStation2.Settings.Scheduler"
        before = await client.request(scheduler, "get", version=1)
        plan = "2" * 24 + "0" * 24 + "1" * 120  # Sun limited, Mon off, rest full
        try:
            await set_schedule(client, schedule_plan=plan, download_rate=256, upload_rate=0)
            stored = await client.request(scheduler, "get", version=1)
            assert stored["schedule"] == plan
            assert stored["download_rate"] == 256
            rendered = await get_schedule(client)
            sun = next(ln for ln in rendered.splitlines() if ln.startswith("Sun"))
            mon = next(ln for ln in rendered.splitlines() if ln.startswith("Mon"))
            assert sun.count("~") == 24 and mon.count(".") == 24
        finally:
            await set_schedule(
                client,
                schedule_plan=str(before["schedule"]),
                download_rate=int(before["download_rate"]),
                upload_rate=int(before["upload_rate"]),
            )
