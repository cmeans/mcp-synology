"""The golden image's Synology Photos setup (#258).

The bake installs Photos, turns on the user home service, grants the test user the
Photos privilege and makes it a Shared Space manager; ``mcpfresh`` gets no Photos
privilege. These checks keep a broken bake from showing up later as failures all
over the Photos module's tests.

Photos calls are form POSTs with JSON-encoded values and ``api``/``method``/``version``
in the body, the transport the Photos module spec chooses.

Run: ``uv run pytest tests/vdsm -m vdsm --dsm-version 7.1 -k photos_bake``
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from mcp_synology.core.errors import SynologyError
from tests.vdsm.golden_image import load_golden_meta, photos_skip_reason

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

pytestmark = pytest.mark.vdsm


async def _photos(
    client: DsmClient, api: str, method: str, version: int = 1, **params: Any
) -> dict[str, Any]:
    return await client.request(
        api,
        method,
        version=version,
        params={k: json.dumps(v) for k, v in params.items()},
        post_form=True,
    )


@pytest.fixture
async def photos_client(nas_client: Any, dsm_version: str, photos_apis: None) -> DsmClient:
    client: DsmClient = nas_client[0]
    reason = photos_skip_reason(load_golden_meta(dsm_version), client.api_cache)
    if reason is not None:
        pytest.skip(reason)
    return client


async def test_test_user_has_photos(photos_client: DsmClient, dsm_version: str) -> None:
    me = await _photos(photos_client, "SYNO.Foto.UserInfo", "me")
    assert me["name"] == load_golden_meta(dsm_version)["test_user"], me


async def test_personal_space_root_lists(photos_client: DsmClient) -> None:
    root = (await _photos(photos_client, "SYNO.Foto.Browse.Folder", "get", 2, id=0))["folder"]
    assert root["name"] == "/", root
    listed = await _photos(
        photos_client, "SYNO.Foto.Browse.Folder", "list", 2, id=root["id"], offset=0, limit=10
    )
    assert isinstance(listed["list"], list), listed


async def test_shared_space_root_lists(photos_client: DsmClient) -> None:
    root = (await _photos(photos_client, "SYNO.FotoTeam.Browse.Folder", "get", 2, id=1))["folder"]
    assert root["id"] == 1 and root["owner_user_id"] == 0, root
    listed = await _photos(
        photos_client, "SYNO.FotoTeam.Browse.Folder", "list", 2, id=1, offset=0, limit=10
    )
    assert isinstance(listed["list"], list), listed


async def test_fresh_user_has_no_photos_privilege(
    fresh_client: Any, dsm_version: str, photos_apis: None
) -> None:
    client: DsmClient = fresh_client[0]
    if photos_skip_reason(load_golden_meta(dsm_version), client.api_cache):
        pytest.skip("Synology Photos isn't in this golden image")
    with pytest.raises(SynologyError) as exc:
        await _photos(client, "SYNO.Foto.UserInfo", "me")
    assert exc.value.code == 160
