"""Where upload_file and download_file may read and write on this machine.

Both tools take local paths from the model. Without a limit, a prompt-injected or
mistaken call could upload any readable file (keys, credentials) to the NAS, or
write a download anywhere the server's user can write. The File Station setting
``allowed_local_paths`` lists the folders they may use; it defaults to the user's
Downloads folder. ``default_download_dir``, when set, is always allowed too.

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
from pathlib import Path

logger = logging.getLogger(__name__)

_XDG_DOWNLOAD = re.compile(r'^\s*XDG_DOWNLOAD_DIR\s*=\s*"(?P<value>[^"]*)"\s*$')


def _xdg_downloads_dir() -> Path | None:
    """The Downloads folder from ``user-dirs.dirs`` (xdg-user-dirs), if set."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    try:
        text = (Path(config_home) / "user-dirs.dirs").read_text(encoding="utf-8")
    except OSError:
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


def allowed_local_roots(
    configured: list[str] | None, default_download_dir: str | None
) -> list[Path]:
    """The resolved folders upload/download may use.

    ``configured`` (the ``allowed_local_paths`` setting) replaces the default
    Downloads folder; ``default_download_dir`` is always added.
    """
    entries: list[str | Path] = list(configured) if configured else [default_downloads_dir()]
    raw: list[tuple[str, str | Path]] = [("allowed_local_paths", e) for e in entries]
    if default_download_dir:
        raw.append(("default_download_dir", default_download_dir))
    roots: list[Path] = []
    for setting, entry in raw:
        try:
            root = _resolve(entry)
        except ValueError as e:
            msg = f"filestation setting {setting}: {e}"
            raise ValueError(msg) from e
        if root not in roots:
            roots.append(root)
    logger.debug("Allowed local paths: %s", [str(r) for r in roots])
    return roots


def outside_allowed_roots(path: str | Path, roots: list[Path]) -> Path | None:
    """Return the resolved ``path`` if it's outside every root, else ``None``.

    Raises ``ValueError`` if ``path`` can't be resolved.
    """
    resolved = _resolve(path)
    if any(resolved == root or root in resolved.parents for root in roots):
        return None
    return resolved
