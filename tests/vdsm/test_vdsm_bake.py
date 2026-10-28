"""What the bake puts in every golden image, checked on the restored image.

The bake's own read-backs cover only bake time; these run on the image a test session
actually restores. Photos' bake checks are in ``test_vdsm_photos_bake.py``.

Run: ``uv run pytest tests/vdsm -m vdsm --dsm-version 7.2.2 -k vdsm_bake``
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.vdsm.config import DSM_TIMEZONES
from tests.vdsm.golden_image import load_golden_meta

pytestmark = pytest.mark.vdsm


async def test_nas_timezone_is_the_baked_one(admin_client: Any, dsm_version: str) -> None:
    """Each golden image runs in its own zone (DSM_TIMEZONES), whatever the bake host's (#342)."""
    ntp = await admin_client[0].request("SYNO.Core.Region.NTP", "get", version=1)
    assert ntp["timezone"] == DSM_TIMEZONES[dsm_version]
    assert load_golden_meta(dsm_version).get("timezone") == DSM_TIMEZONES[dsm_version]


async def test_snapshot_replication_is_installed(admin_client: Any, dsm_version: str) -> None:
    """Bake v8 installs Snapshot Replication (#49): a share snapshot can be taken, and
    `Share.Snapshot list` v2 shows it while v1 doesn't (captured on all four versions while
    writing the Storage spec; the module will pin v2)."""
    assert "ReplicationService" in load_golden_meta(dsm_version).get("dsm_packages", [])
    client = admin_client[0]
    assert "SYNO.Btrfs.Replica.Core" in client.api_cache
    taken = await client.request(
        "SYNO.Core.Share.Snapshot",
        "create",
        version=1,
        params={"name": '"writable"', "snapinfo": '{"desc": "bake check", "lock": false}'},
    )
    additional = '["desc", "lock", "schedule_snapshot"]'
    listed = {}
    try:
        for version in (1, 2):
            listed[version] = await client.request(
                "SYNO.Core.Share.Snapshot",
                "list",
                version=version,
                params={"name": '"writable"', "additional": additional, "offset": 0, "limit": -1},
            )
        assert taken in [s["time"] for s in listed[2]["snapshots"]]
        assert listed[1]["total"] == 0, listed[1]
    finally:
        # Never leave a snapshot on writable for later tests to count.
        await client.request(
            "SYNO.Core.Share.Snapshot",
            "delete",
            version=1,
            params={"name": '"writable"', "snapshots": f'["{taken}"]'},
        )
