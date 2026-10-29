"""list_disks and get_disk: inventory, S.M.A.R.T., test history, health trend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp_synology.core.formatting import format_key_value, format_table
from mcp_synology.modules.storage.helpers import (
    STORAGE_API,
    Sections,
    disk_label,
    items,
    load_info,
    not_found,
    notes_block,
    other_fields,
    size,
    value_text,
)
from mcp_synology.modules.storage.overview import disk_notes, disk_roles

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

_KNOWN = {
    "id",
    "name",
    "longName",
    "device",
    "model",
    "vendor",
    "serial",
    "ui_serial",
    "firm",
    "size_total",
    "isSsd",
    "diskType",
    "portType",
    "temp",
    "status",
    "overview_status",
    "smart_status",
    "unc",
    "remain_life",
    "used_by",
    "smart_testing",
    "testing_progress",
    "smart_progress",
    "testing_type",
    "compatibility",
    "container",
    "num_id",
    "order",
    "slot_id",
    "summary_status_key",
    "summary_status_category",
}

_SMART = "SYNO.Storage.CGI.Smart"
_DISK = "SYNO.Core.Storage.Disk"

# get_health_info's monthly series: unc = bad sectors, idnf = ID-not-found errors.
_TREND = {
    "unc": "Bad sectors",
    "idnf": "IDNF errors",
    "retry": "Retries",
    "remainLife": "Remaining life (%)",
}


def _metric(value: object) -> str:
    """-1 means "not reported by this drive" (HDDs: remain_life, SSDs: unc)."""
    return "—" if value in (-1, "-1", None) else str(value)


def disk_type(disk: dict[str, Any]) -> str:
    kind = "SSD" if disk.get("isSsd") else "HDD"
    port = str(disk.get("diskType") or "")
    return f"{kind} {port}".strip()


def disk_pairs(disk: dict[str, Any], role: str, tasks: Any) -> list[tuple[str, str]]:
    notes = disk_notes(disk, tasks)
    return [
        ("Device", str(disk.get("device") or "—")),
        ("Model", f"{str(disk.get('vendor') or '').strip()} {disk.get('model') or ''}".strip()),
        ("Serial number", str(disk.get("serial") or disk.get("ui_serial") or "—")),
        ("Firmware", str(disk.get("firm") or "—")),
        ("Size", size(disk.get("size_total"))),
        ("Type", disk_type(disk)),
        ("Temperature", f"{disk.get('temp')}°C" if disk.get("temp") is not None else "—"),
        ("Status", str(disk.get("overview_status") or disk.get("status") or "—")),
        ("S.M.A.R.T. status", str(disk.get("smart_status") or "—")),
        ("Bad sectors", _metric(disk.get("unc"))),
        (
            "Remaining life",
            _metric(disk.get("remain_life"))
            + ("%" if _metric(disk.get("remain_life")) != "—" else ""),
        ),
        ("Used for", role),
        (
            "Compatibility",
            "not on Synology's compatibility list"
            if disk.get("compatibility") == "not_in_support"
            else str(disk.get("compatibility") or "—"),
        ),
        ("Notes", ", ".join(notes) or "none"),
    ]


async def list_disks(client: DsmClient) -> str:
    info = await load_info(client, "List disks")
    disks = items(info, "disks")
    if not disks:
        if (info.get("env") or {}).get("model_name") == "VirtualDSM":
            return "No disks reported. Virtual DSM has no physical disks."
        return "No disks reported."
    optional = Sections("List disks")
    tasks = await optional.get(client, "Running tests", STORAGE_API, "get_sche_task_list")
    roles = disk_roles(info)
    blocks = []
    for disk in disks:
        pairs = disk_pairs(disk, roles.get(str(disk.get("id")), "—"), tasks)
        pairs.extend(other_fields(disk, _KNOWN))
        blocks.append(format_key_value(pairs, title=disk_label(disk)))
    return "\n\n".join(blocks) + notes_block(optional.notes)


def resolve_disk(info: dict[str, Any], disk: str, operation: str) -> dict[str, Any]:
    wanted = disk.strip().lower()
    for d in items(info, "disks"):
        names = {str(d.get(k) or "").lower() for k in ("id", "device", "name", "longName")}
        if wanted in names or "/dev/" + wanted in names:
            return d
    valid = [f"{d.get('id')} ({d.get('longName') or d.get('name')})" for d in items(info, "disks")]
    not_found(operation, "disk", "disk", disk, valid)
    raise AssertionError  # not_found raises


def trend_lines(health: Any) -> list[tuple[str, str]]:
    history = ((health or {}).get("healthInfo") or {}).get("history") or {}
    incremental = history.get("incremental") or {}
    pairs: list[tuple[str, str]] = []
    for key, label in _TREND.items():
        points = incremental.get(key)
        if not isinstance(points, list) or not points:
            continue
        values = [_metric(p[1]) if isinstance(p, list) and len(p) > 1 else "—" for p in points]
        if all(v == "—" for v in values):
            values_text = "not reported by this drive"
        else:
            values_text = ", ".join(values)
        pairs.append((f"  {label}", values_text))
    if pairs:
        pairs.insert(0, ("Health trend", "monthly, oldest first (— = no reading)"))
    return pairs


def smart_table(reply: Any) -> str:
    rows = [
        [
            str(a.get("id")),
            str(a.get("name")),
            str(a.get("current")),
            str(a.get("worst")),
            str(a.get("threshold")),
            str(a.get("raw")),
            str(a.get("status")),
        ]
        for a in ((reply or {}).get("smartInfo") or [])
        if isinstance(a, dict)
    ]
    if not rows:
        return ""
    return format_table(
        ["ID", "Attribute", "Current", "Worst", "Threshold", "Raw", "Status"],
        rows,
        title="S.M.A.R.T. attributes",
    )


def test_lines(log: Any) -> list[tuple[str, str]]:
    if not isinstance(log, dict):
        return []
    pairs = [
        ("Last quick test", str(log.get("quick_last") or "never") + " (NAS time)"),
        ("Last extended test", str(log.get("extend_last") or "never") + " (NAS time)"),
    ]
    for info in log.get("testInfo") or []:
        pairs.append(("  quick result", str(info.get("quick") or "—")))
        pairs.append(("  extended result", str(info.get("extend") or "—")))
        for key in ("quickTime", "extendTime"):
            if str(info.get(key) or "").strip():
                pairs.append((f"  {key}", str(info[key]).strip()))
        if info.get("testing"):
            pairs.append(
                ("  testing now", f"yes, {info.get('remain') or 'remaining time unknown'}")
            )
    return pairs


def history_table(reply: Any) -> str:
    rows = [
        [str(e.get("time")), str(e.get("type")), str(e.get("test_type")), str(e.get("result"))]
        for e in ((reply or {}).get("testLog") or [])
        if isinstance(e, dict)
    ]
    if not rows:
        return ""
    return format_table(["Time (NAS)", "Kind", "Test", "Result"], rows, title="Recent tests")


async def get_disk(client: DsmClient, disk: str, history_limit: int = 10) -> str:
    operation = "Get disk"
    info = await load_info(client, operation)
    d = resolve_disk(info, disk, operation)
    device = str(d.get("device") or f"/dev/{d.get('id')}")
    optional = Sections(operation)
    tasks = await optional.get(client, "Running tests", STORAGE_API, "get_sche_task_list")
    health = await optional.get(
        client, "Health trend", _SMART, "get_health_info", {"device": device}
    )
    smart = await optional.get(
        client, "S.M.A.R.T. attributes", _SMART, "get_smart_info", {"device": device}
    )
    warnings = await optional.get(
        client, "S.M.A.R.T. warnings", _SMART, "smart_warning_get", {"device": device}
    )
    tests = await optional.get(
        client, "S.M.A.R.T. test results", _DISK, "get_smart_test_log", {"device": device}
    )
    log = await optional.get(
        client,
        "Test history",
        _DISK,
        "disk_test_log_get",
        {
            "device": device,
            "offset": 0,
            "limit": max(1, min(history_limit, 100)),
            "sort_by": "time",
            "sort_direction": "DESC",
        },
    )
    perf = await optional.get(
        client, "Test state", _DISK, "get_performance_test_status", {"device": device}
    )
    wcache = await optional.get(client, "Write cache", _DISK, "wcache_get", {"device": device})
    prediction = await optional.get(
        client, "Health prediction", _DISK, "disk_health_prediction_log_get"
    )

    pairs = disk_pairs(d, disk_roles(info).get(str(d.get("id")), "—"), tasks)
    if isinstance(wcache, dict) and "wcache_en" in wcache:
        pairs.append(("Write cache", "on" if wcache["wcache_en"] else "off"))
    if isinstance(warnings, list):
        pairs.append(("S.M.A.R.T. warnings", ", ".join(map(value_text, warnings)) or "none"))
    pairs.extend(test_lines(tests))
    if isinstance(perf, dict):
        pairs.extend((f"Test state: {k}", value_text(v)) for k, v in perf.items())
    pairs.extend(trend_lines(health))
    overview = (
        ((health or {}).get("healthInfo") or {}).get("overview")
        if isinstance(health, dict)
        else None
    )
    if isinstance(overview, dict):
        pairs.extend(
            other_fields(overview, set(d) | {"compatibility"}, keep_ui=True, prefix="Health: ")
        )
    if isinstance(prediction, dict) and prediction:
        pairs.extend((f"Health prediction: {k}", value_text(v)) for k, v in prediction.items())
    pairs.extend(other_fields(d, _KNOWN, keep_ui=True))

    parts = [format_key_value(pairs, title=disk_label(d))]
    for table in (smart_table(smart), history_table(log)):
        if table:
            parts.append(table)
    return "\n\n".join(parts) + notes_block(optional.notes)
