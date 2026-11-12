"""Photos plumbing: spaces, JSON-encoded POSTs, lookups by ID, path or name (#258)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import ApiNotFoundError, DsmHttpError, SynologyError
from mcp_synology.modules.filestation.helpers import shown
from mcp_synology.modules.photos.helpers import (
    QUOTES_HINT,
    Photos,
    Ref,
    check_folder_path,
    check_space,
    encode,
    id_list,
    in_reply_quotes,
    is_indexing,
    one_ref,
    page_footer,
    parse_date,
    parse_expires,
    photo_time,
    prefix,
    quoted_name,
    quotes_note,
    refs,
    shown_folder,
    taken,
    with_quotes_hint,
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

    def test_photo_time_has_no_zone(self) -> None:
        """Photos' ``time`` is wall-clock time encoded as if UTC: a file with mtime
        16:09:03 UTC on a NAS in CDT reads back as 11:09:03 (seska, rc4)."""
        assert photo_time(1792494543) == "2026-10-20 11:09:03"
        assert taken({"time": 1792494543}) == "2026-10-20 11:09:03"
        assert taken({}) == "—"

    def test_parse_date(self) -> None:
        assert parse_date("Op", "d", "2024-07-04") == 1720051200
        assert parse_date("Op", "d", "2024-07-04", end=True) == 1720051200 + 86399
        with pytest.raises(ToolError) as exc:
            parse_date("Op", "start_date", "07/04/2024")
        assert _error(exc)["param"] == "start_date"

    def test_parse_expires(self) -> None:
        """A real UTC instant: a date is the end of that day UTC; a date-time keeps its offset."""
        assert parse_expires("Op", "never") == 0
        assert parse_expires("Op", "2030-03-17") == 1900022399  # 23:59:59 UTC
        assert parse_expires("Op", "2030-03-17T23:59:59Z") == 1900022399
        assert parse_expires("Op", "2030-03-17T23:59:59-04:00") == 1900022399 + 4 * 3600
        assert parse_expires("Op", " 2030-03-18T05:29:59+05:30 ") == 1900022399
        assert parse_expires("Op", "2030-03-17 23:59:59+0530") == 1900002599
        for bad, problem in (
            ("2030-03-17T23:59:59", "has no offset"),
            ("20300317", "has no offset"),  # a basic-format date is a naive date-time
            ("soon", "isn't a date or date-time"),
            ("2030", "isn't a date or date-time"),
            ("2030-0317x", "isn't a date or date-time"),
            ("2030-02-30", "isn't a date or date-time"),
            ("1970-01-01T00:00:00Z", "isn't after 1970-01-01"),  # 0 would mean never
            ("1960-01-01", "isn't after 1970-01-01"),
        ):
            with pytest.raises(ToolError) as exc:
                parse_expires("Op", bad)
            err = _error(exc)
            assert err["param"] == "expires" and problem in err["message"], err
            assert "2026-10-25T23:59:59-04:00" in err["message"]

    def test_page_footer(self) -> None:
        assert (
            page_footer(7, 0, 5) == "7 item(s) total; showing 1–5. Pass offset=5 for the next page."
        )
        assert page_footer(7, 5, 2) == "7 item(s) total; showing 6–7."
        assert page_footer(7, 10, 0) == "7 item(s) total."

    def test_page_footer_separates_thousands(self) -> None:
        assert page_footer(12_345, 1_000, 200) == (
            "12,345 item(s) total; showing 1,001–1,200. Pass offset=1200 for the next page."
        )
        assert page_footer(12_345, 20_000, 0) == "12,345 item(s) total."
        assert page_footer(None, 1_000, 5) == (
            "Showing 1,001–1,005. Pass offset=1005 for the next page."
        )

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
            folder = await Photos(client, "shared", "Op").folder(None)
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
            (Ref("folder_id", id=10), {"id": 10}),
            (None, {"id": 0}),
            # A path of digits is that path, never folder ID 10 (#617: `2024`, meant as
            # /2024, was folder ID 2024).
            ("10", {"name": "/10"}),
            (" 10 ", {"name": "/ 10 "}),
            ("/", {"id": 0}),
            ("mcp-spec-capture/", {"name": "/mcp-spec-capture"}),
            ("  /", {"id": 0}),
            # Taken exactly (#510): Photos reaches ` foo` beside `foo` (vdsm, DSM 7.1,
            # 7.2.2, 7.3.2 and 7.4.1, both spaces); stripping looked up `foo`.
            ("/x/ foo", {"name": "/x/ foo"}),
            ("/x/foo ", {"name": "/x/foo "}),
            ("/x/ ", {"name": "/x/ "}),
            ("/x/a\\b", {"name": "/x/a\\b"}),
            # Only whitespace before a leading / is padding, as in File Station.
            ("  /x/", {"name": "/x"}),
            (" x", {"name": "/ x"}),
        ],
    )
    @respx.mock
    async def test_id_root_or_path(self, value: Ref | str | None, sent: dict[str, object]) -> None:
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, captured("folder-get-by-name"))
        install(fake)
        client = make_client()
        ref = Ref("folder_path", name=value) if isinstance(value, str) else value
        async with client:
            await Photos(client, "personal", "Op").folder(ref)
        assert fake.one("SYNO.Foto.Browse.Folder", "get").params == sent

    @pytest.mark.parametrize(
        ("value", "what"),
        [
            # DSM cuts the path at a NUL: `/x/foo<NUL>y` resolved to `/x/foo`.
            ("/x/foo\x00y", "NUL"),
            ("/x//foo", "empty part"),
            ("/x/../y", "'.' and '..' parts"),
            ("/x/ . /y", "'.' and '..' parts"),
        ],
    )
    @respx.mock
    async def test_nul_empty_and_dot_parts_refused_before_any_request(
        self, value: str, what: str
    ) -> None:
        fake = FakePhotos()  # no routes: a request would fail the test
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await Photos(client, "personal", "Op").folder(Ref("destination_path", name=value))
        assert not fake.calls
        err = _error(exc)
        assert (err["code"], err["param"], err["value"]) == (
            "invalid_parameter",
            "destination_path",
            value,
        )
        assert what in err["message"]

    @pytest.mark.parametrize(
        ("param", "what"),
        [
            ("destination_path", "destination_path"),
            ("folder_paths", "an entry in folder_paths"),
            ("condition", "an entry in the condition's folder_paths"),
        ],
    )
    def test_an_empty_path_refusal_names_what_was_empty(self, param: str, what: str) -> None:
        """Pre-QA r6/r7 on #615: a list's or a condition's entry is what's empty."""
        with pytest.raises(ToolError) as exc:
            check_folder_path(" ", param)
        assert _error(exc)["message"] == (
            f"{what} is empty: pass a folder path (the space's root is written `/`)."
        )

    @pytest.mark.parametrize("value", ["", " ", "\t"])
    @pytest.mark.parametrize("param", ["folder_path", "parent_path", "destination_path"])
    @respx.mock
    async def test_an_empty_path_is_refused_not_taken_as_the_root(
        self, param: str, value: str
    ) -> None:
        """QA round 1 on #615: `""` was the root, so a client filling an unset argument with
        "" got past the required folders and wrote there. The root is `/`."""
        fake = FakePhotos()  # no routes: a request would fail the test
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await Photos(client, "personal", "Op").folder(Ref(param, name=value))
        err = _error(exc)
        assert (err["code"], err["param"]) == ("invalid_parameter", param)
        assert "the space's root is written `/`" in err["message"]
        assert not fake.calls

    @respx.mock
    async def test_missing_folder_is_not_found(self) -> None:
        install(FakePhotos().error("SYNO.Foto.Browse.Folder", "get", 2, 642))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await Photos(client, "personal", "Op").folder(Ref("parent_path", name="/nope"))
        err = _error(exc)
        assert err["code"] == "not_found"
        assert err["param"] == "parent_path"

    @pytest.mark.parametrize("kind", ["album", "person"])
    @respx.mock
    async def test_no_dsm_answer_on_the_id_lookup_is_not_not_found(self, kind: str) -> None:
        """Pre-QA r3 on #611: a proxy's 502 on the ID get is that error, not "no album 43",
        and no name lookup follows."""
        api = {"album": "SYNO.Foto.Browse.Album", "person": "SYNO.Foto.Browse.Person"}[kind]
        fake = FakePhotos().on(api, "get", 1, lambda _p: httpx.Response(502, text="Bad Gateway"))
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(DsmHttpError):
                await getattr(Photos(client, "personal", "Op"), kind)(Ref(f"{kind}_id", id=43))
        assert not fake.of(api, "list")

    @respx.mock
    async def test_a_negative_number_is_no_folder_path(self) -> None:
        """#609 pre-QA: -5 sent as a number is a folder ID only, not the path /-5, and
        nothing is asked of DSM."""
        fake = FakePhotos().on("SYNO.Foto.Browse.Folder", "get", 2, {"folder": {"id": 77}})
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await Photos(client, "personal", "Op").folder(Ref("folder_id", id=-5))
        err = _error(exc)
        assert err["code"] == "not_found", err
        assert "no folder -5" in err["message"], err
        assert not fake.calls, fake.calls

    @respx.mock
    async def test_other_errors_pass_through(self) -> None:
        install(FakePhotos().error("SYNO.FotoTeam.Browse.Folder", "get", 2, 801))
        client = make_client()
        async with client:
            with pytest.raises(SynologyError) as exc:
                await Photos(client, "shared", "Op").folder(None)
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
                await Photos(client, "personal", "Op").folder(Ref("folder_path", name="²"))
        assert _error(exc)["code"] == "not_found"
        assert fake.one("SYNO.Foto.Browse.Folder", "get").params == {"name": "/²"}


class TestNames:
    """Albums and people come by ID or by name, each from its own parameter (#614, #616): an ID
    is that ID only, and a name (digits too) that name only."""

    @respx.mock
    async def test_person_by_id(self) -> None:
        fake = FakePhotos().on("SYNO.Foto.Browse.Person", "get", 1, captured("person-get", PEOPLE))
        install(fake)
        client = make_client()
        async with client:
            person = await Photos(client, "personal", "Op").person(Ref("person_id", id=12))
        assert person["name"] == "Person 1"
        assert not fake.of("SYNO.Foto.Browse.Person", "list")

    @pytest.mark.parametrize("kind", ["album", "person"])
    @respx.mock
    async def test_a_missing_id_is_not_found_even_when_a_name_matches(self, kind: str) -> None:
        """#614 (0.11.0rc3 live test): "the album whose ID is 9876543" deleted the album named
        9876543. An ID that doesn't exist is not found; names aren't looked at."""
        api, version = {
            "album": ("SYNO.Foto.Browse.Album", 2),
            "person": ("SYNO.Foto.Browse.Person", 1),
        }[kind]
        fake = (
            FakePhotos()
            .on(api, "get", 1, {"list": []})
            .on(api, "list", version, {"list": [{"id": 49, "name": "9876543"}]})
        )
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await getattr(Photos(client, "personal", "Op"), kind)(Ref(f"{kind}_id", id=9876543))
        err = _error(exc)
        assert (err["code"], err["param"], err["value"]) == (
            "not_found",
            f"{kind}_id",
            9876543,
        )
        assert f"no {kind} 9876543." in err["message"]
        assert not fake.of(api, "list")

    @pytest.mark.parametrize("kind", ["album", "person"])
    @respx.mock
    async def test_a_name_of_digits_is_that_name_even_when_an_id_matches(self, kind: str) -> None:
        """#616: "the album named 43" deleted album 43, another album. A name is matched
        against names only; no ID lookup is made."""
        api, version = {
            "album": ("SYNO.Foto.Browse.Album", 2),
            "person": ("SYNO.Foto.Browse.Person", 1),
        }[kind]
        names = {"list": [{"id": 43, "name": "Holiday"}, {"id": 7, "name": "43"}]}
        fake = FakePhotos().on(api, "list", version, names)
        install(fake)
        client = make_client()
        async with client:
            found = await getattr(Photos(client, "personal", "Op"), kind)(
                Ref(f"{kind}_name", name="43")
            )
        assert found["id"] == 7
        assert not fake.of(api, "get")

    @respx.mock
    async def test_other_id_lookup_errors_are_not_found_but_setup_errors_raise(self) -> None:
        fake = FakePhotos().error("SYNO.Foto.Browse.Album", "get", 1, 609)
        install(fake)
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await Photos(client, "personal", "Op").album(Ref("album_id", id=9))
        assert _error(exc)["code"] == "not_found"
        assert not fake.of("SYNO.Foto.Browse.Album", "list")
        respx.clear()
        install(FakePhotos().error("SYNO.Foto.Browse.Album", "get", 1, 160))
        async with client:
            with pytest.raises(SynologyError) as info:
                await Photos(client, "personal", "Op").album(Ref("album_id", id=9))
        assert info.value.code == 160

    @respx.mock
    async def test_ambiguous_name_lists_ids(self) -> None:
        people = {"list": [{"id": 1, "name": "Jo"}, {"id": 2, "name": "jo"}]}
        install(FakePhotos().on("SYNO.Foto.Browse.Person", "list", 1, people))
        client = make_client()
        async with client:
            with pytest.raises(ToolError) as exc:
                await Photos(client, "personal", "Op").person(Ref("person_name", name="JO"))
        err = _error(exc)
        assert (err["code"], err["param"]) == ("invalid_parameter", "person_name")
        assert '1 "Jo"' in str(err["message"]) and '2 "jo"' in str(err["message"])
        assert err["suggestion"] == "Ask the user which one they mean, then pass its person_id."

    @respx.mock
    async def test_a_name_is_matched_without_its_padding(self) -> None:
        """As before the split: "Trip " finds the album named Trip."""
        install(
            FakePhotos().on(
                "SYNO.Foto.Browse.Album", "list", 2, {"list": [{"id": 5, "name": "Trip"}]}
            )
        )
        client = make_client()
        async with client:
            found = await Photos(client, "personal", "Op").album(Ref("album_name", name="Trip "))
        assert found["id"] == 5

    @respx.mock
    async def test_person_walk_includes_hidden(self) -> None:
        fake = FakePhotos().on(
            "SYNO.Foto.Browse.Person", "list", 1, captured("person-list", PEOPLE)
        )
        install(fake)
        client = make_client()
        async with client:
            person = await Photos(client, "personal", "Op").person(
                Ref("person_name", name="person 3")
            )
        assert person["id"] == 19
        params = fake.one("SYNO.Foto.Browse.Person", "list").params
        assert params["show_hidden"] is True and params["show_more"] is True


class TestOneRef:
    @pytest.mark.parametrize(
        ("param", "id_param"),
        [
            ("album_name", "album_id"),
            ("target_name", "target_id"),
            ("merge_names", "merge_ids"),
            ("album_names", "album_ids"),
            ("condition", "person_ids"),
        ],
    )
    def test_an_ambiguous_name_points_at_its_own_id_parameter(
        self, param: str, id_param: str
    ) -> None:
        """Pre-QA r3 on #615: merge_people has no person_id; its names are merge_names."""
        assert Ref(param, name="Jo").id_param == id_param

    """`album_id` or `album_name` (and the other pairs): one, never both (#614, #616)."""

    def test_an_id_or_a_name(self) -> None:
        assert one_ref("Op", "album", 4, None) == Ref("album_id", id=4)
        assert one_ref("Op", "album", None, "43") == Ref("album_name", name="43")
        assert one_ref("Op", "person", None, None, required=False) is None
        assert one_ref("Op", "person", 5, None, prefix="target") == Ref("target_id", id=5)

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            ((4, "Trip"), "pass album_id or album_name, not both."),
            ((None, None), "pass album_id or album_name."),
        ],
    )
    def test_both_or_neither_is_refused(
        self, args: tuple[int | None, str | None], message: str
    ) -> None:
        with pytest.raises(ToolError) as exc:
            one_ref("Op", "album", *args)
        err = _error(exc)
        assert (err["code"], err["param"]) == ("invalid_parameter", "album_id")
        assert err["message"] == f"Op failed: {message}"

    def test_lists_take_both_kinds_ids_first(self) -> None:
        assert refs("Op", [4], ["43"], prefix="album") == [
            Ref("album_ids", id=4),
            Ref("album_names", name="43"),
        ]
        with pytest.raises(ToolError) as exc:
            refs("Op", [], None, prefix="merge")
        assert _error(exc)["message"] == "Op failed: pass merge_ids or merge_names."

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
        assert '3 "Beach"' in str(err["message"]) and '7 "beach"' in str(err["message"])


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
        assert 'place 1 "Nowhere" can' in str(_error(exc)["message"])  # quoted (#700)

    def test_missing_note_empty(self) -> None:
        from mcp_synology.modules.photos.helpers import Photos

        assert Photos(make_client(), "personal", "Op").missing_note([]) == ""


@pytest.mark.parametrize(
    ("ids", "shown"),
    [([5], "5"), ([1, 2, 3], "1-3"), ([1, 2, 3, 7, 9, 10], "1-3, 7, 9-10"), ([4, 2], "4, 2")],
)
def test_id_list_collapses_runs(ids: list[int], shown: str) -> None:
    assert id_list(ids) == shown


def test_id_limit_refusals_name_what_and_separate_thousands() -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    from mcp_synology.modules.photos.helpers import check_ids

    for param, noun in (("ids", "ids"), ("folder_paths", "folders"), ("folder_ids", "folders")):
        try:
            check_ids("Copy photos", list(range(1_001)), param=param)
        except ToolError as e:
            assert f"at most 1,000 {noun} per call." in str(e)
        else:
            raise AssertionError("1,001 ids accepted")


class TestQuotedNamesAndFolders:
    """#700: a name or folder set in a line of text can't read as the line's structure."""

    def test_names_are_json_quoted_and_a_missing_one_is_the_placeholder(self) -> None:
        assert quoted_name("Trip 2024") == '"Trip 2024"'
        assert quoted_name('Trip" (7)') == '"Trip\\" (7)"'
        assert quoted_name("a, b") == '"a, b"'
        assert quoted_name("Ann\u200b") == '"Ann\\u200b"'
        assert quoted_name("") == "(unnamed)"
        assert quoted_name(None, "(no subject)") == "(no subject)"
        assert quoted_name(0) == '"0"'  # not text, but a name, not missing (QA r1)
        assert quoted_name(False) == '"False"'

    def test_a_folder_is_quoted_only_when_it_holds_what_follows_it(self) -> None:
        assert shown_folder("/Trips/2024") == "/Trips/2024"
        assert shown_folder("/Trips (Shared Space)") == "'/Trips (Shared Space)'"
        assert shown_folder("/a: b") == "'/a: b'"
        assert shown_folder("/Trips:") == "'/Trips:'"  # ": (" with what follows (pre-QA r2)
        assert shown_folder("/a:b") == "/a:b"
        assert shown_folder("/a matching b") == "/a matching b"
        assert shown_folder("/a matching b", " matching ") == "'/a matching b'"
        assert shown_folder("/a, b") == "'/a, b'"  # shown()'s own rule (#685)
        assert shown_folder("/pad \t (x)") == shown("/pad \t (x)")  # escapes kept
        assert shown_folder(None) == "(unnamed)"  # a reply with no name (QA r1)
        assert shown_folder("") == ""


class TestNamesPassedBackInQuotes:
    """QA r1 on #709: only a name wholly in double quotes, as `quoted_name` writes one."""

    def test_in_reply_quotes(self) -> None:
        assert in_reply_quotes('"beach"')
        assert in_reply_quotes('""')
        for name in ('"', "beach", "'beach'", '"beach', 'beach"', 'a "b" c', None, 0):
            assert not in_reply_quotes(name), name

    def test_hint_and_note(self) -> None:
        assert with_quotes_hint("Try X.", '"a"') == f"Try X. {QUOTES_HINT}"
        assert with_quotes_hint(None, '"a"') == QUOTES_HINT
        assert with_quotes_hint("Try X.", "a") == "Try X."
        assert with_quotes_hint(None, "a") is None
        assert quotes_note("a", "tag") == []
        assert quotes_note('"a"', "tag", "fix it") == [
            'Note: the tag "\\"a\\"" starts and ends with a quote mark, which is now part '
            "of it. If it came from a reply in quotes, fix it."
        ]
