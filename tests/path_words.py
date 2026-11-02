"""The words that mark a tool argument as naming a NAS path or folder (#510).

One set for every module's structural guard (File Station's `PATH_PARAMS`, Download
Station's `DESTINATION_PARAMS`, Photos' `FOLDER_PARAMS`), so a new folder argument can't
pass one module's guard under a word another module's would catch (QA round 1 on #526).
"""

from __future__ import annotations

PATHLIKE_WORDS = frozenset(
    {
        "path",
        "paths",
        "folder",
        "folders",
        "share",
        "dest",
        "destination",
        "source",
        "sources",
        "target",
        "src",
        "dir",
        "parent",
    }
)


def words(param: str) -> set[str]:
    """An argument name's words, split on ``_``."""
    return set(param.split("_"))
