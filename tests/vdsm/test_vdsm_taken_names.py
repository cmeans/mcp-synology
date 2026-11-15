"""A name already taken (#726, #749): DSM wraps the cause in `errors`.

CreateFolder answers 1100 with 414 inside for a folder that exists when `force_parent=false`
and for a file at the path with either value; Rename answers 1200 with 414 inside for a name
taken in its folder. The tools said "Insufficient filesystem permissions" for the first.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path

pytestmark = pytest.mark.vdsm


async def _error(call: Awaitable[str]) -> dict[str, Any]:
    with pytest.raises(ToolError) as exc:
        await call
    return json.loads(str(exc.value))["error"]  # type: ignore[no-any-return]


async def test_a_taken_name_is_already_exists(nas_client: Any, tmp_path: Path) -> None:
    from mcp_synology.modules.filestation.operations import create_folder, delete_files, rename
    from mcp_synology.modules.filestation.transfer import upload_file

    client = nas_client[0]
    top = f"/home/t749-{uuid.uuid4().hex[:8]}"
    local = tmp_path / "f.txt"
    local.write_text("x")
    try:
        await create_folder(client, paths=[f"{top}/d", f"{top}/e"])
        await upload_file(client, local_path=str(local), dest_folder=top, allowed_roots=[tmp_path])
        # An existing folder with force_parent=false.
        error = await _error(create_folder(client, paths=[f"{top}/d"], force_parent=False))
        assert error["code"] == "already_exists", error
        assert error["message"] == (
            f"Create folder failed (DSM error 1100): {top}/d already exists."
        ), error
        # A missing parent with force_parent=false: 1100 with 408 inside (QA r1 on #751).
        error = await _error(create_folder(client, paths=[f"{top}/no/x"], force_parent=False))
        assert error["code"] == "not_found", error
        assert error["message"] == (
            f"Create folder failed (DSM error 1100): the parent folder {top}/no doesn't exist."
        ), error
        # With force_parent=true (the default) an existing folder isn't an error.
        assert f"{top}/d" in await create_folder(client, paths=[f"{top}/d"])
        # A file at the path, either way.
        for force_parent in (False, True):
            error = await _error(
                create_folder(client, paths=[f"{top}/f.txt"], force_parent=force_parent)
            )
            assert error["code"] == "already_exists", (force_parent, error)
        # Rename onto a taken name.
        error = await _error(rename(client, path=f"{top}/d", new_name="e"))
        assert error["code"] == "already_exists", error
        assert f"{top}/e already exists" in error["message"], error
    finally:
        with contextlib.suppress(ToolError):
            await delete_files(client, paths=[top], recycle_bin_status={})
