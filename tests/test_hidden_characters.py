"""No tracked text file holds a hidden character or a look-alike space (#515).

GitHub shows a "hidden or bidirectional Unicode text" banner on files with characters like
these, and editors flag them; in a project that refuses such characters in file names, an
example should show where it sits. Write it as an escape in code (``"rep\\u200bort.pdf"``)
and as a placeholder in prose (``rep<ZWSP>ort.pdf``, ``❤<VS16>``). File contents are
checked, not file names."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import regex

from mcp_synology.modules.filestation import helpers

ROOT = Path(__file__).resolve().parent.parent

# Default-ignorable code points (zero-width characters, joiners, variation selectors,
# tags, soft hyphen, BOM, Hangul fillers; the bidi controls are among them), the other
# characters the File Station checks say draw nothing (helpers' `_BRAILLE_BLANK` and
# `_ANNOTATION`: a test keeps this in step), the line and paragraph separators, and every
# space but U+0020 (NBSP, U+3000, ...).
_HIDDEN = regex.compile(
    r"[\p{Default_Ignorable_Code_Point}\u2800\ufff9-\ufffb\U00013430-\U0001343f"
    r"\p{Zl}\p{Zp}\p{Zs}--\x20]",
    flags=regex.V1,
)


def _in_ci() -> bool:
    return os.environ.get("CI", "").lower() not in ("", "0", "false")


def _tracked_files() -> list[Path]:
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout.decode("utf-8", "surrogateescape")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        # An sdist, an archive export or a sandbox without the git directory: nothing to
        # list. CI always has a checkout, so there it's a failure.
        if _in_ci():
            raise
        pytest.skip(f"not a git checkout: {e}")
    return [ROOT / name for name in listed.split("\0") if name]


def _hidden_lines(data: bytes) -> list[tuple[int, list[str]]]:
    """(line number, code points) for each line of ``data`` with a hidden character; none
    for a binary file."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", "replace")
    elif b"\0" in data[:8192]:
        return []  # binary
    else:
        # A stray byte mustn't hide the rest of the file.
        text = data.decode("utf-8", "replace")
    found = []
    # Only "\n" ends a line, so the line number is the one an editor shows.
    for number, line in enumerate(text.split("\n"), 1):
        hidden = sorted({f"U+{ord(c):04X}" for c in _HIDDEN.findall(line)})
        if hidden:
            found.append((number, hidden))
    return found


def test_no_tracked_file_holds_a_hidden_character() -> None:
    found = []
    for path in _tracked_files():
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            continue  # deleted in the working tree
        for number, hidden in _hidden_lines(data):
            found.append(f"{path.relative_to(ROOT)}:{number}: {', '.join(hidden)}")
    assert not found, "write these as escapes or <NAME> placeholders:\n" + "\n".join(found)


@pytest.mark.parametrize(
    "char",
    [
        "\u200b",
        "\u202e",
        "\ufeff",
        "\U000e0067",
        "\u2800",
        "\ufff9",
        "\ufffa",
        "\ufffb",
        "\U00013430",
        "\U0001343f",
        "\u00a0",
        "\u3000",
        "\u2028",
        "\u2029",
    ],
)
def test_the_pattern_finds_each_kind(char: str) -> None:
    assert _HIDDEN.findall(f"a{char}b") == [char]
    assert not _HIDDEN.findall("a b\tc\u0301")


def test_the_pattern_covers_what_the_checks_call_drawing_nothing() -> None:
    for char in helpers._BRAILLE_BLANK + "".join(helpers._ANNOTATION):
        assert _HIDDEN.fullmatch(char), hex(ord(char))


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("a\nb\u200bc\n".encode(), [(2, ["U+200B"])]),
        # A stray byte doesn't hide the rest of the file.
        (b"\xff\n" + "x\u200by".encode(), [(2, ["U+200B"])]),
        # UTF-16 is read (its ASCII has NUL bytes); the BOM itself isn't reported.
        ("a\n\u00a0".encode("utf-16"), [(2, ["U+00A0"])]),
        # Only "\n" ends a line: "\r" and a form feed don't move the count.
        ("a\r\x0cb\n\u2800".encode(), [(2, ["U+2800"])]),
        (b"\x89PNG\0\0\xe2\x80\x8b", []),
    ],
    ids=["utf-8", "stray-byte", "utf-16", "line-count", "binary"],
)
def test_hidden_lines(data: bytes, expected: list[tuple[int, list[str]]]) -> None:
    assert _hidden_lines(data) == expected


@pytest.mark.parametrize(
    ("value", "ci"), [("true", True), ("1", True), ("", False), ("false", False), ("0", False)]
)
def test_in_ci(value: str, ci: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", value)
    assert _in_ci() is ci


@pytest.mark.parametrize("ci", ["true", ""])
def test_outside_a_checkout_it_skips_except_in_ci(
    ci: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    monkeypatch.setenv("CI", ci)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    if ci:
        try:
            _tracked_files()
        except pytest.skip.Exception:
            pytest.fail("skipped in CI")
        except subprocess.CalledProcessError:
            return
        pytest.fail("listed files outside a checkout")
    else:
        with pytest.raises(pytest.skip.Exception):
            _tracked_files()
