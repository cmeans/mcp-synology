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
import os
import re
import shlex
import struct
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import SynologyError
from mcp_synology.modules.photos.albums import delete_photo_albums, photo_album, photo_share_link
from mcp_synology.modules.photos.browse import get_photo, list_photo_collections, list_photos
from mcp_synology.modules.photos.helpers import Photos, Space, encode
from mcp_synology.modules.photos.organize import (
    edit_photo,
    photo_folder,
    photo_request,
    tag_photos,
)
from mcp_synology.modules.photos.tasks import (
    copy_or_move,
    delete_photo_folders,
    delete_photos,
    photo_task,
)
from mcp_synology.modules.photos.transfer import download_photos, upload_photos
from tests.vdsm.conftest import _ssh
from tests.vdsm.golden_image import load_golden_meta, photos_skip_reason

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from mcp_synology.core.client import DsmClient
    from tests.vdsm.container import VirtualDsmContainer

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


async def _upload(
    client: DsmClient, ph: Photos, folder_id: int, file: str | Path
) -> dict[str, Any]:
    """One raw upload, retried once like upload_photos (vdsm answers 623 right after boot).

    ``file`` is a seed's name or a local file; either is sent with its own mtime.
    """
    source = file if isinstance(file, Path) else SEED / file
    fields = encode(
        {
            "duplicate": "rename",
            "name": source.name,
            "mtime": int(source.stat().st_mtime),
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
                file_path=source,
                filename=source.name,
            )
        except SynologyError as e:
            if attempt == 2 or e.code in (620, 621):
                raise
            await asyncio.sleep(2)
    raise AssertionError("unreachable")


@asynccontextmanager
async def seeded(
    client: DsmClient, files: Sequence[str | Path], space: Space = "personal"
) -> AsyncIterator[tuple[str, dict[str, int]]]:
    """A fresh folder holding ``files``, indexed; yields its path and the items' IDs by name.

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
            ids[file.name if isinstance(file, Path) else file] = int(data["id"])

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
        # EXIF DateTimeOriginal, shown as recorded: Photos times carry no zone.
        assert "2024-07-04 19:30:00\n" in out
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


# -- organizing tools ---------------------------------------------------------------


async def test_folders(photos: DsmClient) -> None:
    async with seeded(photos, JPEGS[:1]) as (path, ids):
        assert "Created" in await photo_folder(photos, action="create", parent=path, name="2024")
        with pytest.raises(ToolError) as exc:
            await photo_folder(photos, action="create", parent=path, name="2024")
        assert _error(exc)["code"] == "already_exists"
        out = await photo_folder(photos, action="rename", folder=f"{path}/2024", name="Day One")
        assert out.endswith(f"{path}/Day One.")
        out = await photo_folder(
            photos, action="set_cover", folder=path, item_id=ids["beach-sunset.jpg"]
        )
        assert "cover is now item" in out


async def test_tags_and_edits(photos: DsmClient) -> None:
    async with seeded(photos, JPEGS) as (_path, ids):
        item = ids["city-night.jpg"]
        tag = f"t258tag{uuid.uuid4().hex[:6]}"
        await tag_photos(photos, action="add", ids=[item, 999999], tags=[tag, "t258-shared"])
        assert tag in await get_photo(photos, ids=[item])
        out = await tag_photos(photos, action="remove", ids=[item], tags=[tag])
        assert "Removed tags" in out
        # A tag exists only while some item has it.
        assert tag not in await list_photo_collections(photos, type="tag")

        # The edits go to items with no earlier edits: the tag changes above rewrote
        # city-night.jpg, and a time change right after another edit to the same file
        # can be undone by the pending re-index, which reads the old date back (#289).
        edited = ids["forest-trail.jpg"]
        out = await edit_photo(
            photos, ids=[edited], rating=4, description="t258", taken_time="2021-02-03T04:05:06Z"
        )
        assert "rating 4, description, taken time" in out

        # Photos re-indexes the rewritten file; the new taken time shows up then.
        async def retimed() -> str | None:
            details = await get_photo(photos, ids=[edited])
            return details if "2021-02-03 04:05:06\n" in details else None

        details = await _wait_for(retimed, "the edited taken time")
        assert "Rating:       4" in details and "t258" in details

        other = ids["beach-sunset.jpg"]
        out = await edit_photo(photos, ids=[other], shift_hours=1.5, rotate="right")
        assert "taken time shifted +1.5 h, rotated right" in out

        async def shifted() -> str | None:
            details = await get_photo(photos, ids=[other])
            return details if "2024-07-04 21:00:00\n" in details else None

        await _wait_for(shifted, "the shifted taken time")

        if _is_191(photos):
            assert "favorite" in await edit_photo(photos, ids=[other], favorite=True)
            assert "favorite: yes" in await get_photo(photos, ids=[other])
        else:
            with pytest.raises(ToolError) as exc:
                await edit_photo(photos, ids=[other], favorite=True)
            assert _error(exc)["code"] == "not_supported"


def _strip_exif(data: bytes) -> bytes:
    """The JPEG without its APP1 (EXIF) segment, so Photos takes the time from the mtime."""
    out, i = bytearray(data[:2]), 2
    while i < len(data):
        marker = data[i + 1]
        if marker == 0xDA:  # start of scan: the image data follows
            out += data[i:]
            break
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if marker != 0xE1:
            out += data[i : i + 2 + length]
        i += 2 + length
    return bytes(out)


def _nas_clock(container: VirtualDsmContainer, dsm_version: str, t: int) -> tuple[str, str]:
    """The instant ``t`` on the NAS's clock (``YYYY-MM-DD HH:MM:SS``) and its zone."""
    password = str(load_golden_meta(dsm_version).get("admin_password", ""))
    fmt = shlex.quote("+%Y-%m-%d %H:%M:%S|%Z %z")
    rc, out = _ssh(container, password, f"date -d @{t} {fmt}", 30, sudo=False)
    assert rc == 0, out
    clock, zone = out.strip().splitlines()[-1].split("|")
    return clock, zone


def _no_exif_copy(folder: Path, name: str, mtime: int) -> Path:
    """A seed JPEG without EXIF, so Photos takes its time from ``mtime``."""
    path = folder / name
    path.write_bytes(_strip_exif((SEED / "beach-sunset.jpg").read_bytes()))
    assert b"Exif\x00\x00" not in path.read_bytes()
    os.utime(path, (mtime, mtime))
    return path


def _taken(details: str) -> str | None:
    taken = re.search(r"^\s*Taken:\s+(.*)$", details, re.M)
    return taken.group(1) if taken else None


async def test_taken_time_is_wall_clock(
    photos: DsmClient, vdsm_container: VirtualDsmContainer, dsm_version: str, tmp_path: Path
) -> None:
    """Photos stores the time a photo was taken as wall-clock time encoded as UTC (#342).

    A file with no EXIF date gets its mtime on the NAS's clock, which the test reads
    from the NAS rather than assuming a zone; each DSM version runs in another one
    (DSM_TIMEZONES). The date filters find it on that local day: the two mtimes are
    early and late in the UTC day, so in each baked zone but GMT one of them falls
    on another local date. A taken_time edit's offset is dropped, not converted.
    """
    mtimes = {
        "early-utc.jpg": 1_792_465_200,  # 2026-10-20 03:00:00 UTC
        "late-utc.jpg": 1_792_539_000,  # 2026-10-20 23:30:00 UTC
    }
    files = [_no_exif_copy(tmp_path, name, t) for name, t in mtimes.items()]

    async with seeded(photos, [*files, "city-night.jpg"]) as (path, ids):
        for name, t in mtimes.items():
            clock, zone = _nas_clock(vdsm_container, dsm_version, t)
            where = f"{name}, NAS zone {zone}"
            details = await get_photo(photos, ids=[ids[name]])
            assert _taken(details) == clock, (where, details)

            day = clock[:10]
            found = await list_photos(photos, folder=path, start_date=day, end_date=day)
            assert name in found, (where, day, found)
            utc_day = datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d")
            if utc_day != day:
                found = await list_photos(photos, folder=path, start_date=utc_day, end_date=utc_day)
                assert name not in found, (where, utc_day, found)

        item = ids["city-night.jpg"]
        out = await edit_photo(photos, ids=[item], taken_time="2021-02-03T04:05:06+02:00")
        assert "taken time" in out

        async def retimed() -> str | None:
            details = await get_photo(photos, ids=[item])
            return details if _taken(details) == "2021-02-03 04:05:06" else None

        await _wait_for(retimed, "the edited taken time, offset dropped")


async def test_albums_and_share_links(photos: DsmClient) -> None:
    async with seeded(photos, JPEGS) as (path, ids):
        name = f"t258-album-{uuid.uuid4().hex[:6]}"
        made: list[str] = []
        try:
            out = await photo_album(photos, action="create", name=name, ids=list(ids.values()))
            made.append(name)
            assert "with 3 item(s)" in out
            out = await photo_album(
                photos, action="remove", album=name, ids=[ids["city-night.jpg"]]
            )
            assert "2 item(s)" in out
            out = await list_photos(photos, album=name)
            assert "2 item(s) total" in out

            smart = f"{name}-smart"
            out = await photo_album(
                photos,
                action="create_smart",
                name=smart,
                condition={"folders": [path], "type": "photo"},
            )
            made.append(smart)
            assert "Matches 3 item(s) now." in out

            out = await photo_share_link(
                photos,
                action="share",
                album=name,
                public="view",
                password="t258-Pw",
                expires="2035-01-01",
            )
            assert "/mo/sharing/" in out and "password: yes" in out
            # A date-time with an offset is that exact instant (a real UTC instant).
            out = await photo_share_link(
                photos, action="update", album=name, expires="2034-12-31T23:59:59-05:00"
            )
            assert "expires 2035-01-01 04:59:59 UTC" in out, out
            link = re.search(r"Link:\s+(\S+)", out).group(1)  # type: ignore[union-attr]
            # #348: turning public access off clears the expiry and the password, and
            # Photos would drop a new expiry on a link with public none, so it's refused.
            out = await photo_share_link(photos, action="update", album=name, public="none")
            assert "Public:    none · password: no · expires never" in out, out
            assert "also cleared the link's expiry and password" in out, out
            with pytest.raises(ToolError) as exc:
                await photo_share_link(photos, action="update", album=name, expires="2035-01-01")
            assert _error(exc)["param"] == "expires"
            out = await photo_share_link(
                photos, action="update", album=name, public="view", expires="2035-01-01"
            )
            assert "Public:    view · password: no · expires 2035-01-01 23:59:59 UTC" in out, out
            await photo_share_link(photos, action="stop", album=name)
            again = await photo_share_link(photos, action="share", album=name)
            # Same link, settings reset (verified while implementing #258).
            assert link in again and "Public:    none" in again
        finally:
            out = await delete_photo_albums(photos, albums=made)
            assert f"Deleted {len(made)} album(s)" in out
        # DSM answers success for unknown album IDs, so the reply alone proves nothing:
        # the normal and the smart album must both be gone.
        listed = await list_photo_collections(photos, type="album")
        assert not [m for m in made if m in listed], listed
        for gone in made:
            with pytest.raises(ToolError) as exc:
                await list_photos(photos, album=gone)
            assert _error(exc)["code"] == "not_found"


async def test_photo_requests(photos: DsmClient) -> None:
    async with seeded(photos, JPEGS[:1]) as (path, _ids):
        out = await photo_request(
            photos, action="create", subject="t258", folder=path, max_file_mib=5
        )
        assert "/mo/request/" in out and "5 MiB per file" in out
        listed = await photo_request(photos, action="list")
        request = re.search(r"^  (\S+)\s+t258\s", listed, re.M).group(1)  # type: ignore[union-attr]
        out = await photo_request(photos, action="update", request=request, expires="never")
        assert "expires never" in out
        assert "Deleted" in await photo_request(photos, action="delete", request=request)


# -- uploads and background tasks ----------------------------------------------------


async def test_upload_duplicates_and_unsupported(photos: DsmClient, tmp_path: Path) -> None:
    async with seeded(photos, JPEGS[:1]) as (path, _ids):
        notes = tmp_path / "notes.txt"
        notes.write_text("not a photo")
        out = await upload_photos(
            photos,
            local_paths=[str(SEED / "beach-sunset.jpg"), str(notes)],
            folder=path,
            allowed_roots=[SEED, tmp_path],
        )
        assert "renamed" in out and "(as beach-sunset_1.jpg)" in out
        assert "notes.txt" in out and "(620)" in out
        out = await upload_photos(
            photos,
            local_paths=[str(SEED / "beach-sunset.jpg")],
            folder=path,
            on_duplicate="skip",
            allowed_roots=[SEED],
        )
        assert "skipped" in out and "(already there)" in out


async def test_copy_move_delete(photos: DsmClient) -> None:
    async with seeded(photos, JPEGS) as (src, ids), seeded(photos, []) as (dst, _):
        out = await copy_or_move(
            photos,
            operation="copy",
            ids=[ids["beach-sunset.jpg"], 999999],
            folders=None,
            destination=dst,
        )
        assert out.startswith(f"Copied 1 item(s) to {dst}") and "Not found: 999999" in out
        out = await copy_or_move(
            photos, operation="copy", ids=[ids["beach-sunset.jpg"]], folders=None, destination=dst
        )
        assert out.startswith(f"Nothing was copied to {dst}") and "1 skipped (already there)" in out
        out = await copy_or_move(
            photos, operation="move", ids=[ids["city-night.jpg"]], folders=None, destination=dst
        )
        assert out.startswith("Moved 1 item(s)")
        assert "2 item(s) total" in await list_photos(photos, folder=dst)

        out = await copy_or_move(
            photos,
            operation="copy",
            ids=None,
            folders=[src],
            destination=dst,
            on_conflict="overwrite",
            timeout=0,
        )
        handle = re.search(r"photos:\d+", out).group(0)  # type: ignore[union-attr]

        async def finished() -> str | None:
            text = await photo_task(photos, handle=handle)
            return text if ": done," in text else None

        await _wait_for(finished, "the handed-off copy")
        assert handle in await photo_task(photos)

        out = await delete_photos(photos, ids=[ids["forest-trail.jpg"], 999999])
        assert "Deleted 1 item(s) in the personal space" in out and "Not found: 999999" in out
        with pytest.raises(ToolError):
            await photo_task(photos, handle="photos:999999", action="stop")
        assert "finished task(s)" in await photo_task(photos, action="clear")


async def test_delete_folders(photos: DsmClient) -> None:
    async with seeded(photos, JPEGS[:1]) as (path, _ids):
        out = await delete_photo_folders(photos, folders=[path])
        assert out.startswith(f"Deleted {path} (1 item(s) directly in it) in the personal space")
        assert "recycle bin" in out
        with pytest.raises(ToolError) as exc:
            await list_photos(photos, folder=path)
        assert _error(exc)["code"] == "not_found"


async def test_cross_space(photos: DsmClient) -> None:
    async with (
        seeded(photos, JPEGS[:1]) as (_src, ids),
        seeded(photos, [], space="shared") as (team, _),
    ):
        out = await copy_or_move(
            photos,
            operation="copy",
            ids=[ids["beach-sunset.jpg"]],
            folders=None,
            destination=team,
            dest_space="shared",
        )
        assert out.startswith(f"Copied 1 item(s) to {team} (Shared Space)") and "failed" not in out
        assert "beach-sunset.jpg" in await list_photos(photos, folder=team, space="shared")
        with pytest.raises(ToolError) as exc:
            await copy_or_move(
                photos,
                operation="move",
                ids=[1],
                folders=None,
                destination="/",
                space="shared",
                dest_space="personal",
            )
        assert "copy_photos" in _error(exc)["suggestion"]
