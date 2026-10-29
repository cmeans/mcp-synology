"""get_storage_schedules: scrubbing, S.M.A.R.T. tests, reclamation, dedupe, TRIM."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcp_synology.core.formatting import format_key_value, weekly_plan_grid
from mcp_synology.modules.storage.helpers import (
    EPOCH_NOTE,
    STORAGE_API,
    Sections,
    notes_block,
    value_text,
    yes_no,
)
from mcp_synology.modules.storage.pools import when

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

OPERATION = "Get storage schedules"
_GLYPHS = {"1": "#", "0": "."}
_LEGEND = "Legend: # = may run   . = doesn't run"
_STYLES = {"quick": "quick", "extend": "extended"}
_RUN_OPTIONS = {"run_all_time": "any time", "run_setting_time": "only in the hours below"}


def _grid(plan: object, option: object) -> str:
    if option == "run_all_time" or not isinstance(plan, str):
        return ""
    return "\n".join(
        "    " + line for line in weekly_plan_grid(plan, _GLYPHS, _LEGEND).splitlines()
    )


def scrubbing_block(plan: Any) -> str:
    sched = (plan or {}).get("schedule") if isinstance(plan, dict) else None
    if not isinstance(sched, dict):
        return ""
    option = sched.get("run_time_option")
    pairs = [
        ("On", yes_no(sched.get("schedule_enabled"))),
        ("Every", f"{sched.get('interval')} months" if sched.get("interval") else "—"),
        ("Pools, in order", ", ".join(map(str, sched.get("sche_pool_order") or [])) or "—"),
        ("Next run", when(sched.get("next_start_time"))),
        ("Running now", yes_no(sched.get("sche_is_running"))),
        ("Runs", _RUN_OPTIONS.get(str(option), str(option))),
    ]
    pairs.extend(
        (k, value_text(v))
        for k, v in sched.items()
        if k
        not in (
            "schedule_enabled",
            "interval",
            "sche_pool_order",
            "next_start_time",
            "sche_is_running",
            "run_time_option",
            "schedule_plan",
            "start_time",
        )
    )
    text = format_key_value(pairs, title="Data scrubbing")
    grid = _grid(sched.get("schedule_plan"), option)
    return text + ("\n" + grid if grid else "")


def smart_block(scheduler: Any, tasks: Any) -> str:
    rows: list[tuple[str, str]] = []
    for item in (scheduler or {}).get("items") or [] if isinstance(scheduler, dict) else []:
        try:
            args = json.loads(item.get("app_args") or "{}")
        except (TypeError, ValueError):
            args = {}
        style = _STYLES.get(str(args.get("test_style")), "?")
        disks = args.get("selected_disks") or []
        target = (
            "all disks" if args.get("test_range") == "all" else ", ".join(map(str, disks)) or "—"
        )
        rows.append(
            (
                str(item.get("task_name")),
                f"{style} test of {target}, {'on' if item.get('enabled') else 'off'}, next "
                f"{item.get('next_trigger_time') or '—'} (NAS time)",
            )
        )
    running = (
        [
            f"{_STYLES.get(str(t.get('test_style')), t.get('test_style'))} test of "
            f"{', '.join(map(str, t.get('target') or []))}"
            for t in ((tasks or {}).get("smart") or {}).get("tasks") or []
            if t.get("status") == "running"
        ]
        if isinstance(tasks, dict)
        else []
    )
    if running:
        rows.append(("Running now", "; ".join(running)))
    if not rows:
        rows.append(("Tasks", "none"))
    return format_key_value(rows, title="S.M.A.R.T. tests")


def window_block(title: str, plan: object, option: object, extra: dict[str, Any]) -> str:
    pairs = [("Runs", _RUN_OPTIONS.get(str(option), str(option) if option else "—"))]
    pairs.extend((k, value_text(v)) for k, v in extra.items())
    grid = _grid(plan, option or "run_setting_time")
    return format_key_value(pairs, title=title) + ("\n" + grid if grid else "")


async def get_storage_schedules(client: DsmClient) -> str:
    optional = Sections(OPERATION)
    scrub = await optional.get(client, "Data scrubbing", STORAGE_API, "get_schedule_plan")
    reclaim = await optional.get(
        client, "Space reclamation", STORAGE_API, "get_space_reclaim_schedule"
    )
    tasks = await optional.get(client, "Storage tasks", STORAGE_API, "get_sche_task_list")
    scheduler = await optional.get(
        client, "S.M.A.R.T. tests", "SYNO.Storage.CGI.Smart.Scheduler", "list"
    )
    dedupe = await optional.get(
        client, "Deduplication", "SYNO.Storage.CGI.BtrfsDedupe", "get_schedule_plan"
    )

    blocks = [b for b in (scrubbing_block(scrub), smart_block(scheduler, tasks)) if b]
    if isinstance(reclaim, dict):
        blocks.append(
            window_block(
                "Space reclamation",
                reclaim.get("schedule_table"),
                reclaim.get("run_time_option"),
                {
                    k: v
                    for k, v in reclaim.items()
                    if k not in ("schedule_table", "run_time_option")
                },
            )
        )
    if isinstance(dedupe, dict):
        blocks.append(
            window_block(
                "Btrfs deduplication",
                dedupe.get("schedule_plan"),
                "run_setting_time",
                {k: v for k, v in dedupe.items() if k != "schedule_plan"},
            )
        )
    trim = ((tasks or {}).get("ssd_trim") or {}).get("tasks") if isinstance(tasks, dict) else None
    if trim is not None:
        blocks.append(
            format_key_value(
                [("Tasks", "; ".join(value_text(t) for t in trim) or "none")], title="SSD TRIM"
            )
        )
    if not blocks:
        blocks.append("No storage schedules could be read.")
    blocks.append("Snapshot schedules are per shared folder: use list_snapshots.")
    return "\n\n".join(blocks) + "\n\n" + EPOCH_NOTE + notes_block(optional.notes)
