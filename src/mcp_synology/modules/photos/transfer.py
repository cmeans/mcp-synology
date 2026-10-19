"""Photos transfers: download_photos (originals and previews to this machine)."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

import httpx

from mcp_synology.core.errors import ErrorCode, SynologyError
from mcp_synology.core.formatting import error_response, format_size, synology_error_response
from mcp_synology.core.local_paths import refuse_outside_roots
from mcp_synology.modules.photos.helpers import (
    SETUP_CODES,
    Photos,
    check_space,
    invalid,
    space_label,
)

if TYPE_CHECKING:
    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)

_SIZES = ("original", "xl", "m", "sm")
_MAX_DOWNLOAD = 50
_TIMEOUT = 300.0


def _safe_name(name: str, fallback: str) -> str:
    """``name`` if it's a plain file name, else ``fallback``: never write outside the folder."""
    plain = (
        name
        and name not in (".", "..")
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
        and not PureWindowsPath(name).drive
    )
    return name if plain else fallback


def _dest_name(item: dict[str, Any], size: str, used: set[str]) -> str:
    """The local file name, unique within this call.

    Items in different folders can share a filename (``IMG_0001.jpg``), and previews
    of ``a.jpg`` and ``a.png`` are both ``a_m.jpg``; a repeat gets the item ID added
    (``IMG_0001_41.jpg``), so one item never replaces another's file.
    """
    item_id = item.get("id")
    name = _safe_name(str(item.get("filename") or ""), f"item-{item_id}")
    if size != "original":
        name = f"{PurePosixPath(name).stem or f'item-{item_id}'}_{size}.jpg"
    if name.casefold() in used:
        path = PurePosixPath(name)
        name = f"{path.stem}_{item_id}{path.suffix}"
    used.add(name.casefold())
    return name


async def download_photos(
    client: DsmClient,
    *,
    ids: list[int],
    dest_folder: str | None = None,
    size: str = "original",
    space: str = "personal",
    overwrite: bool = False,
    allowed_roots: list[Path] | None = None,
) -> str:
    """Download items, one request per item (spec: download_photos).

    Two or more ``item_id``s in one Download request make DSM answer with a zip, so
    each item is fetched on its own and its result reported on its own line.
    """
    op = "Download photos"
    sp = check_space(op, space)
    if size not in _SIZES:
        invalid(op, f"unknown size {size!r}.", param="size", value=size, valid=list(_SIZES))
    if not ids:
        invalid(op, "pass at least one id.", param="ids", value=ids)
    if len(ids) > _MAX_DOWNLOAD:
        invalid(op, f"at most {_MAX_DOWNLOAD} ids per call.", param="ids", value=len(ids))
    if dest_folder is None:
        if not allowed_roots or len(allowed_roots) != 1:
            invalid(
                op,
                "dest_folder is required: there's more than one allowed local folder.",
                param="dest_folder",
                suggestion="Pass dest_folder, a local folder inside the allowed folders.",
            )
        dest_folder = str(allowed_roots[0])
    refuse_outside_roots(
        "Download photos",
        "dest_folder",
        dest_folder,
        Path(dest_folder),
        allowed_roots,
        module="photos",
        hide_unknown_users=True,
    )
    local_dir = Path(dest_folder).expanduser()
    if not local_dir.is_dir():
        error_response(
            ErrorCode.NOT_FOUND,
            f"{op} failed: local folder not found: {dest_folder}",
            retryable=False,
            param="dest_folder",
            value=dest_folder,
            suggestion="Pass an existing local folder.",
        )
    unique = list(dict.fromkeys(ids))
    try:
        ph = Photos(client, sp, op)
        data = await ph.call(
            "Browse.Item", "get", {"id": unique, "additional": ["thumbnail"]}, version=2
        )
    except SynologyError as e:
        synology_error_response(op, e)
    found = {int(i["id"]): i for i in data.get("list", [])}
    lines: list[str] = []
    done = 0
    used: set[str] = set()
    for n, item_id in enumerate(unique):
        item = found.get(item_id)
        if item is None:
            why = "not found"
            if sp == "shared":
                why += ", or no permission for its Shared Space folder"
            lines.append(f"  {item_id}  not downloaded: {why}")
            continue
        name = _dest_name(item, size, used)
        try:
            result = await _download_one(ph, item, size, local_dir / name, overwrite=overwrite)
        except httpx.TransportError as e:
            # DSM didn't answer (a dropped connection or a timeout): the rest would most
            # likely wait out the same timeout, so stop and say which weren't tried.
            lines.append(
                f"  {item_id}  {name}: not downloaded: couldn't reach DSM ({type(e).__name__})"
            )
            rest = unique[n + 1 :]
            if rest:
                lines.append(f"  not tried: {', '.join(str(i) for i in rest)}")
            break
        if result.startswith("not downloaded"):
            lines.append(f"  {item_id}  {name}: {result}")
        else:
            done += 1
            lines.append(f"  {item_id}  {name}  {result}")
    what = "original" if size == "original" else f"{size} preview"
    head = f"Downloaded {done} of {len(unique)} item(s) to {local_dir} ({what}, {space_label(sp)})"
    return "\n".join([head, *lines])


async def _download_one(
    ph: Photos, item: dict[str, Any], size: str, dest: Path, *, overwrite: bool
) -> str:
    """Download one item; a result line (``not downloaded: …`` on failure)."""
    if dest.is_dir() and not dest.is_symlink():
        return "not downloaded: a folder has that name"
    if os.path.lexists(dest) and not overwrite:
        return "not downloaded: the file exists (overwrite=true replaces it)"
    if size == "original":
        api, method, version = ph.api("Download"), "download", 1
        params = {"item_id": json.dumps([int(item["id"])]), "force_download": "true"}
    else:
        thumb = (item.get("additional") or {}).get("thumbnail") or {}
        state = thumb.get(size)
        if state != "ready":
            if state == "converting" or state is None:
                return (
                    f"not downloaded: the {size} preview isn't generated yet "
                    f"({state or 'pending'}); try again shortly"
                )
            return f"not downloaded: no {size} preview for this item ({state})"
        api, method, version = ph.api("Thumbnail"), "get", 2
        params = {
            "id": json.dumps(int(thumb.get("unit_id") or item["id"])),
            "cache_key": json.dumps(str(thumb.get("cache_key", ""))),
            "type": json.dumps("unit"),
            "size": json.dumps(size),
        }
    try:
        written = await ph.client.download_binary(
            api, method, version, params, dest, timeout=_TIMEOUT, overwrite=overwrite
        )
    except FileExistsError:
        return "not downloaded: the file appeared while downloading (overwrite=true replaces it)"
    except SynologyError as e:
        code = f" (DSM error {e.code})" if e.code else ""
        return f"not downloaded: {e}{code}"
    except OSError as e:
        if e.errno == errno.ENOSPC:
            return "not downloaded: no space left on the local disk"
        return f"not downloaded: couldn't write the file ({e.strerror or e})"
    return format_size(written)


# -- upload_photos ------------------------------------------------------------------

# Upload errors the web client doesn't retry (604, 605, 620, 622), plus a missing folder
# (621) and the setup and permission codes. Anything else is retried once, as the web
# client does (UPLOAD retry count 1): vdsm answered 623 to some uploads right after boot,
# and the retry went through.
_UPLOAD_FINAL = frozenset({604, 605, 620, 621, 622}) | SETUP_CODES
UPLOAD_RETRY_DELAY = 2.0


async def _upload_one(
    client: DsmClient, api: str, fields: dict[str, Any], path: Path
) -> dict[str, Any]:
    async def send() -> dict[str, Any]:
        return await client.upload_multipart(
            api,
            "upload_to_folder",
            1,
            {k: json.dumps(v) for k, v in fields.items()},
            file_path=path,
            filename=path.name,
        )

    try:
        return await send()
    except SynologyError as e:
        if e.code is None or e.code in _UPLOAD_FINAL:
            raise
        logger.info("Photos upload of %s got %s; retrying once", path.name, e.code)
        await asyncio.sleep(UPLOAD_RETRY_DELAY)
        return await send()


_MAX_UPLOAD = 50
_DUPLICATE = {"rename": "rename", "skip": "ignore"}
_ACTIONS = {"new": "new", "rename": "renamed", "ignore": "skipped"}


async def upload_photos(
    client: DsmClient,
    *,
    local_paths: list[str],
    folder: str = "/",
    space: str = "personal",
    on_duplicate: str = "rename",
    album: str | None = None,
    allowed_roots: list[Path] | None = None,
) -> str:
    """Upload local files into a Photos folder (spec: upload_photos), one request each."""
    op = "Upload photos"
    sp = check_space(op, space)
    if on_duplicate not in _DUPLICATE:
        invalid(
            op,
            f"unknown on_duplicate {on_duplicate!r}.",
            param="on_duplicate",
            value=on_duplicate,
            valid=list(_DUPLICATE),
        )
    if not local_paths:
        invalid(op, "pass at least one local path.", param="local_paths", value=local_paths)
    if len(local_paths) > _MAX_UPLOAD:
        invalid(
            op,
            f"at most {_MAX_UPLOAD} files per call.",
            param="local_paths",
            value=len(local_paths),
        )
    if album is not None and sp == "shared":
        invalid(
            op,
            "albums are personal space only; upload to the personal space to add "
            "the files to an album.",
            param="album",
            value=album,
        )
    files: list[Path] = []
    for value in local_paths:
        # Before any existence check, so a refusal doesn't reveal what exists outside.
        refuse_outside_roots(
            op,
            "local_paths",
            value,
            Path(value),
            allowed_roots,
            module="photos",
            hide_unknown_users=True,
        )
        path = Path(value).expanduser()
        if not path.is_file():
            error_response(
                ErrorCode.NOT_FOUND,
                f"{op} failed: not a local file: {value}",
                retryable=False,
                param="local_paths",
                value=value,
                suggestion="Pass files, not folders; check the path.",
            )
        files.append(path)
    try:
        ph = Photos(client, sp, op)
        target = await ph.folder(folder)
        target_album = await ph.album(album) if album is not None else None
    except SynologyError as e:
        synology_error_response(op, e)
    rows: list[tuple[str, str, str]] = []
    uploaded: list[int] = []
    renamed: list[int] = []
    for n, path in enumerate(files):
        try:
            fields = {
                "duplicate": _DUPLICATE[on_duplicate],
                "name": path.name,
                # Photos shows this as the taken time until it indexes the file's EXIF.
                "mtime": int(path.stat().st_mtime),
                "target_folder_id": int(target["id"]),
            }
            data = await _upload_one(client, ph.api("Upload.Item"), fields, path)
        except SynologyError as e:
            code = f" ({e.code})" if e.code else ""
            rows.append((path.name, "failed", f"{e}{code}"))
            continue
        except OSError as e:
            rows.append((path.name, "failed", f"couldn't read the file ({e.strerror or e})"))
            continue
        except httpx.TransportError as e:
            # DSM didn't answer: the rest would most likely wait out the same timeout, so
            # stop and say which weren't tried (as download_photos). The upload may have
            # landed, so it isn't reported as failed outright.
            rows.append(
                (
                    path.name,
                    "unknown",
                    f"DSM didn't answer ({type(e).__name__}); it may or may not have been stored",
                )
            )
            rows += [(rest.name, "not tried", "") for rest in files[n + 1 :]]
            break
        action = str(data.get("action", "new"))
        item_id = int(data["id"])
        uploaded.append(item_id)
        note = f"id {item_id}"
        if action == "ignore":
            note += " (already there)"
        elif action == "rename":
            renamed.append(item_id)
        rows.append((path.name, _ACTIONS.get(action, action), note))
    if renamed:
        try:
            data = await ph.call("Browse.Item", "get", {"id": renamed, "additional": []}, version=2)
            names = {int(i["id"]): str(i.get("filename", "")) for i in data.get("list", [])}
            rows = [
                (name, state, f"{note} (as {names[int(note.split()[1])]})")
                if state == "renamed" and int(note.split()[1]) in names
                else (name, state, note)
                for name, state, note in rows
            ]
        except (SynologyError, httpx.TransportError) as e:
            # The uploads are done; only the "(as …)" names are missing.
            logger.debug("Couldn't read the renamed items' names: %s", type(e).__name__)
    width = max(len(r[0]) for r in rows)
    lines = [
        f"Uploaded {len(uploaded)} of {len(files)} file(s) to {target.get('name')} "
        f"({space_label(sp)})"
    ]
    lines += [f"  {name:<{width}}  {state:<8} {note}" for name, state, note in rows]
    if target_album is not None and uploaded:
        try:
            await ph.call(
                "Browse.NormalAlbum",
                "add_item",
                {"id": int(target_album["id"]), "item": uploaded},
                personal=True,
            )
            lines.append(f'Added {len(uploaded)} item(s) to album "{target_album.get("name")}".')
        except SynologyError as e:
            lines.append(f'Couldn\'t add them to album "{target_album.get("name")}": {e}')
        except httpx.TransportError as e:
            lines.append(
                f"Couldn't add them to album \"{target_album.get('name')}\": DSM didn't answer "
                f"({type(e).__name__}). Check the album with list_photos before adding again."
            )
    if uploaded:
        lines.append(
            "Photos indexes new items over the next seconds to minutes; taken dates and "
            "previews appear then."
        )
    return "\n".join(lines)
