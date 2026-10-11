"""Download Station task tools: listing, info, create/delete/pause/resume/edit."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp_synology.core.errors import ApiNotFoundError, ErrorCode, SynologyError
from mcp_synology.core.formatting import (
    error_response,
    format_key_value,
    format_size,
    format_table,
    format_timestamp,
    synology_error_response,
)
from mcp_synology.modules.downloadstation.helpers import (
    STATUS_GROUPS,
    ds_is_manager,
    format_eta,
    format_speed,
    format_task_status,
    format_transfer_progress,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

_VALID_STATUS_FILTERS: set[str] = {"all", *STATUS_GROUPS.keys()}


def _format_epoch(epoch: int | None) -> str:
    """Render an epoch timestamp using the shared formatter; '—' for None/0."""
    if not epoch:
        return "—"
    return format_timestamp(float(epoch))


async def list_downloads(
    client: DsmClient,
    *,
    status_filter: str = "all",
    offset: int = 0,
    limit: int = 100,
) -> str:
    """List download tasks in the Download Station queue.

    The DSM v1 ``SYNO.DownloadStation.Task.list`` endpoint does not support
    server-side status filtering, so we fetch and filter client-side. To keep
    each row cheap, only ``detail`` and ``transfer`` additional groups are
    requested — ``get_download_info`` fetches the full set for a single task.
    """
    if status_filter not in _VALID_STATUS_FILTERS:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"List downloads failed: unknown status_filter {status_filter!r}.",
            retryable=False,
            param="status_filter",
            value=status_filter,
            valid=sorted(_VALID_STATUS_FILTERS),
        )

    try:
        data = await client.request(
            "SYNO.DownloadStation.Task",
            "list",
            version=1,
            params={
                "offset": str(offset),
                "limit": str(limit),
                # DS Task API uses comma-separated additional groups, not the
                # JSON-array format FileStation v2 uses.
                "additional": "detail,transfer",
            },
        )
    except SynologyError as e:
        synology_error_response("List downloads", e)

    tasks: list[dict[str, Any]] = data.get("tasks", [])

    # Non-managers only ever see their own tasks (#123), so an empty or short
    # list must not read as "nothing is downloading on the NAS".
    own_tasks_only = await ds_is_manager(client) is False
    scope = f"{status_filter}; your tasks only" if own_tasks_only else status_filter
    title = f"Download Station queue ({scope})"

    if status_filter != "all":
        wanted = STATUS_GROUPS[status_filter]
        tasks = [t for t in tasks if t.get("status") in wanted]

    if not tasks:
        return format_table(
            headers=["ID", "Title", "Type", "Status", "Size", "Progress", "Speed", "ETA"],
            rows=[],
            title=title,
        )

    rows: list[list[str]] = []
    for t in tasks:
        task_id = t.get("id", "—")
        title = t.get("title", "—")
        ttype = t.get("type", "—")
        status = format_task_status(t.get("status"))
        size_total = int(t.get("size", 0))
        transfer = t.get("additional", {}).get("transfer", {})
        size_down = int(transfer.get("size_downloaded", 0))
        speed_down = int(transfer.get("speed_download", 0))
        progress = format_transfer_progress(size_down, size_total)
        speed = format_speed(speed_down)
        eta = format_eta(size_down, size_total, speed_down)
        rows.append(
            [
                task_id,
                title,
                ttype,
                status,
                format_size(size_total),
                progress,
                speed,
                eta,
            ]
        )

    total = data.get("total", len(rows))
    result = format_table(
        headers=["ID", "Title", "Type", "Status", "Size", "Progress", "Speed", "ETA"],
        rows=rows,
        title=title,
    )
    result += f"\n\n{total} task(s) total; showing {len(rows)}."
    return result


async def get_download_info(
    client: DsmClient,
    *,
    task_id: str,
) -> str:
    """Get detailed information for a single download task.

    Requests all ``additional`` groups in one round-trip and renders them as
    distinct sections.
    """
    try:
        data = await client.request(
            "SYNO.DownloadStation.Task",
            "getinfo",
            version=1,
            params={
                "id": task_id,
                # DS Task API uses comma-separated additional groups, not the
                # JSON-array format FileStation v2 uses.
                "additional": "detail,transfer,file,tracker,peer",
            },
        )
    except SynologyError as e:
        synology_error_response(f"Get download info ({task_id})", e)

    tasks: list[dict[str, Any]] = data.get("tasks", [])
    if not tasks:
        error_response(
            ErrorCode.NOT_FOUND,
            f"Task {task_id!r} not found.",
            retryable=False,
            param="task_id",
            value=task_id,
        )
    task = tasks[0]

    sections: list[str] = []

    # Header block
    header_pairs = [
        ("ID", task.get("id", "—")),
        ("Title", task.get("title", "—")),
        ("Type", task.get("type", "—")),
        ("Status", format_task_status(task.get("status"))),
        ("Size", format_size(int(task.get("size", 0)))),
    ]
    sections.append(format_key_value(header_pairs, title="Task"))

    add = task.get("additional", {}) or {}

    # Detail block
    detail = add.get("detail", {}) or {}
    if detail:
        detail_pairs = [
            ("Destination", detail.get("destination", "—")),
            ("URI", detail.get("uri", "—")),
            ("Priority", detail.get("priority", "—")),
            ("Created", _format_epoch(detail.get("create_time"))),
            ("Started", _format_epoch(detail.get("started_time"))),
            ("Completed", _format_epoch(detail.get("completed_time"))),
        ]
        sections.append(format_key_value(detail_pairs, title="Detail"))

    # Transfer block
    transfer = add.get("transfer", {}) or {}
    if transfer:
        size_total = int(task.get("size", 0))
        size_down = int(transfer.get("size_downloaded", 0))
        size_up = int(transfer.get("size_uploaded", 0))
        transfer_pairs = [
            ("Downloaded", format_transfer_progress(size_down, size_total)),
            ("Uploaded", format_size(size_up)),
            ("Speed (down)", format_speed(int(transfer.get("speed_download", 0)))),
            ("Speed (up)", format_speed(int(transfer.get("speed_upload", 0)))),
        ]
        sections.append(format_key_value(transfer_pairs, title="Transfer"))

    # File table (BT)
    files = add.get("file", []) or []
    if files:
        file_rows = [
            [
                f.get("filename", "—"),
                format_size(int(f.get("size", 0))),
                format_transfer_progress(int(f.get("size_downloaded", 0)), int(f.get("size", 0))),
                f.get("priority", "—"),
            ]
            for f in files
        ]
        sections.append(
            format_table(
                headers=["File", "Size", "Progress", "Priority"],
                rows=file_rows,
                title="Files",
            )
        )

    # Tracker table (BT)
    trackers = add.get("tracker", []) or []
    if trackers:
        tracker_rows = [
            [
                tr.get("url", "—"),
                tr.get("status", "—"),
                str(tr.get("peers", 0)),
                str(tr.get("seeds", 0)),
            ]
            for tr in trackers
        ]
        sections.append(
            format_table(
                headers=["Tracker", "Status", "Peers", "Seeds"],
                rows=tracker_rows,
                title="Trackers",
            )
        )

    # Peer table (BT)
    peers = add.get("peer", []) or []
    if peers:
        peer_rows = [
            [
                p.get("address", "—"),
                p.get("agent", "—"),
                f"{int(float(p.get('progress', 0)) * 100)}%",
                format_speed(int(p.get("speed_download", 0))),
                format_speed(int(p.get("speed_upload", 0))),
            ]
            for p in peers
        ]
        sections.append(
            format_table(
                headers=["Peer", "Client", "Progress", "Down", "Up"],
                rows=peer_rows,
                title="Peers",
            )
        )

    return "\n\n".join(sections)


# DS2 Task.create answers these when it can't serve the request at all; only
# then is a v1 retry safe. Any other failure may have happened after DSM
# committed the task, so retrying could create duplicate downloads (#123).
_DS2_NOT_AVAILABLE_CODES = frozenset({102, 103, 104})


def _created_message(task_ids: list[str], n_uris: int) -> str:
    if task_ids:
        return f"Created {len(task_ids)} download task(s): {', '.join(task_ids)}."
    return (
        f"Created download task(s) for {n_uris} URI(s); DSM did not return task IDs "
        "— use list_downloads to find them."
    )


async def create_download(
    client: DsmClient,
    *,
    uri: str | None = None,
    torrent_file_path: str | None = None,
    destination: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> str:
    """Create one or more download tasks.

    Pass exactly one of:
    - ``uri``: comma-separated URIs (HTTP, FTP, magnet, etc.)
    - ``torrent_file_path``: local path to a .torrent or .nzb file (multipart upload)

    URIs go to ``SYNO.DownloadStation2.Task.create`` as a form POST (a GET URL
    past ~8 KB gets HTTP 414, #123), which returns the new task IDs. v1
    ``Task.create`` (also POST) is the fallback when DS2 is unavailable, and
    the path for ``username``/``password`` sources, whose DS2 fields are
    unverified. Files go to DS2 only (v1 file upload is broken on DSM 7.2.2).
    """
    if uri is None and torrent_file_path is None:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            "Create download failed: must supply either `uri` or `torrent_file_path`.",
            retryable=False,
            valid=["uri", "torrent_file_path"],
        )
    if uri is not None and torrent_file_path is not None:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            "Create download failed: supply exactly one of `uri` or `torrent_file_path`, not both.",
            retryable=False,
        )

    if torrent_file_path is not None:
        if username is not None or password is not None:
            error_response(
                ErrorCode.INVALID_PARAMETER,
                "Create download failed: `username`/`password` only apply to `uri` "
                "sources (HTTP/FTP authentication), not to torrent files.",
                retryable=False,
                param="username" if username is not None else "password",
            )
        path = Path(torrent_file_path).expanduser()
        if not path.is_file():
            error_response(
                ErrorCode.NOT_FOUND,
                f"Create download failed: torrent file not found at {torrent_file_path!r}.",
                retryable=False,
                param="torrent_file_path",
                value=torrent_file_path,
            )
        try:
            data = await client.create_download_task_with_file(
                file_path=path,
                filename=path.name,
                destination=destination,
            )
        except SynologyError as e:
            synology_error_response(f"Create download ({path.name})", e)
        task_ids = [str(t) for t in data.get("task_id", [])]
        return f"{_created_message(task_ids, 1)} Source: file {path.name}."

    uris = [u for u in (uri or "").split(",") if u]
    if not uris:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            "Create download failed: `uri` is empty.",
            retryable=False,
            param="uri",
        )

    if username is None and password is None:
        try:
            ds2_version = client.negotiate_version(
                "SYNO.DownloadStation2.Task", min_version=2, max_version=2
            )
        except ApiNotFoundError as e:
            logger.debug("DS2 Task.create unavailable (%s); using v1 Task.create", e)
        else:
            ds2_params = {
                "type": json.dumps("url"),
                "url": json.dumps(uris),
                "create_list": "false",
            }
            if destination is not None:
                ds2_params["destination"] = json.dumps(destination)
            try:
                data = await client.request_form_post(
                    "SYNO.DownloadStation2.Task", "create", ds2_version, ds2_params
                )
            except SynologyError as e:
                if e.code not in _DS2_NOT_AVAILABLE_CODES:
                    synology_error_response("Create download", e)
                logger.debug("DS2 Task.create answered %s; using v1 Task.create", e.code)
            else:
                task_ids = [str(t) for t in data.get("task_id", [])]
                return _created_message(task_ids, len(uris))

    v1_params: dict[str, str] = {"uri": ",".join(uris)}
    if destination is not None:
        v1_params["destination"] = destination
    if username is not None:
        v1_params["username"] = username
    if password is not None:
        v1_params["password"] = password
    try:
        await client.request_form_post("SYNO.DownloadStation.Task", "create", 1, v1_params)
    except SynologyError as e:
        synology_error_response("Create download", e)
    return _created_message([], len(uris))


async def delete_download(
    client: DsmClient,
    *,
    task_ids: list[str],
    force_complete: bool = False,
) -> str:
    """Remove one or more download tasks.

    Behavior verified live on DSM 7.2.2 (#123): files from **finished**
    tasks are always kept. For **unfinished** tasks the partial data is
    discarded, unless ``force_complete=True``, which moves the incomplete
    file into the task's destination (its size on disk does not reflect how
    much was downloaded).
    """
    if not task_ids:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            "Delete download failed: task_ids list is empty.",
            retryable=False,
            param="task_ids",
            value=task_ids,
        )

    ids_joined = ",".join(task_ids)

    try:
        data = await client.request(
            "SYNO.DownloadStation.Task",
            "delete",
            version=1,
            params={
                "id": ids_joined,
                "force_complete": str(force_complete).lower(),
            },
        )
    except SynologyError as e:
        synology_error_response("Delete download", e)

    results = data if isinstance(data, list) else data.get("results", [])
    rows: list[list[str]] = []
    for r in results:
        err = r.get("error", 0)
        status = "ok" if err == 0 else f"error {err}"
        rows.append([r.get("id", "—"), status])

    unfinished = (
        "incomplete files moved into the destination"
        if force_complete
        else "partial data of unfinished tasks discarded"
    )
    return format_table(
        headers=["Task ID", "Result"],
        rows=rows,
        title=(
            f"Delete download — {len(task_ids)} task(s) removed; completed files kept; {unfinished}"
        ),
    )


async def _task_state_change(
    client: DsmClient,
    *,
    task_ids: list[str],
    method: str,
    operation_label: str,
) -> str:
    """Shared shape for pause / resume / future state-toggle handlers."""
    if not task_ids:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"{operation_label} failed: task_ids list is empty.",
            retryable=False,
            param="task_ids",
            value=task_ids,
        )

    ids_joined = ",".join(task_ids)

    try:
        data = await client.request(
            "SYNO.DownloadStation.Task",
            method,
            version=1,
            params={"id": ids_joined},
        )
    except SynologyError as e:
        synology_error_response(operation_label, e)

    results = data if isinstance(data, list) else data.get("results", [])
    rows: list[list[str]] = []
    for r in results:
        err = r.get("error", 0)
        status = "ok" if err == 0 else f"error {err}"
        rows.append([r.get("id", "—"), status])

    return format_table(
        headers=["Task ID", "Result"],
        rows=rows,
        title=f"{operation_label} — {len(task_ids)} task(s)",
    )


async def pause_download(
    client: DsmClient,
    *,
    task_ids: list[str],
) -> str:
    """Pause one or more download tasks."""
    return await _task_state_change(
        client, task_ids=task_ids, method="pause", operation_label="Pause download"
    )


async def resume_download(
    client: DsmClient,
    *,
    task_ids: list[str],
) -> str:
    """Resume one or more paused download tasks."""
    return await _task_state_change(
        client, task_ids=task_ids, method="resume", operation_label="Resume download"
    )


async def edit_download(
    client: DsmClient,
    *,
    task_ids: list[str],
    destination: str | None = None,
) -> str:
    """Edit task parameters. Currently supports ``destination`` only.

    The guide documents only ``destination`` for ``Task.edit`` (v2+); other
    fields are follow-up work once verified against a live NAS.
    """
    if not task_ids:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            "Edit download failed: task_ids list is empty.",
            retryable=False,
            param="task_ids",
            value=task_ids,
        )
    if destination is None:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            "Edit download failed: no editable fields supplied "
            "(destination is the only currently-supported field).",
            retryable=False,
            valid=["destination"],
        )

    ids_joined = ",".join(task_ids)

    # Task.edit is "2 and later" (v1 answers 103, #123). Pinned to exactly v2
    # like the other version pins; a NAS whose Task API tops out at v1 gets
    # negotiate_version's clean 104 instead of a raw "method does not exist".
    try:
        version = client.negotiate_version(
            "SYNO.DownloadStation.Task", min_version=2, max_version=2
        )
    except ApiNotFoundError as e:
        error_response(
            ErrorCode.API_NOT_FOUND,
            f"Edit download failed: editing a task requires a newer Download Station "
            f"(SYNO.DownloadStation.Task v2). {e}",
            retryable=False,
            suggestion="Update Download Station in Package Center.",
        )

    try:
        data = await client.request(
            "SYNO.DownloadStation.Task",
            "edit",
            version=version,
            params={"id": ids_joined, "destination": destination},
        )
    except SynologyError as e:
        synology_error_response("Edit download", e)

    results = data if isinstance(data, list) else data.get("results", [])
    rows: list[list[str]] = []
    for r in results:
        err = r.get("error", 0)
        status = "ok" if err == 0 else f"error {err}"
        rows.append([r.get("id", "—"), status])

    return format_table(
        headers=["Task ID", "Result"],
        rows=rows,
        title=f"Edit download — {len(task_ids)} task(s), destination={destination}",
    )
