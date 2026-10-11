"""Tests for modules/downloadstation/stats.py — get_download_stats."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import respx
from mcp.server.fastmcp.exceptions import ToolError

from mcp_synology.modules.downloadstation.stats import get_download_stats
from tests.modules.downloadstation.dsm import captured, err, get_route, ok

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

STAT = "SYNO.DownloadStation.Statistic"


class TestGetDownloadStats:
    @respx.mock
    async def test_captured_idle_stats(self, mock_client: DsmClient) -> None:
        get_route(STAT, "getinfo", version=1).respond(json=captured("statistic_getinfo"))
        result = await get_download_stats(mock_client)
        assert "Download (total)" in result
        assert "Upload (total)" in result
        assert "eMule" not in result

    @respx.mock
    async def test_renders_active_speeds(self, mock_client: DsmClient) -> None:
        get_route(STAT, "getinfo", version=1).respond(
            json=ok({"speed_download": 5242880, "speed_upload": 1048576})
        )
        result = await get_download_stats(mock_client)
        assert "5 MB/s" in result
        assert "1 MB/s" in result

    @respx.mock
    async def test_includes_emule_when_present(self, mock_client: DsmClient) -> None:
        # eMule keys are not in the vdsm capture (eMule disabled there); shape
        # per the official DS Web API guide's Statistic.getinfo response.
        get_route(STAT, "getinfo", version=1).respond(
            json=ok(
                {
                    "speed_download": 0,
                    "speed_upload": 0,
                    "emule_speed_download": 128 * 1024,
                    "emule_speed_upload": 64 * 1024,
                }
            )
        )
        result = await get_download_stats(mock_client)
        assert "Download (eMule)" in result

    @respx.mock
    async def test_dsm_error_propagates_as_tool_error(self, mock_client: DsmClient) -> None:
        get_route(STAT, "getinfo", version=1).respond(json=err(105))
        with pytest.raises(ToolError, match="Permission denied"):
            await get_download_stats(mock_client)
