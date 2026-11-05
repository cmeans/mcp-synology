"""Every Download Station folder argument goes through `normalize_destination` (#510).

A structural test keeps a new tool or argument from skipping it; a matrix test runs each
registered argument through its tool with the spellings that matter: a NUL (refused: DSM
cuts the path there), and a padded or spaced name (sent exactly: DS reaches it; the v1
CGIs behind edit and config get the space as ``%20``, since they read ``+`` as a plus).
"""

from __future__ import annotations

import contextlib
import inspect
import json
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
from tests.modules.downloadstation.dsm import captured, get_route, post_route
from tests.modules.downloadstation.test_config import _manager
from tests.modules.downloadstation.test_tasks import _form, _has_default
from tests.path_words import PATHLIKE_WORDS, words

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


def _tools() -> dict[str, Any]:
    server = MCPServer("test-ds")
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=MagicMock())
    register_ctx = RegisterContext(
        server=server,
        manager=manager,
        allowed_tools={t.name for t in ds_pkg.MODULE_INFO.tools},
        display_name="test-nas",
    )
    ds_pkg.register(register_ctx)
    return {name: t.fn for name, t in server._tool_manager._tools.items()}


def test_every_folder_argument_is_registered() -> None:
    tools = _tools()
    unregistered = []
    for name, fn in tools.items():
        for param in inspect.signature(fn).parameters:
            if not words(param) & PATHLIKE_WORDS:
                continue
            if DESTINATION_PARAMS.get(name) == param or param in LOCAL_PARAMS.get(name, set()):
                continue
            unregistered.append(f"{name}.{param}")
    assert not unregistered, f"folder arguments not in DESTINATION_PARAMS: {unregistered}"
    for name, param in DESTINATION_PARAMS.items():
        assert param in inspect.signature(tools[name]).parameters, name
    assert set(CALLS) == set(DESTINATION_PARAMS.items())


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


@pytest.mark.parametrize(
    ("tool", "api", "method"),
    [
        ("edit_download", "SYNO.DownloadStation.Task", "edit"),
        ("set_download_config", "SYNO.DownloadStation.Info", "setserverconfig"),
    ],
)
@pytest.mark.parametrize("value", ["video/x/foo ", "video/x/ foo", "video/a b", "video/a+b"])
@respx.mock
async def test_edit_and_config_send_the_destination_exactly(
    tool: str, api: str, method: str, value: str, mock_client: DsmClient
) -> None:
    # Their v1 CGIs read `+` in a query as a plus, so a space goes as %20 (#510): sent as
    # `+`, `video/a b` reached `video/a+b`.
    if tool == "set_download_config":
        _manager()
    route = get_route(api, method).respond(json=captured("task_edit_v2"))
    with contextlib.suppress(ToolError):
        await CALLS[tool, DESTINATION_PARAMS[tool]](mock_client, value)
    request = route.calls.last.request
    assert request.url.params[DESTINATION_PARAMS[tool]] == value
    assert b"+" not in request.url.query


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
