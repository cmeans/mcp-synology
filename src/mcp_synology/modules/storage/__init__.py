"""Storage module: MODULE_INFO, register().

Phase 1 is 11 READ tools over Storage Manager: pools, volumes, disks and S.M.A.R.T.,
SSD caches, schedules, settings, external storage and share snapshots
(docs/specs/storage-module-spec.md). Every API is administrator-only.
"""

# No `from __future__ import annotations` here: the SDK reads the
# Annotated[..., Field(description=...)] metadata of each handler's parameters.

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

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
    name="storage",
    description=(
        "Storage pools, volumes, disks and their health, SSD cache, external storage, snapshots"
    ),
    required_apis=[
        ApiRequirement(api_name="SYNO.Storage.CGI.Storage", min_version=1),
        ApiRequirement(api_name="SYNO.Storage.CGI.Smart", min_version=1),
        ApiRequirement(api_name="SYNO.Core.Storage.Disk", min_version=1),
        ApiRequirement(api_name="SYNO.Storage.CGI.Volume", min_version=1),
        ApiRequirement(api_name="SYNO.Storage.CGI.Pool", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Storage.CGI.Smart.Scheduler", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Storage.CGI.Spare", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Storage.CGI.Spare.Conf", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Storage.CGI.HddMan", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Storage.CGI.Flashcache", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Storage.CGI.BtrfsDedupe", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Storage.CGI.Check", min_version=1, optional=True),
        ApiRequirement(
            api_name="SYNO.Core.ExternalDevice.Storage.USB", min_version=1, optional=True
        ),
        ApiRequirement(
            api_name="SYNO.Core.ExternalDevice.Storage.eSATA", min_version=1, optional=True
        ),
        ApiRequirement(
            api_name="SYNO.Core.ExternalDevice.Storage.EUnit", min_version=1, optional=True
        ),
        ApiRequirement(
            api_name="SYNO.Core.ExternalDevice.Storage.Setting", min_version=1, optional=True
        ),
        ApiRequirement(api_name="SYNO.Core.Share", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Core.Share.Snapshot", min_version=2, optional=True),
        ApiRequirement(api_name="SYNO.Core.System.SystemHealth", min_version=1, optional=True),
    ],
    tools=[
        ToolInfo(
            name="get_storage_overview",
            description=(
                "Get the overall health of the NAS's storage: DSM's own status summary, every "
                "storage pool, volume, disk and SSD cache with its status in one line each, "
                "anything that needs attention (a degraded pool, a failing or incompatible disk, "
                "a nearly full volume) and storage tasks that are running or scheduled next (data "
                "scrubbing, S.M.A.R.T. tests, RAID rebuilds). Needs a DSM administrator account. "
                "Start here for any question about disks, RAID or free space; for one object's "
                "details use get_disk, get_volume or list_storage_pools."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="list_storage_pools",
            description=(
                "List the NAS's storage pools: RAID type, member disks and their state, size and "
                "use, status, any repair, rebuild or expansion in progress with its percentage, "
                "and data scrubbing (state, last and next run). Also lists hot spares (which "
                "pools each protects), disks that could become spares, whether a spare replaces a "
                "failed disk automatically, and pools that are missing or detected but not "
                "assembled. Needs a DSM administrator account."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="list_volumes",
            description=(
                "List the NAS's volumes: path (/volume1), file system, size, used and free space "
                "with the percentage used, status and what DSM says about it (for example almost "
                "full), the storage pool it's on, encryption and lock state (DSM 7.2.2 and "
                "later), and data scrubbing state. Needs a DSM administrator account. For a "
                "volume's space by category (shared folders, snapshots, iSCSI LUNs), TRIM and "
                "deduplication, use get_volume."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_volume",
            description=(
                "Get everything about one volume: what list_volumes shows plus its space by "
                "category (shared folders, snapshots, iSCSI LUNs, other), SSD TRIM support and "
                "schedule, Btrfs deduplication (supported, last run and savings), space "
                "reclamation state, the SSD cache attached to it, and every other field DSM "
                "reports. Needs a DSM administrator account."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="list_disks",
            description=(
                "List the NAS's disks: bay, model, vendor, serial number, firmware, size, type "
                "(HDD or SSD, SATA, SAS, M.2), temperature, status, S.M.A.R.T. status, bad-sector "
                "count, remaining life for SSDs, and what each is used for (which pool, hot "
                "spare, SSD cache, or not in use). Shows a running S.M.A.R.T. or other disk test "
                "with its progress. Needs a DSM administrator account. For one disk's S.M.A.R.T. "
                "attributes, test history and health trend, use get_disk."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_disk",
            description=(
                "Get one disk's full health: everything list_disks shows, every S.M.A.R.T. "
                "attribute (current, worst, threshold, raw value, status), the last quick and "
                "extended S.M.A.R.T. test results, recent test history, the 12-month trend of bad "
                "sectors, retries and SSD remaining life, any S.M.A.R.T. warnings, and its "
                "write-cache setting. Needs a DSM administrator account."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="list_ssd_caches",
            description=(
                "List the NAS's SSD caches: which volume each speeds up, read-only or read-write, "
                "RAID type and SSDs, size, read and write hit rates, status, whether metadata is "
                "pinned, and the SSD cache advisor's recent recommendations (suggested cache "
                "size). Needs a DSM administrator account."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_storage_schedules",
            description=(
                "Get every scheduled storage task: data scrubbing (on or off, interval, which "
                "pools, next run), S.M.A.R.T. test tasks (quick or extended, which disks, "
                "enabled, next run), space reclamation and Btrfs deduplication windows, and SSD "
                "TRIM, with what's running now. Needs a DSM administrator account. Snapshot "
                "schedules are per shared folder: use list_snapshots."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_storage_settings",
            description=(
                "Get the NAS's storage settings: RAID resync speed, fast repair, automatic data "
                "scrubbing and repair, disk health warning thresholds (bad sectors, SSD remaining "
                "life, months of life left), health reports, automatic hot-spare replacement, the "
                "drive compatibility database's version, and external-storage settings. Needs a "
                "DSM administrator account."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="list_external_storage",
            description=(
                "List storage attached to the NAS from outside: USB and eSATA drives (name, size, "
                "file system, partitions, shared folder) and expansion units (model, status, "
                "disks). Needs a DSM administrator account."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="list_snapshots",
            description=(
                "List a shared folder's snapshots (time taken, description, locked, immutable on "
                "DSM 7.2.2 and later, scheduled or manual) and its snapshot schedule. Without a "
                "shared folder, lists every shared folder's snapshot count and whether a schedule "
                "is on. Needs a DSM administrator account and the Snapshot Replication package."
            ),
            permission_tier=PermissionTier.READ,
        ),
    ],
)


def register(ctx: "RegisterContext") -> None:
    """Register the storage tools allowed for this connection."""
    from mcp_synology.modules.storage.caches import list_ssd_caches
    from mcp_synology.modules.storage.disks import get_disk, list_disks
    from mcp_synology.modules.storage.external import list_external_storage
    from mcp_synology.modules.storage.overview import get_storage_overview
    from mcp_synology.modules.storage.pools import list_storage_pools
    from mcp_synology.modules.storage.schedules import get_storage_schedules
    from mcp_synology.modules.storage.settings import get_storage_settings
    from mcp_synology.modules.storage.snapshots import list_snapshots
    from mcp_synology.modules.storage.volumes import get_volume, list_volumes

    server = ctx.server
    manager = ctx.manager
    annotations = {
        t.name: t.annotations or default_annotations(t.permission_tier) for t in MODULE_INFO.tools
    }

    def _desc(name: str) -> str:
        return next(t.description for t in MODULE_INFO.tools if t.name == name)

    def _tool(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return server.tool(name=name, description=_desc(name), annotations=annotations[name])

    # Tools without parameters: each reads the NAS once per call.
    simple = {
        "get_storage_overview": get_storage_overview,
        "list_storage_pools": list_storage_pools,
        "list_volumes": list_volumes,
        "list_disks": list_disks,
        "list_ssd_caches": list_ssd_caches,
        "get_storage_schedules": get_storage_schedules,
        "get_storage_settings": get_storage_settings,
        "list_external_storage": list_external_storage,
    }
    for name, handler in simple.items():
        if name in ctx.allowed_tools:
            _register_simple(_tool(name), manager, handler)

    if "get_volume" in ctx.allowed_tools:

        @_tool("get_volume")
        async def tool_get_volume(
            volume: Annotated[
                str,
                Field(
                    description=(
                        "The volume's path (`/volume1`) or ID (`volume_1`), as list_volumes "
                        "shows it."
                    )
                ),
            ],
        ) -> str:
            client = await manager.get_client()
            return await get_volume(client, volume)

    if "get_disk" in ctx.allowed_tools:

        @_tool("get_disk")
        async def tool_get_disk(
            disk: Annotated[
                str,
                Field(
                    description=(
                        "The disk's ID (`sda`), device (`/dev/sda`) or bay name (`Drive 1`), "
                        "as list_disks shows it."
                    )
                ),
            ],
            history_limit: Annotated[
                int, Field(description="How many recent test-history entries to show (1–100).")
            ] = 10,
        ) -> str:
            client = await manager.get_client()
            return await get_disk(client, disk, history_limit)

    if "list_snapshots" in ctx.allowed_tools:

        @_tool("list_snapshots")
        async def tool_list_snapshots(
            share: Annotated[
                str | None,
                Field(
                    description=(
                        "The shared folder's name (`media`, not a path). Leave empty for a "
                        "count per shared folder."
                    )
                ),
            ] = None,
            offset: Annotated[
                int, Field(description="Skip this many snapshots (newest first).")
            ] = 0,
            limit: Annotated[
                int, Field(description="Show at most this many snapshots (1–500).")
            ] = 50,
        ) -> str:
            client = await manager.get_client()
            return await list_snapshots(client, share, offset, limit)


def _register_simple(decorator: Any, manager: Any, handler: Any) -> None:
    """A tool with no parameters (its own function, so each closure keeps its handler)."""

    async def tool() -> str:
        client = await manager.get_client()
        result: str = await handler(client)
        return result

    tool.__name__ = f"tool_{handler.__name__}"
    decorator(tool)
