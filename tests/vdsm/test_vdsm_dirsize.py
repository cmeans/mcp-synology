"""get_dir_size of a path that doesn't exist or can't be read on virtual-dsm (#332).

DSM's DirSize task finishes at once for a missing path with 0 bytes, 0 files and
0 directories (seen on DSM 7.1.1), which reads as an empty folder; get_dir_size
checks the path first. For a path the user can't read, the task finishes with
``errors`` and no totals, which must not read as 0 B either.

Run with: uv run pytest -m vdsm -v tests/vdsm/test_vdsm_dirsize.py
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

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

    async def check_fails(*_args: Any) -> list[tuple[str, int]]:
        raise DsmReplyError("unreadable reply")

    monkeypatch.setattr(metadata, "find_missing_paths", check_fails)
    with pytest.raises(ToolError) as exc:
        await metadata.get_dir_size(nas_client[0], path="/homes/admin")
    error = json.loads(str(exc.value))["error"]
    assert error["code"] == "permission_denied", error


async def test_existing_folder_still_measured(nas_client: Any) -> None:
    from mcp_synology.modules.filestation.metadata import get_dir_size

    result = await get_dir_size(nas_client[0], path="/testshare")
    assert "Total size" in result, result
