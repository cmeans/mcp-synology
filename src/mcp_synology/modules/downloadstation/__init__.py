"""Download Station module: MODULE_INFO, register(), DownloadStationSettings.

Phase 1 (READ tools): list_downloads, get_download_info, get_download_stats,
get_download_config, get_schedule. Phase 2 adds task CRUD writes; Phase 3
adds BT search + RSS. See docs/superpowers/specs/2026-05-13-downloadstation-module-design.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from mcp_synology.modules import (
    ApiRequirement,
    ModuleInfo,
    PermissionTier,
    ToolInfo,
    default_annotations,
)

if TYPE_CHECKING:
    from mcp_synology.modules import RegisterContext


class DownloadStationSettings(BaseModel):
    """Download Station module settings.

    Phase 1 has no settings; the schema is declared so server.py module-loading
    machinery can pass an empty settings dict cleanly. Fields land in the task
    that needs them.
    """


MODULE_INFO = ModuleInfo(
    name="downloadstation",
    description=("Manage Synology Download Station tasks, schedule, and configuration"),
    required_apis=[
        ApiRequirement(api_name="SYNO.DownloadStation.Task", min_version=1),
        ApiRequirement(api_name="SYNO.DownloadStation.Info", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.DownloadStation.Schedule", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.DownloadStation.Statistic", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.DownloadStation.RSS.Site", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.DownloadStation.RSS.Feed", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.DownloadStation.BTSearch", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.DownloadStation2.Task", min_version=1, optional=True),
        ApiRequirement(
            api_name="SYNO.DownloadStation2.Settings.Scheduler", min_version=1, optional=True
        ),
    ],
    tools=[
        ToolInfo(
            name="list_downloads",
            description=(
                "List download tasks in the Download Station queue. Filter by status "
                "(downloading/finished/paused/error/all). Returns a table with id, title, "
                "type (bt/http/ftp/nzb), status, size, progress%, current speed, and ETA. "
                "Accounts that are not Download Station managers see only their own tasks "
                "(the output says so), so an empty list does not mean the NAS is idle."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_download_info",
            description=(
                "Get detailed information for a specific download task: detail (status, "
                "destination, URI), transfer (size downloaded/uploaded, speed, peers), "
                "files (per-file selection for BT), trackers (BT only), peers (BT only). "
                "Use list_downloads first to find the task_id."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_download_stats",
            description=(
                "Get current Download Station throughput statistics: total download and "
                "upload speed across all tasks, plus per-service (BT, HTTP/FTP, eMule) "
                "breakdowns when those services are enabled."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_download_config",
            description=(
                "Get Download Station global configuration: default destination, BT/HTTP/"
                "FTP/NZB/eMule max speeds, eMule and auto-unzip state. For accounts that are "
                "not Download Station managers DSM returns a per-user view (labeled as "
                "such), not the NAS-wide settings. Use get_schedule for the weekly plan."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_schedule",
            description=(
                "Get the Download Station schedule: whether it is enabled, the limited-speed "
                "(alt) download/upload rates, and the weekly plan as a 7-day × 24-hour grid "
                "where each hour is full speed, limited, or no download. The weekly plan "
                "needs a Download Station manager/admin account; otherwise only the flags "
                "are shown."
            ),
            permission_tier=PermissionTier.READ,
        ),
        # Phase 2 WRITE tools
        ToolInfo(
            name="create_download",
            description=(
                "Create one or more download tasks. Pass a comma-separated list of "
                "URIs (HTTP, FTP, magnet, etc.) via `uri` OR pass a local path to "
                "a .torrent / .nzb file via `torrent_file_path`. `destination` is a "
                "share-relative path (omit to use DSM's default destination). "
                "`username` / `password` may be supplied for protected URLs (not for "
                "torrent files). Returns the new task IDs when DSM provides them; long "
                "magnet links and URI lists are supported."
            ),
            permission_tier=PermissionTier.WRITE,
        ),
        ToolInfo(
            name="delete_download",
            description=(
                "Remove one or more download tasks. `task_ids` is a list. Files from "
                "finished tasks are always kept. For unfinished tasks the partial data "
                "is discarded (irreversible), unless `force_complete` is true, which "
                "moves the incomplete file into the task's destination instead."
            ),
            permission_tier=PermissionTier.WRITE,
        ),
        ToolInfo(
            name="pause_download",
            description=(
                "Pause one or more download tasks. `task_ids` is a list. Pausing "
                "an already-paused task is a no-op for that id; pausing a finished "
                "task returns invalid task action."
            ),
            permission_tier=PermissionTier.WRITE,
        ),
        ToolInfo(
            name="resume_download",
            description=(
                "Resume one or more paused download tasks. `task_ids` is a list. "
                "Resuming an already-active task is a no-op for that id."
            ),
            permission_tier=PermissionTier.WRITE,
        ),
        ToolInfo(
            name="edit_download",
            description=(
                "Edit task parameters. `task_ids` is a list. Currently supports "
                "`destination` (change the task's output share). Requires a newer "
                "Download Station (Task API v2); older versions get a clear error."
            ),
            permission_tier=PermissionTier.WRITE,
        ),
        ToolInfo(
            name="set_download_config",
            description=(
                "Set Download Station global configuration. All parameters are "
                "optional — only fields you pass get updated. Rates are KB/s; 0 "
                "means unlimited. Pass `default_destination` to change DS's "
                "default destination share. Requires a Download Station manager/admin "
                "account: DSM silently ignores this change from other accounts, so the "
                "tool refuses it."
            ),
            permission_tier=PermissionTier.WRITE,
        ),
        ToolInfo(
            name="set_schedule",
            description=(
                "Set the Download Station schedule. All parameters optional — only what "
                "you pass gets updated. `enabled` / `emule_enabled` toggle whether the "
                "schedule applies (DSM lets any Download Station user toggle this NAS-wide "
                "flag). `schedule_plan` is a 168-character string, 24 chars per day, Sun "
                "first through Sat; each char is '0' no download, '1' full speed, '2' "
                "limited. `download_rate` / `upload_rate` set the limited speed in KB/s "
                "(0 = unlimited). The plan and rates need a Download Station manager/admin "
                "account; if that write is refused, nothing is applied."
            ),
            permission_tier=PermissionTier.WRITE,
        ),
    ],
    settings_schema=DownloadStationSettings,
)


def register(ctx: RegisterContext) -> None:
    """Register Download Station tools with the MCP server."""
    from mcp_synology.modules.downloadstation.config import (
        get_download_config,
        get_schedule,
    )
    from mcp_synology.modules.downloadstation.stats import get_download_stats
    from mcp_synology.modules.downloadstation.tasks import (
        get_download_info,
        list_downloads,
    )

    server = ctx.server
    manager = ctx.manager

    _tool_annos = {
        t.name: t.annotations or default_annotations(t.permission_tier) for t in MODULE_INFO.tools
    }

    def _desc(name: str) -> str:
        return next(t.description for t in MODULE_INFO.tools if t.name == name)

    if "list_downloads" in ctx.allowed_tools:

        @server.tool(
            name="list_downloads",
            description=_desc("list_downloads"),
            annotations=_tool_annos["list_downloads"],
        )
        async def tool_list_downloads(
            status_filter: str = "all",
            offset: int = 0,
            limit: int = 100,
        ) -> str:
            client = await manager.get_client()
            return await list_downloads(
                client,
                status_filter=status_filter,
                offset=offset,
                limit=limit,
            )

    if "get_download_info" in ctx.allowed_tools:

        @server.tool(
            name="get_download_info",
            description=_desc("get_download_info"),
            annotations=_tool_annos["get_download_info"],
        )
        async def tool_get_download_info(task_id: str) -> str:
            client = await manager.get_client()
            return await get_download_info(client, task_id=task_id)

    if "get_download_stats" in ctx.allowed_tools:

        @server.tool(
            name="get_download_stats",
            description=_desc("get_download_stats"),
            annotations=_tool_annos["get_download_stats"],
        )
        async def tool_get_download_stats() -> str:
            client = await manager.get_client()
            return await get_download_stats(client)

    if "get_download_config" in ctx.allowed_tools:

        @server.tool(
            name="get_download_config",
            description=_desc("get_download_config"),
            annotations=_tool_annos["get_download_config"],
        )
        async def tool_get_download_config() -> str:
            client = await manager.get_client()
            return await get_download_config(client)

    if "get_schedule" in ctx.allowed_tools:

        @server.tool(
            name="get_schedule",
            description=_desc("get_schedule"),
            annotations=_tool_annos["get_schedule"],
        )
        async def tool_get_schedule() -> str:
            client = await manager.get_client()
            return await get_schedule(client)

    if "create_download" in ctx.allowed_tools:

        @server.tool(
            name="create_download",
            description=_desc("create_download"),
            annotations=_tool_annos["create_download"],
        )
        async def tool_create_download(
            uri: str | None = None,
            torrent_file_path: str | None = None,
            destination: str | None = None,
            username: str | None = None,
            password: str | None = None,
        ) -> str:
            from mcp_synology.modules.downloadstation.tasks import create_download

            client = await manager.get_client()
            return await create_download(
                client,
                uri=uri,
                torrent_file_path=torrent_file_path,
                destination=destination,
                username=username,
                password=password,
            )

    if "delete_download" in ctx.allowed_tools:

        @server.tool(
            name="delete_download",
            description=_desc("delete_download"),
            annotations=_tool_annos["delete_download"],
        )
        async def tool_delete_download(
            task_ids: list[str],
            force_complete: bool = False,
        ) -> str:
            from mcp_synology.modules.downloadstation.tasks import delete_download

            client = await manager.get_client()
            return await delete_download(
                client,
                task_ids=task_ids,
                force_complete=force_complete,
            )

    if "pause_download" in ctx.allowed_tools:

        @server.tool(
            name="pause_download",
            description=_desc("pause_download"),
            annotations=_tool_annos["pause_download"],
        )
        async def tool_pause_download(task_ids: list[str]) -> str:
            from mcp_synology.modules.downloadstation.tasks import pause_download

            client = await manager.get_client()
            return await pause_download(client, task_ids=task_ids)

    if "resume_download" in ctx.allowed_tools:

        @server.tool(
            name="resume_download",
            description=_desc("resume_download"),
            annotations=_tool_annos["resume_download"],
        )
        async def tool_resume_download(task_ids: list[str]) -> str:
            from mcp_synology.modules.downloadstation.tasks import resume_download

            client = await manager.get_client()
            return await resume_download(client, task_ids=task_ids)

    if "edit_download" in ctx.allowed_tools:

        @server.tool(
            name="edit_download",
            description=_desc("edit_download"),
            annotations=_tool_annos["edit_download"],
        )
        async def tool_edit_download(
            task_ids: list[str],
            destination: str | None = None,
        ) -> str:
            from mcp_synology.modules.downloadstation.tasks import edit_download

            client = await manager.get_client()
            return await edit_download(client, task_ids=task_ids, destination=destination)

    if "set_download_config" in ctx.allowed_tools:

        @server.tool(
            name="set_download_config",
            description=_desc("set_download_config"),
            annotations=_tool_annos["set_download_config"],
        )
        async def tool_set_download_config(
            bt_max_download: int | None = None,
            bt_max_upload: int | None = None,
            emule_max_download: int | None = None,
            emule_max_upload: int | None = None,
            default_destination: str | None = None,
        ) -> str:
            from mcp_synology.modules.downloadstation.config import set_download_config

            client = await manager.get_client()
            return await set_download_config(
                client,
                bt_max_download=bt_max_download,
                bt_max_upload=bt_max_upload,
                emule_max_download=emule_max_download,
                emule_max_upload=emule_max_upload,
                default_destination=default_destination,
            )

    if "set_schedule" in ctx.allowed_tools:

        @server.tool(
            name="set_schedule",
            description=_desc("set_schedule"),
            annotations=_tool_annos["set_schedule"],
        )
        async def tool_set_schedule(
            enabled: bool | None = None,
            emule_enabled: bool | None = None,
            schedule_plan: str | None = None,
            download_rate: int | None = None,
            upload_rate: int | None = None,
        ) -> str:
            from mcp_synology.modules.downloadstation.config import set_schedule

            client = await manager.get_client()
            return await set_schedule(
                client,
                enabled=enabled,
                emule_enabled=emule_enabled,
                schedule_plan=schedule_plan,
                download_rate=download_rate,
                upload_rate=upload_rate,
            )
