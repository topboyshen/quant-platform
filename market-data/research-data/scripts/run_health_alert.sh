#!/usr/bin/env bash
# Read the sandboxed research-data health JSON and Telegram-alert only when the
# alert-worthy fault set CHANGED (new failure, worsened, recovered, or the audit
# should have run but left no current result / the audit unit failed). The
# audit's own severity is untouched; an unchanged critical set stays queryable
# in last.json and is not re-sent every night (batch 1, 2026-09-26).
# Delivery is confirmed from the hermes JSON receipt (success + message id, no
# skip); a failed or unconfirmed send exits nonzero so the unit is visibly failed.
# Does not collect data, mutate snapshots, or claim Qlib recovered.
set -euo pipefail
umask 077

readonly HEALTH_JSON="${RESEARCH_DATA_HEALTH_JSON:-/var/lib/research-data-health/last.json}"
readonly PYTHON="${RESEARCH_DATA_PYTHON:-/home/ubuntu/research-data/.venv/bin/python}"
readonly HERMES_PYTHON="${HERMES_PYTHON:-/home/ubuntu/.hermes/hermes-agent/venv/bin/python}"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly HELPER="${SCRIPT_DIR}/health_alert.py"
readonly STATE_DIR="${RESEARCH_DATA_ALERT_STATE_DIR:-${HOME}/.local/state/research-data}"
readonly RECEIPT_FILE="${STATE_DIR}/health-alert-receipt.json"
# Last committed fault set (fingerprint excludes runId/audit timestamps). It is
# committed only after a confirmed delivery, or when nothing needed sending.
readonly ALERT_STATE_FILE="${STATE_DIR}/health-alert-state.json"
readonly INFLIGHT_FILE="${STATE_DIR}/.health-alert-inflight.json"
readonly MAX_AGE_HOURS="${RESEARCH_DATA_HEALTH_MAX_AGE_HOURS:-6}"
readonly AUDIT_UNIT="research-data-health.service"

unit_state() {
  if systemctl is-failed --quiet "$AUDIT_UNIT" 2>/dev/null; then
    printf 'failed'
  elif systemctl is-active --quiet "$AUDIT_UNIT" 2>/dev/null; then
    printf 'active'
  else
    printf 'inactive'
  fi
}

send_telegram() {
  local message="$1"
  if [[ "${ALERT_DRY_RUN:-0}" == "1" ]]; then
    printf '%s\n' "$message"
    printf 'DELIVERED=yes\nRECEIPT_REASON=delivered\nRECEIPT_STORED=no (dry-run)\n'
    return 0
  fi
  if [[ ! -x "$HERMES_PYTHON" ]]; then
    printf 'Hermes Python unavailable\n' >&2
    return 1
  fi
  local out rc receipt_lines
  out="$(mktemp "${TMPDIR:-/tmp}/health-alert.XXXXXX")"
  if ! "$PYTHON" "$HELPER" begin-delivery --pending-file "$PENDING_STATE_FILE" \
      --state-file "$ALERT_STATE_FILE" --inflight-file "$INFLIGHT_FILE"; then
    rm -f -- "$out"
    printf 'health alert delivery intent not persisted; Hermes not called\n' >&2
    return 1
  fi
  set +e
  printf '%s\n' "$message" | timeout 60 "$HERMES_PYTHON" -m hermes_cli.main send --to telegram --file - --json >"$out" 2>/dev/null
  rc=$?
  set -e
  set +e
  receipt_lines="$("$PYTHON" "$HELPER" receipt --json-file "$out" --exit-code "$rc" --state-file "$RECEIPT_FILE" \
    --pending-file "$PENDING_STATE_FILE" --previous-state-file "$ALERT_STATE_FILE" --inflight-file "$INFLIGHT_FILE")"
  local receipt_rc=$?
  set -e
  rm -f "$out"
  if [[ "$receipt_rc" -ne 0 ]]; then
    printf '%s\n' "$receipt_lines" >&2
    printf 'alert delivery not confirmed: receipt validation failed (exit=%s)\n' "$rc" >&2
    if [[ -f "$INFLIGHT_FILE" ]]; then
      printf '投递结果未知，需对账；不会自动重发\n' >&2
    fi
    return 1
  fi
  printf '%s\n' "$receipt_lines"
  return 0
}

if [[ "${1:-}" == "--dry-run-notify" ]]; then
  ALERT_DRY_RUN=1 send_telegram "research-data health 告警（dry-run）"
  exit $?
fi

# systemd serializes its own triggers; also serialize direct wrapper invocations
# through the same prepare -> confirmed delivery -> state commit interval.
if [[ -L "$STATE_DIR" ]]; then
  printf 'health alert state directory is a symlink\n' >&2
  exit 1
fi
mkdir -p -- "$STATE_DIR"
exec 9>"${STATE_DIR}/.health-alert.lock"
if ! flock -w 65 9; then
  printf 'health alert state lock unavailable; no delivery or commit\n' >&2
  exit 1
fi
readonly RUN_DIR="$(mktemp -d "${STATE_DIR}/.health-alert-run.XXXXXX")"
readonly PENDING_STATE_FILE="${RUN_DIR}/pending.json"
readonly MESSAGE_FILE="${RUN_DIR}/message.txt"
cleanup_preparation() {
  rm -f -- "$PENDING_STATE_FILE" "$MESSAGE_FILE"
  rmdir -- "$RUN_DIR"
}
trap cleanup_preparation EXIT

if [[ "${ALERT_DRY_RUN:-0}" != "1" ]]; then
  if ! "$PYTHON" "$HELPER" recover-delivery --receipt-file "$RECEIPT_FILE" --state-file "$ALERT_STATE_FILE" \
      --inflight-file "$INFLIGHT_FILE"; then
    printf 'health alert confirmed-delivery recovery failed; no new delivery\n' >&2
    printf '投递结果未知，需对账；不会自动重发\n' >&2
    exit 1
  fi
fi

# 23:00 review: reconciliation first, then the SAME classifier as 22:45.
# recover-delivery above may apply an already confirmed local receipt
# (stateApplied / clear the matching inflight). It never rewrites old receipts.
# If an inflight marker still exists the outcome is unknown: report it and do not
# send. Otherwise prepare/decide runs unchanged, so a missing or stale 22:45
# audit is recorded as "should run but did not" (one atom per scheduled day) and
# an unchanged fault set stays silent.
REVIEW=no
if [[ "${1:-}" == "--review-unconfirmed" ]]; then
  REVIEW=yes
  if [[ -e "$INFLIGHT_FILE" || -L "$INFLIGHT_FILE" ]]; then
    printf '投递结果未知，需对账；不会自动重发\n' >&2
    exit 1
  fi
fi

STATUS="unknown"; CODES="none"; GENERATED_AT="unknown"; STALE="yes"; REASON="helper_unavailable"; UNIT_STATE="$(unit_state)"; ALERT="unknown"
DECISION="helper_unavailable"; NEW_COUNT="?"; WORSENED_COUNT="?"; RECOVERED_COUNT="?"; UNCONFIRMED_DAYS="?"
PREPARED="no"
entry_reason="scheduled_audit"
if [[ "$REVIEW" == "yes" ]]; then
  entry_reason="scheduled_review"
fi
if [[ "$UNIT_STATE" == "failed" ]]; then
  entry_reason="audit_failed"
fi
if ! preparation="$("$PYTHON" "$HELPER" prepare --json "$HEALTH_JSON" --max-age-hours "$MAX_AGE_HOURS" \
    --unit-state "$UNIT_STATE" --state-file "$ALERT_STATE_FILE" --pending-file "$PENDING_STATE_FILE" \
    --message-file "$MESSAGE_FILE" --host "$(hostname)" --entry-reason "$entry_reason")"; then
  printf 'health alert preparation failed; no delivery or commit\n' >&2
  exit 1
fi
while IFS='=' read -r key value; do
  case "$key" in
    PREPARED|STATUS|CODES|GENERATED_AT|STALE|REASON|UNIT_STATE|ALERT|DECISION|NEW_COUNT|WORSENED_COUNT|RECOVERED_COUNT|UNCONFIRMED_DAYS)
      declare "$key=$value" ;;
  esac
done <<< "$preparation"
if [[ "$PREPARED" != "yes" || ( "$ALERT" != "yes" && "$ALERT" != "no" ) ]]; then
  printf 'health alert preparation invalid; no delivery or commit\n' >&2
  exit 1
fi

commit_state() {
  if [[ "${ALERT_DRY_RUN:-0}" == "1" ]]; then
    rm -f -- "$PENDING_STATE_FILE"
    printf 'STATE_COMMITTED=no (dry-run)\n'
    return 0
  fi
  if [[ ! -f "$PENDING_STATE_FILE" ]]; then
    printf 'STATE_COMMITTED=no (no pending state)\n' >&2
    return 1
  fi
  "$PYTHON" "$HELPER" commit-state --pending-file "$PENDING_STATE_FILE" --state-file "$ALERT_STATE_FILE" \
    --receipt-file "$RECEIPT_FILE" --inflight-file "$INFLIGHT_FILE"
}

if [[ "$ALERT" != "yes" ]]; then
  printf 'health alert not sent: fault set unchanged decision=%s status=%s unit=%s stale=%s unconfirmed_days=%s\n' \
    "$DECISION" "$STATUS" "$UNIT_STATE" "$STALE" "$UNCONFIRMED_DAYS"
  if [[ "$REVIEW" == "yes" ]]; then
    printf 'health alert review: reconciled; same classifier ran; no new event\n'
  fi
  commit_state || printf 'warning: alert state not committed; next run re-decides\n' >&2
  exit 0
fi

message="$(<"$MESSAGE_FILE")"
printf 'health alert status=%s unit=%s stale=%s reason=%s decision=%s new=%s worsened=%s recovered=%s\n' \
  "$STATUS" "$UNIT_STATE" "$STALE" "$REASON" "$DECISION" "$NEW_COUNT" "$WORSENED_COUNT" "$RECOVERED_COUNT" >&2
if [[ "$REVIEW" == "yes" ]]; then
  printf 'health alert review: reconciled; classifier found a new event; sending once\n' >&2
fi
if ! send_telegram "$message"; then
  # Keep the previous committed fault state. If Hermes started, the persistent
  # inflight marker blocks automatic resend until its outcome is reconciled.
  rm -f -- "$PENDING_STATE_FILE"
  exit 1
fi
if ! commit_state; then
  printf 'alert delivered but state not committed; bound receipt retained for next-run recovery\n' >&2
  exit 1
fi
