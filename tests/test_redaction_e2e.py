"""#514's acceptance canary, end to end: real modules, the real server and real logging.

`tests/test_redaction_sinks.py` plants credentials at each output point. This file runs the
paths that reach them, as the design asked (issue #514, design section 4): `serve`'s startup
with DEBUG logging and a log file, a tool call that can't connect, and Download Station's
listing, details and create, with credentials in every place the user's own data or DSM can put
one:

- the password, from an environment variable;
- a 2FA device token, from a keyring entry recorded for this host;
- DSM's session ID and device token in its login reply;
- a tracker passkey in a task's URI, its title and its tracker list;
- a URL with userinfo in the error a connection fails with.

Every tool reply and error, the instructions, the progress messages, stderr and the log file
are read, and no canary may appear in any of them.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import httpx
import keyring
import pytest
import respx
from mcp.client import Client

from mcp_synology.cli import logging_
from mcp_synology.core.config import comparable_target
from mcp_synology.server import create_server
from tests.conftest import BASE_URL, make_api_cache, make_test_config
from tests.memory_keyring import MemoryKeyring
from tests.modules.downloadstation.dsm import captured

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

PASSWORD = "CANARYpw514e2e"
DEVICE_TOKEN = "CANARYdid514e2e"
SESSION = "CANARYsid514e2e"
# As long as a real tracker passkey (32 hex digits): a path token needs 16+ letters and digits.
PASSKEY = "CANARYpk514e2e0123456789abcdef"
USERINFO = "CANARYui514e2e"
CANARIES = (PASSWORD, DEVICE_TOKEN, SESSION, PASSKEY, USERINFO)

TORRENT = f"https://tracker.example/download/{PASSKEY}.torrent?passkey={PASSKEY}"


def _assert_clean(where: str, text: str) -> None:
    found = [c for c in CANARIES if c in text]
    lines = [line for line in text.splitlines() if any(c in line for c in found)]
    assert not found, f"{where} carries {found} in: {lines}"


@pytest.fixture
def isolated_logging(tmp_path: Path) -> Iterator[Path]:
    """The root logger as `serve` sets it up, at DEBUG with a log file; restored after."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    for h in handlers:
        root.removeHandler(h)
    log_file = tmp_path / "serve.log"
    try:
        logging_._init_early_logging(verbose=True)
        logging_._configure_logging("debug", str(log_file))
        yield log_file
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in handlers:
            root.addHandler(h)
        root.setLevel(level)
        logging_._take_early_records()


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """The password from the environment; a device token in a keyring entry recorded for this
    host and port, so the login sends it."""
    for var in ("SYNOLOGY_HOST", "SYNOLOGY_PORT", "SYNOLOGY_HTTPS", "SYNOLOGY_INSTANCE_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SYNOLOGY_USERNAME", "admin")
    monkeypatch.setenv("SYNOLOGY_PASSWORD", PASSWORD)
    store = MemoryKeyring()
    store.values[("mcp-synology/test", "device_id")] = DEVICE_TOKEN
    store.values[("mcp-synology/test", "host")] = comparable_target("nas", 5000)
    previous = keyring.get_keyring()
    keyring.set_keyring(store)
    request.addfinalizer(lambda: keyring.set_keyring(previous))


def _api_info() -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            name: {
                "path": entry.path,
                "minVersion": entry.min_version,
                "maxVersion": entry.max_version,
                **({"requestFormat": entry.request_format} if entry.request_format else {}),
            }
            for name, entry in make_api_cache().items()
        },
    }


def _with_passkeys(body: dict[str, Any]) -> dict[str, Any]:
    """A captured task reply whose tasks carry the passkey wherever DSM can put one."""
    for task in body["data"]["tasks"]:
        task["title"] = f"show.torrent?passkey={PASSKEY}"
        additional = task.setdefault("additional", {})
        additional.setdefault("detail", {})["uri"] = TORRENT
        if "tracker" in additional:
            additional["tracker"] = [
                {"url": f"https://tracker.example/announce/{PASSKEY}", "status": "Success"}
            ]
    return body


class FakeDsm:
    """Answers by `api` and `method`, from the query string or a form body, with real captures;
    records anything it doesn't know, so the test fails on it rather than passing on a 404."""

    def __init__(self) -> None:
        self.unknown: list[str] = []
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = {k: v[-1] for k, v in parse_qs(request.url.query.decode()).items()}
        if request.method == "POST" and request.content:
            ctype = request.headers.get("content-type", "")
            if "x-www-form-urlencoded" in ctype:
                body = parse_qs(request.content.decode())
                params.update({k: v[-1] for k, v in body.items()})
        api, method = params.get("api", ""), params.get("method", "")
        self.calls.append((api, method))
        if request.url.path.endswith("query.cgi"):
            return httpx.Response(200, json=_api_info())
        if api == "SYNO.API.Auth" and method == "login":
            data = {"sid": SESSION, "did": DEVICE_TOKEN, "is_portal_port": False}
            return httpx.Response(200, json={"success": True, "data": data})
        if api == "SYNO.API.Auth" and method == "logout":
            return httpx.Response(200, json={"success": True})
        replies = {
            ("SYNO.DownloadStation.Task", "list"): lambda: _with_passkeys(captured("task_list_v1")),
            ("SYNO.DownloadStation.Task", "getinfo"): lambda: _with_passkeys(
                captured("task_getinfo_v1")
            ),
            ("SYNO.DownloadStation2.Task", "create"): lambda: captured("ds2_task_create_url"),
            ("SYNO.DownloadStation.Task", "create"): lambda: captured("v1_task_create_url"),
            ("SYNO.DownloadStation.Info", "getinfo"): lambda: captured("info_getinfo_admin"),
            ("SYNO.DownloadStation.Info", "getconfig"): lambda: captured("info_getconfig_admin"),
        }
        reply = replies.get((api, method))
        if reply is None:
            self.unknown.append(f"{api} {method}")
            return httpx.Response(200, json={"success": False, "error": {"code": 103}})
        return httpx.Response(200, json=reply())


def _text(result: Any) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


def _config() -> Any:
    return make_test_config(
        modules={
            "filestation": {"enabled": True, "permission": "write"},
            "downloadstation": {"enabled": True, "permission": "write"},
        },
        # The user's own text: `redact_secrets` masks a query's credential and userinfo, not a
        # token in a URL's path, which only Download Station's own `redact_urls` masks.
        custom_instructions=(
            f"The tracker is https://tracker.example/rss?passkey={PASSKEY} and the NAS is "
            f"https://admin:{USERINFO}@nas:5000/."
        ),
    )


async def test_serve_and_download_station_never_show_a_credential(
    isolated_logging: Path, credentials: None, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeDsm()
    outputs: list[tuple[str, str]] = []
    progress: list[str] = []

    async def on_progress(done: float, total: float | None, message: str | None) -> None:
        progress.append(message or "")

    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.route().mock(side_effect=fake)
        server = create_server(_config())
        outputs.append(("instructions", server.instructions or ""))
        async with Client(server) as client:
            for tool, arguments in [
                ("list_downloads", {}),
                ("get_download_info", {"task_id": "dbid_1"}),
                ("create_download", {"uri": TORRENT}),
            ]:
                result = await client.call_tool(tool, arguments, progress_callback=on_progress)
                outputs.append((tool, _text(result)))

    assert fake.unknown == [], fake.unknown
    # The paths really ran: a login, and Download Station's calls.
    assert ("SYNO.API.Auth", "login") in fake.calls
    assert any(api.startswith("SYNO.DownloadStation") for api, _ in fake.calls)
    # The replies are real ones, the task shown with its title masked, not an error.
    listing = dict(outputs)["list_downloads"]
    assert "show.torrent" in listing, listing

    for h in logging.getLogger().handlers:
        h.flush()
    log = isolated_logging.read_text(encoding="utf-8")
    assert "SYNO.API.Auth" in log and "SYNO.DownloadStation" in log
    for where, text in [*outputs, ("progress", "\n".join(progress)), ("log file", log)]:
        _assert_clean(where, text)
    out, err = capsys.readouterr()
    _assert_clean("stdout", out)
    _assert_clean("stderr", err)


async def test_a_tool_call_that_cant_connect_never_shows_a_credential(
    isolated_logging: Path, credentials: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The connection fails with the NAS's URL, userinfo included, in the error's text."""
    refused = httpx.ConnectError(f"refused by https://admin:{USERINFO}@nas:5000/webapi")
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.route().mock(side_effect=refused)
        server = create_server(_config())
        async with Client(server) as client:
            results = [
                await client.call_tool(tool, {}) for tool in ("list_shares", "list_downloads")
            ]
    assert all(r.is_error for r in results)
    for h in logging.getLogger().handlers:
        h.flush()
    for r in results:
        text = _text(r)
        assert "unavailable" in text, text
        _assert_clean("error", text)
        json.loads(text[text.index("{") :])
    _assert_clean("log file", isolated_logging.read_text(encoding="utf-8"))
    out, err = capsys.readouterr()
    _assert_clean("stdout", out)
    _assert_clean("stderr", err)
