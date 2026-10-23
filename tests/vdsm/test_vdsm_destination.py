"""copy_files and move_files check the destination folder first, on virtual-dsm (#344).

DSM answers a copy or move into a folder that doesn't exist with 1002, which read as a
permissions problem. The destination is now checked with getinfo before any task
starts: DSM's 408 there is not_found and its 407 permission_denied, as the non-admin
test user (nas_client) sees them. Each test works in its own folder under /writable.

Run with: uv run pytest -m vdsm -v tests/vdsm/test_vdsm_destination.py
"""

from __future__ import annotations

import contextlib
import json
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.filestation.listing import list_files
from mcp_synology.modules.filestation.operations import (
    copy_files,
    create_folder,
    delete_files,
    move_files,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mcp_synology.core.client import DsmClient

pytestmark = pytest.mark.vdsm


@pytest.fixture
async def base(nas_client: Any) -> AsyncIterator[str]:
    """/writable/<unique>/ with a source folder `src` and an existing destination `dest`."""
    client: DsmClient = nas_client[0]
    top = f"/writable/mcp344-{uuid.uuid4().hex[:8]}"
    try:
        await create_folder(client, paths=[f"{top}/src", f"{top}/dest"])
        yield top
    finally:
        with contextlib.suppress(ToolError):
            await delete_files(client, paths=[top], recycle_bin_status={})


def _error(exc: ToolError) -> dict[str, Any]:
    error: dict[str, Any] = json.loads(str(exc))["error"]
    return error


@pytest.mark.parametrize(("tool", "verb"), [(copy_files, "copied"), (move_files, "moved")])
@pytest.mark.parametrize("missing_part", ["no-such-folder", "no-such-folder/sub"])
async def test_missing_destination_is_not_found(
    nas_client: Any, base: str, tool: Any, verb: str, missing_part: str
) -> None:
    """A missing folder, and one below a missing folder: DSM answers 408 for both."""
    client: DsmClient = nas_client[0]
    missing = f"{base}/{missing_part}"
    with pytest.raises(ToolError) as exc:
        await tool(client, paths=[f"{base}/src"], dest_folder=missing)
    error = _error(exc.value)
    assert error["code"] == "not_found", error
    assert error["param"] == "dest_folder", error
    expected = f"the destination folder {missing} doesn't exist. Nothing was {verb}."
    assert expected in error["message"], error
    # Nothing ran: the source is still where it was.
    assert "src/" in await list_files(client, path=base)


async def test_destination_without_permission_is_permission_denied(
    nas_client: Any, base: str
) -> None:
    """The non-admin test user can't use admin's home folder (#365's path)."""
    client: DsmClient = nas_client[0]
    with pytest.raises(ToolError) as exc:
        await copy_files(client, paths=[f"{base}/src"], dest_folder="/homes/admin")
    error = _error(exc.value)
    assert error["code"] == "permission_denied", error
    assert error["param"] == "dest_folder", error
    assert "no permission to use the destination folder /homes/admin" in error["message"]


async def test_existing_destination_passes_the_check(nas_client: Any, base: str) -> None:
    client: DsmClient = nas_client[0]
    result = await copy_files(client, paths=[f"{base}/src"], dest_folder=f"{base}/dest")
    assert "Copied 1 item(s)" in result, result
    assert "src/" in await list_files(client, path=f"{base}/dest")
