"""Download Station module: MODULE_INFO, register(), DownloadStationSettings.

READ tools: list_downloads, get_download_info, get_download_stats,
get_download_config, get_schedule. WRITE tools: task create/delete/pause/
resume/edit, set_download_config, set_schedule. BT search + RSS are planned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, field_validator

from mcp_synology.core.local_paths import allowed_local_roots, check_local_path_entries
from mcp_synology.modules import (
    ApiRequirement,
    ModuleInfo,
    PermissionTier,
    ToolInfo,
    default_annotations,
)

if TYPE_CHECKING:
    from mcp_synology.modules import RegisterContext

# Clients may auto-approve tools that don't claim destructiveHint. Deleting a task loses
# it (and an unfinished task's partial data). The setters and edit_download replace a
# stored value (NAS-wide settings, a task's destination), so the previous one is lost,
# but repeating a call changes nothing more. pause/resume only toggle a task's state.
_ANNO_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)
_ANNO_DESTRUCTIVE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True
)


class DownloadStationSettings(BaseModel):
    """Download Station module settings."""

    # Local folders create_download may read torrent files from, added to the top-level
    # allowed_local_paths (#257). Unset with no top-level list: the Downloads folder.
    allowed_local_paths: list[str] | None = Field(default=None, min_length=1)

    @field_validator("allowed_local_paths")
    @classmethod
    def _absolute_local_paths(cls, value: list[str] | None) -> list[str] | None:
        return check_local_path_entries("allowed_local_paths", value)


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
                "List download tasks in the Download Station queue. Returns a table with id, "
                "title, type (bt/http/ftp/nzb), status, size, progress%, download and upload "
                "speed, ETA, and completion time. Paged (25 by default); the footer gives the "
                "true total and the next offset. A status filter or sort covers the whole "
                "queue, not just one page: sort_by=completed with limit=1 gives the most "
                "recently finished task. "
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
                "Use list_downloads first to find the task_id. For totals across all tasks, "
                "use get_download_stats; for settings, use get_download_config."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_download_stats",
            description=(
                "Get current Download Station throughput statistics: total download and "
                "upload speed across all tasks, plus eMule download/upload speed when the "
                "eMule service is enabled. For per-task speeds use list_downloads."
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
                "Create one or more download tasks, from URIs or from a local .torrent / .nzb "
                "file. `destination` must be an "
                "existing folder, share-relative (DS doesn't create folders; make a new one "
                "first with File Station's `create_folder` when File Station write is enabled, "
                "or ask the user to create it in "
                "DSM). Omit it to use your default destination (a "
                "non-manager who has none must set it first with `set_download_config`, "
                "because DS won't start their tasks until then). "
                "Returns the new task IDs when DSM provides them (not "
                "for URLs with `username` / `password`); long magnet links and URI "
                "lists are supported."
            ),
            permission_tier=PermissionTier.WRITE,
        ),
        ToolInfo(
            name="delete_download",
            description=(
                "Remove one or more download tasks. A deleted "
                "task can't be restored (its source URL is gone with it), so name the "
                "tasks and confirm with the user before deleting. Files from "
                "finished tasks are always kept. For unfinished tasks the partial data "
                "is discarded (irreversible); don't rely on `force_complete` to keep it."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="pause_download",
            description=(
                "Pause one or more download tasks. An "
                "already-paused or finished task is refused for that id (error 405), "
                "while the others are paused. If none of the ids can be paused, the "
                "whole call fails with invalid_parameter."
            ),
            permission_tier=PermissionTier.WRITE,
        ),
        ToolInfo(
            name="resume_download",
            description=(
                "Resume one or more paused download tasks. An "
                "active or finished task is refused for that id (error 405), while "
                "the others are resumed. list_downloads with status_filter=`paused` "
                "finds the paused tasks."
            ),
            permission_tier=PermissionTier.WRITE,
        ),
        ToolInfo(
            name="edit_download",
            description=(
                "Edit task parameters. Currently supports "
                "`destination` (change the task's output share). Requires a newer "
                "Download Station (Task API v2); older versions get a clear error. This "
                "changes only the listed tasks; for global settings, use set_download_config."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE_IDEMPOTENT,
        ),
        ToolInfo(
            name="set_download_config",
            description=(
                "Set Download Station global configuration. All parameters are "
                "optional — only fields you pass get updated. The rate fields require a "
                "Download Station "
                "manager/admin account: DSM silently ignores them from other accounts, so "
                "the tool refuses them. Any user can set `default_destination`: DSM "
                "applies it per user, and DS won't start a non-manager's tasks until it "
                "is set."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE_IDEMPOTENT,
        ),
        ToolInfo(
            name="set_schedule",
            description=(
                "Set the Download Station schedule. All parameters optional — only what "
                "you pass gets updated. `enabled` / `emule_enabled` toggle whether the "
                "schedule applies (DSM lets any Download Station user toggle this NAS-wide "
                "flag). `schedule_plan` is in the NAS's local hours, and `download_rate` / "
                "`upload_rate` are the limited speed. The plan and rates need a Download "
                "Station manager/admin "
                "account; if that write is refused, nothing is applied."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE_IDEMPOTENT,
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

    settings = DownloadStationSettings(**ctx.settings_dict)
    # Folders create_download may read torrent files from (#257).
    local_roots = allowed_local_roots(
        settings.allowed_local_paths,
        shared_paths=ctx.allowed_local_paths,
        module="downloadstation",
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
            status_filter: Annotated[
                str,
                Field(
                    description=(
                        "`all` (default), `downloading` (also waiting, checking or "
                        "extracting), `finished` (also seeding), `paused` or `error`."
                    )
                ),
            ] = "all",
            sort_by: Annotated[
                str,
                Field(
                    description=(
                        "`queue` (default: queue order, oldest first), `created` or "
                        "`completed` (both newest first; never-completed tasks last)."
                    )
                ),
            ] = "queue",
            offset: Annotated[
                int,
                Field(description="Tasks to skip, 0 or more; the footer gives the next offset."),
            ] = 0,
            limit: Annotated[int, Field(description="Most tasks to return, 1 or more.")] = 25,
        ) -> str:
            client = await manager.get_client()
            return await list_downloads(
                client,
                status_filter=status_filter,
                sort_by=sort_by,
                offset=offset,
                limit=limit,
            )

    if "get_download_info" in ctx.allowed_tools:

        @server.tool(
            name="get_download_info",
            description=_desc("get_download_info"),
            annotations=_tool_annos["get_download_info"],
        )
        async def tool_get_download_info(
            task_id: Annotated[
                str, Field(description="One task ID (`dbid_…`), from list_downloads.")
            ],
        ) -> str:
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
            uri: Annotated[
                str | None,
                Field(
                    description=(
                        "One or more source URIs, comma-separated: HTTP, HTTPS, FTP or "
                        "magnet links. Pass this or `torrent_file_path`, not both."
                    )
                ),
            ] = None,
            torrent_file_path: Annotated[
                str | None,
                Field(
                    description=(
                        "Path on this machine to a `.torrent` or `.nzb` file, inside the "
                        "allowed local folders. Pass this or `uri`, not both."
                    )
                ),
            ] = None,
            destination: Annotated[
                str | None,
                Field(
                    description=(
                        "Existing folder to download into, share-relative "
                        "(`video/Torrents`). Omit it to use your default destination."
                    )
                ),
            ] = None,
            username: Annotated[
                str | None,
                Field(description="User name for a protected `uri`; not for torrent files."),
            ] = None,
            password: Annotated[
                str | None,
                Field(description="Password for a protected `uri`; not for torrent files."),
            ] = None,
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
                local_roots=local_roots,
            )

    if "delete_download" in ctx.allowed_tools:

        @server.tool(
            name="delete_download",
            description=_desc("delete_download"),
            annotations=_tool_annos["delete_download"],
        )
        async def tool_delete_download(
            task_ids: Annotated[
                list[str], Field(description="IDs of the tasks to remove, from list_downloads.")
            ],
            force_complete: Annotated[
                bool,
                Field(
                    description=(
                        "Ask DSM to keep an unfinished task's partial file instead of "
                        "discarding it (DSM may discard it anyway). Leave false (default) "
                        "for finished tasks: DSM refuses them with it."
                    )
                ),
            ] = False,
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
        async def tool_pause_download(
            task_ids: Annotated[
                list[str], Field(description="IDs of the tasks to pause, from list_downloads.")
            ],
        ) -> str:
            from mcp_synology.modules.downloadstation.tasks import pause_download

            client = await manager.get_client()
            return await pause_download(client, task_ids=task_ids)

    if "resume_download" in ctx.allowed_tools:

        @server.tool(
            name="resume_download",
            description=_desc("resume_download"),
            annotations=_tool_annos["resume_download"],
        )
        async def tool_resume_download(
            task_ids: Annotated[
                list[str],
                Field(description="IDs of the paused tasks to resume, from list_downloads."),
            ],
        ) -> str:
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
            task_ids: Annotated[
                list[str], Field(description="IDs of the tasks to change, from list_downloads.")
            ],
            destination: Annotated[
                str | None,
                Field(
                    description=(
                        "New destination folder, share-relative (`video/Torrents`); it must "
                        "already exist. The only editable field, so a call needs it."
                    )
                ),
            ] = None,
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
            bt_max_download: Annotated[
                int | None,
                Field(
                    description=(
                        "NAS-wide BitTorrent download limit in KB/s; `0` means unlimited. "
                        "Manager/admin accounts only."
                    )
                ),
            ] = None,
            bt_max_upload: Annotated[
                int | None,
                Field(
                    description=(
                        "NAS-wide BitTorrent upload limit in KB/s; `0` means unlimited. "
                        "Manager/admin accounts only."
                    )
                ),
            ] = None,
            emule_max_download: Annotated[
                int | None,
                Field(
                    description=(
                        "NAS-wide eMule download limit in KB/s; `0` means unlimited. "
                        "Manager/admin accounts only."
                    )
                ),
            ] = None,
            emule_max_upload: Annotated[
                int | None,
                Field(
                    description=(
                        "NAS-wide eMule upload limit in KB/s; `0` means unlimited. "
                        "Manager/admin accounts only."
                    )
                ),
            ] = None,
            default_destination: Annotated[
                str | None,
                Field(
                    description=(
                        "Folder new tasks go to when create_download gets no destination, "
                        "share-relative (`video/Torrents`). A non-manager sets only their own."
                    )
                ),
            ] = None,
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
            enabled: Annotated[
                bool | None,
                Field(description="Turn the schedule on or off, for the whole NAS."),
            ] = None,
            emule_enabled: Annotated[
                bool | None,
                Field(description="Turn the eMule schedule on or off, for the whole NAS."),
            ] = None,
            schedule_plan: Annotated[
                str | None,
                Field(
                    description=(
                        "168 characters, one per hour from Sunday 00:00 to Saturday 23:00 in "
                        "NAS local time: `0` no download, `1` full speed, `2` limited speed."
                    )
                ),
            ] = None,
            download_rate: Annotated[
                int | None,
                Field(
                    description=(
                        "Download speed in KB/s for hours marked `2`; `0` means unlimited."
                    )
                ),
            ] = None,
            upload_rate: Annotated[
                int | None,
                Field(
                    description="Upload speed in KB/s for hours marked `2`; `0` means unlimited."
                ),
            ] = None,
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
