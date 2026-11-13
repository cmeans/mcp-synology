"""Tests for modules/filestation/transfer.py — upload_file, download_file."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import sys
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core import display
from mcp_synology.core.formatting import DOWNLOADED_CONTENTS_ARE_DATA, ESCAPED_NAME_ADVICE
from mcp_synology.modules.filestation.transfer import (
    download_file,
    upload_file,
)
from tests.conftest import BASE_URL, skip_unless_the_locale_can_encode

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

    # An empty filename counts as none: the local name is used, and checked (pre-QA on #470).
    @pytest.mark.parametrize("extra", [{}, {"filename": ""}], ids=["no-filename", "empty"])
    async def test_upload_refuses_a_local_name_that_isnt_one(
        self, mock_client: DsmClient, tmp_path: Path, extra: dict[str, str]
    ) -> None:
        """With no filename, the local file's own name follows the same rule (pre-QA on
        #470): ` .. ` is a valid local name."""
        local_file = tmp_path / " .. "
        local_file.write_text("x")
        with pytest.raises(ToolError) as exc:
            await upload_file(
                mock_client, local_path=str(local_file), dest_folder="/video/up", **extra
            )
        error = json.loads(str(exc.value))["error"]
        # Named as the local file, not `filename`, which wasn't passed (QA round 3 on #470).
        assert (error["code"], error["param"]) == ("invalid_parameter", "local_path")
        assert error["message"] == (
            "Upload failed: the local file's name (' .. ') should be just a file name, but it "
            "is '.' or '..'."
        )
        assert error["suggestion"] == "Pass filename with a plain name for the file on the NAS."
        assert error["value"] == str(local_file)  # the argument named, as passed

    @pytest.mark.parametrize("filename", ["report.pdf ", " report.pdf", "report.pdf\u00a0"])
    async def test_upload_refuses_a_padded_filename(
        self, mock_client: DsmClient, tmp_path: Path, filename: str
    ) -> None:
        """DSM would keep the padding, making a file other than `report.pdf` (#488)."""
        local_file = tmp_path / "report.pdf"
        local_file.write_text("x")
        with pytest.raises(ToolError) as exc:
            await upload_file(
                mock_client, local_path=str(local_file), dest_folder="/video/up", filename=filename
            )
        error = json.loads(str(exc.value))["error"]
        assert (error["param"], error["value"]) == ("filename", filename)
        assert error["message"] == (
            "Upload failed: filename should be just a file name, but it starts or ends with "
            "whitespace."
        )

    async def test_upload_refuses_a_padded_local_name_without_filename(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """A Linux file named `report.pdf ` would keep its padding on the NAS (#488);
        filename gives it a plain name."""
        local_file = tmp_path / "report.pdf "
        local_file.write_text("x")
        with pytest.raises(ToolError) as exc:
            await upload_file(mock_client, local_path=str(local_file), dest_folder="/video/up")
        error = json.loads(str(exc.value))["error"]
        assert (error["param"], error["value"]) == ("local_path", str(local_file))
        assert error["message"] == (
            "Upload failed: the local file's name ('report.pdf ') should be just a file name, "
            "but it starts or ends with whitespace."
        )
        assert error["suggestion"] == "Pass filename with a plain name for the file on the NAS."

    @pytest.mark.parametrize("name", ["rep\u200bort.pdf", "辛\U000e0100葛\U000e0101"])
    async def test_a_local_name_the_unicode_rules_refuse_keeps_the_filename_advice(
        self,
        mock_client: DsmClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
    ) -> None:
        """A local file name can't be retyped from here: the advice stays 'pass filename',
        not 'retype' or 'write fewer variants' (pre-QA and QA round 4 on #515)."""

        skip_unless_the_locale_can_encode(name)
        monkeypatch.setattr(display, "SELECTOR_LIMIT", 1)
        local_file = tmp_path / name
        local_file.write_text("x")
        with pytest.raises(ToolError) as exc:
            await upload_file(mock_client, local_path=str(local_file), dest_folder="/video/up")
        error = json.loads(str(exc.value))["error"]
        assert error["suggestion"] == "Pass filename with a plain name for the file on the NAS."

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
        # No filename was passed: the conflict is still the file name, as download_file
        # reports it (#189), with the local file's name as the value.
        assert body["error"]["param"] == "filename"
        assert body["error"]["value"] == "test.txt"

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
    async def test_a_padded_destination_is_quoted(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """#688: the reply names the folder it uploaded to, quoted when padded (#519's rule)."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("content")
        missing = {"files": [{"code": 408, "path": "/v /test.txt"}]}
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(json={"success": True, "data": missing})
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(json={"success": True, "data": {}})
        result = await upload_file(mock_client, local_path=str(local_file), dest_folder="/v ")
        assert result.startswith(f"[+] Uploaded test.txt (7 B) to {'/v '!r}/"), result

    @respx.mock
    async def test_a_name_with_a_tab_is_quoted_in_every_line(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """#688 (pre-QA): a tab is allowed in a name, and the reply's lines all quote it."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("content")
        there = {"files": [{"isdir": False, "path": "/v/a\tb.txt"}]}
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(json={"success": True, "data": there})
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(json={"success": True, "data": {}})
        name = "a\tb.txt"
        result = await upload_file(
            mock_client,
            local_path=str(local_file),
            dest_folder="/v",
            filename=name,
            overwrite=True,
        )
        assert result.startswith(f"[+] Uploaded {name!r} (7 B) to /v/"), result
        assert f"It replaced the {name!r} that was there." in result, result
        assert "\t" not in result, result

    @respx.mock
    async def test_couldnt_check_quotes_a_name_with_a_tab(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """#688 (pre-QA): the note for a lookup that failed names the file as line 1 does."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("content")
        name = "a\tb.txt"
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 119}}
        )
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(json={"success": True, "data": {}})
        result = await upload_file(
            mock_client, local_path=str(local_file), dest_folder="/v", filename=name, overwrite=True
        )
        assert f"Couldn't check whether a file named {name!r} was already there." in result, result

    @respx.mock
    async def test_already_exists_quotes_a_name_with_a_tab(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """#688 (pre-QA): the refusal names the file as the reply would, quoted."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("content")
        name = "a\tb.txt"
        # DSM skips an upload whose name is there (overwrite=false): blSkip.
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"blSkip": True}}
        )
        with pytest.raises(ToolError) as refused:
            await upload_file(
                mock_client, local_path=str(local_file), dest_folder="/v", filename=name
            )
        message = json.loads(str(refused.value))["error"]["message"]
        assert message == f"Upload failed: File {name!r} already exists in /v.", message

    @pytest.mark.parametrize(
        ("there", "said"),
        [(True, True), (False, False), (None, False), ("folder", False)],
    )
    @respx.mock
    async def test_overwrite_says_when_it_replaced_a_file(
        self, mock_client: DsmClient, tmp_path: Path, there: bool | str | None, said: bool
    ) -> None:
        """#620 (0.11.0rc3 live test): an overwrite read like a plain upload. ``there``: the
        file was there (True), wasn't (False), the lookup failed (None), or a folder has the
        name ("folder"): only a file there is said to be replaced."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("updated content")
        info: dict[str, object]
        if there is None:
            info = {"success": False, "error": {"code": 119}}
        elif there:
            isdir = there == "folder"
            info = {"success": True, "data": {"files": [{"isdir": isdir, "path": "/v/test.txt"}]}}
        else:
            info = {"success": True, "data": {"files": [{"code": 408, "path": "/v/test.txt"}]}}
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(json=info)
        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(json={"success": True, "data": {}})
        result = await upload_file(
            mock_client, local_path=str(local_file), dest_folder="/v", overwrite=True
        )
        assert result.startswith("[+] Uploaded test.txt"), result
        assert (
            "\n    It replaced the test.txt that was there. A file replaced this way may not be "
            "in the recycle bin." in result
        ) is said, result
        # A failed lookup says so (QA round 1 on #623).
        assert ("Couldn't check whether a file named test.txt was already there." in result) is (
            there is None
        ), result

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


_OCTET = "application/octet-stream"


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
        # The last line says what's in the file is data (#640).
        assert result.splitlines()[-1] == DOWNLOADED_CONTENTS_ARE_DATA

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
    async def test_download_overwrite_success(
        self, mock_client: DsmClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        existing = tmp_path / "movie.mkv"
        existing.write_text("old content")

        new_content = b"new file content"
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=new_content,
            headers={"content-type": "application/octet-stream"},
        )

        with caplog.at_level("DEBUG", logger="mcp_synology.modules.filestation.transfer"):
            result = await download_file(
                mock_client,
                path="/video/movie.mkv",
                dest_folder=str(tmp_path),
                overwrite=True,
            )
        assert "[+]" in result
        assert existing.read_bytes() == new_content
        assert f"Downloaded /video/movie.mkv to {existing} (replaced a file there: True)" in (
            caplog.text
        )
        # #625: an overwrite says it replaced the file (worded like upload_file's note in #623).
        assert result.splitlines()[1:-1] == ["    It replaced the movie.mkv that was there."]

    @respx.mock
    async def test_download_overwrite_with_nothing_there_says_nothing_extra(
        self, mock_client: DsmClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=b"data", headers={"content-type": "application/octet-stream"}
        )
        with caplog.at_level("DEBUG", logger="mcp_synology.modules.filestation.transfer"):
            result = await download_file(
                mock_client, path="/video/movie.mkv", dest_folder=str(tmp_path), overwrite=True
            )
        assert result.startswith("[+] Downloaded movie.mkv"), result
        assert "replaced" not in result, result
        assert "(replaced a file there: False)" in caplog.text

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
        assert "can't act on it" not in err["suggestion"]  # a plain path gets no escape advice

    @respx.mock
    async def test_download_missing_escaped_name_says_it_cant_be_used(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """#604: a name copied from a reply that showed it escaped names nothing, and listing
        again shows the same escape, so the advice says so (the live-test rehearsal)."""
        self._route(
            httpx.Response(
                200, json={"success": True, "data": {"files": [{"code": 408, "path": "/v/x"}]}}
            ),
            httpx.Response(502, text="<html>DSM error page</html>"),
        )
        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client,
                path="/video/invoice\\u202efdp.exe",
                dest_folder=str(tmp_path),
                filename="invoice.pdf",  # its own name, with a backslash, is refused locally
            )
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == "not_found"
        assert err["suggestion"].endswith(ESCAPED_NAME_ADVICE), err["suggestion"]

    @staticmethod
    def _file_info(size: int) -> httpx.Response:
        info = {
            "files": [
                {"path": "/video/report.pdf", "name": "report.pdf", "additional": {"size": size}}
            ]
        }
        return httpx.Response(200, json={"success": True, "data": info})

    @respx.mock
    @pytest.mark.parametrize(
        ("download", "code"),
        [
            # A proxy's typeless redirect, and a 204: once saved as an empty file.
            (httpx.Response(302, headers={"location": "https://portal.example/"}), "unavailable"),
            (httpx.Response(204), "unavailable"),
            # A body shorter than the file getinfo reported (1000 bytes).
            (httpx.Response(200, content=b"y" * 10, headers={"content-type": _OCTET}),
             "unavailable"),
            # DSM's error envelope under a type DSM never sends a file as.
            (httpx.Response(200, content=b'{"success":false,"error":{"code":105}}',
                            headers={"content-type": "text/plain"}), "permission_denied"),
            (httpx.Response(200, content=b'{"success":false,"error":{"code":105}}'),
             "permission_denied"),
        ],
        ids=["302", "204", "short", "envelope-text", "envelope-untyped"],
    )  # fmt: skip
    async def test_an_answer_that_isn_t_the_file_keeps_the_local_one(
        self, mock_client: DsmClient, tmp_path: Path, download: httpx.Response, code: str
    ) -> None:
        """#642: with overwrite=true, each of these replaced the user's file and replied
        "Downloaded"; now it's an error and the local file is as it was, with no temp file."""
        self._route(self._file_info(1000), download)
        existing = tmp_path / "report.pdf"
        existing.write_bytes(b"the user's report")
        with pytest.raises(ToolError) as exc_info:
            await download_file(
                mock_client, path="/video/report.pdf", dest_folder=str(tmp_path), overwrite=True
            )
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == code, err
        assert existing.read_bytes() == b"the user's report"
        assert [p.name for p in tmp_path.iterdir()] == ["report.pdf"]

    @respx.mock
    async def test_a_file_that_grew_while_it_downloaded_is_kept(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """#642's size check refuses only a shorter body: a log appended to while it
        downloads is longer than getinfo's size, and is saved."""
        self._route(
            self._file_info(1000),
            httpx.Response(
                200, content=b"z" * 1200, headers={"content-type": "application/octet-stream"}
            ),
        )
        out = await download_file(mock_client, path="/video/report.pdf", dest_folder=str(tmp_path))
        assert (tmp_path / "report.pdf").read_bytes() == b"z" * 1200
        assert "Downloaded report.pdf (1.2 KiB)" in out

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
    @pytest.mark.parametrize("code", [[407], {"a": 1}, "407", True])
    async def test_download_a_malformed_code_cant_tell(
        self, mock_client: DsmClient, tmp_path: Path, code: Any
    ) -> None:
        """QA r2 on #716: an unhashable code raised TypeError; it can't tell, so the download
        is tried and DSM's own answer stands."""
        calls = self._route(
            httpx.Response(
                200, json={"success": True, "data": {"files": [{"code": code, "path": "/v"}]}}
            ),
            httpx.Response(502, text="<html>DSM error page</html>"),
        )
        with pytest.raises(ToolError):
            await download_file(mock_client, path="/video/x.mkv", dest_folder=str(tmp_path))
        assert calls == ["getinfo", "download"]

    @respx.mock
    @pytest.mark.parametrize("code", [407, 105])
    async def test_download_unreadable_path_refused_up_front(
        self, mock_client: DsmClient, tmp_path: Path, code: int
    ) -> None:
        """A path the user can't read (getinfo's 407 entry, or 105, as every getinfo reader
        takes it, #716): DSM would answer the download with the same bare 502 as a missing
        file (vdsm 7.1, 7.2.2), so it's refused first."""
        calls = self._route(
            httpx.Response(
                200, json={"success": True, "data": {"files": [{"code": code, "path": "/v"}]}}
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
        assert out.splitlines()[-1] == DOWNLOADED_CONTENTS_ARE_DATA  # #640

    @respx.mock
    async def test_a_padded_folder_is_quoted(self, mock_client: DsmClient, tmp_path: Path) -> None:
        """#688: the reply names the folder it downloaded, quoted when padded (#519's rule)."""
        self._route(self._folder_info("/video/shows "), httpx.Response(200, content=b"PK\x03\x04"))
        out = await download_file(mock_client, path="/video/shows ", dest_folder=str(tmp_path))
        assert f"Downloaded the folder {'/video/shows '!r} as a ZIP archive of it" in out, out

    @respx.mock
    async def test_a_name_with_a_tab_is_quoted_in_every_line(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """#688 (pre-QA r2): the NAS name, the local file's name and the replaced line quote a
        tab alike; the local path is quoted whole, by shown()'s rule (#691)."""
        name = "a\tb.txt"
        (tmp_path / name).write_text("old")
        file_info = {"path": f"/video/{name}", "name": name, "isdir": False}
        self._route(
            httpx.Response(200, json={"success": True, "data": {"files": [file_info]}}),
            httpx.Response(200, content=b"new"),
        )
        out = await download_file(
            mock_client, path=f"/video/{name}", dest_folder=str(tmp_path), overwrite=True
        )
        # The whole local path quoted, as shown() quotes a NAS one (#691).
        assert f"Downloaded {name!r} (3 B) to {str(tmp_path / name)!r}" in out, out
        assert f"It replaced the {name!r} that was there." in out, out
        assert "\t" not in out, out

    @respx.mock
    async def test_a_padded_share_root_is_quoted(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """#688 (pre-QA r2): the whole-shared-folder refusal quotes a padded share too."""
        self._route(self._folder_info("/video "), httpx.Response(502))
        with pytest.raises(ToolError) as exc_info:
            await download_file(mock_client, path="/video ", dest_folder=str(tmp_path))
        message = json.loads(str(exc_info.value))["error"]["message"]
        assert message.startswith(f"Download failed: {'/video '!r} is a whole shared folder"), (
            message
        )

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
        out = await download_file(
            mock_client, path="/video/shows", dest_folder=str(tmp_path), overwrite=True
        )
        assert (tmp_path / "shows.zip").read_bytes() == b"PK\x03\x04"
        assert "\n    It replaced the shows.zip that was there." in out, out

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
        catches it: `disk_full`, since #462 gave it ENOSPC (it was `filesystem_error` with a
        filename suggestion), not retryable until space is freed (#490)."""
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
        assert (error["code"], error["retryable"]) == ("disk_full", False)

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
        assert body["error"]["retryable"] is False
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

    @respx.mock
    @pytest.mark.parametrize("target", ["missing-target", "real.txt"])
    async def test_download_overwrite_over_a_symlink_says_it_replaced_the_link(
        self, mock_client: DsmClient, tmp_path: Path, target: str
    ) -> None:
        """QA round 1 on #626: a dangling link is a name that's there (lexists, not exists),
        so the note says it was replaced. Pre-QA r3: a link is replaced, not its target."""
        (tmp_path / "real.txt").write_bytes(b"keep")
        (tmp_path / "report.txt").symlink_to(tmp_path / target)
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=b"new", headers={"content-type": "application/octet-stream"}
        )
        result = await download_file(
            mock_client, path="/docs/report.txt", dest_folder=str(tmp_path), overwrite=True
        )
        assert result.splitlines()[1:-1] == [
            "    It replaced the symlink report.txt that was there (its target is untouched)."
        ]
        assert (tmp_path / "real.txt").read_bytes() == b"keep"
        assert not (tmp_path / "report.txt").is_symlink()
        assert (tmp_path / "report.txt").read_bytes() == b"new"

    @respx.mock
    async def test_a_replaced_symlink_with_a_tab_in_its_name_is_quoted(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """QA round 1 on #690: the replaced-symlink line quotes the name as line 1 does."""
        name = "a\tb.txt"
        (tmp_path / "real.txt").write_bytes(b"keep")
        (tmp_path / name).symlink_to(tmp_path / "real.txt")
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            content=b"new", headers={"content-type": "application/octet-stream"}
        )
        result = await download_file(
            mock_client, path=f"/docs/{name}", dest_folder=str(tmp_path), overwrite=True
        )
        assert (
            f"    It replaced the symlink {name!r} that was there (its target is untouched)."
            in result.splitlines()
        ), result
        assert "\t" not in result, result

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


class TestLocalPathsShown:
    """#691: a local path in a reply or error is written by `shown`'s rule for a NAS path
    (`shown_local`): as is, or quoted whole when a part holds a tab, padding or a character
    that draws nothing, the caller's own refused path included."""

    TAB = "a\tb.mkv"

    def test_shown_local(self, tmp_path: Path) -> None:
        from mcp_synology.core.display import shown_local

        assert shown_local(tmp_path / "plain.txt") == str(tmp_path / "plain.txt")
        assert shown_local(tmp_path / self.TAB) == repr(str(tmp_path / self.TAB))
        assert shown_local(tmp_path / " pad") == repr(str(tmp_path / " pad"))
        assert shown_local(Path("/")) == os.fspath(Path("/"))  # "\\" on Windows (pre-QA r4)
        # A folder part counts too, and the whole path is quoted (pre-QA r1, r2).
        assert shown_local("/home/ x/f.txt") == repr("/home/ x/f.txt")
        assert shown_local("/home/a, b/f.txt") == "'/home/a, b/f.txt'"
        # Not normalized: the caller's text as sent (pre-QA r2).
        assert shown_local("x/./y//z/") == "x/./y//z/"
        assert shown_local("") == "''"
        # A part that is itself quoted can't pass for a quoted part (pre-QA r2): it's plain.
        assert shown_local("/h/'a '/f") == "/h/'a '/f"
        assert shown_local("/h/a /f") == repr("/h/a /f")
        # A path that starts or ends with a quote is quoted, so it can't pass for a quoted
        # path (pre-QA r3).
        assert shown_local("'/home/x/a.txt'") == "\"'/home/x/a.txt'\""

    def test_a_name_starting_or_ending_with_a_quote_is_quoted(self) -> None:
        """Pre-QA r4: shown() wrote ``'a.jpg'`` raw, which reads as a quoted ``a.jpg``; NAS and
        local names alike (upload's and rename's replies, Photos' rows)."""
        from mcp_synology.core.display import shown

        assert shown("'a.jpg'") == "\"'a.jpg'\""
        assert shown('say "hi"') == "'say \"hi\"'"
        assert shown("/s/it's here") == "/s/it's here"  # a quote inside reads as part of it
        # Backticks and typographic quotes read as quoting too (pre-QA r5).
        for wrapped in ("`a.jpg`", "\u2018a.jpg\u2019", "\u201ca.jpg\u201d"):
            assert shown(wrapped) == repr(wrapped), wrapped

    @pytest.mark.parametrize(
        "mark",
        # #711: every character with Unicode's Quotation_Mark property, at either end.
        # Guillemets, low and reversed quotes, CJK corner brackets and their white forms, the
        # double prime quotes, vertical and fullwidth forms, and U+2E42.
        [chr(c) for c in (0x00AB, 0x00BB, 0x2039, 0x203A, 0x201A, 0x201B, 0x201E, 0x201F)]
        + [chr(c) for c in (0x2E42, 0x300C, 0x300D, 0x300E, 0x300F, 0x301D, 0x301E, 0x301F)]
        + [chr(c) for c in (0xFE41, 0xFE42, 0xFE43, 0xFE44, 0xFF02, 0xFF07, 0xFF62, 0xFF63)],
    )
    def test_every_quotation_mark_reads_as_quoting(self, mark: str) -> None:
        from mcp_synology.core.display import edge_quote, shown, shown_local

        # A bare name starting with one, a path ending with one (`edge_quote` reads the text's
        # ends, as `shown` writes the whole path).
        for text in (f"{mark}a.jpg", f"/s/a{mark}"):
            assert edge_quote(text), text
            assert shown(text) == repr(text), text
        assert shown_local(f"/home/u/a{mark}") == repr(f"/home/u/a{mark}")
        assert not edge_quote(f"/s/a{mark}b")  # inside a name it's part of it
        assert not edge_quote(f"/s/a{mark}\n")  # the end is the text's, not before a newline

    def test_quotation_marks_are_unicodes(self) -> None:
        """#711: `edge_quote` follows Unicode's Quotation_Mark property, plus a backtick, so a
        mark Unicode adds is counted without a list of ours to update; a few look-alikes too."""
        import regex

        from mcp_synology.core.display import edge_quote

        marks = regex.findall(r"\p{Quotation_Mark}", "".join(map(chr, range(0x110000))))
        assert len(marks) >= 30
        # And the look-alikes Unicode doesn't give the property (pre-QA r1).
        looks = [
            "`",
            "\uff40",
            *map(chr, range(0x275B, 0x2761)),
            *map(chr, range(0x1F676, 0x1F679)),
            # Primes and reversed primes, and the modifier letters prime, double prime,
            # apostrophe and double apostrophe (QA r1 on #724).
            *map(chr, range(0x2032, 0x2038)),
            "\u02b9",
            "\u02ba",
            "\u02bc",
            "\u02ee",
        ]
        assert all(edge_quote(f"{m}a") and edge_quote(f"a{m}") for m in [*marks, *looks])
        assert not edge_quote("a") and not edge_quote("a-b")
        # Inside a name a prime is part of it (12 inches).
        assert not edge_quote("/s/Record 12\u2033 b")
        assert not edge_quote("\u2031a")  # per ten thousand, the primes' neighbor

        # A combining mark on a final quote doesn't hide it (pre-QA r1).
        assert edge_quote("/s/a'\u0301") and edge_quote("/s/a\u00bb\u0301")
        assert not edge_quote("/s/e\u0301")

    def test_the_quote_class_doesnt_rebind_the_mark_run_bound(self) -> None:
        """QA r1 on #724: the quote class took the name of the lookbehinds' bound."""
        from mcp_synology.core import display

        assert display._MARKS == "{0,32}"

    @pytest.mark.skipif(os.sep != "/", reason="shown() splits on '/' only")
    @pytest.mark.parametrize(
        "path",
        [
            "/h/ pad/\u2764\ufe0f",  # padding and an allowed VS16 in another part (pre-QA r3)
            "/h/a, b/\u2764\ufe0f",
            "/h/rep\u200bort.pdf",
            "/h/" + "x" * 300,
            "/h/a\\b, c",
            "/h/plain.txt",
            "'a.jpg'",  # a bare name that starts and ends with a quote (pre-QA r4)
            "/h/x'",
        ],
    )
    def test_shown_local_matches_shown_on_posix(self, path: str) -> None:
        """Pre-QA r3: one rule for both, through `_needs_escapes`; a quoted part's reason
        decides between `plain_quoted` and escapes, not its output."""
        from mcp_synology.core.display import shown, shown_local

        assert shown_local(path) == shown(path)

    async def test_a_refused_argument_is_quoted_as_given(self, mock_client: DsmClient) -> None:
        # shown_local, not repr: repr doubles each Windows backslash (pre-QA r1).
        given = "/nonexistent/a\tb.txt"
        with pytest.raises(ToolError) as exc:
            await upload_file(mock_client, local_path=given, dest_folder="/video/up")
        message = json.loads(str(exc.value))["error"]["message"]
        assert message.endswith(": " + repr(given)), message
        with pytest.raises(ToolError) as exc:
            await download_file(mock_client, path="/video/m.mkv", dest_folder=given)
        message = json.loads(str(exc.value))["error"]["message"]
        assert message.endswith(": " + repr(given)), message

    @respx.mock
    async def test_a_failed_read_names_the_file_once(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """Pre-QA r1: the OS error's text carries its own copy of the path."""
        local = tmp_path / self.TAB
        local.write_text("x")
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"files": []}}
        )
        error = PermissionError(13, "Permission denied", str(local))
        with (
            patch.object(mock_client, "upload", side_effect=error),
            pytest.raises(ToolError) as exc,
        ):
            await upload_file(mock_client, local_path=str(local), dest_folder="/video/up")
        message = json.loads(str(exc.value))["error"]["message"]
        assert message.endswith(f"Failed to read local file {str(local)!r}: Permission denied"), (
            message
        )
        assert message.count(str(tmp_path)) == 1, message

    @respx.mock
    async def test_a_local_file_already_there_is_shown(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"files": [{"path": "/video/m.mkv", "isdir": False}]}}
        )
        (tmp_path / self.TAB).write_text("existing")
        with pytest.raises(ToolError) as exc:
            await download_file(
                mock_client, path="/video/m.mkv", dest_folder=str(tmp_path), filename=self.TAB
            )
        message = json.loads(str(exc.value))["error"]["message"]
        assert message.endswith(repr(str(tmp_path / self.TAB))), message

    @respx.mock
    async def test_a_local_folder_in_the_way_is_shown(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"files": [{"path": "/video/m.mkv", "isdir": False}]}}
        )
        (tmp_path / self.TAB).mkdir()
        with pytest.raises(ToolError) as exc:
            await download_file(
                mock_client, path="/video/m.mkv", dest_folder=str(tmp_path), filename=self.TAB
            )
        message = json.loads(str(exc.value))["error"]["message"]
        assert f"{repr(str(tmp_path / self.TAB))} is a folder" in message, message

    @respx.mock
    async def test_a_failed_write_names_the_file_shown(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        respx.get(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": True, "data": {"files": []}}
        )
        for code, start in [(errno.ENOSPC, "No space left"), (errno.EACCES, "Failed to write")]:
            with (
                patch.object(mock_client, "download", side_effect=OSError(code, "nope")),
                pytest.raises(ToolError) as exc,
            ):
                await download_file(
                    mock_client, path="/video/m.mkv", dest_folder=str(tmp_path), filename=self.TAB
                )
            message = json.loads(str(exc.value))["error"]["message"]
            assert start in message, message
            assert f"{repr(str(tmp_path / self.TAB))}: nope" in message, message

    def test_every_local_path_in_a_message_is_shown(self) -> None:
        """The guard: in the modules that write local paths (the two transfer modules,
        `core/local_paths.py`, Download Station's tasks and the server's instructions, #697), a
        local path goes through `shown_local` (never ``!r``, which doubles Windows
        backslashes, nor `shown` on a whole local path, which reads only ``/`` as a
        separator). `shown` is for a file's own name (``name``, ``effective_name``,
        ``x.name``), and ``!r`` for the caller's own ``filename`` refused as given.
        `_dest_name`, which builds a file name, is skipped.

        What it sees, by each file's local-path names: an f-string field; a ``+``, ``%`` or
        ``.format()`` with text; a message argument (`path_list`, `refuse`, `error_response`'s
        message, list items and generators in them); in `server.py` a dict's values; and
        ``Path.home()``. What it can't see, and behavior tests hold instead (QA r1 on #712): a
        path carried through another variable (``shown_value = value``) or under a name not
        listed (a renamed loop variable)."""
        import ast

        root = Path(__file__).parents[3] / "src" / "mcp_synology"
        # A file's own name counts in the transfer modules, where `name` is a local file's.
        transfer = {
            "local_path",
            "local_dir",
            "dest_file",
            "dest_folder",
            "local",
            "value",
            "path",
            "name",
            "effective_name",
        }
        # Each file and the names its local paths go by (#697: core's local-path checks, which
        # every tool with a local path meets first, Download Station's .torrent file, and the
        # instructions' LOCAL PATHS).
        targets = {
            "modules/filestation/transfer.py": transfer,
            "modules/photos/transfer.py": transfer,
            # `r` is each allowed folder (`for r in roots`): a raw `str(r)` is caught where it's
            # written (an f-string, a `path_list` item), not under another loop name.
            "core/local_paths.py": {"value", "path", "refused", "resolved", "entry", "r"},
            "modules/downloadstation/tasks.py": {"torrent_file_path", "path", "raw"},
            "server.py": {"r", "root"},
        }
        allowed: set[tuple[str, str, str]] = set()

        def called(call: ast.Call) -> str:
            """A call's function name: ``f(...)`` or ``x.f(...)``."""
            return getattr(call.func, "id", "") or getattr(call.func, "attr", "")

        def bare(expr: ast.expr, local: set[str]) -> set[str]:
            """The local paths in ``expr`` that aren't inside a `shown_local` call: a local
            name read (not a loop's own target, `for r in roots`), or ``Path.home()``."""
            inside: set[int] = set()
            for call in ast.walk(expr):
                if isinstance(call, ast.Call) and called(call) in ("shown_local", "_folder_shown"):
                    inside |= {id(x) for x in ast.walk(call)}
            # A conditional's test isn't written (`a if lexically_inside(path, …) else b`).
            for cond in ast.walk(expr):
                if isinstance(cond, ast.IfExp):
                    inside |= {id(x) for x in ast.walk(cond.test)}
            found = set()
            for x in ast.walk(expr):
                if id(x) in inside:
                    continue
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in local:
                    found.add(x.id)
                if isinstance(x, ast.Call) and ast.unparse(x.func) == "Path.home":
                    found.add("Path.home()")
            return found

        def a_name(arg: ast.expr) -> bool:
            if isinstance(arg, ast.Name):
                return arg.id in {"name", "effective_name"}
            return isinstance(arg, ast.Attribute) and arg.attr == "name"

        found = set()
        for module, local in targets.items():
            tree = ast.parse((root / module).read_text())
            for func in ast.walk(tree):
                if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                if func.name == "_dest_name":
                    continue
                for node in ast.walk(func):
                    # A message argument (QA r1 on #712), positional or by keyword (QA r2):
                    # `path_list`'s heading and items, `refuse`'s text, `error_response`'s
                    # message and suggestion (not `value=`, which is the
                    # caller's text as sent, nor `param=`), and in `server.py` a dict's values (the
                    # instructions' template variables, such as LOCAL MACHINE's `home_dir`).
                    sent: list[ast.expr] = []
                    words = ("message", "suggestion", "what", "heading", "items")
                    if isinstance(node, ast.Call) and called(node) in ("path_list", "refuse"):
                        sent = [*node.args, *(k.value for k in node.keywords if k.arg in words)]
                    elif isinstance(node, ast.Call) and called(node) == "error_response":
                        sent = [*node.args[1:], *(k.value for k in node.keywords if k.arg in words)]
                    elif isinstance(node, ast.Dict) and module == "server.py":
                        sent = [v for v in node.values if v is not None]
                    for arg in sent:
                        if not isinstance(arg, ast.JoinedStr) and bare(arg, local):
                            found.add((module, func.name, ast.unparse(arg)))
                    # A path joined by + or %, or passed to .format(), with a text (pre-QA r4).
                    joined: list[ast.expr] = []
                    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
                        joined = [node.left, node.right]
                    elif (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "format"
                    ):
                        joined = [*node.args, *(k.value for k in node.keywords)]
                    texty = isinstance(node, ast.BinOp) and any(
                        isinstance(side, ast.Constant | ast.JoinedStr) for side in joined
                    )
                    for side in joined if texty or isinstance(node, ast.Call) else []:
                        if bare(side, local - {"effective_name"}):
                            found.add((module, func.name, ast.unparse(side)))
                    if not isinstance(node, ast.JoinedStr):
                        continue
                    texts = [v.value for v in node.values if isinstance(v, ast.Constant)]
                    if texts and all(t == "/" for t in texts):
                        continue  # builds a NAS path (f"{dest}/{name}"), not a message
                    for part in node.values:
                        if not isinstance(part, ast.FormattedValue):
                            continue
                        value = part.value
                        call = (
                            value.func.id
                            if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name))
                            else ""
                        )
                        raw = part.conversion != -1
                        # _folder_shown calls shown_local (pre-QA r1 on #699).
                        if call in ("shown_local", "_folder_shown") and not raw:
                            continue
                        named = call in ("shown", "_row_name")  # _row_name calls shown (#699)
                        if named and not raw and value.args and a_name(value.args[0]):
                            continue
                        # The caller's own file name, refused as given ("Can't save as '..'"),
                        # is quoted with !r as every refusal of an argument is (pre-QA r6).
                        refused_name = isinstance(value, ast.Name) and value.id == "effective_name"
                        if refused_name and part.conversion == ord("r"):
                            continue
                        # A local path's name anywhere in the expression: bare, an attribute,
                        # wrapped in a call (str(local_dir), local_dir.as_posix()), or !r.
                        if bare(value, local) and not (raw is False and call == "shown_local"):
                            found.add((module, func.name, ast.unparse(value)))
        assert found <= allowed, sorted(found - allowed)
