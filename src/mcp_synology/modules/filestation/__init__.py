"""File Station module: MODULE_INFO, register(), FileStationSettings."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mcp.server.fastmcp import (
    Context,  # noqa: TC002 — runtime import needed for FastMCP context injection
)
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from mcp_synology.modules import (
    ApiRequirement,
    ModuleInfo,
    PermissionTier,
    ToolInfo,
    default_annotations,
)

if TYPE_CHECKING:
    from mcp_synology.modules import RegisterContext

# Annotations for tools that need explicit overrides
_ANNO_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
)
_ANNO_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


class FileStationSettings(BaseModel):
    """File Station module settings."""

    hide_recycle_in_listings: bool = False
    file_type_indicator: Literal["emoji", "text"] = "emoji"
    async_timeout: int = Field(default=120, ge=10, le=3600)
    search_timeout: int | None = Field(default=None, ge=10, le=3600)
    copy_move_timeout: int | None = Field(default=None, ge=10, le=3600)
    delete_timeout: int | None = Field(default=None, ge=10, le=3600)
    dir_size_timeout: int | None = Field(default=None, ge=10, le=3600)
    upload_timeout: int | None = Field(default=None, ge=10, le=3600)
    download_timeout: int | None = Field(default=None, ge=10, le=3600)
    default_download_dir: str | None = None
    default_upload_dir: str | None = None
    search_poll_interval: float = Field(default=1.0, ge=0.5, le=10.0)


MODULE_INFO = ModuleInfo(
    name="filestation",
    description="Manage files and folders on the Synology NAS via File Station",
    required_apis=[
        ApiRequirement(api_name="SYNO.FileStation.Info", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.List", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.Search", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.DirSize", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.CreateFolder", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.Rename", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.CopyMove", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.Delete", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.Upload", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.FileStation.Download", min_version=1, optional=True),
    ],
    tools=[
        # READ tools (7)
        ToolInfo(
            name="list_shares",
            description=(
                "List all shared folders on the NAS. This is the starting point for "
                "file navigation — call this first to discover available paths."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="list_files",
            description=(
                "List files and folders in a directory. Supports glob pattern filtering, "
                "file type filtering, sorting, and pagination."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="list_recycle_bin",
            description=(
                "List the contents of a shared folder's recycle bin. Shows recently "
                "deleted files that can be restored."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="search_files",
            description=(
                "Search recursively for files by keyword, extension, size, or "
                "modification date. The pattern parameter is a keyword/substring "
                "match on filenames (not glob). "
                'Use extension for file type filtering (e.g., extension="mkv"). '
                'Accepts human-readable sizes like "500MB". '
                'mtime_from / mtime_to accept ISO 8601 ("2026-04-01T12:00:00+00:00"), '
                'a calendar date ("YYYY-MM-DD"), or Unix epoch seconds; naive dates '
                "are treated as UTC. "
                "For directory contents or sizes, use list_files or get_dir_size instead."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_file_info",
            description=(
                "Get detailed metadata for specific files or folders: size, owner, "
                "timestamps, permissions, and real path."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_dir_size",
            description=(
                "Calculate the total size of a directory, including all files and "
                "subdirectories. Returns total size, file count, and directory count. "
                "This is the best tool for answering 'how much space does X use' questions."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="download_file",
            description=(
                "Download a NAS file to a local directory on this machine. "
                "Provide the NAS file path. dest_folder is optional if "
                "default_download_dir is configured. "
                "Does not overwrite existing local files by default."
            ),
            permission_tier=PermissionTier.READ,
        ),
        # WRITE tools (7)
        ToolInfo(
            name="create_folder",
            description=(
                "Create one or more new folders. Creates parent directories automatically "
                "by default. Idempotent — creating an existing folder succeeds."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_IDEMPOTENT,
        ),
        ToolInfo(
            name="rename",
            description=(
                "Rename a file or folder. Provide the full current path and the new name "
                "(just the name, not a full path)."
            ),
            permission_tier=PermissionTier.WRITE,
        ),
        ToolInfo(
            name="copy_files",
            description=(
                "Copy files or folders to a destination folder. Source files remain in "
                "place. Set overwrite=true to replace existing files."
            ),
            permission_tier=PermissionTier.WRITE,
        ),
        ToolInfo(
            name="move_files",
            description=(
                "Move files or folders to a new location. Source files are REMOVED after "
                "transfer. Set overwrite=true to replace existing files."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="delete_files",
            description=(
                "Delete files or folders. If the share has a recycle bin, files can be "
                "recovered. Otherwise deletion is permanent."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="upload_file",
            description=(
                "Upload a local file from this machine to a NAS folder. "
                "Provide the local file path. dest_folder is optional if "
                "default_upload_dir is configured. "
                "Does not overwrite existing NAS files by default."
            ),
            permission_tier=PermissionTier.WRITE,
        ),
        ToolInfo(
            name="restore_from_recycle_bin",
            description=(
                "Restore deleted files from a shared folder's recycle bin to their "
                "original location or a specified destination."
            ),
            permission_tier=PermissionTier.WRITE,
        ),
    ],
    settings_schema=FileStationSettings,
)


def register(ctx: RegisterContext) -> None:
    """Register File Station tools with the MCP server."""
    from mcp_synology.modules.filestation.listing import (
        list_files,
        list_recycle_bin,
        list_shares,
    )
    from mcp_synology.modules.filestation.metadata import get_dir_size, get_file_info
    from mcp_synology.modules.filestation.operations import (
        copy_files,
        create_folder,
        delete_files,
        move_files,
        rename,
        restore_from_recycle_bin,
    )
    from mcp_synology.modules.filestation.search import search_files
    from mcp_synology.modules.filestation.transfer import download_file, upload_file

    settings = FileStationSettings(**ctx.settings_dict)
    indicator = settings.file_type_indicator
    search_timeout = float(settings.search_timeout or settings.async_timeout)
    copy_move_timeout = float(settings.copy_move_timeout or settings.async_timeout)
    delete_timeout = float(settings.delete_timeout or settings.async_timeout)
    dir_size_timeout = float(settings.dir_size_timeout or settings.async_timeout)
    upload_timeout = float(settings.upload_timeout or 300)
    download_timeout = float(settings.download_timeout or 300)
    # Expand ~ in local paths; NAS paths pass through unchanged
    default_download_dir = (
        str(Path(settings.default_download_dir).expanduser())
        if settings.default_download_dir
        else None
    )
    default_upload_dir = settings.default_upload_dir  # NAS path, no expansion
    search_poll_interval = settings.search_poll_interval
    hide_recycle = settings.hide_recycle_in_listings

    # Closure-shared cache of per-share recycle-bin enabled-ness. Populated
    # lazily by `ensure_recycle_status` on first observation per share, and
    # cleared on session re-auth so admin-side toggles get picked up next
    # time. See helpers.py for the probe and self-correct logic. Closes #37.
    recycle_status: dict[str, bool] = {}
    hostname = ctx.display_name
    server = ctx.server
    manager = ctx.manager

    # Subscribe a cache-invalidator that fires after every successful
    # AuthManager re-auth. Safe to call before the AuthManager exists —
    # SharedClientManager queues the callback and flushes on first
    # `get_client`.
    manager.subscribe_on_reauth(recycle_status.clear)

    # Build a lookup for tool annotations
    _tool_annos: dict[str, ToolAnnotations] = {}
    for t in MODULE_INFO.tools:
        _tool_annos[t.name] = t.annotations or default_annotations(t.permission_tier)

    def _desc(name: str) -> str:
        return next(t.description for t in MODULE_INFO.tools if t.name == name)

    if "list_shares" in ctx.allowed_tools:

        @server.tool(
            name="list_shares",
            description=_desc("list_shares"),
            annotations=_tool_annos["list_shares"],
        )
        async def tool_list_shares(
            sort_by: str = "name",
            sort_direction: str = "asc",
            additional: list[str] | None = None,
        ) -> str:
            """`additional`: optional list of DSM metadata fields. Valid values:
            real_path, size, owner, time, perm, mount_point_type, volume_status.
            Defaults to ["real_path", "size", "owner", "perm"]. Unknown values
            are rejected before reaching DSM.
            """
            client = await manager.get_client()
            return manager.with_update_notice(
                await list_shares(
                    client,
                    additional=additional,
                    sort_by=sort_by,
                    sort_direction=sort_direction,
                    recycle_bin_status=recycle_status,
                    hostname=hostname,
                    file_type_indicator=indicator,
                )
            )

    if "list_files" in ctx.allowed_tools:

        @server.tool(
            name="list_files",
            description=_desc("list_files"),
            annotations=_tool_annos["list_files"],
        )
        async def tool_list_files(
            path: str,
            pattern: str | None = None,
            filetype: str = "all",
            sort_by: str = "name",
            sort_direction: str = "asc",
            offset: int = 0,
            limit: int = 200,
            additional: list[str] | None = None,
        ) -> str:
            """`additional`: optional list of DSM metadata fields. Valid values:
            real_path, size, owner, time, perm, type, mount_point_type. Defaults
            to ["size", "time"]. Unknown values are rejected before reaching DSM.
            """
            client = await manager.get_client()
            return manager.with_update_notice(
                await list_files(
                    client,
                    path=path,
                    pattern=pattern,
                    filetype=filetype,
                    sort_by=sort_by,
                    sort_direction=sort_direction,
                    offset=offset,
                    limit=limit,
                    additional=additional,
                    hide_recycle=hide_recycle,
                    file_type_indicator=indicator,
                )
            )

    if "list_recycle_bin" in ctx.allowed_tools:

        @server.tool(
            name="list_recycle_bin",
            description=_desc("list_recycle_bin"),
            annotations=_tool_annos["list_recycle_bin"],
        )
        async def tool_list_recycle_bin(
            share: str,
            pattern: str | None = None,
            sort_by: str = "mtime",
            sort_direction: str = "desc",
            limit: int = 100,
        ) -> str:
            client = await manager.get_client()
            return manager.with_update_notice(
                await list_recycle_bin(
                    client,
                    share=share,
                    pattern=pattern,
                    sort_by=sort_by,
                    sort_direction=sort_direction,
                    limit=limit,
                    file_type_indicator=indicator,
                    recycle_bin_status=recycle_status,
                )
            )

    if "search_files" in ctx.allowed_tools:

        @server.tool(
            name="search_files",
            description=_desc("search_files"),
            annotations=_tool_annos["search_files"],
        )
        async def tool_search_files(
            folder_path: str,
            pattern: str | None = None,
            extension: str | None = None,
            filetype: str = "all",
            size_from: str | None = None,
            size_to: str | None = None,
            mtime_from: str | None = None,
            mtime_to: str | None = None,
            exclude_pattern: str | None = None,
            recursive: bool = True,
            limit: int = 500,
            additional: list[str] | None = None,
        ) -> str:
            """`additional`: optional list of DSM metadata fields. Valid values:
            real_path, size, owner, time, perm, type. Defaults to ["size", "time"].
            Unknown values are rejected before reaching DSM.
            """
            client = await manager.get_client()
            return manager.with_update_notice(
                await search_files(
                    client,
                    folder_path=folder_path,
                    pattern=pattern,
                    extension=extension,
                    filetype=filetype,
                    size_from=size_from,
                    size_to=size_to,
                    mtime_from=mtime_from,
                    mtime_to=mtime_to,
                    exclude_pattern=exclude_pattern,
                    recursive=recursive,
                    limit=limit,
                    additional=additional,
                    file_type_indicator=indicator,
                    timeout=search_timeout,
                    poll_interval=search_poll_interval,
                )
            )

    if "get_file_info" in ctx.allowed_tools:

        @server.tool(
            name="get_file_info",
            description=_desc("get_file_info"),
            annotations=_tool_annos["get_file_info"],
        )
        async def tool_get_file_info(paths: list[str]) -> str:
            client = await manager.get_client()
            return manager.with_update_notice(await get_file_info(client, paths=paths))

    if "get_dir_size" in ctx.allowed_tools:

        @server.tool(
            name="get_dir_size",
            description=_desc("get_dir_size"),
            annotations=_tool_annos["get_dir_size"],
        )
        async def tool_get_dir_size(path: str) -> str:
            client = await manager.get_client()
            result = await get_dir_size(client, path=path, timeout=dir_size_timeout)
            return manager.with_update_notice(result)

    if "download_file" in ctx.allowed_tools:

        @server.tool(
            name="download_file",
            description=_desc("download_file"),
            annotations=_tool_annos["download_file"],
        )
        async def tool_download_file(
            ctx: Context,  # type: ignore[type-arg]
            path: str,
            dest_folder: str | None = None,
            filename: str | None = None,
            overwrite: bool = False,
        ) -> str:
            effective_dest = dest_folder or default_download_dir
            if not effective_dest:
                return (
                    "[!] No destination folder specified and no default_download_dir "
                    "configured.\n    Provide dest_folder or set default_download_dir "
                    "in filestation module settings."
                )

            client = await manager.get_client()

            async def _progress(current: int, total: int | None) -> None:
                await ctx.report_progress(float(current), float(total) if total else None)

            return manager.with_update_notice(
                await download_file(
                    client,
                    path=path,
                    dest_folder=effective_dest,
                    filename=filename,
                    overwrite=overwrite,
                    timeout=download_timeout,
                    progress_callback=_progress,
                )
            )

    # WRITE tools
    if "create_folder" in ctx.allowed_tools:

        @server.tool(
            name="create_folder",
            description=_desc("create_folder"),
            annotations=_tool_annos["create_folder"],
        )
        async def tool_create_folder(
            paths: list[str],
            force_parent: bool = True,
        ) -> str:
            client = await manager.get_client()
            result = await create_folder(client, paths=paths, force_parent=force_parent)
            return manager.with_update_notice(result)

    if "rename" in ctx.allowed_tools:

        @server.tool(
            name="rename",
            description=_desc("rename"),
            annotations=_tool_annos["rename"],
        )
        async def tool_rename(path: str, new_name: str) -> str:
            client = await manager.get_client()
            return manager.with_update_notice(await rename(client, path=path, new_name=new_name))

    if "copy_files" in ctx.allowed_tools:

        @server.tool(
            name="copy_files",
            description=_desc("copy_files"),
            annotations=_tool_annos["copy_files"],
        )
        async def tool_copy_files(
            paths: list[str],
            dest_folder: str,
            overwrite: bool = False,
        ) -> str:
            client = await manager.get_client()
            return manager.with_update_notice(
                await copy_files(
                    client,
                    paths=paths,
                    dest_folder=dest_folder,
                    overwrite=overwrite,
                    timeout=copy_move_timeout,
                )
            )

    if "move_files" in ctx.allowed_tools:

        @server.tool(
            name="move_files",
            description=_desc("move_files"),
            annotations=_tool_annos["move_files"],
        )
        async def tool_move_files(
            paths: list[str],
            dest_folder: str,
            overwrite: bool = False,
        ) -> str:
            client = await manager.get_client()
            return manager.with_update_notice(
                await move_files(
                    client,
                    paths=paths,
                    dest_folder=dest_folder,
                    overwrite=overwrite,
                    timeout=copy_move_timeout,
                )
            )

    if "delete_files" in ctx.allowed_tools:

        @server.tool(
            name="delete_files",
            description=_desc("delete_files"),
            annotations=_tool_annos["delete_files"],
        )
        async def tool_delete_files(
            paths: list[str],
            recursive: bool = True,
        ) -> str:
            client = await manager.get_client()
            return manager.with_update_notice(
                await delete_files(
                    client,
                    paths=paths,
                    recursive=recursive,
                    recycle_bin_status=recycle_status,
                    timeout=delete_timeout,
                )
            )

    if "upload_file" in ctx.allowed_tools:

        @server.tool(
            name="upload_file",
            description=_desc("upload_file"),
            annotations=_tool_annos["upload_file"],
        )
        async def tool_upload_file(
            ctx: Context,  # type: ignore[type-arg]
            local_path: str,
            dest_folder: str | None = None,
            filename: str | None = None,
            overwrite: bool = False,
            create_parents: bool = True,
        ) -> str:
            effective_dest = dest_folder or default_upload_dir
            if not effective_dest:
                return (
                    "[!] No NAS destination folder specified and no default_upload_dir "
                    "configured.\n    Provide dest_folder or set default_upload_dir "
                    "in filestation module settings."
                )

            client = await manager.get_client()

            async def _progress(current: int, total: int | None) -> None:
                await ctx.report_progress(float(current), float(total) if total else None)

            return manager.with_update_notice(
                await upload_file(
                    client,
                    local_path=local_path,
                    dest_folder=effective_dest,
                    filename=filename,
                    overwrite=overwrite,
                    create_parents=create_parents,
                    timeout=upload_timeout,
                    progress_callback=_progress,
                )
            )

    if "restore_from_recycle_bin" in ctx.allowed_tools:

        @server.tool(
            name="restore_from_recycle_bin",
            description=_desc("restore_from_recycle_bin"),
            annotations=_tool_annos["restore_from_recycle_bin"],
        )
        async def tool_restore_from_recycle_bin(
            share: str,
            paths: list[str],
            dest_folder: str | None = None,
            overwrite: bool = False,
        ) -> str:
            client = await manager.get_client()
            return manager.with_update_notice(
                await restore_from_recycle_bin(
                    client,
                    share=share,
                    paths=paths,
                    dest_folder=dest_folder,
                    overwrite=overwrite,
                    timeout=delete_timeout,
                )
            )
