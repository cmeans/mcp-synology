"""Tests for task handles and the in-memory task records."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.modules.filestation.tasks import (
    Handle,
    TaskRecord,
    TaskRecords,
    parse_handle,
)


def test_parse_handle_roundtrip() -> None:
    text = "delete:FileStation_179219158016597225"
    assert str(parse_handle(text)) == text
    for kind in ("copy", "move", "restore"):
        assert parse_handle(f"{kind}:X1").kind == kind
    assert parse_handle("move:X").api == "SYNO.FileStation.CopyMove"
    assert parse_handle("delete:X").api == "SYNO.FileStation.Delete"


def test_parse_handle_strips_decoration() -> None:
    h = parse_handle(" `delete:FileStation_6AD2AEEDA63770CD`. ")
    assert str(h) == "delete:FileStation_6AD2AEEDA63770CD"


@pytest.mark.parametrize("bad", ["search:X", "delete:", "delete:a/b", "FileStation_1", ""])
def test_parse_handle_rejects(bad: str) -> None:
    with pytest.raises(ToolError) as exc_info:
        parse_handle(bad)
    body = json.loads(str(exc_info.value))
    assert body["error"]["code"] == "invalid_parameter"
    assert body["error"]["param"] == "handle"


def _rec(n: int) -> TaskRecord:
    return TaskRecord(handle=Handle("delete", f"T{n}"), paths=[f"/s/{n}"])


def test_records_evict_oldest_beyond_max() -> None:
    records = TaskRecords(max_entries=2)
    for n in range(3):
        records.add(_rec(n))
    assert records.get(Handle("delete", "T0")) is None
    assert records.get(Handle("delete", "T2")) is not None


def test_records_expire_after_ttl() -> None:
    now = [0.0]
    records = TaskRecords(ttl=10.0, clock=lambda: now[0])
    records.add(_rec(1))
    assert records.get(Handle("delete", "T1")) is not None
    now[0] = 11.0
    assert records.get(Handle("delete", "T1")) is None


def test_set_final_without_record() -> None:
    records = TaskRecords()
    h = Handle("copy", "Z9")
    status: dict[str, Any] = {"finished": True}
    records.set_final(h, status)
    rec = records.get(h)
    assert rec is not None
    assert rec.final_status == status
