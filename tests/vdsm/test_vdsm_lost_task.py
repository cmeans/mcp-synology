"""A Delete whose status keeps answering 599, on real replies (#521).

DSM's spontaneous 599 can't be forced, so status reads go to a task ID DSM has never seen, which
gets its real 599. Everything else is the real call.

Run: ``uv run pytest tests/vdsm -m vdsm --no-cov --dsm-version 7.2.2 -k lost_task``
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.filestation.operations import create_folder, delete_files
from mcp_synology.modules.filestation.tasks import TaskRecords

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mcp_synology.core.client import DsmClient

pytestmark = pytest.mark.vdsm


def _lost_status(client: DsmClient) -> Any:
    real = client.request

    async def request(api: str, method: str, **kwargs: Any) -> Any:
        if method == "status" and api == "SYNO.FileStation.Delete":
            kwargs["params"] = {**kwargs["params"], "taskid": '"FileStation_NeverStarted"'}
        return await real(api, method, **kwargs)

    return patch.object(client, "request", request)


@pytest.fixture
async def top(nas_client: Any) -> AsyncIterator[tuple[DsmClient, str]]:
    client: DsmClient = nas_client[0]
    folder = f"/home/t521-{uuid.uuid4().hex[:8]}"
    await create_folder(client, paths=[f"{folder}/inner"])
    try:
        yield client, folder
    finally:
        for path in (folder, f"/home/#recycle/{folder.rsplit('/', 1)[-1]}"):
            with contextlib.suppress(ToolError):
                await delete_files(client, paths=[path], recycle_bin_status={})


async def test_a_delete_that_went_through_reads_as_done(
    top: tuple[DsmClient, str], caplog: pytest.LogCaptureFixture
) -> None:
    client, folder = top
    with caplog.at_level(logging.WARNING), _lost_status(client):
        out = await delete_files(client, paths=[f"{folder}/inner"], recycle_bin_status={})
    assert "deleted 1 item(s)" in out.lower(), out
    assert "isn't known" not in out
    assert caplog.text.count("answered 599") == 3


async def test_a_delete_that_didnt_hands_off_with_the_handle(top: tuple[DsmClient, str]) -> None:
    """A folder with something in it, recursive=false: DSM refuses, so the path stays."""
    client, folder = top
    records = TaskRecords()
    with _lost_status(client):
        out = await delete_files(
            client, paths=[folder], recursive=False, recycle_bin_status={}, records=records
        )
    assert "(error 599)" in out
    assert "how it ended isn't known" in out
    assert "Handle: `delete:" in out
