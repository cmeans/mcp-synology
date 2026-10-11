"""Tests for modules/downloadstation/helpers.py — pure-function helpers."""

from __future__ import annotations

import pytest

from mcp_synology.modules.downloadstation.helpers import (
    format_eta,
    format_speed,
    format_task_status,
    format_transfer_progress,
)


class TestFormatScheduleGrid:
    """Plan format verified from DSM's own SYNO.ux.ScheduleTable (#123 QA r1):
    day-major Sun..Sat, 24 chars/day; 0 = no download, 1 = full, 2 = limited.
    """

    def test_all_full_speed_has_no_off_or_limited_cells(self) -> None:
        from mcp_synology.modules.downloadstation.helpers import format_schedule_grid

        out = format_schedule_grid("1" * 168)
        grid_lines = [ln for ln in out.splitlines() if ln[:3] in {"Sun", "Mon", "Sat"}]
        assert grid_lines
        assert all("." not in ln and "~" not in ln for ln in grid_lines)

    def test_day_major_sunday_first(self) -> None:
        from mcp_synology.modules.downloadstation.helpers import format_schedule_grid

        # Sunday all limited, everything else no-download.
        out = format_schedule_grid("2" * 24 + "0" * 144)
        sun = next(ln for ln in out.splitlines() if ln.startswith("Sun"))
        mon = next(ln for ln in out.splitlines() if ln.startswith("Mon"))
        assert sun.count("~") == 24
        assert "~" not in mon and mon.count(".") == 24

    def test_legend_matches_dsm_wording(self) -> None:
        from mcp_synology.modules.downloadstation.helpers import format_schedule_grid

        out = format_schedule_grid("0" * 168)
        assert "# = full speed" in out
        assert "~ = limited (alt speed)" in out
        assert ". = no download" in out

    def test_three_is_not_a_valid_cell(self) -> None:
        """'3' was an invented on+eMule value; DSM's widget only knows 0/1/2."""
        from mcp_synology.modules.downloadstation.helpers import format_schedule_grid

        out = format_schedule_grid("3" + "1" * 167)
        sun = next(ln for ln in out.splitlines() if ln.startswith("Sun"))
        assert sun.split()[1] == "?"

    def test_wrong_length_renders_with_malformed_note_instead_of_raising(self) -> None:
        """DS2 Scheduler.set does no server-side validation, so a stored plan
        can be any length; reading it must never error (#123 QA r1 finding 1).
        """
        from mcp_synology.modules.downloadstation.helpers import format_schedule_grid

        out = format_schedule_grid("1" * 100)
        assert "stored plan is malformed (100 chars, expected 168)" in out
        # Missing hours render as '?', not as a crash.
        sat = next(ln for ln in out.splitlines() if ln.startswith("Sat"))
        assert sat.split()[1:] == ["?"] * 24

    def test_out_of_range_chars_render_as_question_mark_with_note(self) -> None:
        from mcp_synology.modules.downloadstation.helpers import format_schedule_grid

        out = format_schedule_grid("9" * 168)
        assert "stored plan is malformed" in out
        sun = next(ln for ln in out.splitlines() if ln.startswith("Sun"))
        assert sun.split()[1:] == ["?"] * 24


class TestValidateSchedulePlan:
    def test_accepts_168_chars_of_012(self) -> None:
        from mcp_synology.modules.downloadstation.helpers import schedule_plan_problem

        assert schedule_plan_problem("012" * 56) is None

    def test_rejects_wrong_length(self) -> None:
        from mcp_synology.modules.downloadstation.helpers import schedule_plan_problem

        problem = schedule_plan_problem("1" * 167)
        assert problem is not None
        assert "168" in problem and "167" in problem

    def test_rejects_three(self) -> None:
        from mcp_synology.modules.downloadstation.helpers import schedule_plan_problem

        problem = schedule_plan_problem("3" + "1" * 167)
        assert problem is not None
        assert "'3'" in problem


class TestFormatTaskStatus:
    """DS v1/v2/v3 Task APIs return status as a string (vdsm-verified, #123)."""

    @pytest.mark.parametrize(
        "status",
        [
            "waiting",
            "downloading",
            "paused",
            "finishing",
            "finished",
            "hash_checking",
            "seeding",
            "filehosting_waiting",
            "extracting",
            "error",
        ],
    )
    def test_known_string_statuses_pass_through(self, status: str) -> None:
        assert format_task_status(status) == status

    def test_unknown_string_preserved_in_diagnostic(self) -> None:
        assert format_task_status("brand_new_state") == "unknown(brand_new_state)"

    def test_int_is_not_mapped(self) -> None:
        """The int map was dropped: no consumer reads DS2 task status (#123 r3)."""
        assert format_task_status(2) == "unknown(2)"

    def test_none_status_returns_unknown(self) -> None:
        assert format_task_status(None) == "unknown"


class TestStatusGroups:
    def test_groups_are_keyed_on_string_labels(self) -> None:
        from mcp_synology.modules.downloadstation.helpers import STATUS_GROUPS

        assert STATUS_GROUPS["downloading"] == {
            "waiting",
            "downloading",
            "finishing",
            "hash_checking",
            "filehosting_waiting",
            "extracting",
        }
        assert STATUS_GROUPS["finished"] == {"finished", "seeding"}
        assert STATUS_GROUPS["paused"] == {"paused"}
        assert STATUS_GROUPS["error"] == {"error"}


class TestFormatTransferProgress:
    def test_zero_size(self) -> None:
        assert format_transfer_progress(downloaded=0, total=0) == "0 B / 0 B (—)"

    def test_partial(self) -> None:
        # 512 MB / 1 GB = exactly 50% (512 * 1024 * 1024 / 1024 * 1024 * 1024)
        out = format_transfer_progress(downloaded=512 * 1024 * 1024, total=1024 * 1024 * 1024)
        assert "(50%)" in out
        # Tightened beyond plan: assert the unit too so a future change to
        # format_size's boundary doesn't pass silently.
        assert "512 MB" in out

    def test_complete(self) -> None:
        out = format_transfer_progress(downloaded=1000, total=1000)
        assert "(100%)" in out

    def test_total_smaller_than_downloaded_clamps_to_100(self) -> None:
        out = format_transfer_progress(downloaded=2000, total=1000)
        assert "(100%)" in out


class TestFormatSpeed:
    def test_zero_renders_em_dash(self) -> None:
        assert format_speed(0) == "—"

    def test_negative_renders_em_dash(self) -> None:
        assert format_speed(-100) == "—"

    def test_positive_renders_size_per_sec(self) -> None:
        out = format_speed(1024 * 1024)
        assert "/s" in out
        assert "1" in out  # 1 MB rendered as "1 MB" by format_size


class TestFormatEta:
    def test_zero_speed_returns_em_dash(self) -> None:
        assert format_eta(downloaded=0, total=1000, speed=0) == "—"

    def test_negative_speed_returns_em_dash(self) -> None:
        assert format_eta(downloaded=0, total=1000, speed=-5) == "—"

    def test_downloaded_equals_total_returns_em_dash(self) -> None:
        assert format_eta(downloaded=1000, total=1000, speed=10) == "—"

    def test_downloaded_exceeds_total_returns_em_dash(self) -> None:
        # DSM occasionally over-reports during seed-after-finish; should not negative ETA
        assert format_eta(downloaded=2000, total=1000, speed=10) == "—"

    def test_under_one_minute_renders_seconds(self) -> None:
        # 30 bytes remaining at 1 B/s → 30s
        assert format_eta(downloaded=0, total=30, speed=1) == "30s"

    def test_under_one_hour_renders_minutes(self) -> None:
        # 600 bytes remaining at 1 B/s → 600s → 10m
        assert format_eta(downloaded=0, total=600, speed=1) == "10m"

    def test_under_one_day_renders_hours_minutes(self) -> None:
        # 7200s = 2h0m
        assert format_eta(downloaded=0, total=7200, speed=1) == "2h0m"
        # 7320s = 2h2m
        assert format_eta(downloaded=0, total=7320, speed=1) == "2h2m"

    def test_one_day_or_more_renders_days_hours(self) -> None:
        # 86400s = exactly 1 day → 1d0h
        assert format_eta(downloaded=0, total=86400, speed=1) == "1d0h"
        # 90000s = 1d1h
        assert format_eta(downloaded=0, total=90000, speed=1) == "1d1h"
