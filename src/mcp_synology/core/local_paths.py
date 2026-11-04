"""Where tools may read and write on this machine (#186, #257).

Several tools take local paths from the model: File Station's upload_file and
download_file, Download Station's create_download (``torrent_file_path``). Without a
limit, a prompt-injected or mistaken call could upload any readable file (keys,
credentials) to the NAS, or write a download anywhere the server's user can write.

``allowed_local_paths`` lists the folders they may use. At the top level of the
config it applies to every module, and a module's own ``allowed_local_paths`` adds to
it. While the top-level list is unset, the rule from before #257 stands: a module's
own list replaces the default, the user's Downloads folder. File Station's
``default_download_dir``, when set, is always allowed too.

Paths are compared after resolving ``~``, ``..`` and symlinks, so a link inside an
allowed folder can't lead out of it. ``/`` is an allowed root like any other: on
macOS and Linux it allows everything. On Windows it is the root of one drive, so
list each drive (``C:\\``, ``D:\\``) instead.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from mcp_synology.core.errors import ErrorCode
from mcp_synology.core.formatting import error_response

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

_XDG_DOWNLOAD = re.compile(r'^\s*XDG_DOWNLOAD_DIR\s*=\s*"(?P<value>[^"]*)"\s*$')


def _xdg_downloads_dir() -> Path | None:
    """The Downloads folder from ``user-dirs.dirs`` (xdg-user-dirs), if set."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    dirs_file = Path(config_home) / "user-dirs.dirs"
    try:
        # Every command gets here: a device would read until memory ran out, a pipe would wait.
        if not dirs_file.is_file():
            return None
        text = dirs_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    home = Path.home()
    for line in text.splitlines():
        match = _XDG_DOWNLOAD.match(line)
        if not match:
            continue
        value = match.group("value")
        if value.startswith("$HOME"):
            path = home / value[len("$HOME") :].lstrip("/")
        elif value.startswith("/"):
            path = Path(value)
        else:
            return None
        # xdg-user-dirs points a disabled directory at $HOME itself; confining to all
        # of home would be far wider than the user chose.
        return None if path == home else path
    return None


def default_downloads_dir(platform: str = sys.platform) -> Path:
    """The user's Downloads folder on this platform, or ``~/Downloads`` as a fallback.

    Linux and other XDG systems read ``user-dirs.dirs``. macOS and Windows use
    ``~/Downloads`` (on Windows, ``%USERPROFILE%\\Downloads``); a Windows Downloads
    folder moved elsewhere needs ``allowed_local_paths``.
    """
    found = None if platform in ("win32", "darwin") else _xdg_downloads_dir()
    return found or Path.home() / "Downloads"


def _resolve(path: str | Path) -> Path:
    """Resolve ``~``, ``..`` and symlinks. Raises ``ValueError`` when that's impossible:
    a NUL byte (every Python), or a symlink loop (``RuntimeError`` on 3.11/3.12)."""
    try:
        return Path(path).expanduser().resolve(strict=False)
    except UnicodeEncodeError as e:  # a ValueError, whose text is Python's codec message
        msg = f"{str(path)!r} can't be resolved: this system's locale can't encode the path"
        raise ValueError(msg) from e
    except (OSError, ValueError, RuntimeError) as e:
        msg = f"{str(path)!r} can't be resolved: {e}"
        raise ValueError(msg) from e


def _lexical(path: str | Path) -> Path:
    """``~`` and ``..`` handled, symlinks not followed."""
    return Path(os.path.normpath(os.path.abspath(Path(path).expanduser())))


def lexically_inside(path: str | Path, roots: list[Path]) -> bool:
    """Whether ``path``, read literally (no symlinks followed), is inside a root.

    Call it only after ``outside_allowed_roots`` resolved ``path``: anything that could
    make this fail (a NUL byte, an unknown ``~user``) already failed there.
    """
    lexical = _lexical(path)
    return any(lexical == root or root in lexical.parents for root in roots)


def expand_setting(setting: str, entry: str) -> Path:
    """``~`` expanded. An unknown ``~user`` raises RuntimeError, which pydantic doesn't
    report as a validation error; re-raise it as ValueError so it does (QA R2 on #186)."""
    try:
        return Path(entry).expanduser()
    except RuntimeError as e:
        msg = f"{setting}: {entry!r} can't be expanded: {e}"
        raise ValueError(msg) from e


def has_control_character(value: str) -> bool:
    """A control character, a line or paragraph separator or a lone surrogate: what can forge a
    line of output, move the terminal, or crash the server's first reply, which can't encode a
    surrogate. Joiners and the like are fine: names need them."""
    return any(unicodedata.category(c) in ("Cc", "Zl", "Zp", "Cs") for c in value)


# Inside double quotes a backslash starts an escape: "C:\temp\new.log" loaded with a real tab
# and newline, with no error (QA round 3 on #532).
CONTROL_CHARACTER_ADVICE = (
    "has a control character. Inside double quotes a backslash starts an escape, so write a "
    "Windows path in single quotes."
)


def check_resolvable(setting: str, entry: str) -> Path:
    """``~`` expanded, and the path checked to have no control character, which would reach
    the model's instructions as written, and to resolve (a NUL byte; a symlink loop on Python
    3.11 and 3.12). The server resolves these when it starts, so a config that passes here
    starts: otherwise `check` passed what `serve` crashed on (#511 PR 3). The instructions
    print the resolved path, so that's checked too: a link can lead to a folder with a line
    break in its name (QA round 1 on #541)."""
    if has_control_character(entry):
        msg = f"{setting} {CONTROL_CHARACTER_ADVICE}"
        raise ValueError(msg)
    path = expand_setting(setting, entry)
    try:
        resolved = _resolve(path)
    except ValueError as e:
        msg = f"{setting}: {e}"
        raise ValueError(msg) from e
    if has_control_character(str(resolved)):
        msg = f"{setting} resolves to a path with a control character: {str(resolved)!r}"
        raise ValueError(msg)
    return path


def check_local_path_entries(setting: str, value: list[str] | None) -> list[str] | None:
    """Validate an ``allowed_local_paths`` list: every entry absolute or starting with ``~``.

    A relative entry would resolve against the server's working directory, which
    depends on how the MCP client launched it (QA R1 on #186).
    """
    for entry in value or []:
        if not check_resolvable(setting, entry).is_absolute():
            msg = f"{setting} entries must be absolute (or start with ~): {entry!r}"
            raise ValueError(msg)
    return value


def allowed_local_roots(
    module_paths: list[str] | None,
    default_download_dir: str | None = None,
    *,
    shared_paths: list[str] | None = None,
    module: str = "filestation",
) -> list[Path]:
    """The resolved folders a module's tools may use.

    With ``shared_paths`` (the top-level ``allowed_local_paths``) set, they're the
    shared folders plus the module's own. Without it, the module's own list replaces
    the Downloads default, as before #257. ``default_download_dir`` is always added.
    """
    raw: list[tuple[str, str | Path]] = []
    if shared_paths:
        raw += [("allowed_local_paths", e) for e in shared_paths]
        raw += [(f"{module} setting allowed_local_paths", e) for e in module_paths or []]
    else:
        own: Sequence[str | Path] = module_paths or [default_downloads_dir()]
        raw += [(f"{module} setting allowed_local_paths", e) for e in own]
    if default_download_dir:
        raw.append((f"{module} setting default_download_dir", default_download_dir))
    roots: dict[Path, None] = {}  # in order, without repeats
    for setting, entry in raw:
        try:
            root = _resolve(entry)
        except ValueError as e:
            msg = f"{setting}: {e}"
            raise ValueError(msg) from e
        roots[root] = None
    logger.debug("Allowed local paths: %s", [str(r) for r in roots])
    return list(roots)


def roots_for_description(roots: list[Path]) -> str:
    """The allowed folders, as a local-path parameter's description names them.

    Some clients never pass the server's instructions to the model (claude.ai through the
    desktop app, in the 0.10.0rc5 and rc6 live tests), so a parameter that takes a local
    path names the folders itself instead of pointing to the instructions' LOCAL PATHS.
    """
    return "one of the allowed local folders (" + ", ".join(str(r) for r in roots) + ")"


def outside_allowed_roots(path: str | Path, roots: list[Path]) -> Path | None:
    """Return the resolved ``path`` if it's outside every root, else ``None``.

    Raises ``ValueError`` if ``path`` can't be resolved.
    """
    resolved = _resolve(path)
    if any(resolved == root or root in resolved.parents for root in roots):
        return None
    return resolved


def refuse_outside_roots(
    operation: str,
    param: str,
    value: str,
    path: Path,
    roots: list[Path] | None,
    *,
    module: str,
    hide_unknown_users: bool = False,
) -> None:
    """Refuse a local path outside the allowed folders (``allowed_local_paths``).

    ``roots`` of ``None`` means unconfined (direct callers); the tools always pass them.
    With ``hide_unknown_users``, a ``~user`` that doesn't exist gets the same refusal as
    any path outside, so the reply doesn't reveal which local users exist (#257).
    """
    if roots is None:
        return

    def refuse(shown: str) -> NoReturn:
        error_response(
            ErrorCode.PERMISSION_DENIED,
            f"{operation} refused: {shown} is outside the folders this server may use on "
            f"this machine: {', '.join(str(r) for r in roots)}.",
            retryable=False,
            param=param,
            value=value,
            suggestion=(
                "Use a path inside one of those folders, or ask the user to add the folder "
                "to allowed_local_paths (at the top level of the config for every module, "
                f"or in the {module} module settings, which replace the Downloads default "
                "when there's no top-level list)."
            ),
        )

    if hide_unknown_users:
        try:
            Path(path).expanduser()
        except RuntimeError:
            refuse(value)
    try:
        refused = outside_allowed_roots(path, roots)
    except ValueError as e:
        # A NUL byte, or a symlink loop on Python 3.11/3.12 (QA R1 on #186).
        error_response(
            ErrorCode.INVALID_PARAMETER,
            f"{operation} refused: {str(e).rstrip('.')}.",
            retryable=False,
            param=param,
            value=value,
            suggestion="Pass a plain local path without NUL characters or symlink loops.",
        )
    if refused is None:
        return
    # Explain the resolved path only for a link inside an allowed folder that leads out;
    # for anything else it would reveal what exists outside (QA R1 on #186).
    refuse(f"{value} (resolves to {refused})" if lexically_inside(path, roots) else value)
