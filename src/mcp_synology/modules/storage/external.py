"""list_external_storage: USB and eSATA drives, expansion units."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp_synology.core.formatting import format_size
from mcp_synology.modules.storage.helpers import (
    Sections,
    dicts,
    json_list,
    notes_block,
    value_text,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

OPERATION = "List external storage"
_ALL = {"additional": json_list(["all"])}

_DEVICE_KNOWN = {
    "dev_id",
    "dev_title",
    "producer",
    "product",
    "total_size_mb",
    "status",
    "partitions",
    "dev_type",
}
_PART_KNOWN = {
    "name_id",
    "partition_title",
    "filesystem",
    "total_size_mb",
    "used_size_mb",
    "share_name",
    "status",
    "dev_fstype",
}


def mib(value: object) -> str:
    """Sizes come in MB, read as binary MiB (a 32 GB stick reports 29,604)."""
    try:
        return format_size(int(str(value)) * 1024 * 1024)
    except (TypeError, ValueError):
        return "—"


async def _usb(client: DsmClient, optional: Sections) -> Any:
    """USB list answers 404, not 105, to a non-admin on DSM 7.2.2 and later."""
    return await optional.get(
        client,
        "USB drives",
        "SYNO.Core.ExternalDevice.Storage.USB",
        "list",
        _ALL,
        refusals=frozenset({105, 404}),
    )


def device_lines(devices: list[dict[str, Any]], kind: str) -> list[str]:
    lines: list[str] = []
    for dev in devices:
        maker = " ".join(str(dev.get(k) or "").strip() for k in ("producer", "product")).strip()
        cells = [f"{dev.get('dev_title') or kind} ({dev.get('dev_id')})"]
        if maker:
            cells.append(maker)
        if dev.get("total_size_mb") is not None:
            cells.append(mib(dev.get("total_size_mb")))
        if dev.get("status"):
            cells.append(str(dev["status"]))
        lines.append("   ".join(cells))
        extra = {k: v for k, v in dev.items() if k not in _DEVICE_KNOWN}
        if extra:
            lines.append("    " + ", ".join(f"{k}: {value_text(v)}" for k, v in extra.items()))
        for part in dicts(dev.get("partitions")):
            used = part.get("used_size_mb")
            space = (
                f"{mib(used)} of {mib(part.get('total_size_mb'))} used"
                if used is not None
                else mib(part.get("total_size_mb"))
            )
            cells = [
                f"  {part.get('partition_title') or 'Partition'} ({part.get('name_id')})",
                str(part.get("filesystem") or part.get("dev_fstype") or "—"),
                space,
                f"shared folder {part['share_name']}"
                if part.get("share_name")
                else "no shared folder",
                str(part.get("status") or "—"),
            ]
            lines.append("   ".join(cells))
            extra = {k: v for k, v in part.items() if k not in _PART_KNOWN}
            if extra:
                lines.append(
                    "      " + ", ".join(f"{k}: {value_text(v)}" for k, v in extra.items())
                )
    return lines


def _devices(reply: Any) -> list[dict[str, Any]] | None:
    if not isinstance(reply, dict):
        return None
    return [d for d in reply.get("devices") or [] if isinstance(d, dict)]


async def list_external_storage(client: DsmClient) -> str:
    optional = Sections(OPERATION)
    usb = _devices(await _usb(client, optional))
    esata_api = "SYNO.Core.ExternalDevice.Storage.eSATA"
    probe = Sections(OPERATION)  # eSATA with `additional` is unverified: fall back without it
    esata = _devices(await probe.get(client, "eSATA drives", esata_api, "list", _ALL))
    if esata is None:
        esata = _devices(await optional.get(client, "eSATA drives", esata_api, "list"))
    eunits = _devices(
        await optional.get(
            client, "Expansion units", "SYNO.Core.ExternalDevice.Storage.EUnit", "list"
        )
    )

    sections = []
    for title, devices, kind in (
        ("USB drives", usb, "USB drive"),
        ("eSATA drives", esata, "eSATA drive"),
    ):
        if devices is not None:
            body = device_lines(devices, kind) or ["none"]
            sections.append("\n".join([title, *(f"  {x}" for x in body)]))
    if eunits is not None:
        body = [", ".join(f"{k}: {value_text(v)}" for k, v in unit.items()) for unit in eunits]
        sections.append("\n".join(["Expansion units", *(f"  {x}" for x in body or ["none"])]))
    if not sections:
        sections.append("No external storage could be read.")
    return "\n\n".join(sections) + notes_block(optional.notes)
