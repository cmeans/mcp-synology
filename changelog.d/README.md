# CHANGELOG fragments

Every PR adds its CHANGELOG entry here as a file of its own, never in `CHANGELOG.md` (#731).
The release PR assembles them into the version's notes and deletes them (CLAUDE.md,
"Bumping the version for a release"), so two PRs never edit the same lines.

- **Name:** `<PR number>.<added|changed|fixed>.md`, one file per category. For a PR's second
  entry in the same category, use `<PR number>.<type>.1.md`, then `.2`, and so on. A release's
  free text (a `**Security release:** …` line, an Upgrading block) goes in
  `+<name>.release.md`, printed before the categories; only a release PR adds or changes one.
- **Content:** the entry exactly as it would appear under its `### ` heading in `CHANGELOG.md`:
  `- **Title** (#PR): what changed and why — closes #N`, usually on one line, with no heading.
  Its first `(#N)` is the file's PR number. Save it as UTF-8 without a byte-order mark. At most
  16 KiB, and no two fragments of one category with the same text (towncrier prints them once).
  - **How it renders is checked with cmark-gfm**, the parser GitHub's Markdown is built on: the
    fragment is placed between two other entries of its category, as a release assembles it, and
    must stay one item of that list with text in it, leaving the entries around it and the next
    category as they were. So indent any later line 2 spaces or more: at column 0 or 1, a fence,
    `>`, `1.`, `---`, a table after a blank line, another `-`, `*` or `+` ends the entry and
    splits the notes or swallows the rest of them. A `+<name>.release.md` is checked the same
    way, before the categories: a fence it opens must close.
  - **Neither holds** a heading; raw HTML, anywhere (GitHub renders it, so `<h2>` adds a heading
    and `</li>` closes the list, or drops it: put a placeholder such as `<name>` in backticks);
    a footnote or a link reference definition (`[label]: url`, which would apply to every entry
    and older release: write links inline); a control character other than a tab, or a
    character that doesn't show (zero-width, direction marks, line and paragraph separators, a
    space other than U+0020; CI's `tests/test_hidden_characters.py` refuses a few more).
  - **A line starting with `#`** (`#` to `######`, then a space, a tab or the line's end) is
    refused even inside a code block: the release tools read `CHANGELOG.md` line by line. No
    `<!--`, and only a `+<name>.release.md` says `**Security release:**`. A fragment is a file
    of its own, never a symbolic link.
- **Order:** the notes list each category's entries highest PR number first; within one PR,
  `<PR>.<type>.1.md` prints before `<PR>.<type>.md`.
- **Checked by** `uv run python scripts/changelog_fragments.py check` (after
  `uv sync --extra dev`; CI runs it too): names, content, and that a PR adds or changes a fragment named for its own number, unless it has the `no changelog` label
  (ask the maintainer for it; the check reads the labels when it runs, so rerun it after).
  Removing fragments needs the label too (a revert, a backport's carry-back), except in a release
  PR, which bumps the version, adds its section and assembles them all. A `+<name>.release.md`
  never counts as a PR's own fragment, and outside a release PR it's refused, label or not.
- **Changing what hasn't shipped:** a PR that only changes something another PR added since the
  last release edits that PR's fragment, so its entry says what will ship, and adds none of its
  own: it takes the `no changelog` label. The notes describe what changed since the last
  release, not each step on the way.
