"""list_ssd_caches: SSD caches, hit rates and the cache advisor."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcp_synology.core.formatting import format_key_value
from mcp_synology.modules.filestation.helpers import dsm_str
from mcp_synology.modules.storage.helpers import (
    Sections,
    as_dict,
    cache_title,
    dicts,
    epoch_note,
    items,
    load_info,
    notes_block,
    other_fields,
    pct,
    raid_name,
    shared_cache_of,
    size,
    status_text,
    usage,
    value_text,
    when,
    yes_no,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

OPERATION = "List SSD caches"
_FLASHCACHE = "SYNO.Storage.CGI.Flashcache"

_CACHE_KNOWN = {
    "id",
    "num_id",
    "mode",
    "mountSpaceId",
    "path",
    "disks",
    "device_type",
    "hit_rate",
    "hit_rate_write",
    "hit_rates",
    "metadataCache",
    "status",
    "summary_status",
    "space_status",
    "size",
    "SSDPath",
    "SSDUUID",
}
_SHARED_KNOWN = {"id", "num_id", "device_type", "disks", "size", "raids", "space_path"}


def _mode(mode: object) -> str:
    return {"read": "read-only", "write": "read-write"}.get(str(mode), str(mode))


def cache_block(
    info: dict[str, Any],
    cache: dict[str, Any],
    shared: dict[str, Any] | None,
    vols: dict[str, str],
    config: Any,
) -> str:
    own_size = as_dict(cache.get("size"))
    pairs = [
        ("For volume", vols.get(str(cache.get("mountSpaceId")), str(cache.get("mountSpaceId")))),
        ("Mode", _mode(cache.get("mode"))),
        ("Status", status_text(cache)),
        ("Read hit rate", pct(cache.get("hit_rate"))),
        ("Write hit rate", pct(cache.get("hit_rate_write"))),
        ("Size", ", ".join(f"{k} {size(v)}" for k, v in own_size.items()) or "—"),
        ("UUID", str(cache.get("SSDUUID") or "—")),
        ("Metadata pinned", yes_no(cache.get("metadataCache"))),
        ("SSDs", ", ".join(map(str, cache.get("disks") or [])) or "—"),
    ]
    if shared:
        pairs.extend(
            [
                ("Shared cache", f"{shared.get('id')} ({raid_name(shared.get('device_type'))})"),
                ("  size", usage(shared.get("size") or {})),
                ("  SSDs", ", ".join(map(str, shared.get("disks") or [])) or "—"),
            ]
        )
        pairs.extend(other_fields(shared, _SHARED_KNOWN, prefix="  "))
    if isinstance(config, dict):
        pairs.extend((f"  setting: {k}", value_text(v)) for k, v in config.items())
    rates = as_dict(cache.get("hit_rates"))
    for period, rate in rates.items():
        rate = as_dict(rate)
        if not rate:
            pairs.append((f"Hit rate history: {period}", value_text(rates[period])))
            continue
        text = (
            f"{rate.get('io_hit')} hits of {rate.get('io_need_acceleration')} cacheable I/O"
            if rate.get("success")
            else "no data"
        )
        extra = [
            f"{k}: {value_text(v)}"
            for k, v in rate.items()
            if k not in ("io_hit", "io_need_acceleration", "success")
        ]
        pairs.append((f"Hit rate history: {period}", ", ".join([text, *extra])))
    pairs.extend(other_fields(cache, _CACHE_KNOWN))
    return format_key_value(pairs, title=cache_title(info, cache))


def advisor_lines(history: Any, vols: dict[str, str]) -> list[str]:
    lines = []
    for item in dicts(history.get("items") if isinstance(history, dict) else None):
        lines.append(
            f"{vols.get(str(item.get('volume')), item.get('volume'))}: finished "
            f"{when(item.get('completed_time'))}, suggests {size(item.get('recommended_size'))} "
            f"({size(item.get('recommended_size_pin_meta'))} with metadata pinned), "
            f"{item.get('suggestion')}"
        )
        extra = {
            k: v
            for k, v in item.items()
            if k
            not in (
                "volume",
                "completed_time",
                "recommended_size",
                "recommended_size_pin_meta",
                "suggestion",
            )
        }
        if extra:
            lines.append("  " + json.dumps(extra))
    return lines


async def list_ssd_caches(client: DsmClient) -> str:
    info = await load_info(client, OPERATION)
    optional = Sections(OPERATION)
    vols = {
        str(v.get("id")): f"volume {v.get('num_id', '?')} ({v.get('vol_path')})"
        for v in items(info, "volumes")
    }
    caches = items(info, "ssdCaches")
    blocks = []
    for cache in caches:
        owner = shared_cache_of(info, cache)
        config = None
        if owner:
            config = await optional.get(
                client,
                f"{owner.get('id')} settings",
                _FLASHCACHE,
                "shared_cache_config_get",
                {"shared_cache": dsm_str(str(owner.get("id")))},
            )
        blocks.append(cache_block(info, cache, owner, vols, config))
    if not caches:
        blocks.append("No SSD caches.")
    history = await optional.get(client, "SSD cache advisor", _FLASHCACHE, "advisor_history_get")
    lines = advisor_lines(history, vols)
    if lines:
        blocks.append("\n".join(["SSD cache advisor", *(f"  {x}" for x in lines)]))
    text = "\n\n".join(blocks)
    return text + epoch_note(text) + notes_block(optional.notes)
