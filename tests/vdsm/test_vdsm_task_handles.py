"""Task handoff, follow-up, stop and list on virtual-dsm (#245).

A delete, copy, move or restore that outlives its wait is handed off: it keeps
running on the NAS and the reply carries a handle. ``file_task`` lists this
user's running tasks, follows one up (from any session of the same user) and
stops it. A new server process seeds its records from the running tasks at its
first connection (``discover_running_tasks``).

The trees are 300,000 empty files (600 dirs x 500), built over SSH; on vdsm
they delete in about 14 s and copy in about 45 s, so a 2 s wait always hands
off. Run on DSM 7.2.2 and 7.1 (``--dsm-version 7.1``).

Run with: uv run pytest -m vdsm -v tests/vdsm/test_vdsm_task_handles.py
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import TYPE_CHECKING, Any

import pytest

from mcp_synology.core.auth import AuthManager
from mcp_synology.core.client import DsmClient
from mcp_synology.modules.filestation.operations import (
    copy_files,
    delete_files,
    move_files,
    restore_from_recycle_bin,
)
from mcp_synology.modules.filestation.tasks import (
    TaskRecords,
    discover_running_tasks,
    file_task,
)
from tests.vdsm.conftest import _ssh
from tests.vdsm.golden_image import load_golden_meta

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from mcp_synology.core.config import AppConfig
    from tests.personas import ReauthSpy
    from tests.vdsm.container import VirtualDsmContainer

pytestmark = pytest.mark.vdsm

DIRS, PER_DIR = 600, 500
TREE_FILES = DIRS * PER_DIR
WAIT = 2  # seconds before a handoff; every tree here takes far longer
FINISH_WITHIN = 300  # seconds a handed-off task gets to finish
_HANDLE = re.compile(r"Handle: `((?:delete|copy|move|restore):FileStation_\w+)`")
_NO_RUNNING = "No running task with this handle."
_ALREADY_FINISHED = "That task had already finished or isn't running."


def _handle(reply: str) -> str:
    match = _HANDLE.search(reply)
    assert match, f"no handle in reply:\n{reply}"
    return match.group(1)


class Nas:
    """SSH access to the guest's volume, as the golden image's admin."""

    def __init__(self, container: VirtualDsmContainer, password: str) -> None:
        self.container = container
        self.password = password
        self.created: list[str] = []

    def run(self, cmd: str, *, timeout: int = 60, sudo: bool = True) -> str:
        rc, out = _ssh(self.container, self.password, f"sh -c '{cmd}'", timeout, sudo=sudo)
        assert rc == 0, f"ssh {cmd!r} failed (rc={rc}): {out[-500:]}"
        return out

    def tree(self, share_path: str, dirs: int = DIRS, per_dir: int = PER_DIR) -> str:
        """Build ``dirs`` x ``per_dir`` empty files at ``share_path`` (e.g. /writable/x)."""
        vol = f"/volume1{share_path}"
        self.created.append(vol)
        self.run(
            f"mkdir -p {vol} && cd {vol} && for d in \\$(seq 1 {dirs}); do mkdir d\\$d; "
            f"(cd d\\$d && seq 1 {per_dir} | xargs touch); done; chmod -R 777 {vol}",
            timeout=600,
        )
        return share_path

    def folder(self, share_path: str) -> str:
        vol = f"/volume1{share_path}"
        self.created.append(vol)
        self.run(f"mkdir -p {vol} && chmod 777 {vol}")
        return share_path

    def count(self, share_path: str) -> int:
        out = self.run(
            f"find /volume1{share_path} -type f 2>/dev/null | wc -l", timeout=180, sudo=False
        )
        return int(out.splitlines()[-1].strip())

    def exists(self, share_path: str) -> bool:
        out = self.run(
            f"test -e /volume1{share_path} && echo yes || echo no", sudo=False
        ).splitlines()
        return out[-1].strip() == "yes"

    def cleanup(self) -> None:
        if self.created:
            _ssh(self.container, self.password, f"rm -rf {' '.join(self.created)}", 600)


@pytest.fixture
def nas(vdsm_container: VirtualDsmContainer, dsm_version: str) -> Any:
    meta = load_golden_meta(dsm_version)
    helper = Nas(vdsm_container, str(meta.get("admin_password", "")))
    try:
        yield helper
    finally:
        helper.cleanup()


@pytest.fixture
def admin_session(
    vdsm_config: tuple[AppConfig, dict[str, str]], dsm_version: str
) -> Callable[[], contextlib.AbstractAsyncContextManager[DsmClient]]:
    """Open another, separately logged-in admin session (a second client of one user)."""
    config, _ = vdsm_config
    meta = load_golden_meta(dsm_version)
    admin = config.model_copy(
        update={
            "instance_id": f"vdsm-admin2-{dsm_version.replace('.', '-')}",
            "auth": config.auth.model_copy(
                update={
                    "username": meta.get("admin_user", "admin"),
                    "password": meta.get("admin_password", ""),
                }
            ),
        }
    )

    @contextlib.asynccontextmanager
    async def session() -> AsyncIterator[DsmClient]:
        conn = admin.connection
        assert conn is not None
        client = DsmClient(base_url=f"http://{conn.host}:{conn.port}", verify_ssl=False, timeout=30)
        async with client:
            await client.query_api_info()
            auth = AuthManager(admin, client)
            await auth.login()
            try:
                yield client
            finally:
                await auth.logout()

    return session


@contextlib.asynccontextmanager
async def _stopped_at_exit(client: DsmClient, records: TaskRecords) -> AsyncIterator[list[str]]:
    """Stop every handle appended to the yielded list when the test ends (no orphans)."""
    handles: list[str] = []
    try:
        yield handles
    finally:
        for handle in handles:
            try:
                reply = await file_task(client, records=records, handle=handle, action="stop")
            except Exception as e:
                reply = f"{type(e).__name__}: {e}"
            print(f"cleanup stop {handle}: {reply}")
        # Fail here, not in a later test, if a task outlives its test.
        deadline = time.monotonic() + 30
        while handles:
            listing = await file_task(client, records=records)
            if not any(f"`{h}`" in listing for h in handles):
                break
            assert time.monotonic() < deadline, f"still running after stop:\n{listing}"
            await asyncio.sleep(2)


async def _until_finished(
    check: Callable[[], Awaitable[str]], *, still: str, within: float = FINISH_WITHIN
) -> tuple[str, list[str]]:
    """Call ``check`` every 2 s until its reply no longer starts with ``still``."""
    deadline = time.monotonic() + within
    seen: list[str] = []
    reply = await check()
    while reply.startswith(still):
        seen.append(reply)
        assert time.monotonic() < deadline, f"not finished after {within}s:\n{reply}"
        await asyncio.sleep(2)
        reply = await check()
    return reply, seen


def _admin(admin_client: Any) -> DsmClient:
    client: DsmClient = admin_client[0]
    return client


# --- Delete ---------------------------------------------------------------


async def test_delete_handoff_then_status_until_finished(
    admin_client: Any, admin_session: Any, nas: Nas
) -> None:
    client = _admin(admin_client)
    path = nas.tree("/writable/del245a")
    records = TaskRecords()

    reply = await delete_files(
        client, paths=[path], recycle_bin_status={}, timeout=WAIT, records=records
    )
    assert reply.startswith(f"Still deleting on the NAS: {path} ("), reply
    handle = _handle(reply)
    assert handle.startswith("delete:")

    async with admin_session() as other:
        first = await file_task(other, records=records, handle=handle)
        assert first.startswith(f"Still deleting on the NAS: {path} ("), first
        assert f"Handle: `{handle}`" in first

        final, _ = await _until_finished(
            lambda: file_task(other, records=records, handle=handle),
            still="Still deleting",
        )
        assert "Permanently deleted 1 item(s):" in final, final
        assert path in final
        # DSM forgets the task after its final status; the record keeps the result.
        again = await file_task(other, records=records, handle=handle)
        assert again == final

    assert not nas.exists(path)


async def test_delete_handoff_then_stop(admin_client: Any, admin_session: Any, nas: Nas) -> None:
    client = _admin(admin_client)
    path = nas.tree("/writable/del245b")
    records = TaskRecords()

    reply = await delete_files(
        client, paths=[path], recycle_bin_status={}, timeout=WAIT, records=records
    )
    handle = _handle(reply)
    # Stop once files are going (a stop while DSM still counts deletes nothing).
    deadline = time.monotonic() + 60
    while nas.count(path) == TREE_FILES:
        assert time.monotonic() < deadline, "the delete never started removing files"
        await asyncio.sleep(0.5)

    async with admin_session() as other:
        stopped = await file_task(other, records=records, handle=handle, action="stop")
        assert stopped.startswith(f"Stopped delete of {path}; it may be partly done."), stopped
        left = nas.count(path)
        await asyncio.sleep(10)
        assert nas.count(path) == left
        assert 0 < left < TREE_FILES

        after = await file_task(other, records=records, handle=handle)
        assert after.startswith(_NO_RUNNING), after
        again = await file_task(other, records=records, handle=handle, action="stop")
        assert again.startswith(_ALREADY_FINISHED), again


async def test_file_task_list_shows_running_delete(admin_client: Any, nas: Nas) -> None:
    client = _admin(admin_client)
    path = nas.tree("/writable/del245c")
    records = TaskRecords()

    async with _stopped_at_exit(client, records) as handles:
        reply = await delete_files(
            client, paths=[path], recycle_bin_status={}, timeout=WAIT, records=records
        )
        handle = _handle(reply)
        handles.append(handle)

        listing = await file_task(client, records=records)
        assert "Running File Station tasks (1)" in listing, listing
        assert f"`{handle}`" in listing
        assert path in listing
        assert "delete" in listing

        # A second delete of the same path starts nothing and returns the same handle.
        second = await delete_files(
            client, paths=[path], recycle_bin_status={}, timeout=WAIT, records=records
        )
        assert second.startswith(
            f"A delete of {path} was already running on the NAS, so no second one was started."
        ), second
        assert _handle(second) == handle

    listing = await file_task(client, records=records)
    assert listing.startswith("No File Station delete, copy or move tasks are running"), listing


async def test_other_user_cannot_see_handle(
    admin_client: Any, nas_client: Any, reauth_spy: ReauthSpy, nas: Nas
) -> None:
    client = _admin(admin_client)
    user_client: DsmClient = nas_client[0]
    path = nas.tree("/writable/del245d")
    records = TaskRecords()

    async with _stopped_at_exit(client, records) as handles:
        reply = await delete_files(
            client, paths=[path], recycle_bin_status={}, timeout=WAIT, records=records
        )
        handle = _handle(reply)
        handles.append(handle)

        theirs = TaskRecords()
        status = await file_task(user_client, records=theirs, handle=handle)
        assert status.startswith(_NO_RUNNING), status
        assert "started by a different DSM user" in status
        stop = await file_task(user_client, records=theirs, handle=handle, action="stop")
        assert stop.startswith(_ALREADY_FINISHED), stop
        listing = await file_task(user_client, records=theirs)
        assert listing.startswith("No File Station delete, copy or move tasks are running")
        assert reauth_spy.reauths == 0, reauth_spy.codes

        # The other user's stop didn't touch it: still running for its owner.
        mine = await file_task(client, records=records, handle=handle)
        assert mine.startswith("Still deleting on the NAS"), mine


async def test_discovery_seeds_records_in_a_new_session(
    admin_client: Any, admin_session: Any, nas: Nas
) -> None:
    """A restarted server (new session, empty records) still gives the full result."""
    client = _admin(admin_client)
    path = nas.tree("/writable/del245e")

    reply = await delete_files(
        client, paths=[path], recycle_bin_status={}, timeout=WAIT, records=TaskRecords()
    )
    handle = _handle(reply)

    async with admin_session() as other:
        records = TaskRecords()
        note = await discover_running_tasks(other, records)
        assert note is not None
        assert note.startswith("Note: 1 operation(s) still running on the NAS:"), note
        assert f"deleting {path} (" in note
        assert f"handle `{handle}`" in note

        final, _ = await _until_finished(
            lambda: file_task(other, records=records, handle=handle),
            still="Still deleting",
        )
        assert "Permanently deleted 1 item(s):" in final, final
        assert path in final
        assert await discover_running_tasks(other, TaskRecords()) is None


# --- Copy -----------------------------------------------------------------


async def test_copy_handoff_then_status_until_finished(
    admin_client: Any, admin_session: Any, nas_client: Any, nas: Nas
) -> None:
    client = _admin(admin_client)
    user_client: DsmClient = nas_client[0]
    src = nas.tree("/writable/cp245a")
    dest = nas.folder("/writable/cp245a_dst")
    records = TaskRecords()

    async with _stopped_at_exit(client, records) as handles:
        reply = await copy_files(
            client, paths=[src], dest_folder=dest, timeout=WAIT, records=records
        )
        assert reply.startswith(f"Still copying on the NAS: {src} ("), reply
        handle = _handle(reply)
        handles.append(handle)
        assert handle.startswith("copy:")

        async with admin_session() as other:
            listing = await file_task(other, records=records)
            assert f"`{handle}`" in listing
            assert f"{src} -> {dest}/" in listing

            # Another user's stop gets CopyMove's blanket success, but stops nothing:
            # the copy still finishes in full below.
            theirs = await file_task(user_client, records=TaskRecords(), handle=handle)
            assert theirs.startswith(_NO_RUNNING), theirs
            stop = await file_task(user_client, records=TaskRecords(), handle=handle, action="stop")
            assert stop.startswith(f"Stopped copy of `{handle}`"), stop

            final, seen = await _until_finished(
                lambda: file_task(other, records=records, handle=handle),
                still="Still copying",
            )
            assert seen, "the copy finished before the first follow-up"
            assert f"Copied 1 item(s) to {dest}/:" in final, final

    assert nas.count(f"{dest}/cp245a") == TREE_FILES
    assert nas.count(src) == TREE_FILES


async def test_copy_handoff_then_stop(admin_client: Any, admin_session: Any, nas: Nas) -> None:
    client = _admin(admin_client)
    src = nas.tree("/writable/cp245b")
    dest = nas.folder("/writable/cp245b_dst")
    records = TaskRecords()

    reply = await copy_files(client, paths=[src], dest_folder=dest, timeout=WAIT, records=records)
    handle = _handle(reply)

    async with admin_session() as other:
        stopped = await file_task(other, records=records, handle=handle, action="stop")
        assert stopped.startswith(f"Stopped copy of {src}; it may be partly done."), stopped
        copied = nas.count(dest)
        await asyncio.sleep(10)
        assert nas.count(dest) == copied
        assert copied < TREE_FILES
        after = await file_task(other, records=records, handle=handle)
        assert after.startswith(_NO_RUNNING), after


async def test_copymove_stop_answers_success_for_any_taskid(admin_client: Any, nas: Nas) -> None:
    """CopyMove ``stop`` answers success for a task that already finished, read or not.

    Unlike Delete (401 for a task it no longer has, #162), DSM 7.1 and 7.2.2 answer
    CopyMove ``stop`` with success for a finished task whose final status nobody read,
    for one it already forgot, and for a made-up task ID. So file_task can't tell a
    stopped copy from one that had finished: it says "Stopped ... may be partly done".
    """
    client = _admin(admin_client)
    src = nas.tree("/writable/cp245c", dirs=2, per_dir=5)
    dest = nas.folder("/writable/cp245c_dst")
    copymove = "SYNO.FileStation.CopyMove"

    async def start() -> str:
        data = await client.request(
            copymove,
            "start",
            version=2,
            params={
                "path": src,
                "dest_folder_path": dest,
                "overwrite": "true",
                "remove_src": "false",
            },
        )
        return str(data["taskid"])

    async def finished_unread(taskid: str) -> None:
        deadline = time.monotonic() + 30
        while True:
            listed = await client.request("SYNO.FileStation.BackgroundTask", "list", version=2)
            task = next((t for t in listed["tasks"] if t.get("taskid") == taskid), None)
            assert task is not None, listed
            if task.get("finished"):
                return
            assert time.monotonic() < deadline, f"the small copy never finished: {task}"
            await asyncio.sleep(1)

    records = TaskRecords()
    # Finished, final status not read yet.
    taskid = await start()
    await finished_unread(taskid)
    stop = await file_task(client, records=records, handle=f"copy:{taskid}", action="stop")
    assert stop.startswith(f"Stopped copy of `copy:{taskid}`; it may be partly done."), stop
    status = await file_task(client, records=records, handle=f"copy:{taskid}")
    assert status.startswith(_NO_RUNNING), status
    assert nas.count(f"{dest}/cp245c") == 10

    # Finished and its final status read (DSM then forgets it: 599).
    taskid = await start()
    await finished_unread(taskid)
    final = await file_task(client, records=records, handle=f"copy:{taskid}")
    assert final.startswith("Copy finished"), final
    stop = await file_task(client, records=records, handle=f"copy:{taskid}", action="stop")
    assert stop.startswith(f"Stopped copy of `copy:{taskid}`"), stop

    # A task ID DSM never issued: CopyMove says success, Delete says 401.
    made_up = "FileStation_00000000DEADBEEF"
    stop = await file_task(client, records=records, handle=f"copy:{made_up}", action="stop")
    assert stop.startswith(f"Stopped copy of `copy:{made_up}`"), stop
    stop = await file_task(client, records=records, handle=f"delete:{made_up}", action="stop")
    assert stop.startswith(_ALREADY_FINISHED), stop


# --- Move and restore -----------------------------------------------------


async def test_move_handoff_then_status_until_finished(
    admin_client: Any, admin_session: Any, nas: Nas
) -> None:
    """A move to another share copies across btrfs subvolumes, so it takes a while."""
    client = _admin(admin_client)
    src = nas.tree("/writable/mv245")
    nas.created.append("/volume1/testshare/mv245")
    dest = "/testshare"
    records = TaskRecords()

    async with _stopped_at_exit(client, records) as handles:
        reply = await move_files(
            client, paths=[src], dest_folder=dest, timeout=WAIT, records=records
        )
        assert reply.startswith(f"Still moving on the NAS: {src} ("), reply
        handle = _handle(reply)
        handles.append(handle)
        assert handle.startswith("move:")

        async with admin_session() as other:
            listing = await file_task(other, records=records)
            assert f"`{handle}`" in listing
            assert "move" in listing

            final, _ = await _until_finished(
                lambda: file_task(other, records=records, handle=handle),
                still="Still moving",
            )
            assert f"Moved 1 item(s) to {dest}/:" in final, final
            assert "Source files have been removed from /writable/." in final

    assert not nas.exists(src)
    assert nas.count("/testshare/mv245") == TREE_FILES


async def test_restore_handoff_then_status_until_finished(
    admin_client: Any, admin_session: Any, nas: Nas
) -> None:
    """A restore to another share; the item sits in /writable/#recycle (made over SSH)."""
    client = _admin(admin_client)
    nas.tree("/writable/#recycle/rs245")
    nas.created.append("/volume1/testshare/rs245")
    dest = "/testshare"
    records = TaskRecords()

    async with _stopped_at_exit(client, records) as handles:
        reply = await restore_from_recycle_bin(
            client,
            share="writable",
            paths=["rs245"],
            dest_folder=dest,
            timeout=WAIT,
            records=records,
        )
        assert reply.startswith("Still restoring on the NAS: /writable/#recycle/rs245 ("), reply
        handle = _handle(reply)
        handles.append(handle)
        assert handle.startswith("restore:")

        async with admin_session() as other:
            listing = await file_task(other, records=records)
            assert f"`{handle}`" in listing
            assert "restore" in listing

            final, _ = await _until_finished(
                lambda: file_task(other, records=records, handle=handle),
                still="Still restoring",
            )
            assert "Restored from /writable recycle bin: 1 item(s)" in final, final

    assert not nas.exists("/writable/#recycle/rs245")
    assert nas.count("/testshare/rs245") == TREE_FILES
