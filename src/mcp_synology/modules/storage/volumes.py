"""list_volumes and get_volume."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp_synology.core.formatting import format_key_value
from mcp_synology.modules.filestation.helpers import dsm_str
from mcp_synology.modules.storage.helpers import (
    STORAGE_API,
    Sections,
    cache_title,
    cell,
    dicts,
    epoch_note,
    field_text,
    items,
    json_list,
    load_info,
    not_found,
    notes_block,
    other_fields,
    pct,
    percent,
    raid_name,
    size,
    status_text,
    to_int,
    usage,
    value_text,
    volume_label,
    yes_no,
)
from mcp_synology.modules.storage.overview import fs_name

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

_KNOWN = {
    "id",
    "num_id",
    "vol_path",
    "fs_type",
    "size",
    "status",
    "summary_status",
    "space_status",
    "pool_path",
    "device_type",
    "is_encrypted",
    "is_locked",
    "is_vault_contain_key",
    "scrubbingStatus",
    "vol_desc",
    "uuid",
    "ssd_trim",
}

_CATEGORIES = {
    "share": "Shared folders",
    "snapshot": "Snapshots",
    "lun": "iSCSI LUNs",
    "others": "Other",
    "drive": "Synology Drive",
    "c2share": "C2 shared folders",
    "petaspace": "PetaSpace",
}


def pool_name(volume: dict[str, Any], pools: dict[str, dict[str, Any]]) -> str:
    pool = pools.get(str(volume.get("pool_path")))
    if pool:
        return f"pool {pool.get('num_id', '?')} ({pool.get('id')})"
    return "none (not in a storage pool)"


def volume_pairs(volume: dict[str, Any], pools: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    sizes = volume.get("size") or {}
    free = None
    if sizes.get("total") is not None and sizes.get("used") is not None:
        try:
            free = size(int(sizes["total"]) - int(sizes["used"]))
        except (TypeError, ValueError):
            free = None
    pairs = [
        ("ID", cell(volume.get("id"))),
        ("File system", fs_name(volume.get("fs_type"))),
        ("Size", usage(sizes)),
        ("Free", free or "—"),
        ("Status", status_text(volume)),
        ("Storage pool", pool_name(volume, pools)),
        ("RAID type", raid_name(volume.get("device_type"))),
    ]
    for key, label in (
        ("is_encrypted", "Encrypted"),
        ("is_locked", "Locked"),
        ("is_vault_contain_key", "Key in the key vault"),
    ):
        if key in volume:
            pairs.append((label, yes_no(volume[key])))
    pairs.append(("Data scrubbing", str(volume.get("scrubbingStatus") or "—")))
    extra_sizes = {k: v for k, v in sizes.items() if k not in ("total", "used")}
    if extra_sizes:
        pairs.append(
            ("Size details", ", ".join(f"{k}: {value_text(v)}" for k, v in extra_sizes.items()))
        )
    if volume.get("vol_desc"):
        pairs.append(("Description", str(volume["vol_desc"])))
    return pairs


async def list_volumes(client: DsmClient) -> str:
    info = await load_info(client, "List volumes")
    pools = {str(p.get("id")): p for p in items(info, "storagePools")}
    volumes = items(info, "volumes")
    if not volumes:
        return "No volumes."
    blocks = []
    for volume in volumes:
        pairs = volume_pairs(volume, pools)
        pairs.append(("UUID", str(volume.get("uuid") or "—")))
        pairs.extend(other_fields(volume, _KNOWN))
        blocks.append(format_key_value(pairs, title=volume_label(volume)))
    text = "\n\n".join(blocks)
    return text + epoch_note(text)


def resolve_volume(info: dict[str, Any], volume: str, operation: str) -> dict[str, Any]:
    wanted = volume.strip().rstrip("/").casefold()
    for vol in items(info, "volumes"):
        names = {str(vol.get(k)).casefold() for k in ("id", "vol_path") if vol.get(k)}
        if wanted and (wanted in names or "/" + wanted.lstrip("/") in names):
            return vol
    not_found(
        operation,
        "volume",
        "volume",
        volume,
        [str(v.get("vol_path")) for v in items(info, "volumes")],
    )
    raise AssertionError  # not_found raises


def space_usage_lines(report: Any) -> list[tuple[str, str]]:
    if not isinstance(report, dict):
        return []
    if report.get("status") != "enabled":
        return [
            (
                "Space by category",
                "off for this volume (turn on space usage details for it in Storage Manager to "
                "see its space by shared folders, snapshots and so on)",
            )
        ]
    total = report.get("total_size")
    pairs = [("Space by category", f"{size(total)} total, {size(report.get('free_size'))} free")]
    for cat in sorted(dicts(report.get("categories")), key=lambda c: -(to_int(c.get("size")) or 0)):
        name = str(cat.get("category_name"))
        pairs.append(
            (
                f"  {_CATEGORIES.get(name, name)}",
                f"{size(cat.get('size'))} ({percent(cat.get('size'), total)})",
            )
        )
    extra = {
        k: v
        for k, v in report.items()
        if k not in ("status", "total_size", "free_size", "categories", "volume_path")
    }
    pairs.extend((f"  {k}", value_text(v)) for k, v in extra.items())
    return pairs


def dedupe_lines(reply: Any, vol_path: str) -> list[tuple[str, str]]:
    if not isinstance(reply, dict):
        return []
    for entry in dicts(reply.get("volumes")):
        if entry.get("vol_path") != vol_path:
            continue
        pairs = [("Deduplication", "supported" if entry.get("support_dedupe") else "not supported")]
        pairs.extend(
            (f"  {k}", field_text(k, v))
            for k, v in entry.items()
            if k not in ("vol_path", "support_dedupe")
        )
        return pairs
    return []


def cache_lines(info: dict[str, Any], volume: dict[str, Any], config: Any) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for cache in items(info, "ssdCaches"):
        if cache.get("mountSpaceId") != volume.get("id"):
            continue
        mode = {"read": "read-only", "write": "read-write"}.get(
            str(cache.get("mode")), str(cache.get("mode"))
        )
        pairs.append(("SSD cache", f"{cache_title(info, cache)}: {mode}, {status_text(cache)}"))
        pairs.append(
            (
                "  hit rate",
                f"read {pct(cache.get('hit_rate'))}, write {pct(cache.get('hit_rate_write'))}",
            )
        )
        pairs.append(("  SSDs", ", ".join(map(str, cache.get("disks") or [])) or "—"))
        pairs.append(("  metadata pinned", yes_no(cache.get("metadataCache"))))
    if isinstance(config, dict):
        pairs.extend((f"  shared cache: {k}", value_text(v)) for k, v in config.items())
    return pairs


async def get_volume(client: DsmClient, volume: str) -> str:
    operation = "Get volume"
    info = await load_info(client, operation)
    vol = resolve_volume(info, volume, operation)
    path = str(vol.get("vol_path"))
    pools = {str(p.get("id")): p for p in items(info, "storagePools")}
    optional = Sections(operation)

    space = await optional.get(
        client,
        "Space by category",
        "SYNO.Storage.CGI.Volume",
        "get_space_usage",
        {"volume_path": path},
    )
    trim = await optional.get(
        client, "SSD TRIM", "SYNO.Storage.CGI.Volume", "ssd_trim_get", {"space_path": path}
    )
    dedupe = await optional.get(
        client,
        "Deduplication",
        "SYNO.Storage.CGI.BtrfsDedupe",
        "get_volume_info",
        {"vol_paths": json_list([path])},
    )
    reclaim = await optional.get(
        client, "Space reclamation", STORAGE_API, "get_space_reclaim_status"
    )

    shared_config = None
    for shared in items(info, "sharedCaches"):
        attached = [
            c
            for c in items(info, "ssdCaches")
            if c.get("mountSpaceId") == vol.get("id")
            and str(c.get("SSDPath", "")).startswith(str(shared.get("space_path")) + "/")
        ]
        if attached:
            shared_config = await optional.get(
                client,
                "Shared SSD cache settings",
                "SYNO.Storage.CGI.Flashcache",
                "shared_cache_config_get",
                {"shared_cache": dsm_str(str(shared.get("id")))},
            )

    pairs = volume_pairs(vol, pools)
    pairs.extend(space_usage_lines(space))
    ssd_trim = trim.get("ssd_trim") if isinstance(trim, dict) else None
    if not isinstance(ssd_trim, dict):
        ssd_trim = vol.get("ssd_trim")
    if isinstance(ssd_trim, dict):
        pairs.append(("SSD TRIM", ", ".join(f"{k}: {value_text(v)}" for k, v in ssd_trim.items())))
    pairs.extend(dedupe_lines(dedupe, path))
    if isinstance(reclaim, dict):
        pairs.append(("Space reclamation", str(reclaim.get("state", "—"))))
        pairs.extend((f"  {k}", field_text(k, v)) for k, v in reclaim.items() if k != "state")
    pairs.extend(cache_lines(info, vol, shared_config))
    pairs.append(("UUID", str(vol.get("uuid") or "—")))
    pairs.extend(other_fields(vol, _KNOWN, keep_ui=True))
    text = format_key_value(pairs, title=volume_label(vol))
    return text + epoch_note(text) + notes_block(optional.notes)
