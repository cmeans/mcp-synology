"""Every Photos folder argument goes through `Photos.folder` and `check_folder_path`
(#510).

A structural test keeps a new tool or argument from skipping the registry; a matrix test
runs each registered argument through its tool: a NUL is refused before any folder lookup
(DSM cuts the path there), and a path with a leading-space name is looked up exactly (Photos
reaches it).
"""

from __future__ import annotations

import contextlib
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import mcp_synology.modules.photos as photos_pkg
from mcp_synology.modules import RegisterContext
from mcp_synology.modules.photos.albums import photo_album
from mcp_synology.modules.photos.browse import list_photo_collections, list_photos
from mcp_synology.modules.photos.helpers import FOLDER_PARAMS
from mcp_synology.modules.photos.organize import photo_folder, photo_request
from mcp_synology.modules.photos.tasks import copy_or_move, delete_photo_folders
from mcp_synology.modules.photos.transfer import upload_photos
from tests.modules.photos.dsm import FakePhotos, captured, install, make_client
from tests.path_words import PATHLIKE_WORDS, words

if TYPE_CHECKING:
    from pathlib import Path

    from mcp_synology.core.client import DsmClient

FOLDER = "SYNO.Foto.Browse.Folder"

# Arguments that name a file or folder on this machine, not in Photos.
LOCAL_PARAMS = {"download_photos": {"dest_folder"}, "upload_photos": {"local_paths"}}
# Arguments whose names look like a folder's but aren't one.
NOT_FOLDERS = {
    "merge_people": {"target_id", "target_name"},  # the person to keep
    "copy_photos": {"dest_space"},  # personal or shared
    "move_photos": {"dest_space"},
}

Call = Callable[["DsmClient", str, "Path"], "Awaitable[str]"]


def _copy_move(operation: str, param: str) -> Call:
    def call(c: DsmClient, v: str, _tmp: Path) -> Awaitable[str]:
        kwargs: dict[str, Any] = {"folder_ids": [10], "destination_path": "/dest"}
        if param == "folder_paths":
            kwargs = {"folder_paths": [v], "destination_path": "/dest"}
        else:
            kwargs[param] = v
        return copy_or_move(c, operation=operation, ids=None, **kwargs)

    return call


def _upload(c: DsmClient, v: str, tmp: Path) -> Awaitable[str]:
    photo = tmp / "a.jpg"
    photo.write_bytes(b"\xff\xd8\xff")
    return upload_photos(c, local_paths=[str(photo)], folder_path=v, allowed_roots=[tmp])


# Each registered argument, called through its tool with `value` as that argument.
CALLS: dict[tuple[str, str], Call] = {
    ("list_photos", "folder_path"): lambda c, v, _t: list_photos(c, folder_path=v),
    ("list_photo_collections", "parent_path"): lambda c, v, _t: list_photo_collections(
        c, type="folder", parent_path=v
    ),
    ("photo_folder", "folder_path"): lambda c, v, _t: photo_folder(
        c, action="rename", folder_path=v, name="renamed"
    ),
    ("photo_folder", "parent_path"): lambda c, v, _t: photo_folder(
        c, action="create", parent_path=v, name="new"
    ),
    ("photo_request", "folder_path"): lambda c, v, _t: photo_request(
        c, action="create", subject="s", folder_path=v
    ),
    ("upload_photos", "folder_path"): _upload,
    ("copy_photos", "folder_paths"): _copy_move("copy", "folder_paths"),
    ("copy_photos", "destination_path"): _copy_move("copy", "destination_path"),
    ("move_photos", "folder_paths"): _copy_move("move", "folder_paths"),
    ("move_photos", "destination_path"): _copy_move("move", "destination_path"),
    ("delete_photo_folders", "folder_paths"): lambda c, v, _t: delete_photo_folders(
        c, folder_paths=[v]
    ),
    ("photo_album", "condition"): lambda c, v, _t: photo_album(
        c, action="create_smart", name="smart", condition={"folder_paths": [v]}
    ),
}


def _tools() -> dict[str, Any]:
    server = MCPServer("test-photos")
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=MagicMock())
    photos_pkg.register(
        RegisterContext(
            server=server,
            manager=manager,
            allowed_tools={t.name for t in photos_pkg.MODULE_INFO.tools},
            display_name="test-nas",
        )
    )
    return {name: t.fn for name, t in server._tool_manager._tools.items()}


def test_every_folder_argument_is_registered() -> None:
    tools = _tools()
    unregistered = []
    for name, fn in tools.items():
        for param in inspect.signature(fn).parameters:
            if not words(param) & PATHLIKE_WORDS:
                continue
            known = FOLDER_PARAMS.get(name, set()) | LOCAL_PARAMS.get(name, set())
            if param in known | NOT_FOLDERS.get(name, set()):
                continue
            if param.endswith(("_id", "_ids")):
                continue  # a folder's ID (#617): no path to check

            unregistered.append(f"{name}.{param}")
    assert not unregistered, f"folder arguments not in FOLDER_PARAMS: {unregistered}"
    registered = {(name, p) for name, params in FOLDER_PARAMS.items() for p in params}
    for name, param in registered:
        assert param in inspect.signature(tools[name]).parameters, (name, param)
    assert set(CALLS) == registered


def _error(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    text = str(exc.value)
    return json.loads(text[text.index("{") :])["error"]  # type: ignore[no-any-return]


def _fake() -> FakePhotos:
    """Answers every lookup a call makes before or after its folder argument."""
    folder = {"folder": {"id": 5, "name": "/x/ foo", "parent": 1}}
    fake = FakePhotos().on(FOLDER, "get", 2, folder)
    fake.on("SYNO.Foto.PhotoRequest", "list", 1, {"list": []})
    fake.on("SYNO.Foto.Browse.Album", "list", 2, {"list": []})
    fake.on("SYNO.Foto.UserInfo", "me", 1, captured("userinfo-me"))
    return fake


@pytest.mark.parametrize(("tool", "param"), list(CALLS))
@respx.mock
async def test_a_nul_is_refused_before_the_folder_lookup(
    tool: str, param: str, tmp_path: Path
) -> None:
    fake = _fake()
    install(fake)
    client = make_client()
    value = "/x/foo\x00y"
    async with client:
        with pytest.raises(ToolError) as exc:
            await CALLS[tool, param](client, value, tmp_path)
    error = _error(exc)
    assert (error["code"], error["param"]) == ("invalid_parameter", param)
    assert "NUL" in error["message"]
    assert not [c for c in fake.of(FOLDER, "get") if "\x00" in str(c.params)]


@pytest.mark.parametrize(("tool", "param"), list(CALLS))
@respx.mock
async def test_a_leading_space_name_is_looked_up_exactly(
    tool: str, param: str, tmp_path: Path
) -> None:
    fake = _fake()
    install(fake)
    client = make_client()
    async with client:
        # What the call does after the lookup isn't this test's concern.
        with contextlib.suppress(Exception):
            await CALLS[tool, param](client, "  /x/ foo/", tmp_path)
    names = [c.params.get("name") for c in fake.of(FOLDER, "get")]
    assert "/x/ foo" in names, names


@pytest.mark.parametrize("value", ["", "  "])
@pytest.mark.parametrize(("tool", "param"), sorted(CALLS))
@respx.mock
async def test_an_empty_path_is_refused_everywhere(
    tool: str, param: str, value: str, tmp_path: Path
) -> None:
    """QA round 1 on #615: an empty path was the root, so `parent_path=""` created in the
    root and `destination_path=" "` moved there. Every registered path argument refuses one
    before any folder lookup."""
    fake = _fake()
    install(fake)
    client = make_client()
    async with client:
        with pytest.raises(ToolError) as exc:
            await CALLS[(tool, param)](client, value, tmp_path)
    err = _error(exc)
    assert err["code"] == "invalid_parameter", err
    # Refused as empty (photo_request's create says the path is required, which it checks
    # first), never looked up as the root.
    assert "`/` for the space's root" in err["message"] or "needs folder_path" in err["message"]
    assert all(c.params != {"id": 0} for c in fake.of(FOLDER)), fake.calls
