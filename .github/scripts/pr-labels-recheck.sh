#!/usr/bin/env bash
# The recheck that doesn't depend on an event (#549, #339). A green PR can strand on Awaiting CI
# when GitHub's run or job listings lag behind the last CI event, as nothing checks again after
# that. So: settle each open PR on Awaiting CI or CI Failed, and each same-repository PR with no
# workflow label at all. A PR at Ready for QA keeps its label, which someone may have applied by
# hand: CodeQL can fail after promotion with nothing firing on it (#578), so a failed check gets
# one comment per commit, and no label changes (maintainer, 2026-11-06). If a result is still missing long after its runs finished, comment once on the PR.
#
# Env: REPO, GH_TOKEN. STRANDED_AFTER_MINUTES defaults to 30. Fork PRs with none of those labels are left
# alone: their runs wait for the maintainer's approval, and the label is the maintainer's call.
set -e

HERE=$(dirname "${BASH_SOURCE[0]}")
STRANDED_AFTER_MINUTES=${STRANDED_AFTER_MINUTES:-30}
FAILED=0
export REPO

# number, head commit, 1 to adopt a PR with no workflow label, 2 to only report on one at Ready for
# QA. CI Failed counts as Awaiting CI: a
# failed job re-run green has the same lag. Only PRs into main: the checks don't run on others.
PRS=$(gh pr list --repo "$REPO" --state open --base main --limit 100 \
  --json number,headRefOid,isDraft,isCrossRepository,labels \
  --jq '.[]
    | ([.labels[].name]) as $names
    | (["Awaiting CI","Ready for QA","QA Active","Ready for QA Signoff","QA Approved","QA Failed","CI Failed","Dev Active"]) as $workflow
    | if (($names | index("Awaiting CI")) or ($names | index("CI Failed"))) then [.number, .headRefOid, 0]
      elif ($names | index("Ready for QA")) then [.number, .headRefOid, 2]
      elif (.isDraft | not) and (.isCrossRepository | not) and ([$names[] | select(. as $n | $workflow | index($n))] | length) == 0 then [.number, .headRefOid, 1]
      else empty end
    | @tsv')

# Post one comment per marker, which is a comment's whole first line, so a job name quoted in another
# comment can't pass for it. Only this job's own comments count. A read that fails isn't "no comment yet": that would post one on every run.
comment_once() {
  local pr=$1 marker=$2 body=$3 comments
  if ! comments=$(gh pr view "$pr" --repo "$REPO" --json comments \
    --jq '.comments[] | select(.author.login | test("^github-actions(\\[bot\\])?$")) | .body'); then
    echo "PR #$pr: couldn't read its comments" >&2
    FAILED=1
    return
  fi
  if printf '%s\n' "$comments" | grep -qxF "$marker"; then
    echo "PR #$pr: already commented: $marker" >&2
    return
  fi
  if ! gh pr comment "$pr" --repo "$REPO" --body "$marker
$body" >&2; then
    echo "PR #$pr: couldn't comment" >&2
    FAILED=1
  fi
}

echo "PRs to settle: $(printf '%s' "$PRS" | grep -c . || true)" >&2
DETAILS=$(mktemp)
# On its own descriptor, so a command in the loop that reads stdin can't take the list.
while IFS=$'\t' read -r PR SHA ADOPT <&3; do
  if [ -z "$PR" ]; then continue; fi
  echo "PR #$PR at $SHA (adopt: $ADOPT)" >&2
  : > "$DETAILS"
  if [ "$ADOPT" = 2 ]; then
    if ! STATE=$(HEAD_SHA=$SHA PENDING_DETAILS=$DETAILS bash "$HERE/pr-checks-state.sh"); then
      echo "PR #$PR: couldn't read its checks" >&2
      FAILED=1
    elif [ "$STATE" = fail ]; then
      # The state read takes up to a minute, and a push or QA may have moved the PR on since the list.
      if ! NOW=$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq .headRefOid) \
        || ! LABELS=$(gh pr view "$PR" --repo "$REPO" --json labels --jq '.labels[].name'); then
        echo "PR #$PR: couldn't read it again" >&2
        FAILED=1
      elif [ "$NOW" != "$SHA" ] || ! printf '%s\n' "$LABELS" | grep -qx "Ready for QA"; then
        echo "PR #$PR: moved on while reading its checks" >&2
      else
        # Workflow or check names only: a fork chooses its job names, and this is the bot's comment.
        # The state stops at its first failure, so the names are in the marker: re-running one
        # failure mustn't silence the next, on this same commit.
        NAMES=$(sed -nE 's/^failed ([^:]+):.*/\1/p' "$DETAILS" | sort -u)
        comment_once "$PR" "<!-- pr-labels-recheck-failed $SHA $(printf '%s' "$NAMES" | tr '\n' ' ') -->" "A check has failed on \`$SHA\`:

\`\`\`
$NAMES
\`\`\`

This PR stays at \`Ready for QA\`, in case that label was applied on purpose; if it wasn't, take it off. The PR's Checks tab has the detail. A failed job can be re-run. A failed CodeQL check clears with a push, the one known way."
      fi
    fi
    continue
  fi
  if ! STATE=$(ADOPT=$ADOPT PENDING_DETAILS=$DETAILS bash "$HERE/pr-labels-settle.sh" "$PR" "$SHA"); then
    # One PR's API error mustn't stop the others; the run still ends red for it.
    echo "PR #$PR: couldn't settle it" >&2
    FAILED=1
    continue
  fi
  if [ "$STATE" != pending ]; then continue; fi
  if grep -qx running "$DETAILS"; then
    echo "PR #$PR: a run is still going, or waiting for approval" >&2
    continue
  fi
  MISSING=$(sed -n 's/^missing //p' "$DETAILS")
  # The latest time a finished run changed. With no run listed at all, nothing has been waiting.
  UPDATED=$(sed -n 's/^updated //p' "$DETAILS" | sort | tail -n 1)
  if [ -z "$MISSING" ] || [ -z "$UPDATED" ]; then
    echo "PR #$PR: no run has finished yet" >&2
    continue
  fi
  if ! UPDATED_AT=$(date -u -d "$UPDATED" +%s); then
    echo "PR #$PR: can't read the time $UPDATED" >&2
    FAILED=1
    continue
  fi
  AGE=$(( ($(date -u +%s) - UPDATED_AT) / 60 ))
  if [ "$AGE" -lt "$STRANDED_AFTER_MINUTES" ]; then
    echo "PR #$PR: results missing, $AGE minutes after its runs finished" >&2
    continue
  fi
  CLEAR="A lagging listing usually clears on a later recheck. If it doesn't, re-run the workflow in Actions."
  HAND="If the PR has no QA or \`Merge conflict\` label, you can also apply \`Ready for QA\` by hand"
  STUCK="\`CodeQL\` is GitHub's default setup, not a workflow in this repository, so re-running CI won't clear it. A new commit normally gets a new check."
  if ! printf '%s\n' "$MISSING" | grep -qv '^CodeQL'; then
    # Only CodeQL: it may never pass, so no waiting on gh pr checks.
    CLEAR="$STUCK $HAND."
  elif printf '%s\n' "$MISSING" | grep -q '^CodeQL'; then
    CLEAR="$CLEAR $STUCK $HAND once \`gh pr checks $PR\` shows every check passed except \`CodeQL\` and \`QA Gate\`."
  else
    CLEAR="$CLEAR $HAND once \`gh pr checks $PR\` shows every check passed except \`QA Gate\`."
  fi
  comment_once "$PR" "<!-- pr-labels-recheck $SHA -->" "The label automation has no result for these on \`$SHA\`, $AGE minutes after its runs finished:

\`\`\`
$MISSING
\`\`\`

$CLEAR"
done 3<<< "$PRS"
rm -f "$DETAILS"
exit "$FAILED"
