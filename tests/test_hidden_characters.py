"""No tracked text file holds a hidden character or a look-alike space (#515).

GitHub shows a "hidden or bidirectional Unicode text" banner on any file with one, and
editors flag them; in a project that refuses such characters in file names, an example
should show where it sits. Write it as an escape in code (``"rep\\u200bort.pdf"``) and as
a placeholder in prose (``rep<ZWJ>ort.pdf``, ``❤<VS16>``)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import regex

ROOT = Path(__file__).resolve().parent.parent

# Default-ignorable code points (zero-width characters, joiners, variation selectors,
# tags, soft hyphen, BOM, Hangul fillers), the bidi controls, the other characters the
# File Station spec says draw nothing (the Braille blank U+2800, the interlinear
# annotation characters U+FFF9-FFFB, the Egyptian format controls U+13430-1343F), the line
# and paragraph separators, and every space but U+0020 (NBSP, U+3000, ...).
_HIDDEN = regex.compile(
    r"[\p{Default_Ignorable_Code_Point}\p{Bidi_Control}\u2800\ufff9-\ufffb"
    r"\U00013430-\U0001343f\p{Zl}\p{Zp}\p{Zs}--\x20]",
    flags=regex.V1,
)


def _tracked_files() -> list[Path]:
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout.decode()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        # An sdist, an archive export or a sandbox without the git directory: nothing to
        # list. CI always has a checkout, so there it's a failure.
        if os.environ.get("CI"):
            raise
        pytest.skip(f"not a git checkout: {e}")
    return [ROOT / name for name in listed.split("\0") if name]


def test_no_tracked_file_holds_a_hidden_character() -> None:
    found = []
    for path in _tracked_files():
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            continue  # deleted in the working tree
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = data.decode("utf-16", "replace")
        elif b"\0" in data[:8192]:
            continue  # binary
        else:
            # A stray byte mustn't hide the rest of the file.
            text = data.decode("utf-8", "replace")
        # Only "\n" ends a line, so the line number is the one an editor shows.
        for number, line in enumerate(text.split("\n"), 1):
            hidden = sorted({f"U+{ord(c):04X}" for c in _HIDDEN.findall(line)})
            if hidden:
                found.append(f"{path.relative_to(ROOT)}:{number}: {', '.join(hidden)}")
    assert not found, "write these as escapes or <NAME> placeholders:\n" + "\n".join(found)


@pytest.mark.parametrize(
    "char",
    ["\u200b", "\u202e", "\ufeff", "\U000e0067", "\u2800", "\ufffa", "\u00a0", "\u2028"],
)
def test_the_pattern_finds_each_kind(char: str) -> None:
    assert _HIDDEN.findall(f"a{char}b") == [char]
    assert not _HIDDEN.findall("a b\tc")
