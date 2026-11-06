#!/usr/bin/env python3
"""Print each runtime dependency pinned to the lowest version pyproject.toml allows.

CI's `floor` job installs this list and runs the unit tests on it (#115), since nothing else
tests what a floor promises. A dependency this can't read is an error, so the job can't quietly
stop covering it. Standard library only.

Usage:
    python scripts/runtime_floors.py > floors.txt
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# A name, one `>=` floor, then any caps. Extras and markers don't match: no dependency has them
# today, and one that did would need a decision about what its floor means here.
_WITH_FLOOR = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)>=([^,;\s]+)(?:,(?:<|<=|!=)[^,;\s]+)*")


def floors(dependencies: list[str]) -> list[str]:
    """Return `name==floor` for each dependency; raise ValueError naming one that has none."""
    pins = []
    for dependency in dependencies:
        match = _WITH_FLOOR.fullmatch(re.sub(r"\s", "", dependency))
        if match is None:
            raise ValueError(
                f"{dependency!r} has no `>=` floor this script can read. Give it one, or teach "
                "this script its form, so CI's floor job still covers it."
            )
        pins.append(f"{match[1]}=={match[2]}")
    return pins


def main(pyproject: Path = PYPROJECT) -> int:
    dependencies = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
    try:
        print("\n".join(floors(dependencies)))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
