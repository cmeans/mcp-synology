"""A proxy from the environment that applies to the NAS host gets a warning (#558)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
import pytest

from mcp_synology.core.config import _proxy_applies, load_config

if TYPE_CHECKING:
    from pathlib import Path

_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
_SECRET_PROXY = "http://user:hunter2@proxy.example:3128"


@pytest.fixture(autouse=True)
def no_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)


def _config(tmp_path: Path, host: str = "nas.home", https: str = "true") -> Path:
    path = tmp_path / "c.yaml"
    path.write_text(
        f"schema_version: 1\ninstance_id: t\nconnection: {{host: {host}, https: {https}}}\n"
        "modules: {filestation: }\n",
        encoding="utf-8",
    )
    return path


def _warnings(caplog: pytest.LogCaptureFixture, tmp_path: Path, **config: str) -> list[str]:
    with caplog.at_level("WARNING"):
        load_config(_config(tmp_path, **config))
    return [m for m in caplog.messages if "go through a proxy" in m]


@pytest.mark.parametrize(
    ("var", "https"),
    [
        ("HTTPS_PROXY", "true"),
        ("https_proxy", "true"),
        ("HTTP_PROXY", "false"),
        ("ALL_PROXY", "true"),
        ("ALL_PROXY", "false"),
        ("all_proxy", "true"),
    ],
)
def test_a_proxy_that_applies_warns_without_its_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    var: str,
    https: str,
) -> None:
    monkeypatch.setenv(var, _SECRET_PROXY)
    assert _warnings(caplog, tmp_path, https=https)
    assert not [m for m in caplog.messages if "hunter2" in m or "proxy.example" in m]


@pytest.mark.parametrize(
    ("var", "https"), [("HTTP_PROXY", "true"), ("HTTPS_PROXY", "false")], ids=["http", "https"]
)
def test_a_proxy_for_the_other_scheme_doesnt_warn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    var: str,
    https: str,
) -> None:
    monkeypatch.setenv(var, _SECRET_PROXY)
    assert not _warnings(caplog, tmp_path, https=https)


@pytest.mark.parametrize("no_proxy", ["nas.home", "*", "other,nas.home", ".home", "nas.home:5001"])
def test_no_proxy_stops_the_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    no_proxy: str,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", _SECRET_PROXY)
    monkeypatch.setenv("NO_PROXY", no_proxy)
    assert not _warnings(caplog, tmp_path)


@pytest.mark.parametrize(
    "no_proxy", ["other.example", ".nas.home", "nas.home:1", "http://nas.home"]
)
def test_a_no_proxy_that_doesnt_cover_the_nas_still_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    no_proxy: str,
) -> None:
    """`.nas.home` is the trap: the standard library skips the proxy for it, and httpx doesn't."""
    monkeypatch.setenv("HTTPS_PROXY", _SECRET_PROXY)
    monkeypatch.setenv("NO_PROXY", no_proxy)
    assert _warnings(caplog, tmp_path)


def test_no_proxy_set_doesnt_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert not _warnings(caplog, tmp_path)


def test_it_shows_at_any_log_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", _SECRET_PROXY)
    log = logging.getLogger("mcp_synology.core.config")
    level = log.level
    log.setLevel(logging.ERROR)
    try:
        assert _warnings(caplog, tmp_path)
    finally:
        log.setLevel(level)


def test_env_only_mode_warns_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MCP_SYNOLOGY_CONFIG", raising=False)
    monkeypatch.setenv("SYNOLOGY_HOST", "nas.home")
    monkeypatch.setenv("HTTP_PROXY", _SECRET_PROXY)
    with caplog.at_level("WARNING"):
        load_config()
    assert [m for m in caplog.messages if "go through a proxy" in m]


@pytest.mark.parametrize(
    "no_proxy",
    [
        None,
        "nas.home",
        "NAS.HOME",
        " nas.home ",
        "*",
        "other.example,*",
        "other.example",
        ".home",
        "home",
        "as.home",
        ".nas.home",
        "*.home",
        "*.nas.home",
        "nas.home:5001",
        "nas.home:5000",
        "nas.home:1",
        "https://nas.home",
        "http://nas.home",
        "all://nas.home",
        "10.0.0.5",
        "10.0.0.0/8",
        "10.0.0.5/32",
        "localhost",
        "::1",
    ],
)
@pytest.mark.parametrize("proxy_var", ["HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", None])
@pytest.mark.parametrize("https", [True, False], ids=["https", "http"])
@pytest.mark.parametrize("host", ["nas.home", "10.0.0.5", "localhost", "[::1]"])
def test_it_agrees_with_what_httpx_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_proxy: str | None,
    proxy_var: str | None,
    https: bool,
    host: str,
) -> None:
    """The warning is only true while it reads as httpx does, whose own reading is private."""
    if proxy_var:
        monkeypatch.setenv(proxy_var, _SECRET_PROXY)
    if no_proxy:
        monkeypatch.setenv("NO_PROXY", no_proxy)
    conn = load_config(_config(tmp_path, host=f'"{host}"', https=str(https).lower())).connection
    assert conn is not None
    scheme = "https" if https else "http"
    with httpx.Client() as client:
        transport = client._transport_for_url(httpx.URL(f"{scheme}://{host}:{conn.port}/"))
    assert _proxy_applies(conn) is (transport is not client._transport)
