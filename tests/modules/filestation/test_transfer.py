"""Tests for modules/filestation/transfer.py — upload_file, download_file."""

from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest
import respx
from mcp.server.fastmcp.exceptions import ToolError

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
        assert json.loads(str(exc_info.value))["error"]["code"] == "already_exists"
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
            raise OSError(errno.ENOSPC, "No space left on device")

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
