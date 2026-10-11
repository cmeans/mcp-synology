"""Tests for core/client.py — DSM API client."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import respx

if TYPE_CHECKING:
    from pathlib import Path

from mcp_synology.core.client import DsmClient
from mcp_synology.core.errors import (
    ApiNotFoundError,
    PathNotFoundError,
    SessionExpiredError,
    SynologyError,
)
from tests.conftest import BASE_URL, make_api_cache, make_client, make_minimal_api_cache

# Local aliases so the existing test bodies don't need a wholesale rename.
_make_client = make_client
_default_cache = make_minimal_api_cache


class TestQueryApiInfo:
    @respx.mock
    async def test_query_api_info_success(self) -> None:
        respx.get(f"{BASE_URL}/webapi/query.cgi").respond(
            json={
                "success": True,
                "data": {
                    "SYNO.API.Auth": {
                        "path": "entry.cgi",
                        "minVersion": 1,
                        "maxVersion": 7,
                    },
                    "SYNO.FileStation.List": {
                        "path": "entry.cgi",
                        "minVersion": 1,
                        "maxVersion": 2,
                    },
                },
            }
        )
        async with DsmClient(base_url=BASE_URL) as client:
            cache = await client.query_api_info()
        assert "SYNO.API.Auth" in cache
        assert cache["SYNO.API.Auth"].max_version == 7
        assert "SYNO.FileStation.List" in cache

    @respx.mock
    async def test_query_api_info_error(self) -> None:
        respx.get(f"{BASE_URL}/webapi/query.cgi").respond(
            json={"success": False, "error": {"code": 102}}
        )
        async with DsmClient(base_url=BASE_URL) as client:
            with pytest.raises(ApiNotFoundError):
                await client.query_api_info()


class TestNegotiateVersion:
    def test_negotiate_picks_highest_compatible(self) -> None:
        client = _make_client(_default_cache())
        version = client.negotiate_version("SYNO.API.Auth", min_version=3, max_version=6)
        assert version == 6

    def test_negotiate_nas_lower_than_requested(self) -> None:
        client = _make_client(_default_cache())
        version = client.negotiate_version("SYNO.FileStation.List", min_version=1, max_version=5)
        assert version == 2  # NAS max is 2

    def test_negotiate_api_not_found(self) -> None:
        client = _make_client(_default_cache())
        with pytest.raises(ApiNotFoundError):
            client.negotiate_version("SYNO.NonExistent.API")

    def test_negotiate_no_compatible_version(self) -> None:
        client = _make_client(_default_cache())
        with pytest.raises(ApiNotFoundError, match="no compatible"):
            client.negotiate_version("SYNO.FileStation.List", min_version=5)


class TestRequest:
    @respx.mock
    async def test_request_success(self) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": True,
                "data": {"shares": [{"name": "video"}]},
            }
        )
        async with _make_client(_default_cache()) as client:
            data = await client.request("SYNO.FileStation.List", "list_share", version=2)
        assert data["shares"][0]["name"] == "video"

    @respx.mock
    async def test_request_injects_session_id(self) -> None:
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {}}
        )
        async with _make_client(_default_cache()) as client:
            client.sid = "test-session-id"
            await client.request("SYNO.FileStation.List", "list_share", version=2)
        assert route.calls[0].request.url.params["_sid"] == "test-session-id"

    @respx.mock
    async def test_request_error_maps_to_exception(self) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 408}}
        )
        async with _make_client(_default_cache()) as client:
            with pytest.raises(PathNotFoundError):
                await client.request("SYNO.FileStation.List", "getinfo", version=2)

    @respx.mock
    async def test_request_api_not_in_cache(self) -> None:
        async with _make_client(_default_cache()) as client:
            with pytest.raises(ApiNotFoundError):
                await client.request("SYNO.NonExistent", "method")

    @respx.mock
    async def test_request_session_error_triggers_reauth(self) -> None:
        call_count = 0

        def side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(200, json={"success": False, "error": {"code": 106}})
            return httpx.Response(200, json={"success": True, "data": {"result": "ok"}})

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)

        reauth_called = False

        async def mock_reauth() -> None:
            nonlocal reauth_called
            reauth_called = True

        async with _make_client(_default_cache()) as client:
            client.set_re_auth_callback(mock_reauth)
            data = await client.request("SYNO.FileStation.List", "list_share", version=2)

        assert reauth_called
        assert data["result"] == "ok"
        assert call_count == 2

    @respx.mock
    async def test_request_session_error_no_callback(self) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 106}}
        )
        async with _make_client(_default_cache()) as client:
            with pytest.raises(SessionExpiredError):
                await client.request("SYNO.FileStation.List", "list_share", version=2)

    @respx.mock
    async def test_request_no_retry_on_105(self) -> None:
        """Error 105 (permission denied) should NOT trigger re-auth."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 105}}
        )
        reauth_called = False

        async def mock_reauth() -> None:
            nonlocal reauth_called
            reauth_called = True

        async with _make_client(_default_cache()) as client:
            client.set_re_auth_callback(mock_reauth)
            with pytest.raises(SynologyError):
                await client.request("SYNO.FileStation.List", "list_share", version=2)
        assert not reauth_called


class TestEscapePathParam:
    def test_single_path(self) -> None:
        assert DsmClient.escape_path_param(["/video/test"]) == "/video/test"

    def test_multiple_paths(self) -> None:
        result = DsmClient.escape_path_param(["/video/a", "/music/b"])
        assert result == "/video/a,/music/b"

    def test_comma_in_path(self) -> None:
        result = DsmClient.escape_path_param(["/video/file,name.mkv"])
        assert result == "/video/file\\,name.mkv"

    def test_backslash_in_path(self) -> None:
        result = DsmClient.escape_path_param(["/video/path\\file"])
        assert result == "/video/path\\\\file"


class TestRequestHttp414:
    """#123 bug 14: DSM answers an over-long GET URL with HTTP 414."""

    @respx.mock
    async def test_get_414_raises_structured_request_too_long(self) -> None:
        from mcp_synology.core.errors import ErrorCode, RequestTooLongError

        client = make_client(make_api_cache())
        respx.get(f"{BASE_URL}/webapi/DownloadStation/task.cgi").respond(status_code=414)
        async with client:
            with pytest.raises(RequestTooLongError) as exc:
                await client.request("SYNO.DownloadStation.Task", "list", version=1)
        assert exc.value.error_code == ErrorCode.INVALID_PARAMETER
        assert exc.value.code is None  # HTTP status, not a DSM error code
        assert "414" in str(exc.value)


class TestRequestFormPost:
    """Form-POST variant of request() — the scoped exception to the GET-only
    rule, used only for DS URI Task.create (#123 item 9a)."""

    @respx.mock
    async def test_params_in_body_sid_in_query(self) -> None:
        client = make_client(make_api_cache())
        client.sid = "the-sid"
        route = respx.post(f"{BASE_URL}/webapi/DownloadStation/task.cgi").respond(
            json={"success": True}
        )
        async with client:
            data = await client.request_form_post(
                "SYNO.DownloadStation.Task",
                "create",
                version=1,
                params={"uri": "magnet:?xt=a", "password": "s3cret"},
            )
        assert data == {}
        request = route.calls.last.request
        assert dict(request.url.params) == {"_sid": "the-sid"}
        body = request.content.decode()
        assert "api=SYNO.DownloadStation.Task" in body
        assert "method=create" in body
        assert "version=1" in body
        assert "s3cret" in body
        assert "s3cret" not in str(request.url)

    @respx.mock
    async def test_password_and_sid_masked_in_debug_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        client = make_client(make_api_cache())
        client.sid = "the-sid"
        respx.post(f"{BASE_URL}/webapi/DownloadStation/task.cgi").respond(json={"success": True})
        caplog.set_level(logging.DEBUG, logger="mcp_synology.core.client")
        async with client:
            await client.request_form_post(
                "SYNO.DownloadStation.Task", "create", version=1, params={"password": "s3cret"}
            )
        assert "s3cret" not in caplog.text
        assert "the-sid" not in caplog.text

    @respx.mock
    async def test_dsm_error_raises_typed_exception(self) -> None:
        client = make_client(make_api_cache())
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 102}}
        )
        async with client:
            with pytest.raises(ApiNotFoundError):
                await client.request_form_post("SYNO.DownloadStation2.Task", "create", version=2)

    @respx.mock
    async def test_session_error_reauths_once_and_retries(self) -> None:
        client = make_client(make_api_cache())
        reauths = 0

        async def fake_reauth() -> None:
            nonlocal reauths
            reauths += 1
            client.sid = "new-sid"

        client.set_re_auth_callback(fake_reauth)
        route = respx.post(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=[
                httpx.Response(200, json={"success": False, "error": {"code": 119}}),
                httpx.Response(200, json={"success": True, "data": {"task_id": ["dbid_1"]}}),
            ]
        )
        async with client:
            data = await client.request_form_post("SYNO.DownloadStation2.Task", "create", version=2)
        assert data == {"task_id": ["dbid_1"]}
        assert reauths == 1
        assert dict(route.calls.last.request.url.params) == {"_sid": "new-sid"}

    @respx.mock
    async def test_permission_denied_never_reauths(self) -> None:
        """CLAUDE.md invariant: 105 is not a session error."""
        from mcp_synology.core.errors import SynologyPermissionError

        client = make_client(make_api_cache())
        reauths = 0

        async def fake_reauth() -> None:
            nonlocal reauths
            reauths += 1

        client.set_re_auth_callback(fake_reauth)
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 105}}
        )
        async with client:
            with pytest.raises(SynologyPermissionError):
                await client.request_form_post("SYNO.DownloadStation2.Task", "create", version=2)
        assert reauths == 0

    @respx.mock
    async def test_http_5xx_raises_structured_error(self) -> None:
        from mcp_synology.core.errors import ErrorCode

        client = make_client(make_api_cache())
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(status_code=502)
        async with client:
            with pytest.raises(SynologyError) as exc:
                await client.request_form_post("SYNO.DownloadStation2.Task", "create", version=2)
        assert exc.value.error_code == ErrorCode.UNAVAILABLE
        assert "502" in str(exc.value)

    @respx.mock
    async def test_timeout_raises_structured_error(self) -> None:
        from mcp_synology.core.errors import ErrorCode

        client = make_client(make_api_cache())
        respx.post(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=httpx.ReadTimeout("slow"))
        async with client:
            with pytest.raises(SynologyError) as exc:
                await client.request_form_post("SYNO.DownloadStation2.Task", "create", version=2)
        assert exc.value.error_code == ErrorCode.TIMEOUT

    @respx.mock
    async def test_post_414_raises_structured_request_too_long(self) -> None:
        from mcp_synology.core.errors import RequestTooLongError

        client = make_client(make_api_cache())
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(status_code=414)
        async with client:
            with pytest.raises(RequestTooLongError):
                await client.request_form_post("SYNO.DownloadStation2.Task", "create", version=2)

    @respx.mock
    async def test_failed_reauth_raises_original_session_error(self) -> None:
        client = make_client(make_api_cache())

        async def failing_reauth() -> None:
            raise SynologyError("login failed")

        client.set_re_auth_callback(failing_reauth)
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 106}}
        )
        async with client:
            with pytest.raises(SessionExpiredError):
                await client.request_form_post("SYNO.DownloadStation2.Task", "create", version=2)

    async def test_unknown_api_raises_api_not_found(self) -> None:
        client = make_client(make_api_cache())
        async with client:
            with pytest.raises(ApiNotFoundError):
                await client.request_form_post("SYNO.Nope", "create", version=1)


class TestCreateDownloadTaskWithFile:
    """Multipart torrent upload via SYNO.DownloadStation2.Task.create (#123 bug 6:
    the v1 multipart path returns 101 on DSM 7.2.2 in every variant)."""

    @respx.mock
    async def test_ds2_multipart_shape(self, tmp_path: Path) -> None:
        client = make_client(make_api_cache())
        client.sid = "the-sid"
        torrent = tmp_path / "ubuntu.torrent"
        torrent.write_bytes(b"d4:infod6:lengthi100eee")
        route = respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"list_id": [], "task_id": ["dbid_1"]}}
        )
        async with client:
            data = await client.create_download_task_with_file(
                file_path=torrent, filename="ubuntu.torrent", destination="writable"
            )
        assert data == {"list_id": [], "task_id": ["dbid_1"]}
        request = route.calls.last.request
        assert dict(request.url.params) == {"_sid": "the-sid"}
        body = request.content
        for name, value in [
            ("api", b"SYNO.DownloadStation2.Task"),
            ("version", b"2"),
            ("method", b"create"),
            ("type", b'"file"'),
            ("file", b'["torrent"]'),
            ("destination", b'"writable"'),
            ("create_list", b"false"),
            ("size", str(torrent.stat().st_size).encode()),
        ]:
            assert f'name="{name}"\r\n\r\n'.encode() + value in body, name
        assert b'name="torrent"; filename="ubuntu.torrent"' in body
        # DSM reads multipart fields in order; the file part must be last.
        assert body.rindex(b'name="torrent"') > body.rindex(b'name="size"')

    @respx.mock
    async def test_destination_omitted_when_not_given(self, tmp_path: Path) -> None:
        client = make_client(make_api_cache())
        torrent = tmp_path / "x.torrent"
        torrent.write_bytes(b"x")
        route = respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"list_id": [], "task_id": ["dbid_2"]}}
        )
        async with client:
            await client.create_download_task_with_file(file_path=torrent, filename="x.torrent")
        assert b'name="destination"' not in route.calls.last.request.content

    async def test_ds2_task_absent_raises_api_not_found(self, tmp_path: Path) -> None:
        cache = make_api_cache()
        del cache["SYNO.DownloadStation2.Task"]
        client = make_client(cache)
        torrent = tmp_path / "x.torrent"
        torrent.write_bytes(b"x")
        async with client:
            with pytest.raises(ApiNotFoundError):
                await client.create_download_task_with_file(file_path=torrent, filename="x.torrent")

    @respx.mock
    async def test_dsm_error_raises_typed_exception(self, tmp_path: Path) -> None:
        from mcp_synology.core.downloadstation_errors import DownloadStationError

        client = make_client(make_api_cache())
        torrent = tmp_path / "bad.torrent"
        torrent.write_bytes(b"not a torrent")
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 400}}
        )
        async with client:
            with pytest.raises(DownloadStationError, match="upload"):
                await client.create_download_task_with_file(
                    file_path=torrent, filename="bad.torrent"
                )

    @respx.mock
    async def test_session_error_triggers_one_reauth_retry(self, tmp_path: Path) -> None:
        client = make_client(make_api_cache())
        torrent = tmp_path / "fine.torrent"
        torrent.write_bytes(b"d4:infod6:lengthi100eee")
        reauths = 0

        async def fake_reauth() -> None:
            nonlocal reauths
            reauths += 1
            client.sid = "new_sid"

        client.set_re_auth_callback(fake_reauth)
        respx.post(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=[
                httpx.Response(200, json={"success": False, "error": {"code": 106}}),
                httpx.Response(200, json={"success": True, "data": {"task_id": ["dbid_3"]}}),
            ]
        )
        async with client:
            data = await client.create_download_task_with_file(
                file_path=torrent, filename="fine.torrent"
            )
        assert data == {"task_id": ["dbid_3"]}
        assert reauths == 1
