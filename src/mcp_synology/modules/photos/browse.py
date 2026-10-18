"""Photos READ tools: list_photos, list_photo_collections, get_photo."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mcp_synology.core.errors import SynologyError
from mcp_synology.core.formatting import (
    format_key_value,
    format_size,
    format_table,
    synology_error_response,
)
from mcp_synology.modules.photos.helpers import (
    V7,
    Photos,
    check_space,
    invalid,
    is_indexing,
    page_footer,
    parse_date,
    space_label,
    taken,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)

_SORTS = {"taken": "takentime", "filename": "filename", "size": "filesize", "type": "item_type"}
_DIRECTIONS = ("asc", "desc")
_TYPES: dict[str, list[int] | None] = {"all": None, "photo": [-1], "video": [-2]}
_MAX_LIMIT = 500
# resolution shows whether an item is still being indexed (0×0 until it is).
_ROW_ADDITIONAL = ["resolution"]


def _check_paging(operation: str, offset: int, limit: int, max_limit: int = _MAX_LIMIT) -> None:
    if offset < 0:
        invalid(operation, "offset can't be negative.", param="offset", value=offset)
    if not 1 <= limit <= max_limit:
        invalid(operation, f"limit must be 1–{max_limit}.", param="limit", value=limit)


def _order(sort_by: str, direction: str) -> str:
    if sort_by == "taken":
        return "newest first" if direction == "desc" else "oldest first"
    return f"by {sort_by}, {'descending' if direction == 'desc' else 'ascending'}"


async def _rows(ph: Photos, items: list[dict[str, Any]]) -> list[list[str]]:
    rows = []
    for item in items:
        when = taken(item) + (" (indexing)" if is_indexing(item) else "")
        folder_id = item.get("folder_id")
        folder = await ph.folder_path(int(folder_id)) if folder_id is not None else "—"
        rows.append(
            [
                str(item.get("id", "")),
                str(item.get("filename", "")),
                when,
                str(item.get("type", "")),
                format_size(int(item.get("filesize") or 0)),
                folder,
            ]
        )
    return rows


async def list_photos(
    client: DsmClient,
    *,
    space: str = "personal",
    folder: str | None = None,
    album: str | None = None,
    person: str | None = None,
    place: int | None = None,
    tag: str | None = None,
    keyword: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    type: str = "all",  # noqa: A002 - the tool's parameter name
    min_rating: int | None = None,
    favorite: bool | None = None,
    concept: int | None = None,
    sort_by: str = "taken",
    sort_direction: str = "desc",
    offset: int = 0,
    limit: int = 50,
) -> str:
    """Browse or search items (spec: list_photos)."""
    op = "List photos"
    sp = check_space(op, space)
    if type not in _TYPES:
        invalid(op, f"unknown type {type!r}.", param="type", value=type, valid=list(_TYPES))
    if sort_by not in _SORTS:
        invalid(
            op, f"unknown sort_by {sort_by!r}.", param="sort_by", value=sort_by, valid=list(_SORTS)
        )
    if sort_direction not in _DIRECTIONS:
        invalid(
            op,
            f"unknown sort_direction {sort_direction!r}.",
            param="sort_direction",
            value=sort_direction,
            valid=list(_DIRECTIONS),
        )
    _check_paging(op, offset, limit)
    if min_rating is not None and not 1 <= min_rating <= 5:
        invalid(op, "min_rating must be 1–5.", param="min_rating", value=min_rating)
    start = parse_date(op, "start_date", start_date) if start_date else None
    end = parse_date(op, "end_date", end_date, end=True) if end_date else None
    if start is not None and end is not None and start > end:
        invalid(op, "start_date is after end_date.", param="start_date", value=start_date)
    want_favorite = favorite is True
    filters = {
        "start_date": start_date,
        "end_date": end_date,
        "type": None if type == "all" else type,
        "min_rating": min_rating,
        "favorite": True if want_favorite else None,
    }
    used_filters = [k for k, v in filters.items() if v is not None]
    sources = {"folder": folder, "person": person, "tag": tag, "place": place}
    used_sources = [k for k, v in sources.items() if v is not None]
    if keyword is not None:
        others = used_sources + used_filters
        others += [k for k, v in {"album": album, "concept": concept}.items() if v is not None]
        if others:
            invalid(
                op,
                f"keyword can't be combined with {', '.join(others)}.",
                param="keyword",
                value=keyword,
            )
    # Views that list_with_filter can't narrow: it has no album or concept key, and its
    # place IDs are address-tree nodes, not Geocoding.list's place IDs (verified, #258).
    alone_views = {"album": album, "concept": concept, "place": place}
    for alone, value in alone_views.items():
        if value is None:
            continue
        others = [k for k in used_sources + used_filters if k != alone]
        others += [k for k, v in alone_views.items() if v is not None and k != alone]
        if others:
            invalid(
                op,
                f"{alone} can't be combined with {', '.join(sorted(set(others)))}.",
                param=alone,
                value=value,
            )
    if album is not None and sp == "shared":
        invalid(op, "albums are personal space only.", param="space", value=space)
    try:
        ph = Photos(client, sp, op)
        if want_favorite:
            ph.require_v7("The favorite filter", param="favorite")
        if concept is not None:
            ph.require_v7("concept", param="concept")
        return await _list(
            ph,
            folder=folder,
            album=album,
            person=person,
            place=place,
            tag=tag,
            keyword=keyword,
            start=start,
            end=end,
            item_type=_TYPES[type],
            min_rating=min_rating,
            favorite=want_favorite,
            concept=concept,
            filters_text=_filters_text(start_date, end_date, type, min_rating, want_favorite),
            sort=(sort_by, sort_direction),
            offset=offset,
            limit=limit,
        )
    except SynologyError as e:
        synology_error_response(op, e)


def _filters_text(
    start_date: str | None, end_date: str | None, type_: str, min_rating: int | None, fav: bool
) -> list[str]:
    out = []
    if start_date or end_date:
        out.append(f"{start_date or '…'} to {end_date or '…'}")
    if type_ != "all":
        out.append(f"{type_}s only")
    if min_rating:
        out.append(f"rated {min_rating}+")
    if fav:
        out.append("favorites")
    return out


async def _list(
    ph: Photos,
    *,
    folder: str | None,
    album: str | None,
    person: str | None,
    place: int | None,
    tag: str | None,
    keyword: str | None,
    start: int | None,
    end: int | None,
    item_type: list[int] | None,
    min_rating: int | None,
    favorite: bool,
    concept: int | None,
    filters_text: list[str],
    sort: tuple[str, str],
    offset: int,
    limit: int,
) -> str:
    sort_by, direction = sort
    page = {"offset": offset, "limit": limit}
    order = {"sort_by": _SORTS[sort_by], "sort_direction": direction}
    described: list[str] = []
    total: int | None

    if keyword is not None:
        data = await ph.call("Search.Search", "list_item", {"keyword": keyword, **page})
        items = data.get("list", [])
        total = int((await ph.call("Search.Search", "count_item", {"keyword": keyword}))["count"])
        return await _render(ph, f'search "{keyword}"', [], "search order", items, total, offset)

    if album is not None:
        found = await ph.album(album)
        params = {"album_id": int(found["id"]), **page, **order, "additional": _ROW_ADDITIONAL}
        items = (await ph.call("Browse.Item", "list", params, version=2)).get("list", [])
        total = int(found.get("item_count", 0))
        source = f'album "{found.get("name", "")}" ({found["id"]})'
        return await _render(ph, source, [], _order(*sort), items, total, offset)

    if concept is not None:
        params = {"concept_id": concept, **page, **order, "additional": _ROW_ADDITIONAL}
        items = (await ph.call("Browse.Item", "list", params, version=V7)).get("list", [])
        info = await ph.call("Browse.Concept", "get", {"id": [concept]}, version=2)
        listed = info.get("list", [])
        # Concept.get's reply shape isn't captured yet (spec open item 1): without it,
        # no total rather than the page length posing as one.
        total = int(listed[0].get("item_count", 0)) if listed else None
        name = listed[0].get("name", "") if listed else ""
        return await _render(
            ph, f"concept {name or concept}".strip(), [], _order(*sort), items, total, offset
        )

    if place is not None:
        # Item.list and Item.count take Geocoding.list's place IDs (verified on 7.1).
        params = {"geocoding_id": place, **page, **order, "additional": _ROW_ADDITIONAL}
        items = (await ph.call("Browse.Item", "list", params, version=2)).get("list", [])
        total = int((await ph.call("Browse.Item", "count", {"geocoding_id": place}))["count"])
        return await _render(ph, f"place {place}", [], _order(*sort), items, total, offset)

    # Resolve the sources to IDs.
    keys: dict[str, Any] = {}
    folder_id: int | None = None
    if folder is not None:
        f = await ph.folder(folder)
        folder_id = int(f["id"])
        keys["folder"] = [folder_id]
        described.append(str(f.get("name", "")) or "/")
    if person is not None:
        p = await ph.person(person)
        # count_with_filter answers 120 without the policy (verified on both versions).
        keys["person"] = [int(p["id"])]
        keys["person_policy"] = "or"
        described.append(f"person {p.get('name') or '(unnamed)'} ({p['id']})")
    if tag is not None:
        tag_id = (await ph.tag_ids([tag]))[tag]
        described.append(f'tag "{tag}"')
        if tag_id is None:
            heading = _heading(ph, described, filters_text, _order(*sort))
            return (
                f"{heading}\nNo items: no item has the tag {tag!r} (a tag exists only while "
                "some item has it)."
            )
        keys["general_tag"] = [tag_id]
        keys["general_tag_policy"] = "or"

    if folder_id is not None and len(keys) == 1 and not filters_text:
        # One folder: Item.list, as the web client's folder view.
        params = {"folder_id": folder_id, **page, **order, "additional": _ROW_ADDITIONAL}
        items = (await ph.call("Browse.Item", "list", params, version=2)).get("list", [])
        total = int((await ph.call("Browse.Item", "count", {"folder_id": folder_id}))["count"])
        return await _render(ph, None, described, _order(*sort), items, total, offset)

    # Everything else: list_with_filter, which (unlike Item.list outside a folder view)
    # honors sort_by. With no keys at all it lists the whole space.
    if start is not None or end is not None:
        span: dict[str, int] = {}
        if start is not None:
            span["start_time"] = start
        if end is not None:
            span["end_time"] = end
        keys["time"] = [span]
    if item_type is not None:
        keys["item_type"] = item_type
    if min_rating is not None:
        keys["rating"] = list(range(min_rating, 6))
    if favorite:
        keys["favorite"] = [True]
    version = V7 if favorite else 2
    params = {**keys, **page, **order, "additional": _ROW_ADDITIONAL}
    items = (await ph.call("Browse.Item", "list_with_filter", params, version=version)).get(
        "list", []
    )
    counted = await ph.call("Browse.Item", "count_with_filter", keys, version=version)
    total = int(counted.get("count", 0))
    return await _render(ph, None, described, _order(*sort), items, total, offset, filters_text)


def _heading(ph: Photos, described: list[str], filters: list[str], order: str) -> str:
    """``Photos in /Trips/2024 (personal space; newest first)``, as the spec shows."""
    if not described:
        lead = "All photos and videos"
    elif described[0].startswith("/"):
        lead = f"Photos in {', '.join(described)}"
    else:
        lead = f"Photos: {', '.join(described)}"
    title = f"{lead} ({'; '.join([space_label(ph.space), *filters, order])})"
    return f"{title}\n{'=' * min(len(title), 78)}"


async def _render(
    ph: Photos,
    source: str | None,
    described: list[str],
    order: str,
    items: list[dict[str, Any]],
    total: int | None,
    offset: int,
    filters: list[str] | None = None,
) -> str:
    heading = _heading(ph, [source] if source else described, filters or [], order)
    rows = await _rows(ph, items)
    if not rows:
        return f"{heading}\nNo items to display.\n\n{page_footer(total, offset, 0)}"
    table = format_table(["ID", "Filename", "Taken", "Type", "Size", "Folder"], rows)
    return f"{heading}\n{table}\n\n{page_footer(total, offset, len(rows))}"


# -- list_photo_collections ------------------------------------------------------

_COLLECTION_TYPES = ("album", "shared_with_me", "person", "tag", "place", "concept", "folder")
_COLLECTION_MAX_LIMIT = 1000


async def list_photo_collections(
    client: DsmClient,
    *,
    type: str,  # noqa: A002 - the tool's parameter name
    space: str = "personal",
    name: str | None = None,
    parent: str | None = None,
    include_hidden: bool = False,
    offset: int = 0,
    limit: int = 100,
) -> str:
    """Albums, people, tags, places, concepts and folders (spec: list_photo_collections)."""
    op = "List photo collections"
    if type not in _COLLECTION_TYPES:
        invalid(
            op, f"unknown type {type!r}.", param="type", value=type, valid=list(_COLLECTION_TYPES)
        )
    sp = check_space(op, space)
    _check_paging(op, offset, limit, _COLLECTION_MAX_LIMIT)
    if parent is not None and type != "folder":
        invalid(op, "parent applies to type=folder only.", param="parent", value=parent)
    try:
        ph = Photos(client, "personal" if type in ("album", "shared_with_me") else sp, op)
        if type == "concept" and not ph.has("Browse.Concept", 2):
            from mcp_synology.modules.photos.helpers import not_supported

            not_supported(op, "type=concept", param="type")
        return await _collections(ph, type, name, parent, include_hidden, offset, limit)
    except SynologyError as e:
        synology_error_response(op, e)


async def _collections(
    ph: Photos,
    kind: str,
    name: str | None,
    parent: str | None,
    include_hidden: bool,
    offset: int,
    limit: int,
) -> str:
    title_space = "" if kind in ("album", "shared_with_me") else f" ({space_label(ph.space)})"
    heading: str
    headers: list[str]
    entries: list[dict[str, Any]]
    if kind == "album":
        entries = await ph.walk(
            "Browse.Album",
            params={
                "sort_by": "create_time",
                "sort_direction": "desc",
                "additional": ["sharing_info"],
            },
            version=2,
            personal=True,
        )
        heading, headers = "Albums", ["ID", "Name", "Type", "Items", "Shared", "Dates"]
        rows = [_album_row(e) for e in entries]
    elif kind == "shared_with_me":
        entries = await ph.walk(
            "Sharing.Misc",
            "list_shared_with_me_album",
            params={
                "sort_by": "share_modify_time",
                "sort_direction": "desc",
                "additional": ["sharing_info"],
            },
            version=2,
            personal=True,
        )
        heading = "Albums shared with you"
        headers = ["ID", "Name", "Owner", "Items", "Your access", "Dates"]
        rows = [_shared_row(e) for e in entries]
    elif kind == "person":
        if ph.space == "shared":
            # Shared Space access first (801 says so), then whether People is on there.
            await ph.call("Browse.Folder", "get", {"id": 0}, version=2)
            setting = await ph.call("Setting.TeamSpace", "get", personal=True)
            if not setting.get("enable_person", True):
                return (
                    "People are turned off for Shared Space (Synology Photos > Settings > "
                    "Shared Space > People), so there's nothing to list."
                )
        params: dict[str, Any] = {"show_more": include_hidden, "show_hidden": include_hidden}
        entries = await ph.walk("Browse.Person", params=params)
        heading, headers = "People", ["ID", "Name", "Items", "Hidden"]
        rows = [
            [
                str(e["id"]),
                str(e.get("name") or "(unnamed)"),
                str(e.get("item_count", 0)),
                "no" if e.get("show", True) else "yes",
            ]
            for e in entries
        ]
    elif kind == "tag":
        entries = await ph.walk("Browse.GeneralTag")
        heading, headers = "Tags", ["ID", "Name", "Items"]
        rows = [
            [str(e["id"]), str(e.get("name", "")), str(e.get("item_count", 0))] for e in entries
        ]
    elif kind == "place":
        entries = await ph.walk("Browse.Geocoding")
        heading, headers = "Places", ["ID", "Name", "Country", "Items"]
        rows = [
            [
                str(e["id"]),
                str(e.get("name", "")),
                str(e.get("country", "")),
                str(e.get("item_count", 0)),
            ]
            for e in entries
        ]
    elif kind == "concept":
        entries = await ph.walk("Browse.Concept", params={"show_hidden": include_hidden}, version=2)
        heading, headers = "Concepts", ["ID", "Name", "Items", "Hidden"]
        rows = [
            [
                str(e["id"]),
                str(e.get("name", "")),
                str(e.get("item_count", 0)),
                "no" if e.get("visibility", True) not in (False, "hidden") else "yes",
            ]
            for e in entries
        ]
    else:
        under = await ph.folder(parent or "/", param="parent")
        entries = await ph.walk(
            "Browse.Folder",
            params={
                "id": int(under["id"]),
                "sort_by": "filename",
                "sort_direction": "asc",
                "additional": ["sharing_info"],
            },
            version=2,
        )
        heading = f"Folders in {under.get('name') or '/'}"
        headers = ["ID", "Path", "Shared"]
        rows = [
            [
                str(e["id"]),
                str(e.get("name", "")),
                _folder_shared(e),
            ]
            for e in entries
        ]
    if name:
        wanted = name.casefold()
        rows = [
            r
            for r, e in zip(rows, entries, strict=True)
            if wanted in str(e.get("name", "")).casefold()
        ]
        heading += f' matching "{name}"'
    total = len(rows)
    shown = rows[offset : offset + limit]
    noun = {"shared_with_me": "album", "person": "person", "place": "place"}.get(kind, kind)
    footer = page_footer(total, offset, len(shown), f"{noun}(s)")
    title = heading + title_space
    if not shown:
        return f"{title}\n{'=' * len(title)}\nNo items to display.\n\n{footer}"
    return f"{format_table(headers, shown, title=title)}\n\n{footer}"


def _privacy(info: dict[str, Any] | None) -> str:
    if not info:
        return "no"
    kind = str(info.get("privacy_type") or "private")
    shared = kind != "private" or bool(info.get("permission"))
    return kind if shared else "no"


def _folder_shared(entry: dict[str, Any]) -> str:
    """A folder's sharing state. 1.3.4 puts ``privacy_type`` in ``sharing_info``; on
    1.9.1 ``sharing_info`` has only ``sharing_link`` and the state is the top-level
    ``shared`` flag (both captured)."""
    info = (entry.get("additional") or {}).get("sharing_info") or {}
    if "privacy_type" in info:
        return _privacy(info)
    return "yes" if entry.get("shared") else "no"


def _span(entry: dict[str, Any]) -> str:
    from mcp_synology.core.formatting import format_timestamp

    start, end = entry.get("start_time"), entry.get("end_time")
    if not start and not end:
        return "—"
    first = format_timestamp(float(start))[:10] if start else "…"
    last = format_timestamp(float(end))[:10] if end else "…"
    return first if first == last else f"{first} – {last}"


def _album_row(e: dict[str, Any]) -> list[str]:
    info = (e.get("additional") or {}).get("sharing_info")
    return [
        str(e["id"]),
        str(e.get("name", "")),
        str(e.get("type", "")),
        str(e.get("item_count", 0)),
        _privacy(info),
        _span(e),
    ]


def _shared_row(e: dict[str, Any]) -> list[str]:
    info = (e.get("additional") or {}).get("sharing_info") or {}
    owner = (info.get("owner") or {}).get("name", "")
    return [
        str(e["id"]),
        str(e.get("name", "")),
        str(owner),
        str(e.get("item_count", 0)),
        str(info.get("privacy_type") or "—"),
        _span(e),
    ]


# -- get_photo -----------------------------------------------------------------

_GET_ADDITIONAL = [
    "description",
    "tag",
    "exif",
    "resolution",
    "orientation",
    "gps",
    "address",
    "rating",
    "person",
    "video_meta",
    "thumbnail",
]
_MAX_GET = 20
_ADDRESS_PARTS = ("village", "town", "district", "city", "county", "state", "country")


async def get_photo(
    client: DsmClient, *, ids: list[int], space: str = "personal", exif: bool = False
) -> str:
    """One or more items in full (spec: get_photo)."""
    op = "Get photo"
    sp = check_space(op, space)
    if not ids:
        invalid(op, "pass at least one id.", param="ids", value=ids)
    if len(ids) > _MAX_GET:
        invalid(op, f"at most {_MAX_GET} ids per call.", param="ids", value=len(ids))
    try:
        ph = Photos(client, sp, op)
        v7 = ph.has("Browse.Item", V7)
        additional = [*_GET_ADDITIONAL, "favorite"] if v7 else _GET_ADDITIONAL
        data = await ph.call(
            "Browse.Item",
            "get",
            {"id": list(dict.fromkeys(ids)), "additional": additional},
            version=V7 if v7 else 2,
        )
        found = {int(i["id"]): i for i in data.get("list", [])}
        blocks = []
        for item_id in dict.fromkeys(ids):
            item = found.get(item_id)
            if item is None:
                continue
            block = await _item_block(ph, item, v7)
            if exif:
                block += "\n" + await _exif_table(ph, item_id)
            blocks.append(block)
        missing = [str(i) for i in dict.fromkeys(ids) if i not in found]
        if missing:
            note = "Not found"
            if sp == "shared":
                note += ", or no permission for its Shared Space folder"
            blocks.append(f"{note}: {', '.join(missing)}")
        return "\n\n".join(blocks)
    except SynologyError as e:
        synology_error_response(op, e)


async def _item_block(ph: Photos, item: dict[str, Any], v7: bool) -> str:
    add = item.get("additional") or {}
    folder = await ph.folder_path(int(item["folder_id"])) if "folder_id" in item else "—"
    res = add.get("resolution") or {}
    size = [format_size(int(item.get("filesize") or 0))]
    if res.get("width") or res.get("height"):
        size.append(f"{res.get('width', 0)}×{res.get('height', 0)}")
    size.append(str(item.get("type", "")))
    video = add.get("video_meta") or {}
    if video.get("duration"):
        size.append(f"{int(video['duration']) / 1000:g} s")
    rating = str(add.get("rating") or "unrated")
    if v7:
        rating += f" · favorite: {'yes' if add.get('favorite') else 'no'}"
    pairs = [
        ("Folder", f"{folder} ({space_label(ph.space)})"),
        ("Taken", taken(item) + (" (still indexing)" if is_indexing(item) else "")),
        ("Size", " · ".join(size)),
        ("Rating", rating),
        ("Description", str(add.get("description") or "—")),
        ("Tags", ", ".join(str(t.get("name", "")) for t in add.get("tag") or []) or "—"),
        (
            "People",
            ", ".join(str(p.get("name") or "(unnamed)") for p in add.get("person") or []) or "—",
        ),
        ("Location", _location(add)),
        ("Camera", _camera(add.get("exif") or {})),
    ]
    return format_key_value(pairs, title=f"Item {item['id']}: {item.get('filename', '')}")


def _location(add: dict[str, Any]) -> str:
    gps = add.get("gps") or {}
    address = add.get("address") or {}
    parts = []
    if gps.get("latitude") is not None and gps.get("longitude") is not None:
        parts.append(f"{gps['latitude']}, {gps['longitude']}")
    names = [str(address[k]) for k in _ADDRESS_PARTS if address.get(k)]
    if names:
        parts.append(", ".join(dict.fromkeys(names)))
    return " · ".join(parts) or "—"


def _camera(exif: dict[str, Any]) -> str:
    camera = str(exif.get("camera") or "")
    details = [
        f"lens {exif.get('lens') or '—'}",
        f"f/{exif.get('aperture') or '—'}",
        f"{exif.get('exposure_time') or '—'} s",
        f"ISO {exif.get('iso') or '—'}",
    ]
    if not camera and not any(exif.get(k) for k in ("lens", "aperture", "exposure_time", "iso")):
        return "—"
    return " · ".join([camera or "—", *details])


async def _exif_table(ph: Photos, item_id: int) -> str:
    data = await ph.call("Browse.Item", "get_exif", {"id": [item_id]})
    listed = data.get("list", [])
    entries = listed[0].get("exif", []) if listed else []
    rows = [[str(e.get("key", "")), str(e.get("value", ""))] for e in entries]
    return format_table(["Key", "Value"], rows, title="EXIF")
