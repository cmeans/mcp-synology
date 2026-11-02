"""Every Download Station folder argument goes through `normalize_destination` (#510).

A structural test keeps a new tool or argument from skipping it; a matrix test runs each
registered argument through its tool with the spellings that matter: a NUL (refused: DSM
cuts the path there), a padded name (taken exactly: DS reaches it) and a space where the
call can't take one (refused before any request).
"""

from __future__ import annotations

import inspect
import json
import re
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import mcp_synology.modules.downloadstation as ds_pkg
from mcp_synology.modules import RegisterContext
from mcp_synology.modules.downloadstation.config import set_download_config
from mcp_synology.modules.downloadstation.helpers import DESTINATION_PARAMS
from mcp_synology.modules.downloadstation.tasks import create_download, edit_download
from tests.modules.downloadstation.dsm import captured, post_route
from tests.modules.downloadstation.test_tasks import _form, _has_default

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp_synology.core.client import DsmClient

# Arguments that name a file on this machine, not a NAS folder.
LOCAL_PARAMS = {"create_download": {"torrent_file_path"}}

# Each registered argument, called through its tool with `value` as that argument.
CALLS: dict[tuple[str, str], Callable[[DsmClient, str], Awaitable[str]]] = {
    ("create_download", "destination"): lambda c, v: create_download(
        c, uri="magnet:?xt=urn:btih:" + "0" * 40, destination=v
    ),
    ("edit_download", "destination"): lambda c, v: edit_download(
        c, task_ids=["dbid_1"], destination=v
    ),
    ("set_download_config", "default_destination"): lambda c, v: set_download_config(
        c, default_destination=v
    ),
}

# What DSM does with a space in the destination, per call (`SPACE_ERRORS`).
TAKES_SPACES = {"create_download": True, "edit_download": False, "set_download_config": False}


def _tools() -> dict[str, Any]:
    server = MCPServer("test-ds")
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=MagicMock())
    register_ctx = RegisterContext(
        server=server,
        manager=manager,
        allowed_tools={t.name for t in ds_pkg.MODULE_INFO.tools},
        settings_dict={},
        display_name="test-nas",
    )
    ds_pkg.register(register_ctx)
    return {name: t.fn for name, t in server._tool_manager._tools.items()}


def _words(param: str) -> set[str]:
    return set(re.split(r"_", param))


def test_every_folder_argument_is_registered() -> None:
    tools = _tools()
    unregistered = []
    for name, fn in tools.items():
        for param in inspect.signature(fn).parameters:
            if not _words(param) & {"destination", "folder", "path", "dir"}:
                continue
            if DESTINATION_PARAMS.get(name) == param or param in LOCAL_PARAMS.get(name, set()):
                continue
            unregistered.append(f"{name}.{param}")
    assert not unregistered, f"folder arguments not in DESTINATION_PARAMS: {unregistered}"
    for name, param in DESTINATION_PARAMS.items():
        assert param in inspect.signature(tools[name]).parameters, name
    assert set(CALLS) == set(DESTINATION_PARAMS.items())
    assert set(TAKES_SPACES) == set(DESTINATION_PARAMS)


def _error(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc.value))["error"]  # type: ignore[no-any-return]


@pytest.mark.parametrize(("tool", "param"), list(CALLS))
@respx.mock  # no routes: a request would fail the test
async def test_a_nul_is_refused_before_any_request(
    tool: str, param: str, mock_client: DsmClient
) -> None:
    value = "video/x/foo\x00y"
    with pytest.raises(ToolError) as exc:
        await CALLS[tool, param](mock_client, value)
    error = _error(exc)
    assert (error["code"], error["param"], error["value"]) == ("invalid_parameter", param, value)
    assert "NUL" in error["message"]


@pytest.mark.parametrize(("tool", "param"), [k for k in CALLS if not TAKES_SPACES[k[0]]])
@pytest.mark.parametrize("value", ["video/x/foo ", "video/x/ foo", "video/a b"])
@respx.mock  # no routes: a request would fail the test
async def test_a_space_is_refused_where_dsm_cant_take_one(
    tool: str, param: str, value: str, mock_client: DsmClient
) -> None:
    with pytest.raises(ToolError) as exc:
        await CALLS[tool, param](mock_client, value)
    error = _error(exc)
    assert (error["code"], error["param"], error["value"]) == ("invalid_parameter", param, value)
    assert "has a space in it" in error["message"]


@pytest.mark.parametrize("value", ["video/x/foo ", "video/x/ foo", "video/a b"])
@respx.mock
async def test_create_sends_a_padded_or_spaced_destination_exactly(
    value: str, mock_client: DsmClient
) -> None:
    _has_default()
    route = post_route("SYNO.DownloadStation2.Task", "create", version=2).respond(
        json=captured("ds2_task_create_url")
    )
    await create_download(mock_client, uri="magnet:?xt=urn:btih:" + "0" * 40, destination=value)
    assert json.loads(_form(route.calls.last.request)["destination"]) == value
