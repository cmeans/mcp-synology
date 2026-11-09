"""#620 overwrite notes, against pre-QA r2 on #623: the note reflects only what was there
before the call, never what the call made, and its lookups never outlive or burst the call."""

from __future__ import annotations

import asyncio
import gc
import json
from typing import TYPE_CHECKING

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.filestation.operations import (
    copy_files,
    move_files,
    restore_from_recycle_bin,
)
from tests.conftest import BASE_URL

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient


def _info(path: str, isdir: bool) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "success": True,
            "data": {"files": [{"isdir": isdir, "path": path, "name": path.rsplit("/", 1)[-1]}]},
        },
    )


def _gone(path: str) -> httpx.Response:
    return httpx.Response(
        200, json={"success": True, "data": {"files": [{"code": 408, "path": path}]}}
    )


@respx.mock
async def test_nested_restore_implicit_parent(mock_client: DsmClient) -> None:
    """Restore #recycle/X (folder) and #recycle/X/Y/f.txt with overwrite: /home/X is created
    by the call (force_parent of /home/X/Y), yet X's reply says it merged into it."""
    exists: dict[str, bool] = {
        "/home": True,
        "/home/#recycle/X": True,
        "/home/#recycle/X/Y/f.txt": False,
    }

    def respond(request: httpx.Request) -> httpx.Response:
        p = dict(request.url.params)
        api, method = p.get("api", ""), p.get("method", "")
        if method == "getinfo":
            path = p["path"]
            return _info(path, exists[path]) if path in exists else _gone(path)
        if api == "SYNO.FileStation.CreateFolder":
            folder = f"{p['folder_path']}/{json.loads(p['name'])}"
            exists["/home/X"] = True
            exists[folder] = True
            return httpx.Response(
                200, json={"success": True, "data": {"folders": [{"path": folder}]}}
            )
        if method == "start":
            return httpx.Response(200, json={"success": True, "data": {"taskid": "t-1"}})
        if method == "status":
            return httpx.Response(200, json={"success": True, "data": {"finished": True}})
        return httpx.Response(200, json={"success": True, "data": {}})

    respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=respond)
    out = await restore_from_recycle_bin(
        mock_client, share="home", paths=["X", "X/Y/f.txt"], overwrite=True
    )
    assert "Merged" not in out, out


@respx.mock
async def test_late_lookup_sees_own_landing(mock_client: DsmClient) -> None:
    """The target lookup answers after the move landed: a fresh move reads as a replace."""
    landed = {"v": False}

    async def respond(request: httpx.Request) -> httpx.Response:
        p = dict(request.url.params)
        method = p.get("method", "")
        if method == "getinfo":
            path = p["path"]
            if path == "/w/dst/a.txt":
                await asyncio.sleep(0.3)  # a slow answer (busy NAS, retried connection)
                return _info(path, False) if landed["v"] else _gone(path)
            return _info(path, path == "/w/dst")
        if method == "start":
            landed["v"] = True
            return httpx.Response(200, json={"success": True, "data": {"taskid": "t1"}})
        if method == "status":
            return httpx.Response(200, json={"success": True, "data": {"finished": True}})
        return httpx.Response(200, json={"success": True, "data": {}})

    respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=respond)
    out = await move_files(
        mock_client, paths=["/w/src/a.txt"], dest_folder="/w/dst", overwrite=True
    )
    assert "Replaced" not in out, out


@respx.mock
async def test_refused_call_leaves_no_task(mock_client: DsmClient) -> None:
    """A source check refuses while lookups are pending or failing: nothing logged or left."""
    loop = asyncio.get_running_loop()
    errors: list[dict] = []
    loop.set_exception_handler(lambda _l, ctx: errors.append(ctx))

    async def respond(request: httpx.Request) -> httpx.Response:
        p = dict(request.url.params)
        path = p.get("path", "")
        if p.get("method") == "getinfo":
            if path.startswith("/w/dst/"):
                await asyncio.sleep(0.05)
                return httpx.Response(502, text="bad")
            if path == "/w/src/b.txt":
                return _gone(path)
            return _info(path, path == "/w/dst")
        return httpx.Response(200, json={"success": True, "data": {}})

    respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=respond)
    with pytest.raises(ToolError):
        await copy_files(
            mock_client,
            paths=["/w/src/a.txt", "/w/src/b.txt"],
            dest_folder="/w/dst",
            overwrite=True,
        )
    await asyncio.sleep(0.2)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    gc.collect()
    await asyncio.sleep(0)
    assert not pending and not errors


@respx.mock
async def test_failed_lookups_after_success(mock_client: DsmClient) -> None:
    """Lookups that fail fast with a refusal still running -> exception retrieved?"""
    loop = asyncio.get_running_loop()
    errors: list[dict] = []
    loop.set_exception_handler(lambda _l, ctx: errors.append(ctx))

    async def respond(request: httpx.Request) -> httpx.Response:
        p = dict(request.url.params)
        path = p.get("path", "")
        if p.get("method") == "getinfo":
            if path.startswith("/w/dst/"):
                raise httpx.ConnectError("boom")
            if path == "/w/src/b.txt":
                await asyncio.sleep(0.05)
                return _gone(path)
            return _info(path, path == "/w/dst")
        return httpx.Response(200, json={"success": True, "data": {}})

    respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=respond)
    with pytest.raises(ToolError):
        await copy_files(
            mock_client,
            paths=["/w/src/a.txt", "/w/src/b.txt"],
            dest_folder="/w/dst",
            overwrite=True,
        )
    gc.collect()
    await asyncio.sleep(0.1)
    gc.collect()
    assert not errors


@respx.mock
async def test_cancel_mid_wait(mock_client: DsmClient) -> None:
    loop = asyncio.get_running_loop()
    errors: list[dict] = []
    loop.set_exception_handler(lambda _l, ctx: errors.append(ctx))

    async def respond(request: httpx.Request) -> httpx.Response:
        p = dict(request.url.params)
        path, method = p.get("path", ""), p.get("method", "")
        if method == "getinfo":
            if path.startswith("/w/dst/"):
                await asyncio.sleep(5)
                return _gone(path)
            return _info(path, path == "/w/dst")
        if method == "start":
            return httpx.Response(200, json={"success": True, "data": {"taskid": "t1"}})
        if method == "status":
            return httpx.Response(200, json={"success": True, "data": {"finished": False}})
        return httpx.Response(200, json={"success": True, "data": {}})

    respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=respond)
    t = asyncio.ensure_future(
        copy_files(mock_client, paths=["/w/src/a.txt"], dest_folder="/w/dst", overwrite=True)
    )
    await asyncio.sleep(0.5)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    await asyncio.sleep(0.2)
    pending = [x for x in asyncio.all_tasks() if x is not asyncio.current_task()]
    assert not pending and not errors


@respx.mock
async def test_burst(mock_client: DsmClient) -> None:
    state = {"now": 0, "max": 0, "first_src_wait": None}
    t0 = asyncio.get_running_loop().time()

    async def respond(request: httpx.Request) -> httpx.Response:
        p = dict(request.url.params)
        path, method = p.get("path", ""), p.get("method", "")
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        try:
            if method == "getinfo" and path == "/w/src/f0" and state["first_src_wait"] is None:
                state["first_src_wait"] = round(asyncio.get_running_loop().time() - t0, 3)
            await asyncio.sleep(0.02)
            if method == "getinfo":
                if path.startswith("/w/dst/"):
                    return _gone(path)
                return _info(path, path == "/w/dst")
            if method == "start":
                return httpx.Response(200, json={"success": True, "data": {"taskid": "t1"}})
            if method == "status":
                return httpx.Response(200, json={"success": True, "data": {"finished": True}})
            return httpx.Response(200, json={"success": True, "data": {}})
        finally:
            state["now"] -= 1

    respx.get(f"{BASE_URL}/webapi/entry.cgi").mock(side_effect=respond)
    await copy_files(
        mock_client,
        paths=[f"/w/src/f{i}" for i in range(300)],
        dest_folder="/w/dst",
        overwrite=True,
    )
    # The lookups are capped (4) so the checks and starts aren't queued behind a burst.
    assert state["max"] <= 6, state
