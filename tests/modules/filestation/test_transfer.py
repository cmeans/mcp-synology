"""Tests for modules/filestation/transfer.py — upload_file, download_file."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import respx

from mcp_synology.modules.filestation.transfer import (
    download_file,
    upload_file,
)
from tests.conftest import BASE_URL

if TYPE_CHECKING:
    from pathlib import Path

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
        result = await upload_file(
            mock_client,
            local_path="/nonexistent/file.txt",
            dest_folder="/video/uploads",
        )
        assert "[!]" in result
        assert "not found" in result.lower()

    @respx.mock
    async def test_upload_file_exists_no_overwrite(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        local_file = tmp_path / "test.txt"
        local_file.write_text("hello")

        respx.post(f"{BASE_URL}/webapi/entry.cgi").respond(
            json={"success": False, "error": {"code": 414}}
        )

        result = await upload_file(
            mock_client,
            local_path=str(local_file),
            dest_folder="/video/uploads",
        )
        assert "[!]" in result
        assert "already exists" in result
        assert "overwrite=true" in result

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

        result = await upload_file(
            mock_client,
            local_path=str(local_file),
            dest_folder="/video/uploads",
        )
        assert "[!]" in result
        assert "Upload" in result

    async def test_upload_local_file_permission_error(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """OSError reading local file should return a formatted error."""
        local_file = tmp_path / "no_read.txt"
        local_file.write_text("hello")
        local_file.chmod(0o000)

        result = await upload_file(
            mock_client,
            local_path=str(local_file),
            dest_folder="/video/uploads",
        )
        # Restore permissions for cleanup
        local_file.chmod(0o644)
        assert "[!]" in result
        assert "permission" in result.lower() or "not found" in result.lower()


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
        result = await download_file(
            mock_client,
            path="/video/movie.mkv",
            dest_folder="/nonexistent/dir",
        )
        assert "[!]" in result
        assert "not found" in result.lower()

    async def test_download_file_exists_no_overwrite(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        existing = tmp_path / "movie.mkv"
        existing.write_text("existing")

        result = await download_file(
            mock_client,
            path="/video/movie.mkv",
            dest_folder=str(tmp_path),
        )
        assert "[!]" in result
        assert "already exists" in result
        assert "overwrite=true" in result

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

        result = await download_file(
            mock_client,
            path="/video/nonexistent.mkv",
            dest_folder=str(tmp_path),
        )
        assert "[!]" in result
        assert "Download" in result
        # Partial file should not exist
        assert not (tmp_path / "nonexistent.mkv").exists()

    @respx.mock
    async def test_download_partial_cleanup(self, mock_client: DsmClient, tmp_path: Path) -> None:
        """Verify partial file is deleted on network failure."""
        respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(
            side_effect=httpx.ReadError("connection reset")
        )

        with contextlib.suppress(httpx.ReadError):
            await download_file(
                mock_client,
                path="/video/big_file.mkv",
                dest_folder=str(tmp_path),
            )

        # Partial file should be cleaned up
        assert not (tmp_path / "big_file.mkv").exists()

    @respx.mock
    async def test_download_write_permission_error(
        self, mock_client: DsmClient, tmp_path: Path
    ) -> None:
        """OSError writing local file should return a formatted error.

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

        result = await download_file(
            mock_client,
            path="/video/file.mkv",
            dest_folder=str(readonly_dir),
            filename="test.mkv",
        )
        # Restore permissions for cleanup
        readonly_dir.chmod(0o755)
        assert "[!]" in result
        assert "filename" in result.lower()

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

        result = await download_file(
            mock_client,
            path="/video/huge.mkv",
            dest_folder=str(tmp_path),
        )
        assert "[!]" in result
        assert "disk space" in result.lower()

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
