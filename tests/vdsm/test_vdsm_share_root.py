"""delete_files on a shared folder deletes its contents, never the folder itself (#451).

On DSM 7.2.2 and later, File Station's Delete on `/<share>` removes the shared folder for
any account with write access; DSM 7.1 refuses it. The tool lists the share's top-level
items and deletes those instead, after a preview the user confirms.

Run: ``uv run pytest tests/vdsm -m vdsm --dsm-version 7.2.2 -k share_root``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mcp_synology.modules.filestation.metadata import dir_size_totals
from mcp_synology.modules.filestation.operations import (
    create_folder,
    delete_files,
    share_contents,
    share_root_preview,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

pytestmark = pytest.mark.vdsm


async def _shares(client: DsmClient) -> set[str]:
    data = await client.request("SYNO.FileStation.List", "list_share", version=2)
    return {s["name"] for s in data.get("shares", [])}


@pytest.mark.parametrize("persona", ["user", "admin"])
async def test_share_root_contents_deleted_share_kept(
    nas_client: Any, admin_client: Any, persona: str
) -> None:
    client: DsmClient = (nas_client if persona == "user" else admin_client)[0]
    await create_folder(client, paths=[f"/writable/sr-{persona}/a", f"/writable/sr-{persona}-b"])

    preview = await share_root_preview(client, ["/writable"], timeout=60)
    size, files, folders = await dir_size_totals(client, path="/writable", timeout=60)
    print(preview, (size, files, folders))
    assert preview.startswith("This would delete everything in these shared folders")
    assert "/writable:" in preview

    items = await share_contents(client, "/writable")
    assert f"/writable/sr-{persona}" in items and f"/writable/sr-{persona}-b" in items
    assert all(not i.endswith("/#recycle") for i in items)

    await delete_files(client, paths=items, timeout=60)
    assert "writable" in await _shares(client)  # the shared folder is still there
    assert await share_contents(client, "/writable") == []
