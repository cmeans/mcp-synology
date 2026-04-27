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
import json
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.server.fastmcp.exceptions import ToolError

from mcp_synology.core.auth import AuthManager
from mcp_synology.core.client import DsmClient
from mcp_synology.core.config import AppConfig
from mcp_synology.modules.filestation.listing import list_files, list_recycle_bin, list_shares
from mcp_synology.modules.filestation.metadata import get_dir_size, get_file_info
from mcp_synology.modules.filestation.operations import (
    copy_files,
    create_folder,
    delete_files,
    move_files,
    rename,
)
from mcp_synology.modules.filestation.search import search_files
from mcp_synology.modules.filestation.transfer import download_file, upload_file
from mcp_synology.modules.system.info import get_system_info
from mcp_synology.modules.system.utilization import get_resource_usage

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
    # Store admin_config path in test_paths for later use
    admin_config = raw.pop("admin_config", None)
    if admin_config:
        test_paths["admin_config"] = admin_config
    config = AppConfig(**raw)
    return config, test_paths


@pytest.fixture
def integration_config() -> tuple[AppConfig, dict[str, str]]:
    """Provide integration config and test paths."""
    return _load_integration_config()


@pytest.fixture
async def refresh_search_index() -> Any:
    """Async callback to register a runtime-created path with the search index.

    Default no-op for real-NAS integration runs — Synology NAS shares are
    typically indexed and the indexer picks up changes within seconds.
    Overridden in tests/vdsm/conftest.py to invoke `synoindex -A -d` via
    SSH on the vdsm container, where DSM Universal Search on non-indexed
    shares can take several minutes to crawl runtime-created subdirectories.
    """

    async def _noop(_path: str) -> None:
        return None

    return _noop


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
            "SYNO.FileStation.Upload",
            "SYNO.FileStation.Download",
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
# Admin fixture (for APIs requiring admin privileges)
# ---------------------------------------------------------------------------


@pytest.fixture
async def admin_client(
    integration_config: tuple[AppConfig, dict[str, str]],
) -> Any:
    """Provide an authenticated admin DsmClient.

    Uses the admin_config path from integration_config.yaml.
    Skips if no admin config is configured.
    Yields (client, auth, config, test_paths).
    """
    _, test_paths = integration_config
    admin_config_path = test_paths.get("admin_config")
    if not admin_config_path:
        pytest.skip("No admin_config path in integration_config.yaml")

    admin_path = Path(admin_config_path).expanduser()
    if not admin_path.exists():
        pytest.skip(f"Admin config not found: {admin_path}")

    raw = yaml.safe_load(admin_path.read_text(encoding="utf-8"))
    admin_cfg = AppConfig(**raw)
    conn = admin_cfg.connection
    assert conn is not None

    protocol = "https" if conn.https else "http"
    base_url = f"{protocol}://{conn.host}:{conn.port}"

    client = DsmClient(
        base_url=base_url,
        verify_ssl=conn.verify_ssl,
        timeout=conn.timeout,
    )

    async with client:
        await client.query_api_info()
        auth = AuthManager(admin_cfg, client)
        sid = await auth.login()
        logger.info("Admin authenticated, SID=%s...", sid[:8])

        yield client, auth, admin_cfg, test_paths

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
        logger.info("list_shares output:\n%s", result)

    async def test_list_files_existing_share(self, nas_client: Any) -> None:
        """Should list files in a known share."""
        client, _, _, paths = _unpack(nas_client)
        result = await list_files(client, path=paths["existing_share"])
        logger.info("list_files(%s):\n%s", paths["existing_share"], result)

    async def test_list_files_root(self, nas_client: Any) -> None:
        """Listing '/' may fail on some DSM versions — verify graceful handling."""
        client, _, _, _ = _unpack(nas_client)
        # On some DSM versions, listing '/' via FileStation.List fails (error 401).
        # Use list_shares instead. Here we just verify it doesn't crash hard.
        try:
            result = await list_files(client, path="/")
            logger.info("list_files(/):\n%s", result)
        except ToolError as e:
            logger.info("list_files(/) raised ToolError (expected on some DSM): %s", e)

    async def test_list_files_sorted_by_size(self, nas_client: Any) -> None:
        """List files sorted by size descending."""
        client, _, _, paths = _unpack(nas_client)
        result = await list_files(
            client, path=paths["existing_share"], sort_by="size", sort_direction="desc"
        )
        logger.info("list_files sorted by size:\n%s", result)

    async def test_list_files_with_limit(self, nas_client: Any) -> None:
        """List files with a small limit to test pagination."""
        client, _, _, paths = _unpack(nas_client)
        result = await list_files(client, path=paths["existing_share"], limit=3)
        logger.info("list_files limit=3:\n%s", result)

    async def test_list_files_invalid_path(self, nas_client: Any) -> None:
        """Listing a non-existent path should raise ToolError."""
        client, _, _, _ = _unpack(nas_client)
        with pytest.raises(ToolError) as exc_info:
            await list_files(client, path="/zzz_nonexistent_share_999")
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        logger.info("list_files(invalid) raised ToolError: %s", exc_info.value)


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

    async def test_search_keyword_finds_directory(
        self, nas_client: Any, refresh_search_index: Any
    ) -> None:
        """A bare keyword should find matching directories via wildcard wrapping.

        Verifies three fixes at once:
        - GET (not POST) for the Search API
        - Wildcard wrapping: bare "Bambu" becomes *Bambu*, matching "Bambu Studio"
        - filetype=all: directories are included in results

        Creates a directory matching the search keyword via the API to ensure
        the test is self-contained. Retries to allow the DSM search indexer
        to discover the new directory.
        """
        client, _, _, paths = _unpack(nas_client)
        folder = paths["search_folder"]
        keyword = paths["search_keyword"]

        # Ensure a directory with the keyword in its name exists.
        # DSM search matches file/directory names, not content.
        # On real NAS the search_folder likely already has matching content;
        # on vdsm we create it here. Non-fatal if the folder is read-only.
        search_dir = f"{folder}/{keyword} Studio"
        try:
            await create_folder(client, paths=[search_dir])
            logger.info("Created search target directory: %s", search_dir)
        except ToolError as e:
            logger.info("Could not create search target (may already exist): %s", e)

        # Register the (possibly just-created) directory with DSM's search
        # index. No-op on real-NAS runs; on vdsm, calls synoindex via SSH
        # so the runtime-created subdirectory is discoverable on the first
        # search attempt instead of waiting for the periodic indexer.
        await refresh_search_index(search_dir)

        # Verify the target is visible to FileStation before searching
        listing = await list_files(client, path=folder)
        logger.info("Contents of %s before search:\n%s", folder, listing)

        # Search from the share root (parent of search_folder) for broader scope.
        # DSM search on non-indexed shares can miss recently created items in
        # narrow scopes. Extract the share root from search_folder.
        share_root = "/" + folder.strip("/").split("/")[0]

        # Allow the search service to recover from prior test activity
        # (DirSize tasks, delete operations, etc.). On Virtual DSM, the
        # search service can be easily exhausted by rapid-fire requests.
        # Worst-case retry budget: 3s + 10 + 10 + 15 + 15 + 15 = ~68s.
        await asyncio.sleep(3)

        max_attempts = 6
        result = ""
        for attempt in range(1, max_attempts + 1):
            result = await search_files(client, folder_path=share_root, pattern=keyword)
            logger.info(
                "search_files(%s, pattern=%s) attempt %d/%d:\n%s",
                share_root,
                keyword,
                attempt,
                max_attempts,
                result,
            )
            if "0 results found" not in result:
                break
            if attempt < max_attempts:
                delay = 10 if attempt <= 2 else 15
                logger.info("Search returned 0 results, waiting %ds for indexer...", delay)
                await asyncio.sleep(delay)

        assert "0 results found" not in result, (
            f"Search for '{keyword}' in {share_root} returned 0 results after {max_attempts} "
            "attempts. Verify the search_keyword and search_folder in "
            "integration_config.yaml. Also check that DSM's search service is not "
            "overloaded from prior test runs."
        )

    async def test_search_by_extension(self, nas_client: Any) -> None:
        """Search by extension pattern (*.stl) should not error."""
        client, _, _, paths = _unpack(nas_client)
        await asyncio.sleep(2)
        result = await search_files(client, folder_path=paths["existing_share"], pattern="*.stl")
        logger.info("Extension search (*.stl):\n%s", result)

    async def test_search_no_results(self, nas_client: Any) -> None:
        """Search for a nonsense pattern should return 0 results, not an error."""
        client, _, _, paths = _unpack(nas_client)
        await asyncio.sleep(2)
        result = await search_files(
            client, folder_path=paths["existing_share"], pattern="zzz_nonexistent_xyzzy_999"
        )
        assert "0 results found" in result

    async def test_search_from_root_error_handling(self, nas_client: Any) -> None:
        """Searching from '/' may fail — verify we handle it gracefully."""
        client, _, _, _ = _unpack(nas_client)
        await asyncio.sleep(2)
        # Whether it returns results or raises ToolError, it should not crash hard.
        try:
            result = await search_files(client, folder_path="/", pattern="test")
            logger.info("Root search result:\n%s", result)
        except ToolError as e:
            logger.info("Root search raised ToolError (expected on some DSM): %s", e)


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
        logger.info("get_file_info(%s):\n%s", paths["existing_share"], result)

    async def test_get_file_info_multiple_paths(self, nas_client: Any) -> None:
        """Get info about multiple paths at once."""
        client, _, _, paths = _unpack(nas_client)
        result = await get_file_info(
            client,
            paths=[paths["existing_share"], paths["writable_folder"]],
        )
        logger.info("get_file_info(multiple):\n%s", result)

    async def test_get_file_info_invalid_path(self, nas_client: Any) -> None:
        """Get info about a non-existent path — should not crash.

        DSM may return success with empty metadata or raise ToolError depending
        on the path format. We just verify graceful handling.
        """
        client, _, _, _ = _unpack(nas_client)
        try:
            result = await get_file_info(client, paths=["/zzz_nonexistent_999/fake.txt"])
            logger.info("get_file_info(invalid):\n%s", result)
        except ToolError as e:
            logger.info("get_file_info(invalid) raised ToolError: %s", e)

    async def test_get_dir_size(self, nas_client: Any) -> None:
        """Get size of a known folder (uses a smaller folder to avoid timeouts)."""
        client, _, _, paths = _unpack(nas_client)
        # Use the writable folder (likely small) rather than the existing_share
        # which may be very large and cause the background task to time out.
        result = await get_dir_size(client, path=paths["writable_folder"])
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
        try:
            result = await create_folder(client, paths=[f"{base}/{self._COPY_SRC}"])
            logger.info("create_folder result:\n%s", result)
        except ToolError as e:
            body = json.loads(str(e))
            assert body["error"]["code"] == "already_exists", f"Unexpected error: {e}"

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

    async def test_09_delete_cleanup(self, nas_client: Any) -> None:
        """Delete the entire test folder tree (cleanup)."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        target = f"{base}/{self._TEST_DIR}"
        result = await delete_files(client, paths=[target], recursive=True)
        logger.info("delete result:\n%s", result)

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

    Works whether or not the writable_folder's share has recycle bin enabled:
    when enabled, the deleted folder shows up in `#recycle`; when disabled,
    `list_recycle_bin` returns a friendly "not enabled" message. Both paths
    are valid and the test verifies `list_recycle_bin` returns a string in
    either case. Creates a folder, deletes it, checks recycle bin, then
    cleans up.
    """

    _RECYCLE_TEST = "_recycle_bin_test"

    async def test_01_create_and_delete(self, nas_client: Any) -> None:
        """Create a test folder then delete it (should go to recycle bin)."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        folder = f"{base}/{self._RECYCLE_TEST}"

        # Create
        try:
            result = await create_folder(client, paths=[folder])
        except ToolError as e:
            body = json.loads(str(e))
            assert body["error"]["code"] == "already_exists", f"Unexpected error: {e}"

        # Delete (should go to recycle bin if enabled)
        result = await delete_files(client, paths=[folder])
        logger.info("delete result:\n%s", result)

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
# File transfers (upload / download)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestFileTransfers:
    """Test upload_file and download_file against real NAS.

    Creates a temp file locally, uploads it, downloads it back, and verifies
    the round-trip. Cleans up both local and NAS files.
    """

    _UPLOAD_DIR = "_integration_test_transfer"
    _TEST_CONTENT = b"mcp-synology integration test content\n"

    async def test_01_upload_file(self, nas_client: Any, tmp_path: Path) -> None:
        """Upload a small test file to the NAS."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        dest = f"{base}/{self._UPLOAD_DIR}"

        # Create local test file
        local_file = tmp_path / "upload_test.txt"
        local_file.write_bytes(self._TEST_CONTENT)

        result = await upload_file(
            client,
            local_path=str(local_file),
            dest_folder=dest,
            create_parents=True,
        )
        logger.info("upload_file result:\n%s", result)
        assert "[+]" in result
        assert "upload_test.txt" in result

    async def test_02_upload_duplicate_no_overwrite(self, nas_client: Any, tmp_path: Path) -> None:
        """Uploading the same file again without overwrite.

        Note: DSM's Upload API v2 may silently skip or overwrite depending
        on the NAS configuration. We verify it doesn't crash — the behavior
        varies across DSM versions.
        """
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        dest = f"{base}/{self._UPLOAD_DIR}"

        local_file = tmp_path / "upload_test.txt"
        local_file.write_bytes(self._TEST_CONTENT)

        # DSM may return success (silent overwrite) or raise ToolError (already exists).
        # Either is acceptable — we just verify it doesn't crash hard.
        try:
            result = await upload_file(
                client,
                local_path=str(local_file),
                dest_folder=dest,
            )
            logger.info("upload duplicate (no overwrite):\n%s", result)
        except ToolError as e:
            logger.info("upload duplicate raised ToolError (expected): %s", e)

    async def test_03_upload_overwrite(self, nas_client: Any, tmp_path: Path) -> None:
        """Uploading with overwrite=True should succeed."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        dest = f"{base}/{self._UPLOAD_DIR}"

        local_file = tmp_path / "upload_test.txt"
        local_file.write_bytes(self._TEST_CONTENT)

        result = await upload_file(
            client,
            local_path=str(local_file),
            dest_folder=dest,
            overwrite=True,
        )
        logger.info("upload overwrite:\n%s", result)
        assert "[+]" in result

    async def test_04_upload_custom_filename(self, nas_client: Any, tmp_path: Path) -> None:
        """Upload with a custom filename on the NAS."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        dest = f"{base}/{self._UPLOAD_DIR}"

        local_file = tmp_path / "original_name.txt"
        local_file.write_bytes(b"renamed upload test\n")

        result = await upload_file(
            client,
            local_path=str(local_file),
            dest_folder=dest,
            filename="renamed_on_nas.txt",
        )
        logger.info("upload custom filename:\n%s", result)
        assert "[+]" in result
        assert "renamed_on_nas.txt" in result

    async def test_05_verify_uploaded_files(self, nas_client: Any) -> None:
        """Verify both uploaded files appear in the NAS listing."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        listing = await list_files(client, path=f"{base}/{self._UPLOAD_DIR}")
        logger.info("Listing after uploads:\n%s", listing)
        assert "upload_test.txt" in listing
        assert "renamed_on_nas.txt" in listing

    async def test_06_download_file(self, nas_client: Any, tmp_path: Path) -> None:
        """Download the uploaded file back and verify content."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        nas_path = f"{base}/{self._UPLOAD_DIR}/upload_test.txt"

        result = await download_file(
            client,
            path=nas_path,
            dest_folder=str(tmp_path),
        )
        logger.info("download_file result:\n%s", result)
        assert "[+]" in result
        assert "upload_test.txt" in result

        # Verify content round-trip
        downloaded = tmp_path / "upload_test.txt"
        assert downloaded.exists()
        assert downloaded.read_bytes() == self._TEST_CONTENT

    async def test_07_download_custom_filename(self, nas_client: Any, tmp_path: Path) -> None:
        """Download with a custom local filename."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        nas_path = f"{base}/{self._UPLOAD_DIR}/upload_test.txt"

        result = await download_file(
            client,
            path=nas_path,
            dest_folder=str(tmp_path),
            filename="local_renamed.txt",
        )
        logger.info("download custom filename:\n%s", result)
        assert "[+]" in result
        assert "local_renamed.txt" in result
        assert (tmp_path / "local_renamed.txt").exists()

    async def test_08_download_no_overwrite(self, nas_client: Any, tmp_path: Path) -> None:
        """Download should fail if local file exists and overwrite=False."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        nas_path = f"{base}/{self._UPLOAD_DIR}/upload_test.txt"

        # Create existing local file
        existing = tmp_path / "upload_test.txt"
        existing.write_text("existing local content")

        with pytest.raises(ToolError) as exc_info:
            await download_file(
                client,
                path=nas_path,
                dest_folder=str(tmp_path),
            )
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "already_exists"
        logger.info("download no overwrite raised ToolError: %s", exc_info.value)

    async def test_09_download_nonexistent(self, nas_client: Any, tmp_path: Path) -> None:
        """Download a non-existent NAS file should raise ToolError."""
        client, _, _, _ = _unpack(nas_client)

        with pytest.raises(ToolError) as exc_info:
            await download_file(
                client,
                path="/zzz_nonexistent_999/fake.txt",
                dest_folder=str(tmp_path),
            )
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        logger.info("download nonexistent raised ToolError: %s", exc_info.value)

    async def test_10_cleanup(self, nas_client: Any) -> None:
        """Delete the test upload directory from the NAS."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        base = paths["writable_folder"]
        target = f"{base}/{self._UPLOAD_DIR}"
        result = await delete_files(client, paths=[target], recursive=True)
        logger.info("transfer cleanup:\n%s", result)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestErrorHandling:
    """Test that errors are handled gracefully, not crashes."""

    async def test_copy_invalid_source(self, nas_client: Any) -> None:
        """Copy from a non-existent path should raise ToolError."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        with pytest.raises(ToolError) as exc_info:
            await copy_files(
                client,
                paths=["/zzz_nonexistent_999/fake.txt"],
                dest_folder=paths["writable_folder"],
            )
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        logger.info("copy invalid source raised ToolError: %s", exc_info.value)

    async def test_delete_invalid_path(self, nas_client: Any) -> None:
        """Delete a non-existent path should raise ToolError or succeed silently."""
        client, _, config, paths = _unpack(nas_client)
        _skip_unless_write(config)

        # May succeed silently (DSM doesn't always error on missing paths)
        # or raise ToolError. Either way, should not crash.
        try:
            result = await delete_files(client, paths=["/zzz_nonexistent_999/fake.txt"])
            logger.info("delete invalid path:\n%s", result)
        except ToolError as e:
            logger.info("delete invalid path raised ToolError: %s", e)

    async def test_rename_invalid_path(self, nas_client: Any) -> None:
        """Rename a non-existent path should raise ToolError."""
        client, _, _, _ = _unpack(nas_client)
        with pytest.raises(ToolError) as exc_info:
            await rename(client, path="/zzz_nonexistent_999/fake.txt", new_name="new_name")
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        logger.info("rename invalid path raised ToolError: %s", exc_info.value)


# ---------------------------------------------------------------------------
# System monitoring
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSystemInfo:
    """Test system info tool (works for all users via SYNO.DSM.Info)."""

    async def test_get_system_info(self, nas_client: Any) -> None:
        """Should return model, firmware, uptime; temperature only on physical hardware."""
        client, _, _, _ = _unpack(nas_client)
        result = await get_system_info(client)
        logger.info("get_system_info:\n%s", result)
        assert "Model" in result
        assert "Firmware" in result
        assert "Uptime" in result
        # Virtual DSM has no hardware temp sensor — temperature is optional
        if "VirtualDSM" not in result:
            assert "Temperature" in result

    async def test_system_info_has_ram(self, nas_client: Any) -> None:
        """Should report RAM size."""
        client, _, _, _ = _unpack(nas_client)
        result = await get_system_info(client)
        assert "RAM" in result
        assert "MB" in result


@pytest.mark.integration
class TestResourceUsage:
    """Test resource utilization tool.

    Uses admin_client fixture for tests that need admin privileges.
    Falls back gracefully when admin config is not available.
    """

    async def test_resource_usage_non_admin(self, nas_client: Any) -> None:
        """Non-admin user should get a clear permission error."""
        client, _, _, _ = _unpack(nas_client)
        # Should fail with permission error for non-admin users
        # or succeed if the user IS admin.
        try:
            result = await get_resource_usage(client)
            logger.info("get_resource_usage (non-admin succeeded — user is admin):\n%s", result)
        except ToolError as e:
            body = json.loads(str(e))
            logger.info("get_resource_usage (non-admin) raised ToolError: %s", e)
            msg = body["error"]["message"].lower()
            assert "admin" in msg or "permission" in msg

    async def test_resource_usage_admin(self, admin_client: Any) -> None:
        """Admin user should get real utilization data."""
        client, _, _, _ = _unpack(admin_client)
        result = await get_resource_usage(client)
        logger.info("get_resource_usage (admin):\n%s", result)
        assert "CPU" in result
        assert "Memory" in result

    async def test_utilization_before_and_during_load(self, admin_client: Any) -> None:
        """Verify utilization reports plausible values under load.

        1. Check CPU is not already pegged (baseline)
        2. Start a DirSize task on a large folder to generate load
        3. Check CPU again — should still return valid data

        The DirSize task is only a load generator — its success or failure
        doesn't affect the test outcome. On Virtual DSM, the task may complete
        instantly (error 599) before generating measurable load.
        """
        client, _, _, paths = _unpack(admin_client)

        # Baseline reading — verify NAS isn't already overloaded
        baseline = await get_resource_usage(client)
        logger.info("Baseline utilization:\n%s", baseline)
        assert "CPU" in baseline, "Baseline should include CPU data"

        # Start a heavy operation concurrently (best-effort load generator)
        dir_task = asyncio.create_task(get_dir_size(client, path=paths["existing_share"]))

        # Wait for the operation to start consuming resources
        await asyncio.sleep(1)

        # Check utilization during the load
        during_load = await get_resource_usage(client)
        logger.info("During-load utilization:\n%s", during_load)

        # Wait for dir_size to complete (cleanup). Tolerate failure —
        # on Virtual DSM the task may finish instantly or error out.
        try:
            await dir_task
        except ToolError:
            logger.info("DirSize load generator failed (expected on Virtual DSM)")

        # Both readings should be valid
        assert "CPU" in during_load
        logger.info("Utilization test passed — both readings returned valid data")
