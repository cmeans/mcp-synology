"""Synology Photos module: MODULE_INFO, register(), PhotosSettings.

Not yet in the server's module registry: the module ships in parts (#258), and is
switched on once every tool in ``docs/specs/photos-module-spec.md`` is in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

# download_photos only reads the NAS, but it writes (and with overwrite=true replaces)
# files on this machine, as File Station's download_file.
_ANNO_LOCAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


class PhotosSettings(BaseModel):
    """Photos module settings."""

    # Local folders download_photos may write to (and upload_photos read from), added to
    # the top-level allowed_local_paths (#257). Unset with no top-level list: Downloads.
    allowed_local_paths: list[str] | None = Field(default=None, min_length=1)

    @field_validator("allowed_local_paths")
    @classmethod
    def _absolute_local_paths(cls, value: list[str] | None) -> list[str] | None:
        return check_local_path_entries("allowed_local_paths", value)


MODULE_INFO = ModuleInfo(
    name="photos",
    description="Browse, search, organize and share a Synology Photos library",
    required_apis=[
        ApiRequirement(api_name="SYNO.Foto.Browse.Item", min_version=2),
        ApiRequirement(api_name="SYNO.Foto.Browse.Folder", min_version=2),
        ApiRequirement(api_name="SYNO.Foto.Browse.Album", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Browse.NormalAlbum", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Browse.ConditionAlbum", min_version=2, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Browse.Person", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Browse.GeneralTag", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Browse.Geocoding", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Search.Search", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Search.Filter", min_version=2, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Thumbnail", min_version=2, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Download", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Upload.Item", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.BackgroundTask.File", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.BackgroundTask.Info", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Sharing.Passphrase", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Sharing.Misc", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.PhotoRequest", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.UserInfo", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Browse.Concept", min_version=2, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Setting.Admin", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Setting.TeamSpace", min_version=1, optional=True),
        # Shared Space mirrors (SYNO.FotoTeam.*) are resolved per call, not required.
    ],
    tools=[
        ToolInfo(
            name="list_photos",
            description=(
                "List photos and videos in Synology Photos, newest first by default. Narrow by "
                "folder, person and tag, plus date range (start_date/end_date, YYYY-MM-DD), "
                "type (photo/video), min_rating or favorite (Photos 1.9.1+); or list one album,"
                " place or concept (1.9.1+) on its own; or search by keyword (filename and "
                "tags). Returns id, filename, taken time, type, size and folder; paged with "
                "offset/limit (default 50) with the total. space: personal (default) or shared."
                " Use the ids with get_photo, download_photos, tag_photos and the other photo "
                "tools. For files outside Photos, use list_files."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="list_photo_collections",
            description=(
                "List Synology Photos collections with their ids and item counts. type: "
                "album (yours, with sharing status), shared_with_me (albums others shared "
                "with you), person (people from face recognition; include_hidden for hidden "
                "ones), tag, place, concept (things and scenes Photos recognized; 1.9.1+), "
                "or folder (subfolders of parent, default the space root). name filters by "
                "substring. Paged with offset/limit (default 100). space: personal "
                "(default) or shared; albums are personal only."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_photo",
            description=(
                "Get full details for Synology Photos items by id: filename, folder, taken "
                "time, size, resolution, rating, favorite (1.9.1+), description, tags, "
                "people, GPS location and address, camera details and the full EXIF table "
                "(exif=true). Up to 20 ids. space: personal (default) or shared."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="download_photos",
            description=(
                "Download Synology Photos items to a local folder by id: the original file, "
                "or a JPEG preview (size xl ~1280px, m ~320px, sm ~240px). Only writes "
                "inside the allowed local folders, and never overwrites unless "
                "overwrite=true. Up to 50 ids. space: personal (default) or shared. For "
                "files outside Photos, use download_file."
            ),
            permission_tier=PermissionTier.READ,
            annotations=_ANNO_LOCAL_WRITE,
            title="Photos: Download",
        ),
    ],
    settings_schema=PhotosSettings,
)


def register(ctx: RegisterContext) -> None:
    """Register Photos tools with the MCP server."""
    from mcp_synology.modules.photos.browse import get_photo, list_photo_collections, list_photos
    from mcp_synology.modules.photos.transfer import download_photos

    settings = PhotosSettings(**ctx.settings_dict)
    local_roots = allowed_local_roots(
        settings.allowed_local_paths,
        shared_paths=ctx.allowed_local_paths,
        module="photos",
    )
    server = ctx.server
    manager = ctx.manager
    tools = {t.name: t for t in MODULE_INFO.tools}

    def _tool(name: str) -> dict[str, object]:
        info = tools[name]
        return {
            "name": name,
            "description": info.description,
            "annotations": info.annotations or default_annotations(info.permission_tier),
            "title": info.title,
        }

    if "list_photos" in ctx.allowed_tools:

        @server.tool(**_tool("list_photos"))  # type: ignore[arg-type]
        async def tool_list_photos(
            space: str = "personal",
            folder: str | None = None,
            album: str | None = None,
            person: str | None = None,
            place: int | None = None,
            tag: str | None = None,
            keyword: str | None = None,
            start_date: str | None = None,
            end_date: str | None = None,
            type: str = "all",  # noqa: A002 - the documented parameter name
            min_rating: int | None = None,
            favorite: bool | None = None,
            concept: int | None = None,
            sort_by: str = "taken",
            sort_direction: str = "desc",
            offset: int = 0,
            limit: int = 50,
        ) -> str:
            client = await manager.get_client()
            return await list_photos(
                client,
                space=space,
                folder=folder,
                album=album,
                person=person,
                place=place,
                tag=tag,
                keyword=keyword,
                start_date=start_date,
                end_date=end_date,
                type=type,
                min_rating=min_rating,
                favorite=favorite,
                concept=concept,
                sort_by=sort_by,
                sort_direction=sort_direction,
                offset=offset,
                limit=limit,
            )

    if "list_photo_collections" in ctx.allowed_tools:

        @server.tool(**_tool("list_photo_collections"))  # type: ignore[arg-type]
        async def tool_list_photo_collections(
            type: str,  # noqa: A002 - the documented parameter name
            space: str = "personal",
            name: str | None = None,
            parent: str | None = None,
            include_hidden: bool = False,
            offset: int = 0,
            limit: int = 100,
        ) -> str:
            client = await manager.get_client()
            return await list_photo_collections(
                client,
                type=type,
                space=space,
                name=name,
                parent=parent,
                include_hidden=include_hidden,
                offset=offset,
                limit=limit,
            )

    if "get_photo" in ctx.allowed_tools:

        @server.tool(**_tool("get_photo"))  # type: ignore[arg-type]
        async def tool_get_photo(
            ids: list[int], space: str = "personal", exif: bool = False
        ) -> str:
            client = await manager.get_client()
            return await get_photo(client, ids=ids, space=space, exif=exif)

    if "download_photos" in ctx.allowed_tools:

        @server.tool(**_tool("download_photos"))  # type: ignore[arg-type]
        async def tool_download_photos(
            ids: list[int],
            dest_folder: str | None = None,
            size: str = "original",
            space: str = "personal",
            overwrite: bool = False,
        ) -> str:
            client = await manager.get_client()
            return await download_photos(
                client,
                ids=ids,
                dest_folder=dest_folder,
                size=size,
                space=space,
                overwrite=overwrite,
                allowed_roots=local_roots,
            )
