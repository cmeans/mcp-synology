# CHANGELOG fragments

Every PR adds its CHANGELOG entry here as a file of its own, never in `CHANGELOG.md` (#731).
The release PR assembles them into the version's notes and deletes them (CLAUDE.md,
"Bumping the version for a release"), so two PRs never edit the same lines.

- **Name:** `<PR number>.<added|changed|fixed>.md`, one file per category. For a PR's second
  entry in the same category, use `<PR number>.<type>.1.md`, then `.2`, and so on. A release's
  free text (a `**Security release:** …` line, an Upgrading block) goes in
  `+<name>.release.md`, printed before the categories.
- **Content:** the entry exactly as it would appear under its `### ` heading in `CHANGELOG.md`:
  `- **Title** (#PR): what changed and why — closes #N`, usually on one line, with no heading.
  Indent any later line 2 spaces or more: at column 0 or 1, a fence, `<pre>`, `>`, `1.` or `***`
  would end the entry and split the notes or swallow the rest of them. Save it as UTF-8 without a
  byte-order mark. Its first
  `(#N)` is the file's PR number. No line reads as a heading inside the entry, a list item whose content
  starts at column 2: up to 5 spaces, `#` to `######`, then a space, a tab or the line's end; a
  `===` or `---` underline; or a heading after a nested `-`, `1.` or `>`. That goes for a code
  block too, so indent its lines 6 spaces or more. No `<!--`, and only a `+<name>.release.md` says `**Security release:**`. A fragment is a file
  of its own, never a symbolic link.
- **Order:** the notes list each category's entries highest PR number first; within one PR,
  `<PR>.<type>.1.md` prints before `<PR>.<type>.md`.
- **Checked by** `python scripts/changelog_fragments.py check` (CI): names, content, and that a
  PR adds or changes a fragment named for its own number, unless it has the `no changelog` label
  (ask the maintainer for it; the check reads the labels when it runs, so rerun it after).
  Removing fragments needs the label too (a revert, a backport's carry-back), except in a release
  PR, which bumps the version, adds its section and assembles them all. A `+<name>.release.md`
  alone doesn't count as a PR's own fragment.
- **Changing what hasn't shipped:** a PR that only changes something another PR added since the
  last release edits that PR's fragment, so its entry says what will ship, and adds none of its
  own: it takes the `no changelog` label. The notes describe what changed since the last
  release, not each step on the way.
