"""Tests for modules/filestation/transfer.py — upload_file, download_file."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import sys
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.filestation.transfer import (
    download_file,
    upload_file,
)
from tests.conftest import BASE_URL

# Root ignores file permission bits, so chmod can't provoke these errors there
# (common in containers). Without the skip, the upload test reads the file,
# then makes an unmocked HTTP call.
_needs_non_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores file permission bits",
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mcp_synology.core.client import DsmClient


class TestUploadFile:
    @respx.mock
    async def test_upload_success(self, mock_client: DsmClient, tmp_path: Path) -> None:
        local_file = tmp_path / "test.txt"
        local_file.write_text("hello world")

        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(json={"success": True, "data": {}})

        result = await upload_file(
            mock_client,
            local_path=str(local_file),
            dest_folder="/video/uploads",
        )
        assert "[+]" in result
        assert "test.txt" in result
        assert "/video/uploads/" in result

    async def test_upload_refuses_a_local_name_that_isnt_one(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """With no filename, the local file's own name follows the same rule (pre-QA on
        #470): ` .. ` is a valid local name."""
        local_file = tmp_path / " .. "
        local_file.write_text("x")
        with pytest.raises(ToolError) as exc:
            await upload_file(mock_client, local_path=str(local_file), dest_folder="/video/up")
        error = json.loads(str(exc.value))["error"]
        # Named as the local file, not `filename`, which wasn't passed (QA round 3 on #470).
        assert (error["code"], error["param"]) == ("invalid_parameter", "local_path")
        assert error["message"] == (
            "Upload failed: the local file's name (' .. ') should be just a file name, but it "
            "is '.' or '..'."
        )
        assert error["suggestion"] == "Pass filename with a plain name for the file on the NAS."
        assert error["value"] == str(local_file)  # the argument named, as passed

    @pytest.mark.parametrize(
        "filename", [".", "..", " .. ", "../x", "a/b", "/abs", "a\0b", "   ", "\t", "\xa0"]
    )
    async def test_upload_refuses_a_filename_that_isnt_a_name(
        self, mock_client: DsmClient, tmp_path: Path, filename: str
    ) -> None:
        """`filename` is the name DSM writes in dest_folder: as with rename's new_name, no
        path and no `.` or `..` (pre-QA on QA round 1, #470)."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("x")
        with pytest.raises(ToolError) as exc:
            await upload_file(
                mock_client,
                local_path=str(local_file),
                dest_folder="/video/uploads",
                filename=filename,
            )
        error = json.loads(str(exc.value))["error"]
        assert (error["code"], error["param"]) == ("invalid_parameter", "filename")

    async def test_upload_local_file_not_found(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError) as exc_info:
            await upload_file(
                mock_client,
                local_path="/nonexistent/file.txt",
                dest_folder="/video/uploads",
            )
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "not_found"

    @respx.mock
    async def test_upload_file_exists_no_overwrite(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        local_file = tmp_path / "test.txt"
        local_file.write_text("hello")

        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 414}}
        )

        with pytest.raises(ToolError) as exc_info:
            await upload_file(
                mock_client,
                local_path=str(local_file),
                dest_folder="/video/uploads",
            )
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "already_exists"
        assert "overwrite=true" in body["error"]["suggestion"]
        # No filename was passed: the error names local_path (pre-QA on #470).
        assert body["error"]["param"] == "local_path"

    @respx.mock
    @pytest.mark.parametrize(("overwrite", "sent"), [(False, None), (True, "true")])
    async def test_upload_sends_overwrite_only_when_true(
        self, mock_client: DsmClient, tmp_path: Path, overwrite: bool, sent: str | None
    ) -> None:
        """DSM reads overwrite=false as "skip" and reports success; only an omitted
        overwrite makes it fail with 414 (#195, DSM 7.2.2 probe)."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("hello")
        route = respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"blSkip": False, "file": "test.txt", "progress": 1}}
        )

        await upload_file(
            mock_client, local_path=str(local_file), dest_folder="/video", overwrite=overwrite
        )
        body = route.calls.last.request.content
        if sent is None:
            assert b'name="overwrite"' not in body
        else:
            assert b'name="overwrite"\r\n\r\n' + sent.encode() + b"\r\n" in body

    @respx.mock
    async def test_upload_skipped_by_dsm_is_already_exists(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """DSM's skip answer (success, blSkip=true; the file is left alone) is never
        reported as uploaded (#195)."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("hello")
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"blSkip": True, "file": "r.txt", "progress": 1}}
        )

        with pytest.raises(ToolError) as exc_info:
            await upload_file(
                mock_client, local_path=str(local_file), dest_folder="/video", filename="r.txt"
            )
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == "already_exists"
        assert err["param"] == "filename"
        assert err["value"] == "r.txt"
        assert "overwrite=true" in err["suggestion"]

    @respx.mock
    async def test_upload_overwrite_success(self, mock_client: DsmClient, tmp_path: Path) -> None:
        local_file = tmp_path / "test.txt"
        local_file.write_text("updated content")

        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(json={"success": True, "data": {}})

        result = await upload_file(
            mock_client,
            local_path=str(local_file),
            dest_folder="/video/uploads",
            overwrite=True,
        )
        assert "[+]" in result
        assert "test.txt" in result

    @respx.mock
    async def test_upload_custom_filename(self, mock_client: DsmClient, tmp_path: Path) -> None:
        local_file = tmp_path / "test.txt"
        local_file.write_text("hello")

        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(json={"success": True, "data": {}})

        result = await upload_file(
            mock_client,
            local_path=str(local_file),
            dest_folder="/video/uploads",
            filename="renamed.txt",
        )
        assert "[+]" in result
        assert "renamed.txt" in result

    @respx.mock
    async def test_upload_dsm_error(self, mock_client: DsmClient, tmp_path: Path) -> None:
        local_file = tmp_path / "test.txt"
        local_file.write_text("hello")

        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 1802}}
        )

        with pytest.raises(ToolError) as exc_info:
            await upload_file(
                mock_client,
                local_path=str(local_file),
                dest_folder="/video/uploads",
            )
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert "Upload" in body["error"]["message"]

    @_needs_non_root
    async def test_upload_local_file_permission_error(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """OSError reading local file should raise ToolError."""
        local_file = tmp_path / "no_read.txt"
        local_file.write_text("hello")
        local_file.chmod(0o000)

        try:
            with pytest.raises(ToolError) as exc_info:
                await upload_file(
                    mock_client,
                    local_path=str(local_file),
                    dest_folder="/video/uploads",
                )
            body = json.loads(str(exc_info.value))
            assert body["status"] == "error"
            # Could be "not_found" (can't stat) or "filesystem_error" (can't read)
            assert body["error"]["code"] in ("not_found", "filesystem_error")
        finally:
            # Restore permissions for cleanup
            local_file.chmod(0o644)


class TestDownloadFile:
    @respx.mock
    async def test_download_success(self, mock_client: DsmClient, tmp_path: Path) -> None:
        file_content = b"binary file content here"
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=file_content,
            headers={"content-type": "application/octet-stream"},
        )

        result = await download_file(
            mock_client,
            path="/video/movie.mkv",
            dest_folder=str(tmp_path),
        )
        assert "[+]" in result
        assert "movie.mkv" in result

        downloaded = tmp_path / "movie.mkv"
        assert downloaded.exists()
        assert downloaded.read_bytes() == file_content

    async def test_download_local_dir_not_found(self, mock_client: DsmClient) -> None:
        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client,
                path="/video/movie.mkv",
                dest_folder="/nonexistent/dir",
            )
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "not_found"

    async def test_download_file_exists_no_overwrite(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        existing = tmp_path / "movie.mkv"
        existing.write_text("existing")

        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client,
                path="/video/movie.mkv",
                dest_folder=str(tmp_path),
            )
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "already_exists"
        assert "overwrite=true" in body["error"]["suggestion"]
        # #189: the conflict is the file name (as upload_file reports it), not dest_folder.
        assert body["error"]["param"] == "filename"
        assert body["error"]["value"] == "movie.mkv"
        assert str(existing) in body["error"]["message"]

    @respx.mock
    async def test_download_overwrite_success(self, mock_client: DsmClient, tmp_path: Path) -> None:
        existing = tmp_path / "movie.mkv"
        existing.write_text("old content")

        new_content = b"new file content"
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=new_content,
            headers={"content-type": "application/octet-stream"},
        )

        result = await download_file(
            mock_client,
            path="/video/movie.mkv",
            dest_folder=str(tmp_path),
            overwrite=True,
        )
        assert "[+]" in result
        assert existing.read_bytes() == new_content

    @respx.mock
    async def test_download_custom_filename(self, mock_client: DsmClient, tmp_path: Path) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=b"data",
            headers={"content-type": "application/octet-stream"},
        )

        result = await download_file(
            mock_client,
            path="/video/movie.mkv",
            dest_folder=str(tmp_path),
            filename="renamed.mkv",
        )
        assert "[+]" in result
        assert "renamed.mkv" in result
        assert (tmp_path / "renamed.mkv").exists()

    @respx.mock
    async def test_download_dsm_error_response(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 408}},
            headers={"content-type": "application/json"},
        )

        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client,
                path="/video/nonexistent.mkv",
                dest_folder=str(tmp_path),
            )
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert "Download" in body["error"]["message"]
        # Partial file should not exist
        assert not (tmp_path / "nonexistent.mkv").exists()

    @staticmethod
    def _route(getinfo: httpx.Response | Exception, download: httpx.Response) -> list[str]:
        """Answer getinfo (or raise for it) and download separately; the methods called."""
        calls: list[str] = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            method = request.url.params.get("method", "")
            calls.append(method)
            if method != "getinfo":
                return download
            if isinstance(getinfo, Exception):
                raise getinfo
            return getinfo

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=side_effect)
        return calls

    @respx.mock
    async def test_download_missing_file_is_not_found_up_front(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """#324: getinfo's 408 entry is a missing file. DSM would answer the download with
        a bare 502 (#292), which can't be told from a server error."""
        calls = self._route(
            httpx.Response(
                200, json={"success": True, "data": {"files": [{"code": 408, "path": "/v/x"}]}}
            ),
            httpx.Response(502, text="<html>DSM error page</html>"),
        )
        with pytest.raises(ToolError) as exc_info:
            await download_file(mock_client, path="/video/x.mkv", dest_folder=str(tmp_path))
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == "not_found"
        assert err["retryable"] is False
        assert err["message"] == "Download failed: /video/x.mkv doesn't exist on the NAS."
        assert calls == ["getinfo"]  # no download sent
        assert list(tmp_path.iterdir()) == []

    @respx.mock
    @pytest.mark.parametrize("status", [502, 504])
    async def test_download_5xx_on_a_file_that_exists_is_retryable(
        self, mock_client: DsmClient, tmp_path: Path, status: int
    ) -> None:
        """#324: getinfo found the file, so a bare 5xx is a server or proxy error. It was
        filestation_error "DSM error 502", not retryable, "check that the file exists"."""
        info = {"files": [{"path": "/video/x.mkv", "name": "x.mkv", "additional": {"size": 3}}]}
        calls = self._route(
            httpx.Response(200, json={"success": True, "data": info}),
            httpx.Response(status, text="<html>Bad Gateway</html>"),
        )
        with pytest.raises(ToolError) as exc_info:
            await download_file(mock_client, path="/video/x.mkv", dest_folder=str(tmp_path))
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == "unavailable"
        assert err["retryable"] is True
        assert err["message"] == (
            f"Download failed: DSM, or a proxy in front of it, answered HTTP {status} for "
            "/video/x.mkv, which exists."
        )
        assert calls == ["getinfo", "download"]
        assert list(tmp_path.iterdir()) == []

    @respx.mock
    async def test_download_unreadable_path_refused_up_front(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """A path the user can't read (getinfo's 407 entry): DSM would answer the download
        with the same bare 502 as a missing file (vdsm 7.1, 7.2.2), so it's refused first."""
        calls = self._route(
            httpx.Response(
                200, json={"success": True, "data": {"files": [{"code": 407, "path": "/v"}]}}
            ),
            httpx.Response(502, text="<html>DSM error page</html>"),
        )
        with pytest.raises(ToolError) as exc_info:
            await download_file(mock_client, path="/video/x.mkv", dest_folder=str(tmp_path))
        err = json.loads(str(exc_info.value))["error"]
        assert (err["code"], err["message"], err["retryable"]) == (
            "permission_denied",
            "Download failed: no permission to read /video/x.mkv on the NAS.",
            False,
        )
        assert calls == ["getinfo"]

    @staticmethod
    def _folder_info(path: str = "/video/shows") -> httpx.Response:
        folder = {"path": path, "name": PurePosixPath(path).name, "isdir": True}
        return httpx.Response(200, json={"success": True, "data": {"files": [folder]}})

    @respx.mock
    async def test_download_folder_saves_its_zip(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """DSM sends a folder below a share as a ZIP of it (vdsm 7.1, 7.2.2); it's saved as
        <folder>.zip, and the reply says what it is (maintainer decision on #327)."""
        zip_bytes = b"PK\x03\x04zip"
        self._route(self._folder_info(), httpx.Response(200, content=zip_bytes))
        out = await download_file(mock_client, path="/video/shows", dest_folder=str(tmp_path))
        assert (tmp_path / "shows.zip").read_bytes() == zip_bytes
        assert not (tmp_path / "shows").exists()
        assert "Downloaded the folder /video/shows as a ZIP archive of it" in out
        assert str(tmp_path / "shows.zip") in out

    @respx.mock
    async def test_download_folder_name_too_long_with_zip(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """A 253-byte folder name fits; with ".zip" it doesn't: a structured error, not
        a raw OSError (File name too long)."""
        path = "/video/" + "a" * 253
        calls = self._route(self._folder_info(path), httpx.Response(200, content=b"PK"))
        with pytest.raises(ToolError) as exc_info:
            await download_file(mock_client, path=path, dest_folder=str(tmp_path))
        err = json.loads(str(exc_info.value))["error"]
        assert (err["code"], err["param"]) == ("invalid_parameter", "filename")
        assert "too long" in err["message"]
        assert calls == ["getinfo"]

    @respx.mock
    async def test_download_folder_filename_and_overwrite(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """filename is used as given; overwrite applies to the .zip name."""
        self._route(self._folder_info(), httpx.Response(200, content=b"PK\x03\x04"))
        await download_file(
            mock_client, path="/video/shows", dest_folder=str(tmp_path), filename="tv.zip"
        )
        assert (tmp_path / "tv.zip").exists()

        (tmp_path / "shows.zip").write_bytes(b"old")
        calls = self._route(self._folder_info(), httpx.Response(200, content=b"PK\x03\x04"))
        with pytest.raises(ToolError) as exc_info:
            await download_file(mock_client, path="/video/shows", dest_folder=str(tmp_path))
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == "already_exists"
        assert str(tmp_path / "shows.zip") in err["message"]
        assert calls == ["getinfo"]
        await download_file(
            mock_client, path="/video/shows", dest_folder=str(tmp_path), overwrite=True
        )
        assert (tmp_path / "shows.zip").read_bytes() == b"PK\x03\x04"

    @respx.mock
    @pytest.mark.parametrize(
        "getinfo",
        [None, httpx.Response(502, text="<html>Bad Gateway</html>")],
        ids=["folder-entry", "getinfo-failed"],
    )
    async def test_download_share_root_refused_up_front(
        self, mock_client: DsmClient, tmp_path: Path, getinfo: httpx.Response | None
    ) -> None:
        """A share's top level: DSM can't send it (502, or 404 as admin), so it's refused
        before any download, whatever getinfo said."""
        calls = self._route(getinfo or self._folder_info("/video"), httpx.Response(502))
        with pytest.raises(ToolError) as exc_info:
            await download_file(mock_client, path="video", dest_folder=str(tmp_path))
        err = json.loads(str(exc_info.value))["error"]
        assert (err["code"], err["retryable"], err["param"]) == (
            "invalid_parameter",
            False,
            "path",
        )
        assert err["message"] == (
            "Download failed: /video is a whole shared folder, which DSM can't send as one "
            "download."
        )
        assert calls == ["getinfo"]
        assert list(tmp_path.iterdir()) == []

    @respx.mock
    async def test_download_missing_share_is_not_found(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        missing = {"files": [{"code": 408, "path": "/nosuch"}]}
        self._route(
            httpx.Response(200, json={"success": True, "data": missing}), httpx.Response(502)
        )
        with pytest.raises(ToolError) as exc_info:
            await download_file(mock_client, path="/nosuch", dest_folder=str(tmp_path))
        assert json.loads(str(exc_info.value))["error"]["code"] == "not_found"

    @respx.mock
    async def test_download_dsm_error_code_over_500_is_not_an_http_status(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """A DSM error reply with a code like 900 isn't an HTTP status: not retryable."""
        info = {"files": [{"path": "/video/x.mkv", "name": "x.mkv", "additional": {"size": 3}}]}
        self._route(
            httpx.Response(200, json={"success": True, "data": info}),
            httpx.Response(200, json={"success": False, "error": {"code": 900}}),
        )
        with pytest.raises(ToolError) as exc_info:
            await download_file(mock_client, path="/video/x.mkv", dest_folder=str(tmp_path))
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] != "unavailable"
        assert "HTTP" not in err["message"]

    @respx.mock
    @pytest.mark.parametrize(
        "getinfo",
        [
            httpx.Response(502, text="<html>Bad Gateway</html>"),
            httpx.Response(200, text="<html>Sign in</html>", headers={"content-type": "text/html"}),
            httpx.ConnectError("refused"),
        ],
        ids=["getinfo-502", "getinfo-page", "getinfo-unreachable"],
    )
    async def test_download_5xx_after_an_unanswered_check_is_retryable(
        self, mock_client: DsmClient, tmp_path: Path, getinfo: httpx.Response | Exception
    ) -> None:
        """#324's own case: a proxy answering 502 answers the getinfo check too. Neither got
        a DSM answer, so it's a server or proxy failure; a retry that gets through reports a
        missing file as not_found."""
        self._route(getinfo, httpx.Response(502, text="<html>Bad Gateway</html>"))
        with pytest.raises(ToolError) as exc_info:
            await download_file(mock_client, path="/video/x.mkv", dest_folder=str(tmp_path))
        err = json.loads(str(exc_info.value))["error"]
        assert (err["code"], err["retryable"]) == ("unavailable", True)
        assert err["message"] == (
            "Download failed: DSM, or a proxy in front of it, answered HTTP 502 and the check "
            "of /video/x.mkv before it got no DSM answer either."
        )

    @respx.mock
    @pytest.mark.parametrize(
        "getinfo",
        [
            httpx.Response(
                200, json={"success": True, "data": {"files": [{"code": 418, "path": "/v"}]}}
            ),
            httpx.Response(200, json={"success": False, "error": {"code": 105}}),
            httpx.Response(403, text="<html>Forbidden</html>"),
        ],
        ids=["other-error-entry", "dsm-error", "proxy-4xx"],
    )
    async def test_download_5xx_when_getinfo_didnt_say_keeps_the_old_error(
        self, mock_client: DsmClient, tmp_path: Path, getinfo: httpx.Response
    ) -> None:
        """getinfo answered, but not that the file exists (DSM's own error, another error
        entry, or a 4xx refusal): the 502 may be DSM's missing-file reply."""
        self._route(getinfo, httpx.Response(502, text="<html>Bad Gateway</html>"))
        with pytest.raises(ToolError) as exc_info:
            await download_file(mock_client, path="/video/x.mkv", dest_folder=str(tmp_path))
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == "filestation_error"
        assert err["retryable"] is False

    @respx.mock
    async def test_download_proxy_page_is_not_saved(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """A reverse proxy's or captive portal's login page was saved as the file and
        reported as downloaded (#292). DSM sends application/octet-stream for every
        download, even a real .html file, so text/html is never the file."""
        existing = tmp_path / "movie.mkv"
        existing.write_bytes(b"local data")
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            200, text="<html>Sign in to proxy</html>", headers={"content-type": "text/html"}
        )

        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client, path="/video/movie.mkv", dest_folder=str(tmp_path), overwrite=True
            )
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == "unavailable"
        assert err["retryable"] is True
        assert "the answer wasn't a DSM reply (Content-Type text/html)" in err["message"]
        assert "Sign in" not in err["message"]
        assert existing.read_bytes() == b"local data"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["movie.mkv"]

    @respx.mock
    async def test_download_dsm_error_keeps_existing_file_on_overwrite(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """A DSM error must not delete the local file overwrite=True would replace.

        Before the fix, the SynologyError cleanup unlinked dest_file even
        though nothing had been written: a missing NAS path destroyed the
        user's existing local copy.
        """
        existing = tmp_path / "report.pdf"
        existing.write_bytes(b"local data")
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 408}},
            headers={"content-type": "application/json"},
        )

        with pytest.raises(ToolError):
            await download_file(
                mock_client,
                path="/docs/report.pdf",
                dest_folder=str(tmp_path),
                overwrite=True,
            )

        assert existing.read_bytes() == b"local data"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["report.pdf"]

    @respx.mock
    async def test_download_interrupted_keeps_existing_file_on_overwrite(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """A dropped connection mid-stream must leave the original file intact.

        Before the fix, the download opened dest_file with "wb" (truncating it)
        and the failure path then deleted it.
        """
        existing = tmp_path / "movie.mkv"
        existing.write_bytes(b"original")

        async def _broken_body() -> AsyncIterator[bytes]:
            yield b"partial "
            raise httpx.ReadError("connection reset")

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=[
                httpx.Response(200, json={"success": True, "data": {"files": []}}),
                httpx.Response(
                    200,
                    headers={"content-type": "application/octet-stream"},
                    content=_broken_body(),
                ),
            ]
        )

        with pytest.raises(httpx.ReadError):
            await download_file(
                mock_client,
                path="/video/movie.mkv",
                dest_folder=str(tmp_path),
                overwrite=True,
            )

        assert existing.read_bytes() == b"original"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["movie.mkv"]

    @respx.mock
    async def test_download_overwrite_leaves_no_temp_files(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        existing = tmp_path / "movie.mkv"
        existing.write_bytes(b"old")
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=b"new content",
            headers={"content-type": "application/octet-stream"},
        )

        await download_file(
            mock_client,
            path="/video/movie.mkv",
            dest_folder=str(tmp_path),
            overwrite=True,
        )

        assert existing.read_bytes() == b"new content"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["movie.mkv"]

    @pytest.mark.parametrize(
        "bad_name",
        [
            "../outside.txt",
            "sub/file.txt",
            "sub\\file.txt",
            "..",
            ".",
            "/abs/file.txt",
            "a\x00b",
            # On Windows a drive-qualified name replaces the folder: C:\dl / "D:x" is "D:x".
            "D:evil.txt",
            "C:",
        ],
    )
    @respx.mock
    async def test_download_rejects_filename_outside_dest_folder(
        self, mock_client: DsmClient, tmp_path: Path, bad_name: str
    ) -> None:
        """filename must be a plain name; anything else could escape dest_folder."""
        dest = tmp_path / "downloads"
        dest.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"keep me")
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=b"payload",
            headers={"content-type": "application/octet-stream"},
        )

        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client,
                path="/share/file.txt",
                dest_folder=str(dest),
                filename=bad_name,
                overwrite=True,
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "filename"
        assert not route.called
        assert outside.read_bytes() == b"keep me"
        assert list(dest.iterdir()) == []

    @respx.mock
    async def test_download_refuses_any_name_that_leaves_dest_folder(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """Backstop: whatever the name rules miss, the joined file must sit in dest_folder."""
        dest = tmp_path / "downloads"
        dest.mkdir()
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(content=b"payload")
        with (
            patch(
                "mcp_synology.modules.filestation.transfer._local_name_problem", return_value=None
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await download_file(
                mock_client, path="/share/f.txt", dest_folder=str(dest), filename="../x.txt"
            )
        assert json.loads(str(exc_info.value))["error"]["code"] == "invalid_parameter"
        assert not route.called
        assert not (tmp_path / "x.txt").exists()

    @respx.mock
    async def test_download_rejects_nas_path_ending_in_dotdot(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """Without filename, the local name comes from the NAS path and is checked too."""
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(content=b"x")

        with pytest.raises(ToolError) as exc_info:
            await download_file(mock_client, path="/share/sub/..", dest_folder=str(tmp_path))

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "path"
        assert not route.called

    @staticmethod
    def _stream(body: object) -> list[httpx.Response]:
        """The pre-flight size lookup, then the streamed download."""
        return [
            httpx.Response(200, json={"success": True, "data": {"files": []}}),
            httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=body),
        ]

    @respx.mock
    async def test_download_no_overwrite_never_replaces_a_file_created_meanwhile(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """overwrite=False must hold to the end: a file that appears while the download
        streams (another call, another process) is not replaced (download review)."""
        dest = tmp_path / "report.pdf"

        async def body() -> AsyncIterator[bytes]:
            yield b"first half "
            dest.write_bytes(b"CREATED MEANWHILE")  # appears mid-download
            yield b"second half"

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._stream(body()))
        with pytest.raises(ToolError) as exc_info:
            await download_file(mock_client, path="/docs/report.pdf", dest_folder=str(tmp_path))
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "already_exists"
        assert (error["param"], error["value"]) == ("filename", "report.pdf")  # #189
        assert dest.read_bytes() == b"CREATED MEANWHILE"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["report.pdf"]  # no temp left

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    @respx.mock
    async def test_download_overwrite_keeps_the_files_permissions(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        existing = tmp_path / "secret.txt"
        existing.write_bytes(b"old")
        existing.chmod(0o600)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=b"new", headers={"content-type": "application/octet-stream"}
        )
        await download_file(
            mock_client, path="/docs/secret.txt", dest_folder=str(tmp_path), overwrite=True
        )
        assert existing.read_bytes() == b"new"
        assert existing.stat().st_mode & 0o777 == 0o600

    @respx.mock
    async def test_download_cancelled_midstream_removes_temp(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        existing = tmp_path / "movie.mkv"
        existing.write_bytes(b"original")

        async def body() -> AsyncIterator[bytes]:
            yield b"partial "
            raise asyncio.CancelledError

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._stream(body()))
        with pytest.raises(asyncio.CancelledError):
            await download_file(
                mock_client, path="/video/movie.mkv", dest_folder=str(tmp_path), overwrite=True
            )
        assert existing.read_bytes() == b"original"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["movie.mkv"]

    @respx.mock
    async def test_download_without_hard_links_still_publishes(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """Where links aren't supported (FAT, some network mounts), overwrite=False falls
        back to a final existence check and publishes a new file normally."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=b"data", headers={"content-type": "application/octet-stream"}
        )
        with patch("mcp_synology.core.client.os.link", side_effect=PermissionError("no links")):
            await download_file(mock_client, path="/docs/a.txt", dest_folder=str(tmp_path))
        assert (tmp_path / "a.txt").read_bytes() == b"data"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]

    @respx.mock
    async def test_download_without_hard_links_never_replaces_a_new_file(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        dest = tmp_path / "a.txt"

        async def body() -> AsyncIterator[bytes]:
            yield b"data"
            dest.write_bytes(b"CREATED MEANWHILE")

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=self._stream(body()))
        with (
            patch("mcp_synology.core.client.os.link", side_effect=OSError(errno.EPERM, "no links")),
            pytest.raises(ToolError) as exc_info,
        ):
            await download_file(mock_client, path="/docs/a.txt", dest_folder=str(tmp_path))
        assert json.loads(str(exc_info.value))["error"]["code"] == "already_exists"
        assert dest.read_bytes() == b"CREATED MEANWHILE"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]

    @respx.mock
    async def test_download_cleanup_tolerates_a_missing_temp(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """If the temp file is already gone when a failure is cleaned up, the original
        error still surfaces."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=b"data", headers={"content-type": "application/octet-stream"}
        )

        def publish(tmp_file: Path, dest_file: Path, *, overwrite: bool) -> None:
            tmp_file.unlink()
            raise OSError(errno.EIO, "publish failed")

        with (
            patch("mcp_synology.core.client._publish_download", side_effect=publish),
            pytest.raises(ToolError) as exc_info,
        ):
            await download_file(mock_client, path="/docs/a.txt", dest_folder=str(tmp_path))
        assert "publish failed" in json.loads(str(exc_info.value))["error"]["message"]
        assert list(tmp_path.iterdir()) == []

    @respx.mock
    async def test_download_cleanup_failure_is_logged_and_original_error_kept(
        self, mock_client: DsmClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=b"data", headers={"content-type": "application/octet-stream"}
        )
        real_unlink = Path.unlink

        def unlink(self: Path, missing_ok: bool = False) -> None:
            if self.name.endswith(".part"):
                raise PermissionError(errno.EACCES, "locked")
            real_unlink(self, missing_ok=missing_ok)

        with (
            patch(
                "mcp_synology.core.client._publish_download",
                side_effect=OSError(errno.EIO, "publish failed"),
            ),
            patch.object(Path, "unlink", unlink),
            pytest.raises(ToolError) as exc_info,
        ):
            await download_file(mock_client, path="/docs/a.txt", dest_folder=str(tmp_path))
        assert "publish failed" in json.loads(str(exc_info.value))["error"]["message"]
        assert "Failed to clean up partial download" in caplog.text

    @respx.mock
    async def test_download_is_flushed_to_disk_before_publishing(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=b"data", headers={"content-type": "application/octet-stream"}
        )
        with patch("mcp_synology.core.client.os.fsync") as fsync:
            await download_file(mock_client, path="/docs/a.txt", dest_folder=str(tmp_path))
        fsync.assert_called_once()

    @_needs_non_root
    @respx.mock
    async def test_download_write_permission_error(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """OSError writing local file should raise ToolError.

        Simulates what happens when the filename contains OS-illegal characters
        (e.g., ':' on Windows) or the directory is read-only, by attempting to
        write to a read-only directory.
        """
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o555)

        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=b"data",
            headers={"content-type": "application/octet-stream"},
        )

        try:
            with pytest.raises(ToolError) as exc_info:
                await download_file(
                    mock_client,
                    path="/video/file.mkv",
                    dest_folder=str(readonly_dir),
                    filename="test.mkv",
                )
            body = json.loads(str(exc_info.value))
            assert body["status"] == "error"
            assert body["error"]["code"] == "filesystem_error"
            assert body["error"]["param"] == "dest_folder"
            assert body["error"]["value"] == str(readonly_dir)
        finally:
            # Restore permissions for cleanup
            readonly_dir.chmod(0o755)

    @respx.mock
    async def test_download_insufficient_disk_space_preflight(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """Pre-flight disk space check should catch insufficient space."""
        # Mock getinfo to return a huge file size
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=[
                # First call: getinfo returns file metadata
                httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "files": [
                                {
                                    "path": "/video/huge.mkv",
                                    "additional": {"size": 999_999_999_999_999},
                                }
                            ]
                        },
                    },
                ),
                # Second call (download) should not be reached
            ]
        )

        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client,
                path="/video/huge.mkv",
                dest_folder=str(tmp_path),
            )
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "disk_full"

    @respx.mock
    async def test_the_clients_free_space_check_reports_disk_full(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """No size from getinfo, so download_file's own check is skipped and the client's
        catches it: `disk_full`, retryable, since #462 gave it ENOSPC (it was
        `filesystem_error` with a filename suggestion)."""
        from unittest.mock import MagicMock, patch

        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={"success": True, "data": {"files": [{"path": "/video/a.mkv"}]}},
                ),
                httpx.Response(200, content=b"x" * 5000, headers={"content-length": "5000"}),
            ]
        )
        with (
            patch("mcp_synology.core.client.shutil.disk_usage", return_value=MagicMock(free=1)),
            pytest.raises(ToolError) as exc_info,
        ):
            await download_file(mock_client, path="/video/a.mkv", dest_folder=str(tmp_path))
        error = json.loads(str(exc_info.value))["error"]
        assert (error["code"], error["retryable"]) == ("disk_full", True)

    async def test_download_enospc_reports_disk_full(
        self, mock_client: DsmClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OSError with errno=ENOSPC during write emits disk_full, not filesystem_error.

        The pre-flight branch catches most disk-full cases up front using
        ``shutil.disk_usage``. The OSError fallback covers races (disk
        filled between the pre-flight and the actual write) or cases
        where the pre-flight was skipped because getinfo failed. Both
        branches must emit the same code so smart clients dispatch
        consistently — this test exercises the fallback path.
        """

        async def _raise_enospc(*args: object, **kwargs: object) -> int:
            raise OSError(
                errno.ENOSPC,
                "No space left on device",
                str(tmp_path / ".mcp-synology-0123456789abcdef.part"),
            )

        monkeypatch.setattr(mock_client, "download", _raise_enospc)

        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client,
                path="/video/file.mkv",
                dest_folder=str(tmp_path),
            )
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "disk_full"
        assert body["error"]["retryable"] is True
        assert "No space left" in body["error"]["message"]
        assert ".part" not in body["error"]["message"]  # QA R1 (#178): no temp path

    async def test_download_non_enospc_oserror_reports_filesystem_error(
        self, mock_client: DsmClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OSError with any errno other than ENOSPC falls through to filesystem_error.

        Complements the ENOSPC test above by exercising the other side
        of the errno branch. Uses EACCES (permission denied) rather than
        relying on a readonly directory so the test is independent of
        filesystem semantics.
        """

        async def _raise_eacces(*args: object, **kwargs: object) -> int:
            raise OSError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(mock_client, "download", _raise_eacces)

        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client,
                path="/video/file.mkv",
                dest_folder=str(tmp_path),
            )
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "filesystem_error"
        assert body["error"]["retryable"] is False

    @respx.mock
    async def test_download_dangling_symlink_is_refused_up_front(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """QA R1 (#178): exists() follows links, so a dangling link passed the check, the
        whole file streamed, and the error blamed a file "created while the download was
        in progress"."""
        (tmp_path / "report.txt").symlink_to(tmp_path / "missing-target")
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(content=b"x")

        with pytest.raises(ToolError) as exc_info:
            await download_file(mock_client, path="/docs/report.txt", dest_folder=str(tmp_path))

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "already_exists"
        assert "while the download" not in body["error"]["message"]
        # Refused before the download: only the getinfo check, which picks the local
        # name (a folder's gets ".zip"), went to the NAS.
        assert [c.request.url.params.get("method") for c in route.calls] == ["getinfo"]

    @pytest.mark.parametrize("overwrite", [False, True])
    @respx.mock
    async def test_download_directory_at_destination_is_refused_up_front(
        self, mock_client: DsmClient, tmp_path: Path, overwrite: bool
    ) -> None:
        """QA R1 (#178): a folder where the file would go can't be replaced; say so
        before downloading, naming the destination, not the internal temp file."""
        (tmp_path / "report.txt").mkdir()
        route = respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(content=b"x")

        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client,
                path="/docs/report.txt",
                dest_folder=str(tmp_path),
                overwrite=overwrite,
            )

        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "invalid_parameter"
        assert "is a folder" in error["message"]
        assert str(tmp_path / "report.txt") in error["message"]
        assert "filename" in error["suggestion"]
        # Refused before the download: only the getinfo check, which picks the local
        # name (a folder's gets ".zip"), went to the NAS.
        assert [c.request.url.params.get("method") for c in route.calls] == ["getinfo"]
        assert (tmp_path / "report.txt").is_dir()

    async def test_download_write_error_names_the_destination_not_the_temp_file(
        self, mock_client: DsmClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA R1 (#178): an OSError from publishing carries the temp path; the message
        names the file the caller asked for."""
        tmp_name = str(tmp_path / ".mcp-synology-0123456789abcdef.part")

        async def _raise(*args: object, **kwargs: object) -> int:
            raise OSError(errno.EACCES, "Permission denied", tmp_name, str(tmp_path / "f.mkv"))

        monkeypatch.setattr(mock_client, "download", _raise)
        with pytest.raises(ToolError) as exc_info:
            await download_file(mock_client, path="/video/f.mkv", dest_folder=str(tmp_path))

        message = json.loads(str(exc_info.value))["error"]["message"]
        assert ".part" not in message
        assert str(tmp_path / "f.mkv") in message
        assert "Permission denied" in message

    @respx.mock
    async def test_download_progress_callback(self, mock_client: DsmClient, tmp_path: Path) -> None:
        """Progress callback should be called during download."""
        file_content = b"x" * 1024
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=file_content,
            headers={
                "content-type": "application/octet-stream",
                "content-length": str(len(file_content)),
            },
        )

        progress_calls: list[tuple[int, int | None]] = []

        async def _track_progress(current: int, total: int | None) -> None:
            progress_calls.append((current, total))

        result = await download_file(
            mock_client,
            path="/video/small.bin",
            dest_folder=str(tmp_path),
            progress_callback=_track_progress,
        )
        assert "[+]" in result
        assert len(progress_calls) > 0
        # Last call should have current == total bytes
        last_current, last_total = progress_calls[-1]
        assert last_current == len(file_content)
        assert last_total == len(file_content)


class TestLargeFileWarnings:
    @respx.mock
    async def test_upload_large_file_timeout_warning(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """Large uploads should include a timeout warning note."""
        local_file = tmp_path / "big.bin"
        local_file.write_bytes(b"\0")

        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(json={"success": True, "data": {}})

        # Temporarily lower the threshold so our tiny file triggers the warning
        with patch("mcp_synology.modules.filestation.transfer._LARGE_FILE_THRESHOLD", 0):
            result = await upload_file(
                mock_client,
                local_path=str(local_file),
                dest_folder="/video/uploads",
            )

        assert "[+]" in result
        assert "upload_timeout" in result

    @respx.mock
    async def test_upload_small_file_no_warning(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """Small uploads should NOT include a timeout warning."""
        local_file = tmp_path / "small.txt"
        local_file.write_text("hello")

        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(json={"success": True, "data": {}})

        result = await upload_file(
            mock_client,
            local_path=str(local_file),
            dest_folder="/video/uploads",
        )
        assert "[+]" in result
        assert "timeout" not in result.lower()


class TestLocalPathConfinement:
    """Upload and download refuse local paths outside the allowed folders, before any NAS
    request, with the allowed folders and the setting that widens them in the error."""

    @respx.mock
    async def test_upload_outside_allowed_roots_is_refused(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        allowed = tmp_path / "Downloads"
        allowed.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"key")
        route = respx.route().respond(json={"success": True, "data": {}})

        with pytest.raises(ToolError) as exc_info:
            await upload_file(
                mock_client,
                local_path=str(secret),
                dest_folder="/share",
                allowed_roots=[allowed.resolve()],
            )

        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "permission_denied"
        assert error["param"] == "local_path"
        assert str(allowed.resolve()) in error["message"]
        assert "allowed_local_paths" in error["suggestion"]
        assert not route.called

    @respx.mock
    async def test_upload_inside_allowed_roots_proceeds(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        f = tmp_path / "ok.txt"
        f.write_bytes(b"data")
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(json={"success": True, "data": {}})

        result = await upload_file(
            mock_client, local_path=str(f), dest_folder="/share", allowed_roots=[tmp_path.resolve()]
        )
        assert "Uploaded ok.txt" in result

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    @respx.mock
    async def test_upload_through_a_symlink_out_of_the_root_is_refused(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        allowed = tmp_path / "Downloads"
        allowed.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"key")
        (allowed / "innocent.txt").symlink_to(secret)
        route = respx.route().respond(json={"success": True, "data": {}})

        with pytest.raises(ToolError) as exc_info:
            await upload_file(
                mock_client,
                local_path=str(allowed / "innocent.txt"),
                dest_folder="/share",
                allowed_roots=[allowed.resolve()],
            )
        assert json.loads(str(exc_info.value))["error"]["code"] == "permission_denied"
        assert not route.called

    @respx.mock
    async def test_download_outside_allowed_roots_is_refused(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        allowed = tmp_path / "Downloads"
        allowed.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        route = respx.route().respond(content=b"x")

        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client,
                path="/share/f.txt",
                dest_folder=str(elsewhere),
                allowed_roots=[allowed.resolve()],
            )

        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "permission_denied"
        assert error["param"] == "dest_folder"
        assert not route.called
        assert list(elsewhere.iterdir()) == []

    @respx.mock
    async def test_download_refusal_does_not_reveal_whether_the_folder_exists(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client,
                path="/share/f.txt",
                dest_folder=str(tmp_path / "no" / "such"),
                allowed_roots=[(tmp_path / "Downloads").resolve()],
            )
        assert json.loads(str(exc_info.value))["error"]["code"] == "permission_denied"

    @respx.mock
    async def test_download_expands_home_in_dest_folder(
        self, mock_client: DsmClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#177: ``~`` in dest_folder means the home folder, not a folder named ``~``."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        (tmp_path / "Downloads").mkdir()
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=b"payload", headers={"content-type": "application/octet-stream"}
        )

        await download_file(
            mock_client,
            path="/share/f.txt",
            dest_folder="~/Downloads",
            allowed_roots=[(tmp_path / "Downloads").resolve()],
        )
        assert (tmp_path / "Downloads" / "f.txt").read_bytes() == b"payload"


class TestLocalPathConfinementErrors:
    """QA R1 (#186): unresolvable paths get a structured error; refusals don't reveal
    where a path outside the allowed folders leads."""

    async def test_nul_in_dest_folder_is_invalid_parameter(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client,
                path="/share/f.txt",
                dest_folder=str(tmp_path / "a\x00b"),
                allowed_roots=[tmp_path.resolve()],
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "invalid_parameter"
        assert error["param"] == "dest_folder"

    async def test_nul_in_local_path_is_invalid_parameter(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await upload_file(
                mock_client,
                local_path=str(tmp_path / "a\x00b"),
                dest_folder="/share",
                allowed_roots=[tmp_path.resolve()],
            )
        assert json.loads(str(exc_info.value))["error"]["param"] == "local_path"

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    @respx.mock
    async def test_symlink_loop_gets_a_structured_error_on_every_python(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """3.11/3.12 raise RuntimeError from resolve(); 3.13 resolves the loop to itself.
        Either way the caller gets a ToolError with a code, never a raw exception."""
        loop = tmp_path / "loop"
        loop.symlink_to(loop)
        respx.route().respond(json={"success": True, "data": {}})
        with pytest.raises(ToolError) as exc_info:
            await upload_file(
                mock_client,
                local_path=str(loop),
                dest_folder="/share",
                allowed_roots=[tmp_path.resolve()],
            )
        assert json.loads(str(exc_info.value))["error"]["code"]

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    async def test_refusal_outside_roots_does_not_reveal_a_link_target(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        allowed = tmp_path / "Downloads"
        allowed.mkdir()
        secret = tmp_path / "secret-outside"
        secret.mkdir()
        (tmp_path / "innocent-link").symlink_to(secret)

        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client,
                path="/share/f.txt",
                dest_folder=str(tmp_path / "innocent-link"),
                allowed_roots=[allowed.resolve()],
            )
        message = json.loads(str(exc_info.value))["error"]["message"]
        assert "secret-outside" not in message
        assert "resolves to" not in message

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    async def test_refusal_explains_a_link_inside_a_root_that_leads_out(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        allowed = tmp_path / "Downloads"
        allowed.mkdir()
        (tmp_path / "out").mkdir()
        (allowed / "shortcut").symlink_to(tmp_path / "out")

        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client,
                path="/share/f.txt",
                dest_folder=str(allowed / "shortcut"),
                allowed_roots=[allowed.resolve()],
            )
        message = json.loads(str(exc_info.value))["error"]["message"]
        assert f"resolves to {(tmp_path / 'out').resolve()}" in message

    @pytest.mark.parametrize("op", ["upload", "download"])
    async def test_unknown_home_user_refused_like_outside(
        self, mock_client: DsmClient, tmp_path: Path, op: str
    ) -> None:
        """With allowed folders, an unknown ``~user`` gets the same refusal as an existing
        user's home outside them, so the reply doesn't reveal which local users exist
        (#260, as Download Station's torrent_file_path in #257)."""
        roots = [tmp_path.resolve()]
        errors = []
        for home in ("~mcp_no_such_user_xyz", "~root"):
            with pytest.raises(ToolError) as exc_info:
                if op == "upload":
                    await upload_file(
                        mock_client,
                        local_path=f"{home}/f.txt",
                        dest_folder="/share",
                        allowed_roots=roots,
                    )
                else:
                    await download_file(
                        mock_client,
                        path="/share/f.txt",
                        dest_folder=f"{home}/dl",
                        allowed_roots=roots,
                    )
            error = json.loads(str(exc_info.value))["error"]
            error["message"] = error["message"].replace(home, "~USER")
            error["value"] = error["value"].replace(home, "~USER")
            errors.append(error)
        assert errors[0]["code"] == "permission_denied"
        assert errors[0] == errors[1]
        assert ".." not in errors[0]["message"]  # QA R2 (#186): no doubled period

    async def test_unknown_home_user_without_roots_is_invalid_parameter(
        self, mock_client: DsmClient
    ) -> None:
        """Direct (unconfined) callers expand ``~`` too; an unknown user is still structured."""
        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client, path="/share/f.txt", dest_folder="~mcp_no_such_user_xyz/dl"
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "invalid_parameter"
        assert error["param"] == "dest_folder"
