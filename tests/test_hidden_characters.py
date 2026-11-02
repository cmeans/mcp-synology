"""No tracked text file holds a hidden character, a control character or a look-alike space
(#515).

GitHub shows a "hidden or bidirectional Unicode text" banner on files with characters like
these, and editors flag them; in a project that refuses such characters in file names, an
example should show where it sits. Write it as an escape in code (``"rep\\u200bort.pdf"``)
and as a placeholder in prose (``rep<ZWSP>ort.pdf``, ``❤<VS16>``). File contents are
checked, not file names."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
    return os.environ.get("CI", "").lower() not in ("", "0", "false", "no", "off")


# The variables that point git at another repository, tree or index; each has a test below.
_GIT_LOCATION = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")
# Also dropped: where git writes objects and refs (a hook's GIT_OBJECT_DIRECTORY or
# GIT_REFERENCE_BACKEND would send the tests' own `git init` and `git add` to another
# repository). GIT_CONFIG_* stays: it's how a container or CI supplies safe.directory for
# a mounted checkout, and it can't move the work tree.
_GIT_DROPPED = (*_GIT_LOCATION, "GIT_OBJECT_DIRECTORY", "GIT_REFERENCE_BACKEND")


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd or ROOT,
        capture_output=True,
        check=True,
        # git's messages in English, so "not a git repository" below matches.
        env={
            # A hook's or a script's repository-location variables don't reach git.
            **{k: v for k, v in os.environ.items() if k not in _GIT_DROPPED},
            "LC_ALL": "C",
            "LANGUAGE": "",
        },
    ).stdout.decode("utf-8", "surrogateescape")


def _tracked_files() -> list[Path]:
    try:
        top = _git("rev-parse", "--show-toplevel").strip()
        listed = _git("ls-files", "-z")
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
            pytest.fail(f"git failed: {reason}")
        pytest.skip(f"not a git checkout: {reason}")
    if Path(top).resolve() != ROOT.resolve():
        # A copy inside another repository's work tree (an sdist unpacked in a monorepo):
        # git would list that repository, which holds nothing of this one.
        if _in_ci():
            pytest.fail(f"git's work tree is {top}, not {ROOT}")
        pytest.skip(f"not a git checkout: {ROOT} is inside {top}")
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
            continue  # deleted in the working tree, or now a folder
        except PermissionError:
            if path.is_dir():  # Windows reports a folder this way
                continue
            raise
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
    ("value", "ci"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("", False),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("OFF", False),
    ],
)
def test_in_ci(value: str, ci: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", value)
    assert _in_ci() is ci


def _require_git() -> None:
    """The tests below run git; without it the guard skips, and so do they (except in CI)."""
    if shutil.which("git") is None and not _in_ci():
        pytest.skip("git isn't installed")


@pytest.mark.parametrize(
    ("git", "ci", "skips"),
    [(None, "", True), (None, "true", False), ("/usr/bin/git", "", False)],
)
def test_require_git(
    git: str | None, ci: str, skips: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: git)
    monkeypatch.setenv("CI", ci)
    if skips:
        with pytest.raises(pytest.skip.Exception, match="git isn't installed"):
            _require_git()
    else:
        try:
            _require_git()
        except pytest.skip.Exception as e:
            pytest.fail(f"skipped: {e}")


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
    _require_git()
    real_root = ROOT
    try:
        # In a linked worktree the git dir and the common dir differ. (Relative paths come
        # back relative to ROOT; --path-format=absolute would need git 2.31.)
        git_dir, common_dir = (
            str((ROOT / line).resolve())
            for line in _git("rev-parse", "--git-dir", "--git-common-dir").splitlines()
        )
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        git_dir = common_dir = str(tmp_path / "no-repo")
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    monkeypatch.setenv("CI", ci)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    # A hook's GIT_DIR (with GIT_WORK_TREE and the rest) mustn't point git back at this repo.
    # (Kept, they'd point git back at this repo: the top-level check would then skip with
    # "is inside" and fail in CI with "work tree is", not with "not a git repository".)
    monkeypatch.setenv("GIT_DIR", git_dir)
    monkeypatch.setenv("GIT_WORK_TREE", str(real_root))
    monkeypatch.setenv("GIT_INDEX_FILE", str(Path(git_dir) / "index"))
    monkeypatch.setenv("GIT_COMMON_DIR", common_dir)
    if ci:
        _must_raise(pytest.fail.Exception, "not a git repository")
    else:
        with pytest.raises(pytest.skip.Exception, match="not a git repository"):
            _tracked_files()


@pytest.mark.parametrize("var", _GIT_LOCATION)
def test_a_stray_git_location_variable_is_ignored(
    var: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each would make git read another repository, tree or index: ROOT's parent as the work
    # tree lists nothing (ROOT's files would sit under a prefix the index doesn't have), and
    # as the git dir, the common dir or the index file it's an error.
    _require_git()
    monkeypatch.setenv("CI", "")
    _tracked_files()  # outside a checkout (an sdist, an archive) this skips, as the guard does
    monkeypatch.setenv(var, str(ROOT.parent))
    try:
        tracked = _tracked_files()
    except pytest.skip.Exception as e:
        pytest.fail(f"skipped: {e}")
    assert ROOT / "pyproject.toml" in tracked


@pytest.mark.parametrize("ci", ["true", ""])
def test_inside_another_repository_it_skips_except_in_ci(
    ci: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An sdist unpacked in a monorepo or a dist-git checkout.
    _require_git()
    repo = tmp_path / "outer"
    _git("init", "-q", str(repo), cwd=tmp_path)
    copy = repo / "mcp-synology-0.0"
    copy.mkdir()
    (copy / "pyproject.toml").write_text("")
    monkeypatch.setattr(f"{__name__}.ROOT", copy)
    monkeypatch.setenv("CI", ci)
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES", raising=False)
    if ci:
        _must_raise(pytest.fail.Exception, "work tree is")
    else:
        with pytest.raises(pytest.skip.Exception, match="is inside"):
            _tracked_files()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_checkout_reached_through_a_symlink_is_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # git reports the real path as its top level (macOS's /var is /private/var).
    _require_git()
    real = tmp_path / "real"
    _git("init", "-q", str(real), cwd=tmp_path)
    (real / "pyproject.toml").write_text("")
    _git("add", "-A", cwd=real)
    (tmp_path / "link").symlink_to(real)
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path / "link")
    monkeypatch.setenv("CI", "")
    try:
        tracked = _tracked_files()
    except pytest.skip.Exception as e:
        pytest.fail(f"skipped: {e}")
    assert tmp_path / "link" / "pyproject.toml" in tracked


def test_git_keeps_its_config_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    # A mounted checkout's safe.directory often comes from GIT_CONFIG_COUNT/KEY/VALUE,
    # GIT_CONFIG_PARAMETERS (`git -c`) or GIT_CONFIG_GLOBAL; without them the guard would
    # fail where git works.
    kept = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*",
        "GIT_CONFIG_PARAMETERS": "'safe.directory'='*'",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CEILING_DIRECTORIES": os.devnull,
    }
    dropped = (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_REFERENCE_BACKEND",
    )
    for k, v in {**kept, **dict.fromkeys(dropped, "x")}.items():
        monkeypatch.setenv(k, v)
    seen: dict[str, str] = {}

    def spy(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(args[0], 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", spy)
    _git("status")
    assert {k: seen.get(k) for k in kept} == kept
    assert not set(dropped) & set(seen)


def test_a_tracked_path_that_became_a_folder_is_passed_over_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "now-a-folder").mkdir()
    monkeypatch.setattr(
        f"{__name__}._tracked_files",
        lambda: [tmp_path / "pyproject.toml", tmp_path / "now-a-folder"],
    )
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    real = Path.read_bytes

    def windows(self: Path) -> bytes:
        if self.is_dir():
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", windows)
    test_no_tracked_file_holds_a_hidden_character()


def test_an_unreadable_tracked_file_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Only a folder is passed over: a file the guard can't read must not go unchecked.
    (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.setattr(f"{__name__}._tracked_files", lambda: [tmp_path / "pyproject.toml"])
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)

    def denied(self: Path) -> bytes:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(PermissionError):
        test_no_tracked_file_holds_a_hidden_character()


def test_a_stray_reference_backend_gets_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tests' own `git init` mustn't rewrite another repository's HEAD (git 2.45+ reads
    # GIT_REFERENCE_BACKEND; older git ignores it).
    _require_git()
    victim = tmp_path / "victim"
    _git("init", "-q", str(victim), cwd=tmp_path)
    head = (victim / ".git" / "HEAD").read_text()
    monkeypatch.setenv("GIT_REFERENCE_BACKEND", f"files://{victim / '.git'}")
    _git("init", "-q", "-b", "other", str(tmp_path / "repo"), cwd=tmp_path)
    assert (victim / ".git" / "HEAD").read_text() == head


def test_a_stray_object_directory_gets_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tests' own `git add` mustn't write objects into another repository's store.
    _require_git()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(elsewhere))
    repo = tmp_path / "repo"
    _git("init", "-q", str(repo), cwd=tmp_path)
    (repo / "f.txt").write_text("x")
    _git("add", "-A", cwd=repo)
    assert not any(elsewhere.iterdir())


def test_odd_names_and_deleted_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A tracked name that isn't UTF-8 is still checked, and a tracked file deleted from the
    # working tree is passed over.
    _require_git()
    # Through _git: a hook's GIT_DIR would otherwise point these at the real repository.
    _git("init", "-q", str(tmp_path), cwd=tmp_path)
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "gone.txt").write_text("")
    try:
        with open(os.path.join(os.fsencode(tmp_path), b"a\xff.txt"), "wb") as f:
            f.write("x\u200by\n".encode())
    # APFS refuses names that aren't UTF-8 (OSError), and Windows can't even decode the
    # bytes path (UnicodeDecodeError, PEP 529); CI's ext4 takes it.
    except (OSError, UnicodeError) as err:
        pytest.skip(f"this platform can't create the name: {err}")
    _git("add", "-A", cwd=tmp_path)
    (tmp_path / "gone.txt").unlink()
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    monkeypatch.setenv("CI", "")
    with pytest.raises(AssertionError) as e:
        test_no_tracked_file_holds_a_hidden_character()
    assert "a\udcff.txt:1: U+200B" in str(e.value)
    assert "gone.txt" not in str(e.value)


def test_an_empty_listing_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # Should git ever list nothing without an error, the guard would check nothing and pass.
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
    _require_git()
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
