"""A proxy from the environment that applies to the NAS host gets a warning (#558)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from mcp_synology.cli.main import main
from mcp_synology.core.config import _no_proxy_entry, _proxy_applies, load_config

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
@pytest.mark.parametrize("host", ["nas.home", "10.0.0.5", "localhost", "sub.localhost", "[::1]"])
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
    assert _proxy_applies(conn) is _httpx_uses_a_proxy(f"{scheme}://{host}:{conn.port}/")


def _httpx_uses_a_proxy(url: str) -> bool:
    with httpx.Client(verify=False) as client:  # no certificate loading: this many would be slow
        return client._transport_for_url(httpx.URL(url)) is not client._transport


@pytest.mark.parametrize(
    "host",
    ["nas.home", "10.0.0.5", "localhost", "[fd00::1]", "bücher.example", "xn--bcher-kva.example"],
)
def test_following_the_warnings_advice_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, host: str
) -> None:
    """Pre-QA on #558: `NO_PROXY=[fd00::1]`, the host's own spelling, crashed httpx at client
    build, as `bücher.example` did, and an `xn--` name stayed on the proxy."""
    monkeypatch.setenv("HTTPS_PROXY", _SECRET_PROXY)
    warnings = _warnings(caplog, tmp_path, host=f'"{host}"')
    entry = _no_proxy_entry(host)
    assert warnings[0].endswith(f"or add {entry} to NO_PROXY.")
    monkeypatch.setenv("NO_PROXY", entry)
    conn = load_config(tmp_path / "c.yaml").connection
    assert conn is not None
    assert not _proxy_applies(conn)
    assert not _httpx_uses_a_proxy(f"https://{host}:{conn.port}/")


@pytest.mark.parametrize(
    "no_proxy", ["[::1]", "https://xn--a.example", "all://xn--a.example", "https://", "all://*"]
)
def test_an_entry_it_cant_use_still_warns_and_loads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    no_proxy: str,
) -> None:
    """Pre-QA on #558: a bad `xn--` label raised out of `load_config`, and `all://*` left a
    trailing-dot host silently proxied."""
    monkeypatch.setenv("HTTPS_PROXY", _SECRET_PROXY)
    monkeypatch.setenv("NO_PROXY", no_proxy)
    assert _warnings(caplog, tmp_path, host="nas.home.")


def test_interactive_setup_warns_before_asking_for_the_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Pre-QA on #558: it builds its config without `load_config`, and the typed password went
    through the proxy with nothing said. The answers stop at the username prompt, so the warning
    can only have come before it."""
    for var in ("SYNOLOGY_HOST", "SYNOLOGY_INSTANCE_ID", "MCP_SYNOLOGY_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTP_PROXY", _SECRET_PROXY)
    with (
        patch("mcp_synology.cli.setup._CONFIG_DIR", tmp_path / "config"),
        patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
        patch("keyring.get_password", return_value=None),  # never the real keyring
        patch("keyring.set_password", side_effect=AssertionError("wrote to the keyring")),
        caplog.at_level("WARNING"),
    ):
        result = CliRunner().invoke(main, ["setup"], input="nas.local\nn\n\nread\n\n")
    assert "DSM username" in result.output
    assert "DSM password:" not in result.output
    warnings = [m for m in caplog.messages if "go through a proxy" in m]
    assert len(warnings) == 1
    assert "hunter2" not in warnings[0]
