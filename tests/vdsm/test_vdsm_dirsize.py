"""get_dir_size of a path that doesn't exist or can't be read on virtual-dsm (#332).

DSM's DirSize task finishes at once for a missing path with 0 bytes, 0 files and
0 directories (seen on DSM 7.1.1), which reads as an empty folder; get_dir_size
checks the path first. For a path the user can't read, the task finishes with
``errors`` and no totals, which must not read as 0 B either. A folder the user is
denied by an ACL or POSIX mode measures 0 B with no errors (#336); getinfo's
permissions show it.

Run with: uv run pytest -m vdsm -v tests/vdsm/test_vdsm_dirsize.py
"""

from __future__ import annotations

import json
import os
import re
import shlex
from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.vdsm.conftest import _ssh
from tests.vdsm.golden_image import load_golden_meta

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.vdsm.container import VirtualDsmContainer

pytestmark = pytest.mark.vdsm


async def test_missing_path_is_not_found(nas_client: Any) -> None:
    from mcp_synology.modules.filestation.metadata import get_dir_size

    with pytest.raises(ToolError) as exc:
        await get_dir_size(nas_client[0], path="/testshare/mcp332-no-such-folder")
    error = json.loads(str(exc.value))["error"]
    assert error["code"] == "not_found", error
    assert "doesn't exist on the NAS" in error["message"]


async def test_unreadable_path_is_permission_denied(nas_client: Any) -> None:
    """nas_client is the non-admin test user, who can't read admin's home."""
    from mcp_synology.modules.filestation.metadata import get_dir_size

    with pytest.raises(ToolError) as exc:
        await get_dir_size(nas_client[0], path="/homes/admin")
    error = json.loads(str(exc.value))["error"]
    assert error["code"] == "permission_denied", error
    assert "no permission to read /homes/admin" in error["message"]


async def test_unreadable_path_without_the_check(
    nas_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If getinfo fails, DSM's own DirSize answer (errors, no totals) is mapped, not 0 B."""
    from mcp_synology.core.errors import DsmReplyError
    from mcp_synology.modules.filestation import metadata

    async def check_fails(*_args: Any) -> dict[str, Any]:
        raise DsmReplyError("unreadable reply")

    monkeypatch.setattr(metadata, "_getinfo_entry", check_fails)
    with pytest.raises(ToolError) as exc:
        await metadata.get_dir_size(nas_client[0], path="/homes/admin")
    error = json.loads(str(exc.value))["error"]
    assert error["code"] == "permission_denied", error


@pytest.fixture
def denied_folders(vdsm_container: VirtualDsmContainer, dsm_version: str) -> Iterator[str]:
    """A folder in /testshare holding a.txt (4 B), acl/ (an ACL entry denies the test user)
    and posix/ (root's, mode 700, no ACL), each with one 8-byte file. Made over SSH."""
    password = str(load_golden_meta(dsm_version).get("admin_password", ""))
    name = f"mcp336-{os.urandom(3).hex()}"
    vol = f"/volume1/testshare/{name}"
    acl = "/usr/syno/bin/synoacltool"
    cmd = (
        f"mkdir -p {vol}/acl {vol}/posix && printf aaaa > {vol}/a.txt && "
        f"printf bbbbbbbb > {vol}/acl/b.txt && printf cccccccc > {vol}/posix/c.txt && "
        f"chmod -R a+rX {vol} && {acl} -add {vol}/acl user:mcptest:deny:rwxpdDaARWcCo:fd-- && "
        f"({acl} -del {vol}/posix || true) && chown -R root:root {vol}/posix && "
        f"chmod -R 700 {vol}/posix"
    )
    try:
        rc, out = _ssh(vdsm_container, password, f"sh -c {shlex.quote(cmd)}", 60)
        assert rc == 0, out
        yield f"/testshare/{name}"
    finally:
        _ssh(vdsm_container, password, f"rm -rf {vol}", 60)


@pytest.mark.parametrize("sub", ["acl", "posix"])
async def test_denied_folder_is_permission_denied(
    nas_client: Any, denied_folders: str, sub: str
) -> None:
    """DSM measures a denied folder as 0 B with no errors; getinfo's read: false refuses it."""
    from mcp_synology.modules.filestation.metadata import get_dir_size

    with pytest.raises(ToolError) as exc:
        await get_dir_size(nas_client[0], path=f"{denied_folders}/{sub}")
    error = json.loads(str(exc.value))["error"]
    assert error["code"] == "permission_denied", error


async def test_denied_subfolders_are_left_out(
    nas_client: Any, admin_client: Any, denied_folders: str
) -> None:
    """DSM leaves a subfolder the user can't read out of the totals, with no signal (#336);
    the tool description says so. If this starts failing, DSM now reports it."""
    from mcp_synology.modules.filestation.metadata import get_dir_size

    as_user = await get_dir_size(nas_client[0], path=denied_folders)
    as_admin = await get_dir_size(admin_client[0], path=denied_folders)
    assert re.search(r"Total size:\s+4 B\n", as_user), as_user
    assert re.search(r"Total size:\s+20 B\n", as_admin), as_admin


async def test_existing_folder_still_measured(nas_client: Any) -> None:
    from mcp_synology.modules.filestation.metadata import get_dir_size

    result = await get_dir_size(nas_client[0], path="/testshare")
    assert "Total size" in result, result
