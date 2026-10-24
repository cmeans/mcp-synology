"""System monitoring module: MODULE_INFO, register()."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp_synology.modules import (
    ApiRequirement,
    ModuleInfo,
    PermissionTier,
    ToolInfo,
    default_annotations,
)

if TYPE_CHECKING:
    from mcp_synology.modules import RegisterContext

MODULE_INFO = ModuleInfo(
    name="system",
    description="Monitor Synology NAS system health: CPU, memory, disk, network, temperature",
    required_apis=[
        ApiRequirement(api_name="SYNO.DSM.Info", min_version=1),
        ApiRequirement(api_name="SYNO.Core.System", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Core.Region.NTP", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Core.System.Status", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Core.System.Utilization", min_version=1, optional=True),
    ],
    tools=[
        ToolInfo(
            name="get_system_info",
            description=(
                "Get everything DSM reports about the NAS. Every account gets the model, serial "
                "number, DSM version, RAM, uptime, the NAS's local time and its code page, and "
                "the temperature where the NAS has a sensor. A DSM administrator account also "
                "gets the CPU, time zone with the current UTC offset, NTP server, firmware "
                "date, whether a DSM update is ready to install, crash and network-card flags, "
                "redundant power and eSATA, and attached devices; any other account gets a line "
                "saying so. Use this to identify the NAS, check its basic health or tell the "
                "time on it. For live CPU, memory, disk and network load, use "
                "get_resource_usage."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_resource_usage",
            description=(
                "Get live resource utilization: CPU load, memory usage, disk I/O, "
                "and network throughput. Needs a DSM administrator account; any other "
                "account gets permission_denied. Use this to check if the NAS is under "
                "heavy load before starting expensive work on it. For the model, firmware, "
                "temperature and uptime, use get_system_info."
            ),
            permission_tier=PermissionTier.READ,
        ),
    ],
)


def register(ctx: RegisterContext) -> None:
    """Register system monitoring tools with the MCP server."""
    from mcp_synology.modules.system.info import get_system_info
    from mcp_synology.modules.system.utilization import get_resource_usage

    server = ctx.server
    manager = ctx.manager

    # Build annotation lookup
    _tool_annos = {
        t.name: t.annotations or default_annotations(t.permission_tier) for t in MODULE_INFO.tools
    }

    def _desc(name: str) -> str:
        return next(t.description for t in MODULE_INFO.tools if t.name == name)

    if "get_system_info" in ctx.allowed_tools:

        @server.tool(
            name="get_system_info",
            description=_desc("get_system_info"),
            annotations=_tool_annos["get_system_info"],
        )
        async def tool_get_system_info() -> str:
            client = await manager.get_client()
            return await get_system_info(client)

    if "get_resource_usage" in ctx.allowed_tools:

        @server.tool(
            name="get_resource_usage",
            description=_desc("get_resource_usage"),
            annotations=_tool_annos["get_resource_usage"],
        )
        async def tool_get_resource_usage() -> str:
            client = await manager.get_client()
            return await get_resource_usage(client)
