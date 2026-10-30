"""download_photos: one request per item, previews only when ready, local confinement (#258)."""

from __future__ import annotations

import contextlib
import json
import os
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.photos.transfer import (
    _dest_name,
    _name_key,
    download_photos,
    upload_photos,
)
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
        assert (
            "12  forest-trail.jpg: not downloaded: another file has this name (if it's "
            "another item's, from a call with more ids, repeat that whole call instead; "
            "otherwise overwrite=true replaces it, or choose another folder)" in out
        )
        assert (tmp_path / "forest-trail.jpg").read_bytes() == b"mine"

    @respx.mock
    async def test_existing_file_of_the_item_s_size_is_kept_quietly(self, tmp_path: Path) -> None:
        """A file with the item's name and its filesize is most likely this item from an
        earlier run: kept, without pushing toward overwrite=true (QA round 2 on #462)."""
        (tmp_path / "forest-trail.jpg").write_bytes(b"x" * 49546)  # the capture's filesize
        install(FakePhotos().on(ITEM, "get", 2, {"list": _items("ready")["list"][:1]}))
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert out.startswith("Downloaded 0 of 1 item(s)") and ", 1 already there)" in out
        assert (
            "12  forest-trail.jpg: already there: a file with this name and this item's size, "
            "most likely this item from an earlier run; same name and size only, not verified "
            "(kept)" in out
        )
        assert "overwrite=true" not in out

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

    @pytest.mark.parametrize("overwrite", [False, True])
    @respx.mock
    async def test_renamed_repeat_never_takes_another_items_name(
        self, tmp_path: Path, overwrite: bool
    ) -> None:
        """#450: a repeat's ID-suffixed name can be another item's real name; the
        third item used to get ``a_9.jpg`` again and replace the first's file."""
        items = _items("ready", "ready")
        third = json.loads(json.dumps(items["list"][1]))
        items["list"].append(third)
        for item, (item_id, name) in zip(
            items["list"], [(1, "a_9.jpg"), (8, "a.jpg"), (9, "a.jpg")], strict=True
        ):
            item.update(id=item_id, filename=name)
        fake = FakePhotos().on(ITEM, "get", 2, items)
        fake.on("SYNO.Foto.Download", "download", 1, binary(b"1"), binary(b"8"), binary(b"9"))
        install(fake)
        client = make_client()
        async with client:
            out = await download_photos(
                client,
                ids=[1, 8, 9],
                dest_folder=str(tmp_path),
                overwrite=overwrite,
                allowed_roots=[tmp_path],
            )
        assert "Downloaded 3 of 3" in out
        assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == {
            "a_9.jpg": b"1",
            "a.jpg": b"8",
            "a_9_2.jpg": b"9",
        }
        assert "9  a_9_2.jpg  " in out

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
        assert out.endswith(
            "To retry, repeat this call with the same ids in the same order (overwrite=false "
            "keeps the files already written): names depend on every item in the call, and "
            "shift if an item that wasn't found is found then."
        )
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
    async def test_no_answer_stops_the_batch_and_keeps_the_rows(self, tmp_path: Path) -> None:
        """QA R1: one network error lost the whole batch: a.jpg's row (and new ID) went
        with it, c.jpg was never tried and the album add never ran."""
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            (tmp_path / name).write_bytes(b"x")
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        fake.on("SYNO.Foto.Browse.Album", "get", 1, captured("album-get"))
        fake.on("SYNO.Foto.Browse.NormalAlbum", "add_item", 1, {"error_list": []})
        answers = iter(
            [
                httpx.Response(200, json={"success": True, "data": {"action": "new", "id": 41}}),
                httpx.ReadTimeout("slow"),
            ]
        )

        def multipart(_r: httpx.Request) -> httpx.Response:
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        fake.multipart = multipart
        install(fake)
        client = make_client()
        async with client:
            out = await upload_photos(
                client,
                local_paths=[str(tmp_path / n) for n in ("a.jpg", "b.jpg", "c.jpg")],
                album="4",
                allowed_roots=[tmp_path],
            )
        lines = out.splitlines()
        assert lines[0] == "Uploaded 1 of 3 file(s) to / (personal space)"
        assert lines[1].split() == ["a.jpg", "new", "id", "41"]
        assert lines[2] == (
            "  b.jpg  unknown  DSM didn't answer (ReadTimeout); it may or may not have been stored"
        )
        assert lines[3].rstrip() == "  c.jpg  not tried"
        assert fake.one("SYNO.Foto.Browse.NormalAlbum", "add_item").params == {
            "id": 4,
            "item": [41],
        }

    @respx.mock
    @pytest.mark.parametrize(
        ("second", "row"),
        [
            (httpx.Response(504, text="<html>Gateway Timeout</html>"),
             "  b.jpg  unknown  SYNO.Foto.Upload.Item/upload_to_folder: DSM answered HTTP 504. "
             "It may or may not have been stored."),
            (httpx.ConnectError("refused"),
             "  b.jpg  failed   DSM couldn't be reached (ConnectError)"),
        ],
    )  # fmt: skip
    async def test_proxy_error_or_refused_stops_the_batch(
        self, tmp_path: Path, second: Any, row: str
    ) -> None:
        """A proxy's 504 is the same stall as a timeout (#282): unknown, rest not tried. A
        refused connection never sent the file, so that one is plainly failed."""
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            (tmp_path / name).write_bytes(b"x")
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        answers = iter(
            [
                httpx.Response(200, json={"success": True, "data": {"action": "new", "id": 41}}),
                second,
            ]
        )

        def multipart(_r: httpx.Request) -> httpx.Response:
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        fake.multipart = multipart
        install(fake)
        client = make_client()
        async with client:
            out = await upload_photos(
                client,
                local_paths=[str(tmp_path / n) for n in ("a.jpg", "b.jpg", "c.jpg")],
                allowed_roots=[tmp_path],
            )
        lines = out.splitlines()
        assert lines[2] == row
        assert lines[3].rstrip() == "  c.jpg  not tried"
        assert "Gateway" not in out

    @respx.mock
    async def test_proxy_4xx_fails_that_file_and_goes_on(self, tmp_path: Path) -> None:
        """QA R2: a proxy's 413 (nginx's body-size limit) refused the file; it wasn't
        stored, and a smaller file after it may go through."""
        for name in ("big.mp4", "small.jpg"):
            (tmp_path / name).write_bytes(b"x")
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        answers = iter(
            [
                httpx.Response(413, text="<html>Request Entity Too Large</html>"),
                httpx.Response(200, json={"success": True, "data": {"action": "new", "id": 42}}),
            ]
        )
        fake.multipart = lambda _r: next(answers)
        install(fake)
        client = make_client()
        async with client:
            out = await upload_photos(
                client,
                local_paths=[str(tmp_path / "big.mp4"), str(tmp_path / "small.jpg")],
                allowed_roots=[tmp_path],
            )
        lines = out.splitlines()
        assert lines[0] == "Uploaded 1 of 2 file(s) to / (personal space)"
        assert lines[1] == (
            "  big.mp4    failed   SYNO.Foto.Upload.Item/upload_to_folder: DSM answered HTTP 413. "
            "The file may be over the proxy's upload size limit."
        )
        assert lines[2].split() == ["small.jpg", "new", "id", "42"]
        assert "Too Large" not in out

    @respx.mock
    @pytest.mark.parametrize(("status", "rest"), [(429, "not tried"), (302, "not tried")])
    async def test_rate_limit_or_redirect_stops_the_batch(
        self, tmp_path: Path, status: int, rest: str
    ) -> None:
        """After a 429 the rest would meet the same limit; a 302 is a proxy's login page."""
        for name in ("a.jpg", "b.jpg"):
            (tmp_path / name).write_bytes(b"x")
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        fake.multipart = lambda _r: httpx.Response(status, headers={"location": "/login"})
        install(fake)
        client = make_client()
        async with client:
            out = await upload_photos(
                client,
                local_paths=[str(tmp_path / "a.jpg"), str(tmp_path / "b.jpg")],
                allowed_roots=[tmp_path],
            )
        lines = out.splitlines()
        state = "failed" if status == 429 else "unknown"
        assert lines[1].split()[:2] == ["a.jpg", state]
        assert lines[2].rstrip() == f"  b.jpg  {rest}"

    @respx.mock
    async def test_reply_without_id_is_its_own_row(self, tmp_path: Path) -> None:
        for name in ("a.jpg", "b.jpg"):
            (tmp_path / name).write_bytes(b"x")
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        answers = iter(
            [
                {"success": True, "data": {}},
                {"success": True, "data": {"action": "new", "id": 42}},
            ]
        )
        fake.multipart = lambda _r: httpx.Response(200, json=next(answers))
        install(fake)
        client = make_client()
        async with client:
            out = await upload_photos(
                client,
                local_paths=[str(tmp_path / "a.jpg"), str(tmp_path / "b.jpg")],
                allowed_roots=[tmp_path],
            )
        assert "a.jpg  failed   DSM's reply couldn't be read (KeyError)" in out
        assert out.splitlines()[0] == "Uploaded 1 of 2 file(s) to / (personal space)"

    @respx.mock
    async def test_file_gone_before_upload_is_its_own_row(self, tmp_path: Path) -> None:
        """A file removed after the checks failed the batch at stat(), outside the try."""
        for name in ("a.jpg", "b.jpg"):
            (tmp_path / name).write_bytes(b"x")
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))

        def multipart(_r: httpx.Request) -> httpx.Response:
            (tmp_path / "b.jpg").unlink()  # gone by the time its upload starts
            return httpx.Response(200, json={"success": True, "data": {"action": "new", "id": 42}})

        fake.multipart = multipart
        install(fake)
        client = make_client()
        async with client:
            out = await upload_photos(
                client,
                local_paths=[str(tmp_path / "a.jpg"), str(tmp_path / "b.jpg")],
                allowed_roots=[tmp_path],
            )
        assert out.splitlines()[0] == "Uploaded 1 of 2 file(s) to / (personal space)"
        assert "b.jpg  failed   couldn't read the file (No such file or directory)" in out

    @respx.mock
    async def test_album_and_names_unanswered(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The uploads are done: a lookup or album add with no answer is a note, not a
        lost batch, and the missing "(as …)" names are logged (QA R1 nit)."""
        (tmp_path / "a.jpg").write_bytes(b"x")

        def boom(_p: dict[str, Any]) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        fake.on("SYNO.Foto.Browse.Album", "get", 1, captured("album-get"))
        fake.on("SYNO.Foto.Browse.NormalAlbum", "add_item", 1, boom)
        fake.on("SYNO.Foto.Browse.Item", "get", 2, boom)
        fake.multipart = lambda _r: httpx.Response(
            200, json={"success": True, "data": {"action": "rename", "id": 9}}
        )
        install(fake)
        client = make_client()
        with caplog.at_level("DEBUG", logger="mcp_synology.modules.photos.transfer"):
            async with client:
                out = await upload_photos(
                    client,
                    local_paths=[str(tmp_path / "a.jpg")],
                    album="4",
                    allowed_roots=[tmp_path],
                )
        assert "renamed  id 9" in out and "(as " not in out
        assert (
            "Couldn't add them to album \"mcp-spec-album\": DSM didn't answer (ReadTimeout). "
            "Check the album with list_photos before adding again."
        ) in out
        assert "Couldn't read the renamed items' names: ReadTimeout" in caplog.text

    @respx.mock
    async def test_time_limit_stops_starting_files_and_lists_the_rest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#416: a 50-file batch took 133 s and Claude Desktop gave up at ~60 s. Past the
        budget no new file starts; the reply names the rest by path for another call."""
        from mcp_synology.modules.photos import transfer

        names = ("a.jpg", "b.jpg", "c.jpg", "d.jpg")
        for name in names:
            (tmp_path / name).write_bytes(b"x")
        clock = [100.0]
        monkeypatch.setattr(transfer.time, "monotonic", lambda: clock[0])
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        fake.on("SYNO.Foto.Browse.Album", "get", 1, captured("album-get"))
        fake.on("SYNO.Foto.Browse.NormalAlbum", "add_item", 1, {"error_list": []})
        ids = iter([41, 42])

        def multipart(_r: httpx.Request) -> httpx.Response:
            clock[0] += 20  # each upload takes 20 s
            return httpx.Response(
                200, json={"success": True, "data": {"action": "new", "id": next(ids)}}
            )

        fake.multipart = multipart
        install(fake)
        client = make_client()
        paths = [str(tmp_path / n) for n in names]
        async with client:
            out = await upload_photos(
                client, local_paths=paths, album="4", allowed_roots=[tmp_path], budget=30
            )
        lines = out.splitlines()
        # a.jpg ends at 20 s, under the limit, so b.jpg starts; at 40 s c.jpg doesn't.
        assert lines[0] == "Uploaded 2 of 4 file(s) to / (personal space)"
        assert [line.split()[:2] for line in lines[1:3]] == [["a.jpg", "new"], ["b.jpg", "new"]]
        assert [line.rstrip() for line in lines[3:5]] == [
            "  c.jpg  not tried",
            "  d.jpg  not tried",
        ]
        assert fake.one("SYNO.Foto.Browse.NormalAlbum", "add_item").params == {
            "id": 4,
            "item": [41, 42],
        }
        assert lines[-3] == (
            "Started no new file after 30 s (the task timeout, at most 45 s) so the reply "
            "isn't cut off; 2 file(s) not tried. To continue, call upload_photos again with "
            "the same folder, space, album and on_duplicate, and only these local_paths "
            "(resending the others makes renamed copies):"
        )
        assert lines[-2:] == [f"  {paths[2]}", f"  {paths[3]}"]

    @respx.mock
    @pytest.mark.parametrize(
        ("budget", "on_duplicate", "uploaded", "footer"),
        [
            # task_timeout goes up to 600, but uploads stop starting files at 45 s (QA on
            # #417): 0, 20 and 40 s start, 60 s doesn't.
            (600, "rename", 3, "after 45 s (the task timeout, at most 45 s)"),
            # With skip, resending a tried file only skips it (vdsm, QA on #417).
            (30, "skip", 2, "(the others would only be skipped as already there):"),
        ],
    )
    async def test_budget_cap_and_skip_footer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        budget: float,
        on_duplicate: str,
        uploaded: int,
        footer: str,
    ) -> None:
        from mcp_synology.modules.photos import transfer

        names = [f"{n}.jpg" for n in "abcde"]
        for name in names:
            (tmp_path / name).write_bytes(b"x")
        clock = [0.0]
        monkeypatch.setattr(transfer.time, "monotonic", lambda: clock[0])
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        ids = iter(range(41, 50))

        def multipart(_r: httpx.Request) -> httpx.Response:
            clock[0] += 20
            data = {"action": "new", "id": next(ids)}
            return httpx.Response(200, json={"success": True, "data": data})

        fake.multipart = multipart
        install(fake)
        client = make_client()
        async with client:
            out = await upload_photos(
                client,
                local_paths=[str(tmp_path / n) for n in names],
                on_duplicate=on_duplicate,
                allowed_roots=[tmp_path],
                budget=budget,
            )
        assert out.startswith(f"Uploaded {uploaded} of 5 file(s)"), out
        assert footer in out, out
        assert ("renamed copies" in out) is (on_duplicate == "rename")

    @respx.mock
    async def test_first_file_always_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The limit only stops new files: with budget=0 the first is still uploaded."""
        from mcp_synology.modules.photos import transfer

        (tmp_path / "a.jpg").write_bytes(b"x")
        monkeypatch.setattr(transfer.time, "monotonic", lambda: 0.0)
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        fake.multipart = lambda _r: httpx.Response(
            200, json={"success": True, "data": {"action": "new", "id": 41}}
        )
        install(fake)
        client = make_client()
        async with client:
            out = await upload_photos(
                client, local_paths=[str(tmp_path / "a.jpg")], allowed_roots=[tmp_path], budget=0
            )
        assert out.startswith("Uploaded 1 of 1 file(s)")
        assert "Stopped after" not in out

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
        assert out.startswith("Uploaded 0 of 1 file(s) (1 skipped, already there) to ")
        assert fake.one("SYNO.Foto.Browse.NormalAlbum", "add_item").params == {"id": 4, "item": [9]}
        assert 'Added 1 item(s) to album "mcp-spec-album".' in out
        # Nothing was stored, so there's nothing for Photos to index.
        assert "Photos indexes" not in out

    @respx.mock
    async def test_counts_stored_and_skipped_apart(self, tmp_path: Path) -> None:
        # The 0.10.0rc1 live test: re-uploading 3 files with skip said "Uploaded 3 of 3".
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            (tmp_path / name).write_bytes(b"x")
        replies = iter(
            [
                {"action": "new", "id": 7},
                {"action": "ignore", "id": 8},
                {"action": "ignore", "id": 9},
            ]
        )
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        fake.multipart = lambda _r: httpx.Response(
            200, json={"success": True, "data": next(replies)}
        )
        install(fake)
        client = make_client()
        async with client:
            out = await upload_photos(
                client,
                local_paths=[str(tmp_path / n) for n in ("a.jpg", "b.jpg", "c.jpg")],
                on_duplicate="skip",
                allowed_roots=[tmp_path],
            )
        assert out.splitlines()[0] == (
            "Uploaded 1 of 3 file(s) (2 skipped, already there) to / (personal space)"
        )
        assert "Photos indexes new items" in out

    @respx.mock
    async def test_missing_album_uploads_nothing(self, tmp_path: Path) -> None:
        # The 0.10.0rc1 live test: a new album name is not_found, and it's checked before
        # any file is sent, so nothing is half done.
        (tmp_path / "a.jpg").write_bytes(b"x")
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        fake.on("SYNO.Foto.Browse.Album", "list", 2, captured("album-list"))
        sent: list[httpx.Request] = []
        fake.multipart = lambda r: sent.append(r) or httpx.Response(500)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await upload_photos(
                    client,
                    local_paths=[str(tmp_path / "a.jpg")],
                    album="mcp-rc-0.10 user album",
                    allowed_roots=[tmp_path],
                )
        error = _error(exc)
        assert error["code"] == "not_found"
        assert error["message"] == "Upload photos failed: no album 'mcp-rc-0.10 user album'."
        assert sent == []

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

        async def flaky(client: Any, api: Any, fields: dict[str, Any], path: Path, *a: Any) -> Any:
            if path.name == "a.jpg":
                raise PermissionError(13, "Permission denied")
            return await real(client, api, fields, path, *a)

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

    async def _upload(
        self,
        tmp_path: Path,
        *answers: dict[str, Any],
        fake: FakePhotos | None = None,
        names: tuple[str, ...] = ("a.jpg",),
        **kwargs: Any,
    ) -> tuple[str, int]:
        for name in names:
            (tmp_path / name).write_bytes(b"x")
        replies = iter(answers)
        sent: list[int] = []

        def multipart(_r: httpx.Request) -> httpx.Response:
            sent.append(1)
            return httpx.Response(200, json=next(replies))

        if fake is None:
            fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        fake.multipart = multipart
        with respx.mock:
            install(fake)
            client = make_client()
            async with client:
                out = await upload_photos(
                    client,
                    local_paths=[str(tmp_path / n) for n in names],
                    allowed_roots=[tmp_path],
                    **kwargs,
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


_NO_FOLDER = {"success": False, "error": {"code": 117}}
_NEW = {"success": True, "data": {"action": "new", "id": 5}}


class TestUploadPersonalRootFolder:
    """#436: an account that has never used Photos has no homes/<user>/Photos yet, and
    every upload to the personal root answers 117 until Setting.User get creates it."""

    @pytest.fixture(autouse=True)
    def _no_delay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mcp_synology.modules.photos import transfer

        monkeypatch.setattr(transfer, "UPLOAD_RETRY_DELAY", 0)

    _upload = TestUploadRetry._upload

    @staticmethod
    def _root(*setting_user: Any) -> FakePhotos:
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-root"))
        return fake.on(
            "SYNO.Foto.Setting.User", "get", 1, *(setting_user or (captured("setting-user"),))
        )

    async def test_setting_user_then_resent(self, tmp_path: Path) -> None:
        """Verified on vdsm 7.1, 7.2.2, 7.3.2 and 7.4.1: the call creates the folder and an
        upload right after it succeeds."""
        fake = self._root()
        out, sent = await self._upload(
            tmp_path, _NO_FOLDER, _NEW, _NEW, fake=fake, names=("a.jpg", "b.jpg")
        )
        assert sent == 3
        assert fake.one("SYNO.Foto.Setting.User", "get").params == {}
        assert out.splitlines()[0] == "Uploaded 2 of 2 file(s) to / (personal space)"

    async def test_still_missing_says_why_once(self, tmp_path: Path) -> None:
        """Setting.User is called once per call; a 117 isn't retried after a delay, which
        can't help (the folder was just looked up)."""
        fake = self._root()
        out, sent = await self._upload(
            tmp_path, _NO_FOLDER, _NO_FOLDER, _NO_FOLDER, fake=fake, names=("a.jpg", "b.jpg")
        )
        assert sent == 3  # a.jpg, a.jpg after Setting.User, b.jpg
        assert len(fake.of("SYNO.Foto.Setting.User")) == 1
        rows = out.splitlines()[1:3]
        for row in rows:
            assert "failed" in row
            assert row.endswith(
                "failed   this account's personal space has no folder on the NAS "
                "(homes/<user>/Photos; 117). Create a folder with photo_folder and upload "
                "into it, or open Synology Photos once as this user."
            )

    @staticmethod
    def _timeout(_p: Any) -> Any:
        raise httpx.ReadTimeout("slow")

    @staticmethod
    def _refused(_p: Any) -> Any:
        raise httpx.ConnectError("refused")

    @staticmethod
    def _connect_timeout(_p: Any) -> Any:
        raise httpx.ConnectTimeout("connect")

    _SETTING_USER_REPLIES: ClassVar[dict[str, tuple[Any, bool]]] = {
        # name: (Setting.User's reply, whether the call may have created the folder)
        "dsm-error": (lambda _p: {"success": False, "error": {"code": 119}}, False),
        "no-answer": (_timeout, True),
        "proxy-502": (lambda _p: httpx.Response(502, text="<html>Bad Gateway</html>"), True),
        "login-page": (
            lambda _p: httpx.Response(
                200, text="<html>Sign in</html>", headers={"content-type": "text/html"}
            ),
            True,
        ),
        "proxy-403": (lambda _p: httpx.Response(403, text="<html>Forbidden</html>"), False),
        "refused": (_refused, False),
        "connect-timeout": (_connect_timeout, False),
    }

    @pytest.mark.parametrize("answer", sorted(_SETTING_USER_REPLIES))
    async def test_setting_user_failing_isnt_resent(self, tmp_path: Path, answer: str) -> None:
        """A DSM error or no answer from Setting.User: the upload's own 117 is reported,
        not a transport error with the other files left untried. After no answer (or a
        proxy's 5xx or login page, #282) the call may have created the folder (b.jpg lands
        there), so the row says to upload again; not after a refused connection or a 4xx."""
        reply, maybe_created = self._SETTING_USER_REPLIES[answer]
        fake = self._root(reply)
        out, sent = await self._upload(
            tmp_path, _NO_FOLDER, _NEW, fake=fake, names=("a.jpg", "b.jpg")
        )
        assert sent == 2  # a.jpg once, then b.jpg
        lines = out.splitlines()
        assert "homes/<user>/Photos; 117" in lines[1] and "failed" in lines[1]
        maybe = "so the folder may exist now. Upload this file again; if it fails the same way"
        assert (maybe in lines[1]) is maybe_created
        assert lines[2].split()[:2] == ["b.jpg", "new"]

    async def test_upload_again_only_for_the_first_file(self, tmp_path: Path) -> None:
        """After no answer from Setting.User, only the file whose 117 came before the call
        is told to upload again: a later file's 117, sent after the call, says the folder
        still isn't there."""
        fake = self._root(self._timeout)
        out, sent = await self._upload(
            tmp_path,
            _NO_FOLDER,
            _NO_FOLDER,
            _NO_FOLDER,
            fake=fake,
            names=("a.jpg", "b.jpg", "c.jpg"),
        )
        assert sent == 3
        rows = out.splitlines()[1:4]
        assert "so the folder may exist now. Upload this file again" in rows[0]
        for row in rows[1:]:
            assert "has no folder on the NAS (homes/<user>/Photos; 117)" in row

    async def test_resend_keeps_the_transient_retry(self, tmp_path: Path) -> None:
        """The resend after Setting.User is an attempt like the first: a 623 on it is
        retried once after the delay, as a first attempt's would be."""
        fake = self._root()
        busy = {"success": False, "error": {"code": 623}}
        out, sent = await self._upload(tmp_path, _NO_FOLDER, busy, _NEW, fake=fake)
        assert sent == 3
        assert "new      id 5" in out

    async def test_delayed_retry_gets_setting_user(self, tmp_path: Path) -> None:
        """A 117 on the delayed retry after a transient code still calls Setting.User (#440
        QA): a new account on a freshly booted NAS can meet 623 first, then 117."""
        fake = self._root()
        busy = {"success": False, "error": {"code": 623}}
        out, sent = await self._upload(
            tmp_path, busy, _NO_FOLDER, _NEW, _NEW, fake=fake, names=("a.jpg", "b.jpg")
        )
        assert sent == 4  # a.jpg 623, a.jpg 117, a.jpg after Setting.User, b.jpg
        assert len(fake.of("SYNO.Foto.Setting.User")) == 1
        assert out.splitlines()[0] == "Uploaded 2 of 2 file(s) to / (personal space)"

    @pytest.mark.parametrize("where", ["subfolder", "shared"])
    async def test_not_the_personal_root(self, tmp_path: Path, where: str) -> None:
        """A subfolder's existence means the space's folder exists, and Shared Space's root
        is a shared folder of its own (inferred, not probed): a 117 there is reported as
        DSM's, with no extra call."""
        if where == "subfolder":
            fake = FakePhotos().on(
                "SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-by-name")
            )
            kwargs: dict[str, Any] = {"folder": "/Trips"}
        else:
            fake = FakePhotos().on(
                "SYNO.FotoTeam.Browse.Folder", "get", 2, captured("folder-get-root")
            )
            kwargs = {"space": "shared"}
        out, sent = await self._upload(tmp_path, _NO_FOLDER, fake=fake, **kwargs)
        assert sent == 1
        assert not fake.of("SYNO.Foto.Setting.User")
        assert "homes/<user>/Photos" not in out
        assert "(117)" in out


class TestDestName:
    """#450: every name is unique within the call, whatever the order and size.

    Previews couldn't hit #450 before either: a preview name ends ``_<size>.jpg`` and an
    ID-suffixed one ``_<id>.jpg``, and IDs are deduplicated first. They're here so a
    change to the naming keeps them unique too.
    """

    @staticmethod
    def _names(items: list[tuple[int, str]], size: str) -> list[str]:
        used: set[str] = set()
        return [_dest_name({"id": i, "filename": f}, size, used) for i, f in items]

    @pytest.mark.parametrize(
        ("items", "size", "expected"),
        [
            (
                [(1, "a_9.jpg"), (8, "a.jpg"), (9, "a.jpg")],
                "original",
                ["a_9.jpg", "a.jpg", "a_9_2.jpg"],
            ),
            (
                [(9, "a.jpg"), (8, "a.jpg"), (1, "a_9.jpg")],
                "original",
                ["a.jpg", "a_8.jpg", "a_9.jpg"],
            ),
            (
                [(8, "a.jpg"), (9, "a.jpg"), (1, "a_9.jpg")],
                "original",
                ["a.jpg", "a_9.jpg", "a_9_1.jpg"],
            ),
            (
                [(1, "a_m_9.jpg"), (8, "a.png"), (9, "a.jpg")],
                "m",
                ["a_m_9_m.jpg", "a_m.jpg", "a_m_9.jpg"],
            ),
            (
                [(1, "a_m_9.jpg"), (8, "a.jpg"), (9, "a.png"), (4, "a_m_9.png")],
                "m",
                ["a_m_9_m.jpg", "a_m.jpg", "a_m_9.jpg", "a_m_9_m_4.jpg"],
            ),
            (
                [(1, "A.JPG"), (2, "a.jpg"), (3, "a_2.JPG")],
                "original",
                ["A.JPG", "a_2.jpg", "a_2_3.JPG"],
            ),
        ],
    )
    def test_names_are_unique(
        self, items: list[tuple[int, str]], size: str, expected: list[str]
    ) -> None:
        names = self._names(items, size)
        assert names == expected
        assert len({_name_key(n) for n in names}) == len(names)

    def test_many_repeats(self) -> None:
        items = [(7, "x.jpg")] * 2 + [(1, f"x_7_{n}.jpg") for n in range(2, 5)] + [(7, "x.jpg")]
        names = self._names(items, "original")
        assert len({_name_key(n) for n in names}) == len(names)


class TestDestNameAcrossFilesystems:
    """QA round 1 on #462: names that are one file on macOS or Windows count as one."""

    @staticmethod
    def _names(items: list[tuple[int, str]]) -> list[str]:
        used: set[str] = set()
        return [_dest_name({"id": i, "filename": f}, "original", used) for i, f in items]

    def test_nfc_and_nfd_are_one_name(self) -> None:
        import unicodedata

        nfc = unicodedata.normalize("NFC", "café.jpg")
        nfd = unicodedata.normalize("NFD", "café.jpg")
        assert nfc != nfd
        names = self._names([(1, nfc), (2, nfd)])
        assert names[0] == nfc
        assert unicodedata.normalize("NFC", names[1]) == "café_2.jpg"

    @pytest.mark.parametrize(
        ("variant", "second"),
        [
            ("a.jpg.", "a_2.jpg"),
            ("a.jpg ", "a_2.jpg"),
            ("a.jpg. .", "a_2.jpg"),
            ("A.JPG", "A_2.JPG"),
        ],
    )
    def test_trailing_dots_spaces_and_case_are_one_name(self, variant: str, second: str) -> None:
        assert self._names([(1, "a.jpg"), (2, variant)]) == ["a.jpg", second]

    def test_only_dots_and_spaces_fall_back(self) -> None:
        assert self._names([(7, ". ."), (8, "...")]) == ["item-7", "item-8"]


class TestRetryAdvice:
    """#467 (pre-QA on QA round 1): any item of a multi-item call that isn't downloaded,
    not only items left after DSM stops answering, gets the whole-call retry advice."""

    ADVICE = "To retry, repeat this call with the same ids in the same order"

    @respx.mock
    async def test_a_preview_not_ready_gets_the_advice(self, tmp_path: Path) -> None:
        items = _items("ready", "converting")
        for item in items["list"]:
            item["filename"] = "a.jpg"
        fake = FakePhotos().on(ITEM, "get", 2, items)
        fake.on("SYNO.Foto.Thumbnail", "get", 2, binary(b"jpeg"))
        install(fake)
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12, 15], size="m", dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert "15  a_m_15.jpg: not downloaded" in out
        assert self.ADVICE in out

    @respx.mock
    async def test_dsm_stopping_on_the_last_item_gets_the_advice(self, tmp_path: Path) -> None:
        def timeout(_params: dict[str, Any]) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        fake = FakePhotos().on(ITEM, "get", 2, _items("ready", "ready"))
        fake.on("SYNO.Foto.Download", "download", 1, binary(b"one"), timeout)
        install(fake)
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12, 15], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert "not tried" not in out
        assert self.ADVICE in out

    @respx.mock
    async def test_no_advice_when_all_were_downloaded(self, tmp_path: Path) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items("ready", "ready"))
        fake.on("SYNO.Foto.Download", "download", 1, binary(b"one"), binary(b"two"))
        install(fake)
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12, 15], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert "Downloaded 2 of 2" in out and self.ADVICE not in out

    @respx.mock
    async def test_the_advised_retry_ends_without_the_advice(self, tmp_path: Path) -> None:
        """QA round 2 on #462: repeating the call exactly, as advised, finds the files the
        first run wrote; they're kept without suggesting overwrite=true, and the advice
        goes away once every item is downloaded or already there."""

        def timeout(_params: dict[str, Any]) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        items = _items("ready", "ready")
        third = json.loads(json.dumps(items["list"][1]))
        third["id"] = 16
        items["list"].append(third)
        for item, name, data in zip(
            items["list"], ["a.jpg", "a.jpg", "b.jpg"], [b"one", b"two", b"three"], strict=True
        ):
            item["filename"], item["filesize"] = name, len(data)
        fake = FakePhotos().on(ITEM, "get", 2, items)
        fake.on("SYNO.Foto.Download", "download", 1, binary(b"one"), timeout)
        install(fake)
        client = make_client()
        async with client:
            first = await download_photos(
                client, ids=[12, 15, 16], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert self.ADVICE in first

        fake = FakePhotos().on(ITEM, "get", 2, items)
        fake.on("SYNO.Foto.Download", "download", 1, binary(b"two"), binary(b"three"))
        install(fake)
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12, 15, 16], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert out.startswith("Downloaded 2 of 3") and ", 1 already there)" in out
        assert (
            "12  a.jpg: already there: a file with this name and this item's size, most "
            "likely this item from an earlier run; same name and size only, not verified" in out
        )
        assert "overwrite=true" not in out and self.ADVICE not in out
        assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg", "a_15.jpg", "b.jpg"]
        assert (tmp_path / "a.jpg").read_bytes() == b"one"

    @respx.mock
    async def test_a_failure_a_retry_cant_fix_gets_no_advice(self, tmp_path: Path) -> None:
        """No preview, or an id not found: the advice would come back on every retry
        (pre-QA on QA round 2 on #462)."""
        install(FakePhotos().on(ITEM, "get", 2, _items("broken", "broken")))
        client = make_client()
        async with client:
            out = await download_photos(
                client,
                ids=[12, 15, 99],
                size="m",
                dest_folder=str(tmp_path),
                allowed_roots=[tmp_path],
            )
        assert "no m preview for this item (broken)" in out and "99  not downloaded" in out
        assert self.ADVICE not in out

    @respx.mock
    async def test_an_item_found_later_never_takes_another_s_file(self, tmp_path: Path) -> None:
        """QA round 2's observation: 12 isn't found, so 15 is written as a.jpg; when 12 is
        found on a later call it's named a.jpg, finds 15's file (another size) and keeps
        it, saying it's another file."""
        items = _items("ready", "ready")
        for item, size in zip(items["list"], [9, 3], strict=True):
            item["filename"], item["filesize"] = "a.jpg", size
        fake = FakePhotos().on(ITEM, "get", 2, {"list": items["list"][1:]})
        fake.on("SYNO.Foto.Download", "download", 1, binary(b"two"))
        install(fake)
        client = make_client()
        async with client:
            await download_photos(
                client, ids=[12, 15], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        fake = FakePhotos().on(ITEM, "get", 2, items)
        fake.on("SYNO.Foto.Download", "download", 1, binary(b"two"))
        install(fake)
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12, 15], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert "12  a.jpg: not downloaded: another file has this name" in out
        assert (tmp_path / "a.jpg").read_bytes() == b"two"

    @respx.mock
    @pytest.mark.parametrize(
        ("answer", "advice"),
        [
            ({"success": False, "error": {"code": 105}}, False),  # permission: not retryable
            (httpx.Response(502), True),  # a proxy's 5xx: retryable
            ("enospc", True),
            ("eacces", False),
            ("precheck", True),  # the client's free-space check (QA round 3 on #462)
        ],
        ids=["dsm-105", "http-502", "enospc", "eacces", "free-space-precheck"],
    )
    async def test_advice_follows_whether_a_retry_can_help(
        self, tmp_path: Path, answer: Any, advice: bool
    ) -> None:
        """Pre-QA on QA round 2 (#462): DSM's own retryable flag decides, and of local
        errors only a full disk is worth a retry."""
        import errno

        fake = FakePhotos().on(ITEM, "get", 2, _items("ready", "ready"))
        if answer == "precheck":
            # The real client: DSM sends a length, and the local disk has less free.
            fake.on("SYNO.Foto.Download", "download", 1, binary(b"x"), binary(b"y"))
            free = MagicMock(free=0)
            patcher = patch("mcp_synology.core.client.shutil.disk_usage", return_value=free)
        elif isinstance(answer, str):
            fake.on("SYNO.Foto.Download", "download", 1, binary(b"x"), binary(b"y"))
            code = errno.ENOSPC if answer == "enospc" else errno.EACCES
            patcher = patch(
                "mcp_synology.core.client.DsmClient.download_binary",
                AsyncMock(side_effect=OSError(code, os.strerror(code))),
            )
        else:
            response = (
                answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)
            )

            def reply(_p: dict[str, Any]) -> httpx.Response:
                return response

            fake.on("SYNO.Foto.Download", "download", 1, reply, reply)
            patcher = contextlib.nullcontext()
        install(fake)
        client = make_client()
        with patcher:
            async with client:
                out = await download_photos(
                    client, ids=[12, 15], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
                )
        assert "Downloaded 0 of 2" in out
        assert (self.ADVICE in out) is advice

    @respx.mock
    async def test_a_folder_with_the_name_is_reported(self, tmp_path: Path) -> None:
        (tmp_path / "forest-trail.jpg").mkdir()
        install(FakePhotos().on(ITEM, "get", 2, {"list": _items("ready")["list"][:1]}))
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert "12  forest-trail.jpg: not downloaded: a folder has that name" in out

    @respx.mock
    async def test_a_file_that_appears_while_downloading_is_kept(self, tmp_path: Path) -> None:
        """Another call or program wrote it, so no overwrite=true hint (QA round 3 on
        #462)."""
        install(FakePhotos().on(ITEM, "get", 2, {"list": _items("ready")["list"][:1]}))
        client = make_client()
        with patch(
            "mcp_synology.core.client.DsmClient.download_binary",
            AsyncMock(side_effect=FileExistsError("x")),
        ):
            async with client:
                out = await download_photos(
                    client, ids=[12], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
                )
        assert (
            "12  forest-trail.jpg: not downloaded: a file with this name appeared while "
            "downloading, written by something else (kept)" in out
        )
        assert "overwrite=true" not in out

    @respx.mock
    @pytest.mark.parametrize("filesize", ["not a number", None])
    async def test_an_unreadable_filesize_is_another_file(
        self, tmp_path: Path, filesize: object
    ) -> None:
        (tmp_path / "forest-trail.jpg").write_bytes(b"mine")
        items = {"list": _items("ready")["list"][:1]}
        items["list"][0]["filesize"] = filesize
        install(FakePhotos().on(ITEM, "get", 2, items))
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12], dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert "12  forest-trail.jpg: not downloaded: another file has this name" in out

    @respx.mock
    async def test_an_existing_preview_says_both_cases(self, tmp_path: Path) -> None:
        (tmp_path / "forest-trail_m.jpg").write_bytes(b"x")
        install(FakePhotos().on(ITEM, "get", 2, _items("ready", "ready")))
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12], size="m", dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert (
            "12  forest-trail_m.jpg: not downloaded: a file with this name is already there "
            "(if an earlier run of this call wrote it, nothing to do; otherwise overwrite=true "
            "replaces it, or choose another folder)" in out
        )

    @respx.mock
    async def test_no_advice_for_a_single_item(self, tmp_path: Path) -> None:
        install(FakePhotos().on(ITEM, "get", 2, {"list": _items("converting")["list"][:1]}))
        client = make_client()
        async with client:
            out = await download_photos(
                client, ids=[12], size="m", dest_folder=str(tmp_path), allowed_roots=[tmp_path]
            )
        assert "not downloaded" in out and self.ADVICE not in out


def test_name_key_is_a_canonical_caseless_match() -> None:
    # U+0390 and U+0399 U+0308 U+0301 are one name under Unicode's canonical caseless
    # match; NFC-then-casefold alone keyed them apart.
    assert _name_key("ΐ.jpg") == _name_key("Ϊ́.jpg")
