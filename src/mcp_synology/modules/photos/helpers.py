"""Shared Photos plumbing: spaces, calls, versions, lookups and formatting.

See ``docs/specs/photos-module-spec.md``. In short:

- Photos keeps two libraries; the API prefix picks one (``SYNO.Foto.*`` personal,
  ``SYNO.FotoTeam.*`` Shared Space). IDs belong to a space.
- Every JSON call is a form POST with each value JSON-encoded (Transport).
- Each call uses a pinned version (Versions); favorites and concepts need
  ``Browse.Item`` v7 (Photos 1.9.1) and are refused locally where it's missing.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NoReturn

import httpx

from mcp_synology.core.display import edge_quote, json_quoted, plain_quoted, shown
from mcp_synology.core.errors import (
    DsmHttpError,
    DsmReplyError,
    DsmTimeoutError,
    DsmUnavailableError,
    ErrorCode,
    SynologyError,
)
from mcp_synology.core.formatting import error_response
from mcp_synology.modules.filestation.paths import check_parts

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mcp_synology.core.client import DsmClient

logger = logging.getLogger(__name__)

Space = Literal["personal", "shared"]
SPACES: tuple[Space, ...] = ("personal", "shared")

# Browse.Item version that has favorites and concepts (Photos 1.9.1, DSM 7.2).
V7 = 7
_NEEDS_191 = "Synology Photos 1.9.1 or later (DSM 7.2)"
# How many ids DSM gets per request, as the web client's BATCH_UPDATE_SIZE.
BATCH_SIZE = 100
# Page size for walking a whole collection (names, tags, people, albums).
WALK_PAGE = 500
# DSM codes that mean the account can't do this at all (spec, Prerequisites), never
# "that ID doesn't exist".
SETUP_CODES = frozenset({105, 160, 602, 801, 803})


def prefix(space: Space) -> str:
    """The API prefix for a space."""
    return "SYNO.FotoTeam" if space == "shared" else "SYNO.Foto"


def space_label(space: Space) -> str:
    return "Shared Space" if space == "shared" else "personal space"


# What the lines that name an album, person, tag, place or request put around the name: a
# name holding one is quoted, so it can't read as the line's structure (#700).
_NAME_MARKS = (", ", "; ", " (", ": ", " · ", " → ")


def quoted_name(
    name: object, unnamed: str = "(unnamed)", *also: str, beside_id: bool = False
) -> str:
    """An album's, person's, tag's, place's or request's name in a line of a reply: as is,
    or JSON-quoted (`json_quoted`, #695) only when it's needed (maintainer, 2026-11-12, on
    #709), as `shown_folder` does for a folder. Needed when the name:

    - holds what its line puts around a name (`_NAME_MARKS`, or ``also``), checked in NFC
      too, where U+037E and U+0387 become ``;`` and ``·``;
    - holds a character JSON escapes (a quote, a backslash, one that draws nothing);
    - has whitespace at an end, starts or ends with any quote mark `shown` counts
      (`edge_quote`: Unicode's Quotation_Mark characters, #711, and a backtick), starts with ``(`` (it would
      read as the server's placeholder) or ends with ``:``;
    - with ``beside_id`` (the line writes it right after an ID: ``9 Trip``, ``person 12
      Jo``), starts with a digit of any script, or ``9 12`` would read as two IDs (the #614
      confusion; pre-QA r9 and QA r7 on #709).

    ``unnamed`` (the server's placeholder, unquoted) when it has none. A name that isn't
    text (``0``) is shown as text. A name made of words is shown as it is: wording isn't a
    boundary (CLAUDE.md)."""
    text = "" if name is None else str(name)
    if not text:
        return unnamed
    quoted = json_quoted(text)
    # NFC too: U+037E and U+0387 normalize to ";" and "·", and look the same (QA r7).
    nfc = unicodedata.normalize("NFC", text)
    needed = (
        quoted != f'"{text}"'
        or text != text.strip()
        or edge_quote(text)  # any quote mark `shown` counts, typographic too (QA r7)
        or text.startswith("(")
        or text.endswith(":")
        or any(m in text or m in nfc for m in (*_NAME_MARKS, *also))
        # Any digit, not only ASCII: `9 ５ Trip` reads as IDs too (QA r7).
        or (beside_id and text[:1].isdigit())
    )
    return quoted if needed else text


def shown_folder(path: object, *also: str) -> str:
    """A Photos folder path in a reply, as `shown` writes one, and plain-quoted when it holds
    ``" ("`` or ``": "``, which the lines that name a folder put after it (``/Trips (Shared
    Space)``, ``… to /Trips: 2 of 2 processed``), so a folder named ``/Trips (Shared Space)``
    can't read as the space (#700); or ends in ``":"`` or holds ``also``, another marker its
    line puts after it. A missing path (``None``, a DSM reply with no name) is the
    placeholder ``(unnamed)``, unquoted, as `quoted_name` writes a missing name (QA r1)."""
    if path is None:
        return "(unnamed)"
    text = str(path)
    out = shown(text)
    # A name ending in ":" makes ": " with the " (" after it (pre-QA r2).
    marked = text.endswith(":") or any(m in text for m in (" (", ": ", *also))
    return plain_quoted(text) if out == text and marked else out


# Replies may show a name in "…" (`quoted_name`), so a model may pass one back with them. Advice
# only, as File Station's for a quoted path (#692): a name that really has quotes still works.
QUOTES_HINT = "If the name came from a reply in quotes, pass it without them."
# A folder `shown_folder` quoted, passed back with its quotes (File Station's wording, #692).
PATH_QUOTES_HINT = "If the path came from a reply in quotes, pass it without them."


def in_reply_quotes(name: object, marks: str = '"') -> bool:
    """Whether ``name`` starts and ends with one of ``marks`` around something: ``"`` as
    `quoted_name` writes a name (QA r1 on #709; an empty name is ``(unnamed)``, never
    ``""``, QA r2), ``'`` or ``"`` as `shown_folder` writes a folder (the root is ``/``,
    never ``''``). The one rule for both, so they can't drift apart (pre-QA r7)."""
    text = "" if name is None else str(name)
    return len(text) >= 3 and text[0] == text[-1] and text[0] in marks


def with_quotes_hint(suggestion: str | None, name: object) -> str | None:
    """``suggestion``, with `QUOTES_HINT` added when ``name`` is in a reply's quotes."""
    if not in_reply_quotes(name):
        return suggestion
    return f"{suggestion} {QUOTES_HINT}" if suggestion else QUOTES_HINT


def check_space(operation: str, space: str) -> Space:
    """``space`` as a ``Space``, or an invalid_parameter error."""
    for known in SPACES:
        if known == space:
            return known
    error_response(
        ErrorCode.INVALID_PARAMETER,
        f"{operation} failed: unknown space {space!r}.",
        retryable=False,
        param="space",
        value=space,
        valid=list(SPACES),
    )


def invalid(
    operation: str,
    message: str,
    *,
    param: str | None = None,
    value: Any = None,
    valid: list[str] | None = None,
    suggestion: str | None = None,
) -> NoReturn:
    """Raise an invalid_parameter error for ``operation``."""
    error_response(
        ErrorCode.INVALID_PARAMETER,
        f"{operation} failed: {message}",
        retryable=False,
        param=param,
        value=value,
        valid=valid,
        suggestion=suggestion,
    )


def not_found(
    operation: str, message: str, *, param: str, value: Any, suggestion: str | None = None
) -> NoReturn:
    error_response(
        ErrorCode.NOT_FOUND,
        f"{operation} failed: {message}",
        retryable=False,
        param=param,
        value=value,
        suggestion=suggestion,
    )


def not_supported(operation: str, what: str, *, param: str | None = None) -> NoReturn:
    """Refuse a feature this NAS's Photos lacks (never a raw 103)."""
    error_response(
        ErrorCode.NOT_SUPPORTED,
        f"{operation} failed: {what} needs {_NEEDS_191}; this NAS's Synology Photos "
        "doesn't have it.",
        retryable=False,
        param=param,
        suggestion="Update Synology Photos in Package Center, or leave this option out.",
    )


def encode(params: dict[str, Any]) -> dict[str, str]:
    """JSON-encode every value, strings included (#121: DSM parses values as JSON)."""
    return {k: json.dumps(v) for k, v in params.items() if v is not None}


# Every tool argument that names a Photos folder by path (each with an ID twin, `folder_id` and
# the like, which needs no path check: #617). Each reaches DSM only through `Photos.folder`,
# which checks a path with `check_folder_path` (#510); a new folder argument goes here, and
# tests/modules/photos/test_paths.py fails until it does.
FOLDER_PARAMS: dict[str, frozenset[str]] = {
    "list_photos": frozenset({"folder_path"}),
    "list_photo_collections": frozenset({"parent_path"}),
    "photo_folder": frozenset({"folder_path", "parent_path"}),
    "photo_request": frozenset({"folder_path"}),
    "upload_photos": frozenset({"folder_path"}),
    "copy_photos": frozenset({"folder_paths", "destination_path"}),
    "move_photos": frozenset({"folder_paths", "destination_path"}),
    "delete_photo_folders": frozenset({"folder_paths"}),
    # A smart album's condition: its `folder_paths` key.
    "photo_album": frozenset({"condition"}),
}

FOLDER_ADVICE = "Pass a path starting with /, such as /Trips/2024."


def check_folder_path(value: str, param: str = "folder", advice: str = FOLDER_ADVICE) -> str:
    """A Photos folder path as DSM's ``Browse.Folder`` takes it: ``/`` and the folders below.

    Taken exactly, by File Station's part rules (#510): `check_parts` drops only whitespace
    before a leading ``/`` (and a trailing ``/``), and refuses a NUL (DSM cuts the path
    there: ``/x/foo<NUL>y`` resolved to ``/x/foo``), a character a reply shows escaped (#604),
    an empty part (``//``) and a ``.`` or ``..`` part (DSM answers 642 for those). Photos
    reaches a folder whose name starts with whitespace exactly (`` foo`` beside ``foo``), so
    stripping looked up another folder (vdsm DSM 7.1, 7.2.2, 7.3.2 and 7.4.1, personal space
    and Shared Space). ``advice`` is the refusal's suggestion, for an argument that takes only
    a path (`photo_request`).
    """
    if not value.strip():
        # Not the root: a client that fills an unset argument with "" would otherwise land a
        # write there, which the required folders exist to prevent (QA round 1 on #615). The
        # root is written `/`.
        what = {
            "condition": "an entry in the condition's folder_paths",
            "folder_paths": "an entry in folder_paths",
        }.get(param, param)
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"{what} is empty: pass a folder path (the space's root is written `/`).",
            retryable=False,
            param=param,
            value=value,
            suggestion=advice,
        )
    return check_parts(value, param, advice)


@dataclass(frozen=True)
class Ref:
    """An album, a person or a folder a tool was given, by ID or by name (a folder's path), each
    from its own parameter (`album_id` / `album_name`, `folder_id` / `folder_path`; #614,
    #616, #617): which one is never inferred from the value. ``param`` is the tool argument
    it came from, for errors."""

    param: str
    id: int | None = None
    name: str | None = None

    @property
    def given(self) -> int | str:
        """The value as the caller gave it, for errors."""
        return self.id if self.id is not None else self.name or ""

    @property
    def id_param(self) -> str:
        """Where the caller gives the ID instead of this name: ``merge_names`` → ``merge_ids``,
        ``target_name`` → ``target_id``; a smart album condition's ``person_ids``."""
        # Only albums and people get here (`_by_ref`): a folder path is never ambiguous.
        if self.param.endswith("_names"):
            return self.param.removesuffix("_names") + "_ids"
        if self.param.endswith("_name"):
            return self.param.removesuffix("_name") + "_id"
        return "person_ids"


def one_ref(
    operation: str,
    kind: str,
    id_value: int | None,
    name_value: str | None,
    *,
    prefix: str | None = None,
    other: str = "name",
    required: bool = True,
) -> Ref | None:
    """The one album, person or folder a tool's ``<prefix>_id`` / ``<prefix>_<other>`` pair
    names (``other`` is ``name``, or ``path`` for a folder): both is refused, and neither when
    ``required``. ``prefix`` defaults to ``kind``."""
    prefix = prefix or kind
    id_param, name_param = f"{prefix}_id", f"{prefix}_{other}"
    if id_value is not None and name_value is not None:
        invalid(
            operation,
            f"pass {id_param} or {name_param}, not both.",
            param=id_param,
            value=id_value,
        )
    if id_value is not None:
        return Ref(id_param, id=id_value)
    if name_value is not None:
        return Ref(name_param, name=name_value)
    if required:
        invalid(operation, f"pass {id_param} or {name_param}.", param=id_param, value=None)
    return None


def refs(
    operation: str,
    ids: Sequence[int] | None,
    names: Sequence[str] | None,
    *,
    prefix: str,
    other: str = "names",
    required: bool = True,
) -> list[Ref]:
    """The albums, people or folders a ``<prefix>_ids`` / ``<prefix>_<other>`` pair names, IDs
    first (``other`` is ``names``, or ``paths`` for folders). Both lists may be given; at least
    one entry when ``required``."""
    found = [Ref(f"{prefix}_ids", id=i) for i in ids or ()]
    found += [Ref(f"{prefix}_{other}", name=n) for n in names or ()]
    if required and not found:
        invalid(
            operation,
            f"pass {prefix}_ids or {prefix}_{other}.",
            param=f"{prefix}_ids",
            value=None,
        )
    return found


@dataclass
class Photos:
    """One tool call's view of Photos: the client, the space and per-call caches."""

    client: DsmClient
    space: Space
    operation: str
    _folder_paths: dict[int, str] = field(default_factory=dict)

    def api(self, suffix: str, *, personal: bool = False) -> str:
        """The full API name; ``personal`` for personal-only APIs (albums, sharing, tasks)."""
        return f"{'SYNO.Foto' if personal else prefix(self.space)}.{suffix}"

    def has(self, suffix: str, version: int = 1, *, personal: bool = False) -> bool:
        """Whether the NAS registers ``suffix`` at ``version`` or later."""
        info = self.client.api_cache.get(self.api(suffix, personal=personal))
        return info is not None and info.max_version >= version

    def require_v7(self, what: str, *, param: str | None = None) -> None:
        """Refuse ``what`` unless ``Browse.Item`` v7 exists (Photos 1.9.1)."""
        if not self.has("Browse.Item", V7):
            not_supported(self.operation, what, param=param)

    async def call(
        self,
        suffix: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        version: int = 1,
        personal: bool = False,
    ) -> dict[str, Any]:
        """Send one JSON call as a form POST with JSON-encoded values."""
        api = self.api(suffix, personal=personal)
        if api not in self.client.api_cache:
            from mcp_synology.core.errors import ApiNotFoundError

            what = "Shared Space" if api.startswith("SYNO.FotoTeam") else "Synology Photos"
            raise ApiNotFoundError(
                f"{api} isn't available on this NAS.",
                code=102,
                suggestion=f"Check that {what} is installed and running (Package Center).",
            )
        return await self.client.request(api, method, version, encode(params or {}), post_form=True)

    async def walk(
        self,
        suffix: str,
        method: str = "list",
        params: dict[str, Any] | None = None,
        *,
        version: int = 1,
        personal: bool = False,
    ) -> list[dict[str, Any]]:
        """Every entry of a paged list (``offset``/``limit``)."""
        out: list[dict[str, Any]] = []
        while True:
            data = await self.call(
                suffix,
                method,
                {**(params or {}), "offset": len(out), "limit": WALK_PAGE},
                version=version,
                personal=personal,
            )
            page = data.get("list", [])
            out.extend(page)
            if len(page) < WALK_PAGE:
                return out

    async def items(self, ids: list[int]) -> tuple[dict[int, dict[str, Any]], list[int]]:
        """The items that exist (by ID) and the IDs that don't, checked before a write.

        DSM answers success for unknown IDs on several writes, and for items in a Shared
        Space folder the user has no permission for, while doing nothing; ``get`` leaves
        both out (spec: Writes that report success for nothing).
        """
        found: dict[int, dict[str, Any]] = {}
        unique = list(dict.fromkeys(ids))
        for start in range(0, len(unique), BATCH_SIZE):
            batch = unique[start : start + BATCH_SIZE]
            data = await self.call("Browse.Item", "get", {"id": batch, "additional": []}, version=2)
            for item in data.get("list", []):
                found[int(item["id"])] = item
        return found, [i for i in unique if i not in found]

    def missing_note(self, missing: list[int] | list[str]) -> str:
        """The line naming IDs that weren't found (and why that may be, in Shared Space)."""
        if not missing:
            return ""
        why = "Not found"
        if self.space == "shared":
            why += ", or no permission for its Shared Space folder"
        return f"  {why}: {', '.join(str(m) for m in missing)}"

    # -- folders ------------------------------------------------------------

    async def folder(self, ref: Ref | None, *, missing_advice: str | None = None) -> dict[str, Any]:
        """A folder by ID or by path inside the space (``/`` is the root; ``None`` too), each
        from its own parameter (`folder_id` / `folder_path` and the like, #617): an ID is
        that ID only, and a path (digits too) that path only.

        Every folder argument comes through here, so the path rules live in one place
        (#510): a path is taken exactly, by File Station's part rules (`check_folder_path`).
        ``missing_advice`` replaces the not-found suggestion, for an argument that takes only
        a path (`photo_request`).
        """
        if ref is None:
            ref = Ref("folder_path", name="/")
        # The path DSM is asked for, set for a path, not for an ID.
        path: str | None = None
        if ref.id is not None:
            if ref.id < 0:
                # No folder has a negative ID; nothing to ask DSM (#609).
                not_found(
                    self.operation,
                    f"no folder {ref.id} in the {space_label(self.space)}.",
                    param=ref.param,
                    value=ref.id,
                    suggestion="list_photo_collections type=folder lists them.",
                )
            params: dict[str, Any] = {"id": ref.id}
        else:
            path = check_folder_path(ref.name or "", ref.param)
            params = {"id": 0} if path == "/" else {"name": path}
        try:
            data = await self.call("Browse.Folder", "get", params, version=2)
        except SynologyError as e:
            if e.code == 642:
                # The path DSM was asked for, not the caller's text: beside "starting with /"
                # advice, `'Up'` read as if the "/" were missing (QA r3 on #709).
                what = ref.id if ref.id is not None else repr(path)
                advice = missing_advice or "list_photo_collections type=folder lists them."
                given = (ref.name or "").strip()
                if ref.id is None and in_reply_quotes(given, "'\""):
                    advice = f"{advice} {PATH_QUOTES_HINT}"
                not_found(
                    self.operation,
                    f"no folder {what} in the {space_label(self.space)}.",
                    param=ref.param,
                    value=ref.given,
                    suggestion=advice,
                )
            raise
        folder: dict[str, Any] = data["folder"]
        self._folder_paths[int(folder["id"])] = str(folder.get("name", ""))
        return folder

    async def folder_path(self, folder_id: int) -> str:
        """A folder's path, looked up once per call."""
        if folder_id not in self._folder_paths:
            try:
                data = await self.call("Browse.Folder", "get", {"id": folder_id}, version=2)
            except SynologyError as e:
                # Only a label: the row still shows, as "folder N". 642 (no such
                # folder) is kept for the call; anything else may pass, so it's retried.
                logger.debug("Folder %d path lookup failed: %s (code %s)", folder_id, e, e.code)
                if e.code != 642:
                    return f"folder {folder_id}"
                self._folder_paths[folder_id] = f"folder {folder_id}"
            else:
                self._folder_paths[folder_id] = str(data["folder"].get("name", ""))
        return self._folder_paths[folder_id]

    # -- names --------------------------------------------------------------

    async def _by_ref(
        self,
        ref: Ref,
        *,
        kind: str,
        get: tuple[str, int, dict[str, Any]],
        walk: tuple[str, int, dict[str, Any]],
        personal: bool = False,
        hint: str,
    ) -> dict[str, Any]:
        """Resolve an album or person given by ID or by name, never a guess between them
        (#614, #616): an ID is looked up as that ID only, and a name (digits too) as a unique
        case-insensitive name only."""
        if ref.id is not None:
            suffix, version, extra = get
            try:
                data = await self.call(
                    suffix, "get", {"id": [ref.id], **extra}, version=version, personal=personal
                )
            except SynologyError as e:
                # A setup or permission problem is the answer, and so is no DSM answer at
                # all (a proxy's 502, a reply that isn't DSM's: code None), which isn't an
                # unknown ID (pre-QA r3 on #611). Any other DSM code is an unknown ID.
                if e.code in SETUP_CODES or e.code is None:
                    raise
                data = {}
            found = data.get("list", [])
            if found:
                return dict(found[0])
            not_found(
                self.operation,
                f"no {kind} {ref.id}.",
                param=ref.param,
                value=ref.id,
                suggestion=hint,
            )
        name = (ref.name or "").strip()  # as before the split (#616)
        suffix, version, extra = walk
        entries = await self.walk(suffix, params=extra, version=version, personal=personal)
        matches = [e for e in entries if str(e.get("name", "")).casefold() == name.casefold()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            not_found(
                self.operation,
                f"no {kind} named {name!r}.",
                param=ref.param,
                value=name,
                suggestion=with_quotes_hint(hint, name),
            )
        listed = ", ".join(
            f"{m['id']} {quoted_name(m.get('name'), beside_id=True)}" for m in matches[:10]
        )
        invalid(
            self.operation,
            f"{len(matches):,} {kind}s are named {name!r}: {listed}.",
            param=ref.param,
            value=name,
            suggestion=(
                "Ask the user which one they mean, then pass its ID in the condition's person_ids."
                if ref.param == "condition"
                else f"Ask the user which one they mean, then pass its {ref.id_param}."
            ),
        )

    async def album(self, ref: Ref) -> dict[str, Any]:
        """An album (personal space only) by ID or by unique name."""
        return await self._by_ref(
            ref,
            kind="album",
            get=("Browse.Album", 1, {"additional": ["sharing_info"]}),
            walk=("Browse.Album", 2, {"additional": ["sharing_info"]}),
            personal=True,
            hint="list_photo_collections type=album lists your albums.",
        )

    async def person(self, ref: Ref) -> dict[str, Any]:
        """A person (face-recognition group) by ID or by unique name."""
        return await self._by_ref(
            ref,
            kind="person",
            get=("Browse.Person", 1, {"additional": ["thumbnail"]}),
            walk=("Browse.Person", 1, {"show_more": True, "show_hidden": True}),
            hint="list_photo_collections type=person include_hidden=true lists people.",
        )

    async def place_node(self, place_id: int, *, param: str) -> int:
        """The address-tree node a smart album condition needs for a place.

        ``list_photo_collections`` gives ``Geocoding.list`` place IDs; conditions (and
        ``list_with_filter``) take the node IDs of ``Search.Filter list``'s geocoding tree
        instead (Chicago was place 5 and node 4; a place ID there answers 950, verified).
        The place's ``country_id`` is the tree's root, and its ``first_level`` and
        ``second_level`` names lead to the node.
        """
        places = {int(p["id"]): p for p in await self.walk("Browse.Geocoding")}
        place = places.get(place_id)
        if place is None:
            not_found(
                self.operation,
                f"no place {place_id}.",
                param=param,
                value=place_id,
                suggestion="list_photo_collections type=place lists places.",
            )
        data = await self.call(
            "Search.Filter", "list", {"setting": {"geocoding": True}, "additional": []}, version=2
        )
        tree = data.get("geocoding", [])
        roots = [n for n in tree if n.get("id") == place.get("country_id")]

        def find(nodes: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
            for node in nodes:
                if str(node.get("name", "")) == name:
                    return node
                hit = find(node.get("children") or [], name)
                if hit is not None:
                    return hit
            return None

        node = find(roots, str(place.get("first_level", "")))
        if node is not None and place.get("second_level"):
            node = find(node.get("children") or [], str(place["second_level"]))
        if node is None:
            invalid(
                self.operation,
                f"place {place_id} {quoted_name(place.get('name'), beside_id=True)} can't be "
                "used in a smart album condition here.",
                param=param,
                value=place_id,
            )
        return int(node["id"])

    async def tag_ids(self, names: list[str], *, param: str = "tag") -> dict[str, int | None]:
        """Each tag name's current ID (``None`` if no item has it), looked up now.

        An exact name wins; otherwise a case-insensitive match must be unique, and
        several (``Beach`` and ``beach``, asked as ``BEACH``) are refused with the
        candidates, as for folders, albums and people. Never cached: tag IDs can
        change when Photos 1.9.1 re-indexes a file.
        """
        tags = await self.walk("Browse.GeneralTag")
        exact: dict[str, int] = {}
        by_name: dict[str, list[int]] = {}
        for t in tags:
            key = str(t.get("name", ""))
            by_name.setdefault(key, []).append(int(t["id"]))
            exact[key] = int(t["id"])
        # Two names that read the same once escaped (#604).
        doubled = {key: ids for key, ids in by_name.items() if len(ids) > 1}
        folded: dict[str, list[dict[str, Any]]] = {}
        for t in tags:
            folded.setdefault(str(t.get("name", "")).casefold(), []).append(t)
        out: dict[str, int | None] = {}
        for name in names:
            if name in doubled:
                invalid(
                    self.operation,
                    f"Tags {', '.join(map(str, doubled[name][:10]))} all read {name!r}: one holds "
                    "a character replies show escaped, another a backslash.",
                    param=param,
                    value=name,
                    suggestion="Rename one of them in Photos.",
                )
            if name in exact:
                out[name] = exact[name]
                continue
            matches = folded.get(name.casefold(), [])
            if len(matches) > 1:
                listed = ", ".join(
                    f"{m['id']} {quoted_name(m.get('name'), beside_id=True)}" for m in matches[:10]
                )
                invalid(
                    self.operation,
                    f"{len(matches):,} tags match {name!r} ignoring case: {listed}.",
                    param=param,
                    value=name,
                    suggestion="Pass the tag's exact name.",
                )
            out[name] = int(matches[0]["id"]) if matches else None
        return out


# -- formatting ---------------------------------------------------------------


def batches(ids: list[int], size: int = BATCH_SIZE) -> list[list[int]]:
    """``ids`` in chunks of ``size`` (the web client's BATCH_UPDATE_SIZE)."""
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def id_order(x: object) -> tuple[int, int, str]:
    """Sort key: IDs as numbers, anything else (an unexpected error_list entry) after."""
    return (0, x, "") if isinstance(x, int) else (1, 0, str(x))


def id_list(ids: Sequence[int]) -> str:
    """``1-3, 7, 9-10``: runs of consecutive IDs collapsed, so a long list stays short."""
    runs: list[list[int]] = []
    for i in ids:
        if runs and i == runs[-1][-1] + 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    return ", ".join(str(r[0]) if len(r) == 1 else f"{r[0]}-{r[-1]}" for r in runs)


async def in_batches(
    ids: list[int],
    send: Callable[[list[int]], Awaitable[dict[str, Any]]],
    *,
    note: str = "",
    before: str = "",
    step: str = "",
) -> list[dict[str, Any]]:
    """``send`` each batch of ``ids`` in turn; the replies, in order.

    When a batch fails, the error says which IDs were already changed, which may or may
    not have been, and which weren't, so a retry can cover only the rest (#322), and
    adds ``note`` (e.g. that a relative edit must not be repeated). IDs DSM rejected in
    an earlier batch (``error_list``) are named too. ``before`` says what earlier steps
    of the call already changed, and ``step`` names this one in the error ("Favorite
    not changed: …"). A failure on the first batch, with nothing changed and nothing in
    doubt, is raised as it is.
    """
    done: list[int] = []
    replies: list[dict[str, Any]] = []
    for batch in batches(ids):
        try:
            replies.append(await send(batch))
        except (SynologyError, httpx.TransportError) as e:
            # No answer, or a proxy's server error or page: the write may have reached
            # DSM. A connection that couldn't be opened sent nothing, and a 4xx is the
            # proxy (or DSM) refusing the request, as for Photos uploads.
            unsure = (
                isinstance(e, httpx.TransportError | DsmReplyError)
                and not isinstance(e, httpx.ConnectError | httpx.ConnectTimeout)
            ) or (isinstance(e, DsmHttpError) and not 400 <= e.status < 500)
            if not (done or before or unsure):
                raise
            parts = [before] if before else []
            rest = ids[len(done) :]
            changed: list[int] = []

            def phrase(text: str) -> str:  # "Not changed" or "Favorite not changed"
                return f"{step} {text[0].lower()}{text[1:]}" if step else text

            if done:
                # IDs an earlier batch's error_list named weren't changed: never count them.
                # Compared as text, so an entry DSM sends as "9" matches ID 9.
                rejected: list[Any] = []
                for r in replies:
                    add_rejected(rejected, r.get("error_list") or [])
                refused = {str(x) for x in rejected}
                changed = [i for i in done if str(i) not in refused]
                if changed:
                    parts.append(
                        f"{phrase('Already changed')} {len(changed):,} of {len(ids):,} item(s): "
                        f"{_ids(changed)}."
                    )
                if rejected:
                    parts.append(f"Rejected by DSM, so not changed: {rejected_text(rejected)}.")
            if unsure:
                parts.append(f"{phrase('May or may not have changed')}: {_ids(batch)}.")
                rest = rest[len(batch) :]
            if rest:
                parts.append(f"{phrase('Not changed')}: {_ids(rest)}.")
            if note and (changed or unsure):
                parts.append(note)
            detail = " ".join(parts)
            if unsure or isinstance(e, httpx.TransportError):
                if isinstance(e, httpx.ConnectError | httpx.ConnectTimeout):
                    what = f"couldn't reach DSM ({type(e).__name__})"
                elif isinstance(e, httpx.TransportError):
                    what = f"no answer from DSM ({type(e).__name__})"
                else:
                    what = str(e).rstrip().rstrip(".")
                kind = (
                    DsmTimeoutError
                    if isinstance(e, httpx.TimeoutException)
                    else DsmUnavailableError
                )
                raise kind(
                    f"{what}. {detail}", suggestion="Check the items before trying again."
                ) from e
            text = str(e).rstrip()
            stop = "" if text.endswith((".", "!", "?")) else "."
            e.args = (f"{text}{stop} {detail}",)
            raise
        done += batch
    return replies


def _ids(ids: list[int]) -> str:
    return id_list(sorted(ids))


def rejected_text(entries: list[Any]) -> str:
    """error_list entries: integer IDs sorted with runs collapsed, then anything else."""
    ints = sorted(x for x in entries if type(x) is int)  # add_rejected deduped them
    rest = [str(x) for x in sorted(entries, key=id_order) if type(x) is not int]
    return ", ".join(([id_list(ints)] if ints else []) + rest)


def add_rejected(rejected: list[Any], entries: list[Any]) -> None:
    """Add error_list ``entries`` to ``rejected``, once each by their text. A list, not a
    set: an entry needn't be hashable (only integer IDs have been seen)."""
    seen = {str(x) for x in rejected}
    for x in entries:
        if isinstance(x, str) and x.isascii() and x.isdigit():
            x = int(x)  # "9" is ID 9
        if str(x) not in seen:
            seen.add(str(x))
            rejected.append(x)


def check_ids(
    operation: str, ids: Sequence[object], *, param: str = "ids", limit: int = 1000
) -> None:
    """At least one ID (or folder), and not more than ``limit``."""
    if not ids:
        invalid(operation, f"pass at least one id in {param}.", param=param, value=ids)
    if len(ids) > limit:
        noun = "folders" if param.startswith("folder") else "ids"
        invalid(operation, f"at most {limit:,} {noun} per call.", param=param, value=len(ids))


def photo_time(seconds: float) -> str:
    """A Photos ``time`` as ``YYYY-MM-DD HH:MM:SS``, with no zone.

    Photos stores the wall-clock time a photo was taken (EXIF's, or the file's time in
    the NAS's zone) encoded as if it were UTC: a file with mtime 16:09:03 UTC on a NAS
    in US Central (CDT) reads back as 11:09:03 (seska, Synology Photos 1.3.4, rc4). So
    it isn't a UTC instant, and is shown as the local time it records.
    """
    return datetime.datetime.fromtimestamp(seconds, tz=datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")


def taken(item: dict[str, Any]) -> str:
    """An item's taken time as Photos records it (local wall-clock time, no zone)."""
    value = item.get("time")
    return photo_time(float(value)) if value else "—"


def taken_or_indexing(item: dict[str, Any], mark: str = "indexing") -> str:
    """The taken time, or ``— (<mark>)`` while the item is still being indexed.

    Until Photos indexes an item, its ``time`` is the file's modification time as a real
    instant, which reads as UTC here, and indexing replaces it with the taken time in local
    wall-clock time: on a NAS on US Central the two were 5 h apart, unlabeled (#415).
    """
    return f"— ({mark})" if is_indexing(item) else taken(item)


def is_indexing(item: dict[str, Any]) -> bool:
    """Metadata not indexed yet: resolution is 0×0 until it is (spec, Indexing lag)."""
    res = (item.get("additional") or {}).get("resolution")
    return isinstance(res, dict) and not res.get("width") and not res.get("height")


def parse_date(operation: str, param: str, value: str, *, end: bool = False) -> int:
    """``YYYY-MM-DD`` as Unix seconds for that day read as UTC; ``end`` gives the day's
    last second. That's Photos' own encoding of the day's wall-clock times (see
    ``photo_time``), so it suits the taken-time filters and smart-album date spans. A
    share link's or request's ``expires`` is a real instant: see ``parse_expires``."""
    try:
        day = datetime.date.fromisoformat(value)
    except ValueError:
        invalid(
            operation,
            f"{param} must be a date as YYYY-MM-DD, not {value!r}.",
            param=param,
            value=value,
        )
    start = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.UTC)
    seconds = int(start.timestamp())
    return seconds + 86399 if end else seconds


def parse_expires(operation: str, value: str) -> int:
    """A share link's or photo request's ``expires`` as Unix seconds (0 for ``never``).

    The expiration is a real UTC instant: DSM refuses a visitor after it whatever the
    NAS's zone (verified on vdsm, 1.3.4 and 1.9.1, in US Central and India). A date is
    the end of that day UTC; a date-time needs an offset or ``Z``, since without one it
    names no instant (the NAS's zone isn't readable by a non-admin, and the user may be
    in another zone anyway).
    """
    text = value.strip()
    if text == "never":
        return 0
    problem = "isn't a date or date-time"
    when: datetime.datetime | None = None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            day = datetime.date.fromisoformat(text)
            when = datetime.datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=datetime.UTC)
        else:
            when = datetime.datetime.fromisoformat(text)
            if when.tzinfo is None:
                problem, when = "has no offset", None
    except ValueError:
        when = None
    # 0 is DSM's "never", and what DSM does with an earlier instant isn't known.
    if when is not None and when.timestamp() <= 0:
        problem, when = "isn't after 1970-01-01", None
    if when is None:
        invalid(
            operation,
            f"expires {value!r} {problem}: pass YYYY-MM-DD (the end of that day, UTC), a "
            "date-time with an offset such as 2026-10-25T23:59:59-04:00, or never.",
            param="expires",
            value=value,
        )
    return int(when.timestamp())


def page_footer(total: int | None, offset: int, shown: int, noun: str = "item(s)") -> str:
    """``N item(s) total; showing a–b.`` plus the next offset when there's more."""
    if total is None:
        line = f"Showing {offset + 1:,}–{offset + shown:,}." if shown else ""
        return line + (f" Pass offset={offset + shown} for the next page." if shown else "")
    if shown == 0:
        return f"{total:,} {noun} total."
    line = f"{total:,} {noun} total; showing {offset + 1:,}–{offset + shown:,}."
    if offset + shown < total:
        line += f" Pass offset={offset + shown} for the next page."
    return line
