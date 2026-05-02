"""Shared test fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

import pytest

from mcp_synology.core.client import DsmClient
from mcp_synology.core.config import AppConfig
from mcp_synology.core.state import ApiInfoEntry

BASE_URL = "http://nas:5000"


def make_test_config(**overrides: Any) -> AppConfig:
    """Create a test config with sensible defaults."""
    raw: dict[str, Any] = {
        "schema_version": 1,
        "instance_id": "test",
        "connection": {"host": "nas", "port": 5000},
        "modules": {
            "filestation": {
                "enabled": True,
                "permission": "write",
                "settings": {
                    "hide_recycle_in_listings": False,
                    "file_type_indicator": "emoji",
                    "async_timeout": 120,
                },
            }
        },
    }
    raw.update(overrides)
    return AppConfig(**raw)


def make_client(api_cache: dict[str, ApiInfoEntry] | None = None) -> DsmClient:
    """Build a sync `DsmClient` for tests that don't need the async `mock_client` fixture.

    Pass `api_cache` to seed `client._api_cache`; omit for an empty cache.
    """
    client = DsmClient(base_url=BASE_URL)
    if api_cache:
        client._api_cache = api_cache
    return client


def make_minimal_api_cache() -> dict[str, ApiInfoEntry]:
    """Tiny cache for testing version negotiation behavior in isolation.

    Distinct from `make_api_cache()` — this exposes only Auth + List so a test
    can verify behavior on uncached APIs without conftest's full FileStation
    surface getting in the way.
    """
    return {
        "SYNO.API.Auth": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=7),
        "SYNO.FileStation.List": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2),
    }


def make_api_cache() -> dict[str, ApiInfoEntry]:
    """Create a mock API info cache with File Station APIs."""
    return {
        "SYNO.API.Auth": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=7),
        "SYNO.FileStation.Info": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2),
        # max_version=3 matches DSM 7.x (where #68 surfaced); production code
        # MUST pin to v2 explicitly via `negotiate_version(..., max_version=2)`
        # because v3 reinterprets multi-path semantics and silently breaks
        # comma-joined `path` queries.
        "SYNO.FileStation.List": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=3),
        "SYNO.FileStation.Search": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2),
        "SYNO.FileStation.DirSize": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2),
        "SYNO.FileStation.CreateFolder": ApiInfoEntry(
            path="entry.cgi", min_version=1, max_version=2
        ),
        "SYNO.FileStation.Rename": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2),
        "SYNO.FileStation.CopyMove": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=3),
        "SYNO.FileStation.Delete": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2),
        "SYNO.FileStation.Upload": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2),
        "SYNO.FileStation.Download": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2),
    }


@pytest.fixture
def mock_config() -> AppConfig:
    """Provide a test config."""
    return make_test_config()


@pytest.fixture
async def mock_client() -> AsyncGenerator[DsmClient, None]:
    """Provide a DsmClient with a mocked API cache."""
    client = DsmClient(base_url=BASE_URL)
    client._api_cache = make_api_cache()
    async with client:
        yield client
