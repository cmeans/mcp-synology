"""``serve`` logs out of DSM when it exits, on virtual-dsm (#402).

Starts the real ``mcp-synology serve`` against the container, makes one tool call, then
ends the process by closing stdin or with SIGTERM or SIGINT. Afterwards the server's
session ID is sent again: DSM must reject it (119), which only a logout causes.

Before #402 the logout ran at atexit, where asyncio can't send a request, so the
session stayed open on every exit, and SIGTERM or SIGINT hung the process for good.

The session ID comes from a test-only ``sitecustomize`` on the server's ``PYTHONPATH``
that wraps ``AuthManager.login``; the server code itself is unchanged.

Run: ``uv run pytest tests/vdsm -m vdsm --dsm-version 7.2.2 -k logout``
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import yaml

if TYPE_CHECKING:
    from mcp_synology.core.config import AppConfig

pytestmark = pytest.mark.vdsm

_SITECUSTOMIZE = """
import asyncio
import os
from mcp_synology.core import auth as _auth
from mcp_synology.core import client as _client

_login = _auth.AuthManager.login


async def login(self):
    sid = await _login(self)
    with open(os.environ["MCP_TEST_SID_FILE"], "w") as f:
        f.write(sid)
    return sid


_auth.AuthManager.login = login

_request = _client.DsmClient.request


async def request(self, api, method, *args, **kwargs):
    # Holds the search open, so the signal comes while the call is still running.
    hold = os.environ.get("MCP_TEST_HOLD_FILE")
    if hold and api == "SYNO.FileStation.Search" and method == "list":
        open(hold, "w").close()
        await asyncio.sleep(60)
    return await _request(self, api, method, *args, **kwargs)


_client.DsmClient.request = request
"""

_EXIT_TIMEOUT = 15


def _write_config(path: Path, config: AppConfig) -> None:
    conn = config.connection
    assert conn is not None and config.auth is not None
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "instance_id": config.instance_id,
                "connection": {"host": conn.host, "port": conn.port, "https": False},
                "auth": {"username": config.auth.username, "password": config.auth.password},
                "modules": {"filestation": {"enabled": True, "permission": "read"}},
                # A PyPI check still running would hold up the EOF exit (see #405) and
                # write the real ~/.local/state; this test is about the logout.
                "check_for_updates": False,
            }
        )
    )


async def _session_alive(base_url: str, sid: str) -> tuple[bool, int | None]:
    """One File Station call with ``sid`` as the ``id`` cookie, as the client sends it."""
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(
            f"{base_url}/webapi/entry.cgi",
            params={"api": "SYNO.FileStation.List", "method": "list_share", "version": "2"},
            headers={"Cookie": f"id={sid}"},
        )
    body = resp.json()
    return bool(body.get("success")), (body.get("error") or {}).get("code")


@pytest.mark.parametrize("how", ["eof", "sigterm", "sigint", "sigterm-in-call"])
async def test_serve_logs_out_when_it_exits(
    how: str, vdsm_config: tuple[AppConfig, dict[str, str]], tmp_path: Path
) -> None:
    config, paths = vdsm_config
    conn = config.connection
    assert conn is not None
    base_url = f"http://{conn.host}:{conn.port}"
    cfg_file = tmp_path / "config.yaml"
    _write_config(cfg_file, config)
    (tmp_path / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    sid_file = tmp_path / "sid"
    hold_file = tmp_path / "hold"
    # No SYNOLOGY_* from the developer's shell: they override the config file, and could
    # point the server at a real NAS.
    env = {k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")}
    env |= {
        "PYTHONPATH": os.pathsep.join(filter(None, [str(tmp_path), env.get("PYTHONPATH")])),
        "MCP_TEST_SID_FILE": str(sid_file),
        "SYNOLOGY_LOG_LEVEL": "debug",
    }
    if how == "sigterm-in-call":
        env["MCP_TEST_HOLD_FILE"] = str(hold_file)
    log = tmp_path / "serve.log"
    serve = Path(sys.executable).with_name("mcp-synology")
    with log.open("w") as errlog:
        proc = await asyncio.create_subprocess_exec(
            str(serve),
            "serve",
            "--config",
            str(cfg_file),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errlog,
            env=env,
        )
    assert proc.stdin is not None and proc.stdout is not None

    def send(msg: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(msg) + "\n").encode())

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "vdsm-logout-test", "version": "1"},
                },
            }
        )
        await proc.stdin.drain()
        await asyncio.wait_for(proc.stdout.readline(), 60)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_shares", "arguments": {}},
            }
        )
        await proc.stdin.drain()
        reply = json.loads(await asyncio.wait_for(proc.stdout.readline(), 120))
        assert not reply["result"].get("isError"), reply

        sid = sid_file.read_text()
        assert await _session_alive(base_url, sid) == (True, None), "probe can't see the session"

        if how == "sigterm-in-call":
            # A call still running when the signal comes (QA rounds 1 and 2): the hook
            # holds its Search `list`, so the signal can't land after the reply. The
            # logout must run anyway, shielded from the cancellation that unwinds the call.
            share = paths["existing_share"]
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "search_files", "arguments": {"folder_path": share}},
                }
            )
            await proc.stdin.drain()
            deadline = time.monotonic() + 60
            while not hold_file.exists():
                assert time.monotonic() < deadline, "the search never reached Search/list"
                await asyncio.sleep(0.05)
        started = time.monotonic()
        if how == "eof":
            proc.stdin.close()
        else:
            proc.send_signal(signal.SIGINT if how == "sigint" else signal.SIGTERM)
        code = await asyncio.wait_for(proc.wait(), _EXIT_TIMEOUT)
        took = time.monotonic() - started
        rest = (await proc.stdout.read()).decode()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    expected = {
        "eof": 0,
        "sigterm": 128 + signal.SIGTERM,
        "sigint": 128 + signal.SIGINT,
        "sigterm-in-call": 128 + signal.SIGTERM,
    }[how]
    assert code == expected, (code, log.read_text()[-2000:])
    assert took < 6, took  # logout bounded by LOGOUT_TIMEOUT (5 s)
    text = log.read_text()
    assert "Logout successful" in text, text[-2000:]
    if how == "sigterm-in-call":
        replies = [json.loads(line) for line in rest.splitlines() if line.strip()]
        assert not any(r.get("id") == 2 for r in replies), replies  # canceled, never answered
        # The canceled search still stops and cleans its NAS task (#391).
        for step in ("stop", "clean"):
            assert f"SYNO.FileStation.Search/{step} — success" in text, text[-2000:]
    alive, err = await _session_alive(base_url, sid)
    assert not alive and err == 119, (alive, err)
