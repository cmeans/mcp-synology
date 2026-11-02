"""No tracked text file holds a hidden character, a control character or a look-alike space
(#515).

GitHub shows a "hidden or bidirectional Unicode text" banner on files with characters like
these, and editors flag them; in a project that refuses such characters in file names, an
example should show where it sits. Write it as an escape in code (``"rep\\u200bort.pdf"``)
and as a placeholder in prose (``rep<ZWSP>ort.pdf``, ``❤<VS16>``). File contents are
checked, not file names."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import regex

from mcp_synology.modules.filestation import helpers

ROOT = Path(__file__).resolve().parent.parent

# Default-ignorable code points (zero-width characters, joiners, variation selectors,
# tags, soft hyphen, BOM, Hangul fillers; the bidi controls are among them), the other
# characters the File Station checks say draw nothing (helpers' `_BRAILLE_BLANK` and
# `_ANNOTATION`: a test keeps this in step), the line and paragraph separators, every
# space but U+0020 (NBSP, U+3000, ...), and every control character but tab, LF and CR
# (ESC draws nothing on GitHub, and in a terminal `ESC[2J` clears the screen).
_HIDDEN = regex.compile(
    r"[\p{Default_Ignorable_Code_Point}\u2800\ufff9-\ufffb\U00013430-\U0001343f"
    r"\p{Zl}\p{Zp}\p{Zs}\p{Cc}--[\x20\t\n\r]]",
    flags=regex.V1,
)


def _in_ci() -> bool:
    return os.environ.get("CI", "").lower() not in ("", "0", "false")


# (GIT_WORK_TREE alone changes nothing here: the list comes from the index, and its paths
# are relative.)
_GIT_LOCATION = ("GIT_DIR", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


def _tracked_files() -> list[Path]:
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            # git's messages in English, so "not a git repository" below matches.
            env={
                # A hook's or a script's GIT_DIR, GIT_INDEX_FILE or GIT_COMMON_DIR would
                # point git at another repository or index than ROOT's.
                **{k: v for k, v in os.environ.items() if k not in _GIT_LOCATION},
                "LC_ALL": "C",
                "LANGUAGE": "",
            },
        ).stdout.decode("utf-8", "surrogateescape")
    except FileNotFoundError as e:
        if _in_ci():
            raise
        pytest.skip(f"git isn't installed: {e}")
    except subprocess.CalledProcessError as e:
        reason = e.stderr.decode("utf-8", "replace").strip()
        # An sdist, an archive export or a sandbox without the git directory: nothing to
        # list. CI always has a checkout, so there it's a failure, and so is any other git
        # error (dubious ownership, a broken index): the guard mustn't skip in a checkout.
        if _in_ci() or "not a git repository" not in reason:
            pytest.fail(f"git ls-files failed: {reason}")
        pytest.skip(f"not a git checkout: {reason}")
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
    tracked = _tracked_files()
    # An empty listing would check nothing and pass.
    assert ROOT / "pyproject.toml" in tracked
    for path in tracked:
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
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
        "\x1b",
        "\x0c",
        "\x00",
        "\x7f",
        "\x85",
    ],
)
def test_the_pattern_finds_each_kind(char: str) -> None:
    assert _HIDDEN.findall(f"a{char}b") == [char]
    assert not _HIDDEN.findall("a b\tc\u0301\r\n")


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
        # Only "\n" ends a line: "\r" and a form feed (itself reported) don't move the count.
        ("a\r\x0cb\n\u2800".encode(), [(1, ["U+000C"]), (2, ["U+2800"])]),
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


def _must_raise(exc: type[BaseException], match: str | None = None) -> None:
    """``_tracked_files()`` raises ``exc``. A skip there would escape ``pytest.raises`` and
    report the test as skipped, so it's a failure."""
    try:
        with pytest.raises(exc, match=match):
            _tracked_files()
    except pytest.skip.Exception as e:
        pytest.fail(f"skipped instead of raising {exc.__name__}: {e}")


@pytest.mark.parametrize("ci", ["true", ""])
def test_outside_a_checkout_it_skips_except_in_ci(
    ci: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_root = ROOT
    try:
        # In a linked worktree the git dir and the common dir differ.
        git_dir, common_dir = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
        ).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        git_dir = common_dir = str(tmp_path / "no-repo")
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    monkeypatch.setenv("CI", ci)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    # A hook's GIT_DIR (with GIT_WORK_TREE and the rest) mustn't point git back at this repo.
    # (With them kept, git lists this repo's files and nothing raises.)
    monkeypatch.setenv("GIT_DIR", git_dir)
    monkeypatch.setenv("GIT_WORK_TREE", str(real_root))
    monkeypatch.setenv("GIT_INDEX_FILE", str(Path(git_dir) / "index"))
    monkeypatch.setenv("GIT_COMMON_DIR", common_dir)
    if ci:
        _must_raise(pytest.fail.Exception, "not a git repository")
    else:
        with pytest.raises(pytest.skip.Exception, match="not a git checkout"):
            _tracked_files()


@pytest.mark.parametrize("var", _GIT_LOCATION)
def test_a_stray_git_location_variable_is_ignored(
    var: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each would make git read another repository or index (a missing index lists nothing).
    monkeypatch.setenv(var, str(tmp_path / "elsewhere"))
    monkeypatch.setenv("CI", "")
    try:
        tracked = _tracked_files()
    except pytest.skip.Exception as e:
        pytest.fail(f"skipped: {e}")
    assert ROOT / "pyproject.toml" in tracked


def test_an_empty_listing_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # git can list nothing without an error (GIT_INDEX_FILE naming a missing index): the
    # guard would then check nothing and pass.
    monkeypatch.setattr(f"{__name__}._tracked_files", lambda: [])
    with pytest.raises(AssertionError, match="pyproject.toml"):
        test_no_tracked_file_holds_a_hidden_character()


def test_any_other_git_error_fails_with_gits_message(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dubious ownership (a checkout mounted for another user) is a checkout git refuses:
    # skipping there would quietly turn the guard off.
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(
            128, args[0], stderr=b"fatal: detected dubious ownership in repository at '/w'"
        )

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setenv("CI", "")
    _must_raise(pytest.fail.Exception, "dubious ownership")


@pytest.mark.parametrize("ci", ["true", ""])
def test_without_git_it_skips_except_in_ci(ci: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(subprocess, "run", missing)
    monkeypatch.setenv("CI", ci)
    if ci:
        _must_raise(FileNotFoundError)
    else:
        with pytest.raises(pytest.skip.Exception, match="git isn't installed"):
            _tracked_files()


def test_git_speaks_english(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The skip matches git's English message; a German locale mustn't turn it into a failure.
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    monkeypatch.setenv("CI", "")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANGUAGE", "de")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    seen: dict[str, Any] = {}
    real_run = subprocess.run

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs.get("env") or {})
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    with pytest.raises(pytest.skip.Exception, match="not a git checkout"):
        _tracked_files()
    assert seen["LC_ALL"] == "C"
    assert seen["LANGUAGE"] == ""
