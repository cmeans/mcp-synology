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
}


def test_tools_registered_with_tiers() -> None:
    assert set(_registered()) == _READ | _WRITE
    tiers = {t.name: t.permission_tier for t in MODULE_INFO.tools}
    assert {n for n, t in tiers.items() if t is PermissionTier.READ} == _READ
    assert {n for n, t in tiers.items() if t is PermissionTier.WRITE} == _WRITE


def test_every_write_is_destructive() -> None:
    """Each write can lose a value, a membership, a share setting or a person (#183)."""
    tools = _registered()
    for name in _WRITE:
        assert tools[name].annotations.destructive_hint is True, name
    idempotent = {n for n in _WRITE if tools[n].annotations.idempotent_hint}
    assert idempotent == {"photo_folder", "edit_photo", "edit_person"}
    assert tools["photo_folder"].title == "Photos: Folder"


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
    ]:
        with pytest.raises(ToolError):
            await tools[name].fn(**args)


def test_shipped_tools_match_the_spec() -> None:
    """The spec check of tests/test_module_specs.py for the tools shipped so far.

    That test needs every spec tool in code, so it applies once the module is in the
    server's registry (the last #258 part); until then this checks each shipped tool.
    """
    import inspect
    import re
    from pathlib import Path

    from mcp_synology.modules import photos
    from tests.test_module_specs import (
        _DESCRIPTION,
        _TIER,
        SPEC_DIR,
        _normalize,
        _registered,
        _sections,
        _spec_params,
    )

    sections = _sections((Path(SPEC_DIR) / "photos-module-spec.md").read_text())
    registered = _registered(photos)
    for info in MODULE_INFO.tools:
        section = sections[info.name]
        tier = _TIER.search(section)
        assert tier is not None and tier.group(1).upper() == info.permission_tier.name
        desc = _DESCRIPTION.search(section)
        assert desc is not None
        assert _normalize(re.sub(r"^>\s?", "", desc.group(1), flags=re.M)) == _normalize(
            info.description
        ), info.name
        params = set(inspect.signature(registered[info.name].fn).parameters)
        assert params == _spec_params(section), info.name
