#!/usr/bin/env bash
# Settles a PR's labels from its head commit's checks: Ready for QA once CI and vdsm have passed,
# with CodeQL's check if the commit has one, CI Failed once either has a failed job or CodeQL has
# failed, when this next reads them, nothing while a result is missing. Used by pr-labels-ci.yml on each CI or vdsm
# completion, pr-labels.yml when Dev Active comes off, and pr-labels-recheck.yml on a schedule.
#
# Usage: pr-labels-settle.sh <pr> <head sha>. Env: REPO, GH_TOKEN, PENDING_DETAILS (see
# pr-checks-state.sh). ADOPT=1 is for a PR with no workflow label at all: it's labeled as if it
# had Awaiting CI. Prints only the state on stdout, as a caller captures it.
set -e

PR=$1
HEAD_SHA=$2
export HEAD_SHA
HERE=$(dirname "${BASH_SOURCE[0]}")

# The details say what failed, for the CodeQL comment below; a caller may pass its own file.
if [ -z "${PENDING_DETAILS:-}" ]; then
  PENDING_DETAILS=$(mktemp)
  trap 'rm -f "$PENDING_DETAILS"' EXIT
fi
export PENDING_DETAILS
STATE=$(bash "$HERE/pr-checks-state.sh")

# The state took up to 80 s to read: if a newer commit was pushed meanwhile, its own runs decide.
CUR=$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq .headRefOid)
if [ "$CUR" != "$HEAD_SHA" ]; then
  echo "PR #$PR moved to $CUR, leaving the labels to its runs" >&2
  echo moved
  exit 0
fi

LABELS=$(gh pr view "$PR" --repo "$REPO" --json labels --jq '.labels[].name')
has() { echo "$LABELS" | grep -qx "$1"; }

# Dev Active means dev isn't done: don't relabel. on-unlabel in pr-labels.yml takes over.
if has "Dev Active"; then
  echo "Dev Active present, skipping" >&2
  echo dev-active
  exit 0
fi

# The state took a while to read, and a list ADOPT came from can be minutes old, so someone may
# have labeled the PR since. These
# writes trigger nothing that would clean up after them (pre-QA on #549).
for LABEL in "Awaiting CI" "Ready for QA" "QA Active" "Ready for QA Signoff" "QA Approved" \
  "QA Failed" "CI Failed"; do
  if has "$LABEL"; then ADOPT=""; fi
done

case "$STATE" in
  pass)
    # Awaiting CI (normal flow) or CI Failed (a failed job re-run green).
    REMOVE=""
    has "Awaiting CI" && REMOVE="Awaiting CI"
    has "CI Failed" && REMOVE="${REMOVE:+$REMOVE,}CI Failed"
    if [ -n "$REMOVE" ]; then
      gh pr edit "$PR" --repo "$REPO" --remove-label "$REMOVE" >&2
    fi
    if [ -n "$REMOVE" ] || [ "${ADOPT:-}" = 1 ]; then
      # Not while the branch has a merge conflict: resolving it changes the diff
      # (pr-behind-main.yml). The fix is a push, which restarts Awaiting CI. Behind main alone
      # doesn't hold QA up.
      if has "Merge conflict"; then
        echo "Merge conflict present: checks passed, Ready for QA withheld" >&2
      elif has "QA Active" || has "Ready for QA Signoff" || has "QA Approved" || has "QA Failed"; then
        # A stranded PR QA already picked up: it's past Ready for QA, and these writes trigger
        # nothing that would clean up (pre-QA on #549).
        echo "Already past Ready for QA, not adding it" >&2
      else
        gh pr edit "$PR" --repo "$REPO" --add-label "Ready for QA" >&2
      fi
    fi
    ;;
  fail)
    # On a CI or vdsm event, or Dev Active coming off: take Ready for QA back. The recheck only
    # comments on a PR at Ready for QA (pr-labels-recheck.sh), except one that also has Awaiting CI
    # or CI Failed, which it settles here: on a fork, a hand-applied Ready for QA beside one of
    # those goes (maintainer, 2026-11-06). A CodeQL failure says so in a comment (CODEQL_FAILED).
    REMOVE=""
    has "Awaiting CI" && REMOVE="Awaiting CI"
    has "Ready for QA" && REMOVE="${REMOVE:+$REMOVE,}Ready for QA"
    if [ -n "$REMOVE" ]; then
      gh pr edit "$PR" --repo "$REPO" --remove-label "$REMOVE" >&2
    fi
    if [ -n "$REMOVE" ] || [ "${ADOPT:-}" = 1 ]; then
      gh pr edit "$PR" --repo "$REPO" --add-label "CI Failed" >&2
      # The workflows are green when CodeQL alone failed, so say which (QA round 3 on #582): a
      # reader re-running them would change nothing. Once per commit; the step doesn't fail over
      # it (a fork's on-unlabel token is read-only).
      if grep -q '^failed CodeQL:' "$PENDING_DETAILS"; then
        MARKER="<!-- pr-labels-codeql-failed $HEAD_SHA -->"
        if COMMENTS=$(gh pr view "$PR" --repo "$REPO" --json comments \
          --jq '.comments[] | select(.author.login | test("^github-actions(\\[bot\\])?$")) | .body') \
          && ! printf '%s\n' "$COMMENTS" | grep -qxF "$MARKER"; then
          gh pr comment "$PR" --repo "$REPO" --body "$MARKER
\`CI Failed\` on \`$HEAD_SHA\` is from the \`CodeQL\` check: GitHub's default setup, not a workflow here, so re-running CI won't change it. \`gh pr checks $PR\` shows the alert's check; fix what it reports and push (an empty commit won't clear it)." >&2 \
            || echo "PR #$PR: couldn't comment on the CodeQL failure" >&2
        fi
      fi
    fi
    ;;
  pending)
    echo "Not every check has a result yet; leaving the labels" >&2
    if [ "${ADOPT:-}" = 1 ]; then
      gh pr edit "$PR" --repo "$REPO" --add-label "Awaiting CI" >&2
    fi
    ;;
esac
echo "$STATE"
