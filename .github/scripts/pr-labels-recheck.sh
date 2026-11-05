#!/usr/bin/env bash
# The recheck that doesn't depend on an event (#549, #339). A green PR can strand on Awaiting CI
# when GitHub's run or job listings lag behind the last CI event, as nothing checks again after
# that. So: settle each open PR on Awaiting CI, and each same-repository PR with no workflow label
# at all. If a result is still missing long after its runs finished, comment once on the PR.
#
# Env: REPO, GH_TOKEN. STRANDED_AFTER_MINUTES defaults to 30. Fork PRs without Awaiting CI are left
# alone: their runs wait for the maintainer's approval, and the label is the maintainer's call.
set -e

HERE=$(dirname "${BASH_SOURCE[0]}")
STRANDED_AFTER_MINUTES=${STRANDED_AFTER_MINUTES:-30}
FAILED=0
export REPO

# number, head commit, 1 to adopt a PR with no workflow label.
PRS=$(gh pr list --repo "$REPO" --state open --limit 100 \
  --json number,headRefOid,isDraft,isCrossRepository,labels \
  --jq '.[]
    | ([.labels[].name]) as $names
    | (["Awaiting CI","Ready for QA","QA Active","Ready for QA Signoff","QA Approved","QA Failed","CI Failed","Dev Active"]) as $workflow
    | if ($names | index("Awaiting CI")) then [.number, .headRefOid, 0]
      elif (.isDraft | not) and (.isCrossRepository | not) and ([$names[] | select(. as $n | $workflow | index($n))] | length) == 0 then [.number, .headRefOid, 1]
      else empty end
    | @tsv')

DETAILS=$(mktemp)
while IFS=$'\t' read -r PR SHA ADOPT; do
  if [ -z "$PR" ]; then continue; fi
  echo "PR #$PR at $SHA (adopt: $ADOPT)" >&2
  : > "$DETAILS"
  if ! STATE=$(ADOPT=$ADOPT PENDING_DETAILS=$DETAILS bash "$HERE/pr-labels-settle.sh" "$PR" "$SHA"); then
    # One PR's API error mustn't stop the others; the run still ends red for it.
    echo "PR #$PR: couldn't settle it" >&2
    FAILED=1
    continue
  fi
  if [ "$STATE" != pending ] || grep -qx running "$DETAILS"; then continue; fi
  MISSING=$(sed -n 's/^missing //p' "$DETAILS")
  # The latest time a finished run changed. With no run listed at all, nothing has been waiting.
  UPDATED=$(sed -n 's/^updated //p' "$DETAILS" | sort | tail -n 1)
  if [ -z "$MISSING" ] || [ -z "$UPDATED" ]; then continue; fi
  AGE=$(( ($(date -u +%s) - $(date -u -d "$UPDATED" +%s)) / 60 ))
  if [ "$AGE" -lt "$STRANDED_AFTER_MINUTES" ]; then continue; fi
  MARKER="<!-- pr-labels-recheck $SHA -->"
  if gh pr view "$PR" --repo "$REPO" --json comments --jq '.comments[].body' | grep -qF "$MARKER"; then
    continue
  fi
  gh pr comment "$PR" --repo "$REPO" --body "$MARKER
The label automation has no result for these on \`$SHA\`, $AGE minutes after its runs finished:

\`\`\`
$MISSING
\`\`\`

It rechecks every 15 minutes. If this is stuck, re-run the job in Actions, or apply \`Ready for QA\` by hand once \`gh pr checks $PR\` shows every check passed."
done <<< "$PRS"
rm -f "$DETAILS"
exit "$FAILED"
