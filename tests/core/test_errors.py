"""Tests for core/errors.py — exception hierarchy and error_from_code factory."""

from mcp_synology.core.errors import (
    ApiNotFoundError,
    AuthenticationError,
    DiskFullError,
    ErrorCode,
    FileStationError,
    IllegalNameError,
    PathNotFoundError,
    SessionExpiredError,
    SynologyError,
    SynologyFileExistsError,
    SynologyPermissionError,
    error_from_code,
)


class TestExceptionHierarchy:
    def test_base_exception_attributes(self) -> None:
        err = SynologyError("test error", code=100, suggestion="try again")
        assert str(err) == "test error"
        assert err.code == 100
        assert err.suggestion == "try again"

    def test_filestation_errors_inherit_from_synology_error(self) -> None:
        err = PathNotFoundError("not found", code=408)
        assert isinstance(err, FileStationError)
        assert isinstance(err, SynologyError)


class TestErrorFromCode:
    def test_common_permission_denied(self) -> None:
        err = error_from_code(105)
        assert isinstance(err, SynologyPermissionError)
        assert err.code == 105

    def test_session_expired_codes(self) -> None:
        for code in (106, 107, 119):
            err = error_from_code(code)
            assert isinstance(err, SessionExpiredError)
            assert err.code == code

    def test_api_not_found(self) -> None:
        err = error_from_code(102)
        assert isinstance(err, ApiNotFoundError)

    def test_filestation_path_not_found(self) -> None:
        err = error_from_code(408, api_name="SYNO.FileStation.List")
        assert isinstance(err, PathNotFoundError)
        assert err.code == 408
        assert err.suggestion is not None

    def test_filestation_file_exists(self) -> None:
        err = error_from_code(414, api_name="SYNO.FileStation.CopyMove")
        assert isinstance(err, SynologyFileExistsError)

    def test_filestation_disk_full(self) -> None:
        err = error_from_code(416, api_name="SYNO.FileStation.CopyMove")
        assert isinstance(err, DiskFullError)

    def test_filestation_illegal_name(self) -> None:
        for code in (418, 419):
            err = error_from_code(code, api_name="SYNO.FileStation.Rename")
            assert isinstance(err, IllegalNameError)

    def test_filestation_407_is_permission_denied(self) -> None:
        """File Station 407 (operation not permitted) is a permission refusal, not an
        unknown error (rc5 live test: list_files /home got DSM 407).
        """
        err = error_from_code(407, api_name="SYNO.FileStation.List")
        assert isinstance(err, SynologyPermissionError)
        assert err.error_code == ErrorCode.PERMISSION_DENIED
        assert err.code == 407
        assert "Unknown" not in str(err) and err.suggestion

    def test_auth_407_is_not_filestation(self) -> None:
        """Auth API 407 stays the lockout, not File Station's permission refusal."""
        err = error_from_code(407, api_name="SYNO.API.Auth")
        assert "login attempts" in str(err)

    def test_auth_402_is_not_filestation(self) -> None:
        """Auth API code 402 must NOT map to FileStation 'System too busy'."""
        err = error_from_code(402, api_name="SYNO.API.Auth")
        assert "Permission denied" in str(err) or "permission" in str(err).lower()
        assert "too busy" not in str(err).lower()
        assert err.code == 402

    def test_auth_400_bad_credentials(self) -> None:
        err = error_from_code(400, api_name="SYNO.API.Auth")
        assert isinstance(err, AuthenticationError)

    def test_auth_403_2fa_required(self) -> None:
        err = error_from_code(403, api_name="SYNO.API.Auth")
        assert isinstance(err, AuthenticationError)
        assert err.code == 403

    def test_auth_codes_not_applied_to_filestation(self) -> None:
        """FileStation code 408 should still be PathNotFoundError, not Auth."""
        err = error_from_code(408, api_name="SYNO.FileStation.List")
        assert isinstance(err, PathNotFoundError)

    def test_json_format_invalid_parameter(self) -> None:
        err = error_from_code(120, api_name="SYNO.DownloadStation2.Task")
        assert err.error_code == ErrorCode.INVALID_PARAMETER
        assert err.code == 120

    def test_unknown_code(self) -> None:
        err = error_from_code(9999)
        assert isinstance(err, SynologyError)
        assert err.code == 9999
        assert "9999" in str(err)


class TestDetachedCopy:
    """QA round 2 on #461: a failed login kept for other waiters keeps no frames."""

    def test_keeps_type_and_fields_drops_traceback_and_cause(self) -> None:
        from mcp_synology.core.errors import DsmHttpError, detached_copy

        def login(passwd: str) -> None:
            try:
                raise ValueError(passwd)
            except ValueError as inner:
                raise DsmHttpError("HTTP 502", 502, suggestion="s") from inner

        try:
            login("Hunter2x")
        except DsmHttpError as e:
            original = e
        copy = detached_copy(original)
        assert type(copy) is DsmHttpError and copy is not original
        assert (str(copy), copy.status, copy.retryable, copy.suggestion) == (
            "HTTP 502",
            502,
            True,
            "s",
        )
        assert copy.__traceback__ is None
        assert copy.__cause__ is None and copy.__context__ is None
        assert original.__traceback__ is not None  # the original is untouched
