"""get_dir_size of a path that doesn't exist on virtual-dsm (#332).

DSM's DirSize task finishes at once for a missing path with 0 bytes, 0 files and
0 directories (seen on DSM 7.1.1), which reads as an empty folder; get_dir_size
checks the path first.

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


async def test_existing_folder_still_measured(nas_client: Any) -> None:
    from mcp_synology.modules.filestation.metadata import get_dir_size

    result = await get_dir_size(nas_client[0], path="/testshare")
    assert "Total size" in result, result
