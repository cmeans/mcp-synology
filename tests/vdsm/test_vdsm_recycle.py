"""list_recycle_bin against a real recycle bin on vdsm (#333).

The shares the bake creates have no recycle bin; the user home service has one (bake
v5+), so these tests delete into ``/home/#recycle`` as the test user.

Run: ``uv run pytest tests/vdsm -m vdsm --dsm-version 7.2.2 -k vdsm_recycle``
"""

from __future__ import annotations

import contextlib
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.filestation.listing import list_files, list_recycle_bin
from mcp_synology.modules.filestation.operations import (
    create_folder,
    delete_files,
    restore_from_recycle_bin,
)
from mcp_synology.modules.filestation.transfer import upload_file

if TYPE_CHECKING:
    from pathlib import Path

    from mcp_synology.core.client import DsmClient

pytestmark = pytest.mark.vdsm


async def _clean(client: DsmClient, top: str) -> None:
    """Remove ``/home/<top>`` and every trace of it in the bin.

    The bin copy is deleted (permanently: it's inside #recycle) before and after the
    live folder, so a second copy never lands next to the first under another name.
    """
    for paths in ([f"/home/#recycle/{top}"], [f"/home/{top}"], [f"/home/#recycle/{top}"]):
        with contextlib.suppress(ToolError):
            await delete_files(client, paths=paths, recycle_bin_status={})
    left = await list_recycle_bin(client, share="home", pattern=f"{top}*")
    assert "(0 found in" in left, left


async def test_pattern_finds_a_file_deleted_from_a_subfolder(
    nas_client: Any, tmp_path: Path
) -> None:
    """The 0.10.0rc3 case: a pattern missed a file in a subfolder of the bin."""
    client: DsmClient = nas_client[0]
    top = f"t333-{uuid.uuid4().hex[:8]}"
    name = f"b2-{uuid.uuid4().hex[:6]}.txt"
    local = tmp_path / name
    local.write_text("deleted into a subfolder of the bin")
    try:
        await create_folder(client, paths=[f"/home/{top}/sub/deeper", f"/home/{top}/sub/empty"])
        for folder in ("sub", "sub/deeper"):
            await upload_file(
                client,
                local_path=str(local),
                dest_folder=f"/home/{top}/{folder}",
                allowed_roots=[tmp_path],
            )
        out = await delete_files(client, paths=[f"/home/{top}"], recycle_bin_status={})
        assert "recycle bin" in out.lower(), out

        # The file sits two levels down in the bin, and a copy three levels down.
        found = await list_recycle_bin(client, share="home", pattern=name, recycle_bin_status={})
        assert f"{top}/sub/{name}" in found and f"{top}/sub/deeper/{name}" in found, found
        assert "(2 found in" in found and "subfolders included" in found
        # A bare word matches anywhere in the name, as DSM's own pattern does.
        assert f"{top}/sub/{name}" in await list_recycle_bin(
            client, share="home", pattern=name[3:9].upper()
        )
        # Folders match too, shown with a slash. DSM doesn't keep an empty folder in the
        # bin (DSM 7.1 and 7.3.2): sub/empty is gone, sub/deeper (it holds a file) isn't.
        folders = await list_recycle_bin(client, share="home", pattern=f"{top}*,deeper,empty")
        assert f"{top}/sub/deeper/" in folders and f"{top}/ " in folders, folders
        assert f"{top}/sub/empty" not in folders, folders

        # The path as shown is what a restore takes.
        # Its folder was deleted with it, so the restore re-creates it (DSM alone gives 1002).
        restored = await restore_from_recycle_bin(client, share="home", paths=[f"{top}/sub/{name}"])
        assert "Restored" in restored, restored
        assert f"Re-created /home/{top}/sub (no longer there) to restore into." in restored
        assert name in await list_files(client, path=f"/home/{top}/sub")
        left = await list_recycle_bin(client, share="home", pattern=name)
        assert "(1 found in" in left and f"{top}/sub/deeper/{name}" in left, left
    finally:
        await _clean(client, top)


async def test_listings_say_dsm_sorts_folders_and_files_separately(
    nas_client: Any, tmp_path: Path
) -> None:
    """DSM groups by type: ascending folders first, descending files first (#333)."""
    client: DsmClient = nas_client[0]
    top = f"/home/t333s-{uuid.uuid4().hex[:8]}"
    local = tmp_path / "a-file.txt"
    local.write_text("x")
    try:
        await create_folder(client, paths=[f"{top}/a-folder"])
        await upload_file(client, local_path=str(local), dest_folder=top, allowed_roots=[tmp_path])
        for direction, first, second in (
            ("asc", "a-folder/", "a-file.txt"),
            ("desc", "a-file.txt", "a-folder/"),
        ):
            for sort_by in ("name", "mtime", "size"):
                out = await list_files(client, path=top, sort_by=sort_by, sort_direction=direction)
                assert out.index(first) < out.index(second), (sort_by, direction, out)
                group = "folders" if direction == "asc" else "files"
                assert f"DSM sorts folders and files separately; {group} come first." in out
    finally:
        await _clean(client, top.rsplit("/", 1)[1])
