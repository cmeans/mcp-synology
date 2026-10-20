"""File Station module: MODULE_INFO, register(), FileStationSettings."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from mcp.server.mcpserver import (
    Context,  # noqa: TC002 — runtime import needed for MCPServer context injection
)
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, field_validator

from mcp_synology.core.local_paths import check_local_path_entries, expand_setting
from mcp_synology.modules import (
    ApiRequirement,
    ModuleInfo,
    PermissionTier,
    ToolInfo,
    default_annotations,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient
    from mcp_synology.modules import RegisterContext

logger = logging.getLogger(__name__)

# Annotations for tools that need explicit overrides
_ANNO_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
# move/delete remove files; copy_files, upload_file and restore_from_recycle_bin replace
# NAS files with overwrite=true. Annotations are static, so they can't depend on
# overwrite, and clients may auto-approve tools that aren't destructive (#183).
_ANNO_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)
# download_file is READ on the NAS but writes to this machine, and with
# overwrite=True it replaces a local file. MCP's readOnlyHint means "does not
# modify its environment", and clients may auto-approve tools that claim it.
_ANNO_LOCAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


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
    # Local folders upload_file/download_file may use (see local_paths.py). None means
    # the user's Downloads folder; default_download_dir is always allowed too.
    allowed_local_paths: list[str] | None = Field(default=None, min_length=1)

    @field_validator("allowed_local_paths")
    @classmethod
    def _absolute_local_paths(cls, value: list[str] | None) -> list[str] | None:
        return check_local_path_entries("allowed_local_paths", value)

    @field_validator("default_download_dir")
    @classmethod
    def _expandable_download_dir(cls, value: str | None) -> str | None:
        if value:
            expand_setting("default_download_dir", value)
        return value

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
                "file navigation — call this first to discover available paths. Shows "
                "each share's volume and that volume's free space (shared by every share "
                "on it); for how much space a share uses, call get_dir_size."
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
                "List the contents of a shared folder's recycle bin: its recently deleted files."
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
                'Accepts human-readable sizes like "500MiB". '
                'mtime_from / mtime_to accept ISO 8601 ("2026-04-01T12:00:00+00:00"), '
                'a calendar date ("YYYY-MM-DD"), or Unix epoch seconds; naive dates '
                "are treated as UTC. mtime_from is inclusive and mtime_to exclusive, to "
                "the second. "
                "For directory contents or sizes, use list_files or get_dir_size instead."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_file_info",
            description=(
                "Get detailed metadata for specific files or folders: size, owner, "
                "timestamps, permissions, and real path. `additional` optionally picks "
                "the DSM fields to fetch: real_path, size, owner, time, perm, type (the "
                "file's extension type) and mount_point_type (shown only for a mount "
                "point). Default: real_path, size, owner, time, perm. Unknown values "
                "are rejected."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_dir_size",
            description=(
                "Calculate the total size of a directory, including all files and "
                "subdirectories. Returns total size, file count, and directory count. "
                "This is the best tool for answering 'how much space does X use' questions. "
                "Counts only what this DSM user can read: a subfolder it can't read is left "
                "out without notice."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="download_file",
            description=(
                "Download a NAS file to a local directory on this machine. "
                "Provide the NAS file path; a folder downloads as a ZIP of it, saved as "
                "<folder>.zip (not a whole shared folder). dest_folder is optional if "
                "default_download_dir is configured. "
                "Does not overwrite existing local files by default."
            ),
            permission_tier=PermissionTier.READ,
            annotations=_ANNO_LOCAL_WRITE,
        ),
        # WRITE tools (8)
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
            annotations=_ANNO_DESTRUCTIVE,
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
                "recovered. Otherwise deletion is permanent, as is deleting inside "
                "/<share>/#recycle."
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
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="restore_from_recycle_bin",
            description=(
                "Restore deleted files from a shared folder's recycle bin to their "
                "original location or a specified destination."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="file_task",
            description=(
                "Follow up on a delete, copy, move or restore that is still running on the "
                "NAS. Those tools hand off long operations and return a handle such as "
                "`delete:FileStation_…`, because the NAS keeps working after the reply. "
                'With a handle: `action="status"` shows progress, or the final result if DSM '
                'still has it; `action="stop"` stops it, and the operation may then be partly '
                "done. Without a handle: lists this user's running operations, which is "
                "useful in a new chat or after a restart. Handles only work for the same "
                "NAS user."
            ),
            permission_tier=PermissionTier.WRITE,
            # Worst case (#183): action="stop" can leave a move or delete half done.
            annotations=_ANNO_DESTRUCTIVE,
        ),
    ],
    settings_schema=FileStationSettings,
)


def register(ctx: RegisterContext) -> None:
    """Register File Station tools with the MCP server."""
    from mcp_synology.core import local_paths
    from mcp_synology.core.errors import ErrorCode
    from mcp_synology.core.formatting import error_response
    from mcp_synology.modules.filestation.helpers import ensure_known_shares
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
    from mcp_synology.modules.filestation.tasks import (
        DiscoveryNote,
        ProgressCallback,
        TaskRecords,
        discover_running_tasks,
        file_task,
        parse_handle,
    )
    from mcp_synology.modules.filestation.transfer import download_file, upload_file

    settings = FileStationSettings(**ctx.settings_dict)
    indicator = settings.file_type_indicator
    search_timeout = float(settings.search_timeout or settings.async_timeout)
    # Delete/copy/move/restore wait this long, then hand the task off to run on (#245).
    # 30 s unless set; capped at 10 minutes, below known client timeouts (a client's
    # timeout arrives as a cancel, which stops the task). async_timeout doesn't apply.
    delete_wait = min(float(settings.delete_timeout or 30), 600.0)
    copy_move_wait = min(float(settings.copy_move_timeout or 30), 600.0)
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
    allowed_roots = local_paths.allowed_local_roots(
        settings.allowed_local_paths,
        default_download_dir,
        shared_paths=ctx.allowed_local_paths,
        module="filestation",
    )
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

    # Shared-folder names for path validation (#160): filled on first use, reloaded once
    # when a path names a share it doesn't hold, and cleared on re-auth.
    known_shares: set[str] = set()
    manager.subscribe_on_reauth(known_shares.clear)

    # Build a lookup for tool annotations
    _tool_annos: dict[str, ToolAnnotations] = {}
    for t in MODULE_INFO.tools:
        _tool_annos[t.name] = t.annotations or default_annotations(t.permission_tier)

    def _desc(name: str) -> str:
        return next(t.description for t in MODULE_INFO.tools if t.name == name)

    # Handed-off tasks this server process knows about (#245), shared by the four
    # operations that hand off and file_task.
    records = TaskRecords()
    # Discovery of tasks still running from before this process (#245): once, at the
    # first successful client; its note goes on the first reply after it. Only where
    # file_task exists, since the note tells the model to use it.
    discovery: dict[str, DiscoveryNote | bool | None] = {
        "pending": "file_task" in ctx.allowed_tools,
        "note": None,
    }

    async def _fs_client() -> DsmClient:
        """The DSM client; the first success also runs running-task discovery."""
        client = await manager.get_client()
        if discovery["pending"]:
            discovery["pending"] = False
            try:
                discovery["note"] = await discover_running_tasks(client, records)
            except Exception as e:
                # Never fail (or retry) the tool call over discovery.
                logger.debug("Running-task discovery failed, ignoring: %s", e)
        return client

    def _reply(result: str, *, skip: str | None = None, listed: bool = False) -> str:
        """The tool result plus the update notice and, once, the discovery note.

        A file_task reply leaves out the task it's about (``skip``), so the note doesn't
        repeat it right below, and a file_task list (``listed``) already shows them all.
        """
        text = manager.with_update_notice(result)
        pending = discovery["note"]
        if isinstance(pending, DiscoveryNote):
            discovery["note"] = None
            note = None if listed else pending.text(skip=skip)
            if note is not None:
                text += f"\n\n---\n{note}"
        return text

    def _progress_cb(mcp_ctx: Context) -> ProgressCallback:
        """Forward a task's progress (with its message) to the client."""
        return lambda p, t, m: mcp_ctx.report_progress(p, t, m)

    if "list_shares" in ctx.allowed_tools:

        @server.tool(
            name="list_shares",
            description=_desc("list_shares"),
            annotations=_tool_annos["list_shares"],
        )
        async def tool_list_shares(
            sort_by: str = "name",
            sort_direction: str = "asc",
        ) -> str:
            client = await _fs_client()
            return _reply(
                await list_shares(
                    client,
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
        ) -> str:
            client = await _fs_client()
            await ensure_known_shares(client, [path], known_shares)
            return _reply(
                await list_files(
                    client,
                    path=path,
                    pattern=pattern,
                    filetype=filetype,
                    sort_by=sort_by,
                    sort_direction=sort_direction,
                    offset=offset,
                    limit=limit,
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
            client = await _fs_client()
            await ensure_known_shares(client, [share], known_shares)
            return _reply(
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
        ) -> str:
            client = await _fs_client()
            await ensure_known_shares(client, [folder_path], known_shares)
            return _reply(
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
        async def tool_get_file_info(paths: list[str], additional: list[str] | None = None) -> str:
            client = await _fs_client()
            await ensure_known_shares(client, paths, known_shares)
            return _reply(await get_file_info(client, paths=paths, additional=additional))

    if "get_dir_size" in ctx.allowed_tools:

        @server.tool(
            name="get_dir_size",
            description=_desc("get_dir_size"),
            annotations=_tool_annos["get_dir_size"],
        )
        async def tool_get_dir_size(path: str) -> str:
            client = await _fs_client()
            await ensure_known_shares(client, [path], known_shares)
            result = await get_dir_size(client, path=path, timeout=dir_size_timeout)
            return _reply(result)

    if "download_file" in ctx.allowed_tools:

        @server.tool(
            name="download_file",
            description=_desc("download_file"),
            annotations=_tool_annos["download_file"],
        )
        async def tool_download_file(
            ctx: Context,
            path: str,
            dest_folder: str | None = None,
            filename: str | None = None,
            overwrite: bool = False,
        ) -> str:
            effective_dest = dest_folder or default_download_dir
            if not effective_dest:
                # A structured error, so a client checking isError doesn't read it as success
                # (#194).
                error_response(
                    ErrorCode.INVALID_PARAMETER,
                    "Download failed: No destination folder specified and no "
                    "default_download_dir configured.",
                    retryable=False,
                    param="dest_folder",
                    suggestion=(
                        "Provide dest_folder (a local folder), or set default_download_dir "
                        "in filestation module settings."
                    ),
                )

            client = await _fs_client()
            await ensure_known_shares(client, [path], known_shares)

            async def _progress(current: int, total: int | None) -> None:
                await ctx.report_progress(float(current), float(total) if total else None)

            return _reply(
                await download_file(
                    client,
                    path=path,
                    dest_folder=effective_dest,
                    filename=filename,
                    overwrite=overwrite,
                    timeout=download_timeout,
                    progress_callback=_progress,
                    allowed_roots=allowed_roots,
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
            client = await _fs_client()
            await ensure_known_shares(client, paths, known_shares)
            result = await create_folder(client, paths=paths, force_parent=force_parent)
            return _reply(result)

    if "rename" in ctx.allowed_tools:

        @server.tool(
            name="rename",
            description=_desc("rename"),
            annotations=_tool_annos["rename"],
        )
        async def tool_rename(path: str, new_name: str) -> str:
            client = await _fs_client()
            await ensure_known_shares(client, [path], known_shares)
            return _reply(await rename(client, path=path, new_name=new_name))

    if "copy_files" in ctx.allowed_tools:

        @server.tool(
            name="copy_files",
            description=_desc("copy_files"),
            annotations=_tool_annos["copy_files"],
        )
        async def tool_copy_files(
            ctx: Context,
            paths: list[str],
            dest_folder: str,
            overwrite: bool = False,
        ) -> str:
            client = await _fs_client()
            await ensure_known_shares(client, [*paths, dest_folder], known_shares)
            return _reply(
                await copy_files(
                    client,
                    paths=paths,
                    dest_folder=dest_folder,
                    overwrite=overwrite,
                    timeout=copy_move_wait,
                    records=records,
                    on_progress=_progress_cb(ctx),
                )
            )

    if "move_files" in ctx.allowed_tools:

        @server.tool(
            name="move_files",
            description=_desc("move_files"),
            annotations=_tool_annos["move_files"],
        )
        async def tool_move_files(
            ctx: Context,
            paths: list[str],
            dest_folder: str,
            overwrite: bool = False,
        ) -> str:
            client = await _fs_client()
            await ensure_known_shares(client, [*paths, dest_folder], known_shares)
            return _reply(
                await move_files(
                    client,
                    paths=paths,
                    dest_folder=dest_folder,
                    overwrite=overwrite,
                    timeout=copy_move_wait,
                    records=records,
                    on_progress=_progress_cb(ctx),
                )
            )

    if "delete_files" in ctx.allowed_tools:

        @server.tool(
            name="delete_files",
            description=_desc("delete_files"),
            annotations=_tool_annos["delete_files"],
        )
        async def tool_delete_files(
            ctx: Context,
            paths: list[str],
            recursive: bool = True,
        ) -> str:
            client = await _fs_client()
            await ensure_known_shares(client, paths, known_shares)
            return _reply(
                await delete_files(
                    client,
                    paths=paths,
                    recursive=recursive,
                    recycle_bin_status=recycle_status,
                    timeout=delete_wait,
                    records=records,
                    on_progress=_progress_cb(ctx),
                )
            )

    if "upload_file" in ctx.allowed_tools:

        @server.tool(
            name="upload_file",
            description=_desc("upload_file"),
            annotations=_tool_annos["upload_file"],
        )
        async def tool_upload_file(
            ctx: Context,
            local_path: str,
            dest_folder: str | None = None,
            filename: str | None = None,
            overwrite: bool = False,
            create_parents: bool = True,
        ) -> str:
            effective_dest = dest_folder or default_upload_dir
            if not effective_dest:
                # Structured, like every other refusal from this tool (#194).
                error_response(
                    ErrorCode.INVALID_PARAMETER,
                    "Upload failed: No NAS destination folder specified and no "
                    "default_upload_dir configured.",
                    retryable=False,
                    param="dest_folder",
                    suggestion=(
                        "Provide dest_folder (a NAS folder such as /video), or set "
                        "default_upload_dir in filestation module settings."
                    ),
                )

            client = await _fs_client()
            await ensure_known_shares(
                client, [d] if (d := dest_folder or default_upload_dir) else [], known_shares
            )

            async def _progress(current: int, total: int | None) -> None:
                await ctx.report_progress(float(current), float(total) if total else None)

            return _reply(
                await upload_file(
                    client,
                    local_path=local_path,
                    dest_folder=effective_dest,
                    filename=filename,
                    overwrite=overwrite,
                    create_parents=create_parents,
                    timeout=upload_timeout,
                    progress_callback=_progress,
                    allowed_roots=allowed_roots,
                )
            )

    if "restore_from_recycle_bin" in ctx.allowed_tools:

        @server.tool(
            name="restore_from_recycle_bin",
            description=_desc("restore_from_recycle_bin"),
            annotations=_tool_annos["restore_from_recycle_bin"],
        )
        async def tool_restore_from_recycle_bin(
            ctx: Context,
            share: str,
            paths: list[str],
            dest_folder: str | None = None,
            overwrite: bool = False,
        ) -> str:
            client = await _fs_client()
            await ensure_known_shares(
                client, [share, *([dest_folder] if dest_folder else [])], known_shares
            )
            return _reply(
                await restore_from_recycle_bin(
                    client,
                    share=share,
                    paths=paths,
                    dest_folder=dest_folder,
                    overwrite=overwrite,
                    timeout=copy_move_wait,
                    records=records,
                    on_progress=_progress_cb(ctx),
                )
            )

    if "file_task" in ctx.allowed_tools:

        @server.tool(
            name="file_task",
            description=_desc("file_task"),
            annotations=_tool_annos["file_task"],
        )
        async def tool_file_task(
            handle: Annotated[
                str | None,
                Field(
                    description=(
                        "The handle from a delete/copy/move/restore reply. Omit it to list "
                        "running operations."
                    )
                ),
            ] = None,
            action: Annotated[
                Literal["status", "stop"], Field(description="`status` (default) or `stop`.")
            ] = "status",
        ) -> str:
            client = await _fs_client()
            result = await file_task(client, records=records, handle=handle, action=action)
            if handle is None:
                return _reply(result, listed=True)
            # file_task has validated the handle by now.
            return _reply(result, skip=str(parse_handle(handle)))
