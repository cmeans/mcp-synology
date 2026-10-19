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
    DsmHttpError,
    PathNotFoundError,
    SessionExpiredError,
    SynologyError,
)
from mcp_synology.core.state import ApiInfoEntry
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


class TestStaleConnection:
    """#222: DSM reloads nginx when its web configuration changes (twice in the 13 s
    after Download Station starts on DSM 7.1), which closes idle keep-alive
    connections; httpx doesn't resend a request sent on one. Reads are resent once on
    a fresh connection; writes go on a connection that's never reused."""

    _OK = httpx.Response(200, json={"success": True, "data": {"shares": []}})

    @pytest.mark.parametrize(
        ("method", "read"),
        [
            ("list", True),
            ("list_share", True),
            ("get", True),
            ("getinfo", True),
            ("getconfig", True),
            ("get_exif", True),
            ("count_with_filter", True),
            ("query", True),
            ("status", True),
            ("me", True),
            ("info", True),
            ("start", False),
            ("delete", False),
            ("create", False),
            ("edit", False),
            ("set", False),
            ("stop", False),
            ("clean", False),
            ("login", False),
            ("logout", False),
            ("add_tag", False),
            ("setserverconfig", False),
        ],
    )
    def test_read_or_write(self, method: str, read: bool) -> None:
        from mcp_synology.core.client import _is_read

        assert _is_read(method) is read

    @respx.mock
    @pytest.mark.parametrize(
        "error", [httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError]
    )
    async def test_read_is_resent_once_on_a_fresh_connection(
        self, error: type[httpx.TransportError]
    ) -> None:
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=[error("Server disconnected without sending a response."), self._OK]
        )
        client = make_client(make_api_cache())
        async with client:
            data = await client.request("SYNO.FileStation.List", "list_share", version=2)
        assert data == {"shares": []}
        assert route.call_count == 2

    @respx.mock
    async def test_read_failing_twice_raises(self) -> None:
        drop = httpx.RemoteProtocolError("Server disconnected without sending a response.")
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=[drop, drop])
        client = make_client(make_api_cache())
        async with client:
            with pytest.raises(httpx.RemoteProtocolError):
                await client.request("SYNO.FileStation.List", "list_share", version=2)
        assert route.call_count == 2

    @respx.mock
    async def test_write_is_never_resent(self) -> None:
        drop = httpx.RemoteProtocolError("Server disconnected without sending a response.")
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=[drop, self._OK])
        client = make_client(make_api_cache())
        async with client:
            with pytest.raises(httpx.RemoteProtocolError):
                await client.request("SYNO.FileStation.Delete", "start", version=2)
        assert route.call_count == 1

    @respx.mock
    async def test_write_goes_on_the_never_reused_client(self) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(return_value=self._OK)
        client = make_client(make_api_cache())
        async with client:
            pooled = client._http
            assert pooled is not None
            once = client._get_http_once()
            pool = once._transport._pool  # type: ignore[attr-defined]
            assert pool._max_keepalive_connections == 0
            used: list[str] = []

            async def spy_get(*args: object, **kwargs: object) -> httpx.Response:
                used.append("pooled")
                raise AssertionError("a write must not use the pooled client")

            pooled.get = spy_get  # type: ignore[method-assign]
            await client.request("SYNO.FileStation.Delete", "start", version=2)
        assert used == []

    @respx.mock
    async def test_resend_uses_the_never_reused_client(self) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(return_value=self._OK)
        client = make_client(make_api_cache())
        async with client:
            pooled = client._http
            assert pooled is not None
            calls: list[str] = []

            async def drop_once(*args: object, **kwargs: object) -> httpx.Response:
                calls.append("pooled")
                if len(calls) > 1:
                    raise AssertionError("the resend must not use the pooled client")
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

            pooled.get = drop_once  # type: ignore[method-assign]
            assert await client.request("SYNO.FileStation.List", "list_share", version=2) == {
                "shares": []
            }
        assert calls == ["pooled"]

    @respx.mock
    async def test_query_api_info_is_resent_once(self) -> None:
        drop = httpx.RemoteProtocolError("Server disconnected without sending a response.")
        info = httpx.Response(
            200,
            json={
                "success": True,
                "data": {"SYNO.API.Auth": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 7}},
            },
        )
        route = respx.get(f"{BASE_URL}/webapi/query.cgi").mock(side_effect=[drop, info])
        client = make_client()
        async with client:
            cache = await client.query_api_info()
        assert "SYNO.API.Auth" in cache
        assert route.call_count == 2

    @pytest.mark.parametrize(
        "call",
        [
            "request_form_post",
            "upload_multipart",
            "create_download_task_with_file",
            "download_binary",
        ],
    )
    def test_send_site_uses_the_never_reused_client(self, call: str) -> None:
        """Uploads, creates and downloads change state or stream, so they never take the
        pooled client: whatever they do, it must come from _get_http_once()."""
        import inspect

        from mcp_synology.core.client import DsmClient

        source = inspect.getsource(getattr(DsmClient, call))
        assert "self._get_http_once()" in source
        assert "self._get_http()" not in source

    async def test_exit_closes_both_clients(self) -> None:
        client = make_client(make_api_cache())
        async with client:
            once = client._get_http_once()
            pooled = client._http
        assert client._http is None and client._http_once is None
        assert once.is_closed and pooled is not None and pooled.is_closed

    @respx.mock
    async def test_both_clients_share_cookies(self) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            return_value=httpx.Response(
                200,
                json={"success": True, "data": {}},
                headers={"set-cookie": "did=device123; Path=/"},
            )
        )
        client = make_client(make_api_cache())
        async with client:
            # A write (fresh connection) sets a cookie; the pooled client sees it.
            await client.request("SYNO.FileStation.Delete", "start", version=2)
            assert client._http is not None
            assert [c.value for c in client._http.cookies.jar if c.name == "did"] == ["device123"]


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
        request = route.calls[0].request
        # The session goes as the id cookie, never as _sid (#176).
        assert "_sid" not in request.url.params
        assert request.headers["cookie"] == "id=test-session-id"

    @respx.mock
    async def test_request_keeps_other_cookies_beside_the_session(self) -> None:
        """An explicit Cookie header replaces httpx's jar, so a reverse proxy's own
        cookie must be sent along with the session (#176)."""
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {}}
        )
        async with _make_client(_default_cache()) as client:
            client._get_http().cookies.set("proxy_auth", "abc")
            client._get_http().cookies.set("id", "stale")  # never sent beside the session
            client.sid = "-4Ms56tk"  # a shape DSM misreads as _sid (begins like a number)
            await client.request("SYNO.FileStation.List", "list_share", version=2)
        assert route.calls[0].request.headers["cookie"] == "id=-4Ms56tk; proxy_auth=abc"

    @respx.mock
    async def test_request_without_session_sends_no_session_cookie(self) -> None:
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {}}
        )
        async with _make_client(_default_cache()) as client:
            await client.request("SYNO.FileStation.List", "list_share", version=2)
        assert "cookie" not in route.calls[0].request.headers

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

    @respx.mock
    async def test_request_read_timeout_overrides_only_read(self) -> None:
        """A per-request read timeout (#245: task stops) leaves connect/write/pool alone."""
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {}}
        )
        async with _make_client(_default_cache()) as client:
            await client.request("SYNO.FileStation.List", "list_share", version=2)
            await client.request("SYNO.FileStation.List", "list_share", version=2, read_timeout=90)
        assert route.calls[0].request.extensions["timeout"]["read"] == 30
        assert route.calls[1].request.extensions["timeout"] == {
            "connect": 30,
            "read": 90,
            "write": 30,
            "pool": 30,
        }

    @respx.mock
    async def test_request_read_timeout_kept_on_reauth_retry(self) -> None:
        replies = iter(
            [
                httpx.Response(200, json={"success": False, "error": {"code": 106}}),
                httpx.Response(200, json={"success": True, "data": {}}),
            ]
        )
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=lambda _: next(replies))

        async def mock_reauth() -> None:
            return None

        async with _make_client(_default_cache()) as client:
            client.set_re_auth_callback(mock_reauth)
            await client.request("SYNO.FileStation.List", "list_share", version=2, read_timeout=90)
        timeouts = [c.request.extensions["timeout"] for c in route.calls]
        assert [t["read"] for t in timeouts] == [90, 90]
        assert [t["connect"] for t in timeouts] == [30, 30]


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


class TestHttpErrorStatus:
    """#232: a non-2xx answer is a DsmHttpError naming the status, API and method.
    httpx's raise_for_status() put the full URL, with passwd or _sid, in the error."""

    @staticmethod
    def _assert_no_url(exc: BaseException, *secrets: str) -> None:
        text = str(exc)
        assert "http://" not in text
        for secret in secrets:
            assert secret not in text
        assert exc.__cause__ is None
        assert exc.__context__ is None

    @respx.mock
    @pytest.mark.parametrize(("status", "retryable"), [(500, True), (502, True), (403, False)])
    async def test_request(self, status: int, retryable: bool) -> None:
        from mcp_synology.core.errors import DsmHttpError, ErrorCode

        client = make_client(make_api_cache())
        client.sid = "SIDSECRET"
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(status_code=status)
        async with client:
            with pytest.raises(DsmHttpError) as exc:
                await client.request("SYNO.FileStation.List", "list_share")
        assert str(exc.value) == f"SYNO.FileStation.List/list_share: DSM answered HTTP {status}."
        assert exc.value.status == status
        assert exc.value.code is None  # HTTP status, not a DSM error code
        assert exc.value.error_code == ErrorCode.UNAVAILABLE
        assert exc.value.retryable is retryable
        assert "mcp-synology check" in (exc.value.suggestion or "")
        self._assert_no_url(exc.value, "SIDSECRET")

    @respx.mock
    async def test_login_does_not_leak_the_password(self) -> None:
        from mcp_synology.core.errors import DsmHttpError

        client = make_client(make_api_cache())
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(status_code=502)
        async with client:
            with pytest.raises(DsmHttpError) as exc:
                await client.request(
                    "SYNO.API.Auth",
                    "login",
                    version=6,
                    params={"account": "chris", "passwd": "S3cretPW", "format": "sid"},
                )
        assert str(exc.value) == "SYNO.API.Auth/login: DSM answered HTTP 502."
        self._assert_no_url(exc.value, "S3cretPW")

    @respx.mock
    async def test_query_api_info(self) -> None:
        from mcp_synology.core.errors import DsmHttpError

        respx.get(f"{BASE_URL}/webapi/query.cgi").respond(status_code=404)
        async with DsmClient(base_url=BASE_URL) as client:
            with pytest.raises(DsmHttpError) as exc:
                await client.query_api_info()
        assert str(exc.value) == "SYNO.API.Info/query: DSM answered HTTP 404."
        assert exc.value.retryable is False
        self._assert_no_url(exc.value)

    @respx.mock
    async def test_upload(self, tmp_path: Path) -> None:
        from mcp_synology.core.errors import DsmHttpError

        client = make_client(make_api_cache())
        client.sid = "SIDSECRET"
        local = tmp_path / "a.txt"
        local.write_bytes(b"a")
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(status_code=500)
        async with client:
            with pytest.raises(DsmHttpError) as exc:
                await client.upload("/video", local, "a.txt")
        assert str(exc.value) == "SYNO.FileStation.Upload/upload: DSM answered HTTP 500."
        self._assert_no_url(exc.value, "SIDSECRET")


class TestUndecodableReply:
    """DSM copies binary EXIF fields into its JSON as raw bytes that aren't UTF-8 (Photos
    get_exif "Picture Info", DSM 7.1.1). The reply is still read, those bytes as U+FFFD,
    instead of failing as "the answer wasn't a DSM reply"."""

    def test_bad_bytes_become_replacement_characters(self) -> None:
        from mcp_synology.core.client import _read_json

        resp = httpx.Response(
            200,
            content=b'{"success": true, "data": {"value": "\\u001ey\xec\\u001b", "ok": "fine"}}',
            headers={"content-type": 'application/json; charset="UTF-8"'},
        )
        body = _read_json(resp, "SYNO.Foto.Browse.Item/get_exif")
        assert body == {"success": True, "data": {"value": "\x1ey\ufffd\x1b", "ok": "fine"}}

    def test_bad_bytes_are_logged_at_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        from mcp_synology.core.client import _read_json

        resp = httpx.Response(
            200,
            content=b'{"success": true, "data": {"value": "\xec"}}',
            headers={"content-type": "application/json"},
        )
        with caplog.at_level("DEBUG", logger="mcp_synology.core.client"):
            _read_json(resp, "SYNO.Foto.Browse.Item/get_exif")
        assert (
            "SYNO.Foto.Browse.Item/get_exif: the reply has bytes that aren't UTF-8; "
            "reading them as U+FFFD"
        ) in caplog.text

    def test_bad_bytes_in_a_download_error_envelope(self) -> None:
        from mcp_synology.core.client import _error_envelope

        body = b'{"success": false, "error": {"code": 408, "errors": [{"path": "/a\xecb"}]}}'
        assert _error_envelope(body) == {"code": 408, "errors": [{"path": "/a\ufffdb"}]}

    def test_bad_bytes_in_a_page_still_not_a_reply(self) -> None:
        from mcp_synology.core.client import _read_json
        from mcp_synology.core.errors import DsmReplyError

        resp = httpx.Response(
            200, content=b"<html>\xec</html>", headers={"content-type": "text/html"}
        )
        with pytest.raises(DsmReplyError):
            _read_json(resp, "SYNO.Foto.Browse.Item/get_exif")


class TestNotADsmReply:
    """#282: an answer that isn't a JSON object (a reverse proxy's or captive portal's
    page) is a DsmReplyError naming the call and Content-Type, never the body. It used
    to reach the model as "Expecting value: line 1 column 1 (char 0)"."""

    _PAGE = httpx.Response(
        200,
        text="<html>Sign in to proxy secret-token</html>",
        headers={"content-type": "text/html; charset=utf-8"},
    )

    @staticmethod
    def _check(exc: BaseException, label: str, ctype: str, *, retryable: bool = True) -> None:
        from mcp_synology.core.errors import DsmReplyError, ErrorCode

        assert isinstance(exc, DsmReplyError)
        assert str(exc) == f"{label}: the answer wasn't a DSM reply (Content-Type {ctype})."
        assert exc.error_code == ErrorCode.UNAVAILABLE
        assert exc.code is None
        assert exc.retryable is retryable
        assert "reverse proxy" in (exc.suggestion or "")
        assert "secret-token" not in str(exc) and "secret-token" not in (exc.suggestion or "")

    @respx.mock
    @pytest.mark.parametrize(
        ("reply", "ctype"),
        [
            (_PAGE, "text/html"),
            (httpx.Response(200, json=["not", "an", "object"]), "application/json"),
            (httpx.Response(200, content=b""), "none"),
        ],
    )
    async def test_request(self, reply: httpx.Response, ctype: str) -> None:
        client = make_client(make_api_cache())
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(return_value=reply)
        async with client:
            with pytest.raises(SynologyError) as exc:
                await client.request("SYNO.FileStation.List", "list_share")
        self._check(exc.value, "SYNO.FileStation.List/list_share", ctype)

    @respx.mock
    async def test_query_api_info(self) -> None:
        respx.get(f"{BASE_URL}/webapi/query.cgi").mock(return_value=self._PAGE)
        async with DsmClient(base_url=BASE_URL) as client:
            with pytest.raises(SynologyError) as exc:
                await client.query_api_info()
        self._check(exc.value, "SYNO.API.Info/query", "text/html")

    @respx.mock
    async def test_upload(self, tmp_path: Path) -> None:
        client = make_client(make_api_cache())
        local = tmp_path / "a.txt"
        local.write_bytes(b"a")
        respx.post(f"{BASE_URL}/webapi/entry.cgi").mock(return_value=self._PAGE)
        async with client:
            with pytest.raises(SynologyError) as exc:
                await client.upload("/video", local, "a.txt")
        self._check(exc.value, "SYNO.FileStation.Upload/upload", "text/html")

    @respx.mock
    async def test_form_post_create_is_not_retryable(self) -> None:
        """Download Station's create may already have been applied: check, don't retry."""
        client = make_client(make_api_cache())
        respx.post(f"{BASE_URL}/webapi/DownloadStation/task.cgi").mock(return_value=self._PAGE)
        async with client:
            with pytest.raises(SynologyError) as exc:
                await client.request_form_post(
                    "SYNO.DownloadStation.Task", "create", version=1, params={"uri": "x"}
                )
        self._check(exc.value, "SYNO.DownloadStation.Task/create", "text/html", retryable=False)
        assert (exc.value.suggestion or "").startswith("DSM may still have applied the request")

    @respx.mock
    async def test_torrent_file_create_is_not_retryable(self, tmp_path: Path) -> None:
        """The torrent-file upload is a create too: DSM may already have added the task."""
        client = make_client(make_api_cache())
        torrent = tmp_path / "x.torrent"
        torrent.write_bytes(b"x")
        respx.post(f"{BASE_URL}/webapi/entry.cgi").mock(return_value=self._PAGE)
        async with client:
            with pytest.raises(SynologyError) as exc:
                await client.create_download_task_with_file(file_path=torrent, filename="x.torrent")
        self._check(exc.value, "SYNO.DownloadStation2.Task/create", "text/html", retryable=False)
        assert (exc.value.suggestion or "").startswith("DSM may still have applied the request")

    # Downloads (#292). DSM sends application/octet-stream for every File Station
    # download (mode=download), including real .html and .json files, and
    # application/force-download or image/jpeg for Photos, on DSM 7.1 and 7.2.2; so a
    # text/html 200 is never the file.
    _DOWNLOADS = [
        ("SYNO.FileStation.Download", "download"),
        ("SYNO.Foto.Thumbnail", "get"),
    ]

    @staticmethod
    async def _download(client: DsmClient, api: str, dest: Path) -> int:
        if api == "SYNO.FileStation.Download":
            return await client.download("/video/movie.mkv", dest)
        return await client.download_binary(api, "get", 2, {"id": "1"}, dest)

    @respx.mock
    @pytest.mark.parametrize(("api", "method"), _DOWNLOADS)
    @pytest.mark.parametrize(
        ("reply", "ctype"),
        [
            (_PAGE, "text/html"),
            (httpx.Response(200, json=["x"]), "application/json"),
            (httpx.Response(200, content=b"", headers={"content-type": "application/json"}),
             "application/json"),
            (httpx.Response(200, json={"success": False}), "application/json"),
            (httpx.Response(200, json={"error": "boom"}), "application/json"),
            (httpx.Response(200, json={"error": {"code": {"a": 1}}}), "application/json"),
            (httpx.Response(200, text="<html/>", headers={"content-type": "TEXT/HTML; charset=x"}),
             "text/html"),
            (httpx.Response(200, text="<html/>", headers={"content-type": "application/xhtml+xml"}),
             "application/xhtml+xml"),
        ],
    )  # fmt: skip
    async def test_download_writes_nothing(
        self, tmp_path: Path, api: str, method: str, reply: httpx.Response, ctype: str
    ) -> None:
        """A proxy's page used to be saved as the file and reported as downloaded; a JSON
        body that isn't an object crashed with AttributeError. Neither touches the
        existing file, and no temp file is left."""
        cache = make_api_cache()
        cache["SYNO.Foto.Thumbnail"] = ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2)
        client = make_client(cache)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(return_value=reply)
        dest = tmp_path / "movie.mkv"
        dest.write_bytes(b"the user's file")
        async with client:
            with pytest.raises(SynologyError) as exc:
                await self._download(client, api, dest)
        self._check(exc.value, f"{api}/{method}", ctype)
        assert dest.read_bytes() == b"the user's file"
        assert [p.name for p in tmp_path.iterdir()] == ["movie.mkv"]

    @respx.mock
    @pytest.mark.parametrize(("api", "method"), _DOWNLOADS)
    async def test_download_http_error_with_a_json_list(
        self, tmp_path: Path, api: str, method: str
    ) -> None:
        """A 502 whose body is a JSON list crashed with AttributeError; it's the same
        HTTP error as any other body that isn't a DSM envelope."""
        from mcp_synology.core.errors import FileStationError

        cache = make_api_cache()
        cache["SYNO.Foto.Thumbnail"] = ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2)
        client = make_client(cache)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(502, json=["x"])
        async with client:
            with pytest.raises(SynologyError) as exc:
                await self._download(client, api, tmp_path / "x")
        if api == "SYNO.FileStation.Download":
            assert isinstance(exc.value, FileStationError)
            assert str(exc.value) == "HTTP 502 from download API"
        else:
            assert isinstance(exc.value, DsmHttpError)
            assert exc.value.status == 502
        assert not (tmp_path / "x").exists()

    @respx.mock
    @pytest.mark.parametrize(
        "ctype", ["application/json", 'application/json; charset="UTF-8"', "text/html"]
    )
    async def test_download_session_error_reauths_and_retries(
        self, tmp_path: Path, ctype: str
    ) -> None:
        """DSM's session-error envelope on a download re-authenticates and retries, also
        when it comes as text/html, which used to be saved as the file."""
        envelope = httpx.Response(
            200,
            content=b'{"success": false, "error": {"code": 119}}',
            headers={"content-type": ctype},
        )
        data = httpx.Response(
            200, content=b"DATA", headers={"content-type": "application/octet-stream"}
        )
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=[envelope, data])
        reauths: list[bool] = []

        async def reauth() -> None:
            reauths.append(True)

        client = make_client(make_api_cache())
        async with client:
            client.set_re_auth_callback(reauth)
            written = await client.download("/video/movie.mkv", tmp_path / "movie.mkv")
        assert reauths == [True]
        assert written == 4
        assert (tmp_path / "movie.mkv").read_bytes() == b"DATA"

    @respx.mock
    async def test_download_http_error_with_a_dsm_envelope(self, tmp_path: Path) -> None:
        """An HTTP error that carries DSM's envelope is that DSM error."""
        client = make_client(make_api_cache())
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            404, json={"success": False, "error": {"code": 408}}
        )
        async with client:
            with pytest.raises(SynologyError) as exc:
                await client.download("/video/missing.mkv", tmp_path / "x")
        assert exc.value.code == 408
        assert not (tmp_path / "x").exists()


class TestRequestPostForm:
    """request(post_form=True): the login-only POST (#235). The parameters go in the
    form body, the session goes as the id cookie (#176), and the rest of request() applies."""

    @respx.mock
    async def test_body_and_query(self) -> None:
        from urllib.parse import parse_qsl

        client = make_client(make_api_cache())
        client.sid = "the-sid"
        route = respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"sid": "new"}}
        )
        async with client:
            data = await client.request(
                "SYNO.API.Auth",
                "login",
                version=6,
                params={"account": "chris", "passwd": "S3cret#PW&x=1", "format": "sid"},
                post_form=True,
            )
        assert data == {"sid": "new"}
        request = route.calls.last.request
        assert not request.url.params
        assert request.headers["cookie"] == "id=the-sid"
        assert "S3cret" not in str(request.url)
        assert dict(parse_qsl(request.content.decode())) == {
            "api": "SYNO.API.Auth",
            "version": "6",
            "method": "login",
            "account": "chris",
            "passwd": "S3cret#PW&x=1",
            "format": "sid",
        }

    @respx.mock
    async def test_dsm_error_and_log(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        from mcp_synology.core.errors import AuthenticationError

        client = make_client(make_api_cache())
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 400}}
        )
        async with client:
            with (
                caplog.at_level(logging.DEBUG, logger="mcp_synology.core.client"),
                pytest.raises(AuthenticationError),
            ):
                await client.request(
                    "SYNO.API.Auth",
                    "login",
                    version=6,
                    params={"account": "chris", "passwd": "S3cretPW"},
                    post_form=True,
                )
        assert "DSM POST: SYNO.API.Auth/login v6" in caplog.text
        assert "S3cretPW" not in caplog.text

    def test_log_safe_masks_share_passphrase(self) -> None:
        """A Photos share link's passphrase is its secret, as a password is."""
        from mcp_synology.core.client import _log_safe

        safe = _log_safe({"passphrase": "AbCd1234", "password": "pw", "album_id": 4})
        assert safe == {"passphrase": "***", "password": "***", "album_id": 4}

    @respx.mock
    async def test_session_retry_stays_post(self) -> None:
        client = make_client(make_api_cache())
        client.sid = "old"
        route = respx.post(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=[
                httpx.Response(200, json={"success": False, "error": {"code": 106}}),
                httpx.Response(200, json={"success": True, "data": {"ok": 1}}),
            ]
        )

        async def reauth() -> None:
            client.sid = "new"

        client.set_re_auth_callback(reauth)
        async with client:
            data = await client.request("SYNO.API.Auth", "login", version=6, post_form=True)
        assert data == {"ok": 1}
        assert route.call_count == 2
        assert not route.calls.last.request.url.params
        assert route.calls.last.request.headers["cookie"] == "id=new"


class TestTransportErrorNamesTheCall:
    """#237: a network error keeps its httpx type (get_client() and `check` rely on it)
    but records which DSM API/method it happened on."""

    @respx.mock
    async def test_request(self) -> None:
        from mcp_synology.core.client import dsm_call_of

        client = make_client(make_api_cache())
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=httpx.ReadTimeout(""))
        async with client:
            with pytest.raises(httpx.ReadTimeout) as exc:
                await client.request("SYNO.FileStation.List", "list_share")
        assert dsm_call_of(exc.value) == "SYNO.FileStation.List/list_share"

    @respx.mock
    async def test_upload(self, tmp_path: Path) -> None:
        from mcp_synology.core.client import dsm_call_of

        client = make_client(make_api_cache())
        local = tmp_path / "a.txt"
        local.write_bytes(b"a")
        respx.post(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=httpx.ConnectError("refused"))
        async with client:
            with pytest.raises(httpx.ConnectError) as exc:
                await client.upload("/video", local, "a.txt")
        assert dsm_call_of(exc.value) == "SYNO.FileStation.Upload/upload"

    def test_untagged(self) -> None:
        from mcp_synology.core.client import dsm_call_of

        assert dsm_call_of(httpx.ReadTimeout("")) is None


class TestRequestFormPost:
    """Form-POST variant of request() — the scoped exception to the GET-only
    rule, used only for DS URI Task.create (#123 item 9a)."""

    @respx.mock
    async def test_params_in_body_sid_as_cookie(self) -> None:
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
        assert not request.url.params
        assert request.headers["cookie"] == "id=the-sid"
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
    async def test_source_url_secrets_redacted_in_debug_log_but_sent_intact(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#143: a source URI's credentials or tracker passkey must not reach the log."""
        import json
        import logging

        passkey = "a1B2c3D4e5F6g7H8i9J0"
        v1_uri = f"https://alice:s3cret@files.example/x.iso,https://t.example/announce/{passkey}"
        ds2_url = json.dumps([f"https://t.example/dl?passkey={passkey}"])
        client = make_client(make_api_cache())
        v1 = respx.post(f"{BASE_URL}/webapi/DownloadStation/task.cgi").respond(
            json={"success": True}
        )
        ds2 = respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(json={"success": True})
        caplog.set_level(logging.DEBUG, logger="mcp_synology.core.client")
        async with client:
            await client.request_form_post(
                "SYNO.DownloadStation.Task", "create", version=1, params={"uri": v1_uri}
            )
            await client.request_form_post(
                "SYNO.DownloadStation2.Task", "create", version=2, params={"url": ds2_url}
            )
        assert passkey not in caplog.text
        assert "s3cret" not in caplog.text
        assert "files.example" in caplog.text  # still useful for debugging
        assert passkey in v1.calls.last.request.content.decode()  # DSM gets the real URI
        assert passkey in ds2.calls.last.request.content.decode()

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
        assert not route.calls.last.request.url.params
        assert route.calls.last.request.headers["cookie"] == "id=new-sid"

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
    async def test_connect_error_raises_structured_error(self) -> None:
        """#154: a transport failure other than a timeout was also a raw httpx error."""
        client = make_client(make_api_cache())
        respx.post(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=httpx.ConnectError(""))
        async with client:
            with pytest.raises(SynologyError) as exc:
                await client.request_form_post("SYNO.DownloadStation2.Task", "create", version=2)
        assert exc.value.error_code.value == "unavailable"
        assert exc.value.retryable is False
        assert str(exc.value)

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

    @pytest.mark.parametrize(
        ("failure", "code"),
        [
            (httpx.ReadTimeout("slow"), "timeout"),
            (httpx.ConnectTimeout(""), "timeout"),
            (httpx.ConnectError(""), "unavailable"),
        ],
    )
    @respx.mock
    async def test_transport_failure_raises_structured_non_retryable_error(
        self, tmp_path: Path, failure: Exception, code: str
    ) -> None:
        """#154: like URI create, a torrent upload that fails in transport must
        not escape as a raw (possibly empty) httpx error or invite a retry."""
        client = make_client(make_api_cache())
        torrent = tmp_path / "x.torrent"
        torrent.write_bytes(b"x")
        respx.post(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=failure)
        async with client:
            with pytest.raises(SynologyError) as exc:
                await client.create_download_task_with_file(file_path=torrent, filename="x.torrent")
        assert exc.value.error_code.value == code
        assert exc.value.retryable is False
        assert str(exc.value)  # never empty, unlike ConnectTimeout('')
        assert "check" in (exc.value.suggestion or "")

    @respx.mock
    async def test_http_5xx_raises_structured_non_retryable_error(self, tmp_path: Path) -> None:
        client = make_client(make_api_cache())
        torrent = tmp_path / "x.torrent"
        torrent.write_bytes(b"x")
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(status_code=502)
        async with client:
            with pytest.raises(SynologyError) as exc:
                await client.create_download_task_with_file(file_path=torrent, filename="x.torrent")
        assert exc.value.error_code.value == "unavailable"
        assert exc.value.retryable is False
        assert "502" in str(exc.value)

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
        assert not request.url.params
        assert request.headers["cookie"] == "id=the-sid"
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


class TestUpload:
    """DsmClient.upload(): the File Station multipart POST. #211 covered the overwrite
    field through upload_file; these test the client itself, including the re-auth retry
    (the gap #200 pointed at)."""

    @staticmethod
    def _field(body: bytes, name: str, value: str) -> bool:
        return f'name="{name}"\r\n\r\n{value}\r\n'.encode() in body

    @respx.mock
    async def test_multipart_shape(self, tmp_path: Path) -> None:
        client = make_client(make_api_cache())
        client.sid = "the-sid"
        local = tmp_path / "report.txt"
        local.write_bytes(b"hello")
        route = respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"blSkip": False, "file": "report.txt"}}
        )
        async with client:
            data = await client.upload("/video/in", local, "report.txt")
        assert data == {"blSkip": False, "file": "report.txt"}
        request = route.calls.last.request
        # The session goes as the id cookie (#176), never in the form.
        assert not request.url.params
        assert request.headers["cookie"] == "id=the-sid"
        body = request.content
        assert b'name="_sid"' not in body
        for name, value in [
            ("api", "SYNO.FileStation.Upload"),
            ("version", "2"),
            ("method", "upload"),
            ("path", "/video/in"),
            ("create_parents", "true"),
        ]:
            assert self._field(body, name, value), name
        assert b'name="file"; filename="report.txt"' in body
        assert b"\r\n\r\nhello\r\n" in body

    @respx.mock
    @pytest.mark.parametrize("overwrite", [False, True])
    async def test_overwrite_sent_only_when_true(self, tmp_path: Path, overwrite: bool) -> None:
        """DSM reads overwrite=false as "skip" and answers success (#195)."""
        client = make_client(make_api_cache())
        local = tmp_path / "a.txt"
        local.write_bytes(b"a")
        route = respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {}}
        )
        async with client:
            await client.upload("/video", local, "a.txt", overwrite=overwrite)
        body = route.calls.last.request.content
        assert (b'name="overwrite"' in body) is overwrite
        if overwrite:
            assert self._field(body, "overwrite", "true")

    @respx.mock
    @pytest.mark.parametrize("overwrite", [False, True])
    async def test_session_error_retries_once_with_the_file_and_overwrite_intact(
        self, tmp_path: Path, overwrite: bool
    ) -> None:
        """The retry reopens the file (the first handle is closed) and keeps the same
        overwrite handling, so a retried upload can't turn into a silent skip."""
        client = make_client(make_api_cache())
        client.sid = "old_sid"
        local = tmp_path / "a.txt"
        local.write_bytes(b"payload")
        reauths = 0

        async def fake_reauth() -> None:
            nonlocal reauths
            reauths += 1
            client.sid = "new_sid"

        client.set_re_auth_callback(fake_reauth)
        route = respx.post(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=[
                httpx.Response(200, json={"success": False, "error": {"code": 119}}),
                httpx.Response(200, json={"success": True, "data": {"blSkip": False}}),
            ]
        )
        async with client:
            data = await client.upload("/video", local, "a.txt", overwrite=overwrite)
        assert data == {"blSkip": False}
        assert reauths == 1
        first, second = (c.request for c in route.calls)
        assert first.headers["cookie"] == "id=old_sid"
        assert second.headers["cookie"] == "id=new_sid"
        for request in (first, second):
            assert b"\r\n\r\npayload\r\n" in request.content
            assert (b'name="overwrite"' in request.content) is overwrite

    @respx.mock
    async def test_session_error_on_the_retry_is_not_retried_again(self, tmp_path: Path) -> None:
        client = make_client(make_api_cache())
        local = tmp_path / "a.txt"
        local.write_bytes(b"a")
        reauths = 0

        async def fake_reauth() -> None:
            nonlocal reauths
            reauths += 1

        client.set_re_auth_callback(fake_reauth)
        route = respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 106}}
        )
        async with client:
            with pytest.raises(SessionExpiredError):
                await client.upload("/video", local, "a.txt")
        assert reauths == 1
        assert route.call_count == 2

    @respx.mock
    async def test_failed_reauth_raises_original_session_error(self, tmp_path: Path) -> None:
        client = make_client(make_api_cache())
        local = tmp_path / "a.txt"
        local.write_bytes(b"a")

        async def failing_reauth() -> None:
            raise SynologyError("login failed")

        client.set_re_auth_callback(failing_reauth)
        route = respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 106}}
        )
        async with client:
            with pytest.raises(SessionExpiredError):
                await client.upload("/video", local, "a.txt")
        assert route.call_count == 1

    @respx.mock
    async def test_file_exists_raises_typed_error(self, tmp_path: Path) -> None:
        from mcp_synology.core.errors import SynologyFileExistsError

        client = make_client(make_api_cache())
        local = tmp_path / "a.txt"
        local.write_bytes(b"a")
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 414}}
        )
        async with client:
            with pytest.raises(SynologyFileExistsError):
                await client.upload("/video", local, "a.txt")

    async def test_upload_api_absent_raises_api_not_found(self, tmp_path: Path) -> None:
        cache = make_api_cache()
        del cache["SYNO.FileStation.Upload"]
        client = make_client(cache)
        local = tmp_path / "a.txt"
        local.write_bytes(b"a")
        async with client:
            with pytest.raises(ApiNotFoundError):
                await client.upload("/video", local, "a.txt")


class TestGeneralTransfers:
    """``upload_multipart()`` and ``download_binary()``: the File Station transfers
    generalized for Synology Photos (#258), with File Station's own wrappers above."""

    @staticmethod
    def _cache() -> dict[str, ApiInfoEntry]:
        cache = make_api_cache()
        for api in ("SYNO.Foto.Upload.Item", "SYNO.Foto.Thumbnail", "SYNO.Foto.Browse.Item"):
            cache[api] = ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2)
        return cache

    @respx.mock
    async def test_upload_multipart_sends_fields_as_given(self, tmp_path: Path) -> None:
        client = make_client(self._cache())
        client.sid = "the-sid"
        local = tmp_path / "2024.jpg"
        local.write_bytes(b"jpeg")
        route = respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"id": 41, "action": "new"}}
        )
        async with client:
            data = await client.upload_multipart(
                "SYNO.Foto.Upload.Item",
                "upload_to_folder",
                1,
                {"name": '"2024.jpg"', "target_folder_id": "7"},
                file_path=local,
                filename="2024.jpg",
            )
        assert data == {"id": 41, "action": "new"}
        request = route.calls.last.request
        assert not request.url.params
        assert request.headers["cookie"] == "id=the-sid"
        body = request.content
        for name, value in [
            ("api", "SYNO.Foto.Upload.Item"),
            ("method", "upload_to_folder"),
            ("version", "1"),
            ("name", '"2024.jpg"'),
            ("target_folder_id", "7"),
        ]:
            assert TestUpload._field(body, name, value), name
        assert b'name="file"; filename="2024.jpg"' in body

    @respx.mock
    async def test_upload_multipart_reauths_once(self, tmp_path: Path) -> None:
        client = make_client(self._cache())
        local = tmp_path / "a.jpg"
        local.write_bytes(b"a")
        route = respx.post(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=[
                httpx.Response(200, json={"success": False, "error": {"code": 119}}),
                httpx.Response(200, json={"success": True, "data": {"id": 1}}),
            ]
        )
        reauths: list[int] = []

        async def _reauth() -> None:
            reauths.append(1)

        client.set_re_auth_callback(_reauth)
        async with client:
            data = await client.upload_multipart(
                "SYNO.Foto.Upload.Item", "upload_to_folder", 1, {}, file_path=local, filename="a"
            )
        assert data == {"id": 1}
        assert reauths == [1]
        assert route.call_count == 2
        assert b"\r\n\r\na\r\n" in route.calls.last.request.content

    @respx.mock
    async def test_upload_multipart_error_names_the_parameter(self, tmp_path: Path) -> None:
        client = make_client(self._cache())
        local = tmp_path / "notes.txt"
        local.write_bytes(b"x")
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 620}}
        )
        async with client:
            with pytest.raises(SynologyError) as info:
                await client.upload_multipart(
                    "SYNO.Foto.Upload.Item",
                    "upload_to_folder",
                    1,
                    {},
                    file_path=local,
                    filename="n",
                )
        assert info.value.code == 620
        assert "file type" in str(info.value)

    @respx.mock
    async def test_download_binary_streams_any_api(self, tmp_path: Path) -> None:
        client = make_client(self._cache())
        client.sid = "the-sid"
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=b"\xff\xd8thumb", headers={"content-type": "image/jpeg"}
        )
        dest = tmp_path / "beach_m.jpg"
        async with client:
            written = await client.download_binary(
                "SYNO.Foto.Thumbnail",
                "get",
                2,
                {"id": "41", "cache_key": '"41_1"', "type": '"unit"', "size": '"m"'},
                dest,
            )
        assert written == 7
        assert dest.read_bytes() == b"\xff\xd8thumb"
        params = dict(route.calls.last.request.url.params)
        assert params == {
            "api": "SYNO.Foto.Thumbnail",
            "version": "2",
            "method": "get",
            "id": "41",
            "cache_key": '"41_1"',
            "type": '"unit"',
            "size": '"m"',
        }
        assert route.calls.last.request.headers["cookie"] == "id=the-sid"

    @respx.mock
    async def test_download_binary_http_error_page_is_dsm_http_error(self, tmp_path: Path) -> None:
        """A thumbnail still converting gets 404 with an HTML page (verified, #258)."""
        client = make_client(self._cache())
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            404, text="<html>Not Found</html>", headers={"content-type": "text/html"}
        )
        dest = tmp_path / "x.jpg"
        async with client:
            with pytest.raises(DsmHttpError) as info:
                await client.download_binary("SYNO.Foto.Thumbnail", "get", 2, {"id": "1"}, dest)
        assert info.value.status == 404
        assert "SYNO.Foto.Thumbnail/get" in str(info.value)
        assert not dest.exists()

    @respx.mock
    async def test_download_binary_json_error_envelope(self, tmp_path: Path) -> None:
        client = make_client(self._cache())
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 803}}
        )
        async with client:
            with pytest.raises(SynologyError) as info:
                await client.download_binary(
                    "SYNO.Foto.Thumbnail", "get", 2, {"id": "1"}, tmp_path / "x"
                )
        assert info.value.code == 803

    @respx.mock
    async def test_request_error_names_the_parameter(self) -> None:
        client = make_client(self._cache())
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": False,
                "error": {"code": 120, "errors": {"name": "rating", "reason": "condition"}},
            }
        )
        async with client:
            with pytest.raises(SynologyError) as info:
                await client.request(
                    "SYNO.Foto.Browse.Item", "set", 2, {"rating": "9"}, post_form=True
                )
        assert str(info.value).endswith("rating (condition)")


class TestErrorDetail:
    """QA round 1 on #266: the 120 detail reaches binary downloads too, and the debug
    log names only the code, ``name`` and ``reason``, never DSM's whole error object."""

    @respx.mock
    async def test_download_binary_passes_the_detail(self, tmp_path: Path) -> None:
        client = make_client(TestGeneralTransfers._cache())
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 120, "errors": {"name": "size"}}}
        )
        async with client:
            with pytest.raises(SynologyError) as info:
                await client.download_binary("SYNO.Foto.Thumbnail", "get", 2, {}, tmp_path / "x")
        assert str(info.value).endswith(": size")

    @respx.mock
    async def test_debug_log_omits_the_raw_error(self, caplog: pytest.LogCaptureFixture) -> None:
        client = make_client(TestGeneralTransfers._cache())
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={
                "success": False,
                "error": {"code": 120, "errors": {"name": "rating", "token": "s3cr3t"}},
            }
        )
        caplog.set_level("DEBUG", logger="mcp_synology.core.client")
        async with client:
            with pytest.raises(SynologyError):
                await client.request("SYNO.Foto.Browse.Item", "set", 2, {}, post_form=True)
        assert "s3cr3t" not in caplog.text
        assert "error code 120: rating" in caplog.text
