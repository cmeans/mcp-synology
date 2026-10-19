"""Photos background tasks: copy_photos, move_photos, delete_photos,
delete_photo_folders and photo_task.

``BackgroundTask.File copy|move|delete`` starts a task; ``BackgroundTask.Info``
(personal API, covering both spaces) reports it. Each tool polls once a second, as
the web client does, until the task is ``done`` or ``task_timeout`` runs out, then
hands it off with a ``photos:<id>`` handle that ``photo_task`` follows up on, the
pattern File Station's ``file_task`` uses (#245). A status request that gets no
answer hands off too: the task is most likely still running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

from mcp_synology.core.errors import DsmHttpError, DsmReplyError, SynologyError
from mcp_synology.core.formatting import format_table, format_timestamp, synology_error_response
from mcp_synology.modules.photos.helpers import (
    Photos,
    Space,
    check_ids,
    check_space,
    invalid,
    not_found,
    space_label,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)

POLL_INTERVAL = 1.0
_HANDLE = re.compile(r"^photos:([0-9]{1,12})$")  # \d would take "５" as 5
_DECORATION = " \t\r\n`'\"*.,;:()[]<>"
_CONFLICT = {"skip": "skip", "overwrite": "overwrite"}
_LIBRARY = {"personal": "personal_space", "shared": "shared_space"}


def handle_of(task_id: int) -> str:
    return f"photos:{task_id}"


def parse_handle(operation: str, text: str) -> int:
    """``photos:<id>`` → the task ID; quoting or punctuation around it is tolerated."""
    match = _HANDLE.match(text.strip(_DECORATION))
    if not match:
        invalid(
            operation,
            f"{text!r} isn't a Photos task handle.",
            param="handle",
            value=text,
            suggestion="Pass the handle exactly as returned, e.g. photos:14.",
        )
    return int(match.group(1))


async def _status(ph: Photos, task_id: int) -> dict[str, Any] | None:
    data = await ph.call("BackgroundTask.Info", "get_status", {"id": [task_id]}, personal=True)
    listed = data.get("list", [])
    return dict(listed[0]) if listed else None


async def _wait(
    ph: Photos, task: dict[str, Any], timeout: float
) -> tuple[dict[str, Any], str | None]:
    """Poll until done or ``timeout``.

    Returns the last status, and ``None`` when it's done, or why it's handed off:
    ``"timeout"``, or what went wrong with a status request: no answer (a transport
    error), or a proxy's HTTP error or page (#282), which is how a proxy reports the same
    stall. The task is most likely still running, so it isn't lost.
    """
    deadline = time.monotonic() + timeout
    status = task
    while status.get("status") != "done":
        if time.monotonic() >= deadline:
            return status, "timeout"
        await asyncio.sleep(POLL_INTERVAL)
        try:
            now = await _status(ph, int(task["id"]))
        except httpx.TransportError as e:
            logger.warning("Photos task %s status got no answer: %s", task["id"], type(e).__name__)
            return status, f"The NAS didn't answer a status check ({type(e).__name__})"
        except DsmHttpError as e:
            logger.warning("Photos task %s status got HTTP %s", task["id"], e.status)
            return status, (
                "The NAS, or a proxy in front of it, answered a status check with an error "
                f"(HTTP {e.status})"
            )
        except DsmReplyError:
            logger.warning("Photos task %s status reply wasn't a DSM reply", task["id"])
            return status, (
                "The NAS's status reply couldn't be read (not a DSM reply; a proxy or captive "
                "portal may be answering)"
            )
        if now is None:
            # Gone from the list: cleared, which only happens once it's finished.
            return {**status, "status": "done"}, None
        status = now
    return status, None


def _progress(status: dict[str, Any]) -> str:
    total = int(status.get("total") or 0)
    done = int(status.get("completion") or 0)
    return f"{done} of {total} done"


def _counts(status: dict[str, Any]) -> str:
    skip, over = int(status.get("skip") or 0), int(status.get("overwrite") or 0)
    errors = int(status.get("error") or 0)
    text = f"{_progress(status)}, {skip} skipped (already there), {over} overwritten"
    return text + (f", {errors} failed" if errors else "")


async def _errors(ph: Photos, task_id: int) -> list[str]:
    try:
        data = await ph.call(
            "BackgroundTask.Info", "get_error_detail", {"id": task_id}, personal=True
        )
    except SynologyError as e:
        return [f"  (couldn't read the failed items: {e})"]
    lines = []
    for entry in data.get("list", [])[:20]:
        what = entry.get("item_id") or entry.get("id") or entry.get("name") or "?"
        reason = entry.get("reason") or entry.get("error") or entry.get("code") or "failed"
        lines.append(f"  item {what}: {reason}")
    return lines


def _handoff(verb: str, status: dict[str, Any], why: str) -> str:
    lead = (
        f"Still {verb} on the NAS ({_progress(status)})."
        if why == "timeout"
        else f"{why}; the task is most likely still {verb}."
    )
    return f"{lead} Handle: {handle_of(int(status['id']))} — check with photo_task."


# -- copy / move ------------------------------------------------------------------


async def copy_or_move(
    client: DsmClient,
    *,
    operation: str,
    ids: list[int] | None,
    folders: list[str] | None,
    destination: str,
    space: str = "personal",
    dest_space: str | None = None,
    on_conflict: str = "skip",
    timeout: float = 30,
) -> str:
    """copy_photos / move_photos (spec): items and folders into one folder."""
    op = "Copy photos" if operation == "copy" else "Move photos"
    src = check_space(op, space)
    dst = check_space(op, dest_space) if dest_space is not None else src
    if on_conflict not in _CONFLICT:
        invalid(
            op,
            f"unknown on_conflict {on_conflict!r}.",
            param="on_conflict",
            value=on_conflict,
            valid=list(_CONFLICT),
        )
    if not ids and not folders:
        invalid(op, "pass ids, folders or both.", param="ids")
    if operation == "move" and src == "shared" and dst == "personal":
        # DSM answers 640 to it (verified on 7.1 and 7.2.2); a copy works, and so does a
        # move the other way.
        invalid(
            op,
            "Photos doesn't move items out of Shared Space into a personal space.",
            param="dest_space",
            value=dest_space,
            suggestion="Copy them with copy_photos, then delete the originals if wanted.",
        )
    if ids:
        check_ids(op, ids)
    if folders:
        check_ids(op, folders, param="folders")  # one lookup each
    try:
        source = Photos(client, src, op)
        found, missing = await source.items(ids or [])
        folder_ids: list[int] = []
        for value in folders or []:
            folder = await source.folder(value, param="folders")
            if folder.get("name") == "/":
                invalid(
                    op,
                    "the space's root folder can't be copied or moved.",
                    param="folders",
                    value=value,
                )
            folder_ids.append(int(folder["id"]))
        target = Photos(client, dst, op)
        dest = await target.folder(destination, param="destination")
        if not found and not folder_ids:
            return "Nothing to do: none of the ids exist.\n" + source.missing_note(missing)
        params: dict[str, Any] = {
            "target_folder_id": int(dest["id"]),
            "item_id": list(found),
            "folder_id": folder_ids,
            "action": _CONFLICT[on_conflict],
        }
        if src != dst:
            # The call goes to the source space's API with a target folder in the other
            # space (verified on 7.2.2: through the destination's API DSM can't find the
            # items, "not_existed"). extra_info names the source library as the web client
            # sends it: a JSON string, encoded again like every other value.
            params["extra_info"] = json.dumps({"version": 2, "source_library": _LIBRARY[src]})
        data = await source.call("BackgroundTask.File", operation, params)
        task = data["task_info"]
        verb = "copying" if operation == "copy" else "moving"
        status, why = await _wait(source, task, timeout)
        what = _what(len(found), len(folder_ids))
        where = f"{dest.get('name')} ({space_label(dst)})"
        if why is not None:
            lines = [
                f"{'Copy' if operation == 'copy' else 'Move'} of {what} to {where}: "
                + _handoff(verb, status, why)
            ]
        else:
            done = "Copied" if operation == "copy" else "Moved"
            lines = [f"{done} {what} to {where}: {_counts(status)}."]
            if int(status.get("error") or 0):
                lines += await _errors(source, int(status["id"]))
        if missing:
            lines.append(source.missing_note(missing))
        return "\n".join(lines)
    except SynologyError as e:
        synology_error_response(op, e)


def _what(items: int, folders: int) -> str:
    parts = []
    if items:
        parts.append(f"{items} item(s)")
    if folders:
        parts.append(f"{folders} folder(s)")
    return " and ".join(parts)


# -- deletes ------------------------------------------------------------------------


async def _recycle_note(ph: Photos) -> str:
    """Where deleted files went, from Setting.Admin's recycle-bin flags (1.9.1)."""
    key = f"enable_{'shared' if ph.space == 'shared' else 'personal'}_dsm_recycle_bin"
    try:
        setting = await ph.call("Setting.Admin", "get", personal=True)
    except SynologyError:
        setting = {}
    if key not in setting:
        return "they're in the recycle bin of the share that holds the space, if it has one"
    if setting[key]:
        return "they're in the recycle bin of the share that holds the space"
    return "deleted permanently: that share has no recycle bin"


async def _run_delete(
    ph: Photos, item_ids: list[int], folder_ids: list[int], timeout: float, what: str
) -> list[str]:
    data = await ph.call(
        "BackgroundTask.File", "delete", {"item_id": item_ids, "folder_id": folder_ids}
    )
    status, why = await _wait(ph, data["task_info"], timeout)
    if why is not None:
        return [f"Delete of {what}: " + _handoff("deleting", status, why)]
    lines = [f"Deleted {what} in the {space_label(ph.space)}; {await _recycle_note(ph)}."]
    if int(status.get("error") or 0):
        lines.insert(0, f"Delete of {what} finished with errors: {_counts(status)}.")
        lines = [lines[0], *await _errors(ph, int(status["id"]))]
    return lines


async def delete_photos(
    client: DsmClient, *, ids: list[int], space: str = "personal", timeout: float = 30
) -> str:
    """Delete items (spec: delete_photos); unknown IDs are reported, never sent."""
    op = "Delete photos"
    sp = check_space(op, space)
    check_ids(op, ids)
    try:
        ph = Photos(client, sp, op)
        found, missing = await ph.items(ids)
        lines = (
            await _run_delete(ph, list(found), [], timeout, f"{len(found)} item(s)")
            if found
            else ["Nothing deleted: none of the ids exist."]
        )
        if missing:
            lines.append(ph.missing_note(missing))
        return "\n".join(lines)
    except SynologyError as e:
        synology_error_response(op, e)


async def delete_photo_folders(
    client: DsmClient, *, folders: list[str], space: str = "personal", timeout: float = 30
) -> str:
    """Delete folders with everything in them (spec: delete_photo_folders)."""
    op = "Delete photo folders"
    sp = check_space(op, space)
    if not folders:
        invalid(op, "pass at least one folder.", param="folders", value=folders)
    check_ids(op, folders, param="folders")  # one lookup each
    try:
        ph = Photos(client, sp, op)
        resolved: dict[int, str] = {}
        for value in folders:
            folder = await ph.folder(value, param="folders")
            if folder.get("name") == "/":
                invalid(
                    op, "the space's root folder can't be deleted.", param="folders", value=value
                )
            resolved[int(folder["id"])] = str(folder.get("name", ""))
        described = []
        for folder_id, path in resolved.items():
            count = await ph.call("Browse.Item", "count", {"folder_id": folder_id})
            described.append(f"{path} ({count.get('count', 0)} item(s) directly in it)")
        what = ", ".join(described)
        return "\n".join(await _run_delete(ph, [], list(resolved), timeout, what))
    except SynologyError as e:
        synology_error_response(op, e)


# -- photo_task ---------------------------------------------------------------------


async def _target(client: DsmClient, op: str, status: dict[str, Any]) -> str:
    folder = status.get("target_folder") or {}
    if "id" not in folder:
        return ""
    space: Space = "shared" if folder.get("owner_user_id") == 0 else "personal"
    path = await Photos(client, space, op).folder_path(int(folder["id"]))
    return f" to {path}" + (" (Shared Space)" if space == "shared" else "")


def _running_for(status: dict[str, Any]) -> str:
    created = status.get("create_time")
    if not created:
        return ""
    seconds = max(0, int(time.time() - int(created) / 1000))
    minutes, sec = divmod(seconds, 60)
    return f", running {minutes} min {sec} s" if minutes else f", running {sec} s"


async def photo_task(
    client: DsmClient, *, handle: str | None = None, action: str = "status"
) -> str:
    """Follow up on, stop, list or clear Photos tasks (spec: photo_task)."""
    op = "Photo task"
    if action not in ("status", "stop", "clear"):
        invalid(
            op,
            f"unknown action {action!r}.",
            param="action",
            value=action,
            valid=["status", "stop", "clear"],
        )
    if action == "stop" and handle is None:
        invalid(op, "action=stop needs a handle.", param="handle")
    task_id = parse_handle(op, handle) if handle is not None else None
    try:
        ph = Photos(client, "personal", op)
        if action == "clear":
            before = await ph.call("BackgroundTask.Info", "list_user_task", personal=True)
            await ph.call("BackgroundTask.Info", "clear_completed_task", personal=True)
            after = await ph.call("BackgroundTask.Info", "list_user_task", personal=True)
            cleared = len(before.get("list", [])) - len(after.get("list", []))
            return f"Removed {max(cleared, 0)} finished task(s) from the list."
        if task_id is None:
            tasks = (await ph.call("BackgroundTask.Info", "list_user_task", personal=True)).get(
                "list", []
            )
            rows = [
                [
                    handle_of(int(t["id"])),
                    str(t.get("operation", "")),
                    (await _target(client, op, t)).removeprefix(" to ") or "—",
                    str(t.get("status", "")),
                    f"{t.get('completion', 0)}/{t.get('total', 0)}",
                    format_timestamp(int(t["create_time"]) / 1000) if t.get("create_time") else "—",
                ]
                for t in tasks
            ]
            return format_table(
                ["Handle", "Operation", "Target folder", "Status", "Done", "Started"],
                rows,
                title=f"Photos tasks ({len(rows)})",
            )
        if action == "stop":
            try:
                await ph.call("BackgroundTask.Info", "abort_task", {"id": [task_id]}, personal=True)
            except SynologyError as e:
                if e.code == 117:
                    not_found(op, f"no task {handle_of(task_id)}.", param="handle", value=handle)
                raise
        status = await _status(ph, task_id)
        if status is None:
            not_found(
                op,
                f"no task {handle_of(task_id)} (finished tasks disappear once cleared).",
                param="handle",
                value=handle,
            )
        line = (
            f"{handle_of(task_id)} {status.get('operation', 'task')}"
            f"{await _target(client, op, status)}: {status.get('status')}, {_counts(status)}"
        )
        if status.get("status") != "done":
            line += _running_for(status)
        if action == "stop":
            line += ". Stop requested; what was already done stays done."
        elif status.get("status") == "done" and int(status.get("error") or 0):
            return "\n".join([line + ".", *await _errors(ph, task_id)])
        return line + ("" if line.endswith(".") else ".")
    except SynologyError as e:
        synology_error_response(op, e)
