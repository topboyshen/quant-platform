#!/usr/bin/env bash
# Single scheduled entry point for the AStock morning report.
#
# This wrapper owns only process-level concurrency. The existing
# run-morning-cron.sh remains the owner of the workflow result. Delivery is
# triggered only by the research-seal unit after source success.

set -euo pipefail

export PATH="/usr/local/bin:/usr/bin:/bin"
export HOME="${HOME:-/home/ubuntu}"
export LANG="${LANG:-C.UTF-8}"
export LC_ALL="${LC_ALL:-C.UTF-8}"

if [[ ! -x /usr/bin/realpath ]]; then
  printf '%s\n' 'AStock: /usr/bin/realpath is required to resolve the scheduled entry point' >&2
  exit 1
fi
if ! SCRIPT_PATH="$(/usr/bin/realpath -e -- "${BASH_SOURCE[0]}")" || [[ ! -f "$SCRIPT_PATH" ]]; then
  printf '%s\n' 'AStock: scheduled entry point cannot be resolved to a regular file' >&2
  exit 1
fi
if ! PROJECT_ROOT="$(/usr/bin/realpath -e -- "${SCRIPT_PATH%/*}/..")" || [[ ! -d "$PROJECT_ROOT" ]]; then
  printf '%s\n' 'AStock: project root cannot be resolved to a directory' >&2
  exit 1
fi
ORCHESTRATOR_ROOT="$PROJECT_ROOT/work/orchestrator"
LOCK_FILE="$ORCHESTRATOR_ROOT/morning-report.lock"

if [[ -L "$ORCHESTRATOR_ROOT" ]]; then
  printf '%s\n' 'AStock: orchestrator root must not be a symbolic link' >&2
  exit 1
fi
mkdir -p "$ORCHESTRATOR_ROOT"
chmod 700 "$ORCHESTRATOR_ROOT"
if [[ ! -d "$ORCHESTRATOR_ROOT" || -L "$ORCHESTRATOR_ROOT" ]]; then
  printf '%s\n' 'AStock: invalid orchestrator root' >&2
  exit 1
fi
if [[ -L "$LOCK_FILE" || ( -e "$LOCK_FILE" && ! -f "$LOCK_FILE" ) ]]; then
  printf '%s\n' 'AStock: invalid morning-report lock file' >&2
  exit 1
fi

# Keep the lock in this shell so a conflict cannot be confused with exit 75
# returned by the fixed orchestrator route. Opening in append mode preserves
# the current owner's diagnostic record until the lock has been acquired.
exec 9>>"$LOCK_FILE"
if ! /usr/bin/flock --exclusive --nonblock 9; then
  observed_at="$(/usr/bin/date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'AStock: morning-report lock conflict at %s; inspect orchestrator status/events (%s)\n' \
    "$observed_at" "$LOCK_FILE" >&2
  if ! /usr/local/bin/node \
    "$PROJECT_ROOT/scripts/task-orchestrator.mjs" \
    record-lock-conflict morning-report; then
    printf '%s\n' 'AStock: failed to record morning-report lock conflict event' >&2
  fi
  exit 75
fi

# The JSON is intentionally small and contains no secrets. It lets a
# contending cron invocation identify the current lock owner in its audit
# event. The record may remain after unlock, but it is only treated as owner
# metadata when flock has independently reported a real conflict.
: >"$LOCK_FILE"
printf '{"schemaVersion":"1.0","task":"morning-report","pid":%s,"startedAt":"%s"}\n' \
  "$$" "$(/usr/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" >&9
chmod 600 "$LOCK_FILE"

# Publish-branch gate (batch 1, 2026-09-26). systemd OnSuccess= only sees the
# exit code, and a validated trading-calendar skip exits 0 like a published
# run. The marker closes the source-to-seal daily publish branch: the seal,
# optional XHS draft and notify units carry ConditionPathExists=!<marker>, so
# systemd skips those units and starts none of their OnSuccess=/OnFailure=
# dependents. No daily report or daily Telegram is produced. The independent
# relay may still emit a signed research_skip receipt. It is cleared under the lock
# before every run, so a stale marker can never close a later trading day, and
# it is written only when publish-branch-gate.mjs validates the skip. A gate
# error leaves the branch open (previous behaviour; the seal still holds).
PUBLISH_CLOSED_MARKER="$ORCHESTRATOR_ROOT/publish-branch.closed"
if [[ -L "$PUBLISH_CLOSED_MARKER" || ( -e "$PUBLISH_CLOSED_MARKER" && ! -f "$PUBLISH_CLOSED_MARKER" ) ]]; then
  printf '%s\n' 'AStock: invalid publish-branch marker' >&2
  exit 1
fi
rm -f -- "$PUBLISH_CLOSED_MARKER"

set +e
/usr/local/bin/node \
  "$PROJECT_ROOT/scripts/task-orchestrator.mjs" \
  run morning-report
orchestrator_exit=$?
set -e

if (( orchestrator_exit == 0 )); then
  if ! /usr/local/bin/node "$PROJECT_ROOT/scripts/publish-branch-gate.mjs" decide; then
    printf '%s\n' 'AStock: publish-branch gate could not decide; branch left open (seal gates still apply)' >&2
  fi
fi
exit "$orchestrator_exit"
