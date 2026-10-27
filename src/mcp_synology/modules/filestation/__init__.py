"""File Station module: MODULE_INFO, register(), FileStationSettings."""

# No `from __future__ import annotations` here: a few parameter descriptions name this
# config's allowed local folders, known only inside register(), and the SDK reads the
# Annotated[..., Field(description=...)] metadata, so it must be built when each
# handler is defined (rc6 live test; see core.local_paths.roots_for_description).

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
                "List all shared folders on the NAS: the top-level folders every file path starts"
                " with (`/video`). Call this first to discover paths. Shows each share's volume, "
                "that volume's free space (shared by every share on it) and whether its recycle "
                "bin is on; for how much space a share uses, call get_dir_size."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="list_files",
            description=(
                "List the files and folders directly inside one folder (one level; subfolders' "
                "contents aren't listed). To find files anywhere below a folder by name, "
                "extension, size or date, use search_files. Folders show no size; for a folder's "
                "total, use get_dir_size. DSM sorts folders and files separately: ascending lists"
                " folders first, descending files first. A long listing is paged, and the reply "
                "says which offset is next."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="list_recycle_bin",
            description=(
                "List what's in one shared folder's recycle bin: the files and folders deleted "
                "from that share. list_shares shows which shares have a bin; for files that "
                "weren't deleted, use list_files. Without pattern, only the bin's top level is "
                "listed, and DSM sorts folders and files separately, as in list_files. With "
                "pattern, the bin is searched, subfolders included (up to 200 folders and 5,000 "
                "items; the reply says when it stops early), and each match shows its path in "
                "the bin. "
                "Modified is the file's own time, not when it was deleted."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="search_files",
            description=(
                "Search a folder and everything below it for files and folders by name, "
                "extension, size or modified date. For what's in one known folder, use "
                "list_files; for how much space a folder uses, use get_dir_size, not a search. "
                "With no filter, it returns everything under the folder, up to `limit`. A search "
                "that runs out of time returns what it found so far and says so."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_file_info",
            description=(
                "Get detailed metadata for files or folders whose paths you know: size, owner, "
                "timestamps, permissions and real path on the volume. To see what's in a folder, "
                "use list_files; for a folder's total size, use get_dir_size. A path that doesn't"
                " exist is reported as not found."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_dir_size",
            description=(
                "Calculate the total size of a folder, including everything below it: total size,"
                " file count and folder count. This is the tool for 'how much space does X use' "
                "questions. Counts only what this DSM user can read: a subfolder it can't read is"
                " left out without notice. A very large tree can take minutes, and the call gives"
                " up after a timeout (120 s unless the server is configured otherwise)."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="download_file",
            description=(
                "Download a NAS file to a folder on this machine (where this server runs). A "
                "folder downloads as a ZIP of it, saved as `<folder>.zip`; a whole shared folder "
                "can't be downloaded. An existing local file is not replaced unless `overwrite` "
                "is true."
            ),
            permission_tier=PermissionTier.READ,
            annotations=_ANNO_LOCAL_WRITE,
        ),
        # WRITE tools (8)
        ToolInfo(
            name="create_folder",
            description=(
                "Create one or more folders, each given as a full path starting with its shared "
                "folder (`/video/TV Shows/New Show`). By default (`force_parent=true`), missing "
                "folders above it are created too, and a folder that already exists is not an "
                "error."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_IDEMPOTENT,
        ),
        ToolInfo(
            name="rename",
            description=(
                "Rename a file or folder in place. If the new name is already taken in that "
                "folder, DSM refuses and nothing is replaced. To move something to another "
                "folder, use move_files."
            ),
            permission_tier=PermissionTier.WRITE,
        ),
        ToolInfo(
            name="copy_files",
            description=(
                "Copy files or folders into a destination folder; a folder is copied with "
                "everything in it, and the sources stay in place. To move instead, use "
                "move_files. `dest_folder` must already exist; it isn't created. With "
                "`overwrite=false` (default), items already at the destination are skipped and "
                "listed, and the rest are copied. If any source is missing, nothing is copied. A "
                "long copy keeps running on the NAS and returns a handle for file_task."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="move_files",
            description=(
                "Move files or folders into a destination folder; a folder moves with everything "
                "in it, and the sources are removed. To keep the sources, use copy_files. "
                "`dest_folder` must already exist; it isn't created. With `overwrite=false` "
                "(default), items already at the destination are skipped, listed and left at the "
                "source. If any source is missing, nothing is moved. A long move keeps running on"
                " the NAS and returns a handle for file_task."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="delete_files",
            description=(
                "Delete files or folders. A folder is deleted with everything in it (`recursive`,"
                " default true). On a share with a recycle bin, deleted items go to the bin: "
                "list_recycle_bin shows them and restore_from_recycle_bin brings them back. "
                "Without a bin, and for anything inside a bin (`/<share>/#recycle`), deletion is "
                "permanent. Paths are exact, not wildcard patterns, and if any path is missing "
                "nothing is deleted. A long delete keeps running on the NAS and returns a handle "
                "for file_task."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="upload_file",
            description=(
                "Upload one file from this machine (where this server runs) to a NAS folder; a "
                "local folder can't be uploaded. An existing NAS file of the same name is not "
                "replaced unless `overwrite` is true."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="restore_from_recycle_bin",
            description=(
                "Restore deleted files or folders from one shared folder's recycle bin. Check "
                "what's there first with list_recycle_bin: every path must be in that share's "
                "bin, or nothing is restored. Without `dest_folder`, each item goes back to its "
                "original folder, which is re-created if it's gone. An item already at the "
                "destination is skipped and stays in the bin, unless `overwrite=true`, which "
                "replaces the existing one. A long restore keeps running on the NAS and returns a"
                " handle for file_task."
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
                "NAS user. Don't poll in a loop: check when the user asks, or before doing "
                "something that depends on the result."
            ),
            permission_tier=PermissionTier.WRITE,
            # Worst case (#183): action="stop" can leave a move or delete half done.
            annotations=_ANNO_DESTRUCTIVE,
        ),
    ],
    settings_schema=FileStationSettings,
)


def register(ctx: "RegisterContext") -> None:
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
    local_folders = local_paths.roots_for_description(allowed_roots)
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

    async def _fs_client() -> "DsmClient":
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
            sort_by: Annotated[
                str,
                Field(
                    description=(
                        "`name` (default), `user` (the owner), `mtime` (modified) or `crtime` "
                        "(created)."
                    )
                ),
            ] = "name",
            sort_direction: Annotated[str, Field(description="`asc` (default) or `desc`.")] = "asc",
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
            path: Annotated[
                str,
                Field(description="The folder to list (`/video/TV Shows`)."),
            ],
            pattern: Annotated[
                str | None,
                Field(
                    description=(
                        "Name filter, case-insensitive: a bare word matches anywhere in the name; "
                        "with `*` or `?` the whole name must match (`*.mkv`); commas separate "
                        "alternatives (`*.mkv,*.mp4`)."
                    )
                ),
            ] = None,
            filetype: Annotated[
                str, Field(description="`all` (default), `file` or `dir`.")
            ] = "all",
            sort_by: Annotated[
                str,
                Field(
                    description=(
                        "`name` (default), `size`, `mtime` (modified), `crtime` (created), `atime` "
                        "(accessed), `ctime` (changed), `user` (the owner) or `type`."
                    )
                ),
            ] = "name",
            sort_direction: Annotated[str, Field(description="`asc` (default) or `desc`.")] = "asc",
            offset: Annotated[
                int,
                Field(description="Items to skip; a reply with more says which offset is next."),
            ] = 0,
            limit: Annotated[
                int, Field(description="Most items to return; `0` returns them all.")
            ] = 200,
        ) -> str:
            client = await _fs_client()
            [path] = await ensure_known_shares(client, [path], known_shares)
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
            share: Annotated[
                str,
                Field(
                    description=(
                        "The shared folder whose bin to list, as a name (`video`) or path "
                        "(`/video`)."
                    )
                ),
            ],
            pattern: Annotated[
                str | None,
                Field(
                    description=(
                        "Name filter, as in list_files (case-insensitive; `*` and `?`; a bare "
                        "word matches anywhere), applied across the bin, subfolders included "
                        "(up to 200 folders and 5,000 items; the reply says when it stops early)."
                    )
                ),
            ] = None,
            sort_by: Annotated[
                str,
                Field(
                    description=(
                        "`mtime` (default: the file's own modified time), `ctime` (closest to "
                        "when it was deleted), `name`, `size`, `crtime` or `atime`."
                    )
                ),
            ] = "mtime",
            sort_direction: Annotated[
                str, Field(description="`desc` (default) or `asc`.")
            ] = "desc",
            limit: Annotated[
                int, Field(description="Most items to return; `0` returns them all.")
            ] = 100,
            offset: Annotated[
                int,
                Field(description="Items to skip; a reply with more says which offset is next."),
            ] = 0,
        ) -> str:
            client = await _fs_client()
            [share] = await ensure_known_shares(client, [share], known_shares)
            return _reply(
                await list_recycle_bin(
                    client,
                    share=share,
                    pattern=pattern,
                    sort_by=sort_by,
                    sort_direction=sort_direction,
                    limit=limit,
                    offset=offset,
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
            folder_path: Annotated[
                str,
                Field(description="The folder to search (`/video`)."),
            ],
            pattern: Annotated[
                str | None,
                Field(
                    description=(
                        "Name to look for: a bare word matches anywhere in the name (`Severance` "
                        "finds `Severance.S02E10.mkv`); with `*` or `?` it's a wildcard pattern. "
                        '`*.mkv` is treated as `extension="mkv"`.'
                    )
                ),
            ] = None,
            extension: Annotated[
                str | None, Field(description="File extension, without the dot (`mkv`).")
            ] = None,
            filetype: Annotated[
                str, Field(description="`all` (default: files and folders), `file` or `dir`.")
            ] = "all",
            size_from: Annotated[
                str | None,
                Field(
                    description=(
                        "Smallest size to match, in bytes or with a unit (`500MiB`, `2GiB`)."
                    )
                ),
            ] = None,
            size_to: Annotated[
                str | None,
                Field(description="Largest size to match, in the same format as `size_from`."),
            ] = None,
            mtime_from: Annotated[
                str | None,
                Field(
                    description=(
                        "Modified at or after this time: ISO 8601 (`2026-04-01T12:00:00+00:00`), a "
                        "date (`2026-04-01`) or Unix epoch seconds; a time with no zone is UTC."
                    )
                ),
            ] = None,
            mtime_to: Annotated[
                str | None,
                Field(
                    description=(
                        "Modified before this time (a file modified at exactly this time doesn't "
                        "match), in the same formats as `mtime_from`."
                    )
                ),
            ] = None,
            exclude_pattern: Annotated[
                str | None,
                Field(
                    description=(
                        "Wildcard pattern for names to leave out (`*.torrent`), case-insensitive. "
                        "Applied to the results DSM returns, so a reply can hold fewer than "
                        "`limit`."
                    )
                ),
            ] = None,
            recursive: Annotated[
                bool,
                Field(
                    description=(
                        "Search subfolders too (default true); `false` searches only `folder_path` "
                        "itself."
                    )
                ),
            ] = True,
            limit: Annotated[
                int,
                Field(description="Most results to return; the reply says when there are more."),
            ] = 500,
        ) -> str:
            client = await _fs_client()
            [folder_path] = await ensure_known_shares(client, [folder_path], known_shares)
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
        async def tool_get_file_info(
            paths: Annotated[
                list[str],
                Field(description="One or more full paths (`/video/Movies/Dune.mkv`)."),
            ],
            additional: Annotated[
                list[str] | None,
                Field(
                    description=(
                        "Fields to fetch: `real_path`, `size`, `owner`, `time`, `perm`, `type` "
                        "(DSM's extension type) and `mount_point_type` (shown only for a mount "
                        "point). Default: the first five. Unknown values are rejected."
                    )
                ),
            ] = None,
        ) -> str:
            client = await _fs_client()
            paths = await ensure_known_shares(client, paths, known_shares)
            return _reply(await get_file_info(client, paths=paths, additional=additional))

    if "get_dir_size" in ctx.allowed_tools:

        @server.tool(
            name="get_dir_size",
            description=_desc("get_dir_size"),
            annotations=_tool_annos["get_dir_size"],
        )
        async def tool_get_dir_size(
            path: Annotated[
                str,
                Field(description="The folder to measure (`/video/TV Shows`)."),
            ],
        ) -> str:
            client = await _fs_client()
            [path] = await ensure_known_shares(client, [path], known_shares)
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
            path: Annotated[
                str,
                Field(description="The NAS file or folder (`/video/Movies/Dune.mkv`)."),
            ],
            dest_folder: Annotated[
                str | None,
                Field(
                    description=(
                        f"Local folder to save into: {local_folders}, or a folder below it. "
                        "Optional when a default download folder is configured."
                    )
                ),
            ] = None,
            filename: Annotated[
                str | None,
                Field(
                    description=(
                        "Name to save as, a plain file name with no folders (default: the NAS "
                        "name, or `<folder>.zip` for a folder)."
                    )
                ),
            ] = None,
            overwrite: Annotated[
                bool,
                Field(
                    description=(
                        "Replace a local file of the same name (default false: the call fails and "
                        "the local file is kept)."
                    )
                ),
            ] = False,
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
            [path] = await ensure_known_shares(client, [path], known_shares)

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
            paths: Annotated[
                list[str],
                Field(
                    description=(
                        'Full paths of the folders to create (`["/video/TV Shows/New Show"]`).'
                    )
                ),
            ],
            force_parent: Annotated[
                bool,
                Field(
                    description=(
                        "Create missing parent folders too (default true); with `false`, each "
                        "parent must already exist."
                    )
                ),
            ] = True,
        ) -> str:
            client = await _fs_client()
            paths = await ensure_known_shares(client, paths, known_shares)
            result = await create_folder(client, paths=paths, force_parent=force_parent)
            return _reply(result)

    if "rename" in ctx.allowed_tools:

        @server.tool(
            name="rename",
            description=_desc("rename"),
            annotations=_tool_annos["rename"],
        )
        async def tool_rename(
            path: Annotated[
                str,
                Field(description="The file or folder to rename (`/video/TV Shows/Severence`)."),
            ],
            new_name: Annotated[
                str, Field(description="The new name only, not a path (`Severance`).")
            ],
        ) -> str:
            client = await _fs_client()
            [path] = await ensure_known_shares(client, [path], known_shares)
            return _reply(await rename(client, path=path, new_name=new_name))

    if "copy_files" in ctx.allowed_tools:

        @server.tool(
            name="copy_files",
            description=_desc("copy_files"),
            annotations=_tool_annos["copy_files"],
        )
        async def tool_copy_files(
            ctx: Context,
            paths: Annotated[
                list[str],
                Field(description="Files or folders to copy (`/video/Downloads/clip.mkv`)."),
            ],
            dest_folder: Annotated[
                str, Field(description="The existing folder to copy into (`/video/Archive`).")
            ],
            overwrite: Annotated[
                bool,
                Field(
                    description=(
                        "Replace items already at the destination (default false: they're skipped "
                        "and listed)."
                    )
                ),
            ] = False,
        ) -> str:
            client = await _fs_client()
            *paths, dest_folder = await ensure_known_shares(
                client, [*paths, dest_folder], known_shares
            )
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
            paths: Annotated[
                list[str],
                Field(description="Files or folders to move (`/video/Downloads/clip.mkv`)."),
            ],
            dest_folder: Annotated[
                str, Field(description="The existing folder to move into (`/video/Archive`).")
            ],
            overwrite: Annotated[
                bool,
                Field(
                    description=(
                        "Replace items already at the destination (default false: they're skipped "
                        "and listed)."
                    )
                ),
            ] = False,
        ) -> str:
            client = await _fs_client()
            *paths, dest_folder = await ensure_known_shares(
                client, [*paths, dest_folder], known_shares
            )
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
            paths: Annotated[
                list[str],
                Field(
                    description=(
                        "Exact paths of the files or folders to delete "
                        "(`/video/Downloads/old.mkv`); wildcards aren't expanded."
                    )
                ),
            ],
            recursive: Annotated[
                bool,
                Field(
                    description=(
                        "Delete a folder with everything in it (default true); with `false`, a "
                        "folder that isn't empty is refused and kept."
                    )
                ),
            ] = True,
        ) -> str:
            client = await _fs_client()
            paths = await ensure_known_shares(client, paths, known_shares)
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
            local_path: Annotated[
                str,
                Field(
                    description=(f"The file on this machine, inside {local_folders}, or below it.")
                ),
            ],
            dest_folder: Annotated[
                str | None,
                Field(
                    description=(
                        "NAS folder to upload into (`/video/Uploads`). Optional when a default "
                        "upload folder is configured."
                    )
                ),
            ] = None,
            filename: Annotated[
                str | None,
                Field(description="Name for the file on the NAS (default: the local file's name)."),
            ] = None,
            overwrite: Annotated[
                bool,
                Field(
                    description=(
                        "Replace a NAS file of the same name (default false: the call fails and "
                        "the NAS file is kept)."
                    )
                ),
            ] = False,
            create_parents: Annotated[
                bool,
                Field(
                    description=(
                        "Create `dest_folder` and any missing folders above it (default true); "
                        "with `false`, `dest_folder` must already exist."
                    )
                ),
            ] = True,
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
            [effective_dest] = await ensure_known_shares(client, [effective_dest], known_shares)

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
            share: Annotated[
                str,
                Field(
                    description=(
                        "The shared folder whose bin holds the items, as a name (`video`) or path "
                        "(`/video`)."
                    )
                ),
            ],
            paths: Annotated[
                list[str],
                Field(
                    description=(
                        "Items to restore, as paths in the bin the way list_recycle_bin shows them "
                        "(`Shows/Ep1.mkv`); full paths such as `/video/#recycle/Shows/Ep1.mkv` "
                        "work too."
                    )
                ),
            ],
            dest_folder: Annotated[
                str | None,
                Field(
                    description=(
                        "An existing folder to restore everything into, flat (default: each "
                        "item's original folder)."
                    )
                ),
            ] = None,
            overwrite: Annotated[
                bool,
                Field(
                    description=(
                        "Replace an item already at the destination (default false: it's skipped "
                        "and stays in the bin)."
                    )
                ),
            ] = False,
        ) -> str:
            client = await _fs_client()
            share, *dest = await ensure_known_shares(
                client, [share, *([dest_folder] if dest_folder else [])], known_shares
            )
            dest_folder = dest[0] if dest else None
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
