#!/usr/bin/env bash
# The recheck that doesn't depend on an event (#549, #339). A green PR can strand on Awaiting CI
# when GitHub's run or job listings lag behind the last CI event, as nothing checks again after
# that. So: settle each open PR on Awaiting CI or CI Failed, and each same-repository PR with no
# workflow label at all. If a result is still missing long after its runs finished, comment once on the PR.
#
# Env: REPO, GH_TOKEN. STRANDED_AFTER_MINUTES defaults to 30. Fork PRs with neither of those labels are left
# alone: their runs wait for the maintainer's approval, and the label is the maintainer's call.
set -e

HERE=$(dirname "${BASH_SOURCE[0]}")
STRANDED_AFTER_MINUTES=${STRANDED_AFTER_MINUTES:-30}
FAILED=0
export REPO

# number, head commit, 1 to adopt a PR with no workflow label. CI Failed counts as Awaiting CI: a
# failed job re-run green has the same lag. Only PRs into main: the checks don't run on others.
PRS=$(gh pr list --repo "$REPO" --state open --base main --limit 100 \
  --json number,headRefOid,isDraft,isCrossRepository,labels \
  --jq '.[]
    | ([.labels[].name]) as $names
    | (["Awaiting CI","Ready for QA","QA Active","Ready for QA Signoff","QA Approved","QA Failed","CI Failed","Dev Active"]) as $workflow
    | if (($names | index("Awaiting CI")) or ($names | index("CI Failed"))) then [.number, .headRefOid, 0]
      elif (.isDraft | not) and (.isCrossRepository | not) and ([$names[] | select(. as $n | $workflow | index($n))] | length) == 0 then [.number, .headRefOid, 1]
      else empty end
    | @tsv')

echo "PRs to settle: $(printf '%s' "$PRS" | grep -c . || true)" >&2
DETAILS=$(mktemp)
# On its own descriptor, so a command in the loop that reads stdin can't take the list.
while IFS=$'\t' read -r PR SHA ADOPT <&3; do
  if [ -z "$PR" ]; then continue; fi
  echo "PR #$PR at $SHA (adopt: $ADOPT)" >&2
  : > "$DETAILS"
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
  # Only this job's own comments count, so nobody can post the marker to silence it. A read that
  # fails isn't "no comment yet": that would post one every 15 minutes.
  MARKER="<!-- pr-labels-recheck $SHA -->"
  if ! COMMENTS=$(gh pr view "$PR" --repo "$REPO" --json comments \
    --jq '.comments[] | select(.author.login | test("^github-actions(\\[bot\\])?$")) | .body'); then
    echo "PR #$PR: couldn't read its comments" >&2
    FAILED=1
    continue
  fi
  if printf '%s\n' "$COMMENTS" | grep -qF "$MARKER"; then
    echo "PR #$PR: already commented on $SHA" >&2
    continue
  fi
  if ! gh pr comment "$PR" --repo "$REPO" --body "$MARKER
The label automation has no result for these on \`$SHA\`, $AGE minutes after its runs finished:

\`\`\`
$MISSING
\`\`\`

A lagging listing usually clears on a later recheck, every 15 minutes. If it doesn't, re-run the workflow in Actions, or, if the PR has no QA or \`Merge conflict\` label, apply \`Ready for QA\` by hand once \`gh pr checks $PR\` shows every check passed except \`QA Gate\`." >&2; then
    echo "PR #$PR: couldn't comment" >&2
    FAILED=1
  fi
done 3<<< "$PRS"
rm -f "$DETAILS"
exit "$FAILED"
