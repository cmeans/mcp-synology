"""Tests for scripts/scrub_nas_capture.py, and a guard on every real-NAS fixture (#428).

The sample data below is invented; never paste values from a real capture here.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "scrub_nas_capture.py"
# Fixtures captured on the maintainer's own NAS are named *-realnas.json and found by glob.
REAL_NAS_FIXTURES = sorted(ROOT.glob("tests/**/*-realnas.json"))
# Real-NAS fixtures from before the scrubber, reviewed by hand; #430 brings them under it.
LEGACY_REAL_NAS = {
    "tests/modules/system/fixtures/ds1618-7.1.1-admin.json",
    "tests/modules/photos/fixtures/dsm-7.1.1-photos-1.3.4-people.json",
}
# A real Synology model name (DS1618+, DS220j, RS1221RP+, FS2500, ...); vdsm reports VirtualDSM.
SYNOLOGY_MODEL = re.compile(
    r"\b(?:DS|RS|FS|SA|HD|UC|DVA|NVR)\d{3,4}(?:j|play|slim|value|II|xs|RP)?\+?(?![\w+])",
    re.IGNORECASE,
)

HOST = "Gizmo"
UUID_A = "aaaaaaaa-1111-4222-8333-444444444444"
UUID_B = "bbbbbbbb-1111-4222-8333-444444444444"
LVM = "AbCdEf-1234-5678-9abc-DEFg-hijk-LmNoPq"


@pytest.fixture(scope="module")
def sc() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scrub_nas_capture", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["scrub_nas_capture"] = module
    spec.loader.exec_module(module)
    return module


def _raw() -> dict[str, Any]:
    shares = [
        {"name": "Holiday Videos", "desc": "Trips 2019-2024", "uuid": UUID_A},
        {"name": "homes", "desc": "", "uuid": UUID_B},
        {"name": "usbshare1", "desc": "Acme Storage Corp.", "uuid": UUID_B},
    ]
    return {
        "admin:SYNO.Core.Share.list.v1": {
            "request": {"api": "SYNO.Core.Share", "method": "list", "version": 1},
            "reply": {"success": True, "data": {"shares": shares}},
        },
        'admin:SYNO.Core.Share.Snapshot.list.v2:{"name": "\\"Holiday Videos\\""}': {
            "request": {"api": "SYNO.Core.Share.Snapshot", "name": '"Holiday Videos"'},
            "reply": {"success": True, "data": {"snapshots": [], "total": 0}},
        },
        "admin:SYNO.Core.System.SystemHealth.get.v1": {
            "request": {"api": "SYNO.Core.System.SystemHealth"},
            "reply": {
                "success": True,
                "data": {"hostname": HOST, "ip": "10.1.2.3", "tailscale": "100.101.102.103"},
            },
        },
        "admin:SYNO.Core.ExternalDevice.Storage.USB.list.v1": {
            "request": {"api": "SYNO.Core.ExternalDevice.Storage.USB"},
            "reply": {
                "success": True,
                "data": {
                    "devices": [{"producer": "Acme Storage Corp.", "share_name": "usbshare1"}]
                },
            },
        },
        "admin:SYNO.Storage.CGI.Storage.load_info.v1": {
            "request": {"api": "SYNO.Storage.CGI.Storage"},
            "reply": {
                "success": True,
                "data": {
                    "disks": [{"serial": "ZZ99XX11", "ui_serial": "ZZ99XX11"}],
                    "env": {"unique_key": "abc123def0", "macs": ["00:11:22:33:44:55"]},
                    "volumes": [{"uuid": LVM, "path": "/volume1/Holiday Videos/clips"}],
                    "note": f"{HOST} - VM Storage 1; gizmos are not {HOST.lower()}s",
                },
            },
        },
    }


def test_scrub_replaces_everything_identifying(sc: ModuleType) -> None:
    raw = _raw()
    out = sc.scrub(raw)
    text = json.dumps(out)
    for real in (
        "Holiday Videos",
        "Trips 2019-2024",
        HOST,
        "ZZ99XX11",
        "abc123def0",
        "10.1.2.3",
        "100.101",
    ):
        assert real not in text, real
    for real in (UUID_A, UUID_B, LVM, '"homes"', '"usbshare1"'):
        assert real not in text, real
    assert sc.check(out, sc.raw_names(raw)) == []


def test_scrub_uses_one_fake_per_value_in_keys_values_and_paths(sc: ModuleType) -> None:
    out = sc.scrub(_raw())
    names = [s["name"] for s in out["admin:SYNO.Core.Share.list.v1"]["reply"]["data"]["shares"]]
    assert names == ["share01", "share02", "share03"]  # sorted: "Holiday Videos", "homes", ...
    label = 'admin:SYNO.Core.Share.Snapshot.list.v2:{"name": "\\"share01\\""}'
    assert out[label]["request"]["name"] == '"share01"'
    data = out["admin:SYNO.Storage.CGI.Storage.load_info.v1"]["reply"]["data"]
    assert data["disks"][0]["serial"] == data["disks"][0]["ui_serial"] == "FAKE0001"
    assert data["env"]["unique_key"].startswith("FAKE")
    assert data["volumes"][0]["path"] == "/volume1/share01/clips"
    # The hostname as a word only: "gizmos" isn't the host.
    assert data["note"] == "NAS01 - VM Storage 1; gizmos are not gizmos"
    assert data["env"]["macs"] == ["FAKE0000000000003"]
    health = out["admin:SYNO.Core.System.SystemHealth.get.v1"]["reply"]["data"]
    assert health == {"hostname": "NAS01", "ip": "192.0.2.10", "tailscale": "192.0.2.11"}


def test_a_description_copied_from_another_field_leaves_that_field(sc: ModuleType) -> None:
    raw = _raw()
    out = sc.scrub(raw)
    usb = out["admin:SYNO.Core.ExternalDevice.Storage.USB.list.v1"]["reply"]["data"]["devices"][0]
    assert usb == {"producer": "Acme Storage Corp.", "share_name": "share03"}
    shares = out["admin:SYNO.Core.Share.list.v1"]["reply"]["data"]["shares"]
    assert [s["desc"] for s in shares] == ["Description 02", "", "Description 01"]
    assert "Acme Storage Corp." not in sc.raw_names(raw)


def test_check_reports_what_still_looks_real(sc: ModuleType) -> None:
    raw = _raw()
    problems = sc.check(raw, sc.raw_names(raw))
    assert f"UUID {UUID_A}" in problems
    assert f"LVM UUID {LVM}" in problems
    assert "private IP 10.1.2.3" in problems
    assert "private IP 100.101.102.103" in problems
    assert "identifier serial=ZZ99XX11" in problems
    assert "identifier unique_key=abc123def0" in problems
    assert "share Holiday Videos" in problems
    assert "share usbshare1" in problems  # the USB entry's share_name
    assert "share description of Holiday Videos" in problems
    assert any(p.startswith("share in admin:SYNO.Core.Share.Snapshot.list.v2:") for p in problems)
    assert f"hostname {HOST}" in problems
    assert "real value left: Trips 2019-2024" in problems


def test_main_writes_only_a_clean_file(sc: ModuleType, tmp_path: Path) -> None:
    raw, out = tmp_path / "raw.json", tmp_path / "out.json"
    raw.write_text(json.dumps(_raw()))
    assert sc.main([str(raw), str(out)]) == 0
    assert sc.check(json.loads(out.read_text())) == []


@pytest.mark.parametrize(
    ("ip", "private"),
    [
        ("100.64.0.1", True),
        ("100.127.255.255", True),
        ("100.63.255.255", False),
        ("100.128.0.1", False),
        ("192.0.2.10", False),
        ("8.8.8.8", False),
    ],
)
def test_private_ip_covers_tailscale_cgnat_only(sc: ModuleType, ip: str, private: bool) -> None:
    assert bool(sc.PRIVATE_IP.search(ip)) is private


@pytest.mark.parametrize(
    "model", ["DS1618+", "DS220j", "DS218play", "DS620slim", "RS1221RP+", "RS3621xs+", "ds920+"]
)
def test_model_names_with_suffixes_count_as_real(model: str) -> None:
    assert SYNOLOGY_MODEL.search(f'"model": "{model}"')


@pytest.mark.parametrize("text", ["VirtualDSM", "DSM 7.2.2", "hd1080p", "DS12"])
def test_other_strings_dont(text: str) -> None:
    assert not SYNOLOGY_MODEL.search(text)


def test_real_nas_fixtures_are_found() -> None:
    assert REAL_NAS_FIXTURES, "no *-realnas.json fixture found"


@pytest.mark.parametrize("path", REAL_NAS_FIXTURES, ids=lambda p: p.name)
def test_real_nas_fixtures_are_scrubbed(sc: ModuleType, path: Path) -> None:
    assert sc.check(json.loads(path.read_text())) == []


def test_every_fixture_from_a_real_nas_is_named_realnas() -> None:
    """A capture from a real NAS saved without the -realnas name would skip the check above."""
    unnamed = [
        str(p.relative_to(ROOT))
        for p in sorted(ROOT.glob("tests/**/*.json"))
        if SYNOLOGY_MODEL.search(p.read_text())
        and not p.name.endswith("-realnas.json")
        and str(p.relative_to(ROOT)) not in LEGACY_REAL_NAS
    ]
    assert unnamed == [], f"name real-NAS fixtures *-realnas.json and scrub them: {unnamed}"
