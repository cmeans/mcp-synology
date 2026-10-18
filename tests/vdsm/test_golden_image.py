"""Unit tests for golden-image staleness detection (no container needed)."""

from __future__ import annotations

from tests.vdsm.config import BAKE_VERSION, ds_start_fallback_allowed
from tests.vdsm.golden_image import (
    download_station_skip_reason,
    photos_skip_reason,
    stale_golden_image_reason,
)


def test_current_bake_is_not_stale() -> None:
    assert stale_golden_image_reason({"bake_version": BAKE_VERSION}, "7.2.2") is None


def test_missing_bake_version_is_stale() -> None:
    reason = stale_golden_image_reason({"version": "7.2.2"}, "7.2.2")
    assert reason is not None
    assert f"predates bake v{BAKE_VERSION}" in reason
    assert ".vdsm/golden/dsm-7.2.2.tar.gz" in reason


def test_older_bake_version_is_stale() -> None:
    reason = stale_golden_image_reason({"bake_version": BAKE_VERSION - 1}, "7.1")
    assert reason is not None
    assert f"bake v{BAKE_VERSION - 1}" in reason


def test_non_int_bake_version_is_stale() -> None:
    assert stale_golden_image_reason({"bake_version": "2"}, "7.1") is not None


def test_ds_start_fallback_is_off_on_ci() -> None:
    assert ds_start_fallback_allowed({"CI": "true"}) is False
    assert ds_start_fallback_allowed({"GITHUB_ACTIONS": "true"}) is False


def test_ds_start_fallback_is_on_locally() -> None:
    assert ds_start_fallback_allowed({}) is True
    assert ds_start_fallback_allowed({"CI": ""}) is True
    assert ds_start_fallback_allowed({"CI": "false"}) is True


def test_ds_listed_in_image_but_missing_from_api_is_not_a_skip() -> None:
    # A stopped DS package drops its APIs from query.cgi: that's a broken
    # image, not a NAS without DS, so it must reach ds_backend_ready and fail.
    meta = {"dsm_packages": ["DownloadStation"]}
    assert download_station_skip_reason(meta, {"SYNO.API.Info"}) is None


def test_ds_listed_and_registered_is_not_a_skip() -> None:
    meta = {"dsm_packages": ["DownloadStation"]}
    assert download_station_skip_reason(meta, {"SYNO.DownloadStation.Task"}) is None


def test_ds_not_listed_in_image_skips() -> None:
    reason = download_station_skip_reason({"dsm_packages": []}, {"SYNO.API.Info"})
    assert reason is not None
    assert "not installed" in reason
    assert download_station_skip_reason({}, {"SYNO.API.Info"}) is not None


def test_photos_listed_in_image_but_missing_from_api_is_not_a_skip() -> None:
    meta = {"dsm_packages": ["DownloadStation", "SynologyPhotos"]}
    assert photos_skip_reason(meta, {"SYNO.API.Info"}) is None


def test_photos_registered_on_unlisted_nas_is_not_a_skip() -> None:
    assert photos_skip_reason({}, {"SYNO.Foto.Browse.Item"}) is None


def test_photos_not_listed_in_image_skips() -> None:
    reason = photos_skip_reason({"dsm_packages": ["DownloadStation"]}, {"SYNO.API.Info"})
    assert reason is not None
    assert "not installed" in reason
