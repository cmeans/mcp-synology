"""Synology Photos module: MODULE_INFO, register(), PhotosSettings.

Not yet in the server's module registry: the module ships in parts (#258), and is
switched on once every tool in ``docs/specs/photos-module-spec.md`` is in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
# Clients may auto-approve tools that don't claim destructiveHint. Every Photos write
# can lose something: a replaced name, rating, description, cover or share setting, a
# removed tag or album member, merged people, a deleted request or album. Annotations
# can't depend on action (#183). The setters that only set absolute values are
# idempotent: repeating one changes nothing more. edit_photo isn't: shift_hours and
# rotate are relative, so a client's retry would shift or rotate again.
_ANNO_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)
_ANNO_DESTRUCTIVE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True
)


class PhotosSettings(BaseModel):
    """Photos module settings."""

    # Local folders download_photos may write to and upload_photos read from, added to
    # the top-level allowed_local_paths (#257). Unset with no top-level list: Downloads.
    allowed_local_paths: list[str] | None = Field(default=None, min_length=1)

    # Seconds copy_photos, move_photos and the deletes wait before handing a task off
    # with a photos:<id> handle (default 30, cap 600), as File Station's
    # copy_move_timeout (#245).
    task_timeout: int = Field(default=30, ge=1, le=600)

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
        ToolInfo(
            name="photo_album",
            description=(
                "Create or change a Synology Photos album. action: create (optionally with item"
                " ids), rename, add or remove items (the photos stay in their folders), "
                "set_cover, copy, create_smart or set_condition (a smart album that fills "
                "itself from folder, people, tags, places, dates, type and rating conditions). "
                "Personal space albums only. To delete albums use delete_photo_albums."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="photo_share_link",
            description=(
                "Share a Synology Photos album by link, change its link settings, or stop "
                "sharing. action: share (returns the link), update or stop. public: none, view "
                "or download; password (empty string removes it); expires (YYYY-MM-DD, or "
                "never); users/groups with view, download or upload roles. Personal space "
                "albums only."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="photo_folder",
            description=(
                "Create or rename a folder in Synology Photos, or set its cover photo. action: "
                "create (name inside parent, default the space root), rename or set_cover. "
                "space: personal (default) or shared. To copy or move folders use copy_photos /"
                " move_photos; to delete, delete_photo_folders. For folders outside Photos, use"
                " create_folder and rename."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE_IDEMPOTENT,
            title="Photos: Folder",
        ),
        ToolInfo(
            name="tag_photos",
            description=(
                "Add or remove tags on Synology Photos items. Tags are given by name; add "
                "creates missing tags. Photos writes tags into the original files and re-"
                "indexes them, so file dates and sizes change. A tag disappears once no item "
                "has it, so to delete a tag remove it from every item that has it (list_photos "
                "tag=...); to rename, add the new name to those items, then remove the old one."
                " space: personal (default) or shared."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="edit_photo",
            description=(
                "Edit Synology Photos items: rating (0-5, 0 clears), description, taken time "
                "(set to a date-time, or shift by hours), rotate (left or right), favorite "
                "(Photos 1.9.1+). Applies to every id given. Photos writes these into the "
                "original files, so file dates and sizes change. Location can't be edited. "
                "space: personal (default) or shared."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="edit_person",
            description=(
                "Change a person (face-recognition group) in Synology Photos: rename (empty "
                "name clears it), hide, unhide, or set_cover to one of their photos. Use "
                "list_photo_collections type=person for ids. To merge people use merge_people. "
                "space: personal (default) or shared."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE_IDEMPOTENT,
        ),
        ToolInfo(
            name="photo_request",
            description=(
                "Manage Synology Photos photo requests: links that let others upload into one "
                "of your folders. action: create (subject, folder, optional description, size "
                "limit in MiB, expiry date), list, update or delete. Returns the request link."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="merge_people",
            description=(
                "Merge people (face-recognition groups) in Synology Photos into one person, "
                "keeping the target's id and the given name. This can't be undone. space: "
                "personal (default) or shared."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="delete_photo_albums",
            description=(
                "Delete Synology Photos albums (normal or smart) and their share links. The "
                "photos themselves stay. Confirm with the user first."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="upload_photos",
            description=(
                "Upload local photos or videos into a Synology Photos folder (default the space"
                " root). on_duplicate: rename (default, keeps both) or skip. Optionally add "
                "them to an album. Only reads files inside the allowed local folders. Photos "
                "indexes new items over the next seconds to minutes. space: personal (default) "
                "or shared."
            ),
            permission_tier=PermissionTier.WRITE,
            title="Photos: Upload",
        ),
        ToolInfo(
            name="copy_photos",
            description=(
                "Copy Synology Photos items and/or folders into another folder of the same or "
                "the other space. on_conflict: skip (default) or overwrite. Waits up to "
                "task_timeout seconds; a longer copy keeps running on the NAS and the reply "
                "gives a handle for photo_task. Overwrite replaces files at the destination."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
            title="Photos: Copy",
        ),
        ToolInfo(
            name="move_photos",
            description=(
                "Move Synology Photos items and/or folders into another folder of the same or "
                "the other space. on_conflict: skip (default) or overwrite. Waits up to "
                "task_timeout seconds; a longer move keeps running on the NAS and the reply "
                "gives a handle for photo_task. Overwrite replaces files at the destination."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
            title="Photos: Move",
        ),
        ToolInfo(
            name="photo_task",
            description=(
                "Follow up on a Synology Photos copy, move or delete that is still running. "
                "With a handle (photos:<id>): action=status shows progress; action=stop stops "
                "it (it may then be partly done). Without a handle: lists this user's copy, "
                "move and delete tasks. action=clear removes finished tasks from that list."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="delete_photos",
            description=(
                "Delete Synology Photos items (the files themselves) by id. They go to the "
                "recycle bin of the share that holds the space if it has one, otherwise they're"
                " gone. A large delete keeps running on the NAS and the reply gives a handle "
                "for photo_task. Confirm with the user first. space: personal (default) or "
                "shared. To only take items out of an album, use photo_album remove."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
            title="Photos: Delete items",
        ),
        ToolInfo(
            name="delete_photo_folders",
            description=(
                "Delete folders in Synology Photos with everything in them (files and "
                "subfolders). They go to the recycle bin of the share that holds the space if "
                "it has one, otherwise they're gone. A large delete keeps running on the NAS "
                "and the reply gives a handle for photo_task. Confirm with the user first. "
                "space: personal (default) or shared."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
            title="Photos: Delete folders",
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

    if "photo_album" in ctx.allowed_tools:

        @server.tool(**_tool("photo_album"))  # type: ignore[arg-type]
        async def tool_photo_album(
            action: str,
            album: str | None = None,
            name: str | None = None,
            ids: list[int] | None = None,
            condition: dict[str, Any] | None = None,
        ) -> str:
            from mcp_synology.modules.photos.albums import photo_album

            client = await manager.get_client()
            return await photo_album(
                client, action=action, album=album, name=name, ids=ids, condition=condition
            )

    if "photo_share_link" in ctx.allowed_tools:

        @server.tool(**_tool("photo_share_link"))  # type: ignore[arg-type]
        async def tool_photo_share_link(
            action: str,
            album: str,
            public: str | None = None,
            password: str | None = None,
            expires: str | None = None,
            members: list[dict[str, Any]] | None = None,
        ) -> str:
            from mcp_synology.modules.photos.albums import photo_share_link

            client = await manager.get_client()
            return await photo_share_link(
                client,
                action=action,
                album=album,
                public=public,
                password=password,
                expires=expires,
                members=members,
            )

    if "photo_folder" in ctx.allowed_tools:

        @server.tool(**_tool("photo_folder"))  # type: ignore[arg-type]
        async def tool_photo_folder(
            action: str,
            folder: str | None = None,
            parent: str = "/",
            name: str | None = None,
            item_id: int | None = None,
            space: str = "personal",
        ) -> str:
            from mcp_synology.modules.photos.organize import photo_folder

            client = await manager.get_client()
            return await photo_folder(
                client,
                action=action,
                folder=folder,
                parent=parent,
                name=name,
                item_id=item_id,
                space=space,
            )

    if "tag_photos" in ctx.allowed_tools:

        @server.tool(**_tool("tag_photos"))  # type: ignore[arg-type]
        async def tool_tag_photos(
            action: str, ids: list[int], tags: list[str], space: str = "personal"
        ) -> str:
            from mcp_synology.modules.photos.organize import tag_photos

            client = await manager.get_client()
            return await tag_photos(client, action=action, ids=ids, tags=tags, space=space)

    if "edit_photo" in ctx.allowed_tools:

        @server.tool(**_tool("edit_photo"))  # type: ignore[arg-type]
        async def tool_edit_photo(
            ids: list[int],
            space: str = "personal",
            rating: int | None = None,
            description: str | None = None,
            taken_time: str | None = None,
            shift_hours: float | None = None,
            rotate: str | None = None,
            favorite: bool | None = None,
        ) -> str:
            from mcp_synology.modules.photos.organize import edit_photo

            client = await manager.get_client()
            return await edit_photo(
                client,
                ids=ids,
                space=space,
                rating=rating,
                description=description,
                taken_time=taken_time,
                shift_hours=shift_hours,
                rotate=rotate,
                favorite=favorite,
            )

    if "edit_person" in ctx.allowed_tools:

        @server.tool(**_tool("edit_person"))  # type: ignore[arg-type]
        async def tool_edit_person(
            action: str,
            person: str,
            name: str | None = None,
            item_id: int | None = None,
            space: str = "personal",
        ) -> str:
            from mcp_synology.modules.photos.organize import edit_person

            client = await manager.get_client()
            return await edit_person(
                client, action=action, person=person, name=name, item_id=item_id, space=space
            )

    if "photo_request" in ctx.allowed_tools:

        @server.tool(**_tool("photo_request"))  # type: ignore[arg-type]
        async def tool_photo_request(
            action: str,
            request: str | None = None,
            subject: str | None = None,
            description: str | None = None,
            folder: str | None = None,
            space: str = "personal",
            max_file_mib: int | None = None,
            expires: str | None = None,
        ) -> str:
            from mcp_synology.modules.photos.organize import photo_request

            client = await manager.get_client()
            return await photo_request(
                client,
                action=action,
                request=request,
                subject=subject,
                description=description,
                folder=folder,
                space=space,
                max_file_mib=max_file_mib,
                expires=expires,
            )

    if "merge_people" in ctx.allowed_tools:

        @server.tool(**_tool("merge_people"))  # type: ignore[arg-type]
        async def tool_merge_people(
            target: str, merge: list[str], name: str | None = None, space: str = "personal"
        ) -> str:
            from mcp_synology.modules.photos.organize import merge_people

            client = await manager.get_client()
            return await merge_people(client, target=target, merge=merge, name=name, space=space)

    if "delete_photo_albums" in ctx.allowed_tools:

        @server.tool(**_tool("delete_photo_albums"))  # type: ignore[arg-type]
        async def tool_delete_photo_albums(albums: list[str]) -> str:
            from mcp_synology.modules.photos.albums import delete_photo_albums

            client = await manager.get_client()
            return await delete_photo_albums(client, albums=albums)

    if "upload_photos" in ctx.allowed_tools:

        @server.tool(**_tool("upload_photos"))  # type: ignore[arg-type]
        async def tool_upload_photos(
            local_paths: list[str],
            folder: str = "/",
            space: str = "personal",
            on_duplicate: str = "rename",
            album: str | None = None,
        ) -> str:
            from mcp_synology.modules.photos.transfer import upload_photos

            client = await manager.get_client()
            return await upload_photos(
                client,
                local_paths=local_paths,
                folder=folder,
                space=space,
                on_duplicate=on_duplicate,
                album=album,
                allowed_roots=local_roots,
            )

    for operation in ("copy", "move"):
        name = f"{operation}_photos"
        if name not in ctx.allowed_tools:
            continue

        def _make(operation: str) -> Any:
            async def tool_copy_or_move(
                destination: str,
                ids: list[int] | None = None,
                folders: list[str] | None = None,
                space: str = "personal",
                dest_space: str | None = None,
                on_conflict: str = "skip",
            ) -> str:
                from mcp_synology.modules.photos.tasks import copy_or_move

                client = await manager.get_client()
                return await copy_or_move(
                    client,
                    operation=operation,
                    ids=ids,
                    folders=folders,
                    destination=destination,
                    space=space,
                    dest_space=dest_space,
                    on_conflict=on_conflict,
                    timeout=settings.task_timeout,
                )

            return tool_copy_or_move

        server.tool(**_tool(name))(_make(operation))  # type: ignore[arg-type]

    if "photo_task" in ctx.allowed_tools:

        @server.tool(**_tool("photo_task"))  # type: ignore[arg-type]
        async def tool_photo_task(handle: str | None = None, action: str = "status") -> str:
            from mcp_synology.modules.photos.tasks import photo_task

            client = await manager.get_client()
            return await photo_task(client, handle=handle, action=action)

    if "delete_photos" in ctx.allowed_tools:

        @server.tool(**_tool("delete_photos"))  # type: ignore[arg-type]
        async def tool_delete_photos(ids: list[int], space: str = "personal") -> str:
            from mcp_synology.modules.photos.tasks import delete_photos

            client = await manager.get_client()
            return await delete_photos(client, ids=ids, space=space, timeout=settings.task_timeout)

    if "delete_photo_folders" in ctx.allowed_tools:

        @server.tool(**_tool("delete_photo_folders"))  # type: ignore[arg-type]
        async def tool_delete_photo_folders(folders: list[str], space: str = "personal") -> str:
            from mcp_synology.modules.photos.tasks import delete_photo_folders

            client = await manager.get_client()
            return await delete_photo_folders(
                client, folders=folders, space=space, timeout=settings.task_timeout
            )
