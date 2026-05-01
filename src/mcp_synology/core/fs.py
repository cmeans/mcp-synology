"""Filesystem utilities used across `core/`.

Currently houses `atomic_write_text`, used by every site that persists
runtime state or user config without a torn-write window.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write text to ``path`` atomically.

    Writes to a sibling ``.tmp`` file first, then ``Path.replace()`` (which
    maps to ``os.replace``) renames it onto the target. ``os.replace`` is
    atomic on both POSIX and Windows, so a process kill, OOM, or power loss
    between truncate and final write can never produce a zero-byte or
    half-written file at ``path`` — the caller either reads the previous
    contents (rename hadn't happened) or the new contents (rename
    happened).

    Creates parent directories if missing. On any failure the temp file is
    best-effort cleaned up before the exception propagates.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(content, encoding=encoding)
        tmp.replace(path)
    except BaseException:
        # Best-effort temp cleanup; don't mask the original exception.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError as cleanup_err:
            logger.warning("Failed to clean up temp file %s: %s", tmp, cleanup_err)
        raise
