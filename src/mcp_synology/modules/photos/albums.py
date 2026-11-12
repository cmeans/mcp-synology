"""Photos album tools: photo_album, photo_share_link, delete_photo_albums.

Albums, share links and their settings exist only in the personal space
(``SYNO.Foto.Browse.Album``, ``NormalAlbum``, ``ConditionAlbum``, ``Sharing.*``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mcp_synology.core.errors import SynologyError
from mcp_synology.core.formatting import format_timestamp, synology_error_response
from mcp_synology.modules.photos.helpers import (
    QUOTES_HINT,
    Photos,
    Ref,
    check_ids,
    in_reply_quotes,
    invalid,
    not_found,
    one_ref,
    parse_date,
    parse_expires,
    quoted_name,
    quotes_note,
    refs,
    with_quotes_hint,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)

_ALBUM_ACTIONS = (
    "create",
    "rename",
    "add",
    "remove",
    "set_cover",
    "copy",
    "create_smart",
    "set_condition",
)
# Condition keys renamed in 0.11.0 (#615): an ID and a name or path in separate keys.
_RENAMED_CONDITION_KEYS = {
    "people": ("person_ids", "person_names"),
    "folders": ("folder_ids", "folder_paths"),
}
_MEMBER_KEYS = ("name", "type", "role")
_CONDITION_KEYS = (
    "folder_ids",
    "folder_paths",
    "person_ids",
    "person_names",
    "people_match",
    "tags",
    "tags_match",
    "places",
    "start_date",
    "end_date",
    "type",
    "ratings",
    "keywords",
    "keywords_match",
)
_MATCH = {"any": "or", "all": "and"}
_TYPE = {"all": [], "photo": [-1], "video": [-2]}


def _span(album: dict[str, Any]) -> str:
    start, end = album.get("start_time"), album.get("end_time")
    if not start and not end:
        return "no dates"
    first = format_timestamp(float(start))[:10] if start else "…"
    last = format_timestamp(float(end))[:10] if end else "…"
    return first if first == last else f"{first} – {last}"


def _shared(album: dict[str, Any]) -> str:
    info = (album.get("additional") or {}).get("sharing_info") or {}
    privacy = str(info.get("privacy_type") or "private")
    if privacy == "private" and not info.get("permission"):
        # A shared album with a private link: only invited members could use it.
        return "shared (private link)" if album.get("shared") else "not shared"
    return f"shared ({privacy})"


def album_line(album: dict[str, Any]) -> str:
    """``Album 9: "Trip 2024" · normal · 2 item(s) · 2024-07-04 – 2024-07-06 · not shared``."""
    return (
        f"Album {album['id']}: {quoted_name(album.get('name'))} · {album.get('type', 'normal')} · "
        f"{int(album.get('item_count') or 0):,} item(s) · {_span(album)} · {_shared(album)}"
    )


async def _album_now(ph: Photos, album_id: int) -> dict[str, Any]:
    data = await ph.call(
        "Browse.Album",
        "get",
        {"id": [album_id], "additional": ["sharing_info"]},
        personal=True,
    )
    listed = data.get("list", [])
    return dict(listed[0]) if listed else {"id": album_id}


async def photo_album(
    client: DsmClient,
    *,
    action: str,
    album_id: int | None = None,
    album_name: str | None = None,
    name: str | None = None,
    ids: list[int] | None = None,
    condition: dict[str, Any] | None = None,
) -> str:
    """Create or change an album (spec: photo_album)."""
    op = "Photo album"
    if action not in _ALBUM_ACTIONS:
        invalid(
            op,
            f"unknown action {action!r}.",
            param="action",
            value=action,
            valid=list(_ALBUM_ACTIONS),
        )
    album = one_ref(op, "album", album_id, album_name, required=False)
    needs_album = action not in ("create", "create_smart")
    if needs_album and album is None:
        invalid(op, f"action={action} needs album_id or album_name.", param="album_id")
    if action in ("create", "create_smart", "rename", "copy") and not (name or "").strip():
        invalid(op, f"action={action} needs name.", param="name", value=name)
    if action in ("add", "remove", "set_cover") or (action == "create" and ids):
        check_ids(op, ids or [])
    if action == "set_cover" and len(ids or []) != 1:
        invalid(op, "set_cover takes exactly one id.", param="ids", value=ids)
    if action in ("create_smart", "set_condition") and not condition:
        invalid(op, f"action={action} needs condition.", param="condition")
    try:
        ph = Photos(client, "personal", op)
        return await _photo_album(ph, action, album, (name or "").strip(), ids or [], condition)
    except SynologyError as e:
        synology_error_response(op, e)


async def _photo_album(
    ph: Photos,
    action: str,
    album: Ref | None,
    name: str,
    ids: list[int],
    condition: dict[str, Any] | None,
) -> str:
    lines: list[str] = []
    if action in ("create", "create_smart"):
        existing = await ph.walk("Browse.Album", version=2, personal=True)
        same = [a for a in existing if str(a.get("name", "")).casefold() == name.casefold()]
        if action == "create":
            found, missing = await ph.items(ids) if ids else ({}, [])
            params: dict[str, Any] = {"name": name}
            if found:
                params["item"] = list(found)
            data = await ph.call("Browse.NormalAlbum", "create", params, personal=True)
            created = data["album"]
            lines.append(
                f"Created album {quoted_name(name)} (id {created['id']}) "
                f"with {len(found):,} item(s)."
            )
            lines += _rejected(data)
            if missing:
                lines.append(ph.missing_note(missing))
        else:
            cond = await _condition(ph, condition or {})
            count = await ph.call(
                "Browse.ConditionAlbum",
                "peek_item_count",
                {"condition": cond},
                version=2,
                personal=True,
            )
            data = await ph.call(
                "Browse.ConditionAlbum",
                "create",
                {"name": name, "condition": cond},
                version=2,
                personal=True,
            )
            created = data["album"]
            lines.append(f"Created smart album {quoted_name(name)} (id {created['id']}).")
            lines.append(f"Matches {int(count.get('count', 0)):,} item(s) now.")
        lines += quotes_note(name, "album name")
        if same:
            others = ", ".join(str(a["id"]) for a in same)
            lines.append(
                f"Note: another album already has this name (id {others}); use album_id to "
                "tell them apart."
            )
        now = await _album_now(ph, int(created["id"]))
        return "\n".join([*lines, album_line(now)])

    assert album is not None  # photo_album checked it
    target = await ph.album(album)
    album_id = int(target["id"])
    kind = str(target.get("type", "normal"))
    if action in ("add", "remove", "copy") and kind != "normal":
        invalid(
            ph.operation,
            f"album {album_id} is a smart album; its items come from its condition.",
            param=album.param,
            value=album.given,
            suggestion="Change what it holds with action=set_condition.",
        )
    if action == "set_condition" and kind != "condition":
        invalid(
            ph.operation,
            f"album {album_id} isn't a smart album.",
            param=album.param,
            value=album.given,
        )

    if action == "rename":
        await ph.call("Browse.Album", "set_name", {"id": album_id, "name": name}, personal=True)
        lines.append(f"Renamed album {album_id} to {quoted_name(name)}.")
        lines += quotes_note(name, "album name", before=target.get("name"))
    elif action in ("add", "remove"):
        found, missing = await ph.items(ids)
        if found:
            method = "add_item" if action == "add" else "delete_item"
            data = await ph.call(
                "Browse.NormalAlbum",
                method,
                {"id": album_id, "item": list(found)},
                personal=True,
            )
            verb = "Added" if action == "add" else "Removed"
            where = "to" if action == "add" else "from"
            lines.append(f"{verb} {len(found):,} item(s) {where} album {album_id}.")
            if action == "remove":
                lines.append("  The photos themselves stay in their folders.")
            lines += _rejected(data)
        else:
            lines.append(f"Album {album_id} unchanged: none of the ids exist.")
        if missing:
            lines.append(ph.missing_note(missing))
    elif action == "set_cover":
        found, missing = await ph.items(ids)
        if missing:
            not_found(ph.operation, f"no item {ids[0]}.", param="ids", value=ids)
        # A scalar here, unlike Folder.set_cover (verified).
        await ph.call(
            "Browse.Album", "set_cover", {"id": album_id, "id_item": ids[0]}, personal=True
        )
        lines.append(f"Album {album_id}'s cover is now item {ids[0]}.")
    elif action == "copy":
        data = await ph.call(
            "Browse.NormalAlbum",
            "copy",
            {"name": name, "source_album_id": album_id},
            personal=True,
        )
        copied = data["album"]
        lines.append(f"Copied album {album_id} to {quoted_name(name)} (id {copied['id']}).")
        lines += quotes_note(name, "album name")
        album_id = int(copied["id"])
    else:
        cond = await _condition(ph, condition or {})
        count = await ph.call(
            "Browse.ConditionAlbum",
            "peek_item_count",
            {"condition": cond},
            version=2,
            personal=True,
        )
        await ph.call(
            "Browse.ConditionAlbum",
            "set_condition",
            {"id": album_id, "condition": cond},
            version=2,
            personal=True,
        )
        lines.append(f"Album {album_id}'s condition is replaced.")
        lines.append(f"Matches {int(count.get('count', 0)):,} item(s) now.")
    return "\n".join([*lines, album_line(await _album_now(ph, album_id))])


def _rejected(data: dict[str, Any]) -> list[str]:
    rejected = data.get("error_list") or []
    if not rejected:
        return []
    return [f"  Rejected by DSM: {', '.join(str(r) for r in rejected)} (error_list)"]


def condition_id(op: str, value: object, key: str) -> Ref:
    """One of a condition's ``person_ids`` or ``folder_ids``: an ID, as a number or its
    digits (a name or a path goes in ``person_names`` / ``folder_paths``)."""
    if type(value) is int:
        return Ref("condition", id=value)
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        return Ref("condition", id=int(value.strip()))
    other = {"person_ids": "names go in person_names", "folder_ids": "paths go in folder_paths"}
    invalid(
        op,
        f"condition {key} are IDs, not {value!r}; {other[key]}.",
        param="condition",
        value=value,
    )


async def _condition(ph: Photos, given: dict[str, Any]) -> dict[str, Any]:
    """The tool's condition (names, dates) as DSM's (IDs, Unix times, policies)."""
    op = ph.operation
    unknown = [k for k in given if k not in _CONDITION_KEYS]
    if unknown:
        # The server's own text only, as for unknown arguments (#615): a renamed key's
        # replacements, never a key the caller wrote, which could carry an instruction.
        renamed = [k for k in _RENAMED_CONDITION_KEYS if k in unknown]
        parts = [
            f"`{k}` is now `{_RENAMED_CONDITION_KEYS[k][0]}` or `{_RENAMED_CONDITION_KEYS[k][1]}`."
            for k in renamed
        ]
        if len(unknown) > len(renamed):
            parts.append(f"{len(unknown) - len(renamed)} key(s) aren't condition keys.")
        invalid(
            op,
            "condition: " + " ".join(parts),
            param="condition",
            valid=list(_CONDITION_KEYS),
        )
    useful = [
        k
        for k in given
        if not k.endswith("_match")
        and given[k] not in (None, [], "")
        and not (k == "type" and given[k] == "all")
    ]
    if not useful:
        # DSM accepts a condition with only user_id and matches everything (verified).
        invalid(
            op,
            "the condition needs at least one of folder_ids, folder_paths, person_ids, "
            "person_names, tags, places, start_date, end_date, type, ratings or keywords.",
            param="condition",
            value=given,
        )
    me = await ph.call("UserInfo", "me", personal=True)
    cond: dict[str, Any] = {"user_id": int(me["id"])}

    def as_list(key: str) -> list[Any]:
        value = given.get(key)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    def policy(key: str) -> str:
        match = given.get(f"{key}_match", "any")
        if match not in _MATCH:
            invalid(op, f"{key}_match must be any or all.", param="condition", value=match)
        return _MATCH[match]

    # Each folder by ID or by path, from its own key (#617).
    folders = [condition_id(op, f, "folder_ids") for f in as_list("folder_ids")]
    folders += [Ref("condition", name=str(f)) for f in as_list("folder_paths")]
    if folders:
        cond["folder_filter"] = [int((await ph.folder(f))["id"]) for f in folders]
    people = [condition_id(op, p, "person_ids") for p in as_list("person_ids")]
    people += [Ref("condition", name=str(p)) for p in as_list("person_names")]
    if people:
        # Each person by ID or by name, from its own key (#614, #616).
        cond["person"] = [int((await ph.person(p))["id"]) for p in people]
        cond["person_policy"] = policy("people")
    if tags := as_list("tags"):
        ids = await ph.tag_ids([str(t) for t in tags], param="condition")
        unknown_tags = [t for t, i in ids.items() if i is None]
        if unknown_tags:
            not_found(
                op,
                f"no tag {', '.join(map(repr, unknown_tags))} (a tag exists only while some "
                "item has it).",
                param="condition",
                value=unknown_tags,
                suggestion=next((QUOTES_HINT for t in unknown_tags if in_reply_quotes(t)), None),
            )
        cond["general_tag"] = [i for i in ids.values() if i is not None]
        cond["general_tag_policy"] = policy("tags")
    if places := as_list("places"):
        if not all(
            (isinstance(p, int) and not isinstance(p, bool))
            or (isinstance(p, str) and p.isascii() and p.isdigit())
            for p in places
        ):
            invalid(op, "condition places are place ids.", param="condition", value=places)
        cond["geocoding"] = [await ph.place_node(int(p), param="condition") for p in places]
    start, end = given.get("start_date"), given.get("end_date")
    if start or end:
        span: dict[str, int] = {}
        if start:
            span["start_time"] = parse_date(op, "condition.start_date", str(start))
        if end:
            span["end_time"] = parse_date(op, "condition.end_date", str(end), end=True)
        cond["time"] = [span]
    kind = given.get("type", "all")
    if not isinstance(kind, str) or kind not in _TYPE:
        invalid(op, "condition type must be all, photo or video.", param="condition", value=kind)
    cond["item_type"] = _TYPE[kind]
    if ratings := as_list("ratings"):
        if not all(isinstance(r, int) and 0 <= r <= 5 for r in ratings):
            invalid(op, "condition ratings are 0–5.", param="condition", value=ratings)
        cond["rating"] = ratings
    if keywords := as_list("keywords"):
        cond["keyword"] = [str(k) for k in keywords]
        cond["keyword_policy"] = policy("keywords")
    return cond


# -- photo_share_link -------------------------------------------------------------

_SHARE_ACTIONS = ("share", "update", "stop")
_PUBLIC = {"none": None, "view": "view", "download": "download"}
_ROLES = ("view", "download", "upload", "none")


async def photo_share_link(
    client: DsmClient,
    *,
    action: str,
    album_id: int | None = None,
    album_name: str | None = None,
    public: str | None = None,
    password: str | None = None,
    expires: str | None = None,
    members: list[dict[str, Any]] | None = None,
) -> str:
    """Share an album by link, change the link's settings, or stop (spec: photo_share_link)."""
    op = "Photo share link"
    album = one_ref(op, "album", album_id, album_name)
    assert album is not None  # required
    if action not in _SHARE_ACTIONS:
        invalid(
            op,
            f"unknown action {action!r}.",
            param="action",
            value=action,
            valid=list(_SHARE_ACTIONS),
        )
    if public is not None and public not in _PUBLIC:
        invalid(
            op, f"unknown public {public!r}.", param="public", value=public, valid=list(_PUBLIC)
        )
    if action == "stop" and any(v is not None for v in (public, password, expires, members)):
        invalid(op, "action=stop takes no settings.", param="action", value=action)
    if action == "update" and all(v is None for v in (public, password, expires)) and not members:
        invalid(
            op,
            "nothing to change: pass public, password, expires or members.",
            param="action",
            value=action,
        )
    expiration = None
    if expires is not None:
        expiration = parse_expires(op, expires)
    for i, member in enumerate(members or []):
        # Keys first, and no member's keys or other fields echoed in a refusal: the caller wrote
        # them (pre-QA r5 and QA round 1 on #615); only a name DSM doesn't know is quoted back.
        # A dropped key would leave the default: `{"name": "bob", "Role": "none"}`, meant to
        # remove bob, gave him view access.
        where = f"members[{i}]"
        if not isinstance(member, dict):
            invalid(op, f"{where} must be an object with name, type and role.", param="members")
        if any(k not in _MEMBER_KEYS for k in member):
            invalid(
                op,
                f"{where} has a key other than name, type and role; nothing was changed.",
                param="members",
                valid=list(_MEMBER_KEYS),
            )
        if not member.get("name"):
            invalid(op, f"{where} needs a name.", param="members")
        if member.get("type", "user") not in ("user", "group"):
            invalid(op, f"{where}: type must be user or group.", param="members")
        if member.get("role", "view") not in _ROLES:
            invalid(op, f"{where}: role must be view, download, upload or none.", param="members")
    try:
        ph = Photos(client, "personal", op)
        target = await ph.album(album)
        album_id = int(target["id"])
        info = (target.get("additional") or {}).get("sharing_info") or {}
        lines: list[str] = []
        if action == "stop":
            await ph.call(
                "Sharing.Passphrase",
                "set_shared",
                {"policy": "album", "album_id": album_id, "enabled": False},
                personal=True,
            )
            return (
                f"Album {album_id} is no longer shared; the link stops working (DSM keeps "
                "it, so sharing again may restore it)."
            )
        passphrase = str(info.get("passphrase") or target.get("passphrase") or "")
        is_shared = bool(target.get("shared"))
        if action == "update" and (not is_shared or not passphrase):
            invalid(
                op,
                f"album {album_id} isn't shared.",
                param="action",
                value=action,
                suggestion="Use action=share first.",
            )
        # Photos keeps a password or an expiry only on a link with public access: with
        # public "none" (members only included) it answers success and drops them, and
        # turning public access off clears them (#348). A fresh share starts with public
        # access off. Checked before set_shared, so a refused share changes nothing.
        # A share of an album that's already shared is also taken to start at none: whether
        # set_shared then keeps the old settings isn't verified, and refusing is the safe side.
        current = "none" if action == "share" else _public_of(info)
        needs_public = [
            name
            for name, given in (("password", bool(password)), ("expires", bool(expiration)))
            if given
        ]
        if needs_public and (public if public is not None else current) == "none":
            both = len(needs_public) == 2
            invalid(
                op,
                f"{' and '.join(needs_public)} need{'' if both else 's'} public access: "
                f"Photos drops {'them' if both else 'it'} on a link with public none, "
                "members only included.",
                param=needs_public[0],
                value=expires if needs_public[0] == "expires" else None,
                suggestion="Pass public=view or public=download too.",
            )
        if action == "share":
            data = await ph.call(
                "Sharing.Passphrase",
                "set_shared",
                {"policy": "album", "album_id": album_id, "enabled": True},
                personal=True,
            )
            passphrase = str(data.get("passphrase", passphrase))
        update: dict[str, Any] = {"passphrase": passphrase}
        if password is not None:
            update["password"] = password
        if expiration is not None:
            update["expiration"] = expiration
        permission = await _permission(ph, public, members or [])
        if permission:
            update["permission"] = permission
        if len(update) > 1:
            await ph.call("Sharing.Passphrase", "update", update, personal=True)
        now = await _album_now(ph, album_id)
        lines.append(_share_block(now))
        if public == "none" and current != "none":
            after = (now.get("additional") or {}).get("sharing_info") or {}
            cleared = [
                name
                for name, key in (("expiry", "expiration"), ("password", "enable_password"))
                if info.get(key) and not after.get(key)
            ]
            if cleared:
                lines.append(
                    f"  Note: turning public access off also cleared the link's "
                    f"{' and '.join(cleared)} (Photos does this); set them again together "
                    "with public."
                )
        if password and ph.client._base_url.startswith("http://"):
            lines.append(
                "  Note: this connection is plain HTTP, so the password crossed the network "
                "unencrypted."
            )
        return "\n".join(lines)
    except SynologyError as e:
        synology_error_response(op, e)


async def _permission(
    ph: Photos, public: str | None, members: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if public is not None:
        role = _PUBLIC[public]
        if role is None:
            entries.append({"action": "delete", "member": {"type": "public"}})
        else:
            entries.append({"action": "update", "role": role, "member": {"type": "public"}})
    if members:
        known = (await ph.call("Sharing.Misc", "list_user_group", personal=True)).get("list", [])
        for member in members:
            kind = member.get("type", "user")
            match = [
                k
                for k in known
                if k.get("type") == kind
                and str(k.get("name", "")).casefold() == str(member["name"]).casefold()
            ]
            if not match:
                not_found(
                    ph.operation,
                    f"no {kind} {member['name']!r}.",
                    param="members",
                    value=member["name"],
                    suggestion=with_quotes_hint(None, member["name"]),
                )
            ref = {"type": kind, "id": int(match[0]["id"])}
            role = member.get("role", "view")
            if role == "none":
                entries.append({"action": "delete", "member": ref})
            else:
                entries.append({"action": "update", "role": role, "member": ref})
    return entries


def _public_of(info: dict[str, Any]) -> str:
    """The link's public access as the tool names it (``none``, ``view``, ``download``)."""
    privacy = str(info.get("privacy_type") or "private")
    return {"private": "none", "public-view": "view", "public-download": "download"}.get(
        privacy, privacy
    )


def _share_block(album: dict[str, Any]) -> str:
    info = (album.get("additional") or {}).get("sharing_info") or {}
    public = _public_of(info)
    expiration = int(info.get("expiration") or 0)
    expires = "expires never" if not expiration else f"expires {format_timestamp(expiration)}"
    if info.get("is_expired"):
        expires += " (expired)"
    members = []
    for entry in info.get("permission") or []:
        member = entry.get("member") or entry
        label = quoted_name(
            member.get("name"), f"{member.get('type', 'member')} {member.get('id', '')}"
        )
        members.append(f"{label} ({entry.get('role', '?')})")
    head = f"Album {album['id']} {quoted_name(album.get('name'))} is " + (
        "shared." if album.get("shared") else "not shared."
    )
    return "\n".join(
        [
            head,
            f"  Link:      {info.get('sharing_link') or '—'}",
            f"  Public:    {public} · password: {'yes' if info.get('enable_password') else 'no'}"
            f" · {expires}",
            f"  Members:   {', '.join(members) or 'none'}",
        ]
    )


# -- delete_photo_albums ------------------------------------------------------------


async def delete_photo_albums(
    client: DsmClient,
    *,
    album_ids: list[int] | None = None,
    album_names: list[str] | None = None,
) -> str:
    """Delete albums; their photos stay (spec: delete_photo_albums)."""
    op = "Delete photo albums"
    wanted = refs(op, album_ids, album_names, prefix="album")
    try:
        ph = Photos(client, "personal", op)
        found: dict[int, str] = {}
        missing: list[str] = []
        quoted_missing = False  # a name not found that's in a reply's quotes (QA r1 on #709)
        existing = await ph.walk("Browse.Album", version=2, personal=True)
        by_id = {int(a["id"]): a for a in existing}
        for ref in wanted:
            # An ID is that ID only and a name that name only (#614, #616).
            if ref.id is not None:
                matches = [by_id[ref.id]] if ref.id in by_id else []
            else:
                name = (ref.name or "").strip().casefold()
                matches = [a for a in existing if str(a.get("name", "")).casefold() == name]
            if not matches:
                missing.append(f"ID {ref.id}" if ref.id is not None else f"named {ref.name!r}")
                quoted_missing = quoted_missing or (
                    ref.id is None and in_reply_quotes((ref.name or "").strip())
                )
            elif len(matches) > 1:
                listed = ", ".join(str(m["id"]) for m in matches)
                invalid(
                    op,
                    f"{len(matches):,} albums are named {ref.name!r} (IDs {listed}); "
                    "nothing was deleted.",
                    param=ref.param,
                    value=ref.name,
                    suggestion="Ask the user which one they mean, then pass its album_ids.",
                )
            else:
                found[int(matches[0]["id"])] = str(matches[0].get("name", ""))
        if found:
            # DSM answers success for album IDs that don't exist (verified), hence the check.
            await ph.call("Browse.Album", "delete", {"id": list(found)}, personal=True)
        lines = []
        if found:
            named = ", ".join(f"{i} {quoted_name(n)}" for i, n in found.items())
            lines.append(
                f"Deleted {len(found):,} album(s): {named}; their photos stay where they are."
            )
        else:
            lines.append("No albums deleted.")
        if missing:
            hint = f" ({QUOTES_HINT})" if quoted_missing else ""
            lines.append(f"  Not found: {', '.join(missing)}{hint}")
        return "\n".join(lines)
    except SynologyError as e:
        synology_error_response(op, e)
