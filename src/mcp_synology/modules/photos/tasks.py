"""Photos background tasks: copy_photos, move_photos, delete_photos,
delete_photo_folders and photo_task.

``BackgroundTask.File copy|move|delete`` starts a task; ``BackgroundTask.Info``
(personal API, covering both spaces) reports it. Each tool polls once a second, as
the web client does, until the task is ``done`` or ``task_timeout`` runs out, then
hands it off with a ``photos:<id>`` handle that ``photo_task`` follows up on, the
pattern File Station's ``file_task`` uses (#245). A status request that gets no
answer, an HTTP error status, a reply that isn't DSM's (a reverse proxy's 502/504 or
login page, #282) or any other unexpected answer hands off too: the task is most likely
still running. A DSM error code on a status request stops the task, and so does
cancellation, as File Station's background tasks (CLAUDE.md, background task cleanup).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, NoReturn

import anyio
import httpx

from mcp_synology.core.errors import (
    DsmHttpError,
    DsmReplyError,
    ErrorCode,
    RequestTooLongError,
    SynologyError,
)
from mcp_synology.core.formatting import (
    error_response,
    format_table,
    format_timestamp,
    synology_error_response,
)
from mcp_synology.modules.filestation.helpers import describe_transport_error
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
    error), a proxy's HTTP error or page (#282), which is how a proxy reports the same
    stall, or any other unexpected answer. The task is most likely still running, so it
    isn't lost. As File Station's ``poll_task`` (CLAUDE.md, background task cleanup): a
    DSM error code on a status request stops the task, and the error (re-raised) says
    whether it was stopped; so does cancellation, which then propagates.
    """
    task_id = int(task["id"])
    deadline = time.monotonic() + timeout
    status = task
    try:
        return await _poll(ph, task_id, status, deadline)
    except asyncio.CancelledError:
        # anyio cancel scopes (the MCP SDK's) are level-triggered: shield the stop or
        # it is canceled again at once and the NAS task is orphaned.
        with anyio.CancelScope(shield=True):
            await _abort(ph, task_id)
        raise


async def _abort(ph: Photos, task_id: int) -> bool:
    """Stop a task; whether DSM confirmed it. Failures are logged, never raised."""
    try:
        await ph.call("BackgroundTask.Info", "abort_task", {"id": [task_id]}, personal=True)
    except Exception as e:  # never let a failed stop replace the error being reported
        logger.warning("Couldn't stop Photos task %s: %s", task_id, type(e).__name__)
        return False
    return True


async def _poll(
    ph: Photos, task_id: int, status: dict[str, Any], deadline: float
) -> tuple[dict[str, Any], str | None]:
    while status.get("status") != "done":
        if time.monotonic() >= deadline:
            return status, "timeout"
        await asyncio.sleep(POLL_INTERVAL)
        try:
            now = await _status(ph, task_id)
        except httpx.TransportError as e:
            logger.warning("Photos task %s status got no answer: %s", task_id, type(e).__name__)
            return status, f"The NAS didn't answer a status check ({type(e).__name__})"
        except (DsmHttpError, RequestTooLongError) as e:
            # A status URL is short, so a 414 comes from a proxy, not the request (#325).
            http = e.status if isinstance(e, DsmHttpError) else 414
            logger.warning("Photos task %s status got HTTP %s", task_id, http)
            return status, (
                "The NAS, or a proxy in front of it, answered a status check with an error "
                f"(HTTP {http})"
            )
        except DsmReplyError:
            logger.warning("Photos task %s status reply wasn't a DSM reply", task_id)
            return status, (
                "The NAS's status reply couldn't be read (not a DSM reply; a proxy or captive "
                "portal may be answering)"
            )
        except SynologyError as e:
            # DSM's own verdict on the status request: stop the task rather than leave it
            # running unreported, and say which happened.
            stopped = await _abort(ph, task_id)
            note = (
                " The task was stopped on the NAS; what was already done stays done."
                if stopped
                else f" Stopping it failed, so it may still be running: {handle_of(task_id)}"
                " (check with photo_task)."
            )
            text = str(e).rstrip()
            e.args = (f"{text}{'' if text.endswith(('.', '!', '?')) else '.'}{note}",)
            raise
        except Exception as e:
            # Any other unexpected answer: the task may well still be running, so hand it
            # off rather than lose it (as File Station, QA R1 on #251).
            logger.warning("Photos task %s status unreadable: %s", task_id, type(e).__name__)
            return status, f"The NAS's status reply couldn't be read ({type(e).__name__})"
        if now is None:
            # Gone from the list: cleared, which only happens once it's finished.
            return {**status, "status": "done"}, None
        status = now
    return status, None


def _progress(status: dict[str, Any]) -> str:
    total = int(status.get("total") or 0)
    done = int(status.get("completion") or 0)
    return f"{done} of {total} processed"


def _counts(status: dict[str, Any]) -> str:
    skip, over = int(status.get("skip") or 0), int(status.get("overwrite") or 0)
    errors = int(status.get("error") or 0)
    text = f"{_progress(status)}, {skip} skipped (already there), {over} overwritten"
    return text + (f", {errors} failed" if errors else "")


def _finished_line(operation: str, what: str, where: str, status: dict[str, Any]) -> str:
    """The headline of a finished copy or move. DSM's ``completion`` counts skipped and
    failed items too, so a headline never claims items moved that were skipped (rc4)."""
    skipped, failed = int(status.get("skip") or 0), int(status.get("error") or 0)
    if not skipped and not failed:
        done = "Copied" if operation == "copy" else "Moved"
        return f"{done} {what} to {where}: {_counts(status)}."
    if int(status.get("completion") or 0) - skipped - failed <= 0:
        noun = "copied" if operation == "copy" else "moved"
        return f"Nothing was {noun} to {where} ({what} requested): {_counts(status)}."
    kind = "Copy" if operation == "copy" else "Move"
    return f"{kind} of {what} to {where} finished: {_counts(status)}."


async def _errors(ph: Photos, task_id: int) -> list[str]:
    try:
        data = await ph.call(
            "BackgroundTask.Info", "get_error_detail", {"id": task_id}, personal=True
        )
    except SynologyError as e:
        return [f"  (couldn't read the failed items: {e})"]
    except httpx.TransportError as e:
        return [f"  (couldn't read the failed items: DSM didn't answer, {type(e).__name__})"]
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
            lines = [_finished_line(operation, what, where, status)]
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
    except (SynologyError, httpx.TransportError) as e:
        # The delete itself is done: an unreadable setting only makes the note vaguer.
        logger.debug("Couldn't read the recycle-bin setting: %s", type(e).__name__)
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


# A 414 too: a status, stop or lookup URL is short, so it comes from a proxy (#325).
_NO_ANSWER = (httpx.TransportError, DsmHttpError, DsmReplyError, RequestTooLongError)


async def _target(
    client: DsmClient, op: str, status: dict[str, Any], *, lookup: bool = True
) -> tuple[str, bool]:
    """`` to <folder path>`` for a task's target, and whether DSM answered the lookup.

    If the lookup gets no answer (or ``lookup`` is off, after an earlier one didn't), the
    folder's ID stands in: the task's status is already in hand.
    """
    folder = status.get("target_folder") or {}
    if "id" not in folder:
        return "", True
    space: Space = "shared" if folder.get("owner_user_id") == 0 else "personal"
    suffix = " (Shared Space)" if space == "shared" else ""
    if lookup:
        try:
            path = await Photos(client, space, op).folder_path(int(folder["id"]))
        except _NO_ANSWER:
            pass
        else:
            return f" to {path}{suffix}", True
    return f" to folder {folder['id']}{suffix}", False


def _running_for(status: dict[str, Any]) -> str:
    created = status.get("create_time")
    if not created:
        return ""
    seconds = max(0, int(time.time() - int(created) / 1000))
    minutes, sec = divmod(seconds, 60)
    return f", running {minutes} min {sec} s" if minutes else f", running {sec} s"


def _transport_what(exc: httpx.TransportError) -> str:
    if _never_sent(exc):
        return "couldn't be reached"
    return "didn't answer" if isinstance(exc, httpx.TimeoutException) else "lost the connection"


def _never_sent(exc: Exception) -> bool:
    """The request never reached DSM: the connection couldn't be opened."""
    return isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)


def _request_failed(op: str, task_id: int, exc: Exception, *, request: str, note: str) -> NoReturn:
    """The structured error for a status or stop request that got no answer, an HTTP error
    status or a reply that isn't DSM's (#282): it names the handle, never the page. The
    wording follows File Station's file_task (#237)."""
    handle = handle_of(task_id)
    follow_up = "Call photo_task with this handle to see whether the task is still running."
    suggestion = follow_up
    if isinstance(exc, httpx.TransportError):
        timed_out = isinstance(exc, httpx.TimeoutException)
        code = ErrorCode.TIMEOUT if timed_out else ErrorCode.UNAVAILABLE
        if _never_sent(exc):
            what = "couldn't be reached for"
        else:
            what = "didn't answer" if timed_out else "lost the connection during"
        detail = f"DSM {what} the {request} request ({describe_transport_error(exc)})."
        retryable = True
    else:
        code, detail = ErrorCode.UNAVAILABLE, str(exc)
        retryable = bool(getattr(exc, "retryable", True))
        # A 414's own suggestion (make the request smaller) doesn't fit a short URL.
        hint = None if isinstance(exc, RequestTooLongError) else getattr(exc, "suggestion", None)
        suggestion = f"{hint} {follow_up}" if hint else follow_up
    error_response(
        code,
        f"{op} failed for `{handle}`: {detail}{note}",
        retryable=retryable,
        param="handle",
        value=handle,
        suggestion=suggestion,
    )


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
            rows = []
            lookup = True  # after one folder lookup gets no answer, don't wait on the rest
            for t in tasks:
                target, lookup = await _target(client, op, t, lookup=lookup)
                rows.append(
                    [
                        handle_of(int(t["id"])),
                        str(t.get("operation", "")),
                        target.removeprefix(" to ") or "—",
                        str(t.get("status", "")),
                        f"{t.get('completion', 0)}/{t.get('total', 0)}",
                        format_timestamp(int(t["create_time"]) / 1000)
                        if t.get("create_time")
                        else "—",
                    ]
                )
            return format_table(
                ["Handle", "Operation", "Target folder", "Status", "Done", "Started"],
                rows,
                title=f"Photos tasks ({len(rows)})",
            )
        if action == "stop":
            try:
                await ph.call("BackgroundTask.Info", "abort_task", {"id": [task_id]}, personal=True)
            except _NO_ANSWER as e:
                # A 414 is rejected before anything acts on it, like a refused connection.
                note = (
                    " Nothing was stopped."
                    if _never_sent(e) or isinstance(e, RequestTooLongError)
                    else " The stop may or may not have taken effect."
                )
                _request_failed(op, task_id, e, request="stop", note=note)
            except SynologyError as e:
                if e.code == 117:
                    not_found(op, f"no task {handle_of(task_id)}.", param="handle", value=handle)
                raise
        try:
            status = await _status(ph, task_id)
        except _NO_ANSWER as e:
            note = (
                " The stop was requested; its status couldn't be read." if action == "stop" else ""
            )
            _request_failed(op, task_id, e, request="status", note=note)
        except SynologyError as e:
            # A DSM error on the status read after a stop DSM accepted: say the stop was
            # sent, or it reads as a failed stop (#323).
            if action == "stop":
                text = str(e).rstrip()
                stop = "" if text.endswith((".", "!", "?")) else "."
                e.args = (f"{text}{stop} The stop was requested; its status couldn't be read.",)
            raise
        if status is None:
            not_found(
                op,
                f"no task {handle_of(task_id)} (finished tasks disappear once cleared).",
                param="handle",
                value=handle,
            )
        line = (
            f"{handle_of(task_id)} {status.get('operation', 'task')}"
            f"{(await _target(client, op, status))[0]}: {status.get('status')}, {_counts(status)}"
        )
        if status.get("status") != "done":
            line += _running_for(status)
        if action == "stop":
            line += ". Stop requested; what was already done stays done."
        elif status.get("status") == "done" and int(status.get("error") or 0):
            return "\n".join([line + ".", *await _errors(ph, task_id)])
        return line + ("" if line.endswith(".") else ".")
    except httpx.TransportError as e:  # listing or clearing: no task to name
        timed_out = isinstance(e, httpx.TimeoutException)
        error_response(
            ErrorCode.TIMEOUT if timed_out else ErrorCode.UNAVAILABLE,
            f"{op} failed: DSM {_transport_what(e)} ({describe_transport_error(e)}).",
            retryable=True,
            suggestion="Try again shortly; a busy NAS can be slow to answer.",
        )
    except SynologyError as e:
        synology_error_response(op, e)
