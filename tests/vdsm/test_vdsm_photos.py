"""The Photos module's tools against Synology Photos on vdsm (#258).

The golden image (bake v5) has Photos installed, the test user (``nas_client``) with
the Photos privilege and the Shared Space manager role, and ``mcpfresh`` with no
Photos privilege. Each test works in its own folder (``/t258-<random>``), seeded from
``tests/vdsm/photos_seed/`` (small generated JPEGs with EXIF dates, camera and GPS, and
a 3 s video), and deletes it afterwards.

Run: ``uv run pytest tests/vdsm -m vdsm --dsm-version 7.1 -k vdsm_photos``
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import SynologyError
from mcp_synology.modules.photos.browse import get_photo, list_photo_collections, list_photos
from mcp_synology.modules.photos.helpers import Photos, Space, encode
from mcp_synology.modules.photos.transfer import download_photos
from tests.vdsm.golden_image import load_golden_meta, photos_skip_reason

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mcp_synology.core.client import DsmClient

pytestmark = pytest.mark.vdsm

SEED = Path(__file__).parent / "photos_seed"
JPEGS = ["beach-sunset.jpg", "city-night.jpg", "forest-trail.jpg"]
VIDEO = "clip-test.mp4"
INDEX_TIMEOUT = 120.0


def _error(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    text = str(exc.value)
    return json.loads(text[text.index("{") :])["error"]  # type: ignore[no-any-return]


@pytest.fixture
async def photos(nas_client: Any, dsm_version: str, photos_apis: None) -> DsmClient:
    client: DsmClient = nas_client[0]
    reason = photos_skip_reason(load_golden_meta(dsm_version), client.api_cache)
    if reason is not None:
        pytest.skip(reason)
    return client


def _is_191(client: DsmClient) -> bool:
    info = client.api_cache.get("SYNO.Foto.Browse.Item")
    return info is not None and info.max_version >= 7


async def _wait_for(probe: Any, what: str, timeout: float = INDEX_TIMEOUT) -> Any:
    """Poll ``probe()`` until it returns something truthy (Photos indexes in the background)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = await probe()
        if result:
            return result
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        await asyncio.sleep(2)


async def _upload(client: DsmClient, ph: Photos, folder_id: int, file: str) -> dict[str, Any]:
    """One raw upload, retried once like upload_photos (vdsm answers 623 right after boot)."""
    fields = encode(
        {
            "duplicate": "rename",
            "name": file,
            "mtime": int((SEED / file).stat().st_mtime),
            "target_folder_id": folder_id,
        }
    )
    for attempt in (1, 2):
        try:
            return await client.upload_multipart(
                ph.api("Upload.Item"),
                "upload_to_folder",
                1,
                fields,
                file_path=SEED / file,
                filename=file,
            )
        except SynologyError as e:
            if attempt == 2 or e.code in (620, 621):
                raise
            await asyncio.sleep(2)
    raise AssertionError("unreachable")


@asynccontextmanager
async def seeded(
    client: DsmClient, files: list[str], space: Space = "personal"
) -> AsyncIterator[tuple[str, dict[str, int]]]:
    """A fresh folder holding ``files``, indexed; yields its path and the items' IDs.

    Seeded with raw API calls, not the module's tools, so a broken upload_photos or
    photo_folder doesn't fail every test.
    """
    name = f"t258-{uuid.uuid4().hex[:8]}"
    ph = Photos(client, space, "seed")
    root = (await ph.call("Browse.Folder", "get", {"id": 0}, version=2))["folder"]
    folder = (await ph.call("Browse.Folder", "create", {"target_id": root["id"], "name": name}))[
        "folder"
    ]
    try:
        ids: dict[str, int] = {}
        for file in files:
            data = await _upload(client, ph, int(folder["id"]), file)
            ids[file] = int(data["id"])

        async def indexed() -> bool:
            if not ids:
                return True
            data = await ph.call(
                "Browse.Item",
                "get",
                {"id": list(ids.values()), "additional": ["resolution"]},
                version=2,
            )
            items = data.get("list", [])
            return len(items) == len(ids) and all(
                (i.get("additional") or {}).get("resolution", {}).get("width") for i in items
            )

        await _wait_for(indexed, "the uploads to be indexed")
        yield f"/{name}", ids
    finally:
        with contextlib.suppress(SynologyError):
            await ph.call("Browse.Folder", "delete", {"id": [int(folder["id"])]})


# -- READ: list_photos, list_photo_collections, get_photo, download_photos --------------


async def test_list_photos_views(photos: DsmClient) -> None:
    async with seeded(photos, [*JPEGS, VIDEO]) as (path, ids):
        out = await list_photos(photos, folder=path)
        assert "4 item(s) total" in out and VIDEO in out

        out = await list_photos(photos, folder=path, sort_by="filename", sort_direction="asc")
        rows = re.findall(r"^  \d+\s+(\S+)", out, re.M)
        assert rows == sorted([*JPEGS, VIDEO]), out

        out = await list_photos(photos, folder=path, type="video")
        assert "1 item(s) total" in out and VIDEO in out

        # Taken dates come from the seeds' EXIF: beach 2024-07-04, city 2023-12-24.
        out = await list_photos(photos, folder=path, start_date="2024-01-01", type="photo")
        assert "beach-sunset.jpg" in out and "city-night.jpg" not in out, out

        ph = Photos(photos, "personal", "test")
        await ph.call("Browse.Item", "set", {"id": [ids["city-night.jpg"]], "rating": 5}, version=2)

        async def rated() -> str | None:
            text = await list_photos(photos, folder=path, min_rating=4)
            return text if "1 item(s) total" in text else None

        assert "city-night.jpg" in await _wait_for(rated, "the rating")


async def test_tag_and_keyword(photos: DsmClient) -> None:
    async with seeded(photos, JPEGS) as (path, ids):
        tag = f"t258tag{uuid.uuid4().hex[:6]}"
        ph = Photos(photos, "personal", "test")
        made = await ph.call("Browse.GeneralTag", "create", {"name": tag})
        await ph.call(
            "Browse.Item", "add_tag", {"id": [ids["forest-trail.jpg"]], "tag": [made["tag"]["id"]]}
        )
        out = await list_photos(photos, tag=tag)
        assert "1 item(s) total" in out and "forest-trail.jpg" in out

        async def found() -> str | None:
            text = await list_photos(photos, keyword=tag)
            return text if "forest-trail.jpg" in text else None

        await _wait_for(found, "search to find the tag")


async def test_places(photos: DsmClient) -> None:
    async with seeded(photos, JPEGS):

        async def places() -> str | None:
            text = await list_photo_collections(photos, type="place", name="Chicago")
            return text if "Chicago" in text else None

        text = await _wait_for(places, "geocoding")
        place = int(re.search(r"^  (\d+)\s+Chicago", text, re.M).group(1))  # type: ignore[union-attr]
        out = await list_photos(photos, place=place)
        assert "beach-sunset.jpg" in out
        with pytest.raises(ToolError):
            await list_photos(photos, place=place, type="photo")


async def test_collections(photos: DsmClient) -> None:
    async with seeded(photos, JPEGS[:1]) as (path, _ids):
        assert path in await list_photo_collections(photos, type="folder")
        assert "People" in await list_photo_collections(photos, type="person")
        assert "Albums" in await list_photo_collections(photos, type="album")
        assert "shared with you" in await list_photo_collections(photos, type="shared_with_me")
        if _is_191(photos):
            assert "Concepts" in await list_photo_collections(photos, type="concept")
        else:
            with pytest.raises(ToolError) as exc:
                await list_photo_collections(photos, type="concept")
            assert _error(exc)["code"] == "not_supported"


async def test_get_photo(photos: DsmClient) -> None:
    async with seeded(photos, JPEGS[:1]) as (path, ids):
        out = await get_photo(photos, ids=[ids["beach-sunset.jpg"], 999999], exif=True)
        assert f"Folder:       {path} (personal space)" in out
        assert "2024-07-04 19:30:00 UTC" in out
        assert "SeedCam 1" in out and "41.8781" in out
        assert "GPS Latitude" in out
        assert out.endswith("Not found: 999999")


async def test_download_photos(photos: DsmClient, tmp_path: Path) -> None:
    async with seeded(photos, JPEGS[:1]) as (_path, ids):
        item = ids["beach-sunset.jpg"]
        out = await download_photos(
            photos, ids=[item], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
        )
        assert "Downloaded 1 of 1" in out
        assert (tmp_path / "beach-sunset.jpg").read_bytes() == (
            SEED / "beach-sunset.jpg"
        ).read_bytes()

        async def preview() -> str | None:
            text = await download_photos(
                photos, ids=[item], size="sm", dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
            return text if "Downloaded 1 of 1" in text else None

        await _wait_for(preview, "the sm preview")
        assert (tmp_path / "beach-sunset_sm.jpg").read_bytes()[:2] == b"\xff\xd8"


async def test_favorites_by_version(photos: DsmClient) -> None:
    async with seeded(photos, JPEGS[:2]) as (path, ids):
        if not _is_191(photos):
            with pytest.raises(ToolError) as exc:
                await list_photos(photos, folder=path, favorite=True)
            assert _error(exc)["code"] == "not_supported"
            return
        ph = Photos(photos, "personal", "test")
        await ph.call(
            "Browse.Item",
            "set_favorite",
            {"id": [ids["beach-sunset.jpg"]], "favorite": True},
            version=7,
        )
        out = await list_photos(photos, folder=path, favorite=True)
        assert "1 item(s) total" in out and "beach-sunset.jpg" in out
        assert "favorite: yes" in await get_photo(photos, ids=[ids["beach-sunset.jpg"]])


async def test_no_photos_privilege(fresh_client: Any, dsm_version: str, photos_apis: None) -> None:
    client: DsmClient = fresh_client[0]
    if photos_skip_reason(load_golden_meta(dsm_version), client.api_cache):
        pytest.skip("Synology Photos isn't in this golden image")
    with pytest.raises(ToolError) as exc:
        await list_photos(client)
    err = _error(exc)
    assert err["code"] == "permission_denied"
    assert "160" in err["message"] and "Application Privileges" in err["suggestion"]


async def test_shared_space(photos: DsmClient) -> None:
    async with seeded(photos, JPEGS[:1], space="shared") as (path, ids):
        out = await list_photos(photos, folder=path, space="shared")
        assert "beach-sunset.jpg" in out
        assert "(Shared Space)" in await get_photo(photos, ids=list(ids.values()), space="shared")
        # IDs belong to a space: the personal API doesn't know a Shared Space item.
        assert "Not found" in await get_photo(photos, ids=list(ids.values()))
