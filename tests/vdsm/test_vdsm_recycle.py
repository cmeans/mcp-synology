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

from mcp_synology.modules.filestation.helpers import ensure_known_shares
from mcp_synology.modules.filestation.listing import list_files, list_recycle_bin, list_shares
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


async def _home_takes_only_its_listed_name(client: DsmClient, tmp_path: Path) -> None:
    """#353: DSM takes an ordinary share in any case, but the home service only as /home.

    So the share check sends the name as listed, and a /Home that does reach
    list_recycle_bin says it's not there, never "cannot be recovered".
    """
    assert "TESTSHARE" in await list_files(client, path="/TESTSHARE")
    with pytest.raises(ToolError, match="408"):
        await list_files(client, path="/Home")
    known: set[str] = set()
    assert await ensure_known_shares(client, ["/Home/x", "Testshare"], known) == [
        "/home/x",
        "/testshare",
    ]
    assert "home" in known

    top = f"t353-{uuid.uuid4().hex[:8]}"
    local = tmp_path / "f.txt"
    local.write_text("deleted, then listed through share=Home")
    try:
        await create_folder(client, paths=[f"/home/{top}"])
        await create_folder(client, paths=[f"/home/{top}/sub"])
        await upload_file(
            client, local_path=str(local), dest_folder=f"/home/{top}", allowed_roots=[tmp_path]
        )
        # The existence probes ask for one entry: DSM's limit=0 lists them all.
        for limit, count in ((0, 2), (1, 1)):
            data = await client.request(
                "SYNO.FileStation.List",
                "list",
                params={"folder_path": f"/home/{top}", "limit": limit},
            )
            assert len(data["files"]) == count and data["total"] == 2, (limit, data)
        await delete_files(client, paths=[f"/home/{top}"], recycle_bin_status={})
        [share] = await ensure_known_shares(client, ["Home"], known)
        found = await list_recycle_bin(client, share=share, pattern=top)
        assert f"{top}/" in found, found
        status: dict[str, bool] = {}
        with pytest.raises(ToolError, match="The user home folder is /home"):
            await list_recycle_bin(client, share="Home", recycle_bin_status=status)
        assert status == {}
    finally:
        await _clean(client, top)


async def test_home_takes_only_its_listed_name(nas_client: Any, tmp_path: Path) -> None:
    await _home_takes_only_its_listed_name(nas_client[0], tmp_path)


async def test_home_takes_only_its_listed_name_admin(admin_client: Any, tmp_path: Path) -> None:
    await _home_takes_only_its_listed_name(admin_client[0], tmp_path)


async def _recycle_column(client: DsmClient) -> tuple[dict[str, str], dict[str, bool], str]:
    cache: dict[str, bool] = {}
    out = await list_shares(client, recycle_bin_status=cache)
    rows = [ln.split() for ln in out.splitlines() if ln.startswith("  ") and " /" in ln]
    column = {r[0]: "couldn't check" if r[-1] == "check" else r[-1] for r in rows}
    return column, cache, out


async def _every_shares_bin(client: DsmClient) -> None:
    """#255, #335: list_shares checks every share's bin, in a fresh session too.

    The bake's shares have no recycle bin; the user home service has one.
    """
    column, cache, out = await _recycle_column(client)
    assert column.get("home") == "enabled", out
    assert column.get("testshare") == "disabled", out
    assert set(column.values()) <= {"enabled", "disabled"}, out
    assert cache == {name: state == "enabled" for name, state in column.items()}, out


async def test_list_shares_shows_every_shares_bin(nas_client: Any) -> None:
    await _every_shares_bin(nas_client[0])


async def test_list_shares_shows_every_shares_bin_admin(admin_client: Any) -> None:
    await _every_shares_bin(admin_client[0])
