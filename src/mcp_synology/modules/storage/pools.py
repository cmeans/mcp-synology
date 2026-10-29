"""list_storage_pools: RAID, members, progress, scrubbing, hot spares."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp_synology.core.formatting import format_key_value
from mcp_synology.modules.storage.helpers import (
    Sections,
    as_dict,
    dicts,
    epoch_note,
    items,
    load_info,
    notes_block,
    other_fields,
    percent,
    pool_label,
    raid_name,
    size,
    status_text,
    value_text,
    when,
    yes_no,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

OPERATION = "List storage pools"

_KNOWN = {
    "id", "num_id", "pool_path", "space_path", "device_type", "status", "summary_status",
    "space_status", "size", "disks", "raids", "spares", "progress", "data_scrubbing",
    "scrubbingStatus", "last_done_time", "next_schedule_time", "is_scheduled", "desc",
    "pool_child", "uuid", "missing_drives", "disk_failure_number", "repair_action",
}  # fmt: skip
_RAID_KNOWN = {"raidPath", "devices", "designedDiskCount", "normalDevCount", "spares"}
_PROGRESS_KNOWN = {"step", "percent", "cur_step", "total_step"}


def members(pool: dict[str, Any]) -> str:
    """Each member disk once, with its state (an SHR pool spans several md arrays)."""
    seen: dict[str, str] = {}
    for raid in dicts(pool.get("raids")):
        for d in dicts(raid.get("devices")):
            seen.setdefault(str(d.get("id")), str(d.get("status")))
    if seen:
        return ", ".join(f"{disk} ({state})" for disk, state in seen.items())
    return ", ".join(str(d) for d in pool.get("disks") or []) or "—"


def raid_lines(pool: dict[str, Any]) -> list[tuple[str, str]]:
    pairs = []
    for raid in dicts(pool.get("raids")):
        normal, designed = raid.get("normalDevCount"), raid.get("designedDiskCount")
        text = f"{normal} of {designed} disks working"
        if normal is not None and designed is not None and normal != designed:
            text += " (degraded)"
        extra = [f"{k}: {value_text(v)}" for k, v in raid.items() if k not in _RAID_KNOWN]
        pairs.append((f"  array {raid.get('raidPath', '?')}", ", ".join([text, *extra])))
    return pairs


def progress_text(progress: dict[str, Any]) -> str:
    step = str(progress.get("step") or "none")
    if step == "none":
        return "none"
    pct = str(progress.get("percent", "-1"))
    text = step + (f" {pct}%" if pct not in ("-1", "") else "")
    if progress.get("total_step"):
        text += f" (step {progress.get('cur_step')} of {progress.get('total_step')})"
    extra = [f"{k}: {value_text(v)}" for k, v in progress.items() if k not in _PROGRESS_KNOWN]
    return ", ".join([text, *extra])


def pool_block(pool: dict[str, Any], vol_names: dict[str, str], setting: Any) -> str:
    scrub = as_dict(pool.get("data_scrubbing"))
    spares = [str(s.get("id")) for s in dicts(pool.get("spares"))]
    sizes = as_dict(pool.get("size"))
    space = as_dict(pool.get("space_status"))
    allocated = (
        f"{size(sizes.get('total'))}, of which {size(sizes.get('used'))} "
        f"({percent(sizes.get('used'), sizes.get('total'))}) is allocated to volumes"
    )
    pairs = [
        ("RAID type", raid_name(pool.get("device_type"))),
        ("Status", status_text(pool)),
        ("Space status", str(space.get("status") or "—")),
        # A pool's "used" is what its volumes take up, not the data in them.
        ("Size", allocated),
        ("Disks", members(pool)),
        *raid_lines(pool),
        ("Hot spares", ", ".join(spares) or "none"),
        ("Failed disks", str(pool.get("disk_failure_number", 0))),
        ("Missing disks", ", ".join(map(str, pool.get("missing_drives") or [])) or "none"),
        ("In progress", progress_text(as_dict(pool.get("progress")))),
        ("Repair", str(pool.get("repair_action") or "none")),
        (
            "Volumes",
            ", ".join(
                vol_names.get(str(c.get("id")), str(c.get("id")))
                for c in dicts(pool.get("pool_child"))
            )
            or "none",
        ),
        ("Data scrubbing", str(pool.get("scrubbingStatus") or "—")),
        ("  scheduled", yes_no(pool.get("is_scheduled"))),
        ("  last done", when(pool.get("last_done_time"))),
        ("  next run", when(pool.get("next_schedule_time"))),
        (
            "  can run",
            f"manually {yes_no(scrub.get('can_do_manual'))}, "
            f"on a schedule {yes_no(scrub.get('can_do_schedule'))}"
            + (f" ({scrub['reason']})" if scrub.get("reason") else ""),
        ),
        ("Device path", str(pool.get("space_path") or "—")),
        ("Pool path", str(pool.get("pool_path") or "—")),
        ("UUID", str(pool.get("uuid") or "—")),
    ]
    if pool.get("desc"):
        pairs.insert(0, ("Description", str(pool["desc"])))
    if isinstance(setting, dict):
        pairs.extend((f"Setting: {k}", value_text(v)) for k, v in setting.items())
    pairs.extend(other_fields(pool, _KNOWN))
    return format_key_value(pairs, title=pool_label(pool))


def spare_lines(spares: Any, pools: list[dict[str, Any]], conf: Any) -> list[str]:
    by_path = {str(p.get("space_path")): pool_label(p) for p in pools}
    lines: list[str] = []
    if isinstance(conf, dict) and "auto_replacement" in conf:
        lines.append(
            "Automatic replacement of a failed disk: " + yes_no(conf.get("auto_replacement"))
        )
    hot = (spares or {}).get("hotSpares") if isinstance(spares, dict) else None
    for spare in dicts(hot):
        protects = (
            ", ".join(by_path.get(str(p), str(p)) for p in spare.get("protect_spaces") or [])
            or "nothing"
        )
        status = spare.get("status")
        state = status.get("type", "—") if isinstance(status, dict) else value_text(status)
        lines.append(
            f"{spare.get('id')}   {str(spare.get('model') or '—').strip()}   serial "
            f"{spare.get('serial') or '—'}   {size(spare.get('size'))}   {state}   "
            f"protects {protects}"
        )
    if hot == []:
        lines.append("No hot spares.")
    return lines


def potential_lines(dump: Any, pools: list[dict[str, Any]]) -> list[str]:
    if not isinstance(dump, dict):
        return []
    by_path = {str(p.get("space_path")): pool_label(p) for p in pools}
    lines = []
    for space in dicts(dump.get("spaces")):
        name = by_path.get(str(space.get("space_path")), str(space.get("space_path")))
        valid = [
            str(v.get("path") if isinstance(v, dict) else v)
            for v in space.get("valid_spares") or []
        ]
        reasons = []
        for key, why in (
            ("size_too_small", "too small"),
            ("diff_drive_type", "a different drive type"),
            ("diff_protocol_type", "a different interface"),
            ("diff_4kn", "a different sector size"),
            ("diff_container", "in another enclosure"),
        ):
            for disk in space.get(key) or []:
                reasons.append(f"{disk}: {why}")
        text = f"{name}: " + (", ".join(valid) if valid else "no disk qualifies")
        text += f" (needs at least {size(space.get('min_hotspare_size'))})"
        if reasons:
            text += "; " + "; ".join(reasons)
        lines.append(text)
    return lines


async def list_storage_pools(client: DsmClient) -> str:
    info = await load_info(client, OPERATION)
    optional = Sections(OPERATION)
    pools = items(info, "storagePools")
    vol_names = {
        str(v.get("id")): f"volume {v.get('num_id', '?')} ({v.get('vol_path')})"
        for v in items(info, "volumes")
    }
    blocks: list[str] = []
    for pool in pools:
        setting = None
        if pool.get("space_path"):
            setting = await optional.get(
                client,
                f"{pool_label(pool)} settings",
                "SYNO.Storage.CGI.Pool",
                "get_setting",
                {"pool_path": pool["space_path"]},
            )
        blocks.append(pool_block(pool, vol_names, setting))

    missing = items(info, "missing_pools")
    detected = items(info, "detected_pools")
    lost = [
        f"{title}: " + (", ".join(value_text(p) for p in found) if found else "none")
        for title, found in (("Missing pools", missing), ("Detected pools", detected))
    ]
    if not pools:
        if missing or detected:
            blocks.append("No assembled storage pools.")
        else:
            blocks.append(
                "No storage pools. Volumes on this NAS aren't in a storage pool (see get_volume)."
            )
        return "\n\n".join([*blocks, *lost]) + notes_block(optional.notes)

    spares = await optional.get(client, "Hot spares", "SYNO.Storage.CGI.Spare", "list")
    potential = await optional.get(
        client, "Possible hot spares", "SYNO.Storage.CGI.Spare", "potential_spares_dump"
    )
    conf = await optional.get(client, "Hot-spare settings", "SYNO.Storage.CGI.Spare.Conf", "get")
    blocks.append("\n".join(["Hot spares", *(f"  {x}" for x in spare_lines(spares, pools, conf))]))
    if potential_lines(potential, pools):
        blocks.append(
            "\n".join(
                [
                    "Disks that could be hot spares",
                    *(f"  {x}" for x in potential_lines(potential, pools)),
                ]
            )
        )
    blocks.extend(lost)
    text = "\n\n".join(blocks)
    return text + epoch_note(text) + notes_block(optional.notes)
