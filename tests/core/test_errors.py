"""Tests for core/errors.py — exception hierarchy and error_from_code factory."""

import json
import re

import pytest

from mcp_synology.core.errors import (
    FILESTATION_ERROR_CODES,
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
        """Auth API 407 stays the blocked address, not File Station's permission refusal."""
        err = error_from_code(407, api_name="SYNO.API.Auth")
        assert "IP address is blocked" in str(err)

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

    def test_a_type_it_cannot_rebuild_gives_none(self) -> None:
        """pydantic's ValidationError takes its own constructor arguments: no copy, and
        the caller keeps the original rather than a TypeError (pre-QA on QA round 2)."""
        from pydantic import BaseModel, ValidationError

        from mcp_synology.core.errors import detached_copy

        class Model(BaseModel):
            n: int

        try:
            Model(n="x")  # type: ignore[arg-type]
        except ValidationError as e:
            assert detached_copy(e) is None


class TestFileStationRetryableAgreesWithItsAdvice:
    """#490: a code whose advice is to wait and retry is retryable, and no other is: a model
    reads the flag before the prose. 402 and 421 say to wait and retry; 401 (unknown) says to
    check the logs; 415 and 416 need space freed first, so the same call would fail again."""

    def test_the_retryable_codes(self) -> None:
        retryable = {
            code
            for code in FILESTATION_ERROR_CODES
            if error_from_code(code, "SYNO.FileStation.CopyMove").retryable
        }
        assert retryable == {402, 421}

    @pytest.mark.parametrize("code", sorted(FILESTATION_ERROR_CODES))
    def test_advice_and_flag_agree(self, code: int) -> None:
        error = error_from_code(code, "SYNO.FileStation.CopyMove")
        _, suggestion = FILESTATION_ERROR_CODES[code]
        # "Wait and retry" or a bare "Retry …" is advice to send the same call again; "then
        # retry" after a fix (1200's "check both … then retry") isn't.
        says_retry = re.search(r"(?:^|\. )(?:wait and )?retry\b", suggestion, re.IGNORECASE)
        assert bool(says_retry) is error.retryable, (code, suggestion)


class TestFileStationCause:
    """#726, #749: a wrapper code's cause in DSM's ``errors`` (CreateFolder 1100 and Rename
    1200 with 414 inside, vdsm 7.1, 7.2.2, 7.3.2 and 7.4.1) is named, and the error keeps the
    wrapper's DSM code and Python type; since #752 its envelope code is the cause's."""

    @pytest.mark.parametrize("wrapper", [900, 1000, 1001, 1002, 1100, 1200])
    def test_a_wrapper_names_its_cause(self, wrapper: int) -> None:
        from mcp_synology.core.errors import FileStationError

        err = error_from_code(
            wrapper, "SYNO.FileStation.CreateFolder", [{"code": 414, "path": "/s/x"}]
        )
        assert type(err) is FileStationError, type(err)
        assert (err.code, err.cause) == (wrapper, 414)
        label = FILESTATION_ERROR_CODES[wrapper][0]
        assert str(err) == f"{label}: File already exists (DSM error 414)", str(err)
        assert err.suggestion is not None and "overwrite=true" in err.suggestion

    @pytest.mark.parametrize(
        "errors",
        [
            None,
            [],
            {},
            [{"path": "/s/x"}],
            [{"code": "414"}],  # not a number
            [{"code": True}],  # True == 1, no File Station code
            [{"code": 9999}],  # not a File Station code
            [{"code": 1100}],  # a wrapper isn't a cause
            [{"code": 599}],  # nor a task's, a search's or an upload's code (QA r1 on #751)
            [{"code": 600}],
            [{"code": 1800}],
            ["414"],
            {"name": "path", "reason": "x"},
        ],
    )
    def test_no_cause_leaves_the_wrapper_as_it_was(self, errors: object) -> None:
        err = error_from_code(1100, "SYNO.FileStation.CreateFolder", errors)
        assert err.cause is None
        assert str(err) == "Failed to create the folder"

    @pytest.mark.parametrize("wrapper", [900, 1000, 1001, 1002, 1100, 1200])
    @pytest.mark.parametrize(
        "cause",
        sorted(c for c in FILESTATION_ERROR_CODES if c < 900),
    )
    def test_the_causes_advice_and_flag_agree(self, wrapper: int, cause: int) -> None:
        """Pre-QA r1 on #749: a busy cause's "wait and retry" came beside the wrapper's
        retryable false."""
        error = error_from_code(wrapper, "SYNO.FileStation.CopyMove", [{"code": cause}])
        assert error.suggestion is not None
        says_retry = re.search(r"(?:^|\. )(?:wait and )?retry\b", error.suggestion, re.I)
        assert bool(says_retry) is error.retryable, (wrapper, cause, error.suggestion)

    @pytest.mark.parametrize("wrapper", [900, 1000, 1001, 1002, 1100, 1200])
    @pytest.mark.parametrize(
        "cause",
        sorted(c for c in FILESTATION_ERROR_CODES if c < 900),
    )
    def test_the_envelope_code_is_the_causes(self, wrapper: int, cause: int) -> None:
        """#752: a 416 inside a 1100 is `disk_full`, as a bare 416 is, with its help link;
        the DSM code and the Python type stay the wrapper's, which callers test."""
        wrapped = error_from_code(wrapper, "SYNO.FileStation.CopyMove", [{"code": cause}])
        bare = error_from_code(cause, "SYNO.FileStation.CopyMove")
        plain = error_from_code(wrapper, "SYNO.FileStation.CopyMove")
        assert wrapped.error_code == bare.error_code, (wrapper, cause)
        assert (wrapped.code, type(wrapped)) == (wrapper, type(plain))
        assert plain.error_code == "filestation_error"

    @pytest.mark.parametrize(
        ("cause", "code"),
        [
            (407, "permission_denied"),
            (408, "not_found"),
            (414, "already_exists"),
            (415, "disk_full"),
            (416, "disk_full"),
            (418, "invalid_parameter"),
        ],
    )
    def test_the_envelope_names_the_causes_code_and_help(self, cause: int, code: str) -> None:
        """#752, through the reply: `code` and `help_url` follow the cause."""
        from mcp.server.mcpserver.exceptions import ToolError

        from mcp_synology.core.formatting import synology_error_response

        error = error_from_code(1100, "SYNO.FileStation.CreateFolder", [{"code": cause}])
        with pytest.raises(ToolError) as exc_info:
            synology_error_response("Create folder", error)
        envelope = json.loads(str(exc_info.value))["error"]
        assert envelope["code"] == code
        assert envelope["help_url"].endswith(f"#{code}")
        assert f"(DSM error {cause})" in envelope["message"]

    def test_errors_for_log_names_codes_and_paths(self) -> None:
        """QA r1 on #751: the DEBUG line read only Photos' name and reason."""
        from mcp_synology.core.errors import errors_for_log

        assert errors_for_log([{"code": 414, "path": "/s/x"}, {"code": 999}]) == (
            " [errors: 414 /s/x, 999]"
        )
        assert errors_for_log({"code": 408}) == " [errors: 408]"
        assert errors_for_log(None) == errors_for_log([{"name": "rating"}]) == ""

    def test_the_first_known_cause_counts(self) -> None:
        err = error_from_code(
            1200, "SYNO.FileStation.Rename", [{"code": 9999}, {"code": 408}, {"code": 414}]
        )
        assert err.cause == 408

    def test_a_dict_cause_counts_too(self) -> None:
        err = error_from_code(1100, "SYNO.FileStation.CreateFolder", {"code": 414})
        assert err.cause == 414

    def test_a_code_that_isnt_a_wrapper_ignores_errors(self) -> None:
        err = error_from_code(408, "SYNO.FileStation.List", [{"code": 414}])
        assert isinstance(err, PathNotFoundError)
        assert err.cause is None and str(err) == "No such file or directory"

    def test_other_apis_ignore_it(self) -> None:
        err = error_from_code(1100, "SYNO.API.Info", [{"code": 414}])
        assert err.cause is None

    def test_the_414_advice_doesnt_assume_overwrite(self) -> None:
        """#726: create_folder and rename take no `overwrite`."""
        err = error_from_code(414, "SYNO.FileStation.CreateFolder")
        assert err.suggestion is not None
        assert err.suggestion.startswith("Something with that name is already there")
        assert "where the tool takes overwrite" in err.suggestion

    def test_1100_and_1101_say_what_the_guide_says(self) -> None:
        """#749: 1100 read as a permissions problem, 1101 as an unsupported file system."""
        assert str(error_from_code(1100, "SYNO.FileStation.CreateFolder")) == (
            "Failed to create the folder"
        )
        assert str(error_from_code(1101, "SYNO.FileStation.CreateFolder")) == (
            "Too many folders in the parent folder"
        )
        # 900 read as a crash with a cause (pre-QA r1 and r2 on #749).
        assert str(error_from_code(900, "SYNO.FileStation.Delete")) == "Failed to delete"
