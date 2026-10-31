"""Paths are taken exactly (#488), and `resolve_paths`' rules hold (#510), against a real DSM.

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
from mcp_synology.modules.filestation.operations import create_folder, delete_files, rename
from mcp_synology.modules.filestation.paths import resolve_paths

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


async def _raw_delete(client: DsmClient, path: str) -> dict[str, Any]:
    import asyncio

    started = await client.request(
        "SYNO.FileStation.Delete", "start", version=2, params={"path": path, "recursive": "true"}
    )
    task = dsm_str(started["taskid"])
    for _ in range(40):
        status = await client.request(
            "SYNO.FileStation.Delete", "status", version=2, params={"taskid": task}
        )
        if status.get("finished"):
            return dict(status)
        await asyncio.sleep(0.5)
    pytest.fail(f"DSM's delete of {path!r} didn't finish")


def _code(exc: pytest.ExceptionInfo[ToolError]) -> str:
    return str(json.loads(str(exc.value))["error"]["code"])


async def test_resolve_paths_rules_hold_on_dsm(nas_client: Any, tmp_path: Any) -> None:
    """#510: what the rules guard against is what DSM does (probes on #472 and #494)."""
    client: DsmClient = nas_client[0]
    known: set[str] = set()
    root = f"/writable/t510-{uuid.uuid4().hex[:8]}"
    await _mk(client, "/writable", root.rsplit("/", 1)[1])
    try:
        # NUL: DSM cuts the path there, so a raw Delete of `a<NUL>x` deletes `a`. Refused.
        for name in ("a", "b"):
            await _mk(client, root, name)
        await _raw_delete(client, f"{root}/a\x00x")
        assert await _names(client, root) == {"b"}
        with pytest.raises(ToolError) as exc:
            await resolve_paths(client, known, "delete_files", "paths", [f"{root}/b\x00x"])
        assert _code(exc) == "invalid_parameter"
        assert await _names(client, root) == {"b"}

        # An item " " at a share's top that doesn't exist: taken exactly, DSM says 408 and
        # deletes nothing; the shared folder stays.
        [blank] = await resolve_paths(client, known, "delete_files", "paths", ["/writable/ "])
        with pytest.raises(ToolError) as exc:
            await delete_files(client, paths=[blank], recycle_bin_status={})
        assert _code(exc) == "not_found"
        assert root.rsplit("/", 1)[1] in await _names(client, "/writable")

        # A padded upload destination that doesn't exist would be created padded: refused.
        with pytest.raises(ToolError) as exc:
            await resolve_paths(
                client, known, "upload_file", "dest_folder", [f"{root}/up "], creates_parents=True
            )
        assert _code(exc) == "invalid_parameter"
        assert await _names(client, root) == {"b"}

        # `#recycle` can't be created at a share's top (DSM would allow it), but deeper it's
        # an ordinary name; a tab inside a new name is created as given.
        with pytest.raises(ToolError) as exc:
            await resolve_paths(client, known, "create_folder", "paths", ["/writable/#recycle"])
        assert _code(exc) == "invalid_parameter"
        assert "#recycle" not in await _names(client, "/writable")
        # Joiners (an emoji ZWJ sequence, a ZWNJ) are allowed in new names: DSM keeps them.
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        made = await resolve_paths(
            client,
            known,
            "create_folder",
            "paths",
            [f"{root}/#recycle", f"{root}/a\tb", f"{root}/{family}", f"{root}/ab\u200ccd"],
        )
        await create_folder(client, paths=made)
        assert await _names(client, root) == {"b", "#recycle", "a\tb", family, "ab\u200ccd"}
        # A direction-changing character is refused before DSM sees it.
        with pytest.raises(ToolError) as exc:
            await resolve_paths(
                client, known, "create_folder", "paths", [f"{root}/invoice\u202etxt.exe"]
            )
        assert _code(exc) == "invalid_parameter"
    finally:
        with contextlib.suppress(ToolError):
            await delete_files(client, paths=[root], recycle_bin_status={})


async def test_a_users_home_folder_is_refused_as_a_write_target(admin_client: Any) -> None:
    """An admin's Rename of /homes/<user> orphans that user's home (probe on #494)."""
    client: DsmClient = admin_client[0]
    known: set[str] = set()
    for tool, param in (("rename", "path"), ("move_files", "paths"), ("delete_files", "paths")):
        with pytest.raises(ToolError) as exc:
            await resolve_paths(client, known, tool, param, ["/homes/mcpfresh"])
        assert "whole home folder" in json.loads(str(exc.value))["error"]["message"]
    assert "mcpfresh" in await _names(client, "/homes")
