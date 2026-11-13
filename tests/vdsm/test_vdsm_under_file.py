"""A path under a file against virtual-dsm (#702).

DSM answers a path below a file (`…/f0.txt/x`) in ways that don't say so: getinfo's 401 entry
("can't be read"), DirSize's 0 B, a download's HTTP 404, a copy or move's 1002 into it,
CreateFolder's 1100 ("Insufficient filesystem permissions"), Upload's 401 and Rename's 1200
(vdsm 7.1). Every File Station tool names the file instead. #517 did list_files and
search_files (`test_vdsm_search.py`). Run as the non-admin test user and as admin.

Run with: uv run pytest -m vdsm -v tests/vdsm/test_vdsm_under_file.py
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.vdsm.test_vdsm_search import dated_folder  # noqa: F401 - the fixture

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path

pytestmark = pytest.mark.vdsm

BELOW = ["child", "x/y/child"]  # deep: 401 above it too


@pytest.fixture(params=["nas_client", "admin_client"])
def client(request: pytest.FixtureRequest) -> Any:
    return request.getfixturevalue(request.param)[0]


async def _error(call: Awaitable[str]) -> dict[str, Any]:
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        await call
    return json.loads(str(exc.value))["error"]  # type: ignore[no-any-return]


@pytest.mark.parametrize("below", BELOW)
class TestVdsmPathUnderAFile:
    async def test_get_file_info(self, client: Any, dated_folder: str, below: str) -> None:  # noqa: F811
        from mcp_synology.modules.filestation.metadata import get_file_info

        file = f"{dated_folder}/f0.txt"
        error = await _error(get_file_info(client, paths=[f"{file}/{below}"]))
        assert (error["code"], error["retryable"]) == ("not_found", False), error
        assert error["message"] == (
            f"Get file info failed: {file}/{below}: doesn't exist (DSM error 401): "
            f"{file} is a file."
        )
        # Beside a path that exists, it's a note under the found one's info.
        out = await get_file_info(client, paths=[f"{file}/{below}", file])
        assert f"{file}/{below}: doesn't exist (DSM error 401): {file} is a file" in out

    async def test_get_dir_size(self, client: Any, dated_folder: str, below: str) -> None:  # noqa: F811
        from mcp_synology.modules.filestation.metadata import get_dir_size

        file = f"{dated_folder}/f0.txt"
        error = await _error(get_dir_size(client, path=f"{file}/{below}"))
        assert (error["code"], error["retryable"]) == ("not_found", False), error
        assert error["message"] == (
            f"Get directory size failed: {file}/{below} doesn't exist on the NAS: {file} is a file."
        )

    async def test_download_file(
        self,
        client: Any,
        dated_folder: str,  # noqa: F811
        below: str,
        tmp_path: Path,
    ) -> None:
        from mcp_synology.modules.filestation.transfer import download_file

        file = f"{dated_folder}/f0.txt"
        error = await _error(
            download_file(
                client, path=f"{file}/{below}", dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        )
        assert (error["code"], error["retryable"]) == ("not_found", False), error
        assert (
            error["message"]
            == f"Download failed: {file}/{below} doesn't exist on the NAS: {file} is a file."
        )
        assert not list(tmp_path.iterdir())

    @pytest.mark.parametrize("op", ["copy", "move"])
    async def test_copy_or_move_a_source(
        self,
        client: Any,
        dated_folder: str,  # noqa: F811
        below: str,
        op: str,
    ) -> None:
        from mcp_synology.modules.filestation import operations

        file = f"{dated_folder}/f0.txt"
        call = getattr(operations, f"{op}_files")
        error = await _error(call(client, paths=[f"{file}/{below}"], dest_folder=dated_folder))
        assert (error["code"], error["retryable"]) == ("not_found", False), error
        verb = op.capitalize()
        assert error["message"] == (
            f"{verb} files refused: {file}/{below}: doesn't exist (DSM error 401): "
            f"{file} is a file. Nothing was {'copied' if op == 'copy' else 'moved'}."
        )

    @pytest.mark.parametrize("op", ["copy", "move"])
    async def test_copy_or_move_into_it(
        self,
        client: Any,
        dated_folder: str,  # noqa: F811
        below: str,
        op: str,
    ) -> None:
        from mcp_synology.modules.filestation import operations
        from mcp_synology.modules.filestation.listing import list_files

        file = f"{dated_folder}/f0.txt"
        call = getattr(operations, f"{op}_files")
        error = await _error(
            call(client, paths=[f"{dated_folder}/f1.txt"], dest_folder=f"{file}/{below}")
        )
        assert (error["code"], error["retryable"]) == ("not_found", False), error
        assert error["message"] == (
            f"{op.capitalize()} files refused: the destination folder {file}/{below} doesn't "
            f"exist: {file} is a file. Nothing was {'copied' if op == 'copy' else 'moved'}."
        )
        assert "f1.txt" in await list_files(client, path=dated_folder)  # nothing moved

    async def test_delete_files(self, client: Any, dated_folder: str, below: str) -> None:  # noqa: F811
        from mcp_synology.modules.filestation.operations import delete_files

        file = f"{dated_folder}/f0.txt"
        error = await _error(delete_files(client, paths=[f"{file}/{below}"]))
        assert (error["code"], error["retryable"]) == ("not_found", False), error
        assert error["message"] == (
            f"Delete files refused: {file}/{below}: doesn't exist (DSM error 401): "
            f"{file} is a file. Nothing was deleted."
        )

    @pytest.mark.parametrize("force_parent", [True, False])
    async def test_create_folder(
        self,
        client: Any,
        dated_folder: str,  # noqa: F811
        below: str,
        force_parent: bool,
    ) -> None:
        from mcp_synology.modules.filestation.operations import create_folder

        file = f"{dated_folder}/f0.txt"
        error = await _error(
            create_folder(client, paths=[f"{file}/{below}"], force_parent=force_parent)
        )
        assert (error["code"], error["retryable"]) == ("not_found", False), error
        assert error["message"] == (
            f"Create folder failed: {file}/{below} can't be created: {file} is a file."
        )

    async def test_rename(self, client: Any, dated_folder: str, below: str) -> None:  # noqa: F811
        from mcp_synology.modules.filestation.operations import rename

        file = f"{dated_folder}/f0.txt"
        error = await _error(rename(client, path=f"{file}/{below}", new_name="z"))
        assert (error["code"], error["retryable"]) == ("not_found", False), error
        assert error["message"] == (
            f"Rename failed (DSM error 1200): {file}/{below} doesn't exist on the NAS: "
            f"{file} is a file."
        )

    @pytest.mark.parametrize("create_parents", [True, False])
    async def test_upload_file(
        self,
        client: Any,
        dated_folder: str,  # noqa: F811
        below: str,
        create_parents: bool,
        tmp_path: Path,
    ) -> None:
        from mcp_synology.modules.filestation.transfer import upload_file

        file = f"{dated_folder}/f0.txt"
        local = tmp_path / "up.txt"
        local.write_text("x")
        error = await _error(
            upload_file(
                client,
                local_path=str(local),
                dest_folder=f"{file}/{below}",
                create_parents=create_parents,
                allowed_roots=[tmp_path],
            )
        )
        assert (error["code"], error["retryable"]) == ("not_found", False), error
        assert error["message"] == (
            f"Upload failed: the destination folder {file}/{below} doesn't exist on the NAS: "
            f"{file} is a file."
        )
