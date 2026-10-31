"""One entry point for every File Station path argument (#510).

Every tool runs its path arguments through `resolve_paths` before any DSM request, so the
same input behaves the same way in every tool. The rules run once, on the raw input:

1. **Parts** (every role): only whitespace before a leading ``/`` is padding; the rest is
   taken exactly (#488: DSM keeps whitespace in a name). A NUL is refused (DSM cuts the
   path there, so ``<dir>/a<NUL>x`` acts on ``a``; probe on #472), and so are an empty part
   (``//``: DSM answers 408) and a ``.`` or ``..`` part (#449; padded with whitespace too).
2. **Share**: the first part is a known shared folder, sent with DSM's spelling (#160,
   #353; `ensure_known_shares`).
3. **Role** (one table, `PATH_PARAMS`): what the argument may name.

`ResolvedPath` is what comes out; the operation functions take it, so ``mypy src/`` rejects
a tool that passes an unchecked string. Tests may call operations with plain strings.
"""

from __future__ import annotations

import enum
import unicodedata
from typing import TYPE_CHECKING, Literal, NamedTuple

from mcp_synology.core.errors import ErrorCode
from mcp_synology.core.formatting import error_response
from mcp_synology.modules.filestation import helpers
from mcp_synology.modules.filestation.helpers import (
    _is_dot,
    draws_nothing,
    invisible_problem,
    normalize_path,
    path_exists,
    shown,
    strip_padding,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mcp_synology.core.client import DsmClient

DEFAULT_SUGGESTION = "Write the path out in full from the shared folder, e.g. /video/Movies."


class ResolvedPath(str):
    """A path that went through `resolve_paths`, or one the server builds from a checked
    one (a share's ``#recycle``)."""

    __slots__ = ()


class PathRole(enum.Enum):
    """What a path argument may name. The structural rules live here, not in each tool."""

    READ = "read"  # list, info, size, search, download, copy source: a share root is fine
    WRITE_TARGET = "write_target"  # delete, rename, move source: no share root, bin, home
    DEST = "dest"  # copy/move/restore/upload destination: a share root is fine
    NEW_FOLDER = "new_folder"  # create_folder: its last part is a new name
    SHARE = "share"  # a shared folder's name (list_recycle_bin, restore)


# Every File Station path argument and its role. tests/modules/filestation/test_paths.py
# fails if a tool has a path argument missing from here, and runs every one of these through
# a table of hostile spellings. Restore's items are relative to the bin and resolved by
# `resolve_bin_items`, after its share.
PATH_PARAMS: dict[str, dict[str, PathRole]] = {
    "list_files": {"path": PathRole.READ},
    "list_recycle_bin": {"share": PathRole.SHARE},
    "search_files": {"folder_path": PathRole.READ},
    "get_file_info": {"paths": PathRole.READ},
    "get_dir_size": {"path": PathRole.READ},
    "download_file": {"path": PathRole.READ},
    "create_folder": {"paths": PathRole.NEW_FOLDER},
    "rename": {"path": PathRole.WRITE_TARGET},
    "copy_files": {"paths": PathRole.READ, "dest_folder": PathRole.DEST},
    "move_files": {"paths": PathRole.WRITE_TARGET, "dest_folder": PathRole.DEST},
    "delete_files": {"paths": PathRole.WRITE_TARGET},
    "upload_file": {"dest_folder": PathRole.DEST},
    "restore_from_recycle_bin": {"share": PathRole.SHARE, "dest_folder": PathRole.DEST},
}
# Arguments that are a new name, not a path (checked by `refuse_bad_name`), and restore's
# bin-relative items (`resolve_bin_items`).
NAME_PARAMS: dict[str, str] = {"rename": "new_name", "upload_file": "filename"}
BIN_ITEM_PARAMS: dict[str, str] = {"restore_from_recycle_bin": "paths"}
# Tools that put sources into a destination; each calls `refuse_planted_bin` (a source named
# `#recycle` landing where it would be a bin). test_paths.py runs each through it, and fails
# if a tool with sources and a DEST isn't listed here.
PLANTED_BIN_TOOLS: frozenset[str] = frozenset(
    {"copy_files", "move_files", "restore_from_recycle_bin"}
)
# Paths on this machine, not the NAS (checked against allowed_local_paths instead).
LOCAL_PARAMS: dict[str, set[str]] = {
    "download_file": {"dest_folder", "filename"},
    "upload_file": {"local_path"},
}

# What to do instead, per tool (QA round 1 on #515): a whole bin can't be renamed, and a
# restore can go elsewhere with dest_folder.
_PLANTED_ADVICE = {
    "copy_files": "Copy the folder's contents (its items by path) instead, or copy it into a "
    "subfolder.",
    "move_files": "Move the folder's contents (its items by path) instead, or move it into a "
    "subfolder.",
    "restore_from_recycle_bin": "Restore it into a subfolder with dest_folder.",
}
_OP_WORDS = {
    "copy_files": "Copy files",
    "move_files": "Move files",
    "restore_from_recycle_bin": "Restore",
}
_WRITE_OPS: dict[str, Literal["Delete", "Move", "Rename"]] = {
    "delete_files": "Delete",
    "move_files": "Move",
    "rename": "Rename",
}


# -- part rules (every role) ---------------------------------------------------------------


def check_parts(raw: str, param: str, suggestion: str = DEFAULT_SUGGESTION) -> str:
    """Refuse a NUL, an empty part, or a ``.``/``..`` part in ``raw``; return it normalized.

    Run on the path as given, before the share check rewrites it.
    """
    if "\x00" in raw:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"Invalid path {raw!r}: a NUL character isn't allowed. DSM cuts a path at a NUL, "
            "so the request would act on another item.",
            retryable=False,
            param=param,
            value=raw,
            suggestion=suggestion,
        )
    normalized = normalize_path(raw)
    parts = normalized.split("/")[1:]
    if any(_is_dot(part) for part in parts if part):
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"Invalid path {raw!r}: '.' and '..' parts aren't allowed.",
            retryable=False,
            param=param,
            value=raw,
            suggestion=suggestion,
        )
    if normalized != "/" and any(not part for part in parts):
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"Invalid path {raw!r}: an empty part (`//`) isn't allowed.",
            retryable=False,
            param=param,
            value=raw,
            suggestion=suggestion,
        )
    return normalized


# -- recycle bins --------------------------------------------------------------------------


def bin_index(parts: list[str]) -> int | None:
    """The index of the ``#recycle`` part when ``parts`` (a normalized path's parts, the
    share first) lead into a recycle bin, else None.

    A share's bin is ``/<share>/#recycle``. The user home service gives each user a bin too,
    which the user sees as ``/home/#recycle`` and an administrator as
    ``/homes/<user>/#recycle`` (pre-QA on #479; vdsm, all four versions). DSM matches
    ``#recycle`` exactly: ``#RECYCLE`` is another folder (408; probe on #494).
    """
    if len(parts) > 1 and parts[1] == "#recycle":
        return 1
    if len(parts) > 2 and parts[0].casefold() == "homes" and parts[2] == "#recycle":
        return 2
    return None


def in_bin(path: str) -> bool:
    """Whether ``path`` (normalized) is inside a recycle bin, or is one."""
    return bin_index(path.split("/")[1:]) is not None


def makes_a_bin(parent: str) -> bool:
    """Whether a folder named ``#recycle`` in ``parent`` would be a recycle bin to the tools
    (`bin_index`): ``parent`` is a shared folder or a user's home folder ``/homes/<user>``."""
    parts = normalize_path(parent).split("/")[1:]
    return bin_index([*parts, "#recycle"]) == len(parts)


def refuse_planted_bin(
    tool: str,
    given: Sequence[str],
    resolved: Sequence[str],
    dests: Sequence[str],
    param: str = "paths",
) -> None:
    """Refuse a copy, move or restore that would put a folder named ``#recycle`` where the
    tools would take it for a recycle bin (`makes_a_bin`), as `refuse_bad_name` refuses
    creating one there (pre-QA on #510; DSM allows it, probe on #494). ``given`` are the
    caller's own values (quoted in the refusal), ``resolved`` the checked paths, ``dests``
    where each would land."""
    for raw, path, dest in zip(given, resolved, dests, strict=True):
        if normalize_path(path).rsplit("/", 1)[-1] == "#recycle" and makes_a_bin(dest):
            _refuse(
                f"{_OP_WORDS.get(tool, tool)} refused: {raw!r} would land in {shown(dest)} "
                "as '#recycle', the name of its recycle bin. Nothing was changed.",
                param,
                raw,
                _PLANTED_ADVICE.get(tool, "Copy or move the folder's contents instead."),
            )


class BinWords(NamedTuple):
    whose: str  # what the bin is called in a refusal
    items: str  # whose deleted items are in it
    lister: str  # the tool that lists its items
    shared: bool  # a shared folder's bin, holding every user's deleted items


def whole_bin_words(parts: list[str]) -> BinWords:
    """How a refusal names the whole bin ``parts`` leads to (`bin_index`).

    A shared folder's bin holds every user's deleted items; a home folder's holds only
    its user's: the caller's own as ``/home/#recycle`` (pre-QA round 3 on #479), another
    user's as an administrator's ``/homes/<user>/#recycle``.
    """
    if bin_index(parts) == 2:
        user = parts[1]
        return BinWords(
            f"the whole recycle bin of {user}'s home folder ({user}'s /home/#recycle)",
            f"{user}'s deleted items",
            "list_files",
            shared=False,
        )
    if parts[0].casefold() == "home":
        return BinWords(
            "your home folder's whole recycle bin",
            "your deleted items",
            "list_recycle_bin",
            shared=False,
        )
    return BinWords(
        "the shared folder's whole recycle bin",
        "every user's deleted items",
        "list_recycle_bin",
        shared=True,
    )


# -- WRITE_TARGET --------------------------------------------------------------------------


def refuse_write_target(
    operation: Literal["Delete", "Move", "Rename"], path: str, param: str, given: str | None = None
) -> None:
    """Refuse a whole shared folder, a whole recycle bin or a user's home folder as what a
    delete deletes, a move moves or a rename renames.

    - **A shared folder** (#451, #479). Delete on ``/<share>`` deletes the shared folder
      itself on DSM 7.2.2 to 7.4.1, for any account with write access, beyond the bin's
      reach (DSM 7.1 refuses it). CopyMove with ``remove_src`` on it took it out of DSM's
      share list for the non-admin test user (DSM 7.1 to 7.4.1); Rename on it made it vanish
      for an administrator, under its new name too. Copying one only copies its contents,
      so ``copy_files`` takes one.
    - **A whole bin** (#479): Delete on ``/<share>/#recycle`` removes the bin folder with
      every user's deleted items (seska, DSM 7.1.1); moving or renaming one would take them
      out of the bin. A user's home bin, ``/homes/<user>/#recycle``, too.
    - **A user's home folder** (#494, #510): an administrator's Rename of
      ``/homes/<user>`` left the user's ``/home`` empty; DSM re-created the home at the
      user's next write, and renaming it back failed with 1200, so the user's files were
      orphaned (vdsm DSM 7.1 and 7.4.1).

    Called after the share check, so a one-part path that isn't a share (a typo) is an
    unknown share, not this (pre-QA on #480). ``path`` is the checked path; ``given`` the
    caller's own text, which the refusal quotes and returns (#489's rule).
    """
    parts = normalize_path(path).split("/")[1:]
    raw = path if given is None else given
    past = {"Delete": "deleted", "Move": "moved", "Rename": "renamed"}[operation]
    acting = {"Delete": "Deleting", "Move": "Moving", "Rename": "Renaming"}[operation]
    head = f"{operation}{'' if operation == 'Rename' else ' files'} refused: {raw!r} is"
    at = bin_index(parts)
    if at is not None and len(parts) == at + 1:
        words = whole_bin_words(parts)
        own_path = f"/{'/'.join(parts)}/<item>"
        if operation == "Delete":
            lost = "everything in the bin" + (
                ", other users' deleted items included" if words.shared else ""
            )
            empty = (
                "; to empty the whole bin, use DSM's Control Panel > Shared Folder > Action > "
                "Empty Recycle Bin"
                if words.shared
                else ""
            )
            message = f"{head} {words.whose}. Deleting it would permanently remove {lost}."
            suggestion = (
                f"To delete items from the bin for good, list them with {words.lister} and "
                f"delete each by its own path ({own_path}), after the user agrees{empty}."
            )
        else:
            message = f"{head} {words.whose}. {acting} it would take {words.items} out of the bin."
            if at == 2:
                suggestion = (
                    f"To bring items back, {parts[1]} can use restore_from_recycle_bin with "
                    f"share='home'; to move some elsewhere, move each by its own path "
                    f"({own_path})."
                )
            else:
                suggestion = (
                    "To bring items back, use restore_from_recycle_bin with the items "
                    "list_recycle_bin shows; to move some elsewhere, restore them with "
                    "dest_folder."
                )
        _refuse(f"{message} Nothing was {past}.", param, raw, suggestion)
    if len(parts) == 2 and parts[0].casefold() == "homes":
        _refuse(
            f"{head} {parts[1]}'s whole home folder. {acting} it through File Station "
            "breaks that user's home: DSM makes them a new, empty one at their next write, "
            f"and their files stay behind (tested on DSM 7.1 and 7.4.1). Nothing was {past}.",
            param,
            raw,
            f"Work with the items in it by their own paths ({path.rstrip('/')}/<item>), or "
            "manage the user's home in DSM's Control Panel > User.",
        )
    if len(parts) != 1:
        return
    if operation == "Delete":
        detail = (
            "delete_files doesn't delete or empty a shared folder: on DSM 7.2.2 to 7.4.1 "
            "(tested), and likely later, that would delete the shared folder itself, beyond "
            "the recycle bin's reach."
        )
        suggestion = (
            "To empty it, list it with list_files and delete its items by their own paths "
            "(leaving out its #recycle), after the user agrees; to delete the shared folder "
            "itself, use DSM's Control Panel > Shared Folder."
        )
    elif operation == "Move":
        detail = (
            "Moving one would remove the shared folder itself from DSM, beyond the recycle "
            "bin's reach (tested on DSM 7.1 to 7.4.1, as a non-admin with write access)."
        )
        suggestion = (
            "To move what's in it, list it with list_files and move its items by their own "
            "paths (leaving out its #recycle); to keep it and copy it, use copy_files; to "
            "move or rename the shared folder itself, use DSM's Control Panel > Shared Folder."
        )
    else:
        detail = (
            "Renaming one through File Station makes it disappear from DSM's shared folders, "
            "under its new name too (tested on DSM 7.1 to 7.4.1)."
        )
        suggestion = "Rename a shared folder in DSM's Control Panel > Shared Folder."
    _refuse(f"{head} a whole shared folder. {detail} Nothing was {past}.", param, raw, suggestion)


def _refuse(message: str, param: str, value: str, suggestion: str) -> None:
    error_response(
        ErrorCode.INVALID_PARAMETER,
        message,
        retryable=False,
        param=param,
        value=value,
        suggestion=suggestion,
    )


# -- new names -----------------------------------------------------------------------------


# Bidirectional controls (U+202E makes `invoice<RLO>txt.exe` show as `invoiceexe.txt`) and
# line/paragraph separators: refused in new names (maintainer, 2026-10-31). The zero-width
# joiner and non-joiner are allowed where Unicode uses them: `invisible_problem`.
# Unicode's Bidi_Control set: U+061C (Arabic letter mark) is the Arabic counterpart of
# U+200F (pre-QA r4).
_BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_LINE_BREAKS = frozenset("\u2028\u2029")


def name_problem(name: str, parent: str | None = None) -> str | None:
    """Why a name a tool would create (a new name, or a parent folder it creates) isn't
    one, beyond the structural checks only whole names get (empty, ``/``, NUL, dots); or
    None. One list, so a new name and a created parent can't drift apart (pre-QA r4)."""
    if any(unicodedata.category(c) == "Cc" and c != "\t" for c in name):
        return "contains a control character"
    if any(c in _BIDI for c in name):
        return "contains a character that changes text direction"
    if any(c in _LINE_BREAKS for c in name):
        return "contains a line or paragraph separator"
    if all(draws_nothing(c) for c in name):
        return "has no visible character"
    if name != name.strip():
        return "starts or ends with whitespace"
    # An invisible character where Unicode doesn't use one makes a look-alike name
    # (`report.pdf<ZWSP>`, `rep<soft hyphen>ort.pdf`), as a trailing space would
    # (maintainer, 2026-10-31); one where it does is ordinary (`नमस्ते`, NFD `Café`, a
    # Persian ZWNJ, `❤️`, `1️⃣`, 🏴󠁧󠁢󠁳󠁣󠁴󠁿): `invisible_problem`.
    problem = invisible_problem(name)
    if problem:
        return problem
    if name == "#recycle" and parent is not None and makes_a_bin(parent):
        return "is '#recycle', the name of a recycle bin there"
    return None


def refuse_bad_name(
    op: str,
    name: str,
    param: str,
    suggestion: str,
    label: str | None = None,
    value: str | None = None,
    *,
    parent: str | None = None,
) -> None:
    """Raise ``invalid_parameter`` for a name a tool would create that isn't one: empty,
    blank, a ``/``, a NUL or another control character (a tab inside the name is fine: DSM
    keeps it, maintainer 2026-10-31), a direction-changing character or a line separator, or
    no visible character at all (`name_problem`), ``.`` or ``..`` (#449), whitespace
    at its start or end (#488: DSM would keep it, and it's almost never meant), or
    ``#recycle`` where it would be taken for a bin: directly in a shared folder or in a
    user's home folder (``/homes/<user>``) (#510: DSM lets one be created where there's no
    bin folder yet). ``label`` names it in the message when
    it isn't ``param`` itself (an upload's local file name); ``parent`` is the folder it
    goes in, when known."""
    problem = (
        "is empty"
        if not name
        else "is blank"
        if not name.strip()
        else "contains '/'"
        if "/" in name
        else "contains a NUL character"
        if "\0" in name
        else "is '.' or '..'"
        if _is_dot(name)
        else name_problem(name, parent)
    )
    if problem:
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"{op} failed: {label or param} should be just a file name, but it {problem}.",
            retryable=False,
            param=param,
            value=name if value is None else value,
            suggestion=suggestion,
        )


# -- the entry point -----------------------------------------------------------------------


async def _refuse_bad_created_parents(
    client: DsmClient,
    raw: str,
    normalized: str,
    param: str,
    upto: int,
    suggestion: str | None = None,
) -> None:
    """Refuse a path whose missing parent folder the tool would create (``create_parents``,
    ``force_parent``) under a name `name_problem` refuses: padded (DSM makes
    ``/video/Uploads `` beside ``Uploads`` and reports success; vdsm DSM 7.1 and 7.4.1),
    with a control or invisible character, or ``#recycle`` where it would be a bin (pre-QA
    on #510). Only such parts are looked up, so an ordinary path costs no request; the
    parent is built as the walk goes, not re-joined for every part (pre-QA r4)."""
    parts = normalized.split("/")[1:]
    parent = "/" + parts[0] if parts else "/"
    for i in range(1, min(upto, len(parts))):
        problem = name_problem(parts[i], parent)
        prefix = f"{parent}/{parts[i]}"
        parent = prefix
        if problem is None:
            continue
        # Only a definite 408: an unreadable answer leaves it to DSM.
        if await path_exists(client, prefix) is False:
            _refuse(
                f"Invalid path {raw!r}: the folder {shown(prefix)} doesn't exist, and would be "
                f"created, but its name {problem}. Nothing was created.",
                param,
                raw,
                suggestion
                or "Give the folder a plain name, or pick an existing folder (list_files shows "
                "them).",
            )


async def resolve_paths(
    client: DsmClient,
    known_shares: set[str],
    tool: str,
    param: str,
    raws: list[str],
    *,
    error_param: str | None = None,
    suggestion: str | None = None,
    creates_parents: bool = False,
) -> list[ResolvedPath]:
    """Check ``raws``, the values of ``tool``'s ``param``, by `PATH_PARAMS`' role for it.

    ``error_param`` names the argument in errors when it isn't ``param`` (a destination that
    came from ``default_upload_dir``); ``suggestion`` replaces the default advice.
    ``creates_parents``: the tool creates missing parent folders (an upload's
    ``create_parents``, create_folder's ``force_parent``).
    """
    role = PATH_PARAMS[tool][param]
    named = error_param or param
    advice = suggestion or DEFAULT_SUGGESTION
    for raw in raws:
        check_parts(raw, named, advice)
    # Through the module, so a test can stand in for the share list.
    shared = await helpers.ensure_known_shares(client, list(raws), known_shares, named, suggestion)
    resolved = [ResolvedPath(normalize_path(p)) for p in shared]
    for raw, path in zip(raws, resolved, strict=True):
        if role is PathRole.WRITE_TARGET:
            refuse_write_target(_WRITE_OPS[tool], path, named, raw)
        elif role is PathRole.SHARE and len(path.split("/")) != 2:
            _refuse(
                f"Invalid shared folder {raw!r}: give the shared folder's name only.",
                named,
                raw,
                "Call list_shares to see the shared folders on this NAS.",
            )
        elif role is PathRole.NEW_FOLDER:
            parent, _, name = path.rpartition("/")
            if not parent:
                _refuse(
                    f"Create folder refused: {raw!r} is a shared folder, not a new folder in "
                    "one. Nothing was created.",
                    named,
                    raw,
                    "Give the new folder's full path inside a shared folder, e.g. "
                    "/video/Movies (shared folders are created in DSM's Control Panel).",
                )
            refuse_bad_name(
                "Create folder",
                name,
                named,
                "Give the new folder's name plainly, e.g. /video/Movies.",
                label=f"the new folder's name {name!r}",
                value=raw,
                parent=parent or "/",
            )
            if creates_parents:
                await _refuse_bad_created_parents(
                    client, raw, path, named, len(path.split("/")) - 2, suggestion
                )
        elif role is PathRole.DEST and creates_parents:
            await _refuse_bad_created_parents(
                client, raw, path, named, len(path.split("/")) - 1, suggestion
            )
    return resolved


def bin_item_origin(share_name: str, item: str) -> str:
    """The folder a bin item (a full path from `resolve_bin_items`) was deleted from: only
    the leading ``/<share>/#recycle`` comes off, since the item's own path may hold the same
    text again (#479). One helper for the restore and its planted-bin check (QA r1 on #515)."""
    original = f"/{share_name}" + item[len(f"/{share_name}/#recycle") :]
    return "/".join(original.split("/")[:-1]) or f"/{share_name}"


def refuse_restore_into_a_bin(given: Sequence[str], items: Sequence[str], share_name: str) -> None:
    """Refuse a restore, with no ``dest_folder``, of an item whose original folder is
    itself inside a bin (an item of a folder named ``#recycle`` deleted from a share's top:
    ``#recycle/#recycle/x``): it would go back into the bin while the reply said
    "Restored" (pre-QA on #515)."""
    for raw, item in zip(given, items, strict=True):
        origin = bin_item_origin(share_name, item)
        if in_bin(origin):
            _refuse(
                f"Restore refused: {raw!r} would go back to {shown(origin)}, inside the "
                "recycle bin. Nothing was restored.",
                "paths",
                raw,
                "Restore it with dest_folder, a folder outside the bin.",
            )


def resolve_bin_items(share: str, items: list[str], param: str = "paths") -> list[ResolvedPath]:
    """Restore's items, each as the full path of an item in ``share``'s recycle bin.

    An item is relative to the bin (``Shows/Ep1.mkv``, ``#recycle/Shows/Ep1.mkv``, either
    with a leading ``/``) or a full path into it (``/video/#recycle/...``, the share in any
    case, re-rooted on ``share``, which the caller has set to DSM's spelling, #353). Only
    padding before a leading ``/`` is dropped (#488). An item is in the bin only when it is
    ``#recycle`` or starts with ``#recycle/`` (#479: ``#recycled/x`` is an item named
    ``#recycled`` inside the bin). The part rules apply to the item as given; an item that
    names the whole bin (``""``, ``"/"``, ``#recycle``) is refused: restoring it would move
    the whole bin out (#479).
    """
    share_name = share.strip("/")
    bin_root = f"/{share_name}/#recycle"
    suggestion = "Give the item as list_recycle_bin shows it, e.g. Shows/Ep1.mkv."

    def bin_relative(rel: str) -> bool:
        return rel == "#recycle" or rel.startswith("#recycle/")

    resolved: list[ResolvedPath] = []
    for item in items:
        if "\x00" in item or any(_is_dot(part) for part in item.split("/") if part):
            check_parts(item, param, suggestion)
        p = strip_padding(item)
        head, _, rest = p.lstrip("/").partition("/")
        if head.casefold() == share_name.casefold() and bin_relative(rest):
            full = normalize_path(f"/{share_name}/{rest}")
        elif bin_relative(p.lstrip("/")):
            full = normalize_path(f"/{share_name}/{p.lstrip('/')}")
        else:
            full = normalize_path(f"/{share_name}/#recycle/{p.lstrip('/')}")
        if any(not part for part in full.split("/")[1:]):
            _refuse(
                f"Restore refused: {item!r} has an empty part (`//`). Nothing was restored.",
                param,
                item,
                suggestion,
            )
        if full == bin_root:
            _refuse(
                f"Restore refused: {item!r} names the whole recycle bin of /{share_name}, "
                "not an item in it. Nothing was restored.",
                param,
                item,
                suggestion,
            )
        resolved.append(ResolvedPath(full))
    return resolved
