#!/usr/bin/env bash
# Prints pass, fail or pending for a commit across the CI and vdsm workflows, judged job by job:
# vdsm's jobs (and CI's tool-quality report) are continue-on-error, so a run's own conclusion says
# "success" even when a job failed (#241). tool-quality is report-only and is ignored; every other
# job must succeed or be skipped. Everything else goes to stderr, for the run's log.
#
# Env: REPO, HEAD_SHA, GH_TOKEN. PENDING_DETAILS, if set, is a file that gets, for a pending state:
#   running              a run hasn't finished
#   updated <time>       when a finished run last changed
#   missing <text>       what has no result (a finished run's job, or a run not listed)
#
# NOTE: names the workflows by file (ci.yml, vdsm.yml). If one is renamed, update it here.
set -e

log() { echo "$*" >&2; }
detail() { if [ -n "${PENDING_DETAILS:-}" ]; then echo "$*" >> "$PENDING_DETAILS"; fi; }

STATE=pass
for WF in ci.yml vdsm.yml; do
  RUN=$(gh api "repos/$REPO/actions/workflows/$WF/runs?head_sha=$HEAD_SHA&event=pull_request&per_page=1" \
    --jq '.workflow_runs[0] // empty | "\(.id) \(.status) \(.conclusion) \(.updated_at)"')
  if [ -z "$RUN" ]; then
    log "$WF: no run yet for $HEAD_SHA"
    detail "missing $WF: no run listed"
    STATE=pending; continue
  fi
  read -r RUN_ID RUN_STATUS RUN_CONCLUSION RUN_UPDATED <<< "$RUN"
  if [ "$RUN_STATUS" != completed ]; then
    log "$WF: run $RUN_ID is $RUN_STATUS"
    detail "running"
    STATE=pending; continue
  fi
  detail "updated $RUN_UPDATED"
  # A run can read "completed" before its jobs' results are recorded (a job with no conclusion, or
  # no jobs listed yet; seen on #298 and #549, where the jobs API lagged by 14 minutes). That's
  # pending, not failed. Read the jobs up to 3 times, 20 s apart; the scheduled recheck
  # (pr-labels-recheck.yml) covers a lag longer than that, as no later event may come.
  for TRY in 1 2 3; do
    JOBS=$(gh api --paginate "repos/$REPO/actions/runs/$RUN_ID/jobs" \
      --jq '.jobs[] | select(.name != "tool-quality") | [(.status // "none"), (.conclusion // "none"), .name] | @tsv')
    OPEN=$(printf '%s\n' "$JOBS" | awk -F'\t' 'NF && ($1 != "completed" || $2 == "none") { print $3 ": " $1 "/" $2 }')
    if [ -z "$JOBS" ]; then
      # No jobs at all: a run that failed before starting any (e.g. startup_failure) has failed;
      # otherwise wait for them.
      case "$RUN_CONCLUSION" in
        success|skipped|null|"") OPEN="no jobs listed" ;;
        *) OPEN="" ;;
      esac
    fi
    if [ -z "$OPEN" ] || [ "$TRY" = 3 ]; then break; fi
    log "$WF: run $RUN_ID: results not recorded yet (try $TRY), rechecking in ${RECHECK_SECONDS:-20} s"
    sleep "${RECHECK_SECONDS:-20}"
  done
  if [ -n "$OPEN" ]; then
    log "$WF: run $RUN_ID is completed, but these jobs have no result yet:"; log "$OPEN"
    while IFS= read -r LINE; do detail "missing $WF: $LINE"; done <<< "$OPEN"
    STATE=pending; continue
  fi
  BAD=$(printf '%s\n' "$JOBS" | awk -F'\t' 'NF && $2 != "success" && $2 != "skipped" { print $3 ": " $2 }')
  if [ -z "$JOBS" ]; then BAD="(no jobs) run conclusion: $RUN_CONCLUSION"; fi
  if [ -n "$BAD" ]; then
    log "$WF: run $RUN_ID has failed jobs:"; log "$BAD"
    STATE=fail; break
  fi
  log "$WF: run $RUN_ID passed"
done
log "Checks on $HEAD_SHA: $STATE"
echo "$STATE"
