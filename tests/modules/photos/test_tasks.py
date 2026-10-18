"""copy_photos, move_photos, deletes and photo_task: start, poll, hand off (#258)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.photos import tasks
from mcp_synology.modules.photos.tasks import (
    copy_or_move,
    delete_photo_folders,
    delete_photos,
    parse_handle,
    photo_task,
)
from tests.modules.photos.dsm import CAPTURE_191, FakePhotos, captured, install, make_client

ITEM = "SYNO.Foto.Browse.Item"
FOLDER = "SYNO.Foto.Browse.Folder"
FILE = "SYNO.Foto.BackgroundTask.File"
INFO = "SYNO.Foto.BackgroundTask.Info"


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "POLL_INTERVAL", 0)


def _error(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    text = str(exc.value)
    return json.loads(text[text.index("{") :])["error"]  # type: ignore[no-any-return]


def _items(*ids: int) -> dict[str, Any]:
    return {"list": [{"id": i, "filename": f"f{i}.jpg", "folder_id": 10} for i in ids]}


_DEST = {"folder": {"id": 14, "name": "/mcp-spec-capture/dest", "parent": 10}}


class TestCopyMove:
    @respx.mock
    async def test_copy_polls_until_done(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(12)).on(FOLDER, "get", 2, _DEST)
        fake.on(FILE, "copy", 1, captured("bg-copy"))
        fake.on(INFO, "get_status", 1, captured("bg-copy-status"), captured("bg-copy-status_last"))
        install(fake)
        client = make_client()
        async with client:
            out = await copy_or_move(
                client, operation="copy", ids=[12, 99], folders=None, destination="14"
            )
        assert fake.one(FILE, "copy").params == {
            "target_folder_id": 14,
            "item_id": [12],
            "folder_id": [],
            "action": "skip",
        }
        assert [c.params for c in fake.of(INFO, "get_status")] == [{"id": [5]}, {"id": [5]}]
        assert out.splitlines()[0] == (
            "Copied 1 item(s) to /mcp-spec-capture/dest (personal space): 1 of 1 done, "
            "0 skipped (already there), 0 overwritten."
        )
        assert "Not found: 99" in out

    @respx.mock
    async def test_skip_counted(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(12)).on(FOLDER, "get", 2, _DEST)
        fake.on(FILE, "copy", 1, captured("bg-copy-again-skip"))
        fake.on(INFO, "get_status", 1, captured("bg-copy-again-status"))
        install(fake)
        client = make_client()
        async with client:
            out = await copy_or_move(
                client, operation="copy", ids=[12], folders=None, destination="14"
            )
        assert "1 skipped (already there)" in out

    @respx.mock
    async def test_timeout_hands_off(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(12)).on(FOLDER, "get", 2, _DEST)
        fake.on(FILE, "move", 1, captured("bg-move"))
        install(fake)
        client = make_client()
        async with client:
            out = await copy_or_move(
                client, operation="move", ids=[12], folders=None, destination="14", timeout=0
            )
        assert "Still moving on the NAS (0 of 1 done). Handle: photos:6" in out
        assert not fake.of(INFO)

    @respx.mock
    async def test_no_answer_hands_off(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(12)).on(FOLDER, "get", 2, _DEST)
        fake.on(FILE, "copy", 1, captured("bg-copy"))

        def boom(_p: dict[str, Any]) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        fake.on(INFO, "get_status", 1, boom)
        install(fake)
        client = make_client()
        async with client:
            out = await copy_or_move(
                client, operation="copy", ids=[12], folders=None, destination="14"
            )
        assert "didn't answer a status check (ReadTimeout)" in out
        assert "photos:5" in out

    @respx.mock
    async def test_failed_items_listed(self) -> None:
        done = captured("bg-copy-status_last")
        done["list"][0]["error"] = 1
        fake = FakePhotos().on(ITEM, "get", 2, _items(12)).on(FOLDER, "get", 2, _DEST)
        fake.on(FILE, "copy", 1, captured("bg-copy")).on(INFO, "get_status", 1, done)
        fake.on(
            INFO,
            "get_error_detail",
            1,
            {"list": [{"type": "item", "id": 12, "reason": "not_existed"}]},
        )
        install(fake)
        client = make_client()
        async with client:
            out = await copy_or_move(
                client, operation="copy", ids=[12], folders=None, destination="14"
            )
        assert "1 failed" in out and "item 12: not_existed" in out
        assert fake.one(INFO, "get_error_detail").params == {"id": 5}

    @respx.mock
    async def test_cross_space_goes_to_the_source_api(self) -> None:
        """Through the destination's API DSM can't find the items (verified on 7.2.2)."""
        fake = FakePhotos().on(ITEM, "get", 2, _items(12))
        fake.on("SYNO.FotoTeam.Browse.Folder", "get", 2, {"folder": {"id": 17, "name": "/team"}})
        # A task that's already done when started: no status poll needed.
        fake.on(FILE, "copy", 1, {"task_info": captured("bg-copy-status_last")["list"][0]})
        install(fake)
        client = make_client()
        async with client:
            out = await copy_or_move(
                client,
                operation="copy",
                ids=[12],
                folders=None,
                destination="/team",
                dest_space="shared",
            )
        params = fake.one(FILE, "copy").params
        assert json.loads(params["extra_info"]) == {
            "version": 2,
            "source_library": "personal_space",
        }
        assert params["target_folder_id"] == 17
        assert "to /team (Shared Space)" in out

    async def test_move_out_of_shared_space_refused(self) -> None:
        with pytest.raises(ToolError) as exc:
            await copy_or_move(
                make_client(),
                operation="move",
                ids=[1],
                folders=None,
                destination="/",
                space="shared",
                dest_space="personal",
            )
        assert "copy_photos" in _error(exc)["suggestion"]

    @respx.mock
    async def test_folders_and_root_refused(self) -> None:
        fake = FakePhotos().on(FOLDER, "get", 2, captured("folder-get-root"))
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await copy_or_move(
                    client, operation="copy", ids=None, folders=["/"], destination="/"
                )
        assert _error(exc)["param"] == "folders"

    @pytest.mark.parametrize(
        ("kwargs", "param"),
        [
            ({"ids": None, "folders": None}, "ids"),
            ({"ids": [1], "folders": None, "on_conflict": "replace"}, "on_conflict"),
            ({"ids": [1], "folders": None, "dest_space": "team"}, "space"),
        ],
    )
    async def test_refusals(self, kwargs: dict[str, Any], param: str) -> None:
        with pytest.raises(ToolError) as exc:
            await copy_or_move(make_client(), operation="copy", destination="/", **kwargs)
        assert _error(exc)["param"] == param


class TestDeletes:
    @respx.mock
    async def test_delete_items_with_recycle_bin(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(25))
        fake.on(FILE, "delete", 1, captured("fav191_bg-delete"))
        fake.on(INFO, "get_status", 1, captured("fav191_bg-delete-status"))
        fake.on("SYNO.Foto.Setting.Admin", "get", 1, captured("qa1_setting-admin-get", CAPTURE_191))
        install(fake)
        client = make_client(CAPTURE_191)
        async with client:
            out = await delete_photos(client, ids=[25, 26])
        assert fake.one(FILE, "delete").params == {"item_id": [25], "folder_id": []}
        assert out.splitlines()[0] == (
            "Deleted 1 item(s) in the personal space; they're in the recycle bin of the share "
            "that holds the space."
        )
        assert "Not found: 26" in out

    @respx.mock
    async def test_shared_space_without_recycle_bin(self) -> None:
        fake = FakePhotos().on("SYNO.FotoTeam.Browse.Item", "get", 2, _items(25))
        fake.on("SYNO.FotoTeam.BackgroundTask.File", "delete", 1, captured("fav191_bg-delete"))
        fake.on(INFO, "get_status", 1, captured("fav191_bg-delete-status"))
        fake.on("SYNO.Foto.Setting.Admin", "get", 1, captured("qa1_setting-admin-get", CAPTURE_191))
        install(fake)
        client = make_client(CAPTURE_191)
        async with client:
            out = await delete_photos(client, ids=[25], space="shared")
        assert "deleted permanently: that share has no recycle bin" in out

    @respx.mock
    async def test_134_has_no_recycle_flags(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(25))
        fake.on(FILE, "delete", 1, captured("fav191_bg-delete"))
        fake.on(INFO, "get_status", 1, captured("fav191_bg-delete-status"))
        fake.on("SYNO.Foto.Setting.Admin", "get", 1, captured("qa1_setting-admin-get"))
        install(fake)
        client = make_client()
        async with client:
            out = await delete_photos(client, ids=[25])
        assert "if it has one" in out

    @respx.mock
    async def test_nothing_sent_for_unknown_ids(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, {"list": []})
        install(fake)
        client = make_client()
        async with client:
            out = await delete_photos(client, ids=[1])
        assert out.startswith("Nothing deleted")
        assert not fake.of(FILE)

    @respx.mock
    async def test_delete_folders(self) -> None:
        fake = FakePhotos().on(FOLDER, "get", 2, captured("folder-get-by-name"))
        fake.on(ITEM, "count", 1, {"count": 4})
        fake.on(FILE, "delete", 1, captured("fav191_bg-delete"))
        fake.on(INFO, "get_status", 1, captured("fav191_bg-delete-status"))
        fake.on("SYNO.Foto.Setting.Admin", "get", 1, captured("qa1_setting-admin-get"))
        install(fake)
        client = make_client()
        async with client:
            out = await delete_photo_folders(client, folders=["/mcp-spec-capture"])
        assert fake.one(FILE, "delete").params == {"item_id": [], "folder_id": [10]}
        assert out.startswith("Deleted /mcp-spec-capture (4 item(s) directly in it)")

    @respx.mock
    async def test_delete_root_refused(self) -> None:
        install(FakePhotos().on(FOLDER, "get", 2, captured("folder-get-root")))
        client = make_client()
        async with client:
            with pytest.raises(ToolError):
                await delete_photo_folders(client, folders=["/"])

    @respx.mock
    async def test_803_is_permission_denied(self) -> None:
        fake = FakePhotos().on("SYNO.FotoTeam.Browse.Item", "get", 2, _items(5))
        fake.error("SYNO.FotoTeam.BackgroundTask.File", "delete", 1, 803)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await delete_photos(client, ids=[5], space="shared")
        assert _error(exc)["code"] == "permission_denied"


class TestPhotoTask:
    def test_parse_handle(self) -> None:
        assert parse_handle("Op", " `photos:14`. ") == 14
        with pytest.raises(ToolError):
            parse_handle("Op", "delete:FileStation_1")

    @respx.mock
    async def test_status(self) -> None:
        fake = FakePhotos().on(INFO, "get_status", 1, captured("bg-copy-status"))
        fake.on(FOLDER, "get", 2, _DEST)
        install(fake)
        client = make_client()
        async with client:
            out = await photo_task(client, handle="photos:5")
        assert out.startswith("photos:5 copy to /mcp-spec-capture/dest: processing, 0 of 1 done")
        assert "running" in out

    @respx.mock
    async def test_stop_unknown_is_not_found(self) -> None:
        install(FakePhotos().error(INFO, "abort_task", 1, 117))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_task(client, handle="photos:999999", action="stop")
        assert _error(exc)["code"] == "not_found"

    @respx.mock
    async def test_stop(self) -> None:
        fake = FakePhotos().on(INFO, "abort_task", 1, {})
        fake.on(INFO, "get_status", 1, captured("bg-copy-status_last"))
        fake.on(FOLDER, "get", 2, _DEST)
        install(fake)
        client = make_client()
        async with client:
            out = await photo_task(client, handle="photos:5", action="stop")
        assert fake.one(INFO, "abort_task").params == {"id": [5]}
        assert "Stop requested" in out

    @respx.mock
    async def test_list_and_clear(self) -> None:
        listed = captured("bg-list-user-task")
        fake = FakePhotos().on(INFO, "list_user_task", 1, listed, listed, {"list": []})
        fake.on(FOLDER, "get", 2, _DEST).on(INFO, "clear_completed_task", 1, {})
        install(fake)
        client = make_client()
        async with client:
            table = await photo_task(client)
            cleared = await photo_task(client, action="clear")
        assert "photos:1" in table and "copy" in table
        assert cleared == f"Removed {len(listed['list'])} finished task(s) from the list."

    async def test_stop_needs_handle(self) -> None:
        with pytest.raises(ToolError) as exc:
            await photo_task(make_client(), action="stop")
        assert _error(exc)["param"] == "handle"
