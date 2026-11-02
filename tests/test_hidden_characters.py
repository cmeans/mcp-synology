"""No tracked text file holds a character that draws nothing (#515).

GitHub shows a "hidden or bidirectional Unicode text" banner on any file with one, and
editors flag them; in a project that refuses such characters in file names, an example
should show where it sits. Write it as an escape in code (``"rep\\u200bort.pdf"``) and as
a placeholder in prose (``rep<ZWJ>ort.pdf``, ``❤<VS16>``)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import regex

ROOT = Path(__file__).resolve().parent.parent

# Default-ignorable code points (zero-width characters, joiners, variation selectors,
# tags, soft hyphen, BOM, Hangul fillers) and the bidi controls.
_HIDDEN = regex.compile(r"[\p{Default_Ignorable_Code_Point}\p{Bidi_Control}]")


def _tracked_text_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [ROOT / name for name in listed.split("\0") if name]


def test_no_tracked_file_holds_a_hidden_character() -> None:
    found = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # binary, or deleted in the working tree
        for number, line in enumerate(text.splitlines(), 1):
            hidden = sorted({f"U+{ord(c):04X}" for c in _HIDDEN.findall(line)})
            if hidden:
                found.append(f"{path.relative_to(ROOT)}:{number}: {', '.join(hidden)}")
    assert not found, "write these as escapes or <NAME> placeholders:\n" + "\n".join(found)
