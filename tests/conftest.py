"""Shared test fixtures."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

import keyring
import pytest
from keyring.backends.null import Keyring as NullKeyring

from mcp_synology.core.client import DsmClient
from mcp_synology.core.config import AppConfig
from mcp_synology.core.state import ApiInfoEntry
from tests.personas import ReauthSpy, reauth_spy_on


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """vdsm and real-NAS integration tests can't run under pytest-xdist.

    Each worker would boot its own vdsm VM on the same storage directory, and the
    integration tests run in a fixed order (create, copy, ... delete) on fixed paths of a
    real NAS. So under xdist they fail here, before any fixture touches a VM or the NAS.
    Unit tests are fine in parallel (`-n auto`).
    """
    if os.environ.get("PYTEST_XDIST_WORKER") and {"vdsm", "integration"} & {
        m.name for m in item.iter_markers()
    }:
        pytest.fail("vdsm and integration tests can't run under pytest-xdist; drop -n.")


@pytest.fixture(autouse=True)
def _fake_home(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Point the home directory at an empty per-test folder, and the keyring at nothing (#311).

    Without this, tests write the developer's real ~/.local/state/mcp-synology (the
    update-check state), and anything derived from the home path (the LOCAL PATHS
    instructions, the default Downloads folder) depends on where the suite runs.
    `XDG_CONFIG_HOME` is cleared so the Downloads folder isn't read from the real
    user-dirs.dirs. The keyring answers "nothing stored", so a test that doesn't mock it
    never reads the developer's real credentials (the OS keyring isn't under HOME).
    vdsm and integration tests keep the real home and keyring: podman and SSH need them.
    """
    if {"vdsm", "integration"} & {m.name for m in request.node.iter_markers()}:
        return
    home = tmp_path_factory.mktemp("home")
    mp = request.getfixturevalue("monkeypatch")
    mp.setenv("HOME", str(home))
    mp.setenv("USERPROFILE", str(home))  # Path.home() on Windows
    mp.delenv("XDG_CONFIG_HOME", raising=False)
    previous = keyring.get_keyring()
    keyring.set_keyring(NullKeyring())
    request.addfinalizer(lambda: keyring.set_keyring(previous))

    # The update check (PyPI, GitHub) never reaches the network from a unit test: the
    # server's lifespan starts it (#314). A test that exercises it patches urlopen itself.
    def _no_network(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("network access is disabled in unit tests")

    mp.setattr("mcp_synology.cli.version.urlopen", _no_network)


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
        "SYNO.FileStation.BackgroundTask": ApiInfoEntry(
            path="entry.cgi", min_version=1, max_version=3
        ),
        "SYNO.FileStation.Upload": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2),
        "SYNO.FileStation.Download": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2),
        # Download Station entries mirror a real query.cgi capture (vdsm DSM
        # 7.2.2 / DS 4.1-5012, see tests/modules/downloadstation/fixtures/).
        # v1 APIs live at their own CGI paths; DS2 APIs live at entry.cgi with
        # requestFormat=JSON. Keep these in sync with a re-capture, never by hand.
        "SYNO.DownloadStation.Task": ApiInfoEntry(
            path="DownloadStation/task.cgi", min_version=1, max_version=3
        ),
        "SYNO.DownloadStation.Statistic": ApiInfoEntry(
            path="DownloadStation/statistic.cgi", min_version=1, max_version=1
        ),
        "SYNO.DownloadStation.Info": ApiInfoEntry(
            path="DownloadStation/info.cgi", min_version=1, max_version=2
        ),
        "SYNO.DownloadStation.Schedule": ApiInfoEntry(
            path="DownloadStation/schedule.cgi", min_version=1, max_version=1
        ),
        "SYNO.DownloadStation2.Task": ApiInfoEntry(
            path="entry.cgi", min_version=1, max_version=2, request_format="JSON"
        ),
        "SYNO.DownloadStation2.Settings.Scheduler": ApiInfoEntry(
            path="entry.cgi", min_version=1, max_version=1, request_format="JSON"
        ),
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


@pytest.fixture
def reauth_spy(nas_client: Any) -> Iterator[ReauthSpy]:
    """Record re-auths (and their DSM codes) on ``nas_client``'s AuthManager."""
    _, auth, _, _ = nas_client
    with reauth_spy_on(auth) as spy:
        yield spy
