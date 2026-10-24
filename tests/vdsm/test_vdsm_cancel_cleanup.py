"""A canceled search_files or get_dir_size stops its task on virtual-dsm.

The MCP SDK cancels a call through a level-triggered anyio scope. Without a shield, the
cleanup's stop and clean requests were canceled at their first await, so a canceled call
left its Search or DirSize task running on the NAS. Each test starts a real task and
cancels the call while its first poll is in flight, so the task exists on DSM and its
result has not been read. Then it asks DSM about the task: an unknown (cleaned) Search task
answers ``list`` with a bare ``{"finished": true}``, and an unknown (stopped) DirSize task
answers ``status`` with error 599.

Run with: uv run pytest -m vdsm -v tests/vdsm/test_vdsm_cancel_cleanup.py
"""

from __future__ import annotations

import asyncio
from typing import Any

import anyio
import pytest

from mcp_synology.core.errors import SynologyError

pytestmark = pytest.mark.vdsm


async def _cancel_during_first_poll(
    module: Any, poll_method: str, run: Any, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Run ``run()``, hold its first ``poll_method`` request, and cancel it like the SDK.

    Returns the task IDs the call used. The held poll never reaches DSM, so the task is
    still there for the cleanup to stop, however fast DSM would have finished it.
    """
    seen: list[str] = []
    polling = asyncio.Event()
    real = module.task_request

    async def spy(client: Any, api: str, method: str, taskid: str, **kwargs: Any) -> Any:
        seen.append(taskid)
        if method == poll_method and not polling.is_set():
            polling.set()
            await anyio.sleep_forever()
        return await real(client, api, method, taskid, **kwargs)

    monkeypatch.setattr(module, "task_request", spy)
    with anyio.fail_after(120):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run)
            await polling.wait()
            tg.cancel_scope.cancel()
    monkeypatch.setattr(module, "task_request", real)
    assert seen, "the call never polled its task"
    return seen


async def test_canceled_search_is_stopped_and_cleaned(
    nas_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_synology.modules.filestation import search
    from mcp_synology.modules.filestation.helpers import task_request

    client = nas_client[0]

    async def run() -> None:
        await search.search_files(client, folder_path="/testshare", pattern="*")

    seen = await _cancel_during_first_poll(search, "list", run, monkeypatch)
    reply = await task_request(client, "SYNO.FileStation.Search", "list", seen[0], version=2)
    # A task that wasn't cleaned (running or finished) answers with total and files.
    assert "total" not in reply and "files" not in reply, reply


async def test_canceled_dir_size_is_stopped(
    nas_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_synology.modules.filestation import metadata
    from mcp_synology.modules.filestation.helpers import task_request

    client = nas_client[0]

    async def run() -> None:
        await metadata.get_dir_size(client, path="/testshare")

    seen = await _cancel_during_first_poll(metadata, "status", run, monkeypatch)
    with pytest.raises(SynologyError) as exc:
        await task_request(client, "SYNO.FileStation.DirSize", "status", seen[0])
    assert exc.value.code == 599
