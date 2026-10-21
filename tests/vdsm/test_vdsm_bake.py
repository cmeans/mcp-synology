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
