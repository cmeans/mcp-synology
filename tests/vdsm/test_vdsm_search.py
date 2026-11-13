"""search_files against virtual-dsm: date-filter bounds and a search with no filters (#306).

Run with: uv run pytest -m vdsm -v tests/vdsm/test_vdsm_search.py
"""

from __future__ import annotations

import os
import shlex
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from tests.vdsm.conftest import _ssh
from tests.vdsm.golden_image import load_golden_meta

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.vdsm.container import VirtualDsmContainer

pytestmark = pytest.mark.vdsm

_BASE = 1_700_000_000  # 2023-11-14 22:13:20 UTC
_T2 = _BASE + 200


def _iso(t: int) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def dated_folder(vdsm_container: VirtualDsmContainer, dsm_version: str) -> Iterator[str]:
    """f0..f4 modified at _BASE + 0, 100, … 400 s, and g at _T2 + 0.9 s (set over SSH)."""
    password = str(load_golden_meta(dsm_version).get("admin_password", ""))
    folder = f"/testshare/mcp306-{os.urandom(3).hex()}"
    on_volume = f"/volume1{folder}"
    files = [f"printf x > f{i}.txt && touch -d @{_BASE + 100 * i} f{i}.txt" for i in range(5)]
    cmd = " && ".join(
        [f"mkdir -p {on_volume}", f"cd {on_volume}", *files, "printf x > g.txt"]
        + [f"touch -d @{_T2}.9 g.txt", f"chmod -R a+r {on_volume}"]
    )
    try:
        rc, out = _ssh(vdsm_container, password, f"sh -c {shlex.quote(cmd)}", 60)
        assert rc == 0, out
        # g's fraction must really be there, or the T+0.9 s check below proves nothing.
        rc, out = _ssh(vdsm_container, password, f"stat -c %y {on_volume}/g.txt", 30, sudo=False)
        assert rc == 0 and ":40.9" in out, out
        yield folder
    finally:
        _ssh(vdsm_container, password, f"rm -rf {on_volume}", 60)


def _names(result: str) -> list[str]:
    return sorted(
        word
        for line in result.splitlines()
        for word in line.split()
        if word.endswith(".txt") and "/" not in word  # the name column, not the path
    )


class TestVdsmSearchFilters:
    async def test_no_filters_lists_everything(self, nas_client: Any, dated_folder: str) -> None:
        """With filetype=all and nothing to match on, DSM refuses the search as "Search
        folder access denied" (error 600, for any account); search_files sends pattern "*"
        instead (#306). Run as the non-admin test user, the default persona."""
        from mcp_synology.modules.filestation.search import search_files

        result = await search_files(nas_client[0], folder_path=dated_folder)
        assert _names(result) == ["f0.txt", "f1.txt", "f2.txt", "f3.txt", "f4.txt", "g.txt"]

    async def test_mtime_from_is_inclusive_and_mtime_to_exclusive(
        self, admin_client: Any, dated_folder: str
    ) -> None:
        """#306: mtime_from matches a file modified at exactly that second; one at T+0.9 s
        counts as T (it doesn't match T+1); mtime_to leaves out a file at exactly T."""
        from mcp_synology.modules.filestation.search import search_files

        client = admin_client[0]
        since = await search_files(client, folder_path=dated_folder, mtime_from=_iso(_T2))
        assert _names(since) == ["f2.txt", "f3.txt", "f4.txt", "g.txt"]
        after = await search_files(client, folder_path=dated_folder, mtime_from=_iso(_T2 + 1))
        assert _names(after) == ["f3.txt", "f4.txt"]
        before = await search_files(client, folder_path=dated_folder, mtime_to=_iso(_T2))
        assert _names(before) == ["f0.txt", "f1.txt"]


class TestVdsmFolderChecks:
    """#517: a missing folder, a padded one and a file where a folder is expected got
    misleading DSM answers (600 "access denied", a lost task the reply called retryable, List's
    401 "Unknown error"). search_files and list_files now check with getinfo first, so each is
    named for what it is. Run as the non-admin test user and as admin."""

    @pytest.fixture(params=["nas_client", "admin_client"])
    def client(self, request: pytest.FixtureRequest) -> Any:
        return request.getfixturevalue(request.param)[0]

    @staticmethod
    def _error(exc: pytest.ExceptionInfo[Any]) -> dict[str, Any]:
        import json

        return json.loads(str(exc.value))["error"]  # type: ignore[no-any-return]

    @pytest.mark.parametrize("suffix", ["/gone", " "])
    async def test_search_a_missing_folder(
        self, client: Any, dated_folder: str, suffix: str
    ) -> None:
        from mcp.server.mcpserver.exceptions import ToolError

        from mcp_synology.core.display import shown
        from mcp_synology.modules.filestation.search import search_files

        missing = dated_folder + suffix
        with pytest.raises(ToolError) as exc:
            await search_files(client, folder_path=missing, pattern="x")
        error = self._error(exc)
        assert (error["code"], error["retryable"]) == ("not_found", False), error
        assert (
            error["message"] == f"Search files failed: {shown(missing)} doesn't exist on the NAS."
        )

    async def test_search_a_file(self, client: Any, dated_folder: str) -> None:
        from mcp.server.mcpserver.exceptions import ToolError

        from mcp_synology.modules.filestation.search import search_files

        with pytest.raises(ToolError) as exc:
            await search_files(client, folder_path=f"{dated_folder}/f0.txt", pattern="x")
        error = self._error(exc)
        assert (error["code"], error["retryable"]) == ("invalid_parameter", False), error
        assert "is a file, not a folder" in error["message"]

    @pytest.mark.parametrize("below", ["child", "x/y/child"])  # deep: 401 above it too
    async def test_list_a_path_under_a_file(
        self, client: Any, dated_folder: str, below: str
    ) -> None:
        from mcp.server.mcpserver.exceptions import ToolError

        from mcp_synology.modules.filestation.listing import list_files

        with pytest.raises(ToolError) as exc:
            await list_files(client, path=f"{dated_folder}/f0.txt/{below}")
        error = self._error(exc)
        # getinfo answers 401 for it too; its parent is a file, so it can't exist.
        assert (error["code"], error["retryable"]) == ("not_found", False), error
        assert error["message"] == (
            f"List files failed: {dated_folder}/f0.txt/{below} doesn't exist on the NAS: "
            f"{dated_folder}/f0.txt is a file."
        ), error

    @pytest.mark.parametrize("below", ["child", "x/y/child"])
    async def test_search_a_path_under_a_file(
        self, client: Any, dated_folder: str, below: str
    ) -> None:
        from mcp.server.mcpserver.exceptions import ToolError

        from mcp_synology.modules.filestation.search import search_files

        with pytest.raises(ToolError) as exc:
            await search_files(client, folder_path=f"{dated_folder}/f0.txt/{below}", pattern="x")
        error = self._error(exc)
        assert (error["code"], error["retryable"]) == ("not_found", False), error
        assert error["message"] == (
            f"Search files failed: {dated_folder}/f0.txt/{below} doesn't exist on the NAS: "
            f"{dated_folder}/f0.txt is a file."
        ), error
