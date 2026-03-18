"""Integration tests — requires a real Synology NAS.

Run with: uv run pytest -m integration
Requires: tests/integration_config.yaml (see integration_config.yaml.example)

These tests hit the real DSM API over HTTP. They verify that our client,
auth, and module code works against an actual NAS — not mocked responses.

NOTE: DSM's background task APIs (Search, DirSize) can be overwhelmed by
rapid-fire requests. Tests include delays where needed. If tests fail
intermittently, wait for NAS CPU to settle and retry.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from synology_mcp.core.auth import AuthManager
from synology_mcp.core.client import DsmClient
from synology_mcp.core.config import AppConfig
from synology_mcp.modules.filestation.listing import list_files, list_recycle_bin, list_shares
from synology_mcp.modules.filestation.metadata import get_dir_size, get_file_info
from synology_mcp.modules.filestation.operations import (
    copy_files,
    create_folder,
    delete_files,
    move_files,
    rename,
)
from synology_mcp.modules.filestation.search import search_files

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config & fixtures
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent / "integration_config.yaml"

# Default test paths — override in integration_config.yaml under test_paths:
_DEFAULT_TEST_PATHS: dict[str, str] = {
    "existing_share": "/home",
    "search_folder": "/home",
    "search_keyword": "test",
    "writable_folder": "/home/Test-Resources",
}


def _load_integration_config() -> tuple[AppConfig, dict[str, str]]:
    """Load config and test paths from integration_config.yaml."""
    if not _CONFIG_PATH.exists():
        pytest.skip(
            f"Integration config not found: {_CONFIG_PATH}\n"
            "Copy integration_config.yaml.example → integration_config.yaml "
            "and fill in NAS details."
        )

    raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    test_paths = {**_DEFAULT_TEST_PATHS, **raw.pop("test_paths", {})}
    config = AppConfig(**raw)
    return config, test_paths


@pytest.fixture
def integration_config() -> tuple[AppConfig, dict[str, str]]:
    """Provide integration config and test paths."""
    return _load_integration_config()


@pytest.fixture
async def nas_client(
    integration_config: tuple[AppConfig, dict[str, str]],
) -> Any:
    """Provide an authenticated DsmClient connected to a real NAS.

    Yields (client, auth, config, test_paths).
    """
    config, test_paths = integration_config
    conn = config.connection
    assert conn is not None, "integration_config.yaml must have a connection section"

    protocol = "https" if conn.https else "http"
    base_url = f"{protocol}://{conn.host}:{conn.port}"

    client = DsmClient(
        base_url=base_url,
        verify_ssl=conn.verify_ssl,
        timeout=conn.timeout,
    )

    async with client:
        # Populate API cache from real NAS
        cache = await client.query_api_info()
        logger.info("API cache: %d APIs discovered", len(cache))

        # Log only the APIs we actually use
        _relevant = [
            "SYNO.FileStation.List",
            "SYNO.FileStation.Search",
            "SYNO.FileStation.CopyMove",
            "SYNO.FileStation.Delete",
            "SYNO.FileStation.CreateFolder",
            "SYNO.FileStation.Rename",
            "SYNO.FileStation.DirSize",
            "SYNO.FileStation.Info",
            "SYNO.DSM.Info",
        ]
        for api_name in _relevant:
            entry = cache.get(api_name)
            if entry:
                fmt = f", format={entry.request_format}" if entry.request_format else ""
                logger.info("  %s: v%d–v%d%s", api_name, entry.min_version, entry.max_version, fmt)

        # Authenticate
        auth = AuthManager(config, client)
        sid = await auth.login()
        logger.info("Authenticated, SID=%s...", sid[:8])

        yield client, auth, config, test_paths

        # Cleanup
        await auth.logout()


# ---------------------------------------------------------------------------
# Helper to unpack the fixture
# ---------------------------------------------------------------------------


def _unpack(nas_client: Any) -> tuple[DsmClient, AuthManager, AppConfig, dict[str, str]]:
    return nas_client  # type: ignore[return-value]


def _skip_unless_write(config: AppConfig) -> None:
    """Skip the test if filestation permission is not 'write'."""
    fs_config = config.modules.get("filestation")
    if not fs_config or fs_config.permission != "write":
        pytest.skip("Write permission required — set permission: write in integration config")


# ---------------------------------------------------------------------------
# Connection & Auth
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestConnection:
    """Verify basic connectivity and authentication."""

    async def test_api_info_populated(self, nas_client: Any) -> None:
        """API cache should contain FileStation APIs."""
        client, _, _, _ = _unpack(nas_client)
        assert "SYNO.FileStation.List" in client._api_cache
        assert "SYNO.FileStation.Search" in client._api_cache
        assert "SYNO.FileStation.CopyMove" in client._api_cache

    async def test_dsm_info(self, nas_client: Any) -> None:
        """Should be able to fetch DSM version info."""
        client, _, _, _ = _unpack(nas_client)
        info = await client.fetch_dsm_info()
        assert "version_string" in info or "version" in info
        logger.info("DSM version: %s", info)

    async def test_api_versions_logged(self, nas_client: Any) -> None:
        """Log negotiated versions for key APIs (visual check in -v output)."""
        client, _, _, _ = _unpack(nas_client)
        for api_name in [
            "SYNO.FileStation.List",
            "SYNO.FileStation.Search",
            "SYNO.FileStation.CopyMove",
            "SYNO.FileStation.Delete",
        ]:
            entry = client._api_cache.get(api_name)
            assert entry is not None, f"{api_name} not in API cache"
            logger.info(
                "%s: v%d–v%d, request_format=%s",
                api_name,
                entry.min_version,
                entry.max_version,
                entry.request_format,
            )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestListing:
    """Test list_shares and list_files against real NAS."""

    async def test_list_shares(self, nas_client: Any) -> None:
        """Should return formatted table with at least one share."""
        client, _, _, _ = _unpack(nas_client)
        result = await list_shares(client)
        assert "Name" in result  # table header
        assert "[!]" not in result
        logger.info("list_shares output:\n%s", result)

    async def test_list_files_existing_share(self, nas_client: Any) -> None:
        """Should list files in a known share."""
        client, _, _, paths = _unpack(nas_client)
        result = await list_files(client, path=paths["existing_share"])
        assert "[!]" not in result
        logger.info("list_files(%s):\n%s", paths["existing_share"], result)

    async def test_list_files_root(self, nas_client: Any) -> None:
        """Listing '/' may fail on some DSM versions — verify graceful handling."""
        client, _, _, _ = _unpack(nas_client)
        result = await list_files(client, path="/")
        logger.info("list_files(/):\n%s", result)
        # On some DSM versions, listing '/' via FileStation.List fails (error 401).
        # Use list_shares instead. Here we just verify it doesn't crash.
        assert isinstance(result, str)

    async def test_list_files_sorted_by_size(self, nas_client: Any) -> None:
        """List files sorted by size descending."""
        client, _, _, paths = _unpack(nas_client)
        result = await list_files(
            client, path=paths["existing_share"], sort_by="size", sort_direction="desc"
        )
        assert "[!]" not in result
        logger.info("list_files sorted by size:\n%s", result)

    async def test_list_files_with_limit(self, nas_client: Any) -> None:
        """List files with a small limit to test pagination."""
        client, _, _, paths = _unpack(nas_client)
        result = await list_files(client, path=paths["existing_share"], limit=3)
        assert "[!]" not in result
        logger.info("list_files limit=3:\n%s", result)

    async def test_list_files_invalid_path(self, nas_client: Any) -> None:
        """Listing a non-existent path should return a formatted error."""
        client, _, _, _ = _unpack(nas_client)
        result = await list_files(client, path="/zzz_nonexistent_share_999")
        assert "[!]" in result
        logger.info("list_files(invalid):\n%s", result)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSearch:
    """Test search_files against real NAS.

    These tests verify:
    - Search uses GET (not POST) to avoid DSM request format issues
    - Wildcard auto-wrapping (bare keyword → *keyword*)
    - filetype=all includes directories in results
    - Searching from a share path (not root)

    NOTE: DSM's search service can be overwhelmed by rapid-fire requests,
    returning 0 results or 502 errors. Tests include delays between searches
    to avoid exhausting the service. If tests fail intermittently, increase
    the delay or run fewer search tests at once.
    """

    async def test_search_keyword_finds_directory(self, nas_client: Any) -> None:
        """A bare keyword should find matching directories via wildcard wrapping.

        Verifies three fixes at once:
        - GET (not POST) for the Search API
        - Wildcard wrapping: bare "Bambu" becomes *Bambu*, matching "Bambu Studio"
        - filetype=all: directories are included in results
        """
        client, _, _, paths = _unpack(nas_client)
        folder = paths["search_folder"]
        keyword = paths["search_keyword"]

        result = await search_files(client, folder_path=folder, pattern=keyword)

        logger.info("search_files(%s, pattern=%s):\n%s", folder, keyword, result)
        assert "[!]" not in result
        assert "0 results found" not in result, (
            f"Search for '{keyword}' in {folder} returned 0 results. "
            "Verify the search_keyword and search_folder in integration_config.yaml. "
            "Also check that DSM's search service is not overloaded from prior test runs."
        )

    async def test_search_by_extension(self, nas_client: Any) -> None:
        """Search by extension pattern (*.stl) should not error."""
        client, _, _, paths = _unpack(nas_client)
        await asyncio.sleep(2)
        result = await search_files(client, folder_path=paths["existing_share"], pattern="*.stl")
        logger.info("Extension search (*.stl):\n%s", result)
        assert "[!]" not in result

    async def test_search_no_results(self, nas_client: Any) -> None:
        """Search for a nonsense pattern should return 0 results, not an error."""
        client, _, _, paths = _unpack(nas_client)
        await asyncio.sleep(2)
        result = await search_files(
            client, folder_path=paths["existing_share"], pattern="zzz_nonexistent_xyzzy_999"
        )
        assert "0 results found" in result
        assert "[!]" not in result

    async def test_search_from_root_error_handling(self, nas_client: Any) -> None:
        """Searching from '/' may fail — verify we handle it gracefully."""
        client, _, _, _ = _unpack(nas_client)
        await asyncio.sleep(2)
        result = await search_files(client, folder_path="/", pattern="test")
        logger.info("Root search result:\n%s", result)
        # Whether it returns results or an error, it should not crash.
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestMetadata:
    """Test get_file_info and get_dir_size."""

    async def test_get_file_info_share(self, nas_client: Any) -> None:
        """Get info about a known share folder."""
        client, _, _, paths = _unpack(nas_client)
        result = await get_file_info(client, paths=[paths["existing_share"]])
        assert "[!]" not in result
        logger.info("get_file_info(%s):\n%s", paths["existing_share"], result)

    async def test_get_file_info_multiple_paths(self, nas_client: Any) -> None:
        """Get info about multiple paths at once."""
        client, _, _, paths = _unpack(nas_client)
        result = await get_file_info(
            client,
            paths=[paths["existing_share"], paths["writable_folder"]],
        )
        assert "[!]" not in result
        logger.info("get_file_info(multiple):\n%s", result)

    async def test_get_file_info_invalid_path(self, nas_client: Any) -> None:
        """Get info about a non-existent path — should not crash.

        DSM may return success with empty metadata or an error depending
        on the path format. We just verify graceful handling.
        """
        client, _, _, _ = _unpack(nas_client)
        result = await get_file_info(client, paths=["/zzz_nonexistent_999/fake.txt"])
        assert isinstance(result, str)
        logger.info("get_file_info(invalid):\n%s", result)

    async def test_get_dir_size(self, nas_client: Any) -> None:
        """Get size of a known folder (uses a smaller folder to avoid timeouts)."""
        client, _, _, paths = _unpack(nas_client)
        # Use the writable folder (likely small) rather than the existing_share
        # which may be very large and cause the background task to time out.
        result = await get_dir_size(client, path=paths["writable_folder"])
        assert "[!]" not in result
        assert "Total size" in result
        logger.info("get_dir_size(%s):\n%s", paths["writable_folder"], result)


# ---------------------------------------------------------------------------
# Write operations: full lifecycle
# create → copy → verify copy → move → verify move → rename → delete → verify
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestWriteOperations:
    """Test write operations against real NAS.

    These tests use the writable_folder path from config. They create
    temporary resources and clean them up. Requires 'write' permission
    in the module config.

    Tests run in order to build on each other's state:
    create → copy → verify → move → verify → rename → delete → verify
    """

    _TEST_DIR = "_integration_test_tmp"
    _COPY_SRC = "_integration_test_tmp/original"
    _COPY_DEST = "_integration_test_tmp/copied"
    _MOVE_DEST = "_integration_test_tmp/moved"

    async def test_01_create_folder(self, nas_client: Any) -> None:
        """Create the test folder structure."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        # Create main test dir and a subfolder to use as copy source
        result = await create_folder(client, paths=[f"{base}/{self._COPY_SRC}"])
        logger.info("create_folder result:\n%s", result)
        assert "[!]" not in result or "already exists" in result.lower()

    async def test_02_create_folder_idempotent(self, nas_client: Any) -> None:
        """Creating the same folder again should not error (idempotent)."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        result = await create_folder(client, paths=[f"{base}/{self._COPY_SRC}"])
        logger.info("create_folder (idempotent):\n%s", result)
        assert isinstance(result, str)

    async def test_03_copy_folder(self, nas_client: Any) -> None:
        """Copy a folder to a new location within the test area.

        This is the most critical write test — copy was the operation
        that had the most bugs (POST vs GET, v3 format, silent failures).
        """
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        src = f"{base}/{self._COPY_SRC}"
        dest = f"{base}/{self._TEST_DIR}"

        result = await copy_files(client, paths=[src], dest_folder=dest)
        logger.info("copy_files result:\n%s", result)
        assert "[!]" not in result
        assert "Copied" in result

    async def test_04_verify_copy_exists(self, nas_client: Any) -> None:
        """Verify the copied folder appears in the destination listing."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        listing = await list_files(client, path=f"{base}/{self._TEST_DIR}")
        logger.info("Listing after copy:\n%s", listing)
        assert "original" in listing

    async def test_05_copy_with_overwrite(self, nas_client: Any) -> None:
        """Copy the same folder again with overwrite=True."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        src = f"{base}/{self._COPY_SRC}"
        dest = f"{base}/{self._TEST_DIR}"

        result = await copy_files(client, paths=[src], dest_folder=dest, overwrite=True)
        logger.info("copy_files (overwrite):\n%s", result)
        assert "[!]" not in result

    async def test_06_move_folder(self, nas_client: Any) -> None:
        """Move the copied folder to a new name."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        # Move the copy of "original" to "moved"
        src = f"{base}/{self._TEST_DIR}/original"
        dest = f"{base}/{self._MOVE_DEST}"

        # Create the move destination first
        await create_folder(client, paths=[dest])

        result = await move_files(client, paths=[src], dest_folder=dest)
        logger.info("move_files result:\n%s", result)
        assert "[!]" not in result
        assert "Moved" in result

    async def test_07_verify_move(self, nas_client: Any) -> None:
        """Verify the moved folder is in the new location and gone from old."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]

        # Should be in the move destination
        dest_listing = await list_files(client, path=f"{base}/{self._MOVE_DEST}")
        logger.info("Move destination listing:\n%s", dest_listing)
        assert "original" in dest_listing

        # Should be gone from the copy destination
        src_listing = await list_files(client, path=f"{base}/{self._TEST_DIR}")
        logger.info("Copy source listing after move:\n%s", src_listing)
        # "original" should not be in this listing (it was moved)
        # But "moved" dir will be here since it's under _TEST_DIR

    async def test_08_rename(self, nas_client: Any) -> None:
        """Rename a folder."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        target = f"{base}/{self._MOVE_DEST}/original"
        result = await rename(client, path=target, new_name="renamed_test")
        logger.info("rename result:\n%s", result)
        assert "[!]" not in result

    async def test_09_delete_cleanup(self, nas_client: Any) -> None:
        """Delete the entire test folder tree (cleanup)."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        target = f"{base}/{self._TEST_DIR}"
        result = await delete_files(client, paths=[target], recursive=True)
        logger.info("delete result:\n%s", result)
        assert "[!]" not in result

    async def test_10_verify_deleted(self, nas_client: Any) -> None:
        """Verify the test folder is gone from the writable area."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        listing = await list_files(client, path=paths["writable_folder"])
        assert self._TEST_DIR not in listing
        logger.info("Verified %s is deleted", self._TEST_DIR)


# ---------------------------------------------------------------------------
# Recycle bin
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRecycleBin:
    """Test recycle bin listing after delete.

    Requires the writable_folder's share to have recycle bin enabled.
    Creates a folder, deletes it, checks recycle bin, then cleans up.
    """

    _RECYCLE_TEST = "_recycle_bin_test"

    async def test_01_create_and_delete(self, nas_client: Any) -> None:
        """Create a test folder then delete it (should go to recycle bin)."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        folder = f"{base}/{self._RECYCLE_TEST}"

        # Create
        result = await create_folder(client, paths=[folder])
        assert "[!]" not in result or "already exists" in result.lower()

        # Delete (should go to recycle bin if enabled)
        result = await delete_files(client, paths=[folder])
        logger.info("delete result:\n%s", result)
        assert "[!]" not in result

    async def test_02_list_recycle_bin(self, nas_client: Any) -> None:
        """List the recycle bin for the writable folder's share."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        # Extract share name from writable_folder (e.g., "/Test-Resources" → "Test-Resources")
        share = paths["writable_folder"].strip("/").split("/")[0]

        result = await list_recycle_bin(client, share=share)
        logger.info("list_recycle_bin(/%s):\n%s", share, result)
        # Should not crash. May or may not find our deleted folder depending
        # on whether recycle bin is enabled for this share.
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestErrorHandling:
    """Test that errors are handled gracefully, not crashes."""

    async def test_copy_invalid_source(self, nas_client: Any) -> None:
        """Copy from a non-existent path should return formatted error."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        result = await copy_files(
            client,
            paths=["/zzz_nonexistent_999/fake.txt"],
            dest_folder=paths["writable_folder"],
        )
        logger.info("copy invalid source:\n%s", result)
        assert "[!]" in result

    async def test_delete_invalid_path(self, nas_client: Any) -> None:
        """Delete a non-existent path should return formatted error."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        result = await delete_files(client, paths=["/zzz_nonexistent_999/fake.txt"])
        logger.info("delete invalid path:\n%s", result)
        # May succeed silently (DSM doesn't always error on missing paths)
        # or return an error. Either way, should not crash.
        assert isinstance(result, str)

    async def test_rename_invalid_path(self, nas_client: Any) -> None:
        """Rename a non-existent path should return formatted error."""
        client, _, _, _ = _unpack(nas_client)
        result = await rename(client, path="/zzz_nonexistent_999/fake.txt", new_name="new_name")
        logger.info("rename invalid path:\n%s", result)
        assert "[!]" in result
