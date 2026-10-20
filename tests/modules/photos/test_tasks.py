"""copy_photos, move_photos, deletes and photo_task: start, poll, hand off (#258)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import DsmHttpError
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
HTML = {"content-type": "text/html"}


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
    @pytest.mark.parametrize(
        ("reply", "shown"),
        [
            (httpx.Response(504, text="<html>Gateway Timeout</html>"),
             "The NAS, or a proxy in front of it, answered a status check with an error "
             "(HTTP 504); the task is most likely still copying."),
            # #325: a status URL is short, so a 414 comes from a proxy too.
            (httpx.Response(414, text="<html>Too long</html>"),
             "The NAS, or a proxy in front of it, answered a status check with an error "
             "(HTTP 414); the task is most likely still copying."),
            (httpx.Response(200, text="<html>Sign in</html>", headers=HTML),
             "The NAS's status reply couldn't be read (not a DSM reply; a proxy or captive "
             "portal may be answering); the task is most likely still copying."),
        ],
    )  # fmt: skip
    async def test_proxy_error_hands_off(self, reply: httpx.Response, shown: str) -> None:
        """#282: a proxy's 502/504 or login page on a status read is how a proxy reports a
        stall; the task is handed off, not lost or reported as failed."""
        fake = FakePhotos().on(ITEM, "get", 2, _items(12)).on(FOLDER, "get", 2, _DEST)
        fake.on(FILE, "copy", 1, captured("bg-copy")).on(INFO, "get_status", 1, lambda _p: reply)
        install(fake)
        client = make_client()
        async with client:
            out = await copy_or_move(
                client, operation="copy", ids=[12], folders=None, destination="14"
            )
        assert out == (
            f"Copy of 1 item(s) to /mcp-spec-capture/dest (personal space): {shown} "
            "Handle: photos:5 — check with photo_task."
        )

    @respx.mock
    async def test_relogin_proxy_error_hands_off(self) -> None:
        """#321: a session error on the status read, then a proxy 502 on the re-login,
        used to stop the task as if DSM had answered "Invalid session"."""
        fake = FakePhotos().on(ITEM, "get", 2, _items(12)).on(FOLDER, "get", 2, _DEST)
        fake.on(FILE, "copy", 1, captured("bg-copy")).error(INFO, "get_status", 1, 119)
        install(fake)
        client = make_client()

        async def reauth() -> None:
            raise DsmHttpError("SYNO.API.Auth/login: DSM answered HTTP 502.", 502)

        client.set_re_auth_callback(reauth)
        async with client:
            out = await copy_or_move(
                client, operation="copy", ids=[12], folders=None, destination="14"
            )
        assert "answered a status check with an error (HTTP 502)" in out
        assert "photos:5" in out
        assert not fake.of(INFO, "abort_task")

    @respx.mock
    @pytest.mark.parametrize("abort_ok", [True, False])
    async def test_dsm_error_on_status_stops_the_task(self, abort_ok: bool) -> None:
        """QA R1: a DSM error code on a status read lost the handle and left the task
        running. It stops the task, as File Station's poll_task, and says so."""
        fake = FakePhotos().on(ITEM, "get", 2, _items(12)).on(FOLDER, "get", 2, _DEST)
        fake.on(FILE, "copy", 1, captured("bg-copy")).error(INFO, "get_status", 1, 105)
        if abort_ok:
            fake.on(INFO, "abort_task", 1, {})
        else:
            fake.error(INFO, "abort_task", 1, 117)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await copy_or_move(
                    client, operation="copy", ids=[12], folders=None, destination="14"
                )
        assert fake.one(INFO, "abort_task").params == {"id": [5]}
        err = _error(exc)
        assert err["code"] == "permission_denied"
        head = "Copy photos failed (DSM error 105): Permission denied. "
        if abort_ok:
            assert err["message"] == (
                head + "The task was stopped on the NAS; what was already done stays done."
            )
        else:
            assert err["message"] == (
                head + "Stopping it failed, so it may still be running: photos:5 "
                "(check with photo_task)."
            )

    @respx.mock
    async def test_cancel_stops_the_task(self) -> None:
        """QA R1: a canceled call (a client's own timeout) left the task running."""
        fake = FakePhotos().on(ITEM, "get", 2, _items(12)).on(FOLDER, "get", 2, _DEST)
        fake.on(FILE, "copy", 1, captured("bg-copy"))
        fake.on(INFO, "get_status", 1, captured("bg-copy-status")).on(INFO, "abort_task", 1, {})
        install(fake)
        client = make_client()
        async with client:
            call = asyncio.ensure_future(
                copy_or_move(client, operation="copy", ids=[12], folders=None, destination="14")
            )
            while not fake.of(INFO, "get_status"):
                await asyncio.sleep(0)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        assert fake.one(INFO, "abort_task").params == {"id": [5]}

    @respx.mock
    async def test_unexpected_status_reply_hands_off(self) -> None:
        """Any other unexpected answer hands the task off rather than losing it."""
        fake = FakePhotos().on(ITEM, "get", 2, _items(12)).on(FOLDER, "get", 2, _DEST)
        fake.on(FILE, "copy", 1, captured("bg-copy")).on(INFO, "get_status", 1, {"list": [7]})
        install(fake)
        client = make_client()
        async with client:
            out = await copy_or_move(
                client, operation="copy", ids=[12], folders=None, destination="14"
            )
        assert out == (
            "Copy of 1 item(s) to /mcp-spec-capture/dest (personal space): The NAS's status "
            "reply couldn't be read (TypeError); the task is most likely still copying. "
            "Handle: photos:5 — check with photo_task."
        )
        assert not fake.of(INFO, "abort_task")

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
            ({"ids": None, "folders": [f"/f{i}" for i in range(1001)]}, "folders"),
        ],
    )
    async def test_refusals(self, kwargs: dict[str, Any], param: str) -> None:
        with pytest.raises(ToolError) as exc:
            await copy_or_move(make_client(), operation="copy", destination="/", **kwargs)
        assert _error(exc)["param"] == param

    async def test_delete_folders_capped(self) -> None:
        """Each folder costs a lookup, so the list is capped like ID lists (#272's QA)."""
        with pytest.raises(ToolError) as exc:
            await delete_photo_folders(make_client(), folders=[f"/f{i}" for i in range(1001)])
        assert _error(exc)["param"] == "folders"


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
    async def test_recycle_setting_unanswered_keeps_the_result(self) -> None:
        """QA R1: a finished delete became an error when the setting read timed out."""

        def boom(_p: dict[str, Any]) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        fake = FakePhotos().on(ITEM, "get", 2, _items(25))
        fake.on(FILE, "delete", 1, captured("fav191_bg-delete"))
        fake.on(INFO, "get_status", 1, captured("fav191_bg-delete-status"))
        fake.on("SYNO.Foto.Setting.Admin", "get", 1, boom)
        install(fake)
        client = make_client(CAPTURE_191)
        async with client:
            out = await delete_photos(client, ids=[25])
        assert out.splitlines()[0] == (
            "Deleted 1 item(s) in the personal space; they're in the recycle bin of the share "
            "that holds the space, if it has one."
        )

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

    @pytest.mark.parametrize("handle", ["photos:\uff15", "photos:\u0665", "photos:\u00b2"])
    def test_parse_handle_takes_ascii_digits_only(self, handle: str) -> None:
        """A fullwidth or Arabic-Indic digit isn't task 5 (as #272's album IDs)."""
        with pytest.raises(ToolError) as exc:
            parse_handle("Op", handle)
        assert _error(exc)["param"] == "handle"

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

    @respx.mock
    @pytest.mark.parametrize(
        ("reply", "code", "detail", "retryable"),
        [
            (httpx.ReadTimeout("slow"), "timeout",
             "DSM didn't answer the status request (ReadTimeout: slow).", True),
            (httpx.Response(502, text="<html>Bad Gateway</html>"), "unavailable",
             f"{INFO}/get_status: DSM answered HTTP 502.", True),
            (httpx.Response(403, text="<html>Forbidden</html>"), "unavailable",
             f"{INFO}/get_status: DSM answered HTTP 403.", False),
            # A 414 comes from a proxy too: a status URL is short (#325).
            (httpx.Response(414, text="<html>Too long</html>"), "unavailable",
             f"{INFO}/get_status: DSM rejected the request with HTTP 414 (Request-URI Too "
             "Large).", False),
            (httpx.Response(200, text="<html>Sign in</html>", headers=HTML),
             "unavailable",
             f"{INFO}/get_status: the answer wasn't a DSM reply (Content-Type text/html).", True),
        ],
    )  # fmt: skip
    @pytest.mark.parametrize("action", ["status", "stop"])
    async def test_status_read_fails_names_the_handle(
        self, action: str, reply: Any, code: str, detail: str, retryable: bool
    ) -> None:
        """#282: a status read with no answer was a raw ReadTimeout; a proxy's 502 or page
        gave a generic error with no handle, and after a stop didn't say it was sent."""

        def answer(_p: dict[str, Any]) -> httpx.Response:
            if isinstance(reply, Exception):
                raise reply
            return reply  # type: ignore[no-any-return]

        fake = FakePhotos().on(INFO, "abort_task", 1, {}).on(INFO, "get_status", 1, answer)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_task(client, handle="photos:5", action=action)
        err = _error(exc)
        note = " The stop was requested; its status couldn't be read." if action == "stop" else ""
        assert err["code"] == code
        assert err["message"] == f"Photo task failed for `photos:5`: {detail}{note}"
        assert err["retryable"] is retryable
        assert (err["param"], err["value"]) == ("handle", "photos:5")
        assert err["suggestion"].endswith(
            "Call photo_task with this handle to see whether the task is still running."
        )
        assert "Reduce the size" not in err["suggestion"]
        if "DSM reply" in detail:  # the error's own hint comes first, as in file_task
            assert "reverse proxy" in err["suggestion"]
        assert len(fake.of(INFO, "abort_task")) == (action == "stop")

    @respx.mock
    @pytest.mark.parametrize(
        ("failure", "detail", "note"),
        [
            (httpx.ReadTimeout("slow"), "DSM didn't answer the stop request (ReadTimeout: slow).",
             "The stop may or may not have taken effect."),
            (httpx.ConnectError("refused"),
             "DSM couldn't be reached for the stop request (ConnectError: refused).",
             "Nothing was stopped."),
        ],
    )  # fmt: skip
    async def test_stop_request_without_answer(
        self, failure: Exception, detail: str, note: str
    ) -> None:
        """A refused connection never sent the stop; a timeout may have."""

        def boom(_p: dict[str, Any]) -> httpx.Response:
            raise failure

        fake = FakePhotos().on(INFO, "abort_task", 1, boom)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_task(client, handle="photos:5", action="stop")
        assert _error(exc)["message"] == f"Photo task failed for `photos:5`: {detail} {note}"
        assert not fake.of(INFO, "get_status")

    @respx.mock
    async def test_folder_name_unreadable_keeps_the_status(self) -> None:
        """The status is in hand; only the target folder's name lookup failed."""

        def boom(_p: dict[str, Any]) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        fake = FakePhotos().on(INFO, "get_status", 1, captured("bg-copy-status"))
        fake.on(FOLDER, "get", 2, boom)
        install(fake)
        client = make_client()
        async with client:
            out = await photo_task(client, handle="photos:5")
        assert out.startswith("photos:5 copy to folder 14: processing, 0 of 1 done")

    @respx.mock
    async def test_list_stops_folder_lookups_after_no_answer(self) -> None:
        """On a stalled NAS, listing N tasks waited out N folder lookups in turn."""
        listed = captured("bg-list-user-task")
        first = listed["list"][0]
        listed["list"] = [first, {**first, "id": int(first["id"]) + 1}]

        def boom(_p: dict[str, Any]) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        fake = FakePhotos().on(INFO, "list_user_task", 1, listed).on(FOLDER, "get", 2, boom)
        install(fake)
        client = make_client()
        async with client:
            table = await photo_task(client)
        assert len(fake.of(FOLDER, "get")) == 1
        folder_id = first["target_folder"]["id"]
        assert table.count(f"folder {folder_id}") == 2

    @respx.mock
    async def test_failed_items_unreadable(self) -> None:
        def boom(_p: dict[str, Any]) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        fake = FakePhotos().on(INFO, "get_status", 1, _done_with_error())
        fake.on(FOLDER, "get", 2, _DEST).on(INFO, "get_error_detail", 1, boom)
        install(fake)
        client = make_client()
        async with client:
            out = await photo_task(client, handle="photos:5")
        assert out.endswith("  (couldn't read the failed items: DSM didn't answer, ReadTimeout)")

    @respx.mock
    async def test_clear_without_answer_is_structured(self) -> None:
        def boom(_p: dict[str, Any]) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        install(FakePhotos().on(INFO, "list_user_task", 1, boom))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_task(client, action="clear")
        err = _error(exc)
        assert (err["code"], err["retryable"]) == ("timeout", True)
        assert err["message"] == "Photo task failed: DSM didn't answer (ReadTimeout: slow)."

    @respx.mock
    async def test_stop_request_fails_may_have_taken_effect(self) -> None:
        fake = FakePhotos().on(INFO, "abort_task", 1, lambda _p: httpx.Response(502, text="x"))
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_task(client, handle="photos:5", action="stop")
        assert _error(exc)["message"] == (
            f"Photo task failed for `photos:5`: {INFO}/abort_task: DSM answered HTTP 502. "
            "The stop may or may not have taken effect."
        )
        assert not fake.of(INFO, "get_status")

    @respx.mock
    async def test_list_without_answer_is_structured(self) -> None:
        def boom(_p: dict[str, Any]) -> httpx.Response:
            raise httpx.ConnectError("refused")

        install(FakePhotos().on(INFO, "list_user_task", 1, boom))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_task(client)
        err = _error(exc)
        assert (err["code"], err["retryable"]) == ("unavailable", True)
        assert (
            err["message"] == "Photo task failed: DSM couldn't be reached (ConnectError: refused)."
        )

    async def test_stop_needs_handle(self) -> None:
        with pytest.raises(ToolError) as exc:
            await photo_task(make_client(), action="stop")
        assert _error(exc)["param"] == "handle"


def _done_with_error() -> dict[str, Any]:
    done: dict[str, Any] = captured("bg-copy-status_last")
    done["list"][0]["error"] = 1
    return done


class TestCoverage:
    """Branches the capture-based tests above don't reach (Codecov, as #272)."""

    @respx.mock
    async def test_task_gone_from_the_list_counts_as_done(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(12)).on(FOLDER, "get", 2, _DEST)
        fake.on(FILE, "copy", 1, captured("bg-copy")).on(INFO, "get_status", 1, {"list": []})
        install(fake)
        client = make_client()
        async with client:
            out = await copy_or_move(
                client, operation="copy", ids=[12], folders=None, destination="14"
            )
        assert out.startswith("Copied 1 item(s) to /mcp-spec-capture/dest")

    @respx.mock
    async def test_error_detail_unreadable(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(12)).on(FOLDER, "get", 2, _DEST)
        fake.on(FILE, "copy", 1, captured("bg-copy")).on(INFO, "get_status", 1, _done_with_error())
        fake.error(INFO, "get_error_detail", 1, 117)
        install(fake)
        client = make_client()
        async with client:
            out = await copy_or_move(
                client, operation="copy", ids=[12], folders=None, destination="14"
            )
        assert "(couldn't read the failed items:" in out

    @respx.mock
    async def test_copy_folders(self) -> None:
        fake = FakePhotos().on(FOLDER, "get", 2, _DEST)
        fake.on(FILE, "copy", 1, captured("bg-copy"))
        fake.on(INFO, "get_status", 1, captured("bg-copy-status_last"))
        install(fake)
        client = make_client()
        async with client:
            out = await copy_or_move(
                client,
                operation="copy",
                ids=None,
                folders=["/mcp-spec-capture/dest"],
                destination="14",
            )
        assert fake.one(FILE, "copy").params["folder_id"] == [14]
        assert out.startswith("Copied 1 folder(s) to ")

    @respx.mock
    async def test_nothing_to_do(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, {"list": []}).on(FOLDER, "get", 2, _DEST)
        install(fake)
        client = make_client()
        async with client:
            out = await copy_or_move(
                client, operation="move", ids=[99], folders=None, destination="14"
            )
        assert out.startswith("Nothing to do: none of the ids exist.")
        assert not fake.of(FILE)

    @respx.mock
    async def test_delete_handoff_and_unknown_recycle_setting(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(25))
        fake.on(FILE, "delete", 1, captured("fav191_bg-delete"))
        install(fake)
        client = make_client(CAPTURE_191)
        async with client:
            out = await delete_photos(client, ids=[25], timeout=0)
        assert out.startswith("Delete of 1 item(s): Still deleting on the NAS")

    @respx.mock
    async def test_delete_with_errors_and_no_recycle_setting(self) -> None:
        status = captured("fav191_bg-delete-status")
        status["list"][0]["error"] = 1
        fake = FakePhotos().on(ITEM, "get", 2, _items(25))
        fake.on(FILE, "delete", 1, captured("fav191_bg-delete")).on(INFO, "get_status", 1, status)
        fake.on(INFO, "get_error_detail", 1, {"list": [{"id": 25, "reason": "busy"}]})
        fake.error("SYNO.Foto.Setting.Admin", "get", 1, 105)
        install(fake)
        client = make_client()
        async with client:
            out = await delete_photos(client, ids=[25])
        lines = out.splitlines()
        assert lines[0].startswith("Delete of 1 item(s) finished with errors:")
        assert "busy" in out

    @pytest.mark.parametrize(
        ("route", "call"),
        [
            ((ITEM, "get", 2), lambda c: copy_or_move(
                c, operation="copy", ids=[1], folders=None, destination="14")),
            ((FOLDER, "get", 2), lambda c: delete_photo_folders(c, folders=["/x"])),
            ((INFO, "abort_task", 1), lambda c: photo_task(c, handle="photos:5", action="stop")),
        ],
    )  # fmt: skip
    @respx.mock
    async def test_dsm_error(self, route: tuple[str, str, int], call: Any) -> None:
        install(FakePhotos().error(*route, 160))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await call(client)
        assert "160" in _error(exc)["message"]

    @respx.mock
    async def test_status_of_unknown_task(self) -> None:
        install(FakePhotos().on(INFO, "get_status", 1, {"list": []}))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_task(client, handle="photos:5")
        assert _error(exc)["code"] == "not_found"

    @respx.mock
    async def test_status_done_with_errors(self) -> None:
        fake = FakePhotos().on(INFO, "get_status", 1, _done_with_error())
        fake.on(FOLDER, "get", 2, _DEST)
        fake.on(INFO, "get_error_detail", 1, {"list": [{"id": 12, "reason": "not_existed"}]})
        install(fake)
        client = make_client()
        async with client:
            out = await photo_task(client, handle="photos:5")
        assert "not_existed" in out

    @respx.mock
    async def test_status_without_target_or_start_time(self) -> None:
        running = {"id": 5, "operation": "delete", "status": "processing", "total": 2,
                   "completion": 1}  # fmt: skip
        install(FakePhotos().on(INFO, "get_status", 1, {"list": [running]}))
        client = make_client()
        async with client:
            out = await photo_task(client, handle="photos:5")
        assert out.startswith("photos:5 delete: processing, 1 of 2 done")
        assert "running" not in out
