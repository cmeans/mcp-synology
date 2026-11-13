<!--
  Thanks for opening a PR! This template auto-fills the body of new PRs.
  Replace the placeholder text below; remove sections that don't apply.
  Dependabot bypasses this template (it supplies its own body); see
  `.github/workflows/dependabot-changelog.yml` for how Dependabot PRs
  get a CHANGELOG entry and QA section automatically.
-->

## Summary

<!-- Two or three sentences on what changed and why. -->

## Test plan

<!-- Checklist the maintainer can walk to verify the change. -->

- [ ] `uv run pytest tests/modules/<area>/test_<file>.py::test_<name>` — passes
- [ ] `uv run ruff check src/ tests/ scripts/` — clean
- [ ] `uv run ruff format --check src/ tests/ scripts/` — clean
- [ ] `uv run mypy src/ scripts/` — clean
- [ ] Confirm no regression in the affected module

## CHANGELOG

<!--
  Add a fragment, changelog.d/<PR>.<added|changed|fixed>.md, holding the entry
  as it would read in CHANGELOG.md (per CLAUDE.md § "Adding a CHANGELOG entry
  on every PR" and changelog.d/README.md). Never edit CHANGELOG.md itself: the
  release PR assembles the fragments. CI checks the fragment; a PR that needs
  no entry gets the `no changelog` label.
-->

- [ ] Added `changelog.d/<PR>.<added|changed|fixed>.md` (or, for a release PR, assembled the fragments into the version's notes)

Closes #
