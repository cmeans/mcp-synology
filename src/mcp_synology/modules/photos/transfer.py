"""Photos transfers: download_photos (originals and previews to this machine)."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import time
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

import httpx

from mcp_synology.core.errors import DsmHttpError, DsmReplyError, ErrorCode, SynologyError
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
    from collections.abc import Awaitable, Callable

    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)

_SIZES = ("original", "xl", "m", "sm")
_MAX_DOWNLOAD = 50
_TIMEOUT = 300.0


def _safe_name(name: str, fallback: str) -> str:
    """``name`` if it's a plain file name, else ``fallback``: never write outside the folder.

    Trailing dots and spaces are dropped: Windows strips them, so ``a.jpg.`` and
    ``a.jpg`` would be one file there (QA round 1 on #462).
    """
    name = name.rstrip(". ")
    plain = (
        name
        and name not in (".", "..")
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
        and not PureWindowsPath(name).drive
    )
    return name if plain else fallback


def _name_key(name: str) -> str:
    """Names that are one file on some filesystem compare equal: case (Windows, macOS) and
    Unicode normalization (macOS treats NFC and NFD as one name; QA round 1 on #462), as
    Unicode's canonical caseless match does (NFD, casefold, then NFC for a stable key)."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", name).casefold())


def _dest_name(item: dict[str, Any], size: str, used: set[str]) -> str:
    """The local file name, unique within this call.

    Items in different folders can share a filename (``IMG_0001.jpg``), and previews
    of ``a.jpg`` and ``a.png`` are both ``a_m.jpg``; a repeat gets the item ID added
    (``IMG_0001_41.jpg``), so one item never replaces another's file. That name can be
    taken too, by an item really named so (``a_9.jpg``): then a counter follows the ID
    (``a_9_2.jpg``) until the name is free (#450). Names depend only on the items and
    their order, so the same call names each item the same way again and the overwrite
    rule then applies to each file on its own, as long as the same items are found (an
    item not found gets no name, so a later item can take the one it would have had).
    Names compare as ``_name_key`` does, so
    two that are one file on Windows or macOS count as the same name.
    """
    item_id = item.get("id")
    name = _safe_name(str(item.get("filename") or ""), f"item-{item_id}")
    if size != "original":
        name = f"{PurePosixPath(name).stem or f'item-{item_id}'}_{size}.jpg"
    if _name_key(name) in used:
        path = PurePosixPath(name)
        base, count = f"{path.stem}_{item_id}", 1
        name = f"{base}{path.suffix}"
        while _name_key(name) in used:
            count += 1
            name = f"{base}_{count}{path.suffix}"
    used.add(_name_key(name))
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
        invalid(op, f"at most {_MAX_DOWNLOAD:,} ids per call.", param="ids", value=len(ids))
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
    done = kept = 0
    retry = False  # a failure that can succeed on a later call
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
            result, retryable = await _download_one(
                ph, item, size, local_dir / name, overwrite=overwrite
            )
        except httpx.TransportError as e:
            # DSM didn't answer (a dropped connection or a timeout): the rest would most
            # likely wait out the same timeout, so stop and say which weren't tried.
            lines.append(
                f"  {item_id}  {name}: not downloaded: couldn't reach DSM ({type(e).__name__})"
            )
            rest = unique[n + 1 :]
            if rest:
                lines.append(f"  not tried: {', '.join(str(i) for i in rest)}")
            retry = True
            break
        retry = retry or retryable
        if result.startswith(_ALREADY_THERE):
            kept += 1
            lines.append(f"  {item_id}  {name}: {result}")
        elif result.startswith("not downloaded"):
            lines.append(f"  {item_id}  {name}: {result}")
        else:
            done += 1
            lines.append(f"  {item_id}  {name}  {result}")
    if len(unique) > 1 and retry:
        # A name depends on every item in the call, so retrying only the ones that failed
        # can give one of them another item's file name and, with overwrite=true, replace
        # it (#467). Only a failure that can succeed later gets the advice: one that can't
        # (no preview, a folder or another file with that name) would get it on every
        # retry (pre-QA on QA round 2).
        lines.append(
            "To retry, repeat this call with the same ids in the same order (overwrite=false "
            "keeps the files already written): names depend on every item in the call, and "
            "shift if an item that wasn't found is found then."
        )
    what = "original" if size == "original" else f"{size} preview"
    there = f", {kept:,} already there" if kept else ""
    head = (
        f"Downloaded {done:,} of {len(unique):,} item(s) to {local_dir} "
        f"({what}, {space_label(sp)}{there})"
    )
    return "\n".join([head, *lines])


_ALREADY_THERE = "already there"


def _existing(dest: Path, item: dict[str, Any], size: str) -> str:
    """The result line for a file already at ``dest``, kept (``overwrite=false``).

    A retry of the same call finds the files the first run wrote. An original whose size
    matches the item's is most likely that, and isn't pushed toward ``overwrite=true``;
    another size is another file. A preview's size isn't known beforehand (QA round 2 and
    its pre-QA on #462).
    """
    if size == "original":
        expected = item.get("filesize")
        try:
            same = (
                expected is not None
                and dest.is_file()
                and not dest.is_symlink()
                and dest.stat().st_size == int(expected)
            )
        except (OSError, ValueError, TypeError):
            same = False
        if same:
            # A size match isn't proof: another photo can have the same name and size (a
            # camera's RAW files; QA round 3 on #462).
            return (
                f"{_ALREADY_THERE}: a file with this name and this item's size, most likely "
                "this item from an earlier run; same name and size only, not verified (kept)"
            )
        return (
            "not downloaded: another file has this name (if it's another item's, from a "
            "call with more ids, repeat that whole call instead; otherwise overwrite=true "
            "replaces it, or choose another folder)"
        )
    return (
        "not downloaded: a file with this name is already there (if an earlier run of "
        "this call wrote it, nothing to do; otherwise overwrite=true replaces it, or choose "
        "another folder)"
    )


async def _download_one(
    ph: Photos, item: dict[str, Any], size: str, dest: Path, *, overwrite: bool
) -> tuple[str, bool]:
    """Download one item; a result line (``not downloaded: …`` on failure), and whether
    the failure can succeed on a later call."""
    if dest.is_dir() and not dest.is_symlink():
        return "not downloaded: a folder has that name", False
    if os.path.lexists(dest) and not overwrite:
        return _existing(dest, item, size), False
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
                ), True
            return f"not downloaded: no {size} preview for this item ({state})", False
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
        # Another call or program wrote it: never this call's own earlier run (QA round 3).
        return (
            "not downloaded: a file with this name appeared while downloading, written by "
            "something else (kept)",
            False,
        )
    except SynologyError as e:
        code = f" (DSM error {e.code})" if e.code else ""
        return f"not downloaded: {e}{code}", e.retryable
    except OSError as e:
        if e.errno == errno.ENOSPC:
            return "not downloaded: no space left on the local disk", True
        # A permission or read-only folder doesn't change by repeating the call.
        return f"not downloaded: couldn't write the file ({e.strerror or e})", False
    return format_size(written), False


# -- upload_photos ------------------------------------------------------------------

# Upload errors the web client doesn't retry (604, 605, 620, 622), plus a missing folder
# (621), a folder Photos can't find on disk (117: the folder was just looked up, so waiting
# doesn't help, #436) and the setup and permission codes. Anything else is retried once, as
# the web client does (UPLOAD retry count 1): vdsm answered 623 to some uploads right after
# boot, and the retry went through.
_UPLOAD_FINAL = frozenset({117, 604, 605, 620, 621, 622}) | SETUP_CODES
UPLOAD_RETRY_DELAY = 2.0


async def _upload_one(
    client: DsmClient,
    api: str,
    fields: dict[str, Any],
    path: Path,
    prepare: Callable[[], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    """Upload one file; on a 117, ``prepare`` (if given) may fix the cause, then it's resent.

    The resend is an attempt like the first: a transient code it gets is retried once. The
    delayed retry after a transient code is one too, so a 117 on it still calls ``prepare``.
    """

    async def send() -> dict[str, Any]:
        return await client.upload_multipart(
            api,
            "upload_to_folder",
            1,
            {k: json.dumps(v) for k, v in fields.items()},
            file_path=path,
            filename=path.name,
        )

    async def attempt() -> dict[str, Any]:
        try:
            return await send()
        except SynologyError as e:
            if e.code == 117 and prepare is not None and await prepare():
                logger.info("Photos upload of %s got 117; resending after Setting.User", path.name)
                return await send()
            raise

    try:
        return await attempt()
    except SynologyError as e:
        if e.code is None or e.code in _UPLOAD_FINAL:
            raise
        logger.info("Photos upload of %s got %s; retrying once", path.name, e.code)
        await asyncio.sleep(UPLOAD_RETRY_DELAY)
        return await attempt()


_MAX_UPLOAD = 50
# The most upload_photos keeps starting files for, whatever task_timeout says (#416 QA):
# Claude Desktop stops waiting for a tool call after about 60 s, and the file in progress
# at the limit still finishes.
UPLOAD_BUDGET_CAP = 45.0
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
    budget: float = 30.0,
) -> str:
    """Upload local files into a Photos folder (spec: upload_photos), one request each.

    No new file is started once ``budget`` seconds (``task_timeout``, at most
    ``UPLOAD_BUDGET_CAP``) have passed, so the reply arrives before a client's tool-call
    timeout (about 60 s on Claude Desktop; #416). The files not tried are listed by
    path, to pass to a second call.
    """
    budget = min(budget, UPLOAD_BUDGET_CAP)
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
            f"at most {_MAX_UPLOAD:,} files per call.",
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
    # An account that has never used Photos has no homes/<user>/Photos folder yet, and
    # every upload to the personal root answers 117 until it does (#436). The web app's
    # Setting.User get creates it (for a user with no items), so after a 117 that call is
    # made once and the file is resent. Only the personal root: a subfolder means the folder
    # exists. Shared Space is left out because its root is a shared folder of its own
    # (inferred: Shared Space wasn't probed).
    personal_root = sp == "personal" and target.get("name") == "/"
    prepared = False
    # No answer from Setting.User, or a proxy's 5xx or login page in its place (#282): the
    # call may still have created the folder, so a later file can land where this one
    # couldn't. The row says so rather than "no folder". A request that never left (refused,
    # connect timeout) or a 4xx created nothing.
    unanswered = False

    async def prepare_root() -> bool:
        nonlocal prepared, unanswered
        if not personal_root or prepared:
            return False
        prepared = True
        try:
            await ph.call("Setting.User", "get", personal=True)
        except (SynologyError, httpx.TransportError) as e:
            logger.debug("Photos Setting.User get failed: %s", type(e).__name__)
            unanswered = (
                isinstance(e, httpx.TransportError | DsmReplyError)
                and not isinstance(e, httpx.ConnectError | httpx.ConnectTimeout)
            ) or (isinstance(e, DsmHttpError) and e.status >= 500)
            return False
        return True

    rows: list[tuple[str, str, str]] = []
    uploaded: list[int] = []  # stored or already there: what an album gets
    renamed: list[int] = []
    skipped = 0
    left: list[str] = []  # local_paths not tried because the time ran out
    start = time.monotonic()
    for n, path in enumerate(files):
        if n and time.monotonic() - start >= budget:
            left = local_paths[n:]
            rows += [(rest.name, "not tried", "") for rest in files[n:]]
            break
        try:
            fields = {
                "duplicate": _DUPLICATE[on_duplicate],
                "name": path.name,
                # Photos shows this as the taken time until it indexes the file's EXIF.
                "mtime": int(path.stat().st_mtime),
                "target_folder_id": int(target["id"]),
            }
            data = await _upload_one(client, ph.api("Upload.Item"), fields, path, prepare_root)
            action = str(data.get("action", "new"))
            item_id = int(data["id"])
        except (DsmHttpError, DsmReplyError, httpx.TransportError) as e:
            if isinstance(e, DsmHttpError) and 400 <= e.status < 500:
                # A 4xx is the proxy (or DSM) refusing this request: not stored. A smaller
                # file may still go through after a 413 (nginx's body-size limit), but
                # after a 429 (rate limited) the rest would only meet the same limit.
                hint = " The file may be over the proxy's upload size limit." * (e.status == 413)
                rows.append((path.name, "failed", f"{e}{hint}"))
                if e.status == 429:
                    rows += [(rest.name, "not tried", "") for rest in files[n + 1 :]]
                    break
                continue
            # No answer, or a proxy's 502/504 or login page (#282): the same stall, which
            # the rest would most likely meet too, so stop and say which weren't tried (as
            # download_photos). Unless the request never left, the upload may have landed,
            # so it isn't reported as failed outright.
            if isinstance(e, httpx.ConnectError | httpx.ConnectTimeout):
                rows.append((path.name, "failed", f"DSM couldn't be reached ({type(e).__name__})"))
            elif isinstance(e, httpx.TransportError):
                why = f"DSM didn't answer ({type(e).__name__})"
                rows.append((path.name, "unknown", f"{why}; it may or may not have been stored"))
            else:
                rows.append((path.name, "unknown", f"{e} It may or may not have been stored."))
            rows += [(rest.name, "not tried", "") for rest in files[n + 1 :]]
            break
        except SynologyError as e:
            code = f" ({e.code})" if e.code else ""
            note = f"{e}{code}"
            if e.code == 117 and personal_root and unanswered:
                # Only the file whose 117 came before that call: a later file's 117, sent
                # after it, says the folder still isn't there.
                unanswered = False
                note = (
                    "this account's personal space had no folder on the NAS "
                    "(homes/<user>/Photos; 117), and the call that creates it got no answer, "
                    "so the folder may exist now. Upload this file again; if it fails the "
                    "same way, create a folder with photo_folder and upload into it, or open "
                    "Synology Photos once as this user."
                )
            elif e.code == 117 and personal_root:
                note = (
                    "this account's personal space has no folder on the NAS "
                    "(homes/<user>/Photos; 117). Create a folder with photo_folder and "
                    "upload into it, or open Synology Photos once as this user."
                )
            rows.append((path.name, "failed", note))
            continue
        except (OSError, KeyError, TypeError, ValueError) as e:
            if isinstance(e, OSError):
                why = f"couldn't read the file ({e.strerror or e})"
            else:  # DSM's reply had no usable id
                why = f"DSM's reply couldn't be read ({type(e).__name__})"
            rows.append((path.name, "failed", why))
            continue
        uploaded.append(item_id)
        note = f"id {item_id}"
        if action == "ignore":
            note += " (already there)"
            skipped += 1
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
    # A skipped duplicate is already there, not uploaded: count it apart.
    head = f"Uploaded {len(uploaded) - skipped:,} of {len(files):,} file(s)"
    if skipped:
        head += f" ({skipped:,} skipped, already there)"
    lines = [f"{head} to {target.get('name')} ({space_label(sp)})"]
    lines += [f"  {name:<{width}}  {state:<8} {note}" for name, state, note in rows]
    if target_album is not None and uploaded:
        try:
            await ph.call(
                "Browse.NormalAlbum",
                "add_item",
                {"id": int(target_album["id"]), "item": uploaded},
                personal=True,
            )
            lines.append(f'Added {len(uploaded):,} item(s) to album "{target_album.get("name")}".')
        except SynologyError as e:
            lines.append(f'Couldn\'t add them to album "{target_album.get("name")}": {e}')
        except httpx.TransportError as e:
            lines.append(
                f"Couldn't add them to album \"{target_album.get('name')}\": DSM didn't answer "
                f"({type(e).__name__}). Check the album with list_photos before adding again."
            )
    if len(uploaded) > skipped:
        lines.append(
            "Photos indexes new items over the next seconds to minutes; taken dates and "
            "previews appear then."
        )
    if left:
        why = (
            "resending the others makes renamed copies"
            if on_duplicate == "rename"
            else "the others would only be skipped as already there"
        )
        lines.append(
            f"Started no new file after {budget:g} s (the task timeout, at most "
            f"{UPLOAD_BUDGET_CAP:g} s) so the reply isn't cut off; {len(left):,} file(s) "
            "not tried. To continue, call upload_photos again with the same folder, space, "
            f"album and on_duplicate, and only these local_paths ({why}):"
        )
        lines += [f"  {value}" for value in left]
    return "\n".join(lines)
