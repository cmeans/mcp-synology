"""Integration tests for Download Station tools against virtual-dsm.

Requires DS installed in the golden image (see tests/vdsm/setup_dsm.py
_install_download_station_via_ssh). Tests skip only when the golden image's
meta.json doesn't list DS; an image that lists it but whose DS APIs are
missing (a stopped package) goes to ds_backend_ready and fails on CI.

One test per tool, exercising the real call shapes verified in #123: string
task statuses, the DS2 weekly-plan API, ``setserverconfig``, ``Task.edit``
v2, DS2 create (URI as form POST, torrent as multipart), and DSM's actual
delete semantics. Run on DSM 7.2.2 and 7.1 (``--dsm-version 7.1``).

Run with: uv run pytest -m vdsm -v tests/vdsm/test_vdsm_downloadstation.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from tests.vdsm.golden_image import download_station_skip_reason, load_golden_meta

if TYPE_CHECKING:
    from pathlib import Path

    from mcp_synology.core.client import DsmClient
    from tests.personas import Persona, ReauthSpy

pytestmark = pytest.mark.vdsm


# A magnet URI that's parseable but won't actually leech anything in CI
# (random hex hash with no real torrent behind it). DS accepts magnets at
# create time without verifying the swarm — the task sits at "waiting" or
# "downloading" with 0 bytes, which is fine for exercising the tools.
_FAKE_MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test-file"

_TASK_ID = re.compile(r"\bdbid_\d+\b")


def _magnet(n: int) -> str:
    """A distinct fake magnet per ``n`` (DS rejects a second task with the same hash)."""
    return f"magnet:?xt=urn:btih:{n:040x}&dn=test-file-{n}"


# HTTP download tests fetch a file the NAS serves itself (File Station
# Download on the guest's own 127.0.0.1:5000), so they never depend on an
# external host. External hosts made these tests skip on a slow network, and
# dual-stack ones never finish on DSM 7.1: vdsm's QEMU networking has no IPv6
# route and DS 3.9 does not fall back to IPv4 (#123).
_SOURCE_DIR = "/testshare"
_SOURCE_NAME = "ds-source.bin"
_SOURCE_SIZE = 4 * 1024 * 1024

# HTTP speed cap (KB/s) for the unfinished-task tests: slow enough that the
# 4 MB source stays partial for minutes, fast enough to show progress quickly.
_THROTTLE_KBPS = 4


def _unpack(nas_client: Any) -> tuple[DsmClient, Any, Any, dict[str, str]]:
    """Unpack the nas_client tuple yielded by conftest."""
    return nas_client  # type: ignore[return-value]


@pytest.fixture
async def _ds_available(nas_client: Any, dsm_version: str, request: pytest.FixtureRequest) -> bool:
    """Skip if the image has no DS; otherwise wait (once per session) for its back end."""
    client, _, _, _ = _unpack(nas_client)
    info = await client.query_api_info()
    reason = download_station_skip_reason(load_golden_meta(dsm_version), info)
    if reason is not None:
        pytest.skip(reason)
    request.getfixturevalue("ds_backend_ready")
    return True


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


def _local_url(client: DsmClient) -> str:
    """URL for the NAS-served source file, fetched by DS from inside the guest."""
    query = urlencode(
        {
            "api": "SYNO.FileStation.Download",
            "version": "2",
            "method": "download",
            "mode": "download",
            "path": f"{_SOURCE_DIR}/{_SOURCE_NAME}",
            "_sid": client.sid or "",
        }
    )
    return f"http://127.0.0.1:5000/webapi/entry.cgi?{query}"


async def _writable_names(client: DsmClient) -> list[str]:
    listing = await client.request(
        "SYNO.FileStation.List", "list", version=2, params={"folder_path": "/writable"}
    )
    return [f["name"] for f in listing.get("files", [])]


async def _remove_from_writable(client: DsmClient, name: str) -> None:
    from mcp_synology.modules.filestation.operations import delete_files

    if name in await _writable_names(client):
        await delete_files(client, paths=[f"/writable/{name}"])


@pytest.fixture
async def _local_source(nas_client: Any, _clean_queue: None, tmp_path: Path) -> Any:
    """Put a random file on the NAS for DS to download; clean up its copies after."""
    from mcp_synology.modules.filestation.transfer import upload_file

    client, _, _, _ = _unpack(nas_client)
    local = tmp_path / _SOURCE_NAME
    local.write_bytes(os.urandom(_SOURCE_SIZE))
    await upload_file(client, local_path=str(local), dest_folder=_SOURCE_DIR, overwrite=True)
    await _remove_from_writable(client, _SOURCE_NAME)
    yield _local_url(client)
    await _remove_from_writable(client, _SOURCE_NAME)


@pytest.fixture
async def _throttled(admin_client: Any, _ds_available: bool) -> Any:
    """Cap HTTP download speed; restore the previous cap afterward.

    Uses the v1 ``http_max_download`` speed cap (KB/s). DS 3.9 on DSM 7.1
    ignores the DS2 weekly plan's limited speed for HTTP tasks, so the plan
    can't hold a task partial there; this cap does on both versions. It is a
    NAS-wide setting, so it is set as admin (DSM ignores it from the test user).
    """
    client, _, _, _ = _unpack(admin_client)
    info = "SYNO.DownloadStation.Info"
    before = await client.request(info, "getconfig", version=1)
    await client.request(
        info, "setserverconfig", version=1, params={"http_max_download": str(_THROTTLE_KBPS)}
    )
    try:
        yield
    finally:
        await client.request(
            info,
            "setserverconfig",
            version=1,
            params={"http_max_download": str(before["http_max_download"])},
        )


async def _task(client: DsmClient, task_id: str) -> dict[str, Any]:
    data = await client.request(
        "SYNO.DownloadStation.Task",
        "getinfo",
        version=1,
        params={"id": task_id, "additional": "transfer"},
    )
    return data["tasks"][0]  # type: ignore[no-any-return]


async def _wait_for(
    client: DsmClient, task_id: str, done: Any, *, timeout: float = 120
) -> dict[str, Any]:
    """Poll a task until ``done(task)`` is true; fail with its last state otherwise."""
    deadline = time.monotonic() + timeout
    task = await _task(client, task_id)
    while not done(task):
        if task["status"] == "error" or time.monotonic() > deadline:
            pytest.fail(f"task {task_id} never reached the expected state: {task}")
        await asyncio.sleep(2)
        task = await _task(client, task_id)
    return task


def _downloaded(task: dict[str, Any]) -> int:
    return int(task.get("additional", {}).get("transfer", {}).get("size_downloaded", 0))


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
        self, admin_client: Any, _ds_available: bool
    ) -> None:
        from mcp_synology.modules.downloadstation.config import get_download_config

        client, _, _, _ = _unpack(admin_client)
        result = await get_download_config(client)
        assert "Default destination" in result
        assert "BT max download" in result
        # The bake configured default_destination=writable; as admin this is
        # the NAS-wide view (the test user's view is pinned in TestVdsmDownloadStationNonAdmin).
        assert "writable" in result
        assert "your user view" not in result

    async def test_get_schedule_returns_flags_rates_and_grid(
        self, admin_client: Any, _ds_available: bool
    ) -> None:
        from mcp_synology.modules.downloadstation.config import get_schedule

        client, _, _, _ = _unpack(admin_client)
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

    async def test_list_downloads_reports_true_total_and_pages(
        self, nas_client: Any, _clean_queue: None
    ) -> None:
        """#140: v1 Task.list's ``total`` is the page size, not the queue size."""
        from mcp_synology.modules.downloadstation.tasks import create_download, list_downloads

        client, _, _, _ = _unpack(nas_client)
        ids = [
            _ids_in(await create_download(client, uri=_magnet(i), destination="writable"))[0]
            for i in range(3)
        ]

        first = await list_downloads(client, limit=2)
        assert "Download Station queue (all" in first  # not a task's title (#140)
        assert "3 task(s) total; showing 1–2." in first, first
        assert "offset=2" in first
        rest = await list_downloads(client, offset=2, limit=2)
        assert "3 task(s) total; showing 3–3." in rest, rest
        assert sorted(_ids_in(first) + _ids_in(rest)) == sorted(ids)

    async def test_status_filter_looks_past_the_first_page(
        self, nas_client: Any, _clean_queue: None
    ) -> None:
        """#140: the filter used to run over one page only, so a paused task
        past ``limit`` was missed."""
        from mcp_synology.modules.downloadstation.tasks import (
            create_download,
            list_downloads,
            pause_download,
        )

        client, _, _, _ = _unpack(nas_client)
        ids = [
            _ids_in(await create_download(client, uri=_magnet(i), destination="writable"))[0]
            for i in range(3)
        ]
        await pause_download(client, task_ids=[ids[-1]])

        paused = await list_downloads(client, status_filter="paused", limit=1)
        assert _ids_in(paused) == [ids[-1]], paused
        assert "1 task(s) total; showing 1–1." in paused

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

    async def test_http_title_passkey_is_masked(self, nas_client: Any, _clean_queue: None) -> None:
        # QA on #144: DSM titles an HTTP task with the URL's last segment, so a private
        # tracker's .../<passkey>.torrent link put the passkey in the title.
        from mcp_synology.modules.downloadstation.tasks import (
            create_download,
            get_download_info,
            list_downloads,
        )

        client, _, _, _ = _unpack(nas_client)
        passkey = "Zq7Lm2Xc9Vb4Nn8Kk1Jj6Hh3Gg5Ff0Dd"  # synthetic
        url = f"http://127.0.0.1:5000/download/123/{passkey}.torrent"
        task_id = _ids_in(await create_download(client, uri=url, destination="writable"))[0]

        raw = await _task(client, task_id)
        assert passkey in raw["title"], f"DSM no longer titles HTTP tasks from the URL: {raw}"
        info = await get_download_info(client, task_id=task_id)
        listed = await list_downloads(client)
        assert passkey not in info
        assert passkey not in listed
        assert "***.torrent" in info and "***.torrent" in listed

    async def test_sort_by_completed_and_completed_column(
        self, nas_client: Any, _local_source: str
    ) -> None:
        """#145: the finished task sorts ahead of a newer unfinished one, and the
        list shows its completion time."""
        from mcp_synology.modules.downloadstation.tasks import create_download, list_downloads

        client, _, _, _ = _unpack(nas_client)
        finished = _ids_in(
            await create_download(client, uri=_local_source, destination="writable")
        )[0]
        await _wait_for(client, finished, lambda t: t["status"] == "finished")
        pending = _ids_in(await create_download(client, uri=_magnet(7), destination="writable"))[0]

        assert _ids_in(await list_downloads(client)) == [finished, pending]  # queue order
        latest = await list_downloads(client, sort_by="completed", limit=1)
        assert _ids_in(latest) == [finished], latest
        assert "newest completed first" in latest
        row = next(line for line in latest.splitlines() if finished in line)
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", row), row  # Completed column
        assert "2 task(s) total; showing 1–1." in latest

    async def test_delete_keeps_finished_file(self, nas_client: Any, _local_source: str) -> None:
        """#123 bug 7: DSM never deletes a finished task's file."""
        from mcp_synology.modules.downloadstation.tasks import create_download, delete_download

        client, _, _, _ = _unpack(nas_client)
        created = await create_download(client, uri=_local_source, destination="writable")
        task_id = _ids_in(created)[0]
        task = await _wait_for(client, task_id, lambda t: t["status"] == "finished")

        await delete_download(client, task_ids=[task_id])
        names = await _writable_names(client)
        assert task["title"] in names, f"finished file should be kept; /writable has {names}"

    async def test_force_complete_on_finished_task_is_refused(
        self, nas_client: Any, _local_source: str
    ) -> None:
        """#130: DSM refuses force_complete on a finished task (per-task 405).

        The task stays listed, so the result must not count it as removed and
        must say to retry without force_complete, which does delete it.
        """
        from mcp_synology.modules.downloadstation.tasks import create_download, delete_download

        client, _, _, _ = _unpack(nas_client)
        created = await create_download(client, uri=_local_source, destination="writable")
        task_id = _ids_in(created)[0]
        await _wait_for(client, task_id, lambda t: t["status"] == "finished")

        refused = await delete_download(client, task_ids=[task_id], force_complete=True)
        assert "0 of 1 task(s) removed" in refused, refused
        assert "error 405" in refused, refused
        assert "retry them with force_complete=false" in refused, refused
        assert task_id in await _all_task_ids(client)

        retried = await delete_download(client, task_ids=[task_id])
        assert "1 of 1 task(s) removed" in retried, retried
        assert task_id not in await _all_task_ids(client)

    @pytest.mark.parametrize("force_complete", [False, True])
    async def test_delete_unfinished_task_discards_partial_data(
        self, nas_client: Any, _local_source: str, _throttled: None, force_complete: bool
    ) -> None:
        """An unfinished HTTP task's partial data is discarded on delete.

        ``force_complete`` is documented as moving the incomplete file into the
        destination, but DS 3.9 and 4.1 discard it either way, downloading or
        paused (#128). Pinned so a DS version that does move it shows up here.
        """
        from mcp_synology.modules.downloadstation.tasks import create_download, delete_download

        client, _, _, _ = _unpack(nas_client)
        created = await create_download(client, uri=_local_source, destination="writable")
        task_id = _ids_in(created)[0]
        task = await _wait_for(client, task_id, lambda t: _downloaded(t) > 0)
        assert task["status"] != "finished", f"throttle did not hold the task partial: {task}"

        await delete_download(client, task_ids=[task_id], force_complete=force_complete)
        assert task_id not in await _all_task_ids(client)
        await asyncio.sleep(10)  # give DS time for any deferred move
        names = await _writable_names(client)
        assert task["title"] not in names, f"partial data should be discarded: {names}"


class TestVdsmDownloadStationSettings:
    async def test_set_download_config_round_trip(
        self, admin_client: Any, _ds_available: bool
    ) -> None:
        """#123 bug 4: config writes go through setserverconfig."""
        from mcp_synology.modules.downloadstation.config import set_download_config

        client, _, _, _ = _unpack(admin_client)
        before = await client.request("SYNO.DownloadStation.Info", "getconfig", version=1)
        try:
            await set_download_config(client, bt_max_download=123)
            after = await client.request("SYNO.DownloadStation.Info", "getconfig", version=1)
            assert after["bt_max_download"] == 123
        finally:
            await set_download_config(client, bt_max_download=int(before["bt_max_download"]))

    async def test_set_schedule_plan_and_rates_round_trip(
        self, admin_client: Any, _ds_available: bool
    ) -> None:
        """#123 bugs 2/3: the plan is read from and written to DS2."""
        from mcp_synology.modules.downloadstation.config import get_schedule, set_schedule

        client, _, _, _ = _unpack(admin_client)
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


def _error(exc: ToolError) -> dict[str, Any]:
    return json.loads(str(exc))["error"]  # type: ignore[no-any-return]


class TestVdsmDownloadStationNonAdmin:
    """What the bake's non-admin test user sees (#124), pinned on DS 3.9 and 4.1.

    ``nas_client`` is that user; ``admin_client`` checks NAS-wide state; the
    re-auth spy asserts DSM's 105s never trigger a session re-auth.
    """

    async def test_get_download_config_is_the_users_own_view(
        self, nas_client: Any, nas_persona: Persona, _ds_available: bool
    ) -> None:
        from mcp_synology.modules.downloadstation.config import get_download_config

        assert nas_persona.is_admin is False
        client, _, _, _ = _unpack(nas_client)
        result = await get_download_config(client)
        assert "your user view" in result
        assert "writable" in result  # the bake set the test user's own default

    async def test_get_schedule_degrades_without_reauth(
        self, nas_client: Any, reauth_spy: ReauthSpy, _ds_available: bool
    ) -> None:
        """DS2 Scheduler.get answers 105: flags still shown, plan marked unavailable."""
        from mcp_synology.modules.downloadstation.config import get_schedule

        client, _, _, _ = _unpack(nas_client)
        result = await get_schedule(client)
        assert "Enabled" in result
        assert "weekly plan not available" in result
        assert "Limited download rate" not in result
        reauth_spy.assert_105_never_reauthed()

    async def test_set_schedule_plan_is_refused_with_nothing_applied(
        self, nas_client: Any, admin_client: Any, reauth_spy: ReauthSpy, _ds_available: bool
    ) -> None:
        from mcp_synology.modules.downloadstation.config import set_schedule

        user, _, _, _ = _unpack(nas_client)
        admin, _, _, _ = _unpack(admin_client)
        scheduler = "SYNO.DownloadStation2.Settings.Scheduler"
        before = await admin.request(scheduler, "get", version=1)
        with pytest.raises(ToolError) as exc:
            await set_schedule(user, schedule_plan="0" * 168, download_rate=1)
        error = _error(exc.value)
        assert error["code"] == "permission_denied"
        assert "nothing was applied" in error["message"]
        assert await admin.request(scheduler, "get", version=1) == before
        reauth_spy.assert_105_never_reauthed()

    async def test_schedule_enabled_flag_is_nas_wide(
        self, nas_client: Any, admin_client: Any, _ds_available: bool
    ) -> None:
        """v1 Schedule.setconfig works for the test user and flips the NAS-wide flag."""
        from mcp_synology.modules.downloadstation.config import set_schedule

        user, _, _, _ = _unpack(nas_client)
        admin, _, _, _ = _unpack(admin_client)
        flags = "SYNO.DownloadStation.Schedule"
        before = bool((await admin.request(flags, "getconfig", version=1))["enabled"])
        try:
            await set_schedule(user, enabled=not before)
            after = await admin.request(flags, "getconfig", version=1)
            assert bool(after["enabled"]) is (not before)
        finally:
            await set_schedule(admin, enabled=before)

    async def test_set_download_config_rates_are_refused(
        self, nas_client: Any, admin_client: Any, reauth_spy: ReauthSpy, _ds_available: bool
    ) -> None:
        """#123 bug 12: DSM ignores a non-manager's rate writes, so nothing is sent."""
        from mcp_synology.modules.downloadstation.config import set_download_config

        user, _, _, _ = _unpack(nas_client)
        admin, _, _, _ = _unpack(admin_client)
        info = "SYNO.DownloadStation.Info"
        before = await admin.request(info, "getconfig", version=1)
        with pytest.raises(ToolError) as exc:
            await set_download_config(user, bt_max_download=321)
        error = _error(exc.value)
        assert error["code"] == "permission_denied"
        assert "bt_max_download" in error["message"]
        assert await admin.request(info, "getconfig", version=1) == before
        reauth_spy.assert_105_never_reauthed()

    async def test_set_download_config_destination_is_per_user(
        self, nas_client: Any, admin_client: Any, _ds_available: bool
    ) -> None:
        """#124: DSM applies a non-manager's default_destination to that user only."""
        from mcp_synology.modules.downloadstation.config import set_download_config

        user, _, _, _ = _unpack(nas_client)
        admin, _, _, _ = _unpack(admin_client)
        info = "SYNO.DownloadStation.Info"
        try:
            result = await set_download_config(user, default_destination="testshare")
            assert "your own default destination" in result
            mine = await user.request(info, "getconfig", version=1)
            assert mine["default_destination"] == "testshare"
            nas_wide = await admin.request(info, "getconfig", version=1)
            assert nas_wide["default_destination"] == "writable"
        finally:
            await set_download_config(user, default_destination="writable")

    async def test_list_downloads_shows_only_own_tasks(
        self, nas_client: Any, admin_client: Any, _clean_queue: None
    ) -> None:
        from mcp_synology.modules.downloadstation.tasks import create_download, list_downloads

        user, _, _, _ = _unpack(nas_client)
        admin, _, _, _ = _unpack(admin_client)
        admin_task = _ids_in(await create_download(admin, uri=_FAKE_MAGNET))[0]
        try:
            assert admin_task not in await list_downloads(user)
            assert admin_task in await list_downloads(admin)
        finally:
            await admin.request(
                "SYNO.DownloadStation.Task",
                "delete",
                version=1,
                params={"id": admin_task, "force_complete": "false"},
            )

    async def test_list_downloads_total_counts_only_own_tasks(
        self, nas_client: Any, admin_client: Any, _clean_queue: None
    ) -> None:
        """#140: the total (from DS2 when present) must be scoped like v1's
        list: a non-manager's own tasks, not the whole queue."""
        from mcp_synology.modules.downloadstation.tasks import create_download, list_downloads

        user, _, _, _ = _unpack(nas_client)
        admin, _, _, _ = _unpack(admin_client)
        admin_tasks = [
            _ids_in(await create_download(admin, uri=_magnet(i), destination="writable"))[0]
            for i in range(2)
        ]
        try:
            mine = _ids_in(await create_download(user, uri=_magnet(9), destination="writable"))
            # DSM itself: DS2's total is scoped to the caller, like v1's list.
            ds2 = "SYNO.DownloadStation2.Task"
            page = {"offset": "0", "limit": "1"}
            assert (await user.request(ds2, "list", version=2, params=page))["total"] == 1
            everyone = len(await _all_task_ids(admin))
            assert everyone >= 3
            assert (await admin.request(ds2, "list", version=2, params=page))["total"] == everyone
            # limit=1 makes each page full, so the total comes from DS2.
            listed = await list_downloads(user, limit=1)
            assert _ids_in(listed) == mine
            assert "1 task(s) total; showing 1–1." in listed, listed
            assert f"{everyone} task(s) total; showing 1–1." in await list_downloads(admin, limit=1)
        finally:
            await admin.request(
                "SYNO.DownloadStation.Task",
                "delete",
                version=1,
                params={"id": ",".join(admin_tasks), "force_complete": "false"},
            )

    async def test_another_users_task_reads_as_not_found(
        self, nas_client: Any, admin_client: Any, _clean_queue: None
    ) -> None:
        """#136: DSM answers 544 (resume/edit/delete) and 101 (pause, no match)
        for another user's task, exactly as for a missing ID; the tools say
        "not found (no such task, or not yours)" instead of a bare code."""
        from mcp_synology.modules.downloadstation.tasks import (
            create_download,
            delete_download,
            edit_download,
            pause_download,
            resume_download,
        )

        user, _, _, _ = _unpack(nas_client)
        admin, _, _, _ = _unpack(admin_client)
        magnet = "magnet:?xt=urn:btih:" + "e" * 40 + "&dn=admins-task"
        theirs = _ids_in(await create_download(admin, uri=magnet, destination="writable"))[0]
        try:
            for result in (
                await resume_download(user, task_ids=[theirs]),
                await edit_download(user, task_ids=[theirs], destination="writable"),
                await delete_download(user, task_ids=[theirs]),
            ):
                assert "not found (no such task, or not yours)" in result, result
            assert "nothing was deleted" in await delete_download(user, task_ids=[theirs])
            with pytest.raises(ToolError) as exc:
                await pause_download(user, task_ids=[theirs])
            assert _error(exc.value)["code"] == "not_found"
            assert theirs in await _all_task_ids(admin)  # untouched
        finally:
            await admin.request(
                "SYNO.DownloadStation.Task",
                "delete",
                version=1,
                params={"id": theirs, "force_complete": "false"},
            )

    async def test_create_without_destination_uses_own_default(
        self, nas_client: Any, _local_source: str
    ) -> None:
        """#133: DS2 create answers 120 to any user who omits destination;
        create_download fills in the user's own default instead."""
        from mcp_synology.modules.downloadstation.tasks import create_download

        client, _, _, _ = _unpack(nas_client)
        created = await create_download(client, uri=_local_source)
        task_id = _ids_in(created)[0]
        assert "won't start" not in created
        await _wait_for(client, task_id, lambda t: t["status"] == "finished")
        assert "ds-source.bin" in await _writable_names(client)


class TestVdsmDownloadStationUnconfiguredUser:
    """A DS-granted non-admin that never set a default destination (#124).

    DS queues such a user's tasks but never starts them, and with no default
    to fill in, create_download refuses a create that omits ``destination``.
    """

    async def test_create_without_destination_is_refused_and_creates_nothing(
        self, fresh_client: Any, _ds_available: bool
    ) -> None:
        from mcp_synology.modules.downloadstation.tasks import create_download

        client, _, _, _ = _unpack(fresh_client)
        with pytest.raises(ToolError) as exc:
            await create_download(client, uri=_FAKE_MAGNET)
        error = _error(exc.value)
        assert error["code"] == "invalid_parameter"
        assert "set_download_config" in error["suggestion"]
        assert await _all_task_ids(client) == []

    async def test_create_with_destination_says_it_wont_start(
        self, fresh_client: Any, _local_source: str
    ) -> None:
        """The NAS-served source finishes in about 2 s for a configured user, so
        still ``waiting`` after 10 s is unambiguous (a magnet could be slow anyway)."""
        from mcp_synology.modules.downloadstation.tasks import create_download

        client, _, _, _ = _unpack(fresh_client)
        created = await create_download(client, uri=_local_source, destination="writable")
        task_ids = _ids_in(created)
        try:
            assert "won't start" in created
            assert "set_download_config(default_destination=" in created
            await asyncio.sleep(10)
            assert (await _task(client, task_ids[0]))["status"] == "waiting"
        finally:
            await client.request(
                "SYNO.DownloadStation.Task",
                "delete",
                version=1,
                params={"id": ",".join(task_ids), "force_complete": "false"},
            )
