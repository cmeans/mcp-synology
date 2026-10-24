"""photo_album, photo_share_link and delete_photo_albums against captured replies (#258)."""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.photos.albums import (
    album_line,
    delete_photo_albums,
    photo_album,
    photo_share_link,
)
from tests.modules.photos.dsm import FakePhotos, captured, install, make_client

ALBUM = "SYNO.Foto.Browse.Album"
NORMAL = "SYNO.Foto.Browse.NormalAlbum"
COND = "SYNO.Foto.Browse.ConditionAlbum"
ITEM = "SYNO.Foto.Browse.Item"
SHARE = "SYNO.Foto.Sharing.Passphrase"


def _error(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    text = str(exc.value)
    return json.loads(text[text.index("{") :])["error"]  # type: ignore[no-any-return]


def _items(*ids: int) -> dict[str, Any]:
    return {"list": [{"id": i, "filename": f"f{i}.jpg", "folder_id": 10} for i in ids]}


def _album(**over: Any) -> dict[str, Any]:
    album = captured("album-get")["list"][0]
    album.update(over)
    return {"list": [album]}


class TestCreate:
    @respx.mock
    async def test_create_checks_ids_and_warns_on_duplicate_name(self) -> None:
        fake = FakePhotos().on(ALBUM, "list", 2, captured("album-list"))
        fake.on(ITEM, "get", 2, _items(12))
        fake.on(NORMAL, "create", 1, captured("album-create"))
        fake.on(ALBUM, "get", 1, _album())
        install(fake)
        client = make_client()
        async with client:
            out = await photo_album(client, action="create", name="mcp-spec-album", ids=[12, 99])
        assert fake.one(NORMAL, "create").params == {"name": "mcp-spec-album", "item": [12]}
        assert 'Created album "mcp-spec-album" (id 4) with 1 item(s).' in out
        assert "Not found: 99" in out
        assert "another album already has this name" in out
        assert "Album 4: mcp-spec-album · normal · 2 item(s)" in out

    @respx.mock
    async def test_create_smart_builds_condition(self) -> None:
        fake = FakePhotos().on(ALBUM, "list", 2, {"list": []})
        fake.on("SYNO.Foto.UserInfo", "me", 1, captured("userinfo-me"))
        fake.on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-by-name"))
        fake.on("SYNO.Foto.Browse.GeneralTag", "list", 1, captured("tag-list"))
        fake.on(COND, "peek_item_count", 2, {"count": 1234})
        fake.on(COND, "create", 2, captured("condalbum-create"))
        fake.on(ALBUM, "get", 1, _album(id=7, type="condition", name="mcp-spec-smart"))
        install(fake)
        client = make_client()
        async with client:
            out = await photo_album(
                client,
                action="create_smart",
                name="mcp-spec-smart",
                condition={
                    "folders": ["/mcp-spec-capture"],
                    "tags": ["mcp-spec-tag", "2024"],
                    "tags_match": "all",
                    "start_date": "2023-07-22",
                    "type": "video",
                    "ratings": [4, 5],
                    "keywords": ["sun"],
                },
            )
        cond = fake.one(COND, "create").params["condition"]
        assert cond == {
            "user_id": 2,
            "folder_filter": [10],
            "general_tag": [3, 4],
            "general_tag_policy": "and",
            "time": [{"start_time": 1689984000}],
            "item_type": [-2],
            "rating": [4, 5],
            "keyword": ["sun"],
            "keyword_policy": "or",
        }
        assert fake.one(COND, "peek_item_count").params["condition"] == cond
        assert "Matches 1,234 item(s) now." in out

    @respx.mock
    async def test_place_condition_uses_the_filter_tree_node(self) -> None:
        """A Geocoding.list place ID answers 950 in a condition; the tree node works."""
        fake = FakePhotos().on(ALBUM, "list", 2, {"list": []})
        fake.on("SYNO.Foto.UserInfo", "me", 1, captured("userinfo-me"))
        fake.on("SYNO.Foto.Browse.Geocoding", "list", 1, captured("geocoding-list"))
        fake.on("SYNO.Foto.Search.Filter", "list", 2, captured("search-filter-list"))
        fake.on(COND, "peek_item_count", 2, {"count": 1})
        fake.on(COND, "create", 2, captured("condalbum-create"))
        fake.on(ALBUM, "get", 1, _album(id=7, type="condition"))
        install(fake)
        client = make_client()
        async with client:
            await photo_album(client, action="create_smart", name="p", condition={"places": [6, 5]})
        # Place 6 is "Manhattan, New York" (node 9 under New York 8); place 5 is Chicago (4).
        assert fake.one(COND, "create").params["condition"]["geocoding"] == [9, 4]

    @pytest.mark.parametrize(
        "condition",
        [
            {},
            {"type": "all"},
            {"tags_match": "all"},
            {"color": "red"},
            {"type": "raw", "tags": []},
            {"type": ["photo"]},
            {"places": ["Chicago"]},
            {"places": [1.5]},
            {"places": ["\uff15"]},
            {"keywords": ["sun"], "keywords_match": "most"},
            {"ratings": [9]},
        ],
    )
    @respx.mock
    async def test_bad_conditions_refused(self, condition: dict[str, Any]) -> None:
        fake = FakePhotos().on(ALBUM, "list", 2, {"list": []})
        fake.on("SYNO.Foto.UserInfo", "me", 1, captured("userinfo-me"))
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_album(client, action="create_smart", name="x", condition=condition)
        assert _error(exc)["param"] == "condition"
        assert not fake.of(COND)


class TestChange:
    def _fake(self, **album: Any) -> FakePhotos:
        return FakePhotos().on(ALBUM, "get", 1, _album(**album))

    @respx.mock
    async def test_rename(self) -> None:
        fake = self._fake()
        fake.on(ALBUM, "set_name", 1, {})
        install(fake)
        client = make_client()
        async with client:
            out = await photo_album(client, action="rename", album="4", name="Trip")
        assert fake.one(ALBUM, "set_name").params == {"id": 4, "name": "Trip"}
        assert 'Renamed album 4 to "Trip".' in out

    @respx.mock
    async def test_add_skips_missing_ids(self) -> None:
        """DSM answers success for a missing item (verified), so it's checked first."""
        fake = self._fake().on(ITEM, "get", 2, _items(15))
        fake.on(NORMAL, "add_item", 1, {"error_list": [15]})
        install(fake)
        client = make_client()
        async with client:
            out = await photo_album(client, action="add", album="4", ids=[15, 999999])
        assert fake.one(NORMAL, "add_item").params == {"id": 4, "item": [15]}
        assert "Rejected by DSM: 15" in out and "Not found: 999999" in out

    @respx.mock
    async def test_remove(self) -> None:
        fake = self._fake().on(ITEM, "get", 2, _items(15))
        fake.on(NORMAL, "delete_item", 1, {})
        install(fake)
        client = make_client()
        async with client:
            out = await photo_album(client, action="remove", album="4", ids=[15])
        assert fake.one(NORMAL, "delete_item").params == {"id": 4, "item": [15]}
        assert "Removed 1 item(s) from album 4." in out
        assert "The photos themselves stay in their folders." in out

    @respx.mock
    async def test_set_cover_unknown_item(self) -> None:
        fake = self._fake().on(ITEM, "get", 2, {"list": []})
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_album(client, action="set_cover", album="4", ids=[99])
        assert _error(exc)["code"] == "not_found"
        assert not fake.of(ALBUM, "set_cover")

    @respx.mock
    async def test_album_line_without_dates_and_public(self) -> None:
        """An album with no dates, shared by a public link."""
        fake = self._fake(
            start_time=0,
            end_time=0,
            additional={"sharing_info": {"privacy_type": "public-view", "permission": []}},
        )
        fake.on(ALBUM, "set_name", 1, {})
        install(fake)
        client = make_client()
        async with client:
            out = await photo_album(client, action="rename", album="4", name="Trip")
        assert "· no dates · shared (public-view)" in out

    @respx.mock
    async def test_set_condition(self) -> None:
        fake = self._fake(type="condition")
        fake.on("SYNO.Foto.UserInfo", "me", 1, captured("userinfo-me"))
        fake.on("SYNO.Foto.Browse.Person", "list", 1, {"list": [{"id": 7, "name": "Alice"}]})
        fake.on(COND, "peek_item_count", 2, {"count": 3456})
        fake.on(COND, "set_condition", 2, {})
        install(fake)
        client = make_client()
        async with client:
            out = await photo_album(
                client,
                action="set_condition",
                album="4",
                condition={
                    "people": ["alice"],
                    "people_match": "all",
                    "end_date": "2024-01-31",
                    "ratings": [5],
                },
            )
        cond = {
            "user_id": 2,
            "person": [7],
            "person_policy": "and",
            "time": [{"end_time": 1706745599}],  # 2024-01-31 23:59:59 UTC
            "item_type": [],
            "rating": [5],
        }
        assert fake.one(COND, "set_condition").params == {"id": 4, "condition": cond}
        assert fake.one(COND, "peek_item_count").params["condition"] == cond
        assert "Album 4's condition is replaced.\nMatches 3,456 item(s) now." in out

    @respx.mock
    async def test_unknown_tag_in_condition(self) -> None:
        fake = self._fake(type="condition")
        fake.on("SYNO.Foto.UserInfo", "me", 1, captured("userinfo-me"))
        fake.on("SYNO.Foto.Browse.GeneralTag", "list", 1, captured("tag-list"))
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_album(
                    client, action="set_condition", album="4", condition={"tags": ["no-such"]}
                )
        assert _error(exc)["code"] == "not_found"
        assert not fake.of(COND)

    @respx.mock
    async def test_remove_none_found(self) -> None:
        fake = self._fake().on(ITEM, "get", 2, {"list": []})
        install(fake)
        client = make_client()
        async with client:
            out = await photo_album(client, action="remove", album="4", ids=[5])
        assert "none of the ids exist" in out
        assert not fake.of(NORMAL)

    @respx.mock
    async def test_set_cover_scalar(self) -> None:
        fake = self._fake().on(ITEM, "get", 2, _items(15)).on(ALBUM, "set_cover", 1, {})
        install(fake)
        client = make_client()
        async with client:
            await photo_album(client, action="set_cover", album="4", ids=[15])
        assert fake.one(ALBUM, "set_cover").params == {"id": 4, "id_item": 15}

    @respx.mock
    async def test_copy(self) -> None:
        fake = self._fake().on(NORMAL, "copy", 1, captured("album-copy"))
        install(fake)
        client = make_client()
        async with client:
            out = await photo_album(client, action="copy", album="4", name="copy")
        assert fake.one(NORMAL, "copy").params == {"name": "copy", "source_album_id": 4}
        assert "(id 6)" in out

    @respx.mock
    async def test_smart_album_items_refused(self) -> None:
        install(self._fake(type="condition"))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_album(client, action="add", album="4", ids=[1])
        assert "set_condition" in _error(exc)["suggestion"]

    @respx.mock
    async def test_set_condition_needs_smart_album(self) -> None:
        install(self._fake())
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_album(
                    client, action="set_condition", album="4", condition={"ratings": [5]}
                )
        assert "isn't a smart album" in _error(exc)["message"]

    @pytest.mark.parametrize(
        ("kwargs", "param"),
        [
            ({"action": "make"}, "action"),
            ({"action": "rename", "name": "x"}, "album"),
            ({"action": "create"}, "name"),
            ({"action": "add", "album": "1"}, "ids"),
            ({"action": "set_cover", "album": "1", "ids": [1, 2]}, "ids"),
            ({"action": "create_smart", "name": "x"}, "condition"),
            ({"action": "create", "name": "x", "ids": list(range(1001))}, "ids"),
        ],
    )
    async def test_refusals(self, kwargs: dict[str, Any], param: str) -> None:
        with pytest.raises(ToolError) as exc:
            await photo_album(make_client(), **kwargs)
        assert _error(exc)["param"] == param


class TestShareLink:
    @respx.mock
    async def test_share_with_settings(self) -> None:
        shared = captured("album-get-shared")
        fake = FakePhotos().on(ALBUM, "get", 1, _album(), shared)
        fake.on(SHARE, "set_shared", 1, captured("share-set-shared"))
        fake.on(SHARE, "update", 1, {})
        fake.on("SYNO.Foto.Sharing.Misc", "list_user_group", 1, captured("share-list-user-group"))
        install(fake)
        client = make_client()
        async with client:
            out = await photo_share_link(
                client,
                action="share",
                album="4",
                public="view",
                password="pw",
                expires="2030-03-17",
                members=[
                    {"name": "ADMINISTRATORS", "type": "group", "role": "download"},
                    {"name": "mcpadmin", "role": "none"},
                ],
            )
        assert fake.one(SHARE, "set_shared").params == {
            "policy": "album",
            "album_id": 4,
            "enabled": True,
        }
        assert fake.one(SHARE, "update").params == {
            "passphrase": "o0HVwFspk",
            "password": "pw",
            "expiration": 1900022399,  # 2030-03-17 23:59:59 UTC
            "permission": [
                {"action": "update", "role": "view", "member": {"type": "public"}},
                {"action": "update", "role": "download", "member": {"type": "group", "id": 101}},
                {"action": "delete", "member": {"type": "user", "id": 1026}},
            ],
        }
        assert "Link:      http://nas.example:5000/mo/sharing/o0HVwFspk" in out
        assert "Public:    view · password: yes" in out
        assert "plain HTTP" in out  # the test client's base URL is http://

    @respx.mock
    async def test_update_public_off_and_members_shown(self) -> None:
        """public=none removes the public entry; the reply lists members and expiry."""
        shared = captured("album-get-shared")
        info = shared["list"][0]["additional"]["sharing_info"]
        info.update(
            is_expired=True,
            privacy_type="private",
            permission=[
                {"member": {"type": "user", "id": 1026, "name": "eve"}, "role": "download"},
                {"member": {"type": "group", "id": 101}, "role": "view"},
            ],
        )
        fake = FakePhotos().on(ALBUM, "get", 1, shared)
        fake.on(SHARE, "update", 1, {})
        install(fake)
        client = make_client()
        async with client:
            out = await photo_share_link(client, action="update", album="4", public="none")
        assert fake.one(SHARE, "update").params["permission"] == [
            {"action": "delete", "member": {"type": "public"}}
        ]
        assert "(expired)" in out
        assert "eve (download)" in out and "group 101 (view)" in out

    @respx.mock
    async def test_update_needs_a_shared_album(self) -> None:
        install(FakePhotos().on(ALBUM, "get", 1, _album()))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_share_link(client, action="update", album="4", password="")
        assert "action=share first" in _error(exc)["suggestion"]

    @respx.mock
    async def test_stop(self) -> None:
        fake = FakePhotos().on(ALBUM, "get", 1, captured("album-get-shared"))
        fake.on(SHARE, "set_shared", 1, captured("share-stop"))
        install(fake)
        client = make_client()
        async with client:
            out = await photo_share_link(client, action="stop", album="4")
        assert fake.one(SHARE, "set_shared").params["enabled"] is False
        assert "no longer shared" in out

    @respx.mock
    async def test_unknown_member(self) -> None:
        fake = FakePhotos().on(ALBUM, "get", 1, captured("album-get-shared"))
        fake.on("SYNO.Foto.Sharing.Misc", "list_user_group", 1, captured("share-list-user-group"))
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_share_link(
                    client, action="update", album="4", members=[{"name": "eve"}]
                )
        assert _error(exc)["code"] == "not_found"
        assert not fake.of(SHARE)

    @staticmethod
    def _shared(**sharing: Any) -> dict[str, Any]:
        """The captured shared album (public view, password, expiry) with ``sharing`` set."""
        shared = captured("album-get-shared")
        shared["list"][0]["additional"]["sharing_info"].update(sharing)
        return shared  # type: ignore[no-any-return]

    @respx.mock
    @pytest.mark.parametrize(
        ("kwargs", "param", "message"),
        [
            ({"expires": "2030-03-17"}, "expires", "expires needs public access"),
            ({"password": "pw"}, "password", "password needs public access"),
            (
                {"password": "pw", "expires": "never", "public": "none"},
                "password",
                "password needs public access",
            ),
            (
                {"password": "pw", "expires": "2030-03-17"},
                "password",
                "password and expires need public access: Photos drops them",
            ),
        ],
    )
    @pytest.mark.parametrize("privacy", ["private", "public-view"])
    async def test_password_or_expiry_without_public_access_is_refused(
        self, kwargs: dict[str, Any], param: str, message: str, privacy: str
    ) -> None:
        """#348: Photos answers success and drops them on a link with public none.

        On a public link they're refused only with public=none in the same call.
        """
        if privacy == "public-view" and kwargs.get("public") != "none":
            kwargs = {**kwargs, "public": "none"}
        fake = FakePhotos().on(ALBUM, "get", 1, self._shared(privacy_type=privacy))
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_share_link(client, action="update", album="4", **kwargs)
        err = _error(exc)
        assert err["param"] == param
        assert message in err["message"]
        assert "public=view" in err["suggestion"]
        assert not fake.of(SHARE)

    @respx.mock
    async def test_share_with_expiry_but_no_public_shares_nothing(self) -> None:
        """A fresh share starts at public none; the refusal comes before set_shared."""
        fake = FakePhotos().on(ALBUM, "get", 1, _album())
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await photo_share_link(client, action="share", album="4", expires="2030-03-17")
        assert _error(exc)["param"] == "expires"
        assert not fake.of(SHARE)

    @respx.mock
    @pytest.mark.parametrize(
        ("privacy", "kwargs"),
        [
            ("private", {"password": "", "expires": "never"}),  # removing is fine
            ("private", {"public": "view", "expires": "2030-03-17", "password": "pw"}),
            ("public-view", {"expires": "2030-03-17"}),  # the link already has public
        ],
    )
    async def test_allowed_with_public_access_or_when_removing(
        self, privacy: str, kwargs: dict[str, Any]
    ) -> None:
        fake = FakePhotos().on(ALBUM, "get", 1, self._shared(privacy_type=privacy))
        fake.on(SHARE, "update", 1, {})
        install(fake)
        client = make_client()
        async with client:
            await photo_share_link(client, action="update", album="4", **kwargs)
        assert fake.one(SHARE, "update")

    @respx.mock
    async def test_public_off_says_what_photos_cleared(self) -> None:
        """#348: turning public access off clears the expiry and the password."""
        before = self._shared()  # public view, password, expiry
        after = self._shared(privacy_type="private", enable_password=False, expiration=0)
        fake = FakePhotos().on(ALBUM, "get", 1, before, after)
        fake.on(SHARE, "update", 1, {})
        install(fake)
        client = make_client()
        async with client:
            out = await photo_share_link(client, action="update", album="4", public="none")
        assert "turning public access off also cleared the link's expiry and password" in out

    @respx.mock
    async def test_public_off_without_settings_has_no_note(self) -> None:
        before = self._shared(enable_password=False, expiration=0)
        after = self._shared(privacy_type="private", enable_password=False, expiration=0)
        fake = FakePhotos().on(ALBUM, "get", 1, before, after)
        fake.on(SHARE, "update", 1, {})
        install(fake)
        client = make_client()
        async with client:
            out = await photo_share_link(client, action="update", album="4", public="none")
        assert "cleared" not in out

    @pytest.mark.parametrize(
        ("kwargs", "param"),
        [
            ({"action": "open"}, "action"),
            ({"action": "share", "public": "all"}, "public"),
            ({"action": "stop", "password": "x"}, "action"),
            ({"action": "share", "expires": "soon"}, "expires"),
            ({"action": "share", "expires": "2030-03-17 23:59"}, "expires"),
            ({"action": "share", "members": [{"type": "user"}]}, "members"),
            ({"action": "share", "members": [{"name": "a", "role": "own"}]}, "members"),
            ({"action": "share", "members": [{"name": "a", "type": "robot"}]}, "members"),
            ({"action": "update"}, "action"),
            ({"action": "update", "members": []}, "action"),
        ],
    )
    async def test_refusals(self, kwargs: dict[str, Any], param: str) -> None:
        with pytest.raises(ToolError) as exc:
            await photo_share_link(make_client(), album="4", **kwargs)
        assert _error(exc)["param"] == param


class TestDeleteAlbums:
    @respx.mock
    async def test_deletes_found_lists_missing(self) -> None:
        fake = FakePhotos().on(ALBUM, "list", 2, captured("album-list")).on(ALBUM, "delete", 1, {})
        install(fake)
        client = make_client()
        async with client:
            out = await delete_photo_albums(client, albums=["4", "nope"])
        assert fake.one(ALBUM, "delete").params == {"id": [4]}
        assert 'Deleted 1 album(s): 4 "mcp-spec-album"; their photos stay' in out
        assert "Not found: nope" in out

    @respx.mock
    async def test_ambiguous_name_deletes_nothing(self) -> None:
        albums = {"list": [{"id": 1, "name": "Trip"}, {"id": 2, "name": "trip"}]}
        fake = FakePhotos().on(ALBUM, "list", 2, albums)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError):
                await delete_photo_albums(client, albums=["trip"])
        assert not fake.of(ALBUM, "delete")

    @respx.mock
    async def test_nothing_found(self) -> None:
        fake = FakePhotos().on(ALBUM, "list", 2, {"list": []})
        install(fake)
        client = make_client()
        async with client:
            out = await delete_photo_albums(client, albums=["9"])
        assert out.startswith("No albums deleted.")
        assert not fake.of(ALBUM, "delete")

    async def test_empty(self) -> None:
        with pytest.raises(ToolError):
            await delete_photo_albums(make_client(), albums=[])

    @pytest.mark.parametrize("value", ["\u00b2", "\uff15"])
    @respx.mock
    async def test_non_ascii_digits_are_names(self, value: str) -> None:
        """A superscript or fullwidth digit is a name, not album id 5 (or a crash)."""
        albums = {"list": [{"id": 5, "name": "Five"}]}
        fake = FakePhotos().on(ALBUM, "list", 2, albums)
        install(fake)
        client = make_client()
        async with client:
            out = await delete_photo_albums(client, albums=[value])
        assert out.startswith("No albums deleted.")
        assert not fake.of(ALBUM, "delete")


class TestDsmErrors:
    """A DSM error (here 160, no Photos privilege) is a structured error, not a crash."""

    @pytest.mark.parametrize(
        ("route", "call"),
        [
            ((ALBUM, "get", 1), lambda c: photo_album(c, action="rename", album="4", name="x")),
            ((ALBUM, "get", 1), lambda c: photo_share_link(c, action="share", album="4")),
            ((ALBUM, "list", 2), lambda c: delete_photo_albums(c, albums=["4"])),
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


def test_album_line_separates_thousands() -> None:
    line = album_line({"id": 9, "name": "Trip", "type": "normal", "item_count": 12_345})
    assert "· 12,345 item(s) ·" in line
