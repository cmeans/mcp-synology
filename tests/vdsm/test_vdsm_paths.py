"""Paths are taken exactly (#488), against a real DSM.

DSM keeps whitespace in a name: ``foo `` and ``" "`` are names beside ``foo``. The tools used
to strip a path's ends, so ``…/foo `` reached ``…/foo`` and ``…/ `` the folder above. These
names are made with DSM's own CreateFolder, since ``create_folder`` refuses a padded new
name (#488).

Run: ``uv run pytest tests/vdsm -m vdsm --dsm-version 7.2.2 -k vdsm_paths``
"""

from __future__ import annotations

import contextlib
import json
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.filestation.helpers import dsm_str
from mcp_synology.modules.filestation.listing import list_files
from mcp_synology.modules.filestation.metadata import get_file_info
from mcp_synology.modules.filestation.operations import delete_files, rename

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

pytestmark = pytest.mark.vdsm


async def _mk(client: DsmClient, parent: str, name: str) -> None:
    await client.request(
        "SYNO.FileStation.CreateFolder",
        "create",
        params={"folder_path": parent, "name": dsm_str(name), "force_parent": "false"},
    )


async def _names(client: DsmClient, folder: str) -> set[str]:
    data = await client.request("SYNO.FileStation.List", "list", params={"folder_path": folder})
    return {f["name"] for f in data["files"]}


async def _exact_paths(client: DsmClient) -> None:
    root = f"/writable/t488-{uuid.uuid4().hex[:8]}"
    await _mk(client, "/writable", root.rsplit("/", 1)[1])
    try:
        for name, marker in (("foo", "in-plain"), ("foo ", "in-space"), (" ", "in-blank")):
            await _mk(client, root, name)
            await _mk(client, f"{root}/{name}", marker)
        assert await _names(client, root) == {"foo", "foo ", " "}

        # Reads reach the padded item, not its stripped twin or the folder above.
        assert "in-space" in await list_files(client, path=f"{root}/foo ")
        assert "in-blank" in await list_files(client, path=f"{root}/ ")
        assert "in-plain" not in await list_files(client, path=f"{root}/foo ")
        info = await get_file_info(client, paths=[f"{root}/foo "])
        assert f"{root}/foo " in info, info

        # Rename moves `foo `, leaving `foo`.
        await rename(client, path=f"{root}/foo ", new_name="renamed")
        assert await _names(client, root) == {"foo", "renamed", " "}
        assert await _names(client, f"{root}/renamed") == {"in-space"}

        # Delete of `…/ ` deletes the item " ", never the folder it's in.
        await delete_files(client, paths=[f"{root}/ "], recycle_bin_status={})
        assert await _names(client, root) == {"foo", "renamed"}

        # A stray space on a missing name is "not found", shown quoted.
        with pytest.raises(ToolError) as exc:
            await delete_files(client, paths=[f"{root}/renamed "], recycle_bin_status={})
        error = json.loads(str(exc.value))["error"]
        assert error["code"] == "not_found", error
        assert repr(f"{root}/renamed ") in error["message"], error
        assert await _names(client, root) == {"foo", "renamed"}
    finally:
        with contextlib.suppress(ToolError):
            await delete_files(client, paths=[root], recycle_bin_status={})


async def test_paths_are_taken_exactly(nas_client: Any) -> None:
    await _exact_paths(nas_client[0])


async def test_paths_are_taken_exactly_admin(admin_client: Any) -> None:
    await _exact_paths(admin_client[0])
