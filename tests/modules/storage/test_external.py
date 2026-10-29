"""list_external_storage against the real-NAS and vdsm captures."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_synology.modules.storage.external import list_external_storage
from tests.modules.storage.dsm import VDSM, Replay, err, ok, run, run_error


async def test_real_nas_usb_stick() -> None:
    text, replay = await run(list_external_storage)
    assert (
        "USB Disk 1 (sdu)   Kingston Technology Company Inc. USB DISK 3.0   28.9 GiB   normal"
        in text
    )
    assert (
        "USB Disk 1 Partition 1 (sdu1)   FAT32   2 MiB of 28.9 GiB used   shared folder "
        "share19   normal"
    ) in text
    assert "formatable: yes" in text
    assert "eSATA drives\n  none" in text
    assert "Expansion units\n  none" in text
    assert (
        replay.calls("SYNO.Core.ExternalDevice.Storage.USB", "list")[0]["additional"] == '["all"]'
    )
    # eSATA with `additional` isn't captured (it answers 599 here): it's asked again without.
    esata = replay.calls("SYNO.Core.ExternalDevice.Storage.eSATA", "list")
    assert [("additional" in q) for q in esata] == [True, False]


async def test_esata_with_additional() -> None:
    def setup(replay: Replay, _: Any) -> None:
        replay.answer(
            {"api": "SYNO.Core.ExternalDevice.Storage.eSATA", "additional": '["all"]'},
            ok(
                {"devices": [{"dev_id": "sdq", "dev_title": "eSATA Disk 1", "total_size_mb": 1024}]}
            ),
        )

    text, replay = await run(list_external_storage, setup=setup)
    assert "eSATA Disk 1 (sdq)   1 GiB" in text
    assert len(replay.calls("SYNO.Core.ExternalDevice.Storage.eSATA", "list")) == 1


@pytest.mark.parametrize("fixture", VDSM)
async def test_vdsm(fixture: str) -> None:
    def setup(replay: Replay, _: Any) -> None:
        replay.answer({"api": "SYNO.Core.ExternalDevice.Storage.USB"}, ok({"devices": []}))

    text, _ = await run(list_external_storage, fixture, setup=setup)
    assert "USB drives\n  none" in text


@pytest.mark.parametrize("code", [105, 404])
async def test_non_admin_usb_refusal(code: int) -> None:
    def setup(replay: Replay, _: Any) -> None:
        replay.answer({"api": "SYNO.Core.ExternalDevice.Storage.USB"}, err(code))

    error = await run_error(list_external_storage, setup=setup)
    assert error["code"] == "permission_denied"


async def test_other_usb_error_is_noted() -> None:
    def setup(replay: Replay, _: Any) -> None:
        replay.answer({"api": "SYNO.Core.ExternalDevice.Storage.USB"}, err(117))

    text, _ = await run(list_external_storage, setup=setup)
    assert "USB drives: couldn't be read (DSM error 117)" in text
