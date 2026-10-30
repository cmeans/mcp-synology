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


def test_every_dsm_version_has_a_bake_timezone() -> None:
    # The bake refuses a version without one (#342), as it does one without a Photos
    # package list; the zones are listzone values seen on vdsm 7.1 through 7.4.1.
    from tests.vdsm.config import DSM_TIMEZONES
    from tests.vdsm.setup_dsm import PHOTOS_PACKAGES

    assert set(DSM_TIMEZONES) == set(PHOTOS_PACKAGES)
    assert "GMT" in DSM_TIMEZONES.values()  # one image covers UTC


def test_every_baked_dsm_version_has_an_update() -> None:
    # Bake v9 installs each baked version's latest update; a version CI tests without
    # one would silently run the bare release. The pack URL is the build's own.
    from tests.vdsm.config import DSM_TIMEZONES, DSM_VERSIONS

    for version in DSM_TIMEZONES:
        info = DSM_VERSIONS[version]
        assert info.update > 0, version
        assert f"/update_pack/{info.build}-{info.update}/" in info.update_url
        assert f"/{info.build}/DSM_VirtualDSM_{info.build}.pat" in info.pat_url
    assert DSM_VERSIONS["7.1"].pat_url.endswith("/7.1.1/42962/DSM_VirtualDSM_42962.pat")


def test_vdsm_image_is_pinned() -> None:
    from tests.vdsm.config import VDSM_IMAGE

    repo, _, tag = VDSM_IMAGE.partition(":")
    assert repo == "vdsm/virtual-dsm"
    assert tag
    assert tag != "latest"


def test_container_boots_the_pinned_image() -> None:
    # The pin only holds if the container uses it; nothing else names the image.
    import inspect

    from tests.vdsm import container

    source = inspect.getsource(container)
    assert "DockerContainer(VDSM_IMAGE)" in source
    assert source.count("DockerContainer(") == 1
