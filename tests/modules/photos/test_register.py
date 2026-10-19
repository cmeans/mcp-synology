"""Photos module registration: tools, tiers, annotations, titles, settings (#258)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import ValidationError

from mcp_synology.modules import PermissionTier, RegisterContext
from mcp_synology.modules.photos import MODULE_INFO, PhotosSettings, register


def _registered(allowed: set[str] | None = None, settings: dict | None = None) -> dict:
    server = MCPServer("test-photos")
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=MagicMock())
    register(
        RegisterContext(
            server=server,
            manager=manager,
            allowed_tools=allowed if allowed is not None else {t.name for t in MODULE_INFO.tools},
            settings_dict=settings or {},
            display_name="test-nas",
        )
    )
    return server._tool_manager._tools  # type: ignore[no-any-return]


_READ = {"list_photos", "list_photo_collections", "get_photo", "download_photos"}
_WRITE = {
    "photo_album",
    "photo_share_link",
    "photo_folder",
    "tag_photos",
    "edit_photo",
    "edit_person",
    "photo_request",
    "merge_people",
    "delete_photo_albums",
    "upload_photos",
    "copy_photos",
    "move_photos",
    "photo_task",
    "delete_photos",
    "delete_photo_folders",
}


def test_tools_registered_with_tiers() -> None:
    assert set(_registered()) == _READ | _WRITE
    tiers = {t.name: t.permission_tier for t in MODULE_INFO.tools}
    assert {n for n, t in tiers.items() if t is PermissionTier.READ} == _READ
    assert {n for n, t in tiers.items() if t is PermissionTier.WRITE} == _WRITE


def test_every_write_is_destructive() -> None:
    """Each write can lose a value, a membership, a share setting or a person (#183)."""
    tools = _registered()
    for name in _WRITE - {"upload_photos"}:
        assert tools[name].annotations.destructive_hint is True, name
    # Uploads never replace anything: a duplicate is renamed or skipped.
    assert tools["upload_photos"].annotations.destructive_hint is False
    idempotent = {n for n in _WRITE if tools[n].annotations.idempotent_hint}
    assert idempotent == {"photo_folder", "edit_person"}
    assert {n: tools[n].title for n in _WRITE if tools[n].title} == {
        "photo_folder": "Photos: Folder",
        "upload_photos": "Photos: Upload",
        "copy_photos": "Photos: Copy",
        "move_photos": "Photos: Move",
        "delete_photos": "Photos: Delete items",
        "delete_photo_folders": "Photos: Delete folders",
    }


def test_annotations_and_titles() -> None:
    tools = _registered()
    assert tools["list_photos"].annotations.read_only_hint is True
    download = tools["download_photos"]
    assert download.annotations.read_only_hint is False
    assert download.annotations.destructive_hint is True
    assert download.title == "Photos: Download"
    assert tools["get_photo"].title is None


def test_only_allowed_tools() -> None:
    assert set(_registered({"get_photo"})) == {"get_photo"}


def test_only_browse_item_and_folder_required() -> None:
    required = {r.api_name for r in MODULE_INFO.required_apis if not r.optional}
    assert required == {"SYNO.Foto.Browse.Item", "SYNO.Foto.Browse.Folder"}


def test_task_timeout_bounds() -> None:
    assert PhotosSettings().task_timeout == 30
    with pytest.raises(ValidationError):
        PhotosSettings(task_timeout=601)


def test_settings_reject_relative_local_paths() -> None:
    with pytest.raises(ValidationError):
        PhotosSettings(allowed_local_paths=["photos"])
    assert PhotosSettings(allowed_local_paths=["~/Pictures"]).allowed_local_paths == ["~/Pictures"]


async def test_handlers_reach_the_client(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Each registered function calls through to its handler with the NAS client."""
    tools = _registered(settings={"allowed_local_paths": [str(tmp_path)]})
    from mcp.server.mcpserver.exceptions import ToolError

    for name, args in [
        ("list_photos", {"space": "bogus"}),
        ("list_photo_collections", {"type": "bogus"}),
        ("get_photo", {"ids": []}),
        ("download_photos", {"ids": [], "dest_folder": str(tmp_path)}),
        ("photo_album", {"action": "bogus"}),
        ("photo_share_link", {"action": "bogus", "album": "1"}),
        ("photo_folder", {"action": "bogus"}),
        ("tag_photos", {"action": "bogus", "ids": [1], "tags": ["x"]}),
        ("edit_photo", {"ids": []}),
        ("edit_person", {"action": "bogus", "person": "1"}),
        ("photo_request", {"action": "bogus"}),
        ("merge_people", {"target": "1", "merge": []}),
        ("delete_photo_albums", {"albums": []}),
        ("upload_photos", {"local_paths": []}),
        ("copy_photos", {"destination": "/"}),
        ("move_photos", {"destination": "/"}),
        ("photo_task", {"action": "bogus"}),
        ("delete_photos", {"ids": []}),
        ("delete_photo_folders", {"folders": []}),
    ]:
        with pytest.raises(ToolError):
            await tools[name].fn(**args)
