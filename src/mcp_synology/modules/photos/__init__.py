"""Synology Photos module: MODULE_INFO, register(), PhotosSettings.

Not yet in the server's module registry: the module ships in parts (#258), and is
switched on once every tool in ``docs/specs/photos-module-spec.md`` is in.
"""

# No `from __future__ import annotations` here: a few parameter descriptions name this
# config's allowed local folders, known only inside register(), and the SDK reads the
# Annotated[..., Field(description=...)] metadata, so it must be built when each
# handler is defined (rc6 live test; see core.local_paths.roots_for_description).

from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, field_validator

from mcp_synology.core.local_paths import (
    allowed_local_roots,
    check_local_path_entries,
    roots_for_description,
)
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
    # copy_move_timeout (#245). upload_photos starts no new file after it, or after
    # 45 s if it's longer, to stay under clients' tool-call timeouts (#416).
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
                "List or search photos and videos in Synology Photos, newest first by default, "
                "with the total. folder, person and tag combine with the date, type, rating and "
                "favorite filters; album, place, concept and keyword each work alone. Each row "
                "has the item's id, filename, taken time, type, size and folder; pass the ids to "
                "get_photo for full details or to download_photos to save the files. For files "
                "outside Photos, use File Station's list_files when that module is enabled."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="list_photo_collections",
            description=(
                "List one type of Synology Photos collection with ids and item counts: albums, "
                "albums shared with you, people, tags, places, concepts or folders. Use it to "
                "find the ids and names that list_photos filters by. Paged; name narrows the "
                "list by substring."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="get_photo",
            description=(
                "Get full details for Synology Photos items by id: filename, folder, taken "
                "time, size, resolution, rating, favorite (Photos 1.9.1+), description, tags, "
                "people, GPS location and address, and camera details, plus the full EXIF "
                "table with exif=true. Use it when list_photos' one-line rows aren't enough. "
                "IDs that don't exist are listed as not found."
            ),
            permission_tier=PermissionTier.READ,
        ),
        ToolInfo(
            name="download_photos",
            description=(
                "Download Synology Photos items by id to a folder on this machine: the original "
                "files or JPEG previews, one file per item, named after the original. Only "
                "writes inside the allowed local folders, and never replaces an existing file "
                "unless overwrite=true. For files outside Photos, use File Station's "
                "download_file when that module is enabled."
            ),
            permission_tier=PermissionTier.READ,
            annotations=_ANNO_LOCAL_WRITE,
            title="Photos: Download",
        ),
        ToolInfo(
            name="photo_album",
            description=(
                "Create or change a Synology Photos album: create one (normal, or smart: filled "
                "automatically from a condition), rename it, add or remove items, set its "
                "cover, copy it, or replace a smart album's condition. The photos themselves "
                "never move. Personal space albums only. To delete albums use "
                "delete_photo_albums; to share one, photo_share_link."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="photo_share_link",
            description=(
                "Share a Synology Photos album by link, change the link's public access, "
                "password, expiry and members, or stop sharing. A password or an expiry needs "
                "public view or download: Photos drops them on a link without public access, so "
                "they're refused there. Stopping keeps the link, and sharing again brings it "
                "back with its settings reset. Personal space albums only."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="photo_folder",
            description=(
                "Create or rename a folder in Synology Photos, or set its cover photo. A name "
                "the parent folder already has is refused. To copy or move folders use "
                "copy_photos / move_photos; to delete, delete_photo_folders. For folders "
                "outside Photos, use File Station's create_folder and rename when File Station "
                "write is enabled, or ask the user to do it in DSM."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE_IDEMPOTENT,
            title="Photos: Folder",
        ),
        ToolInfo(
            name="tag_photos",
            description=(
                "Add or remove tags on Synology Photos items. Photos writes tags into the "
                "original files and re-indexes them, so file dates and sizes change. A tag "
                "disappears once no item has it, so to delete a tag remove it from every item "
                "that has it (list_photos tag=...); to rename, add the new name to those items, "
                "then remove the old one."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="edit_photo",
            description=(
                "Edit Synology Photos items: rating, description, taken time (set or shift), "
                "rotation and favorite (Photos 1.9.1+). Pass only the fields to change; they "
                "apply to every id given. Photos writes these into the original files, so file "
                "dates and sizes change. Location can't be edited; for tags use tag_photos."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="edit_person",
            description=(
                "Change one person (a face-recognition group) in Synology Photos: rename them, "
                "hide or unhide them in the people list, or set their cover photo. To combine "
                "groups that are the same person, use merge_people."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE_IDEMPOTENT,
        ),
        ToolInfo(
            name="photo_request",
            description=(
                "Manage Synology Photos photo requests: links that let anyone with the link "
                "upload files into one of your Photos folders. action: create (returns the "
                "link), list (every request with its passphrase and link), update, or delete "
                "(its link stops working)."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="merge_people",
            description=(
                "Merge people (face-recognition groups) in Synology Photos that are the same "
                "person: every person in merge joins target, which keeps its id, and the merged "
                "people are gone. If any person isn't found, nothing is merged. This can't be "
                "undone, so confirm with the user first. To rename or hide one person, use "
                "edit_person."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="delete_photo_albums",
            description=(
                "Delete Synology Photos albums (normal or smart) and their share links. The "
                "photos in them stay where they are, but the albums' selections, conditions and "
                "links can't be recovered. Confirm with the user first. To only take items out "
                "of an album, use photo_album action=remove; to delete the photos themselves, "
                "delete_photos."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
        ),
        ToolInfo(
            name="upload_photos",
            description=(
                "Upload photos or videos from this machine into a Synology Photos folder, "
                "optionally adding them to an existing album. Only reads files inside the "
                "allowed local folders. Starts no new file after the configured task timeout "
                "(30 s by default, at most 45 s) and lists the files not tried, to pass to "
                "another call. "
                "Photos indexes new items over the next seconds to minutes; until then their "
                "taken dates and previews may be missing."
            ),
            permission_tier=PermissionTier.WRITE,
            title="Photos: Upload",
        ),
        ToolInfo(
            name="copy_photos",
            description=(
                "Copy Synology Photos items and/or whole folders into another folder, in the "
                "same space or the other one. Waits up to the configured task timeout (30 s by "
                "default); a longer copy keeps running on the NAS and the reply gives a handle "
                "for photo_task. With on_conflict=overwrite it replaces files at the "
                "destination."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
            title="Photos: Copy",
        ),
        ToolInfo(
            name="move_photos",
            description=(
                "Move Synology Photos items and/or whole folders into another folder of the "
                "same space, or from the personal space into Shared Space (Photos can't move "
                "out of Shared Space: copy, then delete). Waits up to the configured task "
                "timeout (30 s by default); a longer move keeps running on the NAS and the "
                "reply gives a handle for photo_task. With on_conflict=overwrite it replaces "
                "files at the destination."
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
                "move and delete tasks. action=clear removes finished tasks from that list. "
                "Don't poll in a loop: check when the user asks, or before doing something "
                "that depends on the result."
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
                "for photo_task. Confirm with the user first. To only take items out of an "
                "album, use photo_album remove."
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
                "and the reply gives a handle for photo_task. Confirm with the user first."
            ),
            permission_tier=PermissionTier.WRITE,
            annotations=_ANNO_DESTRUCTIVE,
            title="Photos: Delete folders",
        ),
    ],
    settings_schema=PhotosSettings,
)


# Parameter descriptions used by several tools (#247).
_SPACE = "`personal` (default) or `shared` (Shared Space). IDs belong to one space."
_ITEM_IDS = "Item IDs from list_photos, at most 1,000 per call."
_ALBUM = "Album ID (list_photo_collections type=album) or unique name."
_PERSON = "Person ID (list_photo_collections type=person) or unique name."
_FOLDER = "Folder ID (list_photo_collections type=folder) or path, such as `/Trips/2024`."
_EXPIRES = (
    "`YYYY-MM-DD` (end of that day, UTC), a date-time with offset "
    "(`2026-12-31T18:00-05:00`) or `never`"
)
_OFFSET = "Entries to skip, for the next page."


def register(ctx: "RegisterContext") -> None:
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
            space: Annotated[str, Field(description=(_SPACE))] = "personal",
            folder: Annotated[
                str | None,
                Field(
                    description=(f"Only items directly in this folder, not subfolders. {_FOLDER}")
                ),
            ] = None,
            album: Annotated[
                str | None, Field(description=(f"One album only (personal space). {_ALBUM}"))
            ] = None,
            person: Annotated[
                str | None, Field(description=(f"Only items showing this person. {_PERSON}"))
            ] = None,
            place: Annotated[
                int | None,
                Field(
                    description=(
                        "One place only: a place ID from list_photo_collections type=place."
                    )
                ),
            ] = None,
            tag: Annotated[
                str | None, Field(description=("Only items with this tag name."))
            ] = None,
            keyword: Annotated[
                str | None,
                Field(
                    description=(
                        "Search filenames and tag names; works alone, in search order (no sorting)."
                    )
                ),
            ] = None,
            start_date: Annotated[
                str | None,
                Field(
                    description=(
                        "Taken on or after this date, `YYYY-MM-DD` (the photo's local time)."
                    )
                ),
            ] = None,
            end_date: Annotated[
                str | None, Field(description=("Taken on or before this date, `YYYY-MM-DD`."))
            ] = None,
            type: Annotated[  # noqa: A002 - the documented parameter name
                str, Field(description=("`all` (default), `photo` or `video`."))
            ] = "all",
            min_rating: Annotated[int | None, Field(description=("Minimum rating, 1-5."))] = None,
            favorite: Annotated[
                bool | None,
                Field(
                    description=("`true`: favorites only (Photos 1.9.1+). `false` doesn't filter.")
                ),
            ] = None,
            concept: Annotated[
                int | None,
                Field(
                    description=(
                        "One concept only (a thing or scene Photos recognized): an ID from "
                        "list_photo_collections type=concept. Photos 1.9.1+."
                    )
                ),
            ] = None,
            sort_by: Annotated[
                str, Field(description=("`taken` (default), `filename`, `size` or `type`."))
            ] = ("taken"),
            sort_direction: Annotated[
                str, Field(description=("`desc` (default) or `asc`."))
            ] = "desc",
            offset: Annotated[int, Field(description=(_OFFSET))] = 0,
            limit: Annotated[int, Field(description=("Most items to return, 1-500."))] = 50,
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
            type: Annotated[  # noqa: A002 - the documented parameter name
                str,
                Field(
                    description=(
                        "`album` (yours, with sharing status), `shared_with_me` (albums others "
                        "shared with you), `person` (face recognition), `tag`, `place`, `concept` "
                        "(things and scenes Photos recognized; 1.9.1+) or `folder` (subfolders of "
                        "`parent`)."
                    )
                ),
            ],
            space: Annotated[
                str, Field(description=(f"{_SPACE} Albums are always personal."))
            ] = "personal",
            name: Annotated[
                str | None,
                Field(
                    description=("Only names containing this text, any case (folders: the path).")
                ),
            ] = None,
            parent: Annotated[
                str | None,
                Field(
                    description=(
                        "`type=folder`: the folder ID or path to list (default the space root)."
                    )
                ),
            ] = None,
            include_hidden: Annotated[
                bool,
                Field(
                    description=(
                        "`person`, `concept`: include hidden ones (and people with few photos)."
                    )
                ),
            ] = False,
            offset: Annotated[int, Field(description=(_OFFSET))] = 0,
            limit: Annotated[int, Field(description=("Most entries to return, 1-1,000."))] = 100,
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
            ids: Annotated[list[int], Field(description=("1-20 item IDs from list_photos."))],
            space: Annotated[str, Field(description=(_SPACE))] = "personal",
            exif: Annotated[
                bool, Field(description=("Also show each item's full EXIF table."))
            ] = False,
        ) -> str:
            client = await manager.get_client()
            return await get_photo(client, ids=ids, space=space, exif=exif)

    if "download_photos" in ctx.allowed_tools:

        @server.tool(**_tool("download_photos"))  # type: ignore[arg-type]
        async def tool_download_photos(
            ids: Annotated[list[int], Field(description=("1-50 item IDs from list_photos."))],
            dest_folder: Annotated[
                str | None,
                Field(
                    description=(
                        f"An existing local folder: {roots_for_description(local_roots)}, or "
                        "one below it; default the allowed folder, when there's only one."
                    )
                ),
            ] = None,
            size: Annotated[
                str,
                Field(
                    description=(
                        "`original` (default), or a JPEG preview saved as `<name>_<size>.jpg`: "
                        "`xl` (~1280 px), `m` (~320 px) or `sm` (~240 px)."
                    )
                ),
            ] = "original",
            space: Annotated[str, Field(description=(_SPACE))] = "personal",
            overwrite: Annotated[
                bool,
                Field(
                    description=(
                        "Replace a local file with the same name; otherwise that item is skipped."
                    )
                ),
            ] = False,
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
            action: Annotated[
                str,
                Field(
                    description=(
                        "`create` (`name`, optional `ids`), `create_smart` (`name`, `condition`), "
                        "`rename` (`name`), `add` / `remove` (`ids`; normal albums), `set_cover` "
                        "(one id in `ids`), `copy` (a normal album, into a new one named `name`) "
                        "or `set_condition` (replaces a smart album's)."
                    )
                ),
            ],
            album: Annotated[
                str | None, Field(description=(f"The album to change; not for creates. {_ALBUM}"))
            ] = (None),
            name: Annotated[
                str | None,
                Field(
                    description=("Album name for `create`, `create_smart`, `rename` and `copy`.")
                ),
            ] = None,
            ids: Annotated[
                list[int] | None,
                Field(
                    description=(
                        "Personal space item IDs from list_photos, at most 1,000; exactly one "
                        "for `set_cover`."
                    )
                ),
            ] = None,
            condition: Annotated[
                dict[str, Any] | None,
                Field(
                    description=(
                        "At least one of `folders` (IDs or paths), `people` (IDs or names), `tags` "
                        "(names), `places` (place IDs), `start_date` / `end_date` (`YYYY-MM-DD`), "
                        "`type` (`all`, `photo`, `video`), `ratings` (list of 0-5), `keywords`; "
                        "`people_match`, `tags_match`, `keywords_match`: `any` (default) or `all`. "
                        'Example: `{"tags": ["beach"], "start_date": "2024-01-01"}`.'
                    )
                ),
            ] = None,
        ) -> str:
            from mcp_synology.modules.photos.albums import photo_album

            client = await manager.get_client()
            return await photo_album(
                client, action=action, album=album, name=name, ids=ids, condition=condition
            )

    if "photo_share_link" in ctx.allowed_tools:

        @server.tool(**_tool("photo_share_link"))  # type: ignore[arg-type]
        async def tool_photo_share_link(
            action: Annotated[
                str,
                Field(
                    description=(
                        "`share` (turns the link on, public access `none` unless given; returns "
                        "the link), `update` (an album already shared) or `stop` (turns it off; "
                        "no settings)."
                    )
                ),
            ],
            album: Annotated[str, Field(description=(_ALBUM))],
            public: Annotated[
                str | None,
                Field(
                    description=(
                        "What anyone with the link can do: `none` (invited members only), `view` "
                        "or `download`. `none` also clears the password and expiry."
                    )
                ),
            ] = None,
            password: Annotated[
                str | None,
                Field(
                    description=(
                        'Link password; `""` removes it. Needs public `view` or `download`.'
                    )
                ),
            ] = None,
            expires: Annotated[
                str | None,
                Field(
                    description=(
                        f"Link expiry: {_EXPIRES}. Except `never`, needs public `view` or "
                        "`download`."
                    )
                ),
            ] = None,
            members: Annotated[
                list[dict[str, Any]] | None,
                Field(
                    description=(
                        'DSM users or groups, each `{"name": ..., "type": "user" (default) or '
                        '"group", "role": "view" (default), "download", "upload" or "none" '
                        "(removes)}`."
                    )
                ),
            ] = None,
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
            action: Annotated[
                str,
                Field(
                    description=(
                        "`create` (`name` inside `parent`), `rename` (`folder` to `name`) or "
                        "`set_cover` (`folder`, `item_id`)."
                    )
                ),
            ],
            folder: Annotated[
                str | None,
                Field(description=(f"For `rename` and `set_cover`; not the space root. {_FOLDER}")),
            ] = None,
            parent: Annotated[
                str,
                Field(
                    description=(
                        "`create`: the parent folder's ID or path (default `/`, the space root)."
                    )
                ),
            ] = "/",
            name: Annotated[
                str | None, Field(description=("One folder name, without `/`."))
            ] = None,
            item_id: Annotated[
                int | None, Field(description=("`set_cover`: an item ID from list_photos."))
            ] = None,
            space: Annotated[str, Field(description=(_SPACE))] = "personal",
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
            action: Annotated[
                str, Field(description=("`add` (creates tags that don't exist) or `remove`."))
            ],
            ids: Annotated[list[int], Field(description=(_ITEM_IDS))],
            tags: Annotated[
                list[str], Field(description=('Tag names, any case, such as `["beach", "2024"]`.'))
            ],
            space: Annotated[str, Field(description=(_SPACE))] = "personal",
        ) -> str:
            from mcp_synology.modules.photos.organize import tag_photos

            client = await manager.get_client()
            return await tag_photos(client, action=action, ids=ids, tags=tags, space=space)

    if "edit_photo" in ctx.allowed_tools:

        @server.tool(**_tool("edit_photo"))  # type: ignore[arg-type]
        async def tool_edit_photo(
            ids: Annotated[list[int], Field(description=(_ITEM_IDS))],
            space: Annotated[str, Field(description=(_SPACE))] = "personal",
            rating: Annotated[
                int | None, Field(description=("0-5; `0` clears the rating."))
            ] = None,
            description: Annotated[
                str | None,
                Field(
                    description=(
                        'New description; `""` clears it, and one of only spaces shows as `—`, '
                        "like none."
                    )
                ),
            ] = None,
            taken_time: Annotated[
                str | None,
                Field(
                    description=(
                        "Local date-time such as `2024-07-04T19:30:00`; an offset is dropped, not "
                        "converted. Not with `shift_hours`."
                    )
                ),
            ] = None,
            shift_hours: Annotated[
                float | None,
                Field(
                    description=(
                        "Shifts the taken time by hours (`-6`, `1.5`); a repeat shifts again."
                    )
                ),
            ] = None,
            rotate: Annotated[
                str | None, Field(description=("`left` or `right`; a repeat rotates again."))
            ] = None,
            favorite: Annotated[
                bool | None,
                Field(description=("`true` marks favorite, `false` unmarks (Photos 1.9.1+).")),
            ] = None,
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
            action: Annotated[
                str,
                Field(
                    description=("`rename` (`name`), `hide`, `unhide` or `set_cover` (`item_id`).")
                ),
            ],
            person: Annotated[
                str, Field(description=(f"{_PERSON} Hidden ones need include_hidden=true there."))
            ],
            name: Annotated[
                str | None, Field(description=('The new name; `""` makes them unnamed.'))
            ] = None,
            item_id: Annotated[
                int | None,
                Field(description=("An item ID from list_photos of a photo showing this person.")),
            ] = None,
            space: Annotated[str, Field(description=(_SPACE))] = "personal",
        ) -> str:
            from mcp_synology.modules.photos.organize import edit_person

            client = await manager.get_client()
            return await edit_person(
                client, action=action, person=person, name=name, item_id=item_id, space=space
            )

    if "photo_request" in ctx.allowed_tools:

        @server.tool(**_tool("photo_request"))  # type: ignore[arg-type]
        async def tool_photo_request(
            action: Annotated[
                str,
                Field(
                    description=(
                        "`create` (`subject`, `folder`), `list`, `update` (the fields given) or "
                        "`delete`."
                    )
                ),
            ],
            request: Annotated[
                str | None,
                Field(
                    description=("`update`, `delete`: the request's passphrase from action=list.")
                ),
            ] = None,
            subject: Annotated[
                str | None, Field(description=("The subject; required for `create`."))
            ] = None,
            description: Annotated[str | None, Field(description=("An optional note."))] = None,
            folder: Annotated[
                str | None,
                Field(
                    description=(
                        "The existing folder uploads go into, as a path in `space` such as "
                        "`/Inbox`; required for `create`."
                    )
                ),
            ] = None,
            space: Annotated[
                str, Field(description=("Library of `folder`: `personal` (default) or `shared`."))
            ] = "personal",
            max_file_mib: Annotated[
                int | None,
                Field(
                    description=("Largest file accepted, in MiB; `0` (create's default): no limit.")
                ),
            ] = None,
            expires: Annotated[
                str | None, Field(description=(f"Link expiry: {_EXPIRES} (create's default)."))
            ] = None,
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
            target: Annotated[str, Field(description=(f"The person to keep. {_PERSON}"))],
            merge: Annotated[
                list[str],
                Field(description=("The people merged into `target`, as IDs or unique names.")),
            ],
            name: Annotated[
                str | None, Field(description=("The name after the merge; default the target's."))
            ] = None,
            space: Annotated[str, Field(description=(_SPACE))] = "personal",
        ) -> str:
            from mcp_synology.modules.photos.organize import merge_people

            client = await manager.get_client()
            return await merge_people(client, target=target, merge=merge, name=name, space=space)

    if "delete_photo_albums" in ctx.allowed_tools:

        @server.tool(**_tool("delete_photo_albums"))  # type: ignore[arg-type]
        async def tool_delete_photo_albums(
            albums: Annotated[
                list[str],
                Field(
                    description=(
                        "Album IDs (list_photo_collections type=album) or unique names; a name "
                        "several albums share deletes nothing."
                    )
                ),
            ],
        ) -> str:
            from mcp_synology.modules.photos.albums import delete_photo_albums

            client = await manager.get_client()
            return await delete_photo_albums(client, albums=albums)

    if "upload_photos" in ctx.allowed_tools:

        @server.tool(**_tool("upload_photos"))  # type: ignore[arg-type]
        async def tool_upload_photos(
            local_paths: Annotated[
                list[str],
                Field(
                    description=(
                        f"1-50 local files inside {roots_for_description(local_roots)}, or below "
                        "it."
                    )
                ),
            ],
            folder: Annotated[
                str,
                Field(
                    description=("The existing folder's ID or path (default `/`, the space root).")
                ),
            ] = "/",
            space: Annotated[str, Field(description=(_SPACE))] = "personal",
            on_duplicate: Annotated[
                str,
                Field(
                    description=(
                        "A file with that name already there: `rename` (default) keeps both, "
                        "`skip` leaves the existing one and skips that file."
                    )
                ),
            ] = "rename",
            album: Annotated[
                str | None,
                Field(
                    description=(
                        "An existing album to add them to (personal space; create one with "
                        f"photo_album). {_ALBUM}"
                    )
                ),
            ] = None,
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
                budget=settings.task_timeout,
            )

    for operation in ("copy", "move"):
        name = f"{operation}_photos"
        if name not in ctx.allowed_tools:
            continue

        def _make(operation: str) -> Any:
            async def tool_copy_or_move(
                destination: Annotated[
                    str,
                    Field(
                        description=("The existing target folder's ID or path, in `dest_space`.")
                    ),
                ],
                ids: Annotated[
                    list[int] | None,
                    Field(
                        description=(
                            "Item IDs from list_photos, in `space`, at most 1,000. Pass `ids`, "
                            "`folders` or both."
                        )
                    ),
                ] = None,
                folders: Annotated[
                    list[str] | None,
                    Field(
                        description=(
                            "Folder IDs or paths, with everything in them; not the space root. "
                            "At most 1,000."
                        )
                    ),
                ] = None,
                space: Annotated[
                    str,
                    Field(
                        description=(
                            "Source space: `personal` (default) or `shared` (Shared Space)."
                        )
                    ),
                ] = "personal",
                dest_space: Annotated[
                    str | None,
                    Field(
                        description=(
                            "Space of `destination`, if not `space`: `personal` or `shared`."
                        )
                    ),
                ] = None,
                on_conflict: Annotated[
                    str,
                    Field(
                        description=(
                            "A file with that name already at the destination: `skip` (default) or "
                            "`overwrite` (replaces it)."
                        )
                    ),
                ] = "skip",
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
        async def tool_photo_task(
            handle: Annotated[
                str | None,
                Field(
                    description=(
                        "The handle a reply gave, such as `photos:14`; omit to list the tasks."
                    )
                ),
            ] = None,
            action: Annotated[
                str,
                Field(
                    description=(
                        "`status` (default; progress, or the task list), `stop` (needs `handle`; "
                        "what's done stays done) or `clear` (removes every finished task from the "
                        "list)."
                    )
                ),
            ] = "status",
        ) -> str:
            from mcp_synology.modules.photos.tasks import photo_task

            client = await manager.get_client()
            return await photo_task(client, handle=handle, action=action)

    if "delete_photos" in ctx.allowed_tools:

        @server.tool(**_tool("delete_photos"))  # type: ignore[arg-type]
        async def tool_delete_photos(
            ids: Annotated[list[int], Field(description=(_ITEM_IDS))],
            space: Annotated[str, Field(description=(_SPACE))] = "personal",
        ) -> str:
            from mcp_synology.modules.photos.tasks import delete_photos

            client = await manager.get_client()
            return await delete_photos(client, ids=ids, space=space, timeout=settings.task_timeout)

    if "delete_photo_folders" in ctx.allowed_tools:

        @server.tool(**_tool("delete_photo_folders"))  # type: ignore[arg-type]
        async def tool_delete_photo_folders(
            folders: Annotated[
                list[str],
                Field(description=(f"Not the space root; at most 1,000. {_FOLDER}")),
            ],
            space: Annotated[str, Field(description=(_SPACE))] = "personal",
        ) -> str:
            from mcp_synology.modules.photos.tasks import delete_photo_folders

            client = await manager.get_client()
            return await delete_photo_folders(
                client, folders=folders, space=space, timeout=settings.task_timeout
            )
