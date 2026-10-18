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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from mcp_synology.core.errors import ErrorCode, SynologyError
from mcp_synology.core.formatting import error_response, format_timestamp

if TYPE_CHECKING:
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


def check_space(operation: str, space: str) -> Space:
    """``space`` as a ``Space``, or an invalid_parameter error."""
    if space not in SPACES:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"{operation} failed: unknown space {space!r}.",
            retryable=False,
            param="space",
            value=space,
            valid=list(SPACES),
        )
    return space  # type: ignore[return-value]


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

    # -- folders ------------------------------------------------------------

    async def folder(self, value: str, *, param: str = "folder") -> dict[str, Any]:
        """A folder by ID or by path inside the space (``/`` is the root)."""
        text = value.strip()
        try:
            if text.isdigit():
                params: dict[str, Any] = {"id": int(text)}
            elif text in ("", "/"):
                params = {"id": 0}
            else:
                params = {"name": "/" + text.strip("/")}
            data = await self.call("Browse.Folder", "get", params, version=2)
        except SynologyError as e:
            if e.code == 642:
                not_found(
                    self.operation,
                    f"no folder {value!r} in the {space_label(self.space)}.",
                    param=param,
                    value=value,
                    suggestion=(
                        "Pass a folder ID, or a path starting with / "
                        "(list_photo_collections type=folder lists them)."
                    ),
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
                self._folder_paths[folder_id] = str(data["folder"].get("name", ""))
            except SynologyError:
                self._folder_paths[folder_id] = f"folder {folder_id}"
        return self._folder_paths[folder_id]

    # -- names --------------------------------------------------------------

    async def _by_id_or_name(
        self,
        value: str,
        *,
        kind: str,
        param: str,
        get: tuple[str, int, dict[str, Any]],
        walk: tuple[str, int, dict[str, Any]],
        personal: bool = False,
        hint: str,
    ) -> dict[str, Any]:
        """Resolve an ID (digits; tried first) or a unique case-insensitive name."""
        text = value.strip()
        if text.isdigit():
            suffix, version, extra = get
            try:
                data = await self.call(
                    suffix, "get", {"id": [int(text)], **extra}, version=version, personal=personal
                )
            except SynologyError as e:
                # A setup or permission problem is the answer; anything else (an
                # unknown ID's error) falls through to the name lookup.
                if e.code in SETUP_CODES:
                    raise
                data = {}
            found = data.get("list", [])
            if found:
                return dict(found[0])
        suffix, version, extra = walk
        entries = await self.walk(suffix, params=extra, version=version, personal=personal)
        matches = [e for e in entries if str(e.get("name", "")).casefold() == text.casefold()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            not_found(
                self.operation, f"no {kind} {value!r}.", param=param, value=value, suggestion=hint
            )
        listed = ", ".join(f"{m['id']} ({m.get('name', '')})" for m in matches[:10])
        invalid(
            self.operation,
            f"{len(matches)} {kind}s are named {value!r}: {listed}.",
            param=param,
            value=value,
            suggestion=f"Pass the {kind}'s ID instead.",
        )

    async def album(self, value: str, *, param: str = "album") -> dict[str, Any]:
        """An album (personal space only) by ID or unique name."""
        return await self._by_id_or_name(
            value,
            kind="album",
            param=param,
            get=("Browse.Album", 1, {"additional": ["sharing_info"]}),
            walk=("Browse.Album", 2, {"additional": ["sharing_info"]}),
            personal=True,
            hint="list_photo_collections type=album lists your albums.",
        )

    async def person(self, value: str, *, param: str = "person") -> dict[str, Any]:
        """A person (face-recognition group) by ID or unique name."""
        return await self._by_id_or_name(
            value,
            kind="person",
            param=param,
            get=("Browse.Person", 1, {"additional": ["thumbnail"]}),
            walk=("Browse.Person", 1, {"show_more": True, "show_hidden": True}),
            hint="list_photo_collections type=person include_hidden=true lists people.",
        )

    async def tag_ids(self, names: list[str]) -> dict[str, int | None]:
        """Each tag name's current ID (``None`` if no item has it), looked up now.

        Never cached: tag IDs can change when Photos 1.9.1 re-indexes a file.
        """
        tags = await self.walk("Browse.GeneralTag")
        exact = {str(t.get("name", "")): int(t["id"]) for t in tags}
        folded: dict[str, list[int]] = {}
        for t in tags:
            folded.setdefault(str(t.get("name", "")).casefold(), []).append(int(t["id"]))
        out: dict[str, int | None] = {}
        for name in names:
            if name in exact:
                out[name] = exact[name]
            else:
                ids = folded.get(name.casefold(), [])
                out[name] = ids[0] if len(ids) == 1 else None
        return out


# -- formatting ---------------------------------------------------------------


def taken(item: dict[str, Any]) -> str:
    """An item's taken time (Unix seconds) as UTC."""
    value = item.get("time")
    return format_timestamp(float(value)) if value else "—"


def is_indexing(item: dict[str, Any]) -> bool:
    """Metadata not indexed yet: resolution is 0×0 until it is (spec, Indexing lag)."""
    res = (item.get("additional") or {}).get("resolution")
    return isinstance(res, dict) and not res.get("width") and not res.get("height")


def parse_date(operation: str, param: str, value: str, *, end: bool = False) -> int:
    """``YYYY-MM-DD`` (UTC) as Unix seconds; ``end`` gives the day's last second."""
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


def page_footer(total: int | None, offset: int, shown: int, noun: str = "item(s)") -> str:
    """``N item(s) total; showing a–b.`` plus the next offset when there's more."""
    if total is None:
        line = f"Showing {offset + 1}–{offset + shown}." if shown else ""
        return line + (f" Pass offset={offset + shown} for the next page." if shown else "")
    if shown == 0:
        return f"{total} {noun} total."
    line = f"{total} {noun} total; showing {offset + 1}–{offset + shown}."
    if offset + shown < total:
        line += f" Pass offset={offset + shown} for the next page."
    return line
