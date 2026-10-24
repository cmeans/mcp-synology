"""get_system_info and get_resource_usage by account, on virtual-dsm (#390).

The descriptions now say what an administrator gets and what other accounts don't, so
these check those claims on every DSM version: the non-admin test user (``nas_client``)
gets the DSM.Info fields, a plain "not available" time zone and no CPU line, and
``get_resource_usage`` refuses it; the admin gets the CPU and the time zone with the UTC
offset in force now, which is checked against the NAS's own clock (``date +%z``), not
assumed: each image runs in its own zone (``tests/vdsm/config.py:DSM_TIMEZONES``).

Run with: uv run pytest -m vdsm -v tests/vdsm/test_vdsm_system.py
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.vdsm.conftest import _ssh
from tests.vdsm.golden_image import load_golden_meta

if TYPE_CHECKING:
    from tests.vdsm.container import VirtualDsmContainer

pytestmark = pytest.mark.vdsm


def _rows(result: str) -> dict[str, str]:
    rows = {}
    for line in result.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and value.startswith("  "):
            rows[key] = value.strip()
    return rows


def _nas_offset(container: VirtualDsmContainer, dsm_version: str) -> str:
    """The NAS's current UTC offset as ``UTC+05:30``, from its own ``date``."""
    password = str(load_golden_meta(dsm_version).get("admin_password", ""))
    rc, out = _ssh(container, password, "date +%z", 30, sudo=False)
    assert rc == 0, out
    z = out.strip().splitlines()[-1]  # e.g. "+0530"
    return f"UTC{z[0]}{z[1:3]}:{z[3:5]}"


async def test_non_admin_gets_the_dsm_info_fields_and_a_plain_note(nas_client: Any) -> None:
    from mcp_synology.modules.system.info import get_system_info

    rows = _rows(await get_system_info(nas_client[0]))
    for key in ("Model", "Serial number", "DSM version", "RAM", "System time", "Code page"):
        assert rows.get(key), (key, rows)
    assert rows["Time zone"] == "not available to this account (needs a DSM administrator)"
    assert rows["Needs a DSM administrator"].startswith("CPU, NTP server")
    assert "CPU" not in rows
    assert "NTP" not in rows


async def test_admin_gets_cpu_and_the_current_offset(
    admin_client: Any, vdsm_container: VirtualDsmContainer, dsm_version: str
) -> None:
    from mcp_synology.modules.system.info import get_system_info

    rows = _rows(await get_system_info(admin_client[0]))
    # VirtualDSM names its CPU in cpu_vendor only; the line must not be empty.
    assert rows.get("CPU"), rows
    assert rows["NTP"]
    assert rows["Time zone"].endswith(f", currently {_nas_offset(vdsm_container, dsm_version)}")
    assert "Needs a DSM administrator" not in rows
    for key in ("DSM update", "System crash flag", "USB devices", "SATA devices", "PCIe slots"):
        assert key in rows, (key, rows)


async def test_resource_usage_refuses_a_non_admin(nas_client: Any) -> None:
    from mcp_synology.modules.system.utilization import get_resource_usage

    with pytest.raises(ToolError) as exc:
        await get_resource_usage(nas_client[0])
    error = json.loads(str(exc.value))["error"]
    assert error["code"] == "permission_denied"
    assert "admin account required" in error["message"]


async def test_resource_usage_answers_an_admin(admin_client: Any) -> None:
    from mcp_synology.modules.system.utilization import get_resource_usage

    assert "CPU" in await get_resource_usage(admin_client[0])
