"""FastMCP server initialization, module loading, and startup."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from synology_mcp.core.auth import AuthManager
from synology_mcp.core.client import DsmClient
from synology_mcp.core.state import ServerState
from synology_mcp.modules import PermissionTier, filter_tools_by_permission
from synology_mcp.modules.filestation import MODULE_INFO as FS_MODULE_INFO
from synology_mcp.modules.filestation import FileStationSettings
from synology_mcp.modules.filestation.listing import (
    list_files,
    list_recycle_bin,
    list_shares,
)
from synology_mcp.modules.filestation.metadata import get_dir_size, get_file_info
from synology_mcp.modules.filestation.operations import (
    copy_files,
    create_folder,
    delete_files,
    move_files,
    rename,
    restore_from_recycle_bin,
)
from synology_mcp.modules.filestation.search import search_files

if TYPE_CHECKING:
    from synology_mcp.core.config import AppConfig

logger = logging.getLogger(__name__)

MCP_INSTRUCTIONS = """\
You are connected to a Synology NAS via the synology-mcp File Station module.

PATH FORMAT:
All file paths start with a shared folder name: /video/..., /music/..., etc.
Call list_shares first to discover available shared folders and their permissions.

FILE SIZES:
Size parameters accept human-readable values: "500MB", "2GB", "1.5TB".
Supported units: B, KB, MB, GB, TB (binary, 1 KB = 1024 bytes).

WORKING WITH FILES:
- Start with list_shares to discover available paths
- Use list_files to browse directories, search_files to find specific files
- get_file_info for detailed metadata, get_dir_size for directory totals

MOVING AND ORGANIZING FILES:
When a user asks to move or organize files:
1. Use search_files to find matching files. Use exclude_pattern to filter out
   unwanted file types (e.g., exclude_pattern="*.torrent" when moving media).
2. Present the results with a count and confirm with the user before proceeding.
3. Use move_files or copy_files with the confirmed paths.
Always search first and confirm before destructive operations.

RECYCLE BIN:
Some shares have a recycle bin enabled (shown in list_shares output).
Deleted files on those shares can be recovered:
- list_recycle_bin to see recently deleted files
- restore_from_recycle_bin to recover them
The recycle bin lives at /<share>/#recycle/ internally.
"""

# Tool annotations tell Claude Desktop about operation safety
_ANNO_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
_ANNO_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_ANNO_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
_ANNO_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
)

# Known module registry
_MODULE_REGISTRY: dict[str, Any] = {
    "filestation": FS_MODULE_INFO,
}


def create_server(config: AppConfig) -> FastMCP:
    """Create and configure the MCP server.

    This sets up the FastMCP instance and registers tools based on
    module configuration and permission tiers.
    """
    server = FastMCP(
        "synology-mcp",
        instructions=MCP_INSTRUCTIONS,
    )

    logger.debug("Creating MCP server")

    # Determine which modules and tools to register
    for mod_name, mod_config in config.modules.items():
        if not mod_config.enabled:
            logger.debug("Module '%s' is disabled, skipping", mod_name)
            continue

        if mod_name not in _MODULE_REGISTRY:
            logger.warning("Unknown module '%s' — skipping.", mod_name)
            continue

        module_info = _MODULE_REGISTRY[mod_name]
        tier = PermissionTier(mod_config.permission)
        allowed_tools = filter_tools_by_permission(module_info.tools, tier)
        logger.debug(
            "Module '%s': permission=%s, registering %d/%d tools: %s",
            mod_name,
            tier.value,
            len(allowed_tools),
            len(module_info.tools),
            sorted(allowed_tools),
        )

        if mod_name == "filestation":
            _register_filestation(server, config, mod_config.settings, allowed_tools)

    return server


def _register_filestation(
    server: FastMCP,
    config: AppConfig,
    settings_dict: dict[str, Any],
    allowed_tools: set[str],
) -> None:
    """Register File Station tools with the MCP server."""
    settings = FileStationSettings(**settings_dict)
    indicator = settings.file_type_indicator
    search_timeout = float(settings.search_timeout or settings.async_timeout)
    copy_move_timeout = float(settings.copy_move_timeout or settings.async_timeout)
    delete_timeout = float(settings.delete_timeout or settings.async_timeout)
    dir_size_timeout = float(settings.dir_size_timeout or settings.async_timeout)
    search_poll_interval = settings.search_poll_interval
    hide_recycle = settings.hide_recycle_in_listings

    # These will be initialized at runtime when the server starts
    # For now, create closures that reference config for lazy initialization
    _state: dict[str, Any] = {
        "client": None,
        "auth": None,
        "server_state": ServerState(),
        "update_notice": None,  # Set once on first tool call, then cleared
    }

    async def _get_client() -> DsmClient:
        if _state["client"] is None:
            conn = config.connection
            assert conn is not None
            protocol = "https" if conn.https else "http"
            base_url = f"{protocol}://{conn.host}:{conn.port}"
            client = DsmClient(
                base_url=base_url,
                verify_ssl=conn.verify_ssl,
                timeout=conn.timeout,
            )
            _state["_http_ctx"] = client
            await client.__aenter__()
            await client.query_api_info()

            auth = AuthManager(config, client)
            await auth.login()

            _state["client"] = client
            _state["auth"] = auth

            # Check for updates on first connection (non-blocking)
            if config.check_for_updates:
                try:
                    from synology_mcp.cli import (
                        _check_for_update,
                        _load_global_state,
                        _save_global_state,
                    )

                    gstate = _load_global_state()
                    latest = _check_for_update(gstate)
                    _save_global_state(gstate)
                    if latest:
                        from synology_mcp import __version__

                        _state["update_notice"] = (
                            f"\n\n---\nUpdate available: synology-mcp {latest} "
                            f"(current: {__version__}). "
                            f"Run: synology-mcp --check-update"
                        )
                except Exception:  # noqa: BLE001
                    pass  # Never let update check break tool functionality
        client_result: DsmClient = _state["client"]
        return client_result

    def _with_update_notice(result: str) -> str:
        """Append update notice to tool result (first call only, then clears)."""
        notice = _state.get("update_notice") or ""
        if notice:
            _state["update_notice"] = None
        return result + notice

    recycle_status: dict[str, bool] = {}
    hostname = config.display_name

    if "list_shares" in allowed_tools:

        @server.tool(
            name="list_shares",
            description=next(
                t.description for t in FS_MODULE_INFO.tools if t.name == "list_shares"
            ),
            annotations=_ANNO_READ_ONLY,
        )
        async def tool_list_shares(
            sort_by: str = "name",
            sort_direction: str = "asc",
        ) -> str:
            client = await _get_client()
            return _with_update_notice(
                await list_shares(
                    client,
                    sort_by=sort_by,
                    sort_direction=sort_direction,
                    recycle_bin_status=recycle_status,
                    hostname=hostname,
                    file_type_indicator=indicator,
                )
            )

    if "list_files" in allowed_tools:

        @server.tool(
            name="list_files",
            description=next(t.description for t in FS_MODULE_INFO.tools if t.name == "list_files"),
            annotations=_ANNO_READ_ONLY,
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
            client = await _get_client()
            return _with_update_notice(
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

    if "list_recycle_bin" in allowed_tools:

        @server.tool(
            name="list_recycle_bin",
            description=next(
                t.description for t in FS_MODULE_INFO.tools if t.name == "list_recycle_bin"
            ),
            annotations=_ANNO_READ_ONLY,
        )
        async def tool_list_recycle_bin(
            share: str,
            pattern: str | None = None,
            sort_by: str = "mtime",
            sort_direction: str = "desc",
            limit: int = 100,
        ) -> str:
            client = await _get_client()
            return _with_update_notice(
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

    if "search_files" in allowed_tools:

        @server.tool(
            name="search_files",
            description=next(
                t.description for t in FS_MODULE_INFO.tools if t.name == "search_files"
            ),
            annotations=_ANNO_READ_ONLY,
        )
        async def tool_search_files(
            folder_path: str,
            pattern: str | None = None,
            extension: str | None = None,
            filetype: str = "all",
            size_from: str | None = None,
            size_to: str | None = None,
            exclude_pattern: str | None = None,
            recursive: bool = True,
            limit: int = 500,
        ) -> str:
            client = await _get_client()
            return _with_update_notice(
                await search_files(
                    client,
                    folder_path=folder_path,
                    pattern=pattern,
                    extension=extension,
                    filetype=filetype,
                    size_from=size_from,
                    size_to=size_to,
                    exclude_pattern=exclude_pattern,
                    recursive=recursive,
                    limit=limit,
                    file_type_indicator=indicator,
                    timeout=search_timeout,
                    poll_interval=search_poll_interval,
                )
            )

    if "get_file_info" in allowed_tools:

        @server.tool(
            name="get_file_info",
            description=next(
                t.description for t in FS_MODULE_INFO.tools if t.name == "get_file_info"
            ),
            annotations=_ANNO_READ_ONLY,
        )
        async def tool_get_file_info(paths: list[str]) -> str:
            client = await _get_client()
            return _with_update_notice(await get_file_info(client, paths=paths))

    if "get_dir_size" in allowed_tools:

        @server.tool(
            name="get_dir_size",
            description=next(
                t.description for t in FS_MODULE_INFO.tools if t.name == "get_dir_size"
            ),
            annotations=_ANNO_READ_ONLY,
        )
        async def tool_get_dir_size(path: str) -> str:
            client = await _get_client()
            result = await get_dir_size(client, path=path, timeout=dir_size_timeout)
            return _with_update_notice(result)

    # WRITE tools
    if "create_folder" in allowed_tools:

        @server.tool(
            name="create_folder",
            description=next(
                t.description for t in FS_MODULE_INFO.tools if t.name == "create_folder"
            ),
            annotations=_ANNO_IDEMPOTENT,
        )
        async def tool_create_folder(
            paths: list[str],
            force_parent: bool = True,
        ) -> str:
            client = await _get_client()
            result = await create_folder(client, paths=paths, force_parent=force_parent)
            return _with_update_notice(result)

    if "rename" in allowed_tools:

        @server.tool(
            name="rename",
            description=next(t.description for t in FS_MODULE_INFO.tools if t.name == "rename"),
            annotations=_ANNO_WRITE,
        )
        async def tool_rename(path: str, new_name: str) -> str:
            client = await _get_client()
            return _with_update_notice(await rename(client, path=path, new_name=new_name))

    if "copy_files" in allowed_tools:

        @server.tool(
            name="copy_files",
            description=next(t.description for t in FS_MODULE_INFO.tools if t.name == "copy_files"),
            annotations=_ANNO_WRITE,
        )
        async def tool_copy_files(
            paths: list[str],
            dest_folder: str,
            overwrite: bool = False,
        ) -> str:
            client = await _get_client()
            return _with_update_notice(
                await copy_files(
                    client,
                    paths=paths,
                    dest_folder=dest_folder,
                    overwrite=overwrite,
                    file_type_indicator=indicator,
                    timeout=copy_move_timeout,
                )
            )

    if "move_files" in allowed_tools:

        @server.tool(
            name="move_files",
            description=next(t.description for t in FS_MODULE_INFO.tools if t.name == "move_files"),
            annotations=_ANNO_DESTRUCTIVE,
        )
        async def tool_move_files(
            paths: list[str],
            dest_folder: str,
            overwrite: bool = False,
        ) -> str:
            client = await _get_client()
            return _with_update_notice(
                await move_files(
                    client,
                    paths=paths,
                    dest_folder=dest_folder,
                    overwrite=overwrite,
                    file_type_indicator=indicator,
                    timeout=copy_move_timeout,
                )
            )

    if "delete_files" in allowed_tools:

        @server.tool(
            name="delete_files",
            description=next(
                t.description for t in FS_MODULE_INFO.tools if t.name == "delete_files"
            ),
            annotations=_ANNO_DESTRUCTIVE,
        )
        async def tool_delete_files(
            paths: list[str],
            recursive: bool = True,
        ) -> str:
            client = await _get_client()
            return _with_update_notice(
                await delete_files(
                    client,
                    paths=paths,
                    recursive=recursive,
                    file_type_indicator=indicator,
                    recycle_bin_status=recycle_status,
                    timeout=delete_timeout,
                )
            )

    if "restore_from_recycle_bin" in allowed_tools:

        @server.tool(
            name="restore_from_recycle_bin",
            description=next(
                t.description for t in FS_MODULE_INFO.tools if t.name == "restore_from_recycle_bin"
            ),
            annotations=_ANNO_WRITE,
        )
        async def tool_restore_from_recycle_bin(
            share: str,
            paths: list[str],
            dest_folder: str | None = None,
            overwrite: bool = False,
        ) -> str:
            client = await _get_client()
            return _with_update_notice(
                await restore_from_recycle_bin(
                    client,
                    share=share,
                    paths=paths,
                    dest_folder=dest_folder,
                    overwrite=overwrite,
                    file_type_indicator=indicator,
                    timeout=delete_timeout,
                )
            )
