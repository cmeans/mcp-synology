"""Photos plumbing: spaces, JSON-encoded POSTs, lookups by ID, path or name (#258)."""

from __future__ import annotations

import json

import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import ApiNotFoundError, SynologyError
from mcp_synology.modules.photos.helpers import (
    Photos,
    check_space,
    encode,
    id_list,
    is_indexing,
    page_footer,
    parse_date,
    prefix,
)
from tests.modules.photos.dsm import CAPTURE_191, PEOPLE, FakePhotos, captured, install, make_client


def _error(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, object]:
    text = str(exc.value)
    return json.loads(text[text.index("{") :])["error"]  # type: ignore[no-any-return]


class TestBasics:
    def test_prefix(self) -> None:
        assert prefix("personal") == "SYNO.Foto"
        assert prefix("shared") == "SYNO.FotoTeam"

    def test_check_space_rejects_unknown(self) -> None:
        assert check_space("Op", "shared") == "shared"
        with pytest.raises(ToolError) as exc:
            check_space("Op", "team")
        err = _error(exc)
        assert err["param"] == "space"
        assert err["valid"] == ["personal", "shared"]

    def test_encode_quotes_every_value(self) -> None:
        """#121: a folder named 2024 must reach DSM as a string."""
        assert encode({"name": "2024", "id": 3, "ok": True, "ids": [1], "x": None}) == {
            "name": '"2024"',
            "id": "3",
            "ok": "true",
            "ids": "[1]",
        }

    def test_parse_date(self) -> None:
        assert parse_date("Op", "d", "2024-07-04") == 1720051200
        assert parse_date("Op", "d", "2024-07-04", end=True) == 1720051200 + 86399
        with pytest.raises(ToolError) as exc:
            parse_date("Op", "start_date", "07/04/2024")
        assert _error(exc)["param"] == "start_date"

    def test_page_footer(self) -> None:
        assert (
            page_footer(7, 0, 5) == "7 item(s) total; showing 1–5. Pass offset=5 for the next page."
        )
        assert page_footer(7, 5, 2) == "7 item(s) total; showing 6–7."
        assert page_footer(7, 10, 0) == "7 item(s) total."

    def test_is_indexing(self) -> None:
        assert is_indexing({"additional": {"resolution": {"width": 0, "height": 0}}})
        assert not is_indexing({"additional": {"resolution": {"width": 4, "height": 3}}})
        assert not is_indexing({})


class TestCall:
    @respx.mock
    async def test_form_post_with_json_values(self) -> None:
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-by-name"))
        install(fake)
        client = make_client()
        async with client:
            data = await Photos(client, "personal", "Op").call(
                "Browse.Folder", "get", {"name": "/mcp-spec-capture"}, version=2
            )
        assert data["folder"]["id"] == 10
        call = fake.one("SYNO.Foto.Browse.Folder", "get")
        assert call.http == "POST"
        assert call.params == {"name": "/mcp-spec-capture"}

    @respx.mock
    async def test_shared_space_uses_fototeam(self) -> None:
        fake = FakePhotos().on("SYNO.FotoTeam.Browse.Folder", "get", 2, captured("team_team-root"))
        install(fake)
        client = make_client()
        async with client:
            folder = await Photos(client, "shared", "Op").folder("/")
        assert folder["id"] == 1
        assert fake.one("SYNO.FotoTeam.Browse.Folder", "get").params == {"id": 0}

    async def test_missing_api_names_the_package(self) -> None:
        client = make_client()
        client._api_cache = {}
        with pytest.raises(ApiNotFoundError) as exc:
            await Photos(client, "shared", "Op").call("Browse.Item", "list")
        assert "Shared Space" in (exc.value.suggestion or "")

    def test_require_v7(self) -> None:
        Photos(make_client(CAPTURE_191), "personal", "Op").require_v7("favorites")
        with pytest.raises(ToolError) as exc:
            Photos(make_client(), "personal", "Op").require_v7("favorites", param="favorite")
        err = _error(exc)
        assert err["code"] == "not_supported"
        assert "1.9.1" in str(err["message"])

    @respx.mock
    async def test_walk_pages_until_short(self) -> None:
        full = {"list": [{"id": i, "name": f"t{i}"} for i in range(500)]}
        last = {"list": [{"id": 500, "name": "t500"}]}
        fake = FakePhotos().on("SYNO.Foto.Browse.GeneralTag", "list", 1, full, last)
        install(fake)
        client = make_client()
        async with client:
            tags = await Photos(client, "personal", "Op").walk("Browse.GeneralTag")
        assert len(tags) == 501
        assert [c.params["offset"] for c in fake.calls] == [0, 500]


class TestFolders:
    @pytest.mark.parametrize(
        ("value", "sent"),
        [
            ("10", {"id": 10}),
            ("/", {"id": 0}),
            ("", {"id": 0}),
            ("mcp-spec-capture/", {"name": "/mcp-spec-capture"}),
        ],
    )
    @respx.mock
    async def test_id_root_or_path(self, value: str, sent: dict[str, object]) -> None:
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-by-name"))
        install(fake)
        client = make_client()
        async with client:
            await Photos(client, "personal", "Op").folder(value)
        assert fake.one("SYNO.Foto.Browse.Folder", "get").params == sent

    @respx.mock
    async def test_missing_folder_is_not_found(self) -> None:
        install(FakePhotos().error("SYNO.Foto.Browse.Folder", "get", 2, 642))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await Photos(client, "personal", "Op").folder("/nope", param="parent")
        err = _error(exc)
        assert err["code"] == "not_found"
        assert err["param"] == "parent"

    @respx.mock
    async def test_other_errors_pass_through(self) -> None:
        install(FakePhotos().error("SYNO.FotoTeam.Browse.Folder", "get", 2, 801))
        client = make_client()
        async with client:
            with pytest.raises(SynologyError) as exc:
                await Photos(client, "shared", "Op").folder("/")
        assert exc.value.code == 801

    @respx.mock
    async def test_folder_path_cached_and_falls_back(self) -> None:
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-by-name"))
        install(fake)
        client = make_client()
        async with client:
            ph = Photos(client, "personal", "Op")
            assert await ph.folder_path(10) == "/mcp-spec-capture"
            assert await ph.folder_path(10) == "/mcp-spec-capture"
        assert len(fake.calls) == 1

    @respx.mock
    async def test_folder_path_unreadable(self) -> None:
        fake = FakePhotos().error("SYNO.Foto.Browse.Folder", "get", 2, 642)
        install(fake)
        client = make_client()
        async with client:
            ph = Photos(client, "personal", "Op")
            assert await ph.folder_path(77) == "folder 77"
            assert await ph.folder_path(77) == "folder 77"
        assert len(fake.calls) == 1  # no such folder: kept for the call

    @respx.mock
    async def test_folder_path_other_error_logged_and_retried(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake = FakePhotos().error("SYNO.Foto.Browse.Folder", "get", 2, 117)
        install(fake)
        client = make_client()
        caplog.set_level("DEBUG", logger="mcp_synology.modules.photos.helpers")
        async with client:
            ph = Photos(client, "personal", "Op")
            assert await ph.folder_path(77) == "folder 77"
            assert await ph.folder_path(77) == "folder 77"
        assert len(fake.calls) == 2  # may pass next time, so not kept
        assert "Folder 77 path lookup failed" in caplog.text

    @respx.mock
    async def test_non_ascii_digits_are_a_name(self) -> None:
        """'²'.isdigit() is True but int('²') raises: it's looked up as a path."""
        fake = FakePhotos().error("SYNO.Foto.Browse.Folder", "get", 2, 642)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await Photos(client, "personal", "Op").folder("²")
        assert _error(exc)["code"] == "not_found"
        assert fake.one("SYNO.Foto.Browse.Folder", "get").params == {"name": "/²"}


class TestNames:
    @respx.mock
    async def test_person_by_id(self) -> None:
        fake = FakePhotos().on("SYNO.Foto.Browse.Person", "get", 1, captured("person-get", PEOPLE))
        install(fake)
        client = make_client()
        async with client:
            person = await Photos(client, "personal", "Op").person("12")
        assert person["name"] == "Person 1"

    @respx.mock
    async def test_digits_fall_back_to_a_name(self) -> None:
        """An album named "2024": no album has ID 2024, so the name is tried."""
        albums = {"list": [{"id": 4, "name": "2024", "item_count": 1}]}
        fake = (
            FakePhotos()
            .on("SYNO.Foto.Browse.Album", "get", 1, {"list": []})
            .on("SYNO.Foto.Browse.Album", "list", 2, albums)
        )
        install(fake)
        client = make_client()
        async with client:
            album = await Photos(client, "shared", "Op").album("2024")
        assert album["id"] == 4
        assert fake.one("SYNO.Foto.Browse.Album", "list").api.startswith("SYNO.Foto.")

    @respx.mock
    async def test_id_lookup_error_falls_back_but_setup_errors_raise(self) -> None:
        fake = (
            FakePhotos()
            .error("SYNO.Foto.Browse.Album", "get", 1, 609)
            .on("SYNO.Foto.Browse.Album", "list", 2, {"list": []})
        )
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await Photos(client, "personal", "Op").album("9")
        assert _error(exc)["code"] == "not_found"
        respx.clear()
        install(FakePhotos().error("SYNO.Foto.Browse.Album", "get", 1, 160))
        async with client:
            with pytest.raises(SynologyError) as info:
                await Photos(client, "personal", "Op").album("9")
        assert info.value.code == 160

    @respx.mock
    async def test_ambiguous_name_lists_ids(self) -> None:
        people = {"list": [{"id": 1, "name": "Jo"}, {"id": 2, "name": "jo"}]}
        install(FakePhotos().on("SYNO.Foto.Browse.Person", "list", 1, people))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await Photos(client, "personal", "Op").person("JO")
        err = _error(exc)
        assert err["code"] == "invalid_parameter"
        assert "1 (Jo)" in str(err["message"]) and "2 (jo)" in str(err["message"])

    @respx.mock
    async def test_person_walk_includes_hidden(self) -> None:
        fake = FakePhotos().on(
            "SYNO.Foto.Browse.Person", "list", 1, captured("person-list", PEOPLE)
        )
        install(fake)
        client = make_client()
        async with client:
            person = await Photos(client, "personal", "Op").person("person 3")
        assert person["id"] == 19
        params = fake.one("SYNO.Foto.Browse.Person", "list").params
        assert params["show_hidden"] is True and params["show_more"] is True

    @respx.mock
    async def test_tag_ids_exact_then_unique_casefold(self) -> None:
        tags = {
            "list": [
                {"id": 3, "name": "Beach"},
                {"id": 4, "name": "2024"},
                {"id": 5, "name": "x"},
                {"id": 6, "name": "X"},
            ]
        }
        install(FakePhotos().on("SYNO.Foto.Browse.GeneralTag", "list", 1, tags))
        client = make_client()
        async with client:
            ids = await Photos(client, "personal", "Op").tag_ids(["beach", "2024", "X", "x2"])
        assert ids == {"beach": 3, "2024": 4, "X": 6, "x2": None}

    @respx.mock
    async def test_tag_ids_ambiguous_casefold_refused_with_candidates(self) -> None:
        tags = {"list": [{"id": 3, "name": "Beach"}, {"id": 7, "name": "beach"}]}
        install(FakePhotos().on("SYNO.Foto.Browse.GeneralTag", "list", 1, tags))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await Photos(client, "personal", "Op").tag_ids(["BEACH"])
        err = _error(exc)
        assert err["code"] == "invalid_parameter"
        assert err["param"] == "tag"
        assert "3 (Beach)" in str(err["message"]) and "7 (beach)" in str(err["message"])


class TestPlaceNode:
    @respx.mock
    async def test_unknown_place(self) -> None:
        from mcp_synology.modules.photos.helpers import Photos

        install(FakePhotos().on("SYNO.Foto.Browse.Geocoding", "list", 1, {"list": []}))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await Photos(client, "personal", "Op").place_node(999, param="condition")
        assert _error(exc)["code"] == "not_found"

    @respx.mock
    async def test_place_missing_from_the_filter_tree(self) -> None:
        from mcp_synology.modules.photos.helpers import Photos

        place = {"id": 1, "name": "Nowhere", "country_id": 77, "first_level": "Nowhere"}
        fake = FakePhotos().on("SYNO.Foto.Browse.Geocoding", "list", 1, {"list": [place]})
        fake.on("SYNO.Foto.Search.Filter", "list", 2, {"geocoding": []})
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await Photos(client, "personal", "Op").place_node(1, param="condition")
        assert _error(exc)["code"] == "invalid_parameter"
        assert "can't be used in a smart album condition" in str(_error(exc)["message"])

    def test_missing_note_empty(self) -> None:
        from mcp_synology.modules.photos.helpers import Photos

        assert Photos(make_client(), "personal", "Op").missing_note([]) == ""


@pytest.mark.parametrize(
    ("ids", "shown"),
    [([5], "5"), ([1, 2, 3], "1-3"), ([1, 2, 3, 7, 9, 10], "1-3, 7, 9-10"), ([4, 2], "4, 2")],
)
def test_id_list_collapses_runs(ids: list[int], shown: str) -> None:
    assert id_list(ids) == shown
