"""Tests for scripts/changelog_fragments.py (#731)."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "changelog_fragments.py"
_SPEC = importlib.util.spec_from_file_location("changelog_fragments", _PATH)
assert _SPEC and _SPEC.loader
script = importlib.util.module_from_spec(_SPEC)
sys.modules["changelog_fragments"] = script
_SPEC.loader.exec_module(script)

ROOT = Path(__file__).resolve().parents[2]


def _dir(tmp_path: Path, files: dict[str, str]) -> Path:
    fragments = tmp_path / "changelog.d"
    fragments.mkdir(parents=True)
    for name, text in {"README.md": "how\n", "template.md": "{{ x }}\n", **files}.items():
        (fragments / name).write_text(text, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n<!-- towncrier release notes start -->\n",
        encoding="utf-8",
    )
    _pyproject(tmp_path, "1.0.0")
    return tmp_path


_START = "<!-- towncrier release notes start -->"


def _pyproject(root: Path, version: str) -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nversion = "{version}"\n\n[tool.towncrier]\nstart_string = "{_START}\\n"\n',
        encoding="utf-8",
    )


def _release(root: Path, version: str) -> None:
    """What a release PR's build leaves: the version bumped, its section under the marker."""
    _pyproject(root, version)
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## Unreleased\n\n{_START}\n\n## {version} (2026-01-01)\n\n- x\n",
        encoding="utf-8",
    )


def test_the_repos_own_fragments_pass() -> None:
    assert script.fragment_problems(ROOT) == []


@pytest.mark.parametrize(
    "name",
    [
        "731.added.md",
        "731.changed.md",
        "731.fixed.md",
        "731.fixed.1.md",
        "731.fixed.12.md",
    ],
)
def test_good_entry_names_pass(tmp_path: Path, name: str) -> None:
    root = _dir(tmp_path, {name: "- **Title** (#731): why — closes #1\n"})
    assert script.fragment_problems(root) == []


def test_a_release_fragment_takes_free_text(tmp_path: Path) -> None:
    text = "**Security release:** fixes X.\n\nUpgrading:\n\n- step one\n- step two\n"
    assert script.fragment_problems(_dir(tmp_path, {"+security.release.md": text})) == []


@pytest.mark.parametrize(
    "name",
    [
        "731.fix.md",
        "731.fixed.txt",
        "731.fixed",
        "pr731.fixed.md",
        "0731.fixed.md",
        "731.fixed.0.md",
        "+Security.release.md",
        "+notes.fixed.md",
        "notes.md",
        "731.Fixed.md",
    ],
)
def test_a_misnamed_file_is_refused(tmp_path: Path, name: str) -> None:
    """towncrier build would leave it out of the notes, silently."""
    found = script.fragment_problems(_dir(tmp_path, {name: "- **x** (#731): y\n"}))
    assert len(found) == 1 and "isn't a fragment name" in found[0], found


def test_a_folder_is_refused(tmp_path: Path) -> None:
    root = _dir(tmp_path, {})
    (root / "changelog.d" / "731.fixed.md").mkdir()
    assert "isn't a fragment name" in script.fragment_problems(root)[0]


@pytest.mark.parametrize(
    ("text", "says"),
    [
        ("", "empty"),
        ("\n  \n", "empty"),
        ("**Title** (#731): no bullet\n", "starts with `- `"),
        ("### Fixed\n\n- **x** (#731): y\n", "heading"),
        ("- **x** (#731): y\n- **z** (#731): w\n", "more than one entry"),
        ("- **x** (#731): y\n* **z** (#731): w\n", "more than one entry"),
        ("- **x** (#731): y\n+ **z** (#731): w\n", "more than one entry"),
        ("- **x** (#732): the wrong PR\n", "isn't #731"),
        ("- **x**: no number at all\n", "isn't #731"),
    ],
)
def test_bad_content_is_refused(tmp_path: Path, text: str, says: str) -> None:
    found = script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text}))
    assert len(found) == 1 and says in found[0], found


def test_indented_lines_are_neither_headings_nor_entries(tmp_path: Path) -> None:
    text = (
        "- **x** (#731): y\n\n      # not a heading: indented code inside the entry\n"
        "        ## nor this\n  - not an entry\n"
    )
    assert script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text})) == []


@pytest.mark.parametrize(
    "line",
    [
        "##\tTab heading",
        "##",
        "#",
        "   ## Three spaces",
        " # One space",
        "###### Six",
        # QA r2 on #732: inside the entry (content at column 2), GitHub renders these too.
        "    ## Four spaces",
        "     ## Five spaces",
        "\t## Tab",
        "\n    ## Four after a blank line",
        "  more\n  ===",
        "  more\n  ---",
        "  - ## In a nested item",
        "  > ## In a block quote",
        # Pre-QA r7 on #732: GitHub renders an ordered item's heading too.
        "  1. # Ordered heading",
        "  1) # Ordered heading",
    ],
)
def test_every_commonmark_heading_is_refused(tmp_path: Path, line: str) -> None:
    """Pre-QA r6 and QA r2 on #732: GitHub renders these as headings inside the release notes."""
    found = script.fragment_problems(
        _dir(tmp_path, {"731.fixed.md": f"- **x** (#731): y\n{line}\n"})
    )
    assert len(found) == 1 and "reads as a heading" in found[0], found


def test_a_heading_refusal_names_its_line(tmp_path: Path) -> None:
    """QA r2 on #732: a tab or a bare # doesn't look like a heading in an editor."""
    found = script.fragment_problems(
        _dir(tmp_path, {"731.fixed.md": "- **x** (#731): y\n  more\n##\tTab\n"})
    )
    assert found == [
        "changelog.d/731.fixed.md: line 3, '##\\tTab', reads as a heading; the notes add the "
        "category headings (see changelog.d/README.md)."
    ], found


def test_a_refusal_counts_blank_lines_before_the_entry(tmp_path: Path) -> None:
    """Pre-QA r7 on #732: the number is the file's own line, leading blank lines included."""
    found = script.fragment_problems(
        _dir(tmp_path, {"731.fixed.md": "\n\n- **x** (#731): y\n## h\n"})
    )
    assert len(found) == 1 and "line 4," in found[0], found


@pytest.mark.parametrize(
    "line",
    [
        "```",
        "~~~",
        "> quoted",
        "1. ordered",
        "1) ordered",
        "***",
        "_ _ _",
        # QA r3 on #732: a rule at column 0, not a heading (it splits the category's list).
        "---",
        " ---",
        # A table after a blank line is a block of its own (GitHub's Markdown API agrees).
        "\n| a | b |\n|---|---|",
    ],
)
def test_a_block_opened_outside_the_entry_is_refused(tmp_path: Path, line: str) -> None:
    """Pre-QA r7 on #732: at column 0 or 1 a line isn't inside the list item, so a fence turned
    every later entry and category into one code block on GitHub, and a quote, an ordered item or
    a rule split the list."""
    found = script.fragment_problems(
        _dir(tmp_path, {"731.fixed.md": f"- **x** (#731): y\n{line}\n"})
    )
    assert len(found) == 1 and "starts a block outside the entry" in found[0], found
    assert "heading" not in found[0], found


@pytest.mark.parametrize(
    ("text", "line"),
    [
        # At a line's start an HTML block ends the entry (QA r3 on #732).
        ("<pre>", 2),
        ("<DIV>", 2),
        (" <script>", 2),
        ("<details>\nmore\n</details>", 2),
        ("<table><tr><td>a</td></tr></table>", 2),
        ("<p>para</p>", 2),
        ("<section>", 2),
        ("</p>", 2),
        ("<?php x ?>", 2),
        ("<!DOCTYPE html>", 2),
        ("<![CDATA[x]]>", 2),
        ("<custom-tag>", 2),
        (" <custom-tag>", 2),
        ("<span>", 2),
        ("</span>", 2),
        ("<br>", 2),
        ("<a href=x>", 2),
        ("\n<custom-tag>", 3),
        # Inside the entry GitHub renders the tags: a heading, or a list closed early (QA r4).
        ("  <h2>Block</h2>", 2),
        ("  <pre>x</pre>", 2),
        ("  <details>\n  more\n  </details>", 2),
        ("<b>bold</b>", 2),
        ("<custom-tag> trailing text", 2),
        ("text <kbd>Ctrl</kbd>", 2),
    ],
)
def test_raw_html_is_refused(tmp_path: Path, text: str, line: int) -> None:
    found = script.fragment_problems(
        _dir(tmp_path, {"731.fixed.md": f"- **x** (#731): y\n{text}\n"})
    )
    assert len(found) == 1 and f"line {line}," in found[0], found
    assert "holds raw HTML" in found[0], found


@pytest.mark.parametrize(
    "first",
    [
        "- **T** (#731): x <h2>Big</h2>",
        "- **T** (#731): x </li></ul><h3>Fixed</h3><ul><li>",
        "- **T** (#731): see the `<name>` field and <name>",
    ],
)
def test_raw_html_on_the_entrys_own_line_is_refused(tmp_path: Path, first: str) -> None:
    """QA r4 on #732: GitHub rendered an `<h2>` in the entry, or a second `### Fixed`."""
    found = script.fragment_problems(_dir(tmp_path, {"731.fixed.md": f"{first}\n"}))
    assert len(found) == 1 and "line 1," in found[0] and "holds raw HTML" in found[0], found


@pytest.mark.parametrize(
    "text",
    [
        "- **x** (#731): the `<name>` placeholder and `</li>` in code\n",
        "- **x** (#731): a < b and c > d, <3\n",
        "- **x** (#731): an autolink <https://example.com>\n",
    ],
)
def test_what_isnt_raw_html_passes(tmp_path: Path, text: str) -> None:
    assert script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text})) == []


@pytest.mark.parametrize(
    ("name", "text", "line"),
    [
        # At column 0 GitHub writes nothing for it where it stands, and it ends the list there.
        ("731.fixed.md", "- **x** (#731): y\n[^1]: a note\n", "line 2, '[^1]: a note',"),
        ("731.fixed.md", "- **x** (#731): y[^1]\n[^1]: a note\n", "line 2, '[^1]: a note',"),
        # Indented, the lists stay whole, but GitHub moves the note to the end of CHANGELOG.md
        # (pre-QA r8 on #732: told to indent what was already indented).
        ("731.fixed.md", "- **x** (#731): y[^1]\n\n  [^1]: a note\n", "line 3, '  [^1]: a note',"),
        ("+x.release.md", "Text[^1].\n\n[^1]: a note\n", "line 3, '[^1]: a note',"),
    ],
)
def test_a_footnote_is_refused(tmp_path: Path, name: str, text: str, line: str) -> None:
    found = script.fragment_problems(_dir(tmp_path, {name: text}))
    assert len(found) == 1 and f"{line} defines a footnote" in found[0], found


@pytest.mark.parametrize(
    "line",
    [
        "  ```",
        "  > quoted",
        "  1. ordered",
        "wrapped text",
        "#hashtag",
        "  - nested\n  - items",
        # GFM tables, inside the entry: a lazy one and an indented one render inside it.
        "| a | b |\n|---|---|",
        "\n  | a | b |\n  |---|---|",
        # A fence inside the entry closes with the entry, never past it.
        "  ```\n  never closed",
    ],
)
def test_lines_inside_the_entry_may_open_blocks(tmp_path: Path, line: str) -> None:
    found = script.fragment_problems(
        _dir(tmp_path, {"731.fixed.md": f"- **x** (#731): y\n{line}\n"})
    )
    assert found == [], found


@pytest.mark.parametrize(
    ("body", "closed"),
    [
        ("```\nuv tool upgrade mcp-synology\n```", True),
        ("```\nuv tool upgrade x", False),
        # QA r3 on #732: a fence closes only on its own character, at least as long. Counting
        # fence lines passed the first two and refused the third.
        ("```\na\n~~~", False),
        ("````\na\n```", False),
        ("```\n~~~\n```", True),
        ("~~~~\n```\n~~~~~", True),
    ],
)
def test_a_release_text_may_hold_a_closed_fence_only(
    tmp_path: Path, body: str, closed: bool
) -> None:
    text = f"**Upgrading:**\n\n{body}\n"
    found = script.fragment_problems(_dir(tmp_path, {"+upgrade.release.md": text}))
    if closed:
        assert found == [], found
    else:
        assert len(found) == 1 and "doesn't close" in found[0], found


@pytest.mark.parametrize("line", [" - second", "-\tsecond", " * second", "+ second"])
def test_a_second_entry_at_column_1_or_after_a_tab_is_refused(tmp_path: Path, line: str) -> None:
    """QA r3 on #732: each renders as another item of the category's list."""
    found = script.fragment_problems(
        _dir(tmp_path, {"731.fixed.md": f"- **x** (#731): y\n{line}\n"})
    )
    assert len(found) == 1 and "more than one entry (line 2," in found[0], found


@pytest.mark.parametrize("underline", ["  ---", "  ===", "  -", "  ="])
def test_a_setext_heading_names_its_underline(tmp_path: Path, underline: str) -> None:
    """The line that makes it a heading is the underline, not the text above it."""
    found = script.fragment_problems(
        _dir(tmp_path, {"731.fixed.md": f"- **x** (#731): y\n  more\n{underline}\n"})
    )
    assert len(found) == 1 and f"line 3, {underline!r}, reads as a heading" in found[0], found


@pytest.mark.parametrize(
    "text",
    [
        "**Security release:** x.\n\nUpgrading:\n\n- a\n- b\n",
        "Text.\n\n---\n\nMore text, a rule before it.\n",
        "> A quote.\n",
        "| a | b |\n|---|---|\n| 1 | 2 |\n",
    ],
)
def test_a_release_text_may_hold_blocks(tmp_path: Path, text: str) -> None:
    assert script.fragment_problems(_dir(tmp_path, {"+x.release.md": text})) == []


@pytest.mark.parametrize(
    ("text", "says"),
    [
        ("<details>\nx\n</details>\n", "holds raw HTML"),
        ("Text <b>x</b>.\n", "holds raw HTML"),
        ("Text.\n\n  ## Indented heading\n", "heading"),
    ],
)
def test_a_release_text_may_not_hold_html_or_headings(tmp_path: Path, text: str, says: str) -> None:
    found = script.fragment_problems(_dir(tmp_path, {"+x.release.md": text}))
    assert len(found) == 1 and says in found[0], found


def test_a_byte_order_mark_is_named(tmp_path: Path) -> None:
    """Pre-QA r7 on #732: Notepad's BOM used to read as "an entry starts with `- `"."""
    found = script.fragment_problems(_dir(tmp_path, {"731.fixed.md": "\ufeff- **x** (#731): y\n"}))
    assert len(found) == 1 and "byte-order mark" in found[0], found


def test_a_heading_as_the_entry_itself_is_refused(tmp_path: Path) -> None:
    found = script.fragment_problems(_dir(tmp_path, {"731.fixed.md": "- ## x (#731): y\n"}))
    assert len(found) == 1 and "line 1" in found[0], found


@pytest.mark.parametrize(
    "line",
    [
        "####### Seven",
        "#hashtag",
        "      ## Six spaces is code",
        "\t  ## Tab and two",
        # Pre-QA r7 on #732: a YAML list item with a comment, inside indented code.
        "      modules:\n        - # filestation",
    ],
)
def test_what_commonmark_doesnt_read_as_a_heading_passes(tmp_path: Path, line: str) -> None:
    found = script.fragment_problems(
        _dir(tmp_path, {"731.fixed.md": f"- **x** (#731): y\n{line}\n"})
    )
    assert found == [], found


def test_a_ds_store_is_not_a_fragment(tmp_path: Path) -> None:
    """Pre-QA r6 on #732: macOS Finder writes one; towncrier refused it and so did the check."""
    root = _dir(tmp_path, {})
    (root / "changelog.d" / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    assert script.fragment_problems(root) == []
    assert script._fragments_left(root) == []


def test_git_output_is_read_as_utf8_whatever_the_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-QA r6 on #732: decoding by the locale crashed on CHANGELOG.md's non-ASCII bytes
    under a C or cp1252 locale."""
    seen: list[dict[str, object]] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(script.subprocess, "run", fake_run)
    script._git_show(Path("."), "HEAD", "CHANGELOG.md")
    script.branch_problem(Path("."), "main", 731, [])
    assert len(seen) == 2
    for kwargs in seen:
        assert kwargs.get("encoding") == "utf-8" and kwargs.get("errors") == "replace", kwargs


@pytest.mark.parametrize(
    "text",
    [
        # publish.yml's awk stops at any line starting "## ": a fence doesn't hide it.
        "- **x** (#731): y\n\n  ```\n## 0.0.2 (fake)\n  ```\n",
        # A line with inline backticks used to flip the fence state for the lines after it.
        "- **x** (#731): y ```a``` b\n## 0.0.1 (fake)\n",
    ],
)
def test_a_column_zero_heading_in_a_fence_is_refused(tmp_path: Path, text: str) -> None:
    """Pre-QA r5 on #731: the release body was cut short at the fake heading."""
    found = script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text}))
    assert len(found) == 1 and "heading" in found[0], found


def test_a_fragment_that_is_a_link_is_refused(tmp_path: Path) -> None:
    """Pre-QA r5 on #731: towncrier copies the link's target into the notes."""
    root = _dir(tmp_path, {"731.fixed.md": "- **x** (#731): y\n"})
    (tmp_path / "secret").write_text("token\n", encoding="utf-8")
    (root / "changelog.d" / "+notes.release.md").symlink_to(tmp_path / "secret")
    found = script.fragment_problems(root)
    assert len(found) == 1 and "+notes.release.md is a symbolic link" in found[0], found


@pytest.mark.parametrize(
    "text",
    [
        "- **x** (#731): y <!-- towncrier release notes start -->\n",
        "- **x** (#731): y\n\n<!-- towncrier release notes start -->\n",
        "- **x** (#731): y <!--\n",
    ],
)
def test_a_comment_is_refused(tmp_path: Path, text: str) -> None:
    for name in ("731.fixed.md", "+x.release.md"):
        found = script.fragment_problems(_dir(tmp_path / name, {name: text}))
        assert len(found) == 1 and "`<!--`" in found[0], (name, found)


def test_only_a_release_fragment_says_security_release(tmp_path: Path) -> None:
    """Pre-QA r5 on #731: the update check announces any release whose notes hold it."""
    from mcp_synology.cli import version

    assert script.SECURITY_MARKER == version._SECURITY_MARKER
    entry = f"- **Tidy logs** (#731): quieter. {script.SECURITY_MARKER} upgrade now.\n"
    found = script.fragment_problems(_dir(tmp_path / "a", {"731.fixed.md": entry}))
    assert len(found) == 1 and "only a release's own text" in found[0], found
    release = f"{script.SECURITY_MARKER} fixes a path check; every user should upgrade.\n"
    assert script.fragment_problems(_dir(tmp_path / "b", {"+security.release.md": release})) == []


def test_a_release_fragment_with_a_heading_is_refused(tmp_path: Path) -> None:
    found = script.fragment_problems(_dir(tmp_path, {"+x.release.md": "## 9.9.9\n"}))
    assert "heading" in found[0]


def test_a_missing_directory_is_refused(tmp_path: Path) -> None:
    assert "missing" in script.fragment_problems(tmp_path)[0]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo with changelog.d on main and a feature branch checked out."""
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    root = _dir(tmp_path, {"700.fixed.md": "- **old** (#700): y\n"})
    _git(root, "init", "-q", "-b", "main")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    return root


def _commit(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "change")


def test_a_branch_that_adds_a_fragment_passes(branch: Path) -> None:
    (branch / "changelog.d" / "731.fixed.md").write_text("- **x** (#731): y\n", encoding="utf-8")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is None


def test_a_branch_that_removes_fragments_passes(branch: Path) -> None:
    """A release PR's towncrier build deletes them, bumping the version and adding its section."""
    (branch / "changelog.d" / "700.fixed.md").unlink()
    _release(branch, "1.1.0")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is None


def test_removing_the_last_fragment_without_a_release_is_refused(branch: Path) -> None:
    """Pre-QA r3 on #731: "none left" alone let a PR delete another PR's last entry."""
    (branch / "changelog.d" / "700.fixed.md").unlink()
    _commit(branch)
    problem = script.branch_problem(branch, "main", 731, [])
    assert problem is not None and "only a release PR does" in problem and "restore them" in problem
    assert "'no changelog' label" in problem


def test_a_release_that_leaves_fragments_is_told_which(branch: Path) -> None:
    (branch / "changelog.d" / "701.fixed.md").write_text("- **b** (#701): y\n", encoding="utf-8")
    _commit(branch)
    _git(branch, "branch", "-f", "base")
    (branch / "changelog.d" / "700.fixed.md").unlink()
    _release(branch, "1.1.0")
    _commit(branch)
    problem = script.branch_problem(branch, "base", 731, [])
    assert problem is not None and "release PR (1.1.0) leaves 1" in problem, problem
    # towncrier won't rebuild a version already in CHANGELOG.md (pre-QA r4 on #731): by hand.
    assert "701.fixed.md" in problem and "by hand" in problem
    assert "run the build" not in problem


def test_a_branch_that_only_edits_the_changelog_is_refused(branch: Path) -> None:
    """towncrier check passes this; the entry belongs in a fragment."""
    (branch / "CHANGELOG.md").write_text("- **x** (#731): y\n", encoding="utf-8")
    _commit(branch)
    problem = script.branch_problem(branch, "main", 731, [])
    assert problem is not None and "adds no CHANGELOG fragment" in problem


def test_the_readme_or_template_alone_isnt_a_fragment(branch: Path) -> None:
    (branch / "changelog.d" / "README.md").write_text("more\n", encoding="utf-8")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is not None


def test_the_skip_label_passes(branch: Path) -> None:
    (branch / "x.txt").write_text("x\n", encoding="utf-8")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, ["bug", "no changelog"]) is None


def test_an_unknown_base_is_refused_not_passed(branch: Path) -> None:
    problem = script.branch_problem(branch, "no-such-ref", 731, [])
    assert problem is not None and "couldn't list" in problem


def test_main_reports_every_problem(branch: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (branch / "changelog.d" / "bad.md").write_text("x\n", encoding="utf-8")
    _commit(branch)
    assert script.main(["check", "--base", "main", "--pr", "731", "--labels", ""], branch) == 1
    out = capsys.readouterr().out
    # bad.md is misnamed, and isn't the PR's own fragment either.
    assert out.count("::error::") == 2
    ok = branch.parent / "ok"
    ok.mkdir()
    assert script.main(["check"], _dir(ok, {})) == 0


def test_another_prs_fragment_doesnt_count(branch: Path) -> None:
    """Pre-QA r1 on #731: rewriting 700.fixed.md would lose #700's entry."""
    (branch / "changelog.d" / "700.fixed.md").write_text("- **new** (#700): y\n", encoding="utf-8")
    _commit(branch)
    problem = script.branch_problem(branch, "main", 731, [])
    assert problem is not None and "of its own" in problem


def test_a_fragment_named_for_an_issue_doesnt_count(branch: Path) -> None:
    """The notes would cite #730, the issue, not the PR."""
    (branch / "changelog.d" / "730.fixed.md").write_text("- **x** (#730): y\n", encoding="utf-8")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is not None


def test_a_second_entry_of_its_own_counts(branch: Path) -> None:
    (branch / "changelog.d" / "731.added.1.md").write_text("- **x** (#731): y\n", encoding="utf-8")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is None


def test_a_release_fragment_isnt_a_prs_own(branch: Path) -> None:
    """Pre-QA r2 on #731: counting +<name>.release.md let any PR pass by adding one; a release
    PR's build removes it with the rest, so it passes by removal."""
    (branch / "changelog.d" / "+notes.release.md").write_text(
        "- **my feature** (#12): x\n", encoding="utf-8"
    )
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is not None


@pytest.mark.parametrize("labels", [[], ["no changelog"]])
def test_only_a_release_pr_adds_a_releases_own_text(branch: Path, labels: list[str]) -> None:
    """QA r4 on #732: any PR could add `**Security release:**`, which every running server
    announces once released; the label doesn't excuse it."""
    (branch / "changelog.d" / "731.fixed.md").write_text("- **x** (#731): y\n", encoding="utf-8")
    (branch / "changelog.d" / "+x.release.md").write_text(
        "**Security release:** fake, upgrade now.\n", encoding="utf-8"
    )
    _commit(branch)
    problem = script.branch_problem(branch, "main", 731, labels)
    assert problem is not None and "+x.release.md" in problem, problem
    assert "only a release PR" in problem, problem


def test_a_release_pr_may_add_its_own_text(branch: Path) -> None:
    """Built in the same PR, it's gone; added before the build, a release PR may hold it."""
    (branch / "changelog.d" / "700.fixed.md").unlink()
    (branch / "changelog.d" / "+x.release.md").write_text("Text.\n", encoding="utf-8")
    _release(branch, "1.1.0")
    _commit(branch)
    problem = script.branch_problem(branch, "main", 731, [])
    # Past the release-text rule; it's then told the text is left unbuilt, as any fragment is.
    assert problem is not None and "only a release PR" not in problem, problem
    assert "leaves 1 CHANGELOG fragment(s) (+x.release.md)" in problem, problem


def test_changing_a_releases_own_text_is_refused_too(branch: Path) -> None:
    (branch / "changelog.d" / "+x.release.md").write_text("Text.\n", encoding="utf-8")
    _commit(branch)
    _git(branch, "branch", "-f", "base")
    (branch / "changelog.d" / "+x.release.md").write_text("Other text.\n", encoding="utf-8")
    (branch / "changelog.d" / "731.fixed.md").write_text("- **x** (#731): y\n", encoding="utf-8")
    _commit(branch)
    problem = script.branch_problem(branch, "base", 731, [])
    assert problem is not None and "only a release PR" in problem, problem


def test_removing_some_fragments_isnt_a_release(branch: Path) -> None:
    """Without a version bump, removing fragments is refused and the advice is to restore them,
    not to run the build (which would write a bogus release)."""
    (branch / "changelog.d" / "701.fixed.md").write_text("- **b** (#701): y\n", encoding="utf-8")
    _commit(branch)
    _git(branch, "branch", "-f", "base")
    (branch / "changelog.d" / "700.fixed.md").unlink()
    _commit(branch)
    problem = script.branch_problem(branch, "base", 731, [])
    assert problem is not None and "restore them" in problem, problem
    assert "run the build" not in problem


def test_another_prs_fragment_renamed_to_ours_is_refused(branch: Path) -> None:
    """The renamed file still cites #700, so the content check refuses it."""
    (branch / "changelog.d" / "700.fixed.md").rename(branch / "changelog.d" / "731.fixed.md")
    _commit(branch)
    found = script.fragment_problems(branch)
    assert any("isn't #731" in problem for problem in found), found


def test_base_needs_pr(branch: Path) -> None:
    with pytest.raises(SystemExit):
        script.main(["check", "--base", "main"], branch)


def test_the_three_lists_of_non_fragments_agree() -> None:
    """Pre-QA r1 on #731: towncrier's ignore list and the two scripts' copies drift apart only
    at release time, when the gate would refuse the tag."""
    import tomllib

    _gate_spec = importlib.util.spec_from_file_location(
        "check_release_for_lists", ROOT / "scripts" / "check_release.py"
    )
    assert _gate_spec and _gate_spec.loader
    gate = importlib.util.module_from_spec(_gate_spec)
    _gate_spec.loader.exec_module(gate)
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ignored = frozenset(pyproject["tool"]["towncrier"]["ignore"])
    assert ignored == script.NOT_FRAGMENTS == gate._NOT_FRAGMENTS


def test_moving_its_own_entry_to_another_category_counts(branch: Path) -> None:
    """--no-renames: git would call 731.added.md -> 731.fixed.md a rename, which matches
    nothing; as a removal plus an addition, the addition is the PR's own fragment."""
    _git(branch, "branch", "-f", "base")
    (branch / "changelog.d" / "731.added.md").write_text("- **x** (#731): y\n", encoding="utf-8")
    _commit(branch)
    _git(branch, "branch", "-f", "base")
    (branch / "changelog.d" / "731.added.md").rename(branch / "changelog.d" / "731.fixed.md")
    _commit(branch)
    assert script.branch_problem(branch, "base", 731, []) is None


_HEAD = "# Changelog\n\n## Unreleased\n\n<!-- towncrier release notes start -->\n\n"


@pytest.mark.parametrize(
    ("text", "says"),
    [
        (_HEAD + "## 1.0.0 (2026-01-01)\n\n- x\n", None),
        (
            "# Changelog\n\n## Unreleased\n\n## 1.0.0 (2026-01-01)\n",
            "0 of towncrier's start marker",
        ),
        (_HEAD + _HEAD, "2 of towncrier's start marker"),
        (
            _HEAD.replace("\n\n<!--", "\n\n### Fixed\n\n- **x** (#1): y\n\n<!--")
            + "## 1.0.0 (x)\n",
            "besides towncrier's marker",
        ),
        (_HEAD + "- **stray** (#1): y\n\n## 1.0.0 (x)\n", "besides towncrier's marker"),
        (
            "# Changelog\n\n## Unreleased\n\n"
            "<!-- towncrier release notes start -->  \n\n## 1.0.0 (x)\n",
            "0 of towncrier's start marker",
        ),
        (
            "# Changelog\n\n"
            "<!-- towncrier release notes start -->\n\n## Unreleased\n\n## 1.0.0 (x)\n",
            "isn't under '## Unreleased'",
        ),
        (
            "# Changelog\n\n## Unreleased\n\n## 1.0.0 (x)\n\n### Added\n\n"
            "<!-- towncrier release notes start -->\n- y\n",
            "isn't under '## Unreleased'",
        ),
        (
            _HEAD + "## 1.0.0 (x)\n\n## Unreleased\n\n- **stray** (#1): y\n",
            "2 '## Unreleased' headings",
        ),
        (
            "# Changelog\n\n<!-- towncrier release notes start -->\n\n## 1.0.0 (x)\n",
            "0 '## Unreleased'",
        ),
    ],
)
def test_the_changelogs_layout_is_checked(tmp_path: Path, text: str, says: str | None) -> None:
    """Pre-QA r2 on #731: without the marker the build writes above '# Changelog', and an entry
    under Unreleased would reach the tag gate only after the release PR merged."""
    (tmp_path / "CHANGELOG.md").write_text(text, encoding="utf-8")
    _pyproject(tmp_path, "1.0.0")
    found = script.changelog_problems(tmp_path)
    if says is None:
        assert found == []
    else:
        assert any(says in problem for problem in found), found


def test_the_repos_own_changelog_is_laid_out_right() -> None:
    assert script.changelog_problems(ROOT) == []


def test_check_runs_the_layout_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _dir(tmp_path, {})
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n- **x** (#1): y\n", encoding="utf-8"
    )
    assert script.main(["check"], root) == 1
    assert "start marker" in capsys.readouterr().out


def test_the_towncrier_pins_agree() -> None:
    """Pre-QA r3 on #731: CI's draft and the release build must run the same towncrier."""
    import re as _re

    pins = set()
    for path in (
        ROOT / ".github" / "workflows" / "ci.yml",
        ROOT / "CLAUDE.md",
        ROOT / "pyproject.toml",
    ):
        pins |= set(
            _re.findall(r"towncrier==([0-9][0-9.]*[0-9])", path.read_text(encoding="utf-8"))
        )
    assert len(pins) == 1, pins
    for path in (ROOT / ".github" / "workflows" / "ci.yml", ROOT / "CLAUDE.md"):
        assert "towncrier==" in path.read_text(encoding="utf-8"), path


@pytest.mark.parametrize(
    ("text", "line", "says"),
    [
        # QA r4 on #732: after a nested list marker, across two lines, with an escaped bracket.
        ("- **x** (#731): y\n\n  - [baz]: https://example.com\n", 3, "link reference"),
        ("- **x** (#731): y\n\n  * [baz]: https://example.com\n", 3, "link reference"),
        ("- **x** (#731): y\n\n  1. [baz]: https://example.com\n", 3, "link reference"),
        ("- **x** (#731): y\n\n  1) [baz]: https://example.com\n", 3, "link reference"),
        ("- **x** (#731): y\n\n  [foo\n  bar]: https://example.com\n", 3, "link reference"),
        ("- **x** (#731): y\n\n  [a\\]b]: https://example.com\n", 3, "link reference"),
        # A footnote the fragment doesn't reference itself, for another entry's [^1].
        ("- **x** (#731): y\n\n  [^1]: note\n", 3, "footnote"),
        ("- **x** (#731): y\n\n  - [^1]: note\n", 3, "footnote"),
    ],
)
def test_every_kind_of_definition_is_refused(
    tmp_path: Path, text: str, line: int, says: str
) -> None:
    found = script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text}))
    assert len(found) == 1 and f"line {line}," in found[0], found
    assert f"defines a {says}" in found[0], found


@pytest.mark.parametrize(
    ("name", "text"),
    [
        # Pre-QA r10 on #732: `\\` escaped the line break, so the label was never a candidate;
        # GitHub links another entry's `[a\\ b]` to it.
        ("731.fixed.md", "- **x** (#731): y\n\n  [a\\\n  b]: https://evil.example\n"),
        ("+x.release.md", "Text.\n\n[a\\\nb]: https://evil.example\n"),
    ],
)
def test_a_label_with_an_escaped_line_break_is_refused(
    tmp_path: Path, name: str, text: str
) -> None:
    found = script.fragment_problems(_dir(tmp_path, {name: text}))
    assert len(found) == 1 and "line 3," in found[0] and "defines a link reference" in found[0]


@pytest.mark.parametrize(
    "text",
    [
        # GitHub links an email or a `www.` address by itself: not the fragment's definition.
        "- **X** (#731): mail [a@b.co]: here\n",
        "- **X** (#731): see [www.example.com]: there\n",
    ],
)
def test_text_github_links_by_itself_isnt_a_definition(tmp_path: Path, text: str) -> None:
    assert script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text})) == []


def test_a_run_of_brackets_is_read_in_linear_time(tmp_path: Path) -> None:
    """Pre-QA r10 on #732: a label allowed `[`, so 16,000 of them took 4 s in the regex."""
    import time

    started = time.monotonic()
    script.fragment_problems(
        _dir(tmp_path, {"731.fixed.md": "- **x** (#731): y\n  " + "[" * 16_000 + "\n"})
    )
    assert time.monotonic() - started < 1, "quadratic again?"


def test_a_footnote_refusal_names_its_definition(tmp_path: Path) -> None:
    """Pre-QA r10 on #732: it named the line citing it."""
    text = "- **X** (#731): a\n\n  [^1] cited here\n\n  [^1]: note\n"
    found = script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text}))
    assert len(found) == 1 and "line 5, '  [^1]: note', defines a footnote" in found[0], found


@pytest.mark.parametrize(
    ("name", "text", "line"),
    [
        # Pre-QA r10 on #732: a code span or a fence across lines named the wrong line.
        ("731.fixed.md", "- **X** (#731): `a\n  <b>` ok\n  <i>x</i>\n", 3),
        ("+x.release.md", "Text.\n\n```\n<div>\n```\n\nand <span>x</span>\n", 7),
        ("+x.release.md", "Text.\n\n~~~\n<div>\n~~~\n\nand <span>x</span>\n", 7),
    ],
)
def test_a_raw_html_refusal_skips_code(tmp_path: Path, name: str, text: str, line: int) -> None:
    found = script.fragment_problems(_dir(tmp_path, {name: text}))
    assert len(found) == 1 and f"line {line}," in found[0] and "raw HTML" in found[0], found


def test_the_same_text_in_two_categories_passes(tmp_path: Path) -> None:
    """Pre-QA r10 on #732: towncrier merges identical entries within a category only."""
    entry = "- **Same** (#900): y\n"
    assert (
        script.fragment_problems(_dir(tmp_path, {"900.added.md": entry, "900.fixed.md": entry}))
        == []
    )


def test_an_entry_that_renders_empty_is_refused(tmp_path: Path) -> None:
    """QA r4 on #732: `(#731)` was the definition's title, so the number check passed, and
    GitHub rendered an empty item."""
    found = script.fragment_problems(
        _dir(tmp_path, {"731.fixed.md": "- [foo]: http://evil.example (#731)\n"})
    )
    assert found and "renders with no text" in found[0], found


@pytest.mark.parametrize(
    ("text", "line", "code"),
    [
        ("- **T** (#731): x\x0b\n  ---\n", 1, "000B"),
        ("- **T** (#731): x\x0b## y\n", 1, "000B"),
        ("- **T** (#731): x\x0c y\n", 1, "000C"),
        ("- **T** (#731): x\x85 y\n", 1, "0085"),
        ("- **T** (#731): x\u2028y\n", 1, "2028"),
        ("- **T** (#731): x\u2029y\n", 1, "2029"),
        ("- **T** (#731): x\n  y\u200bz\n", 2, "200B"),
        ("- **T** (#731): x\u202ey\u202c\n", 1, "202E"),
        ("- **T** (#731): x\x1b[2K\x1b[1A\n", 1, "001B"),
        ("- **T** (#731): x\x00\n", 1, "0000"),
        ("- **T** (#731): x\xad y\n", 1, "00AD"),
        # Pre-QA r10 on #732: a space other than U+0020, as the hidden-character test refuses.
        ("- **T** (#731): x\xa0y\n", 1, "00A0"),
        ("- **T** (#731): x\u3000y\n", 1, "3000"),
    ],
)
def test_control_and_invisible_characters_are_named(
    tmp_path: Path, text: str, line: int, code: str
) -> None:
    """QA r4 on #732: `splitlines()` broke lines where Markdown doesn't, and nothing refused
    characters a reader can't see or a terminal acts on."""
    found = script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text}))
    assert len(found) == 1 and f"line {line} holds U+{code}" in found[0], found


@pytest.mark.parametrize(
    "text",
    ["- **T** (#731): x\r\n  more\r\n", "- **T** (#731): x\r  more\r", "- **T** (#731): a\tb\n"],
)
def test_line_breaks_and_tabs_pass(tmp_path: Path, text: str) -> None:
    (tmp_path / "x").mkdir()
    root = _dir(tmp_path / "x", {})
    (root / "changelog.d" / "731.fixed.md").write_bytes(text.encode("utf-8"))
    assert script.fragment_problems(root) == []


def test_the_content_check_reads_cr_line_ends_as_line_breaks() -> None:
    """A file read in text mode arrives with `\\n` only; text given another way is normalized
    the same, never refused as holding a control character."""
    assert script._content_problem("- **T** (#731): x\r\n  more\r\n", "731") is None
    assert script._content_problem("- **T** (#731): x\r  more\r", "731") is None


def test_a_fragment_over_the_size_limit_is_refused_before_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA r4 on #732: each render costs memory (cmarkgfm keeps some per call), so a big fragment
    could fill a contributor's machine; it's refused before the parser sees it."""
    rendered: list[str] = []
    monkeypatch.setattr(
        script.cmarkgfm,
        "github_flavored_markdown_to_html",
        lambda text, options: rendered.append(text) or "",
    )
    text = "- **x** (#731): y\n  " + "a" * script._MAX_BYTES + "\n"
    found = script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text}))
    assert len(found) == 1 and "over the 16 KiB" in found[0] and rendered == [], found


def test_many_definition_candidates_render_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA r4 on #732: confirming each candidate line rendered the notes once per line."""
    real = script.cmarkgfm.github_flavored_markdown_to_html
    calls: list[int] = []

    def counting(text: str, options: int) -> str:
        calls.append(1)
        return str(real(text, options=options))

    monkeypatch.setattr(script.cmarkgfm, "github_flavored_markdown_to_html", counting)
    text = "- **x** (#731): y\n  " + " ".join(f"[l{n}]: x" for n in range(200)) + "\n"
    script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text}))
    # The layout, and the definitions with and without the fragment (pre-QA r10 on #732).
    assert len(calls) <= 3, len(calls)


def test_fragments_with_the_same_text_are_named(tmp_path: Path) -> None:
    """QA r4 on #732: towncrier prints identical entries once, and the draft check's count
    mismatch didn't say why."""
    entry = "- **x** (#716): y\n"
    found = script.fragment_problems(
        _dir(tmp_path, {"716.fixed.md": entry, "716.fixed.1.md": entry})
    )
    assert (
        len(found) == 1
        and "716.fixed.1.md, changelog.d/716.fixed.md hold the same text" in found[0]
    ), found


@pytest.mark.parametrize(
    ("name", "text", "line"),
    [
        ("731.fixed.md", "- **x** (#731): y\n\n  [tracking]: https://example.com/x\n", 3),
        ("731.fixed.md", "- **x** (#731): see [a][b]\n\n  [b]: https://example.com\n", 3),
        ("+x.release.md", "Text.\n\n[t]: https://example.com\n", 3),
    ],
)
def test_a_link_reference_definition_is_refused(
    tmp_path: Path, name: str, text: str, line: int
) -> None:
    """Pre-QA r9 on #732: it applies to the whole of CHANGELOG.md, so `[tracking]` in another
    PR's entry, or an older release, became a link to its URL."""
    found = script.fragment_problems(_dir(tmp_path, {name: text}))
    assert len(found) == 1 and f"line {line}," in found[0], found
    assert "defines a link reference" in found[0], found


@pytest.mark.parametrize(
    "text",
    [
        # Right after the entry's text, it's text, not a definition (GitHub agrees).
        "- **x** (#731): y\n  [not]: a definition\n",
        "- **x** (#731): y `[a]: b`\n",
        "- **x** (#731): an [inline](https://example.com) link\n",
    ],
)
def test_what_isnt_a_link_reference_definition_passes(tmp_path: Path, text: str) -> None:
    assert script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text})) == []


def test_a_long_run_of_dashes_is_read_in_linear_time(tmp_path: Path) -> None:
    """Pre-QA r9 on #732: the underline regex took quadratic time on a long run of dashes (20,000
    took 2.7 s; a megabyte, about 2 h of CI)."""
    import time

    started = time.monotonic()
    assert script._is_underline("-" * 1_000_000 + "x") is False
    assert time.monotonic() - started < 1, "quadratic again?"
    # As big as a fragment may be (QA r4 on #732's size limit), refused for the heading.
    text = "- **x** (#731): y\n  " + "-" * 15_000 + "x\n  ===\n"
    found = script.fragment_problems(_dir(tmp_path, {"731.fixed.md": text}))
    assert len(found) == 1 and "line 3, '  ===', reads as a heading" in found[0], found


@pytest.mark.parametrize(
    ("text", "underline"),
    [
        ("---", True),
        ("  ===  ", True),
        ("  > - 1. ---", True),
        ("-", True),
        ("--x", False),
        ("x---", False),
        ("", False),
        ("  ", False),
        ("== x", False),
    ],
)
def test_what_reads_as_an_underline(text: str, underline: bool) -> None:
    assert script._is_underline(text) is underline


def test_notes_that_dont_start_with_the_versions_heading_are_refused() -> None:
    """The layout's first block is the version's `## ` heading; anything else is a problem."""
    for text, kind in (("### Added\n\n- x\n", "heading"), ("Text.\n\n### Added\n\n- x\n", "split")):
        layout = script._layout(text)
        assert layout.problem is not None and layout.problem[0] == kind, (text, layout)


def test_the_parser_pins_agree() -> None:
    """CI runs the check from the `changelog` dependency group, at uv.lock's versions (pre-QA r8
    on #732: pins copied into the workflow broke on a Dependabot bump of cffi); the tests run it
    from the dev extra, at the same cmarkgfm."""
    import tomllib

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    group = pyproject["dependency-groups"]["changelog"]
    dev = [d for d in pyproject["project"]["optional-dependencies"]["dev"] if "cmarkgfm" in d]
    assert len(group) == 1 and group == dev and group[0].startswith("cmarkgfm=="), (group, dev)
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    runs = re.findall(r"uv run (.*?)python scripts/changelog_fragments\.py", ci)
    assert len(runs) == 3 and set(runs) == {"--locked --only-group changelog "}, runs


def test_towncriers_dependencies_are_pinned_alike() -> None:
    """Pre-QA r8 on #732: uvx took the newest Jinja2 on the day, so CI's draft and the release
    build could differ. Both pin the same set."""
    sets = []
    for path in (ROOT / ".github" / "workflows" / "ci.yml", ROOT / "CLAUDE.md"):
        text = path.read_text(encoding="utf-8")
        found = re.findall(r"uvx --from towncrier==[0-9.]+((?: --with [a-z0-9-]+==[0-9.]+)+)", text)
        assert len(found) == 1, (path, found)
        sets.append(sorted(found[0].split(" --with ")[1:]))
    assert sets[0] == sets[1], sets
    assert {pin.split("==")[0] for pin in sets[0]} == {"click", "jinja2", "markupsafe"}, sets


def _draft(entries: dict[str, list[str]], release: str = "") -> str:
    """A draft as `towncrier build --draft` writes it, preamble included."""
    parts = [
        "Draft only -- nothing has been written.",
        "What is seen below is what would be written.",
        "",
        "## 0.0.0 (2000-01-01)",
        "",
    ]
    if release:
        parts += [release, ""]
    for name, items in entries.items():
        parts += [f"### {name}", "", *items, ""]
    return "\n".join(parts) + "\n"


def test_the_draft_check_passes_notes_that_match(tmp_path: Path) -> None:
    root = _dir(
        tmp_path,
        {
            "731.added.md": "- **a** (#731): a\n",
            "731.fixed.md": "- **b** (#731): b\n",
            "730.fixed.md": "- **c** (#730): c\n",
        },
    )
    draft = _draft(
        {"Added": ["- **a** (#731): a"], "Fixed": ["- **b** (#731): b", "- **c** (#730): c"]},
        release="**Security release:** x.",
    )
    assert script.notes_problems(root, draft) == []


@pytest.mark.parametrize(
    ("entries", "says"),
    [
        # One entry fewer than the fragments: a fragment that took in its neighbor.
        ({"Fixed": ["- **b** (#731): b"]}, "renders {'Fixed': 1} where changelog.d/ holds"),
        (
            {"Fixed": ["- **b** (#731): b", "- **c** (#730): c", "<details>", "x", "</details>"]},
            "holds raw HTML",
        ),
        (
            {"Fixed": ["- **b** (#731): b", "- **c** (#730): c", "", "### Fixed", "", "- d"]},
            "a heading",
        ),
        ({"Fixed": ["- **b** (#731): b", "", "---", "", "- **c** (#730): c"]}, "a block"),
    ],
)
def test_the_draft_check_refuses_notes_that_dont(
    tmp_path: Path, entries: dict[str, list[str]], says: str
) -> None:
    root = _dir(
        tmp_path, {"731.fixed.md": "- **b** (#731): b\n", "730.fixed.md": "- **c** (#730): c\n"}
    )
    found = script.notes_problems(root, _draft(entries))
    assert found and says in " ".join(found), found


def test_the_draft_check_needs_the_versions_heading(tmp_path: Path) -> None:
    assert "no '## <version>'" in script.notes_problems(_dir(tmp_path, {}), "nothing\n")[0]


def test_the_notes_command_reads_the_draft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _dir(tmp_path, {"731.fixed.md": "- **b** (#731): b\n"})
    draft = tmp_path / "notes.md"
    draft.write_text(_draft({"Fixed": ["- **b** (#731): b"]}), encoding="utf-8")
    assert script.main(["notes", str(draft)], root) == 0
    draft.write_text(_draft({"Fixed": []}), encoding="utf-8")
    assert script.main(["notes", str(draft)], root) == 1
    assert "::error::" in capsys.readouterr().out
    assert script.main(["notes", str(tmp_path / "missing.md")], root) == 1
    assert "couldn't read" in capsys.readouterr().out


def test_fragment_only_prs_skip_vdsm() -> None:
    """Pre-QA r3 on #731: every PR adds a fragment, so a docs-only PR must still skip vdsm."""
    vdsm = (ROOT / ".github" / "workflows" / "vdsm.yml").read_text(encoding="utf-8")
    found = re.search(r"grep -qvE '([^']+)'", vdsm)
    assert found is not None
    docs_only = re.compile(found.group(1))
    for path in ("changelog.d/731.fixed.md", "changelog.d/+security.release.md", "README.md"):
        assert docs_only.search(path), path
    for path in ("changelog.d/sub/731.fixed.md", "changelog.d/731.py", "src/x.md", "scripts/a.py"):
        assert not docs_only.search(path), path


def test_a_version_bump_without_its_section_isnt_a_release(branch: Path) -> None:
    (branch / "changelog.d" / "700.fixed.md").unlink()
    _pyproject(branch, "1.1.0")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is not None


def test_a_section_without_a_version_bump_isnt_a_release(branch: Path) -> None:
    """The version stays 1.0.0 (the base's), though the branch adds a '## 1.0.0 (' section."""
    (branch / "changelog.d" / "700.fixed.md").unlink()
    _release(branch, "1.0.0")
    _commit(branch)
    assert script.branch_problem(branch, "main", 731, []) is not None


def test_its_own_fragment_doesnt_excuse_deleting_another(branch: Path) -> None:
    """Pre-QA r4 on #731: the own-fragment pass ran before the removal rule."""
    (branch / "changelog.d" / "700.fixed.md").unlink()
    (branch / "changelog.d" / "731.fixed.md").write_text("- **x** (#731): y\n", encoding="utf-8")
    _commit(branch)
    problem = script.branch_problem(branch, "main", 731, [])
    assert problem is not None and "700.fixed.md" in problem


def test_a_release_section_already_on_the_base_isnt_a_release(branch: Path) -> None:
    """Pre-QA r4 on #731: a step-7 follow-up (the base already holds '## 1.1.0 (') that
    removes a fragment needs the label, not the release pass."""
    _release(branch, "1.1.0")
    (branch / "changelog.d" / "701.fixed.md").write_text("- **b** (#701): y\n", encoding="utf-8")
    _commit(branch)
    _git(branch, "branch", "-f", "base")
    (branch / "changelog.d" / "701.fixed.md").unlink()
    _commit(branch)
    problem = script.branch_problem(branch, "base", 731, [])
    assert problem is not None and "'no changelog' label" in problem
    assert script.branch_problem(branch, "base", 731, ["no changelog"]) is None


def test_long_lists_are_cut_short() -> None:
    names = [f"{n}.fixed.md" for n in range(1, 13)]
    assert script._named(names).endswith("and 2 more"), script._named(names)
    assert "12.fixed.md" not in script._named(names)
    assert "3.fixed.md" in script._named(names[:3])


def test_only_ci_has_a_job_named_changelog() -> None:
    """QA r1 on #732: a required check is matched by name, and another workflow's `changelog`
    job, skipped on every PR it doesn't run for, would satisfy it."""
    # A check run is named for the job's `name:` when it has one, else for its id (pre-QA r6 on
    # #732: a `name: changelog` on another job brought the clash back unseen).
    owners = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        jobs = yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}
        for job_id, job in jobs.items():
            shown = job.get("name", job_id) if isinstance(job, dict) else job_id
            if str(shown).strip().casefold() == "changelog":
                owners.append(f"{path.name}:{job_id}")
    assert owners == ["ci.yml:changelog"], owners
