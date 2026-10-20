"""download_file of a folder and of a share's top level on virtual-dsm (#327, #330).

DSM sends a folder below a share as a ZIP of it, saved as <folder>.zip; a share's top
level gets a 502 (404 as admin), so it's refused before any download.

Run with: uv run pytest -m vdsm -v tests/vdsm/test_vdsm_download.py
"""

from __future__ import annotations

import json
import os
import shlex
import zipfile
from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.vdsm.conftest import _ssh
from tests.vdsm.golden_image import load_golden_meta

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tests.vdsm.container import VirtualDsmContainer

pytestmark = pytest.mark.vdsm


@pytest.fixture
def small_folder(vdsm_container: VirtualDsmContainer, dsm_version: str) -> Iterator[str]:
    """A folder in /testshare holding a.txt and sub/b.txt (made over SSH)."""
    password = str(load_golden_meta(dsm_version).get("admin_password", ""))
    name = f"mcp327-{os.urandom(3).hex()}"
    on_volume = f"/volume1/testshare/{name}"
    cmd = (
        f"mkdir -p {on_volume}/sub && printf one > {on_volume}/a.txt && "
        f"printf two > {on_volume}/sub/b.txt && chmod -R a+rX {on_volume}"
    )
    try:
        rc, out = _ssh(vdsm_container, password, f"sh -c {shlex.quote(cmd)}", 60)
        assert rc == 0, out
        yield f"/testshare/{name}"
    finally:
        _ssh(vdsm_container, password, f"rm -rf {on_volume}", 60)


async def test_folder_downloads_as_its_zip(
    nas_client: Any, small_folder: str, tmp_path: Path
) -> None:
    """As the non-admin test user: <folder>.zip, a real ZIP holding the folder's files."""
    from mcp_synology.modules.filestation.transfer import download_file

    out = await download_file(nas_client[0], path=small_folder, dest_folder=str(tmp_path))
    name = small_folder.rsplit("/", 1)[1]
    saved = tmp_path / f"{name}.zip"
    assert "as a ZIP archive of it" in out, out
    assert saved.read_bytes()[:4] == b"PK\x03\x04"
    with zipfile.ZipFile(saved) as archive:
        members = {m.rstrip("/").split("/", 1)[-1] for m in archive.namelist()}
        assert {"a.txt", "sub/b.txt"} <= members, archive.namelist()
    assert sorted(p.name for p in tmp_path.iterdir()) == [saved.name]


async def _refused(client: Any, tmp_path: Path) -> None:
    from mcp_synology.modules.filestation.transfer import download_file

    with pytest.raises(ToolError) as exc:
        await download_file(client, path="/testshare", dest_folder=str(tmp_path))
    error = json.loads(str(exc.value))["error"]
    assert error["code"] == "invalid_parameter", error
    assert "is a whole shared folder" in error["message"]
    assert list(tmp_path.iterdir()) == []


async def test_share_top_level_is_refused(nas_client: Any, tmp_path: Path) -> None:
    """DSM answers a share's top level with a 502 for the non-admin user: refused first."""
    await _refused(nas_client[0], tmp_path)


async def test_share_top_level_is_refused_for_admin(admin_client: Any, tmp_path: Path) -> None:
    """...and with a 404 for admin."""
    await _refused(admin_client[0], tmp_path)
