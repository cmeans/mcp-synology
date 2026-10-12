"""Tests for DOWNLOADSTATION_ERROR_CODES dispatch.

DS overloads the 400-series with different semantics than FileStation, so the
typed exception raised must depend on both the numeric code AND the source API.
"""

from __future__ import annotations

import pytest

from mcp_synology.core.downloadstation_errors import (
    DOWNLOADSTATION_ERROR_CODES,
    DownloadStationError,
)
from mcp_synology.core.errors import (
    SessionExpiredError,
    SynologyError,
    SynologyPermissionError,
    error_from_code,
)


class TestDownloadstationErrorCodes:
    @pytest.mark.parametrize(
        ("code", "expected_in_message"),
        [
            (400, "upload failed"),
            (401, "max number of tasks"),
            (402, "destination denied"),
            (403, "destination doesn't exist"),
            (404, "task id"),
            (405, "invalid task action"),
            (406, "no default destination"),
            (407, "set destination failed"),
            (408, "file doesn't exist"),
        ],
    )
    def test_known_codes_have_actionable_message(self, code: int, expected_in_message: str) -> None:
        message, suggestion = DOWNLOADSTATION_ERROR_CODES[code]
        combined = f"{message} {suggestion}".lower()
        assert expected_in_message.lower() in combined
        assert suggestion.strip(), f"code {code} has empty suggestion"

    def test_405_example_is_one_dsm_really_answers_with_405(self) -> None:
        """Pausing a finished task is a whole-call 101, not 405 (probed in #146), so
        the suggestion mustn't use it as the example (QA on #153)."""
        _, suggestion = DOWNLOADSTATION_ERROR_CODES[405]
        assert "pausing a finished task" not in suggestion
        assert "already running" in suggestion

    def test_all_codes_400_through_408_present(self) -> None:
        for code in range(400, 409):
            assert code in DOWNLOADSTATION_ERROR_CODES, f"missing code {code}"


class TestErrorFromCodeDispatchesDsApi:
    def test_ds_api_400_routes_to_download_error_not_filestation(self) -> None:
        exc = error_from_code(400, "SYNO.DownloadStation.Task")
        assert isinstance(exc, DownloadStationError)
        assert "upload" in str(exc).lower()

    def test_filestation_api_400_still_uses_filestation_map(self) -> None:
        exc = error_from_code(400, "SYNO.FileStation.List")
        assert "invalid" in str(exc).lower()

    def test_unknown_ds_code_falls_back_to_generic_synology_error(self) -> None:
        # Codes not in any map should produce a generic SynologyError, NOT a
        # DownloadStationError — the DS branch must fall through cleanly when
        # the code isn't DS-specific.
        exc = error_from_code(999, "SYNO.DownloadStation.Task")
        assert isinstance(exc, SynologyError)
        assert not isinstance(exc, DownloadStationError)
        assert "999" in str(exc)

    def test_ds_api_105_routes_to_permission_error_not_session_expired(self) -> None:
        # CLAUDE.md invariant: code 105 (permission denied) must NOT be a
        # session error — never trigger re-auth. The DS branch in
        # error_from_code() must fall through to the common 100-series
        # handling so that 105 on a DS API still maps to SynologyPermissionError.
        exc = error_from_code(105, "SYNO.DownloadStation.Task")
        assert isinstance(exc, SynologyPermissionError)
        assert not isinstance(exc, SessionExpiredError)
        assert not isinstance(exc, DownloadStationError)

    def test_ds_api_106_routes_to_session_expired(self) -> None:
        # Symmetric guard: 106 is a session error and must route there for DS
        # APIs too, so the transparent re-auth path in DsmClient.request() fires
        # the same way it does for FileStation.
        exc = error_from_code(106, "SYNO.DownloadStation.Task")
        assert isinstance(exc, SessionExpiredError)
        assert not isinstance(exc, DownloadStationError)
