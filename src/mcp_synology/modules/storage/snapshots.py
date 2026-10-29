"""list_snapshots: a shared folder's snapshots, or every folder's count."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from mcp_synology.core.errors import ErrorCode
from mcp_synology.core.formatting import error_response, format_key_value, format_table
from mcp_synology.modules.storage.helpers import (
    Sections,
    call,
    json_list,
    not_found,
    notes_block,
    value_text,
    yes_no,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

OPERATION = "List snapshots"
_SNAPSHOT = "SYNO.Core.Share.Snapshot"
_SHARE_ADDITIONAL = json_list(["vol_path", "enable_snapshot", "snapshot_count"])
_SNAPSHOT_ADDITIONAL = json_list(["desc", "lock", "worm_lock", "schedule_snapshot"])
_KNOWN = {"time", "desc", "lock", "worm_lock", "schedule_snapshot"}

# "GMT+00-2026.10.28-15.04.00": DSM names a snapshot by its time, with the UTC offset.
_NAME = re.compile(
    r"GMT([+-])(\d{2}):?(\d{2})?-(\d{4})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})"
)


def snapshot_time(name: str) -> str:
    m = _NAME.fullmatch(name)
    if not m:
        return name
    sign, oh, om, *parts = m.groups()
    offset = timedelta(hours=int(oh), minutes=int(om or 0)) * (1 if sign == "+" else -1)
    year, month, day, hour, minute, second = (int(p) for p in parts)
    local = datetime(year, month, day, hour, minute, second, tzinfo=timezone(offset))
    return local.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def require_snapshot_api(client: DsmClient) -> int:
    if _SNAPSHOT not in client._api_cache:
        error_response(
            ErrorCode.API_NOT_FOUND,
            f"{OPERATION} failed: snapshots need the Snapshot Replication package.",
            retryable=False,
            suggestion="Install Snapshot Replication from Package Center, then retry.",
        )
    # v1 answers an empty list even when the share has snapshots; v2 lists them.
    return client.negotiate_version(_SNAPSHOT, min_version=2)


async def shares(client: DsmClient) -> list[dict[str, Any]]:
    data = await call(
        client, OPERATION, "SYNO.Core.Share", "list", {"additional": _SHARE_ADDITIONAL}
    )
    return [s for s in (data or {}).get("shares") or [] if isinstance(s, dict)]


def schedule_text(reply: Any) -> list[tuple[str, str]]:
    if not isinstance(reply, dict):
        return []
    if not reply.get("enable_snapshot_schedule"):
        return [("Schedule", "off")]
    sched = reply.get("schedule") or {}
    pairs = [("Schedule", f"on, next {sched.get('next_trigger_time') or '—'} (NAS time)")]
    pairs.extend((f"  {k}", value_text(v)) for k, v in sched.items() if k != "next_trigger_time")
    return pairs


async def _schedule(client: DsmClient, optional: Sections, name: str) -> Any:
    return await optional.get(
        client, f"{name} schedule", _SNAPSHOT, "get_schedule", {"name": json.dumps(name)}
    )


async def list_snapshots(
    client: DsmClient, share: str | None = None, offset: int = 0, limit: int = 50
) -> str:
    version = require_snapshot_api(client)
    all_shares = await shares(client)
    optional = Sections(OPERATION)

    if not share:
        rows = []
        for s in all_shares:
            name = str(s.get("name"))
            count = (s.get("snapshot_info") or {}).get("snapshot_count", "—")
            sched = await _schedule(client, optional, name)
            on = (
                "—"
                if not isinstance(sched, dict)
                else yes_no(sched.get("enable_snapshot_schedule"))
            )
            rows.append([name, str(s.get("vol_path") or "—"), str(count), on])
        table = format_table(
            ["Shared folder", "Volume", "Snapshots", "Schedule on"],
            rows,
            title="Snapshots per shared folder",
        )
        return table + notes_block(optional.notes)

    name = share.strip().strip("/")
    match = next((s for s in all_shares if str(s.get("name")) == name), None)
    if match is None:
        not_found(
            OPERATION, "shared folder", "share", share, [str(s.get("name")) for s in all_shares]
        )
    reply = await call(
        client,
        OPERATION,
        _SNAPSHOT,
        "list",
        {"name": json.dumps(name), "additional": _SNAPSHOT_ADDITIONAL, "offset": 0, "limit": -1},
        version=version,
    )
    snaps = [x for x in (reply or {}).get("snapshots") or [] if isinstance(x, dict)]
    snaps.sort(key=lambda x: snapshot_time(str(x.get("time"))), reverse=True)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    page = snaps[offset : offset + limit]

    pairs: list[tuple[str, str]] = [("Snapshots", str(len(snaps)))]
    pairs.extend(schedule_text(await _schedule(client, optional, name)))
    parts = [format_key_value(pairs, title=f"Snapshots of {name}")]
    if page:
        rows = []
        for snap in page:
            extra = {k: v for k, v in snap.items() if k not in _KNOWN}
            rows.append(
                [
                    snapshot_time(str(snap.get("time"))),
                    str(snap.get("time")),
                    str(snap.get("desc") or "—"),
                    yes_no(snap.get("lock")) if "lock" in snap else "—",
                    yes_no(snap.get("worm_lock")) if "worm_lock" in snap else "—",
                    "scheduled" if snap.get("schedule_snapshot") else "manual",
                    ", ".join(f"{k}: {value_text(v)}" for k, v in extra.items()) or "",
                ]
            )
        parts.append(
            format_table(
                ["Taken", "Name", "Description", "Locked", "Immutable", "Made", "Other"], rows
            )
        )
        if offset + limit < len(snaps):
            parts.append(
                f"Showing {offset + 1}–{offset + len(page)} of {len(snaps)}, newest "
                f"first. Use offset={offset + limit} for more."
            )
    elif snaps:
        parts.append(f"No snapshots at offset {offset} (there are {len(snaps)}).")
    return "\n\n".join(parts) + notes_block(optional.notes)
