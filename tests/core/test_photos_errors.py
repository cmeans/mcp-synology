"""Synology Photos error codes (#258): dispatch, error codes and the 120 detail."""

from __future__ import annotations

import pytest

from mcp_synology.core.errors import (
    ErrorCode,
    InvalidParameterError,
    SynologyError,
    error_from_code,
)
from mcp_synology.core.photos_errors import PHOTOS_ERROR_CODES, PhotosError


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (101, ErrorCode.INVALID_PARAMETER),
        (103, ErrorCode.NOT_SUPPORTED),
        (117, ErrorCode.NOT_FOUND),
        (160, ErrorCode.PERMISSION_DENIED),
        (602, ErrorCode.UNAVAILABLE),
        (620, ErrorCode.INVALID_PARAMETER),
        (621, ErrorCode.NOT_FOUND),
        (642, ErrorCode.NOT_FOUND),
        (801, ErrorCode.PERMISSION_DENIED),
        (803, ErrorCode.PERMISSION_DENIED),
        (901, ErrorCode.UNAVAILABLE),
        (950, ErrorCode.INVALID_PARAMETER),
        (953, ErrorCode.INVALID_PARAMETER),
    ],
)
@pytest.mark.parametrize("api", ["SYNO.Foto.Browse.Item", "SYNO.FotoTeam.Browse.Item"])
def test_photos_codes_map_to_their_error_code(code: int, expected: ErrorCode, api: str) -> None:
    exc = error_from_code(code, api)
    assert isinstance(exc, PhotosError)
    assert exc.error_code is expected
    assert exc.code == code
    assert exc.suggestion


def test_every_code_has_message_and_suggestion() -> None:
    for code, (_error_code, message, suggestion) in PHOTOS_ERROR_CODES.items():
        assert message.strip() and suggestion.strip(), code


def test_setup_errors_name_the_fix() -> None:
    assert "Application Privileges" in (error_from_code(160, "SYNO.Foto.UserInfo").suggestion or "")
    assert "User Home" in (error_from_code(602, "SYNO.Foto.Browse.Folder").suggestion or "")
    assert "Shared Space" in (error_from_code(801, "SYNO.FotoTeam.Browse.Folder").suggestion or "")


def test_photos_codes_only_for_photos_apis() -> None:
    """Photos' 117/160 mean nothing to File Station; 105 stays the common handler."""
    assert not isinstance(error_from_code(160, "SYNO.FileStation.List"), PhotosError)
    exc = error_from_code(105, "SYNO.Foto.Browse.Item")
    assert not isinstance(exc, PhotosError)
    assert exc.error_code is ErrorCode.PERMISSION_DENIED


def test_unmapped_photos_code_keeps_the_number() -> None:
    exc = error_from_code(622, "SYNO.Foto.Upload.Item")
    assert type(exc) is SynologyError
    assert exc.code == 622
    assert exc.error_code is ErrorCode.DSM_ERROR


def test_120_names_the_parameter() -> None:
    exc = error_from_code(120, "SYNO.Foto.Browse.Item", {"name": "rating", "reason": "condition"})
    assert isinstance(exc, InvalidParameterError)
    assert str(exc).endswith(": rating (condition)")


def test_detail_accepts_a_list_and_ignores_junk() -> None:
    exc = error_from_code(120, "SYNO.DownloadStation2.Task", [{"name": "a"}, "x", {"no": 1}])
    assert str(exc).endswith(": a")
    assert ":" not in str(error_from_code(120, "SYNO.X", "junk"))


def test_101_from_photos_names_the_parameter_when_given() -> None:
    exc = error_from_code(101, "SYNO.Foto.Browse.Item", {"name": "gps"})
    assert exc.error_code is ErrorCode.INVALID_PARAMETER
    assert str(exc).endswith(": gps")


def test_reason_only_detail_from_a_capture() -> None:
    """A 1.9.1 favorite filter below v7 answers ``{"reason": "favorite is not supported"}``
    with no name (fav191_lwf-favorite-v2 in the capture fixtures)."""
    import json
    from pathlib import Path

    capture = json.loads(
        (
            Path(__file__).parents[1] / "modules/photos/fixtures/dsm-7.2.2-photos-1.9.1.json"
        ).read_text()
    )
    error = capture["fav191_lwf-favorite-v2"]["response"]["error"]
    exc = error_from_code(error["code"], "SYNO.Foto.Browse.Item", error.get("errors"))
    assert str(exc).endswith(": favorite is not supported")


def test_detail_uses_only_name_and_reason() -> None:
    exc = error_from_code(120, "SYNO.X", {"name": "a", "reason": "type", "token": "secret"})
    assert "secret" not in str(exc)
