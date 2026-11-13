# CHANGELOG fragments

Every PR adds its CHANGELOG entry here as a file of its own, never in `CHANGELOG.md` (#731).
The release PR assembles them into the version's notes and deletes them (CLAUDE.md,
"Bumping the version for a release"), so two PRs never edit the same lines.

- **Name:** `<PR number>.<added|changed|fixed>.md`, one file per category. For a PR's second
  entry in the same category, use `<PR number>.<type>.1.md`, then `.2`, and so on. A release's
  free text (a `**Security release:** …` line, an Upgrading block) goes in
  `+<name>.release.md`, printed before the categories.
- **Content:** the entry exactly as it would appear under its `### ` heading in `CHANGELOG.md`:
  `- **Title** (#PR): what changed and why — closes #N`, on one line, with no heading. Its first
  `(#N)` is the file's PR number.
- **Order:** the notes list each category's entries highest PR number first; within one PR,
  `<PR>.<type>.1.md` prints before `<PR>.<type>.md`.
- **Checked by** `python scripts/changelog_fragments.py check` (CI): names, content, and that a
  PR adds or changes a fragment named for its own number, unless it has the `no changelog` label
  (ask the maintainer for it; the check reads the labels when it runs, so rerun it after).
