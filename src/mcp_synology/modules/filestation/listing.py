"""File Station listing tools: list_shares, list_files, list_recycle_bin."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.core.errors import ErrorCode, SynologyError
from mcp_synology.core.formatting import (
    error_response,
    format_size,
    format_table,
    format_timestamp,
    synology_error_response,
)
from mcp_synology.modules.filestation.helpers import (
    correct_recycle_status_from_observation,
    dsm_str,
    ensure_recycle_status,
    file_type_icon,
    normalize_path,
    probe_recycle_status,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

# Map common sort field names to DSM API field names
_SORT_ALIASES: dict[str, str] = {
    "modified": "mtime",
    "date": "mtime",
    "time": "mtime",
    "created": "crtime",
    "accessed": "atime",
    "changed": "ctime",
    "owner": "user",
}


def _resolve_sort_by(sort_by: str) -> str:
    return _SORT_ALIASES.get(sort_by, sort_by)


def _sort_note(files: list[dict[str, Any]], sort_by: str, sort_direction: str) -> str:
    """Say how DSM ordered a listing that mixes folders and files (#333).

    DSM sorts by type first: ascending puts every folder before the files, descending
    puts the files first, and each group is sorted on its own (verified on DSM 7.1,
    7.3.2 and seska's 7.1.1 for name, size, mtime and ctime). Without the note a
    listing sorted by date looks unsorted where the groups meet.
    """
    kinds = {bool(f.get("isdir")) for f in files}
    if len(kinds) < 2 or sort_by == "type":
        return ""
    first = "files" if sort_direction == "desc" else "folders"
    return f"\nDSM sorts folders and files separately; {first} come first."


def dsm_pattern_matcher(pattern: str) -> Any:
    """Match names the way DSM's ``List.list`` ``pattern`` does (#333).

    Measured on DSM 7.1 and 7.3.2: case is ignored; a comma separates alternatives; a
    pattern without ``*`` or ``?`` matches anywhere in the name; with one, it must match
    the whole name; ``[...]`` is not a character class (``[ab]2*`` matched nothing).
    """
    regexes = []
    for part in (p.strip() for p in pattern.split(",")):
        if not part:
            continue
        body = re.escape(part).replace(r"\*", ".*").replace(r"\?", ".")
        if "*" not in part and "?" not in part:
            body = f".*{body}.*"
        regexes.append(re.compile(body, re.IGNORECASE | re.DOTALL))

    def matches(name: str) -> bool:
        return any(r.fullmatch(name) for r in regexes)

    return matches


def _volume_name(real_path: object) -> str:
    """``/volume1/homes/admin`` -> ``volume1``; ``—`` when DSM didn't send it."""
    if not isinstance(real_path, str) or not real_path.startswith("/"):
        return "—"
    return real_path.split("/")[1] or "—"


def _volume_free(volume_status: object) -> str:
    """Free space of the share's volume, e.g. ``1.5 TiB of 36.6 TiB``."""
    if not isinstance(volume_status, dict) or "freespace" not in volume_status:
        return "—"
    free = format_size(int(volume_status["freespace"]))
    total = volume_status.get("totalspace")
    text = f"{free} of {format_size(int(total))}" if total else free
    return f"{text}, read-only" if volume_status.get("readonly") else text


# The fields each table shows; nothing else is fetched (#149). No "size": DSM's
# list_share never returns one for a share (#151).
_SHARE_FIELDS = '["real_path","owner","volume_status"]'
_FILE_FIELDS = '["size","time"]'


# list_shares checks every share's recycle bin not yet known (#255, #335), a few at a
# time, and waits at most RECYCLE_COLUMN_TIMEOUT seconds for all of them together, so a
# busy NAS adds at most that to the listing; checks still running then are canceled.
RECYCLE_PROBES_AT_ONCE = 4
RECYCLE_COLUMN_TIMEOUT = 15.0
# "none yet": no #recycle folder, and this account can't read the share's setting (#409).
_RECYCLE_LABELS = {True: "enabled", False: "disabled", None: "couldn't check", "": "none yet"}


async def _recycle_column(
    client: DsmClient, names: list[str], recycle_status: dict[str, bool]
) -> dict[str, bool | str | None]:
    """Each share's recycle-bin status: cached, else probed; definite answers are cached.

    A share with no ``#recycle`` folder whose setting this account can't read is ``""``.
    """
    gate = asyncio.Semaphore(RECYCLE_PROBES_AT_ONCE)

    async def probe(name: str) -> bool | str | None:
        async with gate:
            try:
                status, definite = await probe_recycle_status(client, name)
            except httpx.HTTPError as e:
                logger.debug("Recycle-bin probe on /%s got no answer (%r).", name, e)
                return None
            if status is not None and definite:
                recycle_status[name] = status
            return "" if status is False and not definite else status

    unknown = [n for n in dict.fromkeys(names) if n and n not in recycle_status]
    tasks = {name: asyncio.ensure_future(probe(name)) for name in unknown}
    if tasks:
        try:
            _done, pending = await asyncio.wait(tasks.values(), timeout=RECYCLE_COLUMN_TIMEOUT)
        finally:
            for task in tasks.values():
                task.cancel()
        if pending:
            logger.debug(
                "Recycle-bin checks still running after %.0f s, canceled: %s",
                RECYCLE_COLUMN_TIMEOUT,
                ", ".join(n for n, t in tasks.items() if t in pending),
            )
            await asyncio.gather(*pending, return_exceptions=True)
    found: dict[str, bool | str | None] = {
        name: task.result() if task.done() and not task.cancelled() else None
        for name, task in tasks.items()
    }
    return {n: recycle_status[n] if n in recycle_status else found.get(n) for n in names}


async def list_shares(
    client: DsmClient,
    *,
    sort_by: str = "name",
    sort_direction: str = "asc",
    recycle_bin_status: dict[str, bool] | None = None,
    hostname: str = "NAS",
    file_type_indicator: str = "emoji",
) -> str:
    """List all shared folders on the NAS."""
    sort_by = _resolve_sort_by(sort_by)

    try:
        data = await client.request(
            "SYNO.FileStation.List",
            "list_share",
            params={
                "additional": _SHARE_FIELDS,
                "sort_by": sort_by,
                "sort_direction": sort_direction,
            },
        )
    except SynologyError as e:
        synology_error_response("List shares", e)

    shares = data.get("shares", [])
    if not shares:
        return format_table(
            headers=["Name", "Path"],
            rows=[],
            title=f"Shared Folders on {hostname}",
        )

    headers = ["Name", "Path", "Volume", "Free", "Owner"]
    recycle: dict[str, bool | str | None] = {}
    if recycle_bin_status is not None:
        headers.append("Recycle Bin")
        recycle = await _recycle_column(
            client, [str(share.get("name", "")) for share in shares], recycle_bin_status
        )

    rows: list[list[str]] = []
    shows_free = False
    for share in shares:
        name = share.get("name", "")
        path = share.get("path", f"/{name}")
        add_info = share.get("additional") or {}
        free = _volume_free(add_info.get("volume_status"))
        shows_free = shows_free or free != "—"
        owner = add_info.get("owner", {}).get("user", "—")
        row = [name, path, _volume_name(add_info.get("real_path")), free, owner]
        if recycle_bin_status is not None:
            status = recycle.get(name)
            row.append(_RECYCLE_LABELS[status])
        rows.append(row)

    total = data.get("total", len(shares))
    result = format_table(headers=headers, rows=rows, title=f"Shared Folders on {hostname}")
    result += f"\n\n{total:,} shared folders found."
    if None in recycle.values():
        result += (
            "\n\nRecycle Bin \"couldn't check\": the NAS didn't answer in time or refused the "
            "check (for example, this account can't read that share's #recycle), so whether "
            "files deleted there can be recovered is unknown."
        )
    if "" in recycle.values():
        result += (
            '\n\nRecycle Bin "none yet": the share has no #recycle folder, so its bin is off, '
            "or nothing has been deleted into it yet (DSM creates the folder at the first "
            "delete); this account can't read the share's setting. After a delete there, "
            "delete_files says whether the files can be recovered."
        )
    if shows_free:
        result += (
            "\n\nFree space is per volume: shares on one volume share it. "
            "For how much space a share uses, call get_dir_size on its path."
        )
    result += (
        "\n\nPaths shown above are the root for all file operations."
        '\nExample: to list files in the video share, use list_files(path="/video")'
    )
    return result


async def list_files(
    client: DsmClient,
    *,
    path: str,
    pattern: str | None = None,
    filetype: str = "all",
    sort_by: str = "name",
    sort_direction: str = "asc",
    offset: int = 0,
    limit: int = 200,
    hide_recycle: bool = True,
    file_type_indicator: str = "emoji",
) -> str:
    """List files and folders within a directory path."""

    normalized = normalize_path(path)

    sort_by = _resolve_sort_by(sort_by)

    params: dict[str, Any] = {
        "folder_path": normalized,
        "additional": _FILE_FIELDS,
        "filetype": filetype,
        "sort_by": sort_by,
        "sort_direction": sort_direction,
        "offset": str(offset),
        "limit": str(limit),
    }
    if pattern:
        params["pattern"] = dsm_str(pattern)

    try:
        data = await client.request("SYNO.FileStation.List", "list", params=params)
    except SynologyError as e:
        synology_error_response("List files", e)

    files = data.get("files", [])
    total = data.get("total", len(files))

    # Optionally filter out #recycle (disabled by default)
    if hide_recycle:
        files = [f for f in files if f.get("name") != "#recycle"]

    if not files:
        return format_table(
            headers=["Type", "Name"],
            rows=[],
            title=f"Contents of {normalized} (0 items)",
        )

    headers = ["Type", "Name", "Size", "Modified"]
    rows: list[list[str]] = []
    for f in files:
        is_dir = f.get("isdir", False)
        name = f.get("name", "")
        display_name = name + "/" if is_dir else name
        icon = file_type_icon(is_dir, name, style=file_type_indicator)

        add_info = f.get("additional", {})
        size = "—" if is_dir or "size" not in add_info else format_size(add_info["size"])

        time_info = add_info.get("time", {})
        mtime = time_info.get("mtime", 0)
        modified = format_timestamp(mtime) if mtime else "—"

        rows.append([icon, display_name, size, modified])

    shown = len(files)
    title = f"Contents of {normalized} ({total:,} items)"
    result = format_table(headers=headers, rows=rows, title=title)
    result += _sort_note(files, sort_by, sort_direction)

    # The range, not just the count, so a later page says where it is: "Showing 3–4 of
    # 5", as the recycle-bin pattern listing does (0.10.0rc6 live test).
    next_offset = offset + shown
    page = f"{offset + 1:,}–{next_offset:,}"
    if next_offset < total:
        result += f"\n\nShowing {page} of {total:,} items. Use offset={next_offset} to see more."
    elif shown > 0 and offset > 0:
        result += f"\n\nShowing {page} of {total:,} items."
    elif shown > 0:
        result += f"\n\n{shown:,} of {total:,} items."

    return result


async def _not_enabled(
    client: DsmClient,
    share_name: str,
    recycle_bin_status: dict[str, bool] | None,
    *,
    definite: bool = False,
) -> str:
    """Say the share has no bin, once DSM confirms the share itself is there (#353).

    Only a ``definite`` answer (the share's own setting) says the bin is off; a missing
    ``#recycle`` alone doesn't (#409).

    DSM answers 408 for ``/<share>/#recycle`` whenever the folder is missing (the bin off,
    or on with nothing deleted into it yet) and also when the share isn't there: a typo, or
    ``/Home`` for the home service, which only answers as ``home``.
    Without this check, a restorable file read as "cannot be recovered".
    """
    try:
        await client.request(
            "SYNO.FileStation.List", "list", params={"folder_path": f"/{share_name}", "limit": 1}
        )
    except SynologyError as e:
        if e.code != 408:
            synology_error_response("List recycle bin", e)
        if recycle_bin_status is not None:
            recycle_bin_status.pop(share_name, None)
        home = (
            " The user home folder is /home (lowercase)." if share_name.casefold() == "home" else ""
        )
        error_response(
            ErrorCode.NOT_FOUND,
            f"No shared folder /{share_name} on this NAS.{home}",
            retryable=False,
            param="share",
            value=share_name,
            suggestion="Call list_shares and use a share name as it lists it.",
        )
    if definite:
        return f"Recycle bin is not enabled on /{share_name}. Deleted files cannot be recovered."
    return (
        f"/{share_name} has no recycle bin folder (#recycle), so there's nothing in it to "
        "list: its bin is off, or nothing has been deleted into it yet (DSM creates the "
        "folder at the first delete). After a delete there, delete_files says whether the "
        "files can be recovered."
    )


async def list_recycle_bin(
    client: DsmClient,
    *,
    share: str,
    pattern: str | None = None,
    sort_by: str = "mtime",
    sort_direction: str = "desc",
    limit: int = 100,
    offset: int = 0,
    file_type_indicator: str = "emoji",
    recycle_bin_status: dict[str, bool] | None = None,
) -> str:
    """List contents of a shared folder's recycle bin."""
    offset = max(0, offset)  # a negative slice would page from the end
    # Normalize share name
    share_name = share.strip("/").split("/")[0]

    # Probe (or hit cache) for recycle-bin status before issuing the actual list
    # call. Pre-#37 the cache was always empty, so this fast-path early-return
    # never fired and every share fell into the list-and-catch-408 path below.
    if recycle_bin_status is not None:
        enabled = await ensure_recycle_status(client, share_name, recycle_bin_status)
        if not enabled:
            return await _not_enabled(
                client,
                share_name,
                recycle_bin_status,
                definite=recycle_bin_status.get(share_name) is False,
            )

    recycle_path = f"/{share_name}/#recycle"

    try:
        if pattern:
            # DSM matches a pattern only at the level listed, and its Search refuses
            # #recycle (600, admin included; seska 7.1.1), so the bin is walked here.
            result = await _search_recycle_bin(
                client,
                recycle_path,
                pattern=pattern,
                sort_by=sort_by,
                sort_direction=sort_direction,
                limit=limit,
                offset=offset,
                file_type_indicator=file_type_indicator,
            )
            if recycle_bin_status is not None:
                correct_recycle_status_from_observation(
                    share_name, observed_enabled=True, recycle_status=recycle_bin_status
                )
            return result
        result = await list_files(
            client,
            path=recycle_path,
            pattern=pattern,
            sort_by=sort_by,
            sort_direction=sort_direction,
            limit=limit,
            offset=offset,
            hide_recycle=False,
            file_type_indicator=file_type_indicator,
        )
    except ToolError as e:
        # list_files converts SynologyError to ToolError with a JSON envelope.
        # DSM error 408 = "No such file or directory": there's no #recycle folder. That
        # doesn't mean the bin is off: DSM creates the folder at the share's creation and
        # at the first delete into the bin, so it's also missing when nothing has been
        # deleted into a bin that's on, or after #recycle itself was deleted (#409).
        try:
            body = json.loads(str(e))
            if body.get("error", {}).get("code") == "not_found":
                logger.debug("Recycle bin path %s not found", recycle_path)
                # A cached "on" is dropped, not flipped to off (#409): the next call
                # checks again, and finds the folder once a delete has re-created it.
                if recycle_bin_status is not None:
                    correct_recycle_status_from_observation(
                        share_name, observed_enabled=False, recycle_status=recycle_bin_status
                    )
                return await _not_enabled(client, share_name, recycle_bin_status)
        except (json.JSONDecodeError, TypeError):
            pass
        raise

    # Successful list confirms recycle is enabled — self-correct if cache disagreed.
    if recycle_bin_status is not None:
        correct_recycle_status_from_observation(
            share_name, observed_enabled=True, recycle_status=recycle_bin_status
        )
    return result


# Caps for walking a recycle bin (#333): enough for a typical bin, and a walk that
# hits one says so, so a partial search never reads as complete.
_WALK_MAX_FOLDERS = 200
_WALK_MAX_ENTRIES = 5000
_WALK_PAGE = 1000
_WALK_SORT_KEYS = {"name", "size", "mtime", "ctime", "crtime", "atime"}


async def _search_recycle_bin(
    client: DsmClient,
    recycle_path: str,
    *,
    pattern: str,
    sort_by: str,
    sort_direction: str,
    limit: int,
    offset: int,
    file_type_indicator: str,
) -> str:
    """Find ``pattern`` anywhere in a recycle bin: subfolders included, paths in the bin.

    A missing ``#recycle`` raises the ``not_found`` ToolError ``list_files`` would, so the
    caller answers it the same way: "has no recycle bin folder", or "not enabled" only when
    the share's setting says the bin is off (#409).
    """
    matches = dsm_pattern_matcher(pattern)
    found: list[tuple[str, dict[str, Any]]] = []
    # Each folder's path in the bin is built from the walk itself, never taken from
    # DSM's ``path`` (its case or form could differ from the share name given).
    queue = [""]
    folders = entries = 0
    unreadable: list[str] = []
    stopped = False
    while queue and not stopped:
        folder_rel = queue.pop(0)
        # A folder is left: stop before opening it if a cap is reached.
        if folders >= _WALK_MAX_FOLDERS or entries >= _WALK_MAX_ENTRIES:
            stopped = True
            break
        folders += 1
        folder = f"{recycle_path}/{folder_rel}" if folder_rel else recycle_path
        listed = 0
        while True:
            params = {
                "folder_path": folder,
                "additional": _FILE_FIELDS,
                "offset": str(listed),
                "limit": str(_WALK_PAGE),
            }
            try:
                data = await client.request("SYNO.FileStation.List", "list", params=params)
            except SynologyError as e:
                if not folder_rel:
                    synology_error_response("List recycle bin", e)
                unreadable.append(folder_rel)
                break
            page = data.get("files", [])
            for f in page:
                if entries >= _WALK_MAX_ENTRIES:
                    stopped = True  # there's at least one more entry, unread
                    break
                entries += 1
                name = str(f.get("name", ""))
                rel = f"{folder_rel}/{name}" if folder_rel else name
                if matches(name):
                    found.append((rel, f))
                if f.get("isdir"):
                    queue.append(rel)
            listed += len(page)
            if stopped or not page or listed >= int(data.get("total", 0)):
                break

    resolved = _resolve_sort_by(sort_by)
    key = resolved if resolved in _WALK_SORT_KEYS else "mtime"
    fallback = (
        f"sort_by={sort_by} doesn't apply to a search of the bin; sorted by mtime."
        if key != resolved
        else ""
    )

    def sort_value(item: tuple[str, dict[str, Any]]) -> Any:
        rel, f = item
        add = f.get("additional", {})
        if key == "name":
            return rel.casefold()
        if key == "size":
            return 0 if f.get("isdir") else int(add.get("size", 0))
        return int(add.get("time", {}).get(key, 0))

    found.sort(key=sort_value, reverse=sort_direction == "desc")
    # limit 0 or less lists every match, as DSM's limit=0 lists a whole folder.
    shown = found[offset : offset + limit] if limit > 0 else found[offset:]
    title = (
        f'Matches for "{pattern}" in {recycle_path} ({len(found):,} found in '
        f"{folders:,} folder(s), subfolders included)"
    )
    rows = []
    for rel, f in shown:
        is_dir = bool(f.get("isdir"))
        add = f.get("additional", {})
        size = "—" if is_dir or "size" not in add else format_size(add["size"])
        mtime = add.get("time", {}).get("mtime", 0)
        rows.append(
            [
                file_type_icon(is_dir, str(f.get("name", "")), style=file_type_indicator),
                rel + ("/" if is_dir else ""),
                size,
                format_timestamp(mtime) if mtime else "—",
            ]
        )
    result = format_table(
        headers=["Type", "Path in bin", "Size", "Modified"], rows=rows, title=title
    )
    notes = [fallback] if fallback else []
    if found and offset >= len(found):
        notes.append(f"No matches from offset={offset}: there are {len(found):,}.")
    elif len(found) > len(shown):
        end = offset + len(shown)
        more = f" Use offset={end} to see more." if end < len(found) else ""
        notes.append(f"Showing {offset + 1:,}–{end:,} of {len(found):,} matches.{more}")
    if found:
        notes.append("Paths are relative to the recycle bin, as a restore takes them.")
    if unreadable:
        more = f" and {len(unreadable) - 5} more" if len(unreadable) > 5 else ""
        notes.append(f"Not searched (couldn't be listed): {', '.join(unreadable[:5])}{more}.")
    if stopped:
        notes.append(
            f"Stopped after {folders:,} folder(s) and {entries:,} item(s), so there may be more "
            f"matches: narrow the pattern, or list a subfolder with list_files on "
            f"{recycle_path}/<folder>."
        )
    return result + ("\n\n" + "\n".join(notes) if notes else "")
