"""What DSM does when two sources share a landing: the facts `refuse_same_names` and the skip
report rest on (#495, #580). They go through the operations with the landing checks skipped
(the fixture below).

Run: ``uv run pytest tests/vdsm -m vdsm --no-cov --dsm-version 7.2.2 -k same_names``
"""

from __future__ import annotations

import contextlib
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.filestation.listing import list_files, list_recycle_bin
from mcp_synology.modules.filestation.operations import (
    copy_files,
    create_folder,
    delete_files,
    move_files,
)
from mcp_synology.modules.filestation.transfer import download_file, upload_file

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mcp_synology.core.client import DsmClient

pytestmark = pytest.mark.vdsm


@pytest.fixture(autouse=True)
def _behind_the_landing_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests probe what DSM does when sources share a landing, so they run the
    operations with the landing checks skipped. Since #684 those checks run in the operation's
    plan (`paths.plan_landings`), not in the tool layer, so a plain call would be refused first.
    The plan still lists the sources, so `run_batch`'s check that it ran them holds."""
    from mcp_synology.modules.filestation import operations, paths

    def unchecked(tool: str, given: Any, resolved: Any, landings: Any, **kwargs: Any) -> Any:
        return paths.plan_landings(tool, given, resolved, None, overwrite=False)

    monkeypatch.setattr(operations, "plan_landings", unchecked)


class Tree:
    """A folder of its own under ``/home``, the only place on vdsm with a recycle bin."""

    def __init__(self, client: DsmClient, top: str, tmp_path: Path) -> None:
        self.client = client
        self.top = f"/home/{top}"
        self.name = top
        self.tmp_path = tmp_path

    def at(self, relative: str) -> str:
        return f"{self.top}/{relative}"

    async def file(self, relative: str, text: str) -> str:
        folder, _, name = relative.rpartition("/")
        await create_folder(self.client, paths=[self.at(folder)])
        local = self.tmp_path / "up" / uuid.uuid4().hex / name
        local.parent.mkdir(parents=True)
        local.write_text(text)
        await upload_file(
            self.client,
            local_path=str(local),
            dest_folder=self.at(folder),
            allowed_roots=[self.tmp_path],
        )
        return self.at(relative)

    async def text(self, relative: str) -> str:
        down = self.tmp_path / "down" / uuid.uuid4().hex
        down.mkdir(parents=True)
        await download_file(
            self.client,
            path=self.at(relative),
            dest_folder=str(down),
            allowed_roots=[self.tmp_path],
        )
        return (down / relative.rpartition("/")[2]).read_text()

    async def listing(self, relative: str) -> str:
        return await list_files(self.client, path=self.at(relative))


@pytest.fixture
async def tree(nas_client: Any, tmp_path: Path) -> AsyncIterator[Tree]:
    client: DsmClient = nas_client[0]
    top = f"t495-{uuid.uuid4().hex[:8]}"
    made = Tree(client, top, tmp_path)
    await create_folder(client, paths=[made.at("d")])
    try:
        yield made
    finally:
        for paths in ([f"/home/{top}"], [f"/home/#recycle/{top}"]):
            with contextlib.suppress(ToolError):
                await delete_files(client, paths=paths, recycle_bin_status={})


async def test_the_second_source_replaces_the_first_with_no_copy_in_the_bin(tree: Tree) -> None:
    first = await tree.file("a/x.txt", "first")
    second = await tree.file("b/x.txt", "second")
    out = await move_files(
        tree.client, paths=[first, second], dest_folder=tree.at("d"), overwrite=True
    )
    assert "Moved 2 item(s)" in out
    assert await tree.text("d/x.txt") == "second"
    assert "(0 found in" in await list_recycle_bin(tree.client, share="home", pattern="x.txt")


async def test_names_differing_only_in_case_stay_two_files_here(tree: Tree) -> None:
    """On this file system only: why the guard's case rule is caution, not this behavior."""
    upper = await tree.file("a/X.TXT", "upper")
    lower = await tree.file("b/x.txt", "lower")
    await move_files(tree.client, paths=[upper, lower], dest_folder=tree.at("d"), overwrite=True)
    assert await tree.text("d/X.TXT") == "upper"
    assert await tree.text("d/x.txt") == "lower"


async def test_overwrite_false_skips_the_second_and_warns_against_a_retry(tree: Tree) -> None:
    """#580: the reply used to say `Pass overwrite=true`, which replaced the moved file."""
    first = await tree.file("a/x.txt", "first")
    second = await tree.file("b/x.txt", "second")
    out = await move_files(
        tree.client, paths=[first, second], dest_folder=tree.at("d"), overwrite=False
    )
    assert "1 skipped" in out
    assert "Don't retry these with overwrite=true" in out
    assert "Pass overwrite=true" not in out
    assert await tree.text("d/x.txt") == "first"
    assert await tree.text("b/x.txt") == "second"


async def test_two_folders_merge_and_overwrite_false_replaces_nothing(tree: Tree) -> None:
    """The refusal's advice: one call with overwrite=false is the safe merge."""
    a_same = await tree.file("a/n/same.txt", "from a")
    await tree.file("a/n/only-a.txt", "a")
    await tree.file("b/n/same.txt", "from b")
    await tree.file("b/n/only-b.txt", "b")
    folders = [a_same.rpartition("/")[0], tree.at("b/n")]
    out = await move_files(tree.client, paths=folders, dest_folder=tree.at("d"), overwrite=False)
    assert "Don't retry these with overwrite=true" in out
    assert await tree.text("d/n/same.txt") == "from a"
    assert await tree.text("d/n/only-a.txt") == "a"
    assert await tree.text("d/n/only-b.txt") == "b"
    assert await tree.text("b/n/same.txt") == "from b"


async def test_a_copy_replaces_the_first_too(tree: Tree) -> None:
    first = await tree.file("a/x.txt", "first")
    second = await tree.file("b/x.txt", "second")
    out = await copy_files(
        tree.client, paths=[first, second], dest_folder=tree.at("d"), overwrite=True
    )
    assert "Copied 2 item(s)" in out
    assert await tree.text("d/x.txt") == "second"


async def test_names_differing_only_in_case_both_land_without_overwrite(tree: Tree) -> None:
    upper = await tree.file("a/X.TXT", "upper")
    lower = await tree.file("b/x.txt", "lower")
    await move_files(tree.client, paths=[upper, lower], dest_folder=tree.at("d"), overwrite=False)
    assert await tree.text("d/X.TXT") == "upper"
    assert await tree.text("d/x.txt") == "lower"


async def test_two_folders_with_overwrite_replace_same_named_items(tree: Tree) -> None:
    """Why two folders of one name are refused: the merge loses the first's copy, not in the bin."""
    await tree.file("a/n/same.txt", "from a")
    await tree.file("a/n/only-a.txt", "a")
    await tree.file("b/n/same.txt", "from b")
    await tree.file("b/n/only-b.txt", "b")
    await move_files(
        tree.client,
        paths=[tree.at("a/n"), tree.at("b/n")],
        dest_folder=tree.at("d"),
        overwrite=True,
    )
    assert await tree.text("d/n/same.txt") == "from b"
    assert await tree.text("d/n/only-a.txt") == "a"
    assert await tree.text("d/n/only-b.txt") == "b"
    assert "(0 found in" in await list_recycle_bin(tree.client, share="home", pattern="same.txt")


@pytest.mark.parametrize(
    ("operation", "folder_first", "overwrite", "code"),
    [
        # A folder landing on a file fails under either overwrite value.
        (copy_files, False, False, "1004"),
        (copy_files, False, True, "1004"),
        (move_files, False, False, "401"),
        (move_files, False, True, "401"),
        # A file landing on a folder is skipped, or fails with overwrite=true.
        (copy_files, True, False, None),
        (copy_files, True, True, "1004"),
        (move_files, True, False, None),
        (move_files, True, True, "1004"),
    ],
)
async def test_a_file_and_a_folder_of_one_name(
    tree: Tree, operation: Any, folder_first: bool, overwrite: bool, code: str | None
) -> None:
    """The exception to `overwrite=false` not raising errors for a conflict."""
    file_source = await tree.file("a/n", "a file")
    await tree.file("b/n/child.txt", "in a folder")
    sources = [file_source, tree.at("b/n")]
    call = operation(
        tree.client,
        paths=sources[::-1] if folder_first else sources,
        dest_folder=tree.at("d"),
        overwrite=overwrite,
    )
    if code is None:
        assert "skipped" in await call
    else:
        with pytest.raises(ToolError, match=f"DSM error {code}"):
            await call


@pytest.mark.parametrize("overwrite", [False, True])
async def test_a_folder_landing_on_a_file_already_there_fails(tree: Tree, overwrite: bool) -> None:
    await tree.file("d/n", "there")
    await tree.file("b/n/child.txt", "in a folder")
    with pytest.raises(ToolError, match="DSM error 1004"):
        await copy_files(
            tree.client, paths=[tree.at("b/n")], dest_folder=tree.at("d"), overwrite=overwrite
        )


async def test_a_source_inside_where_another_lands_is_replaced(tree: Tree) -> None:
    """QA round 1 on #575: both counted as moved, and the second's data is gone."""
    folder_child = await tree.file("s/n/q", "from the folder")
    inner = await tree.file("d/n/q", "the inner source")
    out = await move_files(
        tree.client,
        paths=[folder_child.rpartition("/")[0], inner],
        dest_folder=tree.at("d"),
        overwrite=True,
    )
    assert "Moved 2 item(s)" in out
    assert await tree.text("d/q") == "from the folder"
    # /d/n itself stays, emptied: the merged folder's q has moved on over the inner source.
    assert " q" not in await tree.listing("d/n")
    assert "(0 found in" in await list_recycle_bin(tree.client, share="home", pattern="q")
