"""get_storage_overview: the answer to "is anything wrong with my storage?"."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcp_synology.core.formatting import format_key_value, format_timestamp
from mcp_synology.modules.storage.helpers import (
    STORAGE_API,
    Sections,
    dicts,
    disk_label,
    epoch_note,
    items,
    load_info,
    notes_block,
    percent,
    pool_label,
    raid_name,
    shared_cache_of,
    size,
    status_text,
    usage,
    volume_label,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

OPERATION = "Storage overview"

# overview_data.status_level: bare on DSM 7.1 ("normal"), prefixed elsewhere
# ("status_level_normal").
_LEVELS = {"normal": "normal", "abnormal": "attention", "danger": "danger"}

_HEALTH_RULES = {
    "system_health": "healthy",
    "storage_is_attention": "storage needs attention",
    "storage_is_danger": "storage is in danger",
}

_TEST_STYLES = {"quick": "quick", "extend": "extended"}


def headline(info: dict[str, Any]) -> str:
    level = str((info.get("overview_data") or {}).get("status_level") or "")
    level = level.removeprefix("status_level_")
    return _LEVELS.get(level, level or "unknown")


def fs_name(fs_type: object) -> str:
    text = str(fs_type or "")
    return {"btrfs": "Btrfs", "ext4": "ext4"}.get(text, text or "—")


def disk_roles(info: dict[str, Any]) -> dict[str, str]:
    """What each disk is used for: 'pool 1', 'hot spare', 'SSD cache 1'."""
    roles: dict[str, str] = {}
    pools = items(info, "storagePools")
    for pool in pools:
        for spare in pool.get("spares") or []:
            if isinstance(spare, dict) and spare.get("id"):
                roles[str(spare["id"])] = "hot spare"
    for cache in items(info, "sharedCaches") or items(info, "ssdCaches"):
        for disk in cache.get("disks") or []:
            roles.setdefault(str(disk), f"SSD cache {cache.get('num_id', '?')} ({cache.get('id')})")
    by_id = {str(p.get("id")): p for p in pools}
    for disk in items(info, "disks"):
        did = str(disk.get("id"))
        used_by = str(disk.get("used_by") or "")
        if did in roles:
            continue
        if used_by in by_id:
            roles[did] = f"pool {by_id[used_by].get('num_id', '?')}"
        elif used_by == "hotspare":
            roles[did] = "hot spare"
        elif used_by:
            roles[did] = used_by
        else:
            roles[did] = "not in use"
    return roles


def running_test_style(tasks: dict[str, Any] | None, disk_id: str) -> str:
    for task in task_list(tasks, "smart"):
        if task.get("status") == "running" and disk_id in (task.get("target") or []):
            return _TEST_STYLES.get(str(task.get("test_style")), str(task.get("test_style")))
    return ""


def disk_notes(disk: dict[str, Any], tasks: dict[str, Any] | None) -> list[str]:
    """What's worth saying on a disk's line beside its status."""
    notes: list[str] = []
    if disk.get("smart_testing") or disk.get("testing_progress"):
        style = running_test_style(tasks, str(disk.get("id")))
        kind = (
            "S.M.A.R.T."
            if disk.get("testing_type") in ("smart", None)
            else str(disk.get("testing_type"))
        )
        progress = disk.get("testing_progress") or disk.get("smart_progress") or ""
        notes.append(" ".join(x for x in (style, kind, "test", str(progress)) if x))
    life = disk.get("remain_life")
    if isinstance(life, int) and life >= 0:
        notes.append(f"{life}% life left")
    if disk.get("compatibility") == "not_in_support":
        notes.append("not on Synology's compatibility list")
    return notes


def pool_shape(pool: dict[str, Any]) -> str:
    disks = len(pool.get("disks") or [])
    spares = len(pool.get("spares") or [])
    text = f"{raid_name(pool.get('device_type'))}, {disks} disk{'s' if disks != 1 else ''}"
    if spares:
        text += f" + {spares} hot spare{'s' if spares != 1 else ''}"
    return text


def cache_lines(info: dict[str, Any]) -> list[str]:
    """One line per shared SSD cache (or per volume cache, on DSMs without shared ones)."""
    lines: list[str] = []
    vol_num = {str(v.get("id")): v.get("num_id", "?") for v in items(info, "volumes")}
    ssd = items(info, "ssdCaches")
    for shared in items(info, "sharedCaches"):
        attached = [c for c in ssd if shared_cache_of(info, c) is shared]
        disks = len(shared.get("disks") or [])
        parts = [f"Cache {shared.get('num_id', '?')} ({shared.get('id')})"]
        modes = {str(c.get("mode")) for c in attached}
        mode = ", ".join(
            "read-only" if m == "read" else "read-write" if m == "write" else m for m in modes
        )
        parts.append(f"{mode + ', ' if mode else ''}{disks} SSD{'s' if disks != 1 else ''}")
        for c in attached:
            parts.append(f"for volume {vol_num.get(str(c.get('mountSpaceId')), '?')}")
            if c.get("hit_rate") is not None:
                parts.append(f"hit rate {c.get('hit_rate')}%")
        lines.append("   ".join(parts))
    if not lines:
        for c in ssd:
            lines.append(
                f"Cache {c.get('num_id', '?')} ({c.get('id')})   {c.get('mode')}   "
                f"for volume {vol_num.get(str(c.get('mountSpaceId')), '?')}"
            )
    return lines


def _names(entries: object) -> set[str]:
    return {
        str(e.get("id") if isinstance(e, dict) else e).lower()
        for e in (entries if isinstance(entries, list) else [])
    }


def _disk_names(disk: dict[str, Any]) -> set[str]:
    return {str(disk.get(k)).lower() for k in ("id", "name", "longName", "device") if disk.get(k)}


def task_list(tasks: Any, kind: str) -> list[dict[str, Any]]:
    """One kind's tasks from ``get_sche_task_list`` (smart, data_scrubbing, ssd_trim)."""
    section = tasks.get(kind) if isinstance(tasks, dict) else None
    return dicts(section.get("tasks")) if isinstance(section, dict) else []


def attention_items(info: dict[str, Any], bad: dict[str, Any] | None) -> list[str]:
    found: list[str] = []
    for pool in items(info, "storagePools"):
        if pool.get("summary_status", "normal") != "normal":
            found.append(f"Storage {pool_label(pool).lower()}: {status_text(pool)}")
    for vol in items(info, "volumes"):
        if vol.get("summary_status", "normal") != "normal":
            sizes = vol.get("size") or {}
            found.append(
                f"{volume_label(vol)}: {status_text(vol)}, "
                f"{percent(sizes.get('used'), sizes.get('total'))} used"
            )
    for cache in items(info, "ssdCaches"):
        if cache.get("summary_status", "normal") != "normal":
            found.append(f"SSD cache {cache.get('id')}: {status_text(cache)}")
    bad = bad or {}
    # DSM lists them by display name ("Drive 1"); vdsm, which has no disks, lists two
    # drives that don't exist, so an entry that matches no disk is left out.
    unhealthy = _names(bad.get("unhealthy"))
    incompatible = _names(bad.get("not_compatible"))
    for disk in items(info, "disks"):
        reasons = []
        if disk.get("overview_status", "normal") != "normal":
            reasons.append(str(disk.get("overview_status")))
        names = _disk_names(disk)
        if names & unhealthy:
            reasons.append("unhealthy")
        if names & incompatible:
            reasons.append("not compatible")
        if reasons:
            found.append(f"{disk_label(disk)}: {', '.join(reasons)}")
    return found


def scheduled_local_times(scheduler: dict[str, Any] | None) -> dict[str, str]:
    """Next run of each enabled S.M.A.R.T. task style, as the NAS's local clock time."""
    times: dict[str, str] = {}
    for task in dicts(scheduler.get("items") if isinstance(scheduler, dict) else None):
        try:
            args = json.loads(task.get("app_args") or "{}")
        except (TypeError, ValueError):
            args = {}
        style = str(args.get("test_style") or "")
        when = str(task.get("next_trigger_time") or "")
        if task.get("enabled") and style and when and (style not in times or when < times[style]):
            times[style] = when
    return times


def task_lines(
    info: dict[str, Any],
    tasks: dict[str, Any] | None,
    scheduler: dict[str, Any] | None,
    scrubbing: bool,
    building: bool,
) -> list[str]:
    disks = {str(d.get("id")): d for d in items(info, "disks")}
    pools = {str(p.get("id")): p for p in items(info, "storagePools")}
    running: list[str] = []
    upcoming: list[str] = []
    local = scheduled_local_times(scheduler)

    for task in task_list(tasks, "smart"):
        style = _TEST_STYLES.get(str(task.get("test_style")), str(task.get("test_style")))
        targets = [str(t) for t in task.get("target") or []]
        if task.get("status") == "running":
            testing = [
                f"{disks[t].get('longName') or t} at {disks[t].get('testing_progress')}"
                for t in targets
                if t in disks and disks[t].get("smart_testing")
            ]
            running.append(
                f"S.M.A.R.T. {style} test" + (f" ({', '.join(testing)})" if testing else "")
            )
        else:
            when = local.get(str(task.get("test_style")))
            when_text = (
                f"{when} (NAS time)"
                if when
                else f"about {format_timestamp(task['next_run_time'])}"
                if task.get("next_run_time")
                else "not scheduled"
            )
            count = f"{len(targets)} disk{'s' if len(targets) != 1 else ''}"
            upcoming.append(f"S.M.A.R.T. {style} test of {count}, {when_text}")

    for task in task_list(tasks, "data_scrubbing"):
        names = [
            pool_label(pools[t]).split(" (")[0].lower() if t in pools else t
            for t in task.get("target") or []
        ]
        line = f"Data scrubbing of {', '.join(names) or 'all pools'}"
        if task.get("status") == "running":
            running.append(line)
        elif task.get("next_run_time"):
            upcoming.append(f"{line}, about {format_timestamp(task['next_run_time'])}")

    for pool in pools.values():
        progress = pool.get("progress") or {}
        if str(progress.get("percent", "-1")) not in ("-1", "") and progress.get("step") != "none":
            running.append(f"{pool_label(pool)}: {progress.get('step')} {progress.get('percent')}%")
    if scrubbing and not any("scrubbing" in r for r in running):
        running.append("Data scrubbing")
    if building and not any(":" in r for r in running):
        running.append("A RAID build or repair")

    lines = (
        [f"Running:  {running[0]}", *(f"          {r}" for r in running[1:])]
        if running
        else ["Running:  nothing"]
    )
    if upcoming:
        lines.append(f"Next:     {upcoming[0]}")
        lines.extend(f"          {u}" for u in upcoming[1:])
    return lines


def _section(title: str, lines: list[str]) -> str:
    return "\n".join([title, *(f"  {line}" for line in (lines or ["none"]))])


async def get_storage_overview(client: DsmClient) -> str:
    info = await load_info(client, OPERATION)
    optional = Sections(OPERATION)
    bad = await optional.get(client, "Bad-disk list", STORAGE_API, "load_bad_disks")
    tasks = await optional.get(client, "Storage tasks", STORAGE_API, "get_sche_task_list")
    scheduler = await optional.get(
        client, "S.M.A.R.T. schedule", "SYNO.Storage.CGI.Smart.Scheduler", "list"
    )
    scrub = await optional.get(
        client, "Data scrubbing state", "SYNO.Storage.CGI.Check", "is_data_scrubbing"
    )
    build = await optional.get(client, "RAID build state", "SYNO.Storage.CGI.Check", "is_building")
    health = await optional.get(client, "System health", "SYNO.Core.System.SystemHealth", "get")

    env = info.get("env") or {}
    pairs: list[tuple[str, str]] = []
    rule = (health or {}).get("rule") or {}
    if rule.get("id"):
        rid = str(rule["id"])
        pairs.append(("System health", f"{_HEALTH_RULES.get(rid, rid.replace('_', ' '))} ({rid})"))
    eunits = env.get("eunits") or []
    model = f"{env.get('model_name', '?')}, {env.get('bay_number', '?')} bays, " + (
        f"{len(eunits)} expansion unit{'s' if len(eunits) != 1 else ''}"
        if eunits
        else "no expansion units"
    )
    pairs.append(("Model", model))

    pools = items(info, "storagePools")
    volumes = items(info, "volumes")
    disks = items(info, "disks")
    roles = disk_roles(info)
    attention = attention_items(info, bad)

    parts = [format_key_value(pairs, title=f"Storage Overview — {headline(info)}")]
    if attention:
        parts.append(_section("Needs attention", [f"- {a}" for a in attention]))
    parts.append(
        _section(
            "Storage pools",
            [
                "   ".join(
                    (
                        pool_label(p),
                        pool_shape(p),
                        status_text(p).split(",")[0],
                        size((p.get("size") or {}).get("total")),
                    )
                )
                for p in pools
            ],
        )
    )
    parts.append(
        _section(
            "Volumes",
            [
                "   ".join(
                    (
                        volume_label(v),
                        fs_name(v.get("fs_type")),
                        str(v.get("summary_status") or v.get("status")),
                        usage(v.get("size") or {}),
                    )
                )
                for v in volumes
            ],
        )
    )
    disk_lines = []
    for d in disks:
        cells = [
            disk_label(d),
            str(d.get("model") or "—").strip(),
            size(d.get("size_total")),
            f"{d.get('temp')}°C" if d.get("temp") is not None else "—",
            str(d.get("overview_status") or d.get("status") or "—"),
            roles.get(str(d.get("id")), "—"),
        ]
        notes = disk_notes(d, tasks)
        disk_lines.append("   ".join(cells) + (f"   {', '.join(notes)}" if notes else ""))
    if not disks and env.get("model_name") == "VirtualDSM":
        disk_lines = ["none (Virtual DSM has no physical disks)"]
    parts.append(_section("Disks", disk_lines))
    parts.append(_section("SSD caches", cache_lines(info)))
    parts.append(
        _section(
            "Storage tasks",
            task_lines(
                info,
                tasks,
                scheduler,
                bool((scrub or {}).get("isBuilding") or (scrub or {}).get("isDataScrubbing")),
                bool((build or {}).get("isBuilding")),
            ),
        )
    )
    text = "\n\n".join(parts)
    return text + epoch_note(text) + notes_block(optional.notes)
