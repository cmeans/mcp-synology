"""list_photos, list_photo_collections and get_photo against captured replies (#258)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.photos.browse import (
    _camera,
    _exif_value,
    _items,
    get_photo,
    list_photo_collections,
    list_photos,
)
from tests.modules.photos.dsm import (
    CAPTURE_191,
    PEOPLE,
    FakePhotos,
    captured,
    install,
    make_client,
)

ITEM = "SYNO.Foto.Browse.Item"
FOLDER = "SYNO.Foto.Browse.Folder"
_FOLDER_10 = {"folder": {"id": 10, "name": "/mcp-spec-capture", "parent": 3}}


def _error(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    text = str(exc.value)
    return json.loads(text[text.index("{") :])["error"]  # type: ignore[no-any-return]


def _folders(fake: FakePhotos) -> FakePhotos:
    return fake.on(FOLDER, "get", 2, _FOLDER_10)


class TestListPhotosRouting:
    @respx.mock
    async def test_one_folder_uses_item_list_and_count(self) -> None:
        fake = _folders(FakePhotos())
        fake.on(ITEM, "list", 2, captured("item-list-v2")).on(ITEM, "count", 1, {"count": 9})
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(client, folder_path="/mcp-spec-capture", limit=2)
        listed = fake.one(ITEM, "list").params
        assert listed["folder_id"] == 10
        assert listed["sort_by"] == "takentime" and listed["sort_direction"] == "desc"
        assert listed["additional"] == ["resolution"]
        assert fake.one(ITEM, "count").params == {"folder_id": 10}
        assert out.startswith("Photos in /mcp-spec-capture (personal space; newest first)")
        assert "forest-trail.jpg" in out
        assert "9 item(s) total; showing 1–" in out

    @respx.mock
    async def test_the_title_quotes_a_folder_ending_in_a_colon(self) -> None:
        """Pre-QA r3 on #709: the title's folder goes through `shown_folder`."""
        fake = FakePhotos().on(
            FOLDER, "get", 2, {"folder": {"id": 10, "name": "/Trips:", "parent": 3}}
        )
        fake.on(ITEM, "list", 2, captured("item-list-v2")).on(ITEM, "count", 1, {"count": 9})
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(client, folder_id=10, limit=2)
        assert out.startswith("Photos in '/Trips:' (personal space; newest first)"), out

    @respx.mock
    async def test_no_source_uses_list_with_filter(self) -> None:
        """Item.list ignores sort_by outside a folder; list_with_filter honors it."""
        fake = _folders(FakePhotos())
        fake.on(ITEM, "list_with_filter", 2, captured("item-list-with-filter"))
        fake.on(ITEM, "count_with_filter", 2, {"count": 4})
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(client, sort_by="filename", sort_direction="asc")
        params = fake.one(ITEM, "list_with_filter").params
        assert params["sort_by"] == "filename" and params["sort_direction"] == "asc"
        assert fake.one(ITEM, "count_with_filter").params == {}
        assert out.startswith("All photos and videos (personal space; by filename, ascending)")

    @respx.mock
    async def test_tag_and_filters(self) -> None:
        fake = _folders(FakePhotos())
        fake.on("SYNO.Foto.Browse.GeneralTag", "list", 1, captured("tag-list"))
        fake.on(ITEM, "list_with_filter", 2, captured("item-list-with-filter"))
        fake.on(ITEM, "count_with_filter", 2, {"count": 4})
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(
                client,
                tag="MCP-SPEC-TAG",
                start_date="2024-01-01",
                end_date="2024-12-31",
                type="photo",
                min_rating=4,
            )
        keys = fake.one(ITEM, "count_with_filter").params
        assert keys == {
            "general_tag": [3],
            "general_tag_policy": "or",
            "time": [{"start_time": 1704067200, "end_time": 1735689599}],
            "item_type": [-1],
            "rating": [4, 5],
        }
        assert {**keys, "offset": 0, "limit": 50}.items() <= fake.one(
            ITEM, "list_with_filter"
        ).params.items()
        assert 'Photos: tag "MCP-SPEC-TAG" (personal space; 2024-01-01 to 2024-12-31' in out

    @respx.mock
    async def test_the_title_quotes_names_holding_its_separator(self) -> None:
        """#695: the title joins the folder, person and tag with ", ", so each is quoted when
        it holds ", " (a tag's quotes JSON-escaped), and none can read as another."""
        fake = FakePhotos().on(
            FOLDER, "get", 2, {"folder": {"id": 10, "name": "/a, person X (3)", "parent": 3}}
        )
        person = captured("person-get", PEOPLE)
        person["list"][0]["name"] = 'Ann, tag "y"'
        fake.on("SYNO.Foto.Browse.Person", "get", 1, person)
        tags = captured("tag-list")
        tags["list"][0]["name"] = 'x", person Bob'
        fake.on("SYNO.Foto.Browse.GeneralTag", "list", 1, tags)
        fake.on(ITEM, "list_with_filter", 2, {"list": []})
        fake.on(ITEM, "count_with_filter", 2, {"count": 0})
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(
                client,
                folder_path="/a, person X (3)",
                person_id=12,
                tag='x", person Bob',
            )
        head = out.splitlines()[0]
        assert head.startswith(
            'Photos in \'/a, person X (3)\', person "Ann, tag \\"y\\"" (12), '
            'tag "x\\", person Bob" ('
        ), head

    @respx.mock
    async def test_person_sends_policy(self) -> None:
        fake = _folders(FakePhotos())
        fake.on("SYNO.Foto.Browse.Person", "get", 1, captured("person-get", PEOPLE))
        fake.on(ITEM, "list_with_filter", 2, {"list": []})
        fake.on(ITEM, "count_with_filter", 2, {"count": 0})
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(client, person_id=12)
        assert fake.one(ITEM, "count_with_filter").params == {"person": [12], "person_policy": "or"}
        assert "No items to display." in out and "0 item(s) total." in out

    @respx.mock
    async def test_unknown_tag_lists_nothing(self) -> None:
        install(FakePhotos().on("SYNO.Foto.Browse.GeneralTag", "list", 1, captured("tag-list")))
        client = make_client()
        async with client:
            out = await list_photos(client, tag="nope")
        assert 'no item has the tag "nope"' in out  # as the title writes it (pre-QA r2 on #695)

    @respx.mock
    async def test_unknown_tag_in_a_folder_keeps_photos_in(self) -> None:
        """Pre-QA r5 on #695: the unknown-tag branch's folder flag was unpinned."""
        fake = _folders(FakePhotos())
        fake.on("SYNO.Foto.Browse.GeneralTag", "list", 1, captured("tag-list"))
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(client, folder_path="/mcp-spec-capture", tag="nope")
        assert out.startswith('Photos in /mcp-spec-capture, tag "nope" ('), out

    @respx.mock
    async def test_place_uses_geocoding_id(self) -> None:
        fake = _folders(FakePhotos())
        fake.on(ITEM, "list", 2, captured("item-list-by-tag")).on(ITEM, "count", 1, {"count": 2})
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(client, place=5)
        assert fake.one(ITEM, "list").params["geocoding_id"] == 5
        assert fake.one(ITEM, "count").params == {"geocoding_id": 5}
        assert out.startswith("Photos: place 5")

    @respx.mock
    async def test_keyword_search(self) -> None:
        fake = _folders(FakePhotos())
        fake.on("SYNO.Foto.Search.Search", "list_item", 1, captured("search-list-item-filename"))
        fake.on("SYNO.Foto.Search.Search", "count_item", 1, {"count": 2})
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(client, keyword="2024")
        assert fake.one("SYNO.Foto.Search.Search", "list_item").params["keyword"] == "2024"
        assert 'search "2024"' in out and "beach-sunset_1.jpg" in out

    @respx.mock
    async def test_a_keyword_holding_a_quote_is_escaped_in_the_title(self) -> None:
        """Pre-QA r3 on #695: the keyword's quoting had no test."""
        fake = _folders(FakePhotos())
        fake.on("SYNO.Foto.Search.Search", "list_item", 1, {"list": []})
        fake.on("SYNO.Foto.Search.Search", "count_item", 1, {"count": 0})
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(client, keyword='a" (3)')
        assert out.startswith('Photos: search "a\\" (3)" ('), out

    @respx.mock
    async def test_album_total_from_album(self) -> None:
        fake = _folders(FakePhotos())
        fake.on("SYNO.Foto.Browse.Album", "get", 1, captured("album-get"))
        fake.on(ITEM, "list", 2, captured("item-list-album"))
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(client, album_id=4)
        assert fake.one(ITEM, "list").params["album_id"] == 4
        assert 'album "mcp-spec-album" (4)' in out and "2 item(s) total" in out

    @respx.mock
    async def test_an_unnamed_person_isnt_quoted_as_a_name(self) -> None:
        """Pre-QA r2 on #695: "(unnamed)" is the server's placeholder, not a name to pass back."""
        person = captured("person-get", PEOPLE)
        person["list"][0]["name"] = ""
        fake = FakePhotos().on("SYNO.Foto.Browse.Person", "get", 1, person)
        fake.on(ITEM, "list_with_filter", 2, {"list": []})
        fake.on(ITEM, "count_with_filter", 2, {"count": 0})
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(client, person_id=12)
        assert out.startswith("Photos: person (unnamed) (12) ("), out

    @respx.mock
    async def test_a_folder_ending_in_a_quote_is_quoted(self) -> None:
        """#701 after #698: shown() quotes a path that starts or ends with a quote, so the title's
        folder `/Trips'` can't read as a quoted `/Trips`, and the title still reads "Photos in"."""
        fake = FakePhotos().on(
            FOLDER, "get", 2, {"folder": {"id": 10, "name": "/Trips'", "parent": 3}}
        )
        fake.on(ITEM, "list", 2, captured("item-list-v2")).on(ITEM, "count", 1, {"count": 9})
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(client, folder_path="/Trips'")
        assert out.startswith('Photos in "/Trips\'" (personal space'), out

    @respx.mock
    async def test_names_holding_the_titles_other_separators_are_quoted(self) -> None:
        """#695 (pre-QA r1): an album's quote, and " (" in a person's or a folder's name, forged
        the title's ID or its space-and-order group."""
        album = captured("album-get")
        album["list"][0]["name"] = 'Trip" (7)'
        fake = _folders(FakePhotos()).on("SYNO.Foto.Browse.Album", "get", 1, album)
        fake.on(ITEM, "list", 2, captured("item-list-album"))
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(client, album_id=4)
        assert out.startswith('Photos: album "Trip\\" (7)" (4) ('), out

        fake = FakePhotos().on(
            FOLDER, "get", 2, {"folder": {"id": 10, "name": "/a (shared space)", "parent": 3}}
        )
        person = captured("person-get", PEOPLE)
        person["list"][0]["name"] = "Ann (13)"
        fake.on("SYNO.Foto.Browse.Person", "get", 1, person)
        fake.on(ITEM, "list_with_filter", 2, {"list": []})
        fake.on(ITEM, "count_with_filter", 2, {"count": 0})
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(client, folder_path="/a (shared space)", person_id=12)
        assert out.startswith(
            "Photos in '/a (shared space)', person \"Ann (13)\" (12) (personal space"
        ), out

    @respx.mock
    async def test_favorite_on_191_uses_v7(self) -> None:
        fake = FakePhotos().on(FOLDER, "get", 2, _FOLDER_10)
        fake.on(ITEM, "list_with_filter", 7, captured("fav_lwf-favorite-v7", CAPTURE_191))
        fake.on(ITEM, "count_with_filter", 7, {"count": 1})
        install(fake)
        client = make_client(CAPTURE_191)
        async with client:
            out = await list_photos(client, favorite=True)
        assert fake.one(ITEM, "count_with_filter").params == {"favorite": [True]}
        assert "favorites" in out

    @respx.mock
    async def test_concept_on_191(self) -> None:
        fake = FakePhotos().on(ITEM, "list", 7, {"list": []})
        fake.on(
            "SYNO.Foto.Browse.Concept",
            "get",
            2,
            {"list": [{"id": 1, "name": "dog", "item_count": 0}]},
        )
        install(fake)
        client = make_client(CAPTURE_191)
        async with client:
            out = await list_photos(client, concept=1)
        assert fake.one(ITEM, "list").params["concept_id"] == 1
        assert 'concept "dog"' in out

    @respx.mock
    async def test_concept_without_get_result_gives_no_total(self) -> None:
        """Concept.get's shape isn't captured: without it, no total, never the page length."""
        rows = captured("fav_list-folder-favorite-only-v7", CAPTURE_191)
        fake = FakePhotos().on(ITEM, "list", 7, rows)
        fake.on("SYNO.Foto.Browse.Concept", "get", 2, {"list": []})
        fake.on(FOLDER, "get", 2, {"folder": {"id": 31, "name": "/trips"}})
        install(fake)
        client = make_client(CAPTURE_191)
        async with client:
            out = await list_photos(client, concept=1, offset=50)
        assert "total" not in out
        assert "Showing 51–52. Pass offset=52 for the next page." in out

    @respx.mock
    async def test_indexing_row_marked(self) -> None:
        rows = captured("item-list-v2")
        rows["list"][0]["additional"]["resolution"] = {"width": 0, "height": 0}
        fake = _folders(FakePhotos()).on(ITEM, "list", 2, rows).on(ITEM, "count", 1, {"count": 1})
        install(fake)
        client = make_client()
        async with client:
            out = await list_photos(client, folder_id=10)
        # The file's modification time, read as UTC, isn't shown: indexing replaces it
        # with the local taken time, 5 h off on a US Central NAS (#415).
        row = next(ln for ln in out.splitlines() if "(indexing)" in ln)
        assert "— (indexing)" in row
        assert not re.search(r"\d{4}-\d\d-\d\d \d\d:", row), row


class TestListPhotosRefusals:
    @pytest.mark.parametrize(
        ("kwargs", "param"),
        [
            ({"keyword": "x", "folder_path": "/"}, "keyword"),
            ({"album_id": 1, "tag": "x"}, "album_id"),
            ({"place": 1, "type": "photo"}, "place"),
            ({"concept": 1, "album_name": "2"}, "album_name"),
            ({"type": "raw"}, "type"),
            ({"sort_by": "date"}, "sort_by"),
            ({"sort_direction": "up"}, "sort_direction"),
            ({"limit": 0}, "limit"),
            ({"limit": 501}, "limit"),
            ({"offset": -1}, "offset"),
            ({"min_rating": 6}, "min_rating"),
            ({"start_date": "2024-02-01", "end_date": "2024-01-01"}, "start_date"),
            ({"space": "team"}, "space"),
            ({"album_id": 1, "space": "shared"}, "space"),
        ],
    )
    async def test_refused_before_any_request(self, kwargs: dict[str, Any], param: str) -> None:
        with pytest.raises(ToolError) as exc:
            await list_photos(make_client(), **kwargs)
        err = _error(exc)
        assert err["code"] == "invalid_parameter"
        assert err["param"] == param

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"keyword": "x", "album_id": 1}, "keyword can't be combined with album_id."),
            ({"keyword": "x", "person_name": "Jo"}, "keyword can't be combined with person_name."),
            ({"album_name": "a", "person_id": 2}, "album_name can't be combined with person_id."),
        ],
    )
    async def test_conflicts_name_the_parameters_given(
        self, kwargs: dict[str, Any], message: str
    ) -> None:
        """Pre-QA r3 on #615: the conflict names the parameters the caller used."""
        with pytest.raises(ToolError) as exc:
            await list_photos(make_client(), **kwargs)
        assert _error(exc)["message"] == f"List photos failed: {message}"

    @pytest.mark.parametrize("kwargs", [{"favorite": True}, {"concept": 3}])
    async def test_191_features_refused_on_134(self, kwargs: dict[str, Any]) -> None:
        """1.3.4 silently ignores a favorite filter (every item comes back)."""
        with pytest.raises(ToolError) as exc:
            await list_photos(make_client(), **kwargs)
        assert _error(exc)["code"] == "not_supported"

    @respx.mock
    async def test_dsm_error_is_structured(self) -> None:
        install(FakePhotos().error("SYNO.FotoTeam.Browse.Item", "list_with_filter", 2, 801))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await list_photos(client, space="shared")
        err = _error(exc)
        assert err["code"] == "permission_denied"
        assert "801" in err["message"]


class TestCollections:
    @respx.mock
    async def test_albums(self) -> None:
        fake = FakePhotos().on("SYNO.Foto.Browse.Album", "list", 2, captured("album-list"))
        install(fake)
        client = make_client()
        async with client:
            out = await list_photo_collections(client, type="album", space="shared")
        assert fake.calls[0].api == "SYNO.Foto.Browse.Album"  # albums are personal only
        assert "mcp-spec-album" in out and "normal" in out
        assert "2020-09-13 – 2024-07-05" in out

    @respx.mock
    async def test_people_hidden_and_unnamed(self) -> None:
        people = captured("person-list", PEOPLE)
        people["list"][1].update(name="", show=False)
        fake = FakePhotos().on("SYNO.Foto.Browse.Person", "list", 1, people)
        install(fake)
        client = make_client()
        async with client:
            out = await list_photo_collections(client, type="person", include_hidden=True, limit=2)
        assert fake.one("SYNO.Foto.Browse.Person", "list").params["show_hidden"] is True
        assert "(unnamed)" in out and "yes" in out
        assert "5 person(s) total; showing 1–2. Pass offset=2 for the next page." in out

    @respx.mock
    async def test_shared_people_off(self) -> None:
        fake = FakePhotos().on("SYNO.FotoTeam.Browse.Folder", "get", 2, captured("team_team-root"))
        fake.on("SYNO.Foto.Setting.TeamSpace", "get", 1, captured("qa1_setting-teamspace-get"))
        install(fake)
        client = make_client()
        async with client:
            out = await list_photo_collections(client, type="person", space="shared")
        assert "People are turned off for Shared Space" in out

    @respx.mock
    async def test_tags_name_filter(self) -> None:
        install(FakePhotos().on("SYNO.Foto.Browse.GeneralTag", "list", 1, captured("tag-list")))
        client = make_client()
        async with client:
            out = await list_photo_collections(client, type="tag", name="SPEC")
        assert "mcp-spec-tag" in out and "2024" not in out.split("matching")[1].split("\n", 1)[1]
        assert "1 tag(s) total" in out

    @respx.mock
    async def test_places(self) -> None:
        install(
            FakePhotos().on("SYNO.Foto.Browse.Geocoding", "list", 1, captured("geocoding-list"))
        )
        client = make_client()
        async with client:
            out = await list_photo_collections(client, type="place")
        assert "Manhattan, New York" in out and "United States" in out

    @respx.mock
    async def test_folders_under_parent(self) -> None:
        fake = (
            FakePhotos()
            .on(FOLDER, "get", 2, _FOLDER_10)
            .on(FOLDER, "list", 2, captured("folder-list"))
        )
        install(fake)
        client = make_client()
        async with client:
            out = await list_photo_collections(
                client, type="folder", parent_path="/mcp-spec-capture"
            )
        assert fake.one(FOLDER, "list").params["id"] == 10
        assert "/mcp-spec-capture/2024" in out

    @respx.mock
    async def test_folders_heading_quotes_the_parent_and_the_filter(self) -> None:
        """#700: "Folders in <parent> matching "<name>"" wrote both raw."""
        parent = {"folder": {"id": 10, "name": "/a (x", "parent": 3}}
        fake = FakePhotos().on(FOLDER, "get", 2, parent)
        fake.on(FOLDER, "list", 2, captured("folder-list"))
        install(fake)
        client = make_client()
        async with client:
            out = await list_photo_collections(client, type="folder", parent_id=10, name='2"')
        assert out.startswith('Folders in \'/a (x\' matching "2\\""'), out

    @respx.mock
    @pytest.mark.parametrize(
        ("name", "heading"),
        [
            ("x", "Folders in '/a matching b' matching \"x\""),
            (None, "Folders in /a matching b"),
        ],
    )
    async def test_folders_heading_quotes_matching_only_beside_a_filter(
        self, name: str | None, heading: str
    ) -> None:
        """Pre-QA r2 on #700: " matching " follows the parent only when a filter is given."""
        parent = {"folder": {"id": 10, "name": "/a matching b", "parent": 3}}
        fake = FakePhotos().on(FOLDER, "get", 2, parent)
        fake.on(FOLDER, "list", 2, captured("folder-list"))
        install(fake)
        client = make_client()
        async with client:
            out = await list_photo_collections(client, type="folder", parent_id=10, name=name)
        assert out.splitlines()[0].startswith(heading), out

    @respx.mock
    async def test_folders_shared_flag_on_191(self) -> None:
        """1.9.1's sharing_info has only sharing_link; the state is the top-level flag."""
        listed = captured("folder-list", CAPTURE_191)
        listed["list"][0]["shared"] = True
        fake = (
            FakePhotos()
            .on(FOLDER, "get", 2, {"folder": {"id": 21, "name": "/mcp-spec-capture"}})
            .on(FOLDER, "list", 2, listed)
        )
        install(fake)
        client = make_client(CAPTURE_191)
        async with client:
            out = await list_photo_collections(client, type="folder", parent_id=21)
        rows = {
            line.split()[1]: line.split()[-1]
            for line in out.splitlines()
            if len(line.split()) == 3 and line.split()[0].isdigit()
        }
        first = str(listed["list"][0]["name"])
        assert rows.pop(first) == "yes"
        assert set(rows.values()) == {"no"}

    @respx.mock
    async def test_shared_with_me_empty(self) -> None:
        fake = FakePhotos().on(
            "SYNO.Foto.Sharing.Misc", "list_shared_with_me_album", 2, captured("share-with-me")
        )
        install(fake)
        client = make_client()
        async with client:
            out = await list_photo_collections(client, type="shared_with_me")
        assert "No items to display." in out

    async def test_concepts_refused_on_134(self) -> None:
        with pytest.raises(ToolError) as exc:
            await list_photo_collections(make_client(), type="concept")
        assert _error(exc)["code"] == "not_supported"

    @respx.mock
    async def test_concepts_on_191(self) -> None:
        concepts = {"list": [{"id": 1, "name": "dog", "item_count": 3, "visibility": True}]}
        install(FakePhotos().on("SYNO.Foto.Browse.Concept", "list", 2, concepts))
        client = make_client(CAPTURE_191)
        async with client:
            out = await list_photo_collections(client, type="concept")
        assert "dog" in out

    @pytest.mark.parametrize(
        ("kwargs", "param"),
        [
            ({"type": "albums"}, "type"),
            ({"type": "tag", "parent_path": "/"}, "parent_path"),
            ({"type": "tag", "limit": 0}, "limit"),
        ],
    )
    async def test_refusals(self, kwargs: dict[str, Any], param: str) -> None:
        with pytest.raises(ToolError) as exc:
            await list_photo_collections(make_client(), **kwargs)
        assert _error(exc)["param"] == param


class TestGetPhoto:
    @respx.mock
    async def test_full_block_and_missing(self) -> None:
        item = captured("item-get")
        item["list"][0]["additional"].update(
            rating=4,
            description="spec capture",
            tag=[{"id": 3, "name": "beach"}],
            person=[{"id": 1, "name": "Jo"}, {"id": 2, "name": ""}],
        )
        fake = _folders(FakePhotos()).on(ITEM, "get", 2, item)
        fake.on(ITEM, "get_exif", 1, captured("item-get-exif"))
        install(fake)
        client = make_client()
        async with client:
            out = await get_photo(client, ids=[12, 999999, 12], exif=True)
        assert fake.one(ITEM, "get").params["id"] == [12, 999999]
        assert "Item 12: forest-trail.jpg" in out
        assert "1600×1200 · photo" in out
        assert "Rating:       4" in out and "favorite" not in out
        assert '"Jo", (unnamed)' in out
        assert "44.428, -110.5885 · Teton County, Wyoming, United States" in out
        assert "SeedCam 1" in out
        assert "GPS Latitude Reference" in out
        assert out.endswith("Not found: 999999")

    @respx.mock
    async def test_names_holding_the_list_separator_are_quoted(self) -> None:
        """#700: Tags and People join names with ", ", so a person "Jo, Ann" read as two people
        and a tag "a, b" as two tags; a folder holding " (" read as the space."""
        item = captured("item-get")
        item["list"][0]["additional"].update(
            tag=[{"id": 3, "name": "a, b"}, {"id": 4, "name": 'x"y'}],
            person=[{"id": 1, "name": "Jo, Ann"}, {"id": 2, "name": ""}],
        )
        folder = {"folder": {"id": 10, "name": "/a (Shared Space)", "parent": 3}}
        install(FakePhotos().on(FOLDER, "get", 2, folder).on(ITEM, "get", 2, item))
        client = make_client()
        async with client:
            out = await get_photo(client, ids=[12])
        assert 'Tags:         "a, b", "x\\"y"' in out, out
        assert 'People:       "Jo, Ann", (unnamed)' in out, out
        assert "Folder:       '/a (Shared Space)' (personal space)" in out, out

    @respx.mock
    async def test_indexing_item_has_no_taken_time_yet(self) -> None:
        item = captured("item-get")
        item["list"][0]["additional"]["resolution"] = {"width": 0, "height": 0}
        install(_folders(FakePhotos()).on(ITEM, "get", 2, item))
        client = make_client()
        async with client:
            out = await get_photo(client, ids=[12])
        assert re.search(r"^  Taken: +— \(not known yet: still indexing\)$", out, re.MULTILINE), out

    @respx.mock
    async def test_whitespace_description_shows_as_dash(self) -> None:
        """Some cameras pad EXIF ImageDescription with spaces, and Photos takes the
        description from it (0.10.0rc5 live test, a Nikon E3500 photo; #362)."""
        item = captured("item-get")
        item["list"][0]["additional"].update(description="                   ")
        install(_folders(FakePhotos()).on(ITEM, "get", 2, item))
        client = make_client()
        async with client:
            out = await get_photo(client, ids=[12])
        assert re.search(r"^  Description: +—$", out, re.MULTILINE), out

    @respx.mock
    async def test_exif_table_cuts_long_values_and_hides_binary(self) -> None:
        # Real values from a phone photo on DSM 7.1.1: a 3,029-character Maker Note byte
        # dump padded every row, and "Picture Info" holds binary (here already read as
        # U+FFFD for its non-UTF-8 bytes).
        fake = _folders(FakePhotos()).on(ITEM, "get", 2, captured("item-get"))
        maker = "77 79 84 0 " * 300
        fake.on(
            ITEM,
            "get_exif",
            1,
            {
                "list": [
                    {
                        "id": 12,
                        "exif": [
                            {"key": "Maker Note", "value": maker},
                            {"key": "Picture Info", "value": "\x1ey\x14\ufffd\x1b"},
                            {"key": "Model", "value": "motorola razr plus 2023"},
                        ],
                    }
                ]
            },
        )
        install(fake)
        client = make_client()
        async with client:
            out = await get_photo(client, ids=[12], exif=True)
        assert f"{maker[:80]}… (3,300 characters)" in out
        assert "(binary data, 5 characters; not shown)" in out
        assert "motorola razr plus 2023" in out
        assert max(len(line) for line in out.splitlines()) < 140

    @pytest.mark.parametrize(
        ("value", "shown"),
        [
            ("a" * 80, "a" * 80),
            ("a" * 81, "a" * 80 + "… (81 characters)"),
            ("a\tb", "a b"),
            # A line break stays visible, escaped by the client (#604); a literal backslash
            # sequence in text is shown as it is.
            ("line one\r\nline two\nthree", "line one\\r\\nline two\\nthree"),
            ("C:\\new\\x41b", "C:\\new\\x41b"),
            # The cap counts characters as DSM sent them, and a cut keeps an escape whole.
            ("a" * 70 + "\n" * 10, "a" * 70 + "\\n" * 10),
            ("a" * 78 + "\u202e" * 3, "a" * 78 + "\\u202e" * 2 + "… (81 characters)"),
            ("Canon\ufffd", "(binary data, 6 characters; not shown)"),
            ("\x17", "(binary data, 1 character; not shown)"),
            ("x\x85y", "(binary data, 3 characters; not shown)"),
            ("\x1ey", "(binary data, 2 characters; not shown)"),
            ("\x00" * 1_200, "(binary data, 1,200 characters; not shown)"),
        ],
    )
    def test_exif_value(self, value: str, shown: str) -> None:
        from mcp_synology.core.formatting import escape_controls

        # As the client hands it over: every DSM reply is escaped where it's read (#604).
        assert _exif_value(escape_controls(value)) == shown

    @pytest.mark.parametrize(
        ("exif", "line"),
        [
            # Photos 1.3.4 (DSM 7.1.1) formats these itself; nothing is added.
            (
                {"camera": "razr", "aperture": "F1.5", "exposure_time": "1/30 s", "iso": "500"},
                "razr · lens — · F1.5 · 1/30 s · ISO 500",
            ),
            (
                {"camera": "razr", "aperture": "1.5", "exposure_time": "1/30", "iso": "500"},
                "razr · lens — · f/1.5 · 1/30 s · ISO 500",
            ),
            ({"camera": "SeedCam"}, "SeedCam · lens — · aperture — · exposure — · ISO —"),
            # Not a bare number: shown as sent, never a doubled unit.
            (
                {"camera": "x", "aperture": "2.8 (max)"},
                "x · lens — · 2.8 (max) · exposure — · ISO —",
            ),
            (
                {"camera": "x", "exposure_time": "0.5 sec"},
                "x · lens — · aperture — · 0.5 sec · ISO —",
            ),
            ({"aperture": "", "exposure_time": ""}, "—"),
        ],
    )
    def test_camera_line_units(self, exif: dict[str, str], line: str) -> None:
        assert _camera(exif) == line

    @respx.mock
    async def test_favorite_on_191(self) -> None:
        fake = FakePhotos().on(FOLDER, "get", 2, _FOLDER_10)
        fake.on(ITEM, "get", 7, captured("fav191_get-additional-favorite-v7", CAPTURE_191))
        install(fake)
        client = make_client(CAPTURE_191)
        async with client:
            out = await get_photo(client, ids=[38])
        assert "favorite" in fake.one(ITEM, "get").params["additional"]
        assert "favorite: yes" in out

    @respx.mock
    async def test_shared_missing_mentions_permission(self) -> None:
        install(FakePhotos().on("SYNO.FotoTeam.Browse.Item", "get", 2, {"list": []}))
        client = make_client()
        async with client:
            out = await get_photo(client, ids=[5], space="shared")
        assert out == "Not found, or no permission for its Shared Space folder: 5"

    @pytest.mark.parametrize("ids", [[], list(range(21))])
    async def test_id_count(self, ids: list[int]) -> None:
        with pytest.raises(ToolError) as exc:
            await get_photo(make_client(), ids=ids)
        assert _error(exc)["param"] == "ids"


def test_privacy_column() -> None:
    from mcp_synology.modules.photos.browse import _privacy

    assert _privacy({"privacy_type": "public-view"}) == "public-view"
    assert _privacy({"privacy_type": "private"}, shared=True) == "private link"
    assert _privacy(None) == "no"


def test_collection_item_counts_separate_thousands() -> None:
    assert _items({"item_count": 12_345}) == "12,345"
    assert _items({"item_count": None}) == "0"
    assert _items({}) == "0"


def test_collection_limit_refusal_separates_thousands() -> None:
    from mcp_synology.modules.photos.browse import _check_paging

    with pytest.raises(ToolError) as exc:
        _check_paging("List photo collections", 0, 5_000, 1_000)
    assert json.loads(str(exc.value))["error"]["message"].endswith("limit must be 1–1,000.")


class TestNamesPassedBackInQuotes:
    """QA r1 on #709: an album or tag passed back in a reply's quotes and not found."""

    HINT = "If the name came from a reply in quotes, pass it without them."

    @respx.mock
    async def test_an_album_name_in_quotes_hints(self) -> None:
        install(FakePhotos().on("SYNO.Foto.Browse.Album", "list", 2, captured("album-list")))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await list_photos(client, album_name='"Trip 2024"')
            with pytest.raises(ToolError) as plain:
                await list_photos(client, album_name="Trip 2024")
        assert _error(exc)["suggestion"].endswith(f"lists your albums. {self.HINT}"), _error(exc)
        assert self.HINT not in _error(plain)["suggestion"]

    @respx.mock
    async def test_a_tag_in_quotes_with_no_items_hints(self) -> None:
        install(FakePhotos().on("SYNO.Foto.Browse.GeneralTag", "list", 1, captured("tag-list")))
        client = make_client()
        async with client:
            out = await list_photos(client, tag='"2024"')
            plain = await list_photos(client, tag="nope")
        assert out.endswith(f"while some item has it). {self.HINT}"), out
        assert self.HINT not in plain, plain


@respx.mock
async def test_an_empty_tag_is_shown_as_the_callers_text() -> None:
    """Pre-QA r6 on #709: `(unnamed)` is for a name missing from the NAS, not the caller's
    empty tag."""
    install(FakePhotos().on("SYNO.Foto.Browse.GeneralTag", "list", 1, captured("tag-list")))
    client = make_client()
    async with client:
        out = await list_photos(client, tag="")
    assert 'no item has the tag "" (' in out and "(unnamed)" not in out, out
