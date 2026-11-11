"""The batch runner's lifecycle on virtual-dsm (#512 PR 2).

- **One wait per call (#496):** a delete of three paths, the second a tree that takes far longer
  than the wait. The first is deleted, the second handed off with its handle when the call's
  time is up, and the third is listed as not started; nothing is stopped.
- **A cancel during a start (#394 C3):** the call is canceled while DSM's answer to a delete's
  start is held back. The answer is awaited anyway, and the task it names is stopped, so no task
  of this user is left running that nothing knows of.

The tree is 300,000 empty files, as in ``test_vdsm_task_handles.py``. Run on every DSM version CI
runs (``--dsm-version``; see CLAUDE.md).

Run with: uv run pytest -m vdsm -v tests/vdsm/test_vdsm_batch.py
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from mcp_synology.modules.filestation.batch import MIN_ITEM_WAIT
from mcp_synology.modules.filestation.operations import delete_files
from mcp_synology.modules.filestation.tasks import TaskRecords, _list_running
from tests.vdsm.test_vdsm_task_handles import (
    WAIT,
    Nas,
    _admin,
    _handle,
    _stopped_at_exit,
    nas,  # noqa: F401 - the fixture
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

pytestmark = pytest.mark.vdsm

# Seconds a call may take beyond its wait: the checks before the first start (getinfo of each
# path), the starts and the last status read, on a busy VM.
SLACK = 15.0


async def _running_on(client: DsmClient, path: str) -> list[str]:
    """The handles of this user's running tasks on ``path``."""
    return [str(t.handle) for t in await _list_running(client) if path in t.paths]


async def test_a_delete_of_three_paths_shares_one_wait(admin_client: Any, nas: Nas) -> None:  # noqa: F811
    client = _admin(admin_client)
    first = nas.folder("/writable/b512a")
    tree = nas.tree("/writable/b512b")
    last = nas.folder("/writable/b512c")
    records = TaskRecords()

    async with _stopped_at_exit(client, records) as handles:
        started = time.monotonic()
        reply = await delete_files(
            client,
            paths=[first, tree, last],  # type: ignore[list-item]
            recycle_bin_status={},
            timeout=WAIT,
            records=records,
        )
        took = time.monotonic() - started
        handle = _handle(reply)
        handles.append(handle)
        assert f"Still deleting on the NAS: {tree} (" in reply, reply
        assert f"Not started: {last}." in reply, reply
        assert took < WAIT + MIN_ITEM_WAIT + SLACK, took
        # Handed off, not stopped: it's still running.
        assert handle in await _running_on(client, tree)

    assert not nas.exists(first)
    assert nas.exists(last)


async def test_a_call_canceled_during_a_start_stops_the_task_it_started(
    admin_client: Any,
    nas: Nas,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _admin(admin_client)
    tree = nas.tree("/writable/b512d")
    records = TaskRecords()
    real = client.request
    holding = asyncio.Event()

    async def held(api: str, method: str, *args: Any, **kwargs: Any) -> Any:
        result = await real(api, method, *args, **kwargs)
        if api == "SYNO.FileStation.Delete" and method == "start" and not holding.is_set():
            # DSM has started the task; its answer reaches the call only after the cancel.
            holding.set()
            await asyncio.sleep(5)
        return result

    monkeypatch.setattr(client, "request", held)

    async def run() -> None:
        await delete_files(
            client,
            paths=[tree],  # type: ignore[list-item]
            recycle_bin_status={},
            timeout=WAIT,
            records=records,
        )

    with anyio.fail_after(120):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run)
            await holding.wait()
            tg.cancel_scope.cancel()
    monkeypatch.setattr(client, "request", real)

    # The task it started was stopped (DSM 7.1 can take about a minute to act on a stop, #630).
    deadline = time.monotonic() + 90
    while running := await _running_on(client, tree):
        assert time.monotonic() < deadline, f"still running after the cancel: {running}"
        await asyncio.sleep(2)
    assert nas.exists(tree)  # stopped partway, not run to the end
