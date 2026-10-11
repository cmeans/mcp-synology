"""Unit tests for golden-image staleness detection (no container needed)."""

from __future__ import annotations

from tests.vdsm.config import BAKE_VERSION, ds_start_fallback_allowed
from tests.vdsm.golden_image import stale_golden_image_reason


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
