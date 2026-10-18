"""download_photos: one request per item, previews only when ready, local confinement (#258)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.photos.transfer import download_photos
from tests.modules.photos.dsm import FakePhotos, binary, captured, install, make_client

if TYPE_CHECKING:
    from pathlib import Path

ITEM = "SYNO.Foto.Browse.Item"


def _error(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    text = str(exc.value)
    return json.loads(text[text.index("{") :])["error"]  # type: ignore[no-any-return]


def _items(*states: str) -> dict[str, Any]:
    """Two captured items whose m previews have the given states."""
    data = captured("item-get-thumb-wait")
    first = data["list"][0]
    second = json.loads(json.dumps(first))
    second.update(id=15, filename="beach-sunset_1.jpg")
    second["additional"]["thumbnail"].update(unit_id=15, cache_key="15_1")
    for item, state in zip((first, second), states, strict=False):
        item["additional"]["thumbnail"]["m"] = state
    return {"list": [first, second]}


class TestOriginals:
    @respx.mock
    async def test_one_request_per_item(self, tmp_path: Path) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items("ready", "ready"))
        fake.on("SYNO.Foto.Download", "download", 1, binary(b"one"), binary(b"two!"))
        install(fake)
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12, 15, 999], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        downloads = fake.of("SYNO.Foto.Download")
        assert [d.params["item_id"] for d in downloads] == [[12], [15]]
        assert all(d.http == "GET" and d.params["force_download"] is True for d in downloads)
        assert (tmp_path / "forest-trail.jpg").read_bytes() == b"one"
        assert (tmp_path / "beach-sunset_1.jpg").read_bytes() == b"two!"
        assert out.startswith(f"Downloaded 2 of 3 item(s) to {tmp_path} (original, personal space)")
        assert "999  not downloaded: not found" in out

    @respx.mock
    async def test_existing_file_kept_without_overwrite(self, tmp_path: Path) -> None:
        (tmp_path / "forest-trail.jpg").write_bytes(b"mine")
        install(FakePhotos().on(ITEM, "get", 2, {"list": _items("ready")["list"][:1]}))
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert "the file exists (overwrite=true replaces it)" in out
        assert (tmp_path / "forest-trail.jpg").read_bytes() == b"mine"

    @respx.mock
    async def test_overwrite_replaces(self, tmp_path: Path) -> None:
        (tmp_path / "forest-trail.jpg").write_bytes(b"mine")
        fake = FakePhotos().on(ITEM, "get", 2, {"list": _items("ready")["list"][:1]})
        fake.on("SYNO.Foto.Download", "download", 1, binary(b"new"))
        install(fake)
        client = make_client()
        async with client:
            await download_photos(
                client,
                ids=[12],
                dest_folder=str(tmp_path),
                overwrite=True,
                allowed_roots=[tmp_path],
            )
        assert (tmp_path / "forest-trail.jpg").read_bytes() == b"new"

    @respx.mock
    async def test_unsafe_name_from_nas_replaced(self, tmp_path: Path) -> None:
        items = {"list": _items("ready")["list"][:1]}
        items["list"][0]["filename"] = "../escape.jpg"
        fake = FakePhotos().on(ITEM, "get", 2, items)
        fake.on("SYNO.Foto.Download", "download", 1, binary(b"x"))
        install(fake)
        client = make_client()
        async with client:
            await download_photos(
                client, ids=[12], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert (tmp_path / "item-12").read_bytes() == b"x"
        assert not (tmp_path.parent / "escape.jpg").exists()

    @respx.mock
    async def test_dsm_error_reported_per_item(self, tmp_path: Path) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items("ready", "ready"))
        fake.on(
            "SYNO.Foto.Download",
            "download",
            1,
            lambda _p: httpx.Response(200, json={"success": False, "error": {"code": 803}}),
            binary(b"ok"),
        )
        install(fake)
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12, 15], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert "Downloaded 1 of 2" in out
        assert "not downloaded: No permission for this Shared Space folder (DSM error 803)" in out

    @respx.mock
    async def test_shared_space_api(self, tmp_path: Path) -> None:
        fake = FakePhotos().on(
            "SYNO.FotoTeam.Browse.Item", "get", 2, {"list": _items("ready")["list"][:1]}
        )
        fake.on("SYNO.FotoTeam.Download", "download", 1, binary(b"t"))
        install(fake)
        client = make_client()
        async with client:
            out = await download_photos(
                client,
                ids=[12],
                space="shared",
                dest_folder=str(tmp_path),
                allowed_roots=[tmp_path],
            )
        assert "Shared Space" in out


class TestPreviews:
    @respx.mock
    async def test_ready_preview_by_unit_and_cache_key(self, tmp_path: Path) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items("ready", "converting"))
        fake.on("SYNO.Foto.Thumbnail", "get", 2, binary(b"jpeg"))
        install(fake)
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12, 15], size="m", dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        thumb = fake.one("SYNO.Foto.Thumbnail", "get")
        assert thumb.http == "GET"
        assert thumb.params == {"id": 12, "cache_key": "12_1792274689", "type": "unit", "size": "m"}
        assert (tmp_path / "forest-trail_m.jpg").read_bytes() == b"jpeg"
        assert "isn't generated yet (converting); try again shortly" in out

    @respx.mock
    async def test_broken_preview(self, tmp_path: Path) -> None:
        install(FakePhotos().on(ITEM, "get", 2, {"list": _items("broken")["list"][:1]}))
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12], size="m", dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert "no m preview for this item (broken)" in out

    @respx.mock
    async def test_404_page_reported(self, tmp_path: Path) -> None:
        """The status said ready, but DSM still answered with its 404 page."""
        fake = FakePhotos().on(ITEM, "get", 2, {"list": _items("ready")["list"][:1]})
        fake.on(
            "SYNO.Foto.Thumbnail",
            "get",
            2,
            lambda _p: httpx.Response(404, text="<html/>", headers={"content-type": "text/html"}),
        )
        install(fake)
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12], size="m", dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert "not downloaded: SYNO.Foto.Thumbnail/get: DSM answered HTTP 404." in out
        assert not list(tmp_path.iterdir())


class TestRefusals:
    async def test_outside_allowed_roots(self, tmp_path: Path) -> None:
        inside = tmp_path / "ok"
        inside.mkdir()
        with pytest.raises(ToolError) as exc:
            await download_photos(
                make_client(), ids=[1], dest_folder=str(tmp_path), allowed_roots=[inside]
            )
        assert _error(exc)["code"] == "permission_denied"

    async def test_default_folder_needs_a_single_root(self, tmp_path: Path) -> None:
        with pytest.raises(ToolError) as exc:
            await download_photos(make_client(), ids=[1], allowed_roots=[tmp_path, tmp_path / "b"])
        assert _error(exc)["param"] == "dest_folder"

    @respx.mock
    async def test_default_folder_is_the_single_root(self, tmp_path: Path) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, {"list": _items("ready")["list"][:1]})
        fake.on("SYNO.Foto.Download", "download", 1, binary(b"x"))
        install(fake)
        client = make_client()
        async with client:
            await download_photos(client, ids=[12], allowed_roots=[tmp_path])
        assert (tmp_path / "forest-trail.jpg").exists()

    async def test_missing_folder(self, tmp_path: Path) -> None:
        with pytest.raises(ToolError) as exc:
            await download_photos(
                make_client(), ids=[1], dest_folder=str(tmp_path / "no"), allowed_roots=[tmp_path]
            )
        assert _error(exc)["code"] == "not_found"

    @pytest.mark.parametrize(
        ("kwargs", "param"),
        [
            ({"ids": []}, "ids"),
            ({"ids": list(range(51))}, "ids"),
            ({"ids": [1], "size": "l"}, "size"),
            ({"ids": [1], "space": "x"}, "space"),
        ],
    )
    async def test_parameters(self, kwargs: dict[str, Any], param: str, tmp_path: Path) -> None:
        with pytest.raises(ToolError) as exc:
            await download_photos(
                make_client(), dest_folder=str(tmp_path), allowed_roots=[tmp_path], **kwargs
            )
        assert _error(exc)["param"] == param
