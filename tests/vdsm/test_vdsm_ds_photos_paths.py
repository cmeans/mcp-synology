"""Download Station destinations and Photos folders are taken exactly (#510 PR 2), on vdsm.

DSM keeps whitespace in a folder name, and Download Station and Photos reach ``foo `` or
`` foo`` beside ``foo``; the tools used to ``strip()`` and sent a task or a lookup to
``foo``. DSM cuts a path at a NUL, so a NUL is refused (pinned with a raw call). The v1
CGIs behind ``edit_download`` and ``set_download_config`` read ``+`` in a query as a plus,
so the client sends a space as ``%20``; an ``a+b`` folder beside ``a b`` catches a
regression.

Run: ``uv run pytest tests/vdsm -m vdsm --dsm-version 7.2.2 -k ds_photos_paths``
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import SynologyError
from mcp_synology.core.formatting import escape_controls
from mcp_synology.modules.downloadstation.config import set_download_config
from mcp_synology.modules.downloadstation.tasks import create_download, edit_download
from mcp_synology.modules.filestation.helpers import dsm_str
from mcp_synology.modules.photos.browse import list_photos
from mcp_synology.modules.photos.helpers import Photos
from mcp_synology.modules.photos.organize import photo_folder
from tests.vdsm.golden_image import (
    download_station_skip_reason,
    load_golden_meta,
    photos_skip_reason,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mcp_synology.core.client import DsmClient

pytestmark = pytest.mark.vdsm


@pytest.fixture
async def ds_client(
    nas_client: Any,
    dsm_version: str,
    request: pytest.FixtureRequest,
    live_clients: set[DsmClient],
) -> DsmClient:
    """The test user's client, once Download Station is up (as test_vdsm_downloadstation)."""
    client: DsmClient = nas_client[0]
    reason = download_station_skip_reason(
        load_golden_meta(dsm_version), await client.query_api_info()
    )
    if reason is not None:
        pytest.skip(reason)
    request.getfixturevalue("ds_backend_ready")
    for open_client in list(live_clients):
        await open_client.query_api_info()
    return client


@pytest.fixture
async def photos(nas_client: Any, dsm_version: str, photos_apis: None) -> DsmClient:
    """The test user's client, once Photos is up (as test_vdsm_photos)."""
    client: DsmClient = nas_client[0]
    reason = photos_skip_reason(load_golden_meta(dsm_version), client.api_cache)
    if reason is not None:
        pytest.skip(reason)
    return client


def _magnet() -> str:
    return f"magnet:?xt=urn:btih:{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}&dn=t510b"


def _error(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    text = str(exc.value)
    return json.loads(text[text.index("{") :])["error"]  # type: ignore[no-any-return]


async def _task_destination(client: DsmClient, task_id: str) -> str:
    data = await client.request(
        "SYNO.DownloadStation.Task", "getinfo", 1, {"id": task_id, "additional": "detail"}
    )
    return str(data["tasks"][0]["additional"]["detail"]["destination"])


async def _task_ids(client: DsmClient) -> list[str]:
    data = await client.request("SYNO.DownloadStation.Task", "list", version=1)
    return [str(t["id"]) for t in data.get("tasks", [])]


async def _delete_tasks(client: DsmClient, ids: list[str]) -> None:
    for task_id in ids:
        with contextlib.suppress(SynologyError):
            await client.request(
                "SYNO.DownloadStation.Task",
                "delete",
                1,
                {"id": task_id, "force_complete": "false"},
            )


async def _ds2_create(client: DsmClient, destination: str) -> list[str]:
    data = await client.request_form_post(
        "SYNO.DownloadStation2.Task",
        "create",
        2,
        {
            "type": json.dumps("url"),
            "url": json.dumps([_magnet()]),
            "create_list": "false",
            "destination": json.dumps(destination),
        },
    )
    return [str(t) for t in data.get("task_id", [])]


@pytest.fixture
async def folders(nas_client: Any) -> AsyncIterator[str]:
    """``writable/<unique>`` holding ``foo``, ``foo ``, `` foo``, ``a b`` and ``a+b``."""
    client: DsmClient = nas_client[0]
    name = f"t510b-{uuid.uuid4().hex[:8]}"
    await client.request(
        "SYNO.FileStation.CreateFolder",
        "create",
        params={"folder_path": "/writable", "name": dsm_str(name), "force_parent": "false"},
    )
    try:
        for child in ("foo", "foo ", " foo", "a b", "a+b"):
            await client.request(
                "SYNO.FileStation.CreateFolder",
                "create",
                params={
                    "folder_path": f"/writable/{name}",
                    "name": dsm_str(child),
                    "force_parent": "false",
                },
            )
        yield f"writable/{name}"
    finally:
        with contextlib.suppress(SynologyError):
            await client.request(
                "SYNO.FileStation.Delete",
                "start",
                2,
                {"path": f"/writable/{name}", "recursive": "true"},
            )


class TestDownloadStation:
    async def test_create_reaches_a_padded_folder_exactly(
        self, ds_client: DsmClient, folders: str
    ) -> None:
        client = ds_client
        for child in ("foo ", " foo", "a b"):
            out = await create_download(client, uri=_magnet(), destination=f"{folders}/{child}")
            ids = re.findall(r"\bdbid_\d+\b", out)
            try:
                assert ids, out
                assert await _task_destination(client, ids[0]) == f"{folders}/{child}"
            finally:
                await _delete_tasks(client, ids)

    async def test_v1_create_reaches_a_padded_folder_exactly(
        self, ds_client: DsmClient, folders: str
    ) -> None:
        # A source with username/password goes through v1 Task.create, a form POST to
        # task.cgi (whose form body reads `+` as a space). v1 returns no task IDs, so the new
        # task is found by listing before and after.
        client = ds_client
        for child in ("foo ", " foo", "a b"):
            before = set(await _task_ids(client))
            await create_download(
                client,
                uri=f"http://example.invalid/{uuid.uuid4().hex}.bin",
                destination=f"{folders}/{child}",
                username="u",
                password="p",
            )
            new = sorted(set(await _task_ids(client)) - before)
            try:
                assert len(new) == 1, new
                assert await _task_destination(client, new[0]) == f"{folders}/{child}"
            finally:
                await _delete_tasks(client, new)

    async def test_dsm_cuts_a_destination_at_a_nul_so_it_is_refused(
        self, ds_client: DsmClient, folders: str
    ) -> None:
        client = ds_client
        ids = await _ds2_create(client, f"{folders}/foo\x00x")
        try:
            assert await _task_destination(client, ids[0]) == f"{folders}/foo"
        finally:
            await _delete_tasks(client, ids)
        with pytest.raises(ToolError) as exc:
            await create_download(client, uri=_magnet(), destination=f"{folders}/foo\x00x")
        assert _error(exc)["code"] == "invalid_parameter"

    async def test_edit_and_config_reach_a_spaced_folder_exactly(
        self, ds_client: DsmClient, folders: str
    ) -> None:
        # The v1 CGIs behind them read `+` in a query as a plus: sent as `+`, `a b` went to
        # the `a+b` folder beside it. The client sends %20 (#510).
        client = ds_client
        ids = await _ds2_create(client, f"{folders}/foo")
        try:
            for child in ("foo ", " foo", "a b", "a+b"):
                out = await edit_download(client, task_ids=ids, destination=f"{folders}/{child}")
                assert "ok" in out, out
                assert await _task_destination(client, ids[0]) == f"{folders}/{child}"
        finally:
            await _delete_tasks(client, ids)

        config = await client.request("SYNO.DownloadStation.Info", "getconfig", 1)
        original = config["default_destination"]
        try:
            for child in ("a b", " foo"):
                await set_download_config(client, default_destination=f"{folders}/{child}")
                config = await client.request("SYNO.DownloadStation.Info", "getconfig", 1)
                assert config["default_destination"] == f"{folders}/{child}"
        finally:
            await set_download_config(client, default_destination=original)
        config = await client.request("SYNO.DownloadStation.Info", "getconfig", 1)
        assert config["default_destination"] == original


@pytest.mark.parametrize("space", ["personal", "shared"])
class TestPhotos:
    async def test_folders_are_taken_exactly(self, photos: DsmClient, space: str) -> None:
        ph = Photos(photos, space, "test")  # type: ignore[arg-type]
        root = (await ph.call("Browse.Folder", "get", {"id": 0}, version=2))["folder"]
        name = f"t510b-{uuid.uuid4().hex[:8]}"
        top = (await ph.call("Browse.Folder", "create", {"target_id": root["id"], "name": name}))[
            "folder"
        ]
        try:
            ids = {}
            for child in ("foo", " foo"):
                made = await ph.call(
                    "Browse.Folder", "create", {"target_id": int(top["id"]), "name": child}
                )
                ids[child] = int(made["folder"]["id"])

            # ` foo` is its own folder, and the lookup reaches it, not `foo`.
            got = await ph.folder(f"/{name}/ foo")
            assert int(got["id"]) == ids[" foo"]
            out = await list_photos(photos, space=space, folder=f"/{name}/ foo")
            assert f"/{name}/ foo" in out and "No items to display." in out, out

            # DSM cuts a path at a NUL, so the tools refuse one.
            raw = await ph.call("Browse.Folder", "get", {"name": f"/{name}/foo\x00x"}, version=2)
            assert int(raw["folder"]["id"]) == ids["foo"]
            with pytest.raises(ToolError) as exc:
                await list_photos(photos, space=space, folder=f"/{name}/foo\x00x")
            assert _error(exc)["code"] == "invalid_parameter"

            # DSM keeps these new names as given; photo_folder refuses them.
            for bad in (" bar", "a\x1bb", "a\u200bb", "a\u202eb"):
                made = await ph.call(
                    "Browse.Folder", "create", {"target_id": int(top["id"]), "name": bad}
                )
                # The client escapes what could forge a reply line (#604).
                assert made["folder"]["name"] == escape_controls(f"/{name}/{bad}")
                with pytest.raises(ToolError) as exc:
                    await photo_folder(
                        photos, action="create", parent=f"/{name}", name=bad, space=space
                    )
                assert (_error(exc)["code"], _error(exc)["param"]) == ("invalid_parameter", "name")
        finally:
            with contextlib.suppress(SynologyError):
                await ph.call("Browse.Folder", "delete", {"id": [int(top["id"])]})
