"""photo_folder, tag_photos, edit_photo, edit_person, merge_people, photo_request (#258)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.photos.organize import (
    edit_person,
    edit_photo,
    merge_people,
    person_line,
    photo_folder,
    photo_request,
    tag_photos,
)
from tests.modules.photos.dsm import CAPTURE_191, PEOPLE, FakePhotos, captured, install, make_client

ITEM = "SYNO.Foto.Browse.Item"
FOLDER = "SYNO.Foto.Browse.Folder"
TAG = "SYNO.Foto.Browse.GeneralTag"
PERSON = "SYNO.Foto.Browse.Person"
REQ = "SYNO.Foto.PhotoRequest"


def _error(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    text = str(exc.value)
    return json.loads(text[text.index("{") :])["error"]  # type: ignore[no-any-return]


def _deny(_p: dict[str, Any]) -> dict[str, Any]:
    return {"success": False, "error": {"code": 105}}


def _raise(exc: Exception) -> Any:
    def reply(_p: dict[str, Any]) -> dict[str, Any]:
        raise exc

    return reply


def _items(*ids: int) -> dict[str, Any]:
    return {"list": [{"id": i, "filename": f"f{i}.jpg", "folder_id": 10} for i in ids]}


class TestFolder:
    @respx.mock
    async def test_create_in_parent(self) -> None:
        fake = FakePhotos().on(FOLDER, "get", 2, captured("folder-get-by-name"))
        fake.on(FOLDER, "create", 1, captured("folder-create-duplicate-name"))
        install(fake)
        client = make_client()
        async with client:
            out = await photo_folder(
                client, action="create", parent_path="/mcp-spec-capture", name="2024"
            )
        assert fake.one(FOLDER, "create").params == {"target_id": 10, "name": "2024"}
        assert out == "Created /mcp-spec-capture/mcp-spec-capture (id 11, personal space)."

    @respx.mock
    @pytest.mark.parametrize("action", ["create", "rename"])
    async def test_a_folder_holding_the_space_marker_is_quoted(self, action: str) -> None:
        """#700: "Created <folder> (id N, space)" read a folder named "/a (id 9" as another."""
        made = captured("folder-create-duplicate-name")
        made["folder"]["name"] = "/a (id 9"
        fake = FakePhotos().on(
            FOLDER, "get", 2, {"folder": {"id": 13, "name": "/a/sub", "parent": 10}}
        )
        fake.on(FOLDER, "create", 1, made).on(FOLDER, "rename", 1, made)
        install(fake)
        client = make_client()
        async with client:
            if action == "create":
                out = await photo_folder(client, action="create", parent_id=13, name="x")
            else:
                out = await photo_folder(client, action="rename", folder_id=13, name="x")
        assert "'/a (id 9'" in out, out

    @respx.mock
    async def test_rename(self) -> None:
        fake = FakePhotos().on(
            FOLDER, "get", 2, {"folder": {"id": 13, "name": "/a/sub", "parent": 10}}
        )
        fake.on(FOLDER, "rename", 1, captured("folder-rename"))
        install(fake)
        client = make_client()
        async with client:
            out = await photo_folder(client, action="rename", folder_id=13, name="sub-renamed")
        assert out == "Renamed folder 13 to /mcp-spec-capture/sub-renamed."

    @respx.mock
    async def test_existing_name_is_already_exists(self) -> None:
        """DSM refuses a name taken in the parent with 641 (verified on both versions)."""
        fake = FakePhotos().on(FOLDER, "get", 2, captured("folder-get-by-name"))
        fake.error(FOLDER, "create", 1, 641)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_folder(client, action="create", parent_path="/x", name="dup")
        assert _error(exc)["code"] == "already_exists"

    @respx.mock
    async def test_root_rename_refused(self) -> None:
        fake = FakePhotos().on(FOLDER, "get", 2, captured("folder-get-root"))
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError):
                await photo_folder(client, action="rename", folder_path="/", name="x")
        assert not fake.of(FOLDER, "rename")

    @respx.mock
    async def test_set_cover_unknown_item(self) -> None:
        fake = FakePhotos().on(FOLDER, "get", 2, captured("folder-get-by-name"))
        fake.on(ITEM, "get", 2, {"list": []})
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_folder(client, action="set_cover", folder_id=10, item_id=99)
        assert _error(exc)["code"] == "not_found"
        assert not fake.of(FOLDER, "set_cover")

    @respx.mock
    async def test_set_cover_list_id(self) -> None:
        fake = FakePhotos().on(FOLDER, "get", 2, captured("folder-get-by-name"))
        fake.on(ITEM, "get", 2, _items(29)).on(FOLDER, "set_cover", 2, {})
        install(fake)
        client = make_client()
        async with client:
            await photo_folder(client, action="set_cover", folder_id=10, item_id=29)
        assert fake.one(FOLDER, "set_cover").params == {"id": 10, "id_item": [29]}

    @pytest.mark.parametrize(
        ("kwargs", "param"),
        [
            ({"action": "move"}, "action"),
            ({"action": "create"}, "name"),
            ({"action": "create", "name": "a/b"}, "name"),
            ({"action": "rename", "name": "x"}, "folder_id"),
            # #615: required, the root too, so a lost parent can't create in the root.
            ({"action": "create", "name": "x"}, "parent_id"),
            ({"action": "set_cover", "folder_id": 1}, "item_id"),
        ],
    )
    async def test_refusals(self, kwargs: dict[str, Any], param: str) -> None:
        with pytest.raises(ToolError) as exc:
            await photo_folder(make_client(), **kwargs)
        assert _error(exc)["param"] == param

    @pytest.mark.parametrize("action", ["create", "rename"])
    @pytest.mark.parametrize(
        ("name", "problem"),
        [
            # File Station's new-name rules (#510). DSM keeps each of these as given (vdsm,
            # DSM 7.1, 7.2.2, 7.3.2 and 7.4.1); a leading space was stripped before.
            (" x", "starts or ends with whitespace"),
            ("x ", "starts or ends with whitespace"),
            ("   ", "is blank"),
            ("a\x1bb", "control character"),
            ("a\u202eb", "changes text direction"),
            ("a\u200bb", "invisible character (U+200B)"),
            ("a\x00b", "NUL"),
            ("..", "'.' or '..'"),
            ("a/b", "contains '/'"),
        ],
    )
    @respx.mock
    async def test_a_bad_new_name_is_refused_before_any_request(
        self, action: str, name: str, problem: str
    ) -> None:
        fake = FakePhotos()  # no routes: a request would fail the test
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_folder(client, action=action, folder_id=13, parent_path="/x", name=name)
        assert not fake.calls
        err = _error(exc)
        assert (err["code"], err["param"], err["value"]) == ("invalid_parameter", "name", name)
        assert problem in err["message"]

    @respx.mock
    async def test_a_tab_inside_a_name_is_kept(self) -> None:
        # As in File Station: DSM keeps a tab inside a name (maintainer, 2026-10-31).
        fake = FakePhotos().on(FOLDER, "get", 2, captured("folder-get-by-name"))
        fake.on(FOLDER, "create", 1, captured("folder-create-duplicate-name"))
        install(fake)
        client = make_client()
        async with client:
            await photo_folder(client, action="create", parent_path="/x", name="a\tb")
        assert fake.one(FOLDER, "create").params["name"] == "a\tb"


class TestTags:
    @respx.mock
    async def test_add_creates_missing_and_sends_one_call(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(12, 15))
        fake.on(TAG, "list", 1, captured("tag-list"))
        fake.on(TAG, "create", 1, {"tag": {"id": 9, "name": "new"}})
        fake.on(ITEM, "add_tag", 1, {"error_list": [15]})
        install(fake)
        client = make_client()
        async with client:
            out = await tag_photos(client, action="add", ids=[12, 15, 999], tags=["2024", "new"])
        assert fake.one(TAG, "create").params == {"name": "new"}
        assert fake.one(ITEM, "add_tag").params == {"id": [12, 15], "tag": [4, 9]}
        assert 'Added tags "2024", "new" to 2 item(s)' in out
        assert "Rejected by DSM: 15" in out
        assert "Not found: 999" in out
        assert "dates and sizes change" in out

    @respx.mock
    async def test_remove_unknown_tag(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(12))
        fake.on(TAG, "list", 1, captured("tag-list")).on(ITEM, "remove_tag", 1, {"error_list": []})
        install(fake)
        client = make_client()
        async with client:
            out = await tag_photos(client, action="remove", ids=[12], tags=["2024", "gone, beach"])
        assert fake.one(ITEM, "remove_tag").params == {"id": [12], "tag": [4]}
        assert 'No item has the tag(s) "gone, beach", so' in out, out  # #700
        assert not fake.of(TAG, "create")

    @respx.mock
    async def test_batches_of_100(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(*range(1, 101)), _items(*range(101, 151)))
        fake.on(TAG, "list", 1, captured("tag-list")).on(ITEM, "add_tag", 1, {"error_list": []})
        install(fake)
        client = make_client()
        async with client:
            await tag_photos(client, action="add", ids=list(range(1, 151)), tags=["2024"])
        assert [len(c.params["id"]) for c in fake.of(ITEM, "add_tag")] == [100, 50]

    @respx.mock
    async def test_rejected_ids_are_sorted(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(*range(1, 101)), _items(*range(101, 151)))
        fake.on(TAG, "list", 1, captured("tag-list"))
        fake.on(ITEM, "add_tag", 1, {"error_list": [100, 9]}, {"error_list": [120]})
        install(fake)
        client = make_client()
        async with client:
            out = await tag_photos(client, action="add", ids=list(range(1, 151)), tags=["2024"])
        assert "  Rejected by DSM: 9, 100, 120 (error_list; e.g. still being indexed)" in out

    @respx.mock
    async def test_failed_batch_names_what_was_tagged(self) -> None:
        """#322: as edit_photo, a later batch failing says which items were tagged."""
        fake = FakePhotos().on(ITEM, "get", 2, _items(*range(1, 101)), _items(*range(101, 151)))
        fake.on(TAG, "list", 1, captured("tag-list"))
        fake.on(ITEM, "add_tag", 1, {"error_list": []}, _deny)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await tag_photos(client, action="add", ids=list(range(1, 151)), tags=["2024"])
        assert (
            "Already changed 100 of 150 item(s): 1-100. Not changed: 101-150."
            in (_error(exc)["message"])
        )

    @respx.mock
    async def test_shared_space_missing_note(self) -> None:
        """An entry-only Shared Space user's get leaves the item out; nothing is sent."""
        fake = FakePhotos().on("SYNO.FotoTeam.Browse.Item", "get", 2, {"list": []})
        install(fake)
        client = make_client()
        async with client:
            out = await tag_photos(client, action="add", ids=[5], tags=["x"], space="shared")
        assert "or no permission for its Shared Space folder: 5" in out
        assert len(fake.calls) == 1

    @pytest.mark.parametrize(
        ("kwargs", "param"),
        [
            ({"action": "set", "ids": [1], "tags": ["a"]}, "action"),
            ({"action": "add", "ids": [], "tags": ["a"]}, "ids"),
            ({"action": "add", "ids": [1], "tags": [" "]}, "tags"),
        ],
    )
    async def test_refusals(self, kwargs: dict[str, Any], param: str) -> None:
        with pytest.raises(ToolError) as exc:
            await tag_photos(make_client(), **kwargs)
        assert _error(exc)["param"] == param


class TestEditPhoto:
    @respx.mock
    async def test_rejected_by_dsm(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(15)).on(ITEM, "set", 2, {"error_list": [15]})
        install(fake)
        client = make_client()
        async with client:
            out = await edit_photo(client, ids=[15], rating=3)
        assert "  Rejected by DSM: 15 (error_list)" in out

    @respx.mock
    async def test_none_of_the_ids_exist(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, {"list": []})
        install(fake)
        client = make_client()
        async with client:
            out = await edit_photo(client, ids=[99], rating=3)
        assert out.startswith("No items changed: none of the ids exist.")
        assert not fake.of(ITEM, "set")

    @respx.mock
    async def test_one_set_per_field(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(15)).on(ITEM, "set", 2, {"error_list": []})
        install(fake)
        client = make_client()
        async with client:
            out = await edit_photo(
                client,
                ids=[15, 9],
                rating=4,
                description="",
                taken_time="2020-09-13T12:26:40Z",
                rotate="right",
            )
        # One set with every field: each file is rewritten once.
        assert fake.one(ITEM, "set").params == {
            "id": [15],
            "rating": 4,
            "description": "",
            "time": 1600000000,
            "rotate_action": "clockwise",
        }
        assert "rating 4, description, taken time, rotated right." in out
        assert "wait before changing it again" in out
        assert "Not found: 9" in out

    @respx.mock
    async def test_shift_hours_and_taken_time_is_wall_clock(self) -> None:
        """Photos stores wall-clock time encoded as if UTC, so an offset is dropped."""
        fake = FakePhotos().on(ITEM, "get", 2, _items(15)).on(ITEM, "set", 2, {"error_list": []})
        install(fake)
        client = make_client()
        async with client:
            await edit_photo(client, ids=[15], shift_hours=-1.5)
            await edit_photo(client, ids=[15], taken_time="2020-09-13T12:26:40")
            await edit_photo(client, ids=[15], taken_time="2020-09-13T12:26:40+02:00")
        assert [c.params for c in fake.of(ITEM, "set")] == [
            {"id": [15], "shift_time": -5400},
            {"id": [15], "time": 1600000000},
            {"id": [15], "time": 1600000000},
        ]

    @respx.mock
    async def test_favorite_on_191(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(38)).on(ITEM, "set_favorite", 7, {})
        install(fake)
        client = make_client(CAPTURE_191)
        async with client:
            out = await edit_photo(client, ids=[38], favorite=False)
        assert fake.one(ITEM, "set_favorite").params == {"id": [38], "favorite": False}
        assert "not favorite" in out

    @staticmethod
    def _150(fake: FakePhotos) -> FakePhotos:
        return fake.on(ITEM, "get", 2, _items(*range(1, 101)), _items(*range(101, 151)))

    @respx.mock
    @pytest.mark.parametrize(
        ("kwargs", "retry_note"),
        [({"shift_hours": 2}, True), ({"rotate": "left"}, True), ({"rating": 3}, False)],
    )
    async def test_failed_batch_names_what_changed(
        self, kwargs: dict[str, Any], retry_note: bool
    ) -> None:
        """#322: a later batch failing used to report only the error, so a retry shifted
        or rotated the first 100 items again."""
        fake = self._150(FakePhotos()).on(ITEM, "set", 2, {"error_list": []}, _deny)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await edit_photo(client, ids=list(range(1, 151)), **kwargs)
        assert [len(c.params["id"]) for c in fake.of(ITEM, "set")] == [100, 50]
        err = _error(exc)
        assert err["code"] == "permission_denied"
        assert (
            "Already changed 100 of 150 item(s): 1-100. Not changed: 101-150." in (err["message"])
        )
        assert ("Retry with only the ids not changed" in err["message"]) is retry_note

    @respx.mock
    async def test_first_batch_failing_adds_nothing(self) -> None:
        fake = self._150(FakePhotos()).error(ITEM, "set", 2, 105)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await edit_photo(client, ids=list(range(1, 151)), shift_hours=2)
        assert "changed" not in _error(exc)["message"].lower()

    @respx.mock
    @pytest.mark.parametrize(
        ("reply", "code", "cause"),
        [
            (_raise(httpx.ReadTimeout("")), "timeout", "no answer from DSM (ReadTimeout)"),
            (_raise(httpx.RemoteProtocolError("")), "unavailable",
             "no answer from DSM (RemoteProtocolError)"),
            (lambda _p: httpx.Response(502, text="<html>Bad Gateway</html>"), "unavailable",
             "SYNO.Foto.Browse.Item/set: DSM answered HTTP 502"),
            (lambda _p: httpx.Response(200, text="<html>Sign in</html>",
                                       headers={"content-type": "text/html"}),
             "unavailable", "SYNO.Foto.Browse.Item/set"),
        ],
    )  # fmt: skip
    async def test_batch_without_a_dsm_answer_may_have_changed(
        self, reply: Any, code: str, cause: str
    ) -> None:
        """No answer, or a proxy's error status or page: the write may have reached DSM,
        so the batch is named as "may or may not have changed", never retryable."""
        fake = self._150(FakePhotos()).on(ITEM, "set", 2, {"error_list": []}, reply)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await edit_photo(client, ids=list(range(1, 151)), shift_hours=2)
        err = _error(exc)
        assert err["code"] == code
        assert err["retryable"] is False
        assert cause in err["message"]
        assert (
            "Already changed 100 of 150 item(s): 1-100. May or may not have changed: 101-150. "
            "Retry with only"
        ) in err["message"]

    @respx.mock
    async def test_first_batch_without_an_answer_is_named(self) -> None:
        fake = self._150(FakePhotos()).on(ITEM, "set", 2, _raise(httpx.ReadTimeout("")))
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await edit_photo(client, ids=list(range(1, 151)), rotate="left")
        assert (
            "no answer from DSM (ReadTimeout). May or may not have changed: 1-100. "
            "Not changed: 101-150. Retry with only"
        ) in _error(exc)["message"]

    @respx.mock
    async def test_connection_never_opened_is_not_changed(self) -> None:
        fake = self._150(FakePhotos()).on(
            ITEM, "set", 2, {"error_list": []}, _raise(httpx.ConnectError(""))
        )
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await edit_photo(client, ids=list(range(1, 151)), shift_hours=2)
        message = _error(exc)["message"]
        assert "Already changed 100 of 150 item(s): 1-100. Not changed: 101-150." in message
        assert "May or may not" not in message

    @respx.mock
    async def test_favorite_dsm_error_names_the_step_and_rejections(self) -> None:
        """The favorite step's "not changed" can't read as the rating's, and IDs the set
        rejected aren't claimed as changed."""
        fake = self._150(FakePhotos()).on(ITEM, "set", 2, {"error_list": [9, 3]})
        fake.error(ITEM, "set_favorite", 7, 105)
        install(fake)
        client = make_client(CAPTURE_191)
        async with client:
            with pytest.raises(ToolError) as exc:
                await edit_photo(client, ids=list(range(1, 151)), rating=3, favorite=True)
        assert (
            "The other changes (rating 3) were already made to all 150 item(s) except those "
            "DSM rejected (3, 9). Favorite not changed: 1-150."
        ) in _error(exc)["message"]

    @respx.mock
    async def test_proxy_4xx_is_not_changed(self) -> None:
        """A 4xx is the proxy (or DSM) refusing the request, as for uploads."""
        fake = self._150(FakePhotos()).on(
            ITEM, "set", 2, {"error_list": []}, lambda _p: httpx.Response(403, text="no")
        )
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await edit_photo(client, ids=list(range(1, 151)), shift_hours=2)
        message = _error(exc)["message"]
        assert "Already changed 100 of 150 item(s): 1-100. Not changed: 101-150." in message
        assert "May or may not" not in message

    @respx.mock
    async def test_earlier_rejections_are_named(self) -> None:
        fake = self._150(FakePhotos()).on(ITEM, "set", 2, {"error_list": [10, 9, 100]}, _deny)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await edit_photo(client, ids=list(range(1, 151)), rating=2)
        assert (
            "Already changed 97 of 150 item(s): 1-8, 11-99. Rejected by DSM, so not changed: "
            "9-10, 100. "
            "Not changed: 101-150."
        ) in _error(exc)["message"]

    @respx.mock
    async def test_odd_rejection_entries_on_success(self) -> None:
        """Every batch succeeding with odd error_list entries (a dict, a string, a repeat)
        can't crash the reply; integer IDs are sorted and runs collapsed."""
        fake = self._150(FakePhotos()).on(
            ITEM,
            "set",
            2,
            {"error_list": [{"id": 5}, 4, 3, True]},
            {"error_list": ["7", 3, {"id": 5}, 7]},
        )
        install(fake)
        client = make_client()
        async with client:
            out = await edit_photo(client, ids=list(range(1, 151)), rating=2)
        # Each once; "7" is ID 7; True isn't an ID, so it doesn't join a run.
        assert "  Rejected by DSM: 3-4, 7, True, {'id': 5} (error_list)" in out

    @respx.mock
    async def test_repeated_rejections_on_the_failure_path_are_listed_once(self) -> None:
        reply = {"error_list": ["7", {"id": 5}]}
        fake = FakePhotos().on(
            ITEM, "get", 2, _items(*range(1, 101)), _items(*range(101, 201)), _items(201)
        )
        fake.on(ITEM, "set", 2, reply, {"error_list": [7, {"id": 5}]}, _deny)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await edit_photo(client, ids=list(range(1, 202)), rating=2)
        assert (
            "Already changed 199 of 201 item(s): 1-6, 8-200. Rejected by DSM, so not changed: "
            "7, {'id': 5}. Not changed: 201."
        ) in _error(exc)["message"]

    @respx.mock
    async def test_odd_rejection_entries_and_all_rejected(self) -> None:
        """A string ID matches its integer; an entry that isn't an ID can't crash the reply;
        a batch DSM rejected whole changed nothing, so there's no "would shift again"."""
        rejected = [str(i) for i in range(1, 100)] + [{"id": 100}]
        fake = self._150(FakePhotos()).on(ITEM, "set", 2, {"error_list": rejected}, _deny)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await edit_photo(client, ids=list(range(1, 151)), shift_hours=2)
        message = _error(exc)["message"]
        assert "Already changed 1 of 150 item(s): 100." in message  # the dict matched nothing
        assert "Rejected by DSM, so not changed: 1-99, {'id': 100}." in message
        assert "Not changed: 101-150." in message

        fake = self._150(FakePhotos()).on(
            ITEM, "set", 2, {"error_list": list(range(1, 101))}, _deny
        )
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await edit_photo(client, ids=list(range(1, 151)), shift_hours=2)
        message = _error(exc)["message"]
        assert message.endswith(
            "Permission denied. Rejected by DSM, so not changed: 1-100. Not changed: 101-150."
        )

    @respx.mock
    async def test_ids_are_listed_in_order(self) -> None:
        fake = FakePhotos().on(
            ITEM, "get", 2, _items(*range(150, 50, -1)), _items(*range(50, 0, -1))
        )
        fake.on(ITEM, "set", 2, {"error_list": []}, _deny)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await edit_photo(client, ids=list(range(1, 151)), rating=2)
        assert (
            "Already changed 100 of 150 item(s): 51-150. Not changed: 1-50."
            in (_error(exc)["message"])
        )

    @respx.mock
    async def test_favorite_first_batch_without_an_answer(self) -> None:
        fake = self._150(FakePhotos()).on(ITEM, "set", 2, {"error_list": []})
        fake.on(ITEM, "set_favorite", 7, _raise(httpx.ReadTimeout("")))
        install(fake)
        client = make_client(CAPTURE_191)
        async with client:
            with pytest.raises(ToolError) as exc:
                await edit_photo(client, ids=list(range(1, 151)), rating=3, favorite=True)
        assert (
            "The other changes (rating 3) were already made to all 150 item(s). "
            "Favorite may or may not have changed: 1-100. Favorite not changed: 101-150."
        ) in _error(exc)["message"]

    @respx.mock
    async def test_favorite_failing_says_the_other_changes_were_made(self) -> None:
        fake = self._150(FakePhotos()).on(ITEM, "set", 2, {"error_list": []})
        fake.error(ITEM, "set_favorite", 7, 105)
        install(fake)
        client = make_client(CAPTURE_191)
        async with client:
            with pytest.raises(ToolError) as exc:
                await edit_photo(client, ids=list(range(1, 151)), rating=3, favorite=True)
        assert (
            "The other changes (rating 3) were already made to all 150 item(s)."
            in (_error(exc)["message"])
        )

    async def test_favorite_refused_on_134(self) -> None:
        with pytest.raises(ToolError) as exc:
            await edit_photo(make_client(), ids=[1], favorite=True)
        assert _error(exc)["code"] == "not_supported"

    @pytest.mark.parametrize(
        ("kwargs", "param"),
        [
            ({"rating": 6}, "rating"),
            ({"taken_time": "2020-01-01", "shift_hours": 1}, "shift_hours"),
            ({"rotate": "flip"}, "rotate"),
            ({"shift_hours": float("inf")}, "shift_hours"),
            ({"shift_hours": float("nan")}, "shift_hours"),
            ({"taken_time": "yesterday"}, "taken_time"),
            ({}, "ids"),
        ],
    )
    async def test_refusals(self, kwargs: dict[str, Any], param: str) -> None:
        with pytest.raises(ToolError) as exc:
            await edit_photo(make_client(), ids=[1], **kwargs)
        assert _error(exc)["param"] == param


class TestPeople:
    def _fake(self) -> FakePhotos:
        return FakePhotos().on(PERSON, "get", 1, captured("person-get", PEOPLE))

    @respx.mock
    async def test_rename(self) -> None:
        fake = self._fake().on(PERSON, "set", 1, captured("person-set-name", PEOPLE))
        install(fake)
        client = make_client()
        async with client:
            out = await edit_person(client, action="rename", person_id=12, name="")
        assert fake.one(PERSON, "set").params == {"id": 12, "name": ""}
        assert out == 'Person 12: "Person 1" (70 items, visible, cover: item 26).'

    @respx.mock
    async def test_hide_and_cover(self) -> None:
        fake = self._fake().on(PERSON, "show", 1, {}).on(PERSON, "set_cover", 1, {})
        install(fake)
        client = make_client()
        async with client:
            await edit_person(client, action="hide", person_id=12)
            await edit_person(client, action="set_cover", person_id=12, item_id=2121)
        assert fake.one(PERSON, "show").params == {"id": [12], "show": False}
        assert fake.one(PERSON, "set_cover").params == {"id": 12, "photo_id": 2121}

    @respx.mock
    async def test_merge_checks_every_person_first(self) -> None:
        people = captured("person-list", PEOPLE)
        fake = FakePhotos().on(PERSON, "get", 1, {"list": []}, {"list": []})
        fake.on(PERSON, "list", 1, people)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await merge_people(
                    client, target_name="Person 1", merge_names=["Person 2", "Nobody"]
                )
        assert _error(exc)["code"] == "not_found"
        assert not fake.of(PERSON, "merge")

    @respx.mock
    async def test_merge(self) -> None:
        fake = FakePhotos().on(PERSON, "list", 1, captured("person-list", PEOPLE))
        fake.on(PERSON, "merge", 2, captured("person-merge-missing", PEOPLE))
        fake.on(PERSON, "get", 1, captured("person-get", PEOPLE))
        install(fake)
        client = make_client()
        async with client:
            out = await merge_people(
                client, target_name="person 1", merge_names=["Person 2", "Person 3"]
            )
        assert fake.one(PERSON, "merge").params == {
            "target_id": 12,
            "merged_id": [11, 19],
            "name": "Person 1",
        }
        assert out == 'Merged 2 people into person 12 "Person 1" (now 70 items).'

    @respx.mock
    async def test_merge_quotes_the_kept_person(self) -> None:
        """#700: the kept person's name was in plain quotes, so `Jo" (now 1` closed it early."""
        kept = captured("person-get", PEOPLE)
        kept["list"][0]["name"] = 'Jo" (now 1'
        fake = FakePhotos().on(PERSON, "list", 1, captured("person-list", PEOPLE))
        fake.on(PERSON, "merge", 2, captured("person-merge-missing", PEOPLE))
        fake.on(PERSON, "get", 1, kept)
        install(fake)
        client = make_client()
        async with client:
            out = await merge_people(
                client, target_name="person 1", merge_names=["Person 2", "Person 3"]
            )
        assert out == 'Merged 2 people into person 12 "Jo\\" (now 1" (now 70 items).', out

    @respx.mock
    async def test_a_numeric_id_never_merges_a_person_by_name(self) -> None:
        """#609 pre-QA, #614: merge_ids=[43], with no person 43 but one named "43", merges
        nothing: it can't be undone."""
        people = {"list": [{"id": 12, "name": "Keep"}, {"id": 99, "name": "43"}]}
        keep = {"list": [{"id": 12, "name": "Keep"}]}
        fake = FakePhotos().on(PERSON, "get", 1, keep, {"list": []})
        fake.on(PERSON, "list", 1, people)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await merge_people(client, target_id=12, merge_ids=[43])
        assert _error(exc)["code"] == "not_found"
        assert "no person 43" in _error(exc)["message"]
        # Not a hint to resend the digits as text: that would match the person named "43".
        assert "as text" not in _error(exc)["suggestion"], _error(exc)
        assert not fake.of(PERSON, "merge")

    @respx.mock
    async def test_merge_into_itself_refused(self) -> None:
        install(self._fake())
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await merge_people(client, target_id=12, merge_ids=[12])
        assert "into itself" in _error(exc)["message"]

    @pytest.mark.parametrize(
        ("kwargs", "param"),
        [
            ({"action": "delete", "person_id": 1}, "action"),
            ({"action": "rename", "person_id": 1}, "name"),
            ({"action": "set_cover", "person_id": 1}, "item_id"),
        ],
    )
    async def test_refusals(self, kwargs: dict[str, Any], param: str) -> None:
        with pytest.raises(ToolError) as exc:
            await edit_person(make_client(), **kwargs)
        assert _error(exc)["param"] == param


class TestRequests:
    @respx.mock
    async def test_a_crafted_subject_and_folder_are_quoted(self) -> None:
        """#700: the subject was in plain quotes, so `x" (abc)` forged the request's ID on
        delete, and the folder before " (" read a folder named "/a (Shared Space)" as it."""
        listed = captured("request-list")
        listed["list"][0].update(subject='x" (abc)', folder_home_path="/a (Shared Space)")
        fake = FakePhotos().on(REQ, "list", 1, listed, listed, listed)
        fake.on(REQ, "update", 1, {}).on(REQ, "delete", 1, {})
        install(fake)
        client = make_client()
        async with client:
            block = await photo_request(
                client, action="update", request="ZxOc9Ep8q", max_file_mib=10
            )
            gone = await photo_request(client, action="delete", request="ZxOc9Ep8q")
        assert block.startswith(
            'Photo request "x\\" (abc)" → \'/a (Shared Space)\' (personal space)'
        ), block
        assert gone.startswith('Deleted photo request "x\\" (abc)" (ZxOc9Ep8q);'), gone

    @respx.mock
    async def test_a_request_with_no_subject_shows_the_placeholder(self) -> None:
        """Pre-QA r2 on #700: no subject is the server's placeholder, unquoted."""
        listed = captured("request-list")
        listed["list"][0]["subject"] = ""
        fake = FakePhotos().on(REQ, "list", 1, listed, listed, listed)
        fake.on(REQ, "update", 1, {}).on(REQ, "delete", 1, {})
        install(fake)
        client = make_client()
        async with client:
            block = await photo_request(
                client, action="update", request="ZxOc9Ep8q", max_file_mib=10
            )
            gone = await photo_request(client, action="delete", request="ZxOc9Ep8q")
        assert block.startswith("Photo request (no subject) → /mcp-spec-capture"), block
        assert gone.startswith("Deleted photo request (no subject) (ZxOc9Ep8q);"), gone

    @respx.mock
    async def test_create(self) -> None:
        fake = FakePhotos().on(REQ, "list", 1, {"list": []})
        fake.on(FOLDER, "get", 2, captured("folder-get-by-name"))
        fake.on(REQ, "create", 1, captured("request-create"))
        install(fake)
        client = make_client()
        async with client:
            out = await photo_request(
                client,
                action="create",
                subject="mcp spec",
                folder_path="mcp-spec-capture/",
                description="capture",
                max_file_mib=10,
            )
        assert fake.one(REQ, "create").params == {
            "passphrase": "",
            "subject": "mcp spec",
            "description": "capture",
            "library": "personal_space",
            "folder_home_path": "/mcp-spec-capture",
            "filesize_limit": 10485760,
            "expiration": 0,
        }
        assert 'Photo request "mcp spec" → /mcp-spec-capture (personal space)' in out
        assert "Link:      http://nas.example:5000/mo/request/ZxOc9Ep8q" in out

    @respx.mock
    async def test_folder_taken_exactly(self) -> None:
        # #510: the folder checked and the folder sent are the same exact path.
        fake = FakePhotos().on(REQ, "list", 1, {"list": []})
        fake.on(FOLDER, "get", 2, {"folder": {"id": 5, "name": "/x/foo "}})
        fake.on(REQ, "create", 1, captured("request-create"))
        install(fake)
        client = make_client()
        async with client:
            await photo_request(client, action="create", subject="s", folder_path="  /x/foo ")
        assert fake.one(FOLDER, "get").params == {"name": "/x/foo "}
        assert fake.one(REQ, "create").params["folder_home_path"] == "/x/foo "

    @respx.mock
    async def test_folder_with_a_nul_is_refused(self) -> None:
        fake = FakePhotos().on(REQ, "list", 1, {"list": []})
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_request(client, action="create", subject="s", folder_path="/x\x00y")
        assert _error(exc)["param"] == "folder_path"
        # A path only: no ID advice (QA round 1 on #526).
        assert _error(exc)["suggestion"] == (
            "Pass the folder as a path starting with /, such as /Inbox."
        )
        assert not fake.of(REQ, "create") and not fake.of(FOLDER)

    @respx.mock
    async def test_missing_folder_advice_is_path_only(self) -> None:
        # The folder is a path only: no ID advice for a folder that isn't there either (#526).
        fake = FakePhotos().on(REQ, "list", 1, {"list": []})
        fake.error(FOLDER, "get", 2, 642)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_request(client, action="create", subject="s", folder_path="Inbx")
        err = _error(exc)
        assert (err["code"], err["param"]) == ("not_found", "folder_path")
        assert err["suggestion"] == (
            "Pass the folder as a path starting with / (list_photo_collections type=folder "
            "lists them)."
        )
        assert not fake.of(REQ, "create")

    @respx.mock
    async def test_shared_space_folder_checked_there(self) -> None:
        fake = FakePhotos().on(REQ, "list", 1, {"list": []})
        fake.on("SYNO.FotoTeam.Browse.Folder", "get", 2, {"folder": {"id": 5, "name": "/team"}})
        fake.on(REQ, "create", 1, {**captured("request-create"), "library": "shared_space"})
        install(fake)
        client = make_client()
        async with client:
            out = await photo_request(
                client, action="create", subject="s", folder_path="/team", space="shared"
            )
        assert fake.one(REQ, "create").params["library"] == "shared_space"
        assert "(Shared Space)" in out

    @respx.mock
    async def test_list_update_delete(self) -> None:
        listed = captured("request-list")
        updated = json.loads(json.dumps(listed))
        updated["list"][0].update(subject="mcp spec 2", filesize_limit=10485760)
        fake = FakePhotos().on(REQ, "list", 1, listed, listed, updated, listed)
        fake.on(REQ, "update", 1, {}).on(REQ, "delete", 1, {})
        install(fake)
        client = make_client()
        async with client:
            table = await photo_request(client, action="list")
            out = await photo_request(
                client, action="update", request="ZxOc9Ep8q", subject="mcp spec 2"
            )
            gone = await photo_request(client, action="delete", request="ZxOc9Ep8q")
        assert "ZxOc9Ep8q" in table and "no size limit" in table
        assert fake.one(REQ, "update").params == {
            "passphrase": "ZxOc9Ep8q",
            "subject": "mcp spec 2",
        }
        assert 'Photo request "mcp spec 2"' in out and "10 MiB per file" in out
        assert fake.one(REQ, "delete").params == {"passphrase": ["ZxOc9Ep8q"]}
        assert "its link stops working" in gone

    @respx.mock
    async def test_list_quotes_a_folder_holding_the_cells_space_marker(self) -> None:
        """QA r1 on #709: the Folder cell adds " (shared)" after the folder, so a personal
        request on a folder named "/Up (shared)" read the same as a Shared Space one on "/Up"."""
        listed = captured("request-list")
        mine, team = (
            json.loads(json.dumps(listed["list"][0])),
            json.loads(json.dumps(listed["list"][0])),
        )
        mine.update(passphrase="Mine1", folder_home_path="/Up (shared)", library="personal")
        team.update(passphrase="Team1", folder_home_path="/Up", library="shared_space")
        fake = FakePhotos().on(REQ, "list", 1, {**listed, "list": [mine, team]})
        install(fake)
        client = make_client()
        async with client:
            table = await photo_request(client, action="list")
        rows = {r.split()[0]: r for r in table.splitlines()[4:]}
        assert "  '/Up (shared)'  " in rows["Mine1"], table
        assert "  /Up (shared)  " in rows["Team1"], table

    @respx.mock
    @pytest.mark.parametrize(
        ("description", "note"), [("   ", None), ("  bring snacks ", "bring snacks")]
    )
    async def test_note_is_stripped_and_blank_is_left_out(
        self, description: str, note: str | None
    ) -> None:
        """#362: a whitespace-only description isn't shown as an empty Note line."""
        listed = captured("request-list")
        updated = json.loads(json.dumps(listed))
        updated["list"][0]["description"] = description
        fake = FakePhotos().on(REQ, "list", 1, listed, updated).on(REQ, "update", 1, {})
        install(fake)
        client = make_client()
        async with client:
            out = await photo_request(
                client, action="update", request="ZxOc9Ep8q", subject="mcp spec 2"
            )
        if note is None:
            assert "Note:" not in out
        else:
            assert f"  Note:      {note}\n" in out + "\n"

    @respx.mock
    async def test_update_expiry_and_missing_folder_warning(self) -> None:
        listed = captured("request-list")
        listed["list"][0]["is_folder_valid"] = False
        updated = json.loads(json.dumps(listed))
        updated["list"][0]["expiration"] = 1900022399
        fake = FakePhotos().on(REQ, "list", 1, listed, updated).on(REQ, "update", 1, {})
        install(fake)
        client = make_client()
        async with client:
            out = await photo_request(
                client, action="update", request="ZxOc9Ep8q", expires="2030-03-17"
            )
        assert fake.one(REQ, "update").params == {
            "passphrase": "ZxOc9Ep8q",
            "expiration": 1900022399,  # 2030-03-17 23:59:59 UTC
        }
        assert "expires 2030-03-17 23:59:59" in out
        assert "Warning:   its folder no longer exists, so uploads fail." in out

    @respx.mock
    async def test_update_expiry_with_offset(self) -> None:
        listed = captured("request-list")
        fake = FakePhotos().on(REQ, "list", 1, listed, listed).on(REQ, "update", 1, {})
        install(fake)
        client = make_client()
        async with client:
            await photo_request(
                client, action="update", request="ZxOc9Ep8q", expires="2030-03-17T23:59:59-04:00"
            )
        assert fake.one(REQ, "update").params == {
            "passphrase": "ZxOc9Ep8q",
            "expiration": 1900036799,  # 2030-03-18 03:59:59 UTC
        }

    @respx.mock
    async def test_update_nothing_to_change(self) -> None:
        fake = FakePhotos().on(REQ, "list", 1, captured("request-list"))
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_request(client, action="update", request="ZxOc9Ep8q")
        assert _error(exc)["param"] == "action"
        assert not fake.of(REQ, "update")

    @respx.mock
    async def test_unknown_request(self) -> None:
        install(FakePhotos().on(REQ, "list", 1, {"list": []}))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_request(client, action="delete", request="nope")
        assert _error(exc)["code"] == "not_found"

    @pytest.mark.parametrize(
        ("kwargs", "param"),
        [
            ({"action": "edit"}, "action"),
            ({"action": "create", "folder_path": "/a"}, "subject"),
            ({"action": "create", "subject": "s"}, "folder_path"),
            ({"action": "update"}, "request"),
            ({"action": "list", "max_file_mib": -1}, "max_file_mib"),
            ({"action": "list", "expires": "2030"}, "expires"),
            ({"action": "list", "expires": "2030-03-17T23:59:59"}, "expires"),
        ],
    )
    async def test_refusals(self, kwargs: dict[str, Any], param: str) -> None:
        with pytest.raises(ToolError) as exc:
            await photo_request(make_client(), **kwargs)
        assert _error(exc)["param"] == param


class TestDsmErrors:
    """A DSM error (here 160, no Photos privilege) is a structured error, not a crash."""

    @pytest.mark.parametrize(
        ("route", "call"),
        [
            (
                (FOLDER, "get", 2),
                lambda c: photo_folder(c, action="rename", folder_path="/a", name="b"),
            ),
            ((ITEM, "get", 2), lambda c: edit_photo(c, ids=[1], rating=3)),
            ((ITEM, "get", 2), lambda c: tag_photos(c, action="add", ids=[1], tags=["x"])),
            ((PERSON, "get", 1), lambda c: edit_person(c, action="hide", person_id=5)),
            ((PERSON, "get", 1), lambda c: merge_people(c, target_id=5, merge_ids=[6])),
            ((REQ, "list", 1), lambda c: photo_request(c, action="list")),
        ],
    )
    @respx.mock
    async def test_dsm_error(self, route: tuple[str, str, int], call: Any) -> None:
        install(FakePhotos().error(*route, 160))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await call(client)
        assert "160" in _error(exc)["message"]


def test_person_line_separates_thousands() -> None:
    assert person_line({"id": 12, "name": "Jo", "item_count": 1_234}) == (
        'Person 12: "Jo" (1,234 items, visible).'
    )


class TestNamesShownEscaped:
    """#604: Photos reads every name escaped, so a name sent back, or two that read the same,
    need care."""

    @respx.mock
    async def test_two_tags_that_read_the_same_are_refused(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(12))
        tags = {"list": [{"id": 4, "name": "a\\nb"}, {"id": 5, "name": "a\nb"}]}
        fake.on(TAG, "list", 1, tags)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await tag_photos(client, action="remove", ids=[12], tags=["a\\nb"])
        assert _error(exc)["code"] == "invalid_parameter"
        assert "Tags 4, 5 all read" in _error(exc)["message"]
        assert not fake.of(ITEM, "remove_tag")

    @respx.mock
    async def test_a_merge_never_sends_back_an_escaped_name(self) -> None:
        """QA round 1 on #605: it renamed the person to the escaped spelling."""
        people = captured("person-list", PEOPLE)
        for person in people["list"]:
            if person["id"] == 12:
                person["name"] = "Dov\u200f Cohen"
        fake = FakePhotos().on(PERSON, "list", 1, people)
        fake.on(
            PERSON,
            "get",
            1,
            {"list": [p for p in people["list"] if p["id"] == 12]},
            {"list": [p for p in people["list"] if p["id"] == 11]},
        )
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await merge_people(client, target_id=12, merge_ids=[11])
        assert (_error(exc)["code"], _error(exc)["param"]) == ("invalid_parameter", "name")
        assert not fake.of(PERSON, "merge")
        assert "without the backslash escapes" in _error(exc)["suggestion"]


class TestNamesPassedBackInQuotes:
    """QA r1 on #709: replies show names in "…", so a model may pass one back with the
    quotes. A lookup that finds nothing says so (advice only: a name with real quotes still
    works), and a call that sets such a name notes that the quotes are now part of it."""

    HINT = "If the name came from a reply in quotes, pass it without them."

    @respx.mock
    async def test_tag_add_notes_a_new_tag_in_quotes(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(12))
        fake.on(TAG, "list", 1, captured("tag-list"))
        fake.on(TAG, "create", 1, {"tag": {"id": 9, "name": '"beach"'}})
        fake.on(ITEM, "add_tag", 1, {"error_list": []})
        install(fake)
        client = make_client()
        async with client:
            out = await tag_photos(client, action="add", ids=[12], tags=['"beach"', "new2"])
        assert (
            '\n  Note: the new tag "\\"beach\\"" starts and ends with a quote mark, which is '
            "now part of it. If it came from a reply in quotes, remove it and add the tag "
            "without them." in out
        ), out
        assert out.count("Note: the new tag") == 1, out  # not for "new2"

    @respx.mock
    async def test_tag_add_of_an_existing_tag_in_quotes_has_no_note(self) -> None:
        tags = captured("tag-list")
        tags["list"].append({"id": 77, "name": '"quoted"'})
        fake = FakePhotos().on(ITEM, "get", 2, _items(12)).on(TAG, "list", 1, tags)
        fake.on(ITEM, "add_tag", 1, {"error_list": []})
        install(fake)
        client = make_client()
        async with client:
            out = await tag_photos(client, action="add", ids=[12], tags=['"quoted"'])
        assert fake.one(ITEM, "add_tag").params == {"id": [12], "tag": [77]}
        assert "Note:" not in out.split("dates and sizes")[0], out

    @respx.mock
    async def test_tag_remove_of_an_unknown_tag_in_quotes_hints(self) -> None:
        fake = FakePhotos().on(ITEM, "get", 2, _items(12))
        fake.on(TAG, "list", 1, captured("tag-list")).on(ITEM, "remove_tag", 1, {"error_list": []})
        install(fake)
        client = make_client()
        async with client:
            out = await tag_photos(client, action="remove", ids=[12], tags=["2024", '"gone"'])
        assert f"so there was nothing to remove. {self.HINT}" in out, out
        async with client:
            plain = await tag_photos(client, action="remove", ids=[12], tags=["2024", "gone"])
        assert self.HINT not in plain, plain

    @respx.mock
    async def test_person_lookup_by_a_name_in_quotes_hints(self) -> None:
        fake = FakePhotos().on(PERSON, "list", 1, captured("person-list", PEOPLE))
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await edit_person(client, action="hide", person_name='"Person 1"')
            with pytest.raises(ToolError) as plain:
                await edit_person(client, action="hide", person_name="Nobody")
        assert _error(exc)["code"] == "not_found"
        assert _error(exc)["suggestion"].endswith(f"lists people. {self.HINT}"), _error(exc)
        assert self.HINT not in _error(plain)["suggestion"]

    @respx.mock
    async def test_person_rename_and_merge_note_a_name_in_quotes(self) -> None:
        fake = FakePhotos().on(PERSON, "get", 1, captured("person-get", PEOPLE))
        fake.on(PERSON, "set", 1, captured("person-set-name", PEOPLE))
        fake.on(PERSON, "list", 1, captured("person-list", PEOPLE))
        fake.on(PERSON, "merge", 2, captured("person-merge-missing", PEOPLE))
        install(fake)
        client = make_client()
        note = (
            'Note: the person\'s name "\\"Jo\\"" starts and ends with a quote mark, which '
            "is now part of it. If it came from a reply in quotes, rename it without them."
        )
        async with client:
            renamed = await edit_person(client, action="rename", person_id=12, name='"Jo"')
            plain = await edit_person(client, action="rename", person_id=12, name="Jo")
            merged = await merge_people(client, target_id=12, merge_names=["Person 2"], name='"Jo"')
        assert renamed.splitlines()[1:] == [note], renamed
        assert "\n" not in plain, plain
        assert merged.splitlines()[1:] == [note], merged

    @respx.mock
    async def test_request_subject_in_quotes_is_noted(self) -> None:
        listed = captured("request-list")
        fake = FakePhotos().on(REQ, "list", 1, listed, listed, listed, listed)
        fake.on(REQ, "update", 1, {})
        fake.on("SYNO.FotoTeam.Browse.Folder", "get", 2, {"folder": {"id": 5, "name": "/team"}})
        fake.on(REQ, "create", 1, {**captured("request-create"), "library": "shared_space"})
        install(fake)
        client = make_client()
        tail = (
            'Note: the subject "\\"S\\"" starts and ends with a quote mark, which is now '
            "part of it. If it came from a reply in quotes, update the request's subject "
            "without them."
        )
        async with client:
            updated = await photo_request(
                client, action="update", request="ZxOc9Ep8q", subject='"S"'
            )
            made = await photo_request(
                client, action="create", subject='"S"', folder_path="/team", space="shared"
            )
            plain = await photo_request(client, action="update", request="ZxOc9Ep8q", subject="S")
        assert updated.splitlines()[-1] == tail, updated
        assert made.splitlines()[-1] == tail, made
        assert "Note: the subject" not in plain, plain

    @respx.mock
    async def test_a_nas_escape_shows_doubled_and_its_decoded_text_finds_it(self) -> None:
        """Pre-QA r4 on #709: a person whose name holds a newline is shown escaped (#604), and
        inside quotes that escape's backslash is doubled; passing the decoded text (the escape
        itself) finds the person, as the SYNOLOGY PHOTOS instructions say."""
        people = captured("person-list", PEOPLE)
        people["list"][0]["name"] = "Ann\nX"
        one = captured("person-get", PEOPLE)
        one["list"][0]["name"] = "Ann\nX"
        fake = FakePhotos().on(PERSON, "list", 1, people).on(PERSON, "get", 1, one)
        fake.on(PERSON, "show", 1, {})
        install(fake)
        client = make_client()
        async with client:
            out = await edit_person(client, action="hide", person_name="Ann\\nX")
        assert out.startswith('Person 12: "Ann\\\\nX" ('), out


def test_a_request_with_no_folder_shows_the_placeholder() -> None:
    """Pre-QA r4 on #709: a reply with no folder showed an empty one (`→  (personal space)`)."""
    from mcp_synology.modules.photos.organize import _request_block

    assert _request_block({}).splitlines()[0] == (
        "Photo request (no subject) → (unnamed) (personal space)"
    )


class TestQuotesRound2:
    """QA r2 on #709: the hint where photo_request adds "/", and no note on a name that
    already had the quotes."""

    PATH_HINT = "If the path came from a reply in quotes, pass it without them."

    @respx.mock
    async def test_photo_request_folder_in_quotes_hints(self) -> None:
        fake = FakePhotos().on(REQ, "list", 1, {"list": []})
        fake.error("SYNO.Foto.Browse.Folder", "get", 2, 642)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_request(
                    client, action="create", subject="s", folder_path="'/Up (shared)'"
                )
            with pytest.raises(ToolError) as plain:
                await photo_request(client, action="create", subject="s", folder_path="/Up")
        assert _error(exc)["suggestion"].endswith(self.PATH_HINT), _error(exc)
        assert self.PATH_HINT not in _error(plain)["suggestion"], _error(plain)
        assert not fake.of(REQ, "create")

    @respx.mock
    async def test_no_note_when_the_name_already_had_the_quotes(self) -> None:
        kept = captured("person-get", PEOPLE)
        kept["list"][0]["name"] = '"Jo"'
        people = captured("person-list", PEOPLE)
        fake = FakePhotos().on(PERSON, "get", 1, kept)
        fake.on(PERSON, "set", 1, {}).on(PERSON, "list", 1, people)
        fake.on(PERSON, "merge", 2, captured("person-merge-missing", PEOPLE))
        listed = captured("request-list")
        listed["list"][0]["subject"] = '"S"'
        fake.on(REQ, "list", 1, listed, listed).on(REQ, "update", 1, {})
        install(fake)
        client = make_client()
        async with client:
            renamed = await edit_person(client, action="rename", person_id=12, name='"Jo"')
            merged = await merge_people(client, target_id=12, merge_names=["Person 2"], name='"Jo"')
            updated = await photo_request(
                client, action="update", request="ZxOc9Ep8q", subject='"S"'
            )
        for out in (renamed, merged, updated):
            assert "Note: the" not in out, out


@respx.mock
async def test_several_new_tags_in_quotes_get_one_note() -> None:
    """Pre-QA r5 on #709: a quoted tag list passed back made one note line per tag."""
    fake = FakePhotos().on(ITEM, "get", 2, _items(12))
    fake.on(TAG, "list", 1, captured("tag-list"))
    fake.on(TAG, "create", 1, {"tag": {"id": 9, "name": "x"}}, {"tag": {"id": 10, "name": "y"}})
    fake.on(ITEM, "add_tag", 1, {"error_list": []})
    install(fake)
    client = make_client()
    async with client:
        out = await tag_photos(client, action="add", ids=[12], tags=['"a"', '"b"', "2024"])
    notes = [line for line in out.splitlines() if "Note: the new tag" in line]
    assert notes == [
        '  Note: the new tags "\\"a\\"", "\\"b\\"" start and end with a quote mark, which is '
        "now part of them. If they came from a reply in quotes, remove them and add the tags "
        "without them."
    ], out
