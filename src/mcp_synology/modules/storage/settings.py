"""get_storage_settings: repair, health thresholds, hot spares, external storage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp_synology.core.formatting import format_key_value
from mcp_synology.modules.storage.helpers import (
    STORAGE_API,
    Sections,
    epoch_note,
    notes_block,
    value_text,
    when,
    yes_no,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

OPERATION = "Get storage settings"

_SPEEDS = {
    "resync_slower": "lower impact on the system",
    "resync_faster": "faster resync",
    "resync_custom": "custom",
}

_HDDMAN = {
    "BadSctrThrEn": "Warn on bad sectors",
    "RemainLifeThrEn": "Warn on SSD remaining life",
    "RemainLifeThrVal": "  below (%)",
    "SBMonthLeftThrEn": "Warn on months of life left",
    "SBMonthLeftThrVal": "  below (months)",
    "healthReportEn": "Health reports",
    "chkMailSetting": "Check email settings",
    "WddaEn": "Western Digital Device Analytics",
}


def _pairs(reply: Any, labels: dict[str, str]) -> list[tuple[str, str]]:
    if not isinstance(reply, dict):
        return []
    out = []
    for key, value in reply.items():
        label = str(labels.get(key, key))
        is_flag = (
            key.endswith(("En", "_enable")) or key == "chkMailSetting" or isinstance(value, bool)
        )
        out.append((label, yes_no(value) if is_flag else value_text(value)))
    return out


async def get_storage_settings(client: DsmClient) -> str:
    optional = Sections(OPERATION)
    resync = await optional.get(client, "RAID resync speed", STORAGE_API, "get_resync_speed")
    fast = await optional.get(client, "Fast repair", STORAGE_API, "get_fast_repair_config")
    auto = await optional.get(client, "Automatic repair", STORAGE_API, "get_auto_repair_config")
    hdd = await optional.get(client, "Disk health warnings", "SYNO.Storage.CGI.HddMan", "get")
    spare = await optional.get(client, "Hot spares", "SYNO.Storage.CGI.Spare.Conf", "get")
    db = await optional.get(
        client, "Drive database", "SYNO.Storage.CGI.Smart", "get_latest_online_drive_db_info"
    )
    external = await optional.get(
        client, "External storage", "SYNO.Core.ExternalDevice.Storage.Setting", "get"
    )

    blocks = []
    if isinstance(resync, dict):
        pairs = [
            ("Mode", _SPEEDS.get(str(resync.get("speed_option")), str(resync.get("speed_option"))))
        ]
        if "speed_min" in resync:
            pairs.append(("Limits", f"{resync.get('speed_min')} to {resync.get('speed_max')}"))
        pairs.extend(
            (k, value_text(v))
            for k, v in resync.items()
            if k not in ("speed_option", "speed_min", "speed_max")
        )
        blocks.append(format_key_value(pairs, title="RAID resync speed"))
    repair = _pairs(
        fast,
        {
            "is_fast_repair_enable": "Fast repair",
            "is_auto_scrubbing_enable": "Automatic data scrubbing",
        },
    )
    if isinstance(auto, dict):
        repair.extend(_pairs(auto, {"enable": "Automatic repair"}))
    if repair:
        blocks.append(format_key_value(repair, title="Repair"))
    if isinstance(hdd, dict):
        pairs = _pairs({k: v for k, v in hdd.items() if k != "db_last_update_time"}, _HDDMAN)
        if "db_last_update_time" in hdd:
            pairs.append(("Drive database updated", when(hdd.get("db_last_update_time"))))
        blocks.append(format_key_value(pairs, title="Disk health"))
    if isinstance(db, dict):
        blocks.append(
            format_key_value(
                [(k, value_text(v)) for k, v in db.items()], title="Latest online drive database"
            )
        )
    if isinstance(spare, dict):
        blocks.append(
            format_key_value(
                _pairs(
                    spare,
                    {"auto_replacement": "Replace a failed disk with a hot spare automatically"},
                ),
                title="Hot spares",
            )
        )
    if isinstance(external, dict):
        blocks.append(
            format_key_value(
                _pairs(
                    external,
                    {
                        "forbid_usb": "Block USB storage",
                        "delalloc": "Delayed allocation",
                        "support_exfat_mkfs": "Can format exFAT",
                        "non_admin_eject": "Non-admins may eject",
                        "needReboot": "Needs a restart",
                    },
                ),
                title="External storage",
            )
        )
    if not blocks:
        blocks.append("No storage settings could be read.")
    text = "\n\n".join(blocks)
    return text + epoch_note(text) + notes_block(optional.notes)
