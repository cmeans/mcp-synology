"""photo_album, photo_share_link and delete_photo_albums against captured replies (#258)."""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.photos.albums import delete_photo_albums, photo_album, photo_share_link
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
        fake.on(COND, "peek_item_count", 2, {"count": 4})
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
        assert "Matches 4 item(s) now." in out

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

    @pytest.mark.parametrize(
        ("kwargs", "param"),
        [
            ({"action": "open"}, "action"),
            ({"action": "share", "public": "all"}, "public"),
            ({"action": "stop", "password": "x"}, "action"),
            ({"action": "share", "expires": "soon"}, "expires"),
            ({"action": "share", "members": [{"type": "user"}]}, "members"),
            ({"action": "share", "members": [{"name": "a", "role": "own"}]}, "members"),
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
