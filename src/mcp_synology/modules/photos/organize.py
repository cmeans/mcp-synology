"""Photos organizing tools: photo_folder, tag_photos, edit_photo, edit_person,
merge_people, photo_request."""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

from mcp_synology.core.errors import SynologyError
from mcp_synology.core.formatting import format_size, format_table, synology_error_response
from mcp_synology.modules.photos.helpers import (
    V7,
    Photos,
    batches,
    check_ids,
    check_space,
    invalid,
    not_found,
    parse_date,
    space_label,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)

_REWRITE_NOTE = "Photos rewrites {what} files, so their dates and sizes change."


# -- photo_folder -----------------------------------------------------------------

_FOLDER_ACTIONS = ("create", "rename", "set_cover")


async def photo_folder(
    client: DsmClient,
    *,
    action: str,
    folder: str | None = None,
    parent: str = "/",
    name: str | None = None,
    item_id: int | None = None,
    space: str = "personal",
) -> str:
    """Create or rename a folder, or set its cover (spec: photo_folder)."""
    op = "Photo folder"
    sp = check_space(op, space)
    if action not in _FOLDER_ACTIONS:
        invalid(
            op,
            f"unknown action {action!r}.",
            param="action",
            value=action,
            valid=list(_FOLDER_ACTIONS),
        )
    leaf = (name or "").strip()
    if action in ("create", "rename"):
        if not leaf:
            invalid(op, f"action={action} needs name.", param="name", value=name)
        if "/" in leaf or leaf in (".", ".."):
            invalid(op, "name is one folder name, without /.", param="name", value=name)
    if action in ("rename", "set_cover") and folder is None:
        invalid(op, f"action={action} needs folder.", param="folder")
    if action == "set_cover" and item_id is None:
        invalid(op, "action=set_cover needs item_id.", param="item_id")
    try:
        ph = Photos(client, sp, op)
        if action == "create":
            under = await ph.folder(parent, param="parent")
            data = await ph.call(
                "Browse.Folder", "create", {"target_id": int(under["id"]), "name": leaf}
            )
            made = data["folder"]
            return f"Created {made.get('name')} (id {made['id']}, {space_label(sp)})."
        target = await ph.folder(folder or "")
        folder_id = int(target["id"])
        if action == "rename":
            if target.get("parent") == folder_id or target.get("name") == "/":
                invalid(
                    op, "the space's root folder can't be renamed.", param="folder", value=folder
                )
            data = await ph.call("Browse.Folder", "rename", {"id": folder_id, "name": leaf})
            return f"Renamed folder {folder_id} to {data['folder'].get('name')}."
        found, _missing = await ph.items([int(item_id or 0)])
        if not found:
            not_found(
                op, f"no item {item_id} in the {space_label(sp)}.", param="item_id", value=item_id
            )
        await ph.call(
            "Browse.Folder", "set_cover", {"id": folder_id, "id_item": [item_id]}, version=2
        )
        return f"Folder {folder_id}'s cover is now item {item_id}."
    except SynologyError as e:
        synology_error_response(op, e)


# -- tag_photos -------------------------------------------------------------------


async def tag_photos(
    client: DsmClient, *, action: str, ids: list[int], tags: list[str], space: str = "personal"
) -> str:
    """Add or remove tags by name (spec: tag_photos)."""
    op = "Tag photos"
    sp = check_space(op, space)
    if action not in ("add", "remove"):
        invalid(
            op, f"unknown action {action!r}.", param="action", value=action, valid=["add", "remove"]
        )
    check_ids(op, ids)
    names = list(dict.fromkeys(t.strip() for t in tags if t and t.strip()))
    if not names:
        invalid(op, "pass at least one tag name.", param="tags", value=tags)
    try:
        ph = Photos(client, sp, op)
        found, missing = await ph.items(ids)
        lines: list[str] = []
        if found:
            # Resolved now, never cached: 1.9.1 renumbers tags on re-index (spec).
            resolved = await ph.tag_ids(names, param="tags")
            if action == "add":
                for tag, tag_id in resolved.items():
                    if tag_id is None:
                        # Idempotent by name: returns the existing tag's ID (verified).
                        made = await ph.call("Browse.GeneralTag", "create", {"name": tag})
                        resolved[tag] = int(made["tag"]["id"])
            tag_ids = [i for i in resolved.values() if i is not None]
            unknown = [t for t, i in resolved.items() if i is None]
            rejected: list[Any] = []
            if tag_ids:
                method = "add_tag" if action == "add" else "remove_tag"
                # One call per batch carries every tag: several add_tag calls in quick
                # succession on one item overwrite each other (#248).
                for batch in batches(list(found)):
                    data = await ph.call("Browse.Item", method, {"id": batch, "tag": tag_ids})
                    rejected += data.get("error_list") or []
            shown = ", ".join(t for t in names if t not in unknown)
            if tag_ids:
                verb = "Added tags" if action == "add" else "Removed tags"
                where = "to" if action == "add" else "from"
                lines.append(f"{verb} {shown} {where} {len(found)} item(s) ({space_label(sp)}).")
            if unknown:
                lines.append(
                    f"  No item has the tag(s) {', '.join(unknown)}, so there was nothing to "
                    "remove."
                )
            if rejected:
                lines.append(
                    f"  Rejected by DSM: {', '.join(map(str, rejected))} (error_list; e.g. "
                    "still being indexed)"
                )
        else:
            lines.append("No items changed: none of the ids exist.")
        if missing:
            lines.append(ph.missing_note(missing))
        if found and len(lines) and lines[0].startswith(("Added", "Removed")):
            lines.append(_REWRITE_NOTE.format(what="tagged"))
        return "\n".join(lines)
    except SynologyError as e:
        synology_error_response(op, e)


# -- edit_photo -------------------------------------------------------------------

_ROTATE = {"left": "counter_clockwise", "right": "clockwise"}


def _taken_time(op: str, value: str) -> int:
    text = value.strip().replace("Z", "+00:00")
    try:
        when = datetime.datetime.fromisoformat(text)
    except ValueError:
        invalid(
            op,
            f"taken_time must be an ISO 8601 date-time, not {value!r}.",
            param="taken_time",
            value=value,
        )
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.UTC)
    return int(when.timestamp())


async def edit_photo(
    client: DsmClient,
    *,
    ids: list[int],
    space: str = "personal",
    rating: int | None = None,
    description: str | None = None,
    taken_time: str | None = None,
    shift_hours: float | None = None,
    rotate: str | None = None,
    favorite: bool | None = None,
) -> str:
    """Edit rating, description, taken time, rotation or favorite (spec: edit_photo)."""
    op = "Edit photo"
    sp = check_space(op, space)
    check_ids(op, ids)
    if rating is not None and not 0 <= rating <= 5:
        invalid(op, "rating must be 0–5 (0 clears it).", param="rating", value=rating)
    if taken_time is not None and shift_hours is not None:
        invalid(op, "pass taken_time or shift_hours, not both.", param="shift_hours")
    if rotate is not None and rotate not in _ROTATE:
        invalid(
            op, f"unknown rotate {rotate!r}.", param="rotate", value=rotate, valid=list(_ROTATE)
        )
    fields: list[tuple[str, str, Any]] = []
    if rating is not None:
        fields.append(("rating", f"rating {rating}", rating))
    if description is not None:
        fields.append(("description", "description", description))
    if taken_time is not None:
        fields.append(("time", "taken time", _taken_time(op, taken_time)))
    if shift_hours is not None:
        fields.append(
            ("shift_time", f"taken time shifted {shift_hours:+g} h", round(shift_hours * 3600))
        )
    if rotate is not None:
        fields.append(("rotate_action", f"rotated {rotate}", _ROTATE[rotate]))
    if not fields and favorite is None:
        invalid(
            op,
            "nothing to change: pass rating, description, taken_time, shift_hours, "
            "rotate or favorite.",
            param="ids",
        )
    try:
        ph = Photos(client, sp, op)
        if favorite is not None:
            ph.require_v7("favorite", param="favorite")
        found, missing = await ph.items(ids)
        lines: list[str] = []
        rejected: set[Any] = set()
        done: list[str] = []
        if found:
            # Every field in one set (verified on both versions), so each file is rewritten
            # once. The web client sends one set per field; but a taken-time change that
            # follows another edit within seconds can be undone by Photos' re-index of the
            # earlier rewrite (seen on 7.2.2), and one set leaves nothing to follow.
            if fields:
                values = {key: value for key, _label, value in fields}
                for batch in batches(list(found)):
                    data = await ph.call("Browse.Item", "set", {"id": batch, **values}, version=2)
                    rejected.update(data.get("error_list") or [])
                done += [label for _key, label, _value in fields]
            if favorite is not None:
                for batch in batches(list(found)):
                    await ph.call(
                        "Browse.Item",
                        "set_favorite",
                        {"id": batch, "favorite": favorite},
                        version=V7,
                    )
                done.append("favorite" if favorite else "not favorite")
            lines.append(f"Edited {len(found)} item(s) ({space_label(sp)}): {', '.join(done)}.")
            if rejected:
                lines.append(
                    f"  Rejected by DSM: {', '.join(map(str, sorted(rejected, key=str)))}"
                    " (error_list)"
                )
        else:
            lines.append("No items changed: none of the ids exist.")
        if missing:
            lines.append(ph.missing_note(missing))
        if found and fields:
            lines.append(_REWRITE_NOTE.format(what="edited"))
            if taken_time is not None or shift_hours is not None:
                lines.append(
                    "Photos re-indexes the files over the next seconds; another taken-time "
                    "change to them before then can be undone, so wait before changing it again."
                )
        return "\n".join(lines)
    except SynologyError as e:
        synology_error_response(op, e)


# -- edit_person / merge_people ---------------------------------------------------

_PERSON_ACTIONS = ("rename", "hide", "unhide", "set_cover")


def person_line(person: dict[str, Any]) -> str:
    """``Person 12: Jo (70 items, visible, cover: item 26).``"""
    cover = ""
    key = str(((person.get("additional") or {}).get("thumbnail") or {}).get("cache_key", ""))
    if key.split("_")[0].isdigit():
        cover = f", cover: item {key.split('_')[0]}"
    shown = "visible" if person.get("show", True) else "hidden"
    return (
        f"Person {person['id']}: {person.get('name') or '(unnamed)'} "
        f"({person.get('item_count', 0)} items, {shown}{cover})."
    )


async def _person_now(ph: Photos, person_id: int) -> dict[str, Any]:
    data = await ph.call("Browse.Person", "get", {"id": [person_id], "additional": ["thumbnail"]})
    listed = data.get("list", [])
    return dict(listed[0]) if listed else {"id": person_id}


async def edit_person(
    client: DsmClient,
    *,
    action: str,
    person: str,
    name: str | None = None,
    item_id: int | None = None,
    space: str = "personal",
) -> str:
    """Rename, hide, unhide or set a person's cover (spec: edit_person)."""
    op = "Edit person"
    sp = check_space(op, space)
    if action not in _PERSON_ACTIONS:
        invalid(
            op,
            f"unknown action {action!r}.",
            param="action",
            value=action,
            valid=list(_PERSON_ACTIONS),
        )
    if action == "rename" and name is None:
        invalid(op, "action=rename needs name (an empty name clears it).", param="name")
    if action == "set_cover" and item_id is None:
        invalid(op, "action=set_cover needs item_id.", param="item_id")
    try:
        ph = Photos(client, sp, op)
        target = await ph.person(person)
        person_id = int(target["id"])
        if action == "rename":
            await ph.call("Browse.Person", "set", {"id": person_id, "name": name})
        elif action in ("hide", "unhide"):
            await ph.call("Browse.Person", "show", {"id": [person_id], "show": action == "unhide"})
        else:
            # photo_id is an item of this person; DSM picks that item's face (verified).
            await ph.call("Browse.Person", "set_cover", {"id": person_id, "photo_id": item_id})
        return person_line(await _person_now(ph, person_id))
    except SynologyError as e:
        synology_error_response(op, e)


async def merge_people(
    client: DsmClient,
    *,
    target: str,
    merge: list[str],
    name: str | None = None,
    space: str = "personal",
) -> str:
    """Merge people into one (spec: merge_people). Every ID is checked first: DSM
    ignores a missing ``merged_id`` and reports success."""
    op = "Merge people"
    sp = check_space(op, space)
    if not merge:
        invalid(op, "pass at least one person in merge.", param="merge", value=merge)
    try:
        ph = Photos(client, sp, op)
        keep = await ph.person(target, param="target")
        keep_id = int(keep["id"])
        merged: dict[int, dict[str, Any]] = {}
        for value in merge:
            found = await ph.person(str(value), param="merge")
            if int(found["id"]) == keep_id:
                invalid(
                    op,
                    f"person {keep_id} is the target; it can't be merged into itself.",
                    param="merge",
                    value=value,
                )
            merged[int(found["id"])] = found
        final = name if name is not None else str(keep.get("name") or "")
        await ph.call(
            "Browse.Person",
            "merge",
            {"target_id": keep_id, "merged_id": list(merged), "name": final},
            version=2,
        )
        now = await _person_now(ph, keep_id)
        return (
            f"Merged {len(merged)} people into person {keep_id} "
            f'"{now.get("name") or "(unnamed)"}" (now {now.get("item_count", "?")} items).'
        )
    except SynologyError as e:
        synology_error_response(op, e)


# -- photo_request ------------------------------------------------------------------

_REQUEST_ACTIONS = ("create", "list", "update", "delete")
_LIBRARY = {"personal": "personal_space", "shared": "shared_space"}


def _limit(value: int) -> str:
    return "no size limit" if not value else f"{format_size(int(value))} per file"


def _expiry(value: int) -> str:
    if not value:
        return "expires never"
    from mcp_synology.core.formatting import format_timestamp

    return f"expires {format_timestamp(float(value))}"


def _request_block(req: dict[str, Any]) -> str:
    space = "Shared Space" if req.get("library") == "shared_space" else "personal space"
    lines = [
        f'Photo request "{req.get("subject", "")}" → {req.get("folder_home_path", "")} ({space})',
        f"  Link:      {req.get('sharing_link', '—')}",
        f"  Limit:     {_limit(int(req.get('filesize_limit') or 0))} · "
        f"{_expiry(int(req.get('expiration') or 0))}",
    ]
    if req.get("description"):
        lines.append(f"  Note:      {req['description']}")
    if req.get("is_folder_valid") is False:
        lines.append("  Warning:   its folder no longer exists, so uploads fail.")
    return "\n".join(lines)


async def photo_request(
    client: DsmClient,
    *,
    action: str,
    request: str | None = None,
    subject: str | None = None,
    description: str | None = None,
    folder: str | None = None,
    space: str = "personal",
    max_file_mib: int | None = None,
    expires: str | None = None,
) -> str:
    """Create, list, update or delete photo requests (spec: photo_request)."""
    op = "Photo request"
    sp = check_space(op, space)
    if action not in _REQUEST_ACTIONS:
        invalid(
            op,
            f"unknown action {action!r}.",
            param="action",
            value=action,
            valid=list(_REQUEST_ACTIONS),
        )
    if action == "create":
        if not (subject or "").strip():
            invalid(op, "action=create needs subject.", param="subject", value=subject)
        if not folder:
            invalid(op, "action=create needs folder.", param="folder")
    if action in ("update", "delete") and not request:
        invalid(
            op, f"action={action} needs request (its passphrase from action=list).", param="request"
        )
    if max_file_mib is not None and max_file_mib < 0:
        invalid(op, "max_file_mib can't be negative.", param="max_file_mib", value=max_file_mib)
    expiration = None
    if expires is not None:
        expiration = 0 if expires == "never" else parse_date(op, "expires", expires, end=True)
    try:
        ph = Photos(client, "personal", op)
        if action == "list":
            entries = await ph.walk("PhotoRequest", personal=True)
            rows = [
                [
                    str(e.get("passphrase", "")),
                    str(e.get("subject", "")),
                    str(e.get("folder_home_path", ""))
                    + (" (shared)" if e.get("library") == "shared_space" else ""),
                    _limit(int(e.get("filesize_limit") or 0)),
                    _expiry(int(e.get("expiration") or 0)).removeprefix("expires "),
                    str(e.get("sharing_link", "")),
                ]
                for e in entries
            ]
            return format_table(
                ["Request", "Subject", "Folder", "Size limit", "Expires", "Link"],
                rows,
                title=f"Photo requests ({len(rows)})",
            )
        existing = {
            str(e.get("passphrase")): e for e in await ph.walk("PhotoRequest", personal=True)
        }
        if action in ("update", "delete") and request not in existing:
            not_found(
                op,
                f"no photo request {request!r}.",
                param="request",
                value=request,
                suggestion="action=list shows the requests and their passphrases.",
            )
        if action == "delete":
            await ph.call("PhotoRequest", "delete", {"passphrase": [request]}, personal=True)
            req = existing[str(request)]
            return (
                f'Deleted photo request "{req.get("subject", "")}" ({request}); its link stops '
                "working."
            )
        fields: dict[str, Any] = {}
        if subject is not None:
            fields["subject"] = subject
        if description is not None:
            fields["description"] = description
        if folder is not None:
            path = "/" + folder.strip().strip("/")
            # Checked here, so a typo isn't a request into a folder that doesn't exist.
            await Photos(client, sp, op).folder(path)
            fields["library"] = _LIBRARY[sp]
            fields["folder_home_path"] = path
        if max_file_mib is not None:
            fields["filesize_limit"] = max_file_mib * 1024 * 1024
        if expiration is not None:
            fields["expiration"] = expiration
        if action == "create":
            fields.setdefault("filesize_limit", 0)
            fields.setdefault("expiration", 0)
            made = await ph.call(
                "PhotoRequest", "create", {"passphrase": "", **fields}, personal=True
            )
            return _request_block(made)
        if not fields:
            invalid(op, "nothing to change.", param="action", value=action)
        await ph.call("PhotoRequest", "update", {"passphrase": request, **fields}, personal=True)
        now = {str(e.get("passphrase")): e for e in await ph.walk("PhotoRequest", personal=True)}
        return _request_block(now.get(str(request), {**existing[str(request)], **fields}))
    except SynologyError as e:
        synology_error_response(op, e)
