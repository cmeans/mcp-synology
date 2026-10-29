"""Storage helpers: formatting and the shared call paths."""

from __future__ import annotations

import pytest

from mcp_synology.modules.storage.helpers import (
    cell,
    is_ui_field,
    percent,
    raid_name,
    size,
    status_text,
    to_int,
    usage,
    value_text,
    yes_no,
)


@pytest.mark.parametrize(
    ("device_type", "expected"),
    [
        ("raid_5", "RAID 5"),
        ("raid_10", "RAID 10"),
        ("raid_f1", "RAID F1"),
        ("shr_without_1", "SHR"),
        ("shr_2", "SHR-2"),
        ("basic", "Basic"),
        ("jbod", "JBOD"),
        ("", "—"),
        ("future", "future"),
    ],
)
def test_raid_name(device_type: str, expected: str) -> None:
    assert raid_name(device_type) == expected


def test_sizes_and_percentages() -> None:
    assert size("1024") == "1 KiB"
    assert size("n/a") == "n/a"
    # A missing size is "—", as usage() and percent() show it (QA R1 #3).
    assert size(None) == "—" and size("") == "—"
    assert cell(None) == "—" and cell("") == "—" and cell(0) == "0"
    assert to_int("x") is None
    assert percent("1", "4") == "25.0%"
    assert percent("1", "0") == "—"
    assert percent(None, "4") == "—"
    assert usage({}) == "—"
    assert usage({"total": "1024"}) == "1 KiB"


def test_value_text_and_yes_no() -> None:
    assert value_text(True) == "yes"
    assert value_text("") == "—"
    assert value_text(None) == "—"
    assert value_text({"a": 1}) == '{"a": 1}'
    assert value_text(3) == "3"
    assert yes_no("1") == "yes"
    assert yes_no(0) == "no"
    assert yes_no("maybe") == "maybe"


def test_status_text() -> None:
    assert status_text({"summary_status": "normal"}) == "normal"
    assert status_text({"status": "attention", "space_status": {"detail": "x"}}) == "attention, x"
    assert (
        status_text({"summary_status": "normal", "space_status": {"detail": "normal"}}) == "normal"
    )
    assert status_text({}) == "unknown"


def test_ui_fields() -> None:
    assert is_ui_field("show_assemble_btn")
    assert is_ui_field("can_do")
    assert is_ui_field("foo_btn_bar")
    assert not is_ui_field("status")
