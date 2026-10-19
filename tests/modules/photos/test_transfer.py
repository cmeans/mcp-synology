"""download_photos: one request per item, previews only when ready, local confinement (#258)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.photos.transfer import download_photos, upload_photos
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
    async def test_a_proxy_page_is_not_saved(self, tmp_path: Path) -> None:
        """A login page in front of the NAS isn't saved as the photo (#292)."""
        (tmp_path / "forest-trail.jpg").write_bytes(b"mine")
        fake = FakePhotos().on(ITEM, "get", 2, {"list": _items("ready")["list"][:1]})
        fake.on("SYNO.Foto.Download", "download", 1, binary(b"<html>Sign in</html>", "text/html"))
        install(fake)
        client = make_client()
        async with client:
            out = await download_photos(
                client,
                ids=[12],
                dest_folder=str(tmp_path),
                allowed_roots=[tmp_path],
                overwrite=True,
            )
        assert out.startswith(f"Downloaded 0 of 1 item(s) to {tmp_path}")
        assert (
            "12  forest-trail.jpg: not downloaded: SYNO.Foto.Download/download: the answer "
            "wasn't a DSM reply (Content-Type text/html)." in out
        )
        assert "Sign in" not in out
        assert [p.name for p in tmp_path.iterdir()] == ["forest-trail.jpg"]
        assert (tmp_path / "forest-trail.jpg").read_bytes() == b"mine"

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

    @pytest.mark.parametrize("overwrite", [False, True])
    @respx.mock
    async def test_same_filename_twice_keeps_both(self, tmp_path: Path, overwrite: bool) -> None:
        """Items in different folders can share a filename; the second gets its ID
        added instead of replacing (or being refused by) the first's file."""
        items = _items("ready", "ready")
        for item in items["list"]:
            item["filename"] = "IMG_0001.jpg"
        fake = FakePhotos().on(ITEM, "get", 2, items)
        fake.on("SYNO.Foto.Download", "download", 1, binary(b"one"), binary(b"two!"))
        install(fake)
        client = make_client()
        async with client:
            out = await download_photos(
                client,
                ids=[12, 15],
                dest_folder=str(tmp_path),
                overwrite=overwrite,
                allowed_roots=[tmp_path],
            )
        assert (tmp_path / "IMG_0001.jpg").read_bytes() == b"one"
        assert (tmp_path / "IMG_0001_15.jpg").read_bytes() == b"two!"
        assert "Downloaded 2 of 2" in out
        assert "15  IMG_0001_15.jpg  " in out

    @respx.mock
    async def test_dsm_not_answering_stops_and_names_the_rest(self, tmp_path: Path) -> None:
        items = _items("ready", "ready")
        third = json.loads(json.dumps(items["list"][1]))
        third.update(id=16, filename="third.jpg")
        items["list"].append(third)

        def timeout(_params: dict[str, Any]) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        fake = FakePhotos().on(ITEM, "get", 2, items)
        fake.on("SYNO.Foto.Download", "download", 1, binary(b"one"), timeout)
        install(fake)
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12, 15, 16], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert out.startswith("Downloaded 1 of 3")
        assert "15  beach-sunset_1.jpg: not downloaded: couldn't reach DSM (ReadTimeout)" in out
        assert "not tried: 16" in out
        assert len(fake.of("SYNO.Foto.Download")) == 2


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
    async def test_previews_sharing_a_stem_keep_both(self, tmp_path: Path) -> None:
        """a.jpg and a.png both preview as a_m.jpg."""
        items = _items("ready", "ready")
        items["list"][0]["filename"] = "a.jpg"
        items["list"][1]["filename"] = "a.png"
        fake = FakePhotos().on(ITEM, "get", 2, items)
        fake.on("SYNO.Foto.Thumbnail", "get", 2, binary(b"jpg"), binary(b"png"))
        install(fake)
        client = make_client()
        async with client:
            await download_photos(
                client, ids=[12, 15], size="m", dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert (tmp_path / "a_m.jpg").read_bytes() == b"jpg"
        assert (tmp_path / "a_m_15.jpg").read_bytes() == b"png"

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


class TestUpload:
    @staticmethod
    def _files(tmp_path: Path) -> list[str]:
        paths = []
        for name in ("beach.jpg", "2024", "notes.txt"):
            (tmp_path / name).write_bytes(b"x")
            paths.append(str(tmp_path / name))
        return paths

    @respx.mock
    async def test_per_file_results(self, tmp_path: Path) -> None:
        upload = "SYNO.Foto.Upload.Item"
        sent: list[bytes] = []
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-by-name"))
        fake.on(ITEM, "get", 2, {"list": [{"id": 47, "filename": "2024_1"}]})
        answers = iter(
            [
                {"success": True, "data": {"action": "new", "id": 41, "unit_id": 41}},
                {"success": True, "data": {"action": "rename", "id": 47, "unit_id": 47}},
                {"success": False, "error": {"code": 620}},
            ]
        )

        def multipart(request: httpx.Request) -> httpx.Response:
            sent.append(request.content)
            return httpx.Response(200, json=next(answers))

        fake.multipart = multipart
        install(fake)
        client = make_client()
        async with client:
            out = await upload_photos(
                client,
                local_paths=self._files(tmp_path),
                folder="/mcp-spec-capture",
                allowed_roots=[tmp_path],
            )
        assert len(sent) == 3
        assert b'name="name"\r\n\r\n"2024"\r\n' in sent[1]  # JSON-quoted even when numeric
        assert b'name="target_folder_id"\r\n\r\n10\r\n' in sent[0]
        assert b'name="duplicate"\r\n\r\n"rename"\r\n' in sent[0]
        assert f'name="api"\r\n\r\n{upload}'.encode() in sent[0]
        assert "Uploaded 2 of 3 file(s) to /mcp-spec-capture (personal space)" in out
        assert "renamed  id 47 (as 2024_1)" in out
        assert "notes.txt  failed   Synology Photos doesn't support this file type (620)" in out
        assert "Photos indexes new items" in out

    @respx.mock
    async def test_skip_and_album(self, tmp_path: Path) -> None:
        (tmp_path / "a.jpg").write_bytes(b"x")
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        fake.on("SYNO.Foto.Browse.Album", "get", 1, captured("album-get"))
        fake.on("SYNO.Foto.Browse.NormalAlbum", "add_item", 1, {"error_list": []})
        fake.multipart = lambda _r: httpx.Response(
            200, json={"success": True, "data": {"action": "ignore", "id": 9}}
        )
        install(fake)
        client = make_client()
        async with client:
            out = await upload_photos(
                client,
                local_paths=[str(tmp_path / "a.jpg")],
                on_duplicate="skip",
                album="4",
                allowed_roots=[tmp_path],
            )
        assert "skipped  id 9 (already there)" in out
        assert fake.one("SYNO.Foto.Browse.NormalAlbum", "add_item").params == {"id": 4, "item": [9]}
        assert 'Added 1 item(s) to album "mcp-spec-album".' in out

    @respx.mock
    async def test_folder_lookup_error(self, tmp_path: Path) -> None:
        (tmp_path / "a.jpg").write_bytes(b"x")
        install(FakePhotos().error("SYNO.Foto.Browse.Folder", "get", 2, 160))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await upload_photos(
                    client, local_paths=[str(tmp_path / "a.jpg")], allowed_roots=[tmp_path]
                )
        assert "160" in _error(exc)["message"]

    @respx.mock
    async def test_unreadable_file_rename_lookup_and_album_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file that can't be read is one failed row; a failed rename lookup or album
        add leaves the upload's own result standing."""
        from mcp_synology.modules.photos import transfer

        for name in ("a.jpg", "b.jpg"):
            (tmp_path / name).write_bytes(b"x")
        real = transfer._upload_one

        async def flaky(client: Any, api: Any, fields: dict[str, Any], path: Path) -> Any:
            if path.name == "a.jpg":
                raise PermissionError(13, "Permission denied")
            return await real(client, api, fields, path)

        monkeypatch.setattr(transfer, "_upload_one", flaky)
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        fake.on("SYNO.Foto.Browse.Album", "get", 1, captured("album-get"))
        fake.error(ITEM, "get", 2, 117)
        fake.error("SYNO.Foto.Browse.NormalAlbum", "add_item", 1, 117)
        fake.multipart = lambda _r: httpx.Response(
            200, json={"success": True, "data": {"action": "rename", "id": 9}}
        )
        install(fake)
        client = make_client()
        async with client:
            out = await upload_photos(
                client,
                local_paths=[str(tmp_path / "a.jpg"), str(tmp_path / "b.jpg")],
                album="4",
                allowed_roots=[tmp_path],
            )
        assert "a.jpg  failed   couldn't read the file (Permission denied)" in out
        assert "renamed  id 9" in out and "(as " not in out
        assert 'Couldn\'t add them to album "mcp-spec-album":' in out

    async def test_outside_roots_refused_first(self, tmp_path: Path) -> None:
        inside = tmp_path / "in"
        inside.mkdir()
        with pytest.raises(ToolError) as exc:
            await upload_photos(
                make_client(), local_paths=["/etc/hostname"], allowed_roots=[inside]
            )
        assert _error(exc)["code"] == "permission_denied"

    async def test_folder_path_not_a_file(self, tmp_path: Path) -> None:
        with pytest.raises(ToolError) as exc:
            await upload_photos(
                make_client(), local_paths=[str(tmp_path)], allowed_roots=[tmp_path]
            )
        assert _error(exc)["code"] == "not_found"

    @pytest.mark.parametrize(
        ("kwargs", "param"),
        [
            ({"local_paths": []}, "local_paths"),
            ({"local_paths": ["a"] * 51}, "local_paths"),
            ({"local_paths": ["a"], "on_duplicate": "overwrite"}, "on_duplicate"),
            ({"local_paths": ["a"], "album": "1", "space": "shared"}, "album"),
        ],
    )
    async def test_refusals(self, kwargs: dict[str, Any], param: str) -> None:
        with pytest.raises(ToolError) as exc:
            await upload_photos(make_client(), **kwargs)
        assert _error(exc)["param"] == param


class TestUploadRetry:
    @pytest.fixture(autouse=True)
    def _no_delay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mcp_synology.modules.photos import transfer

        monkeypatch.setattr(transfer, "UPLOAD_RETRY_DELAY", 0)

    async def _upload(self, tmp_path: Path, *answers: dict[str, Any]) -> tuple[str, int]:
        (tmp_path / "a.jpg").write_bytes(b"x")
        replies = iter(answers)
        sent: list[int] = []

        def multipart(_r: httpx.Request) -> httpx.Response:
            sent.append(1)
            return httpx.Response(200, json=next(replies))

        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        fake.multipart = multipart
        with respx.mock:
            install(fake)
            client = make_client()
            async with client:
                out = await upload_photos(
                    client, local_paths=[str(tmp_path / "a.jpg")], allowed_roots=[tmp_path]
                )
        return out, len(sent)

    async def test_transient_error_retried_once(self, tmp_path: Path) -> None:
        """vdsm answered 623 to uploads right after boot; the web client retries once."""
        out, sent = await self._upload(
            tmp_path,
            {"success": False, "error": {"code": 623}},
            {"success": True, "data": {"action": "new", "id": 5}},
        )
        assert sent == 2
        assert "new      id 5" in out

    async def test_unsupported_type_not_retried(self, tmp_path: Path) -> None:
        out, sent = await self._upload(tmp_path, {"success": False, "error": {"code": 620}})
        assert sent == 1
        assert "(620)" in out

    async def test_second_failure_reported(self, tmp_path: Path) -> None:
        out, sent = await self._upload(
            tmp_path,
            {"success": False, "error": {"code": 623}},
            {"success": False, "error": {"code": 623}},
        )
        assert sent == 2
        assert "failed" in out and "(623)" in out
