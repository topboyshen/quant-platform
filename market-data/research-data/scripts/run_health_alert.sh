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
readonly STATE_DIR="${HOME}/.local/state/research-data"
readonly RECEIPT_FILE="${STATE_DIR}/health-alert-receipt.json"
# Last committed fault set (fingerprint excludes runId/audit timestamps). It is
# committed only after a confirmed delivery, or when nothing needed sending.
readonly ALERT_STATE_FILE="${STATE_DIR}/health-alert-state.json"
readonly PENDING_STATE_FILE="${STATE_DIR}/.health-alert-state.pending.json"
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
  set +e
  printf '%s\n' "$message" | timeout 60 "$HERMES_PYTHON" -m hermes_cli.main send --to telegram --file - --json >"$out" 2>/dev/null
  rc=$?
  set -e
  set +e
  receipt_lines="$("$PYTHON" "$HELPER" receipt --json-file "$out" --exit-code "$rc" --state-file "$RECEIPT_FILE")"
  local receipt_rc=$?
  set -e
  rm -f "$out"
  if [[ "$receipt_rc" -ne 0 ]]; then
    printf '%s\n' "$receipt_lines" >&2
    printf 'alert not delivered: receipt validation failed (exit=%s)\n' "$rc" >&2
    return 1
  fi
  printf '%s\n' "$receipt_lines"
  return 0
}

if [[ "${1:-}" == "--dry-run-notify" ]]; then
  ALERT_DRY_RUN=1 send_telegram "research-data health 告警（dry-run）"
  exit $?
fi

# Fail toward alerting: any helper/audit problem leaves ALERT=yes.
STATUS="unknown"; CODES="none"; GENERATED_AT="unknown"; STALE="yes"; REASON="helper_unavailable"; UNIT_STATE="unknown"; ALERT="yes"
DECISION="helper_unavailable"; NEW_COUNT="?"; WORSENED_COUNT="?"; RECOVERED_COUNT="?"; UNCONFIRMED_DAYS="?"
rm -f -- "$PENDING_STATE_FILE"
while IFS='=' read -r key value; do
  case "$key" in
    STATUS|CODES|GENERATED_AT|STALE|REASON|UNIT_STATE|ALERT|DECISION|NEW_COUNT|WORSENED_COUNT|RECOVERED_COUNT|UNCONFIRMED_DAYS)
      declare "$key=$value" ;;
  esac
done < <("$PYTHON" "$HELPER" describe --json "$HEALTH_JSON" --max-age-hours "$MAX_AGE_HOURS" \
           --unit-state "$(unit_state)" --state-file "$ALERT_STATE_FILE" \
           --pending-file "$PENDING_STATE_FILE" 2>/dev/null || true)

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
  "$PYTHON" "$HELPER" commit-state --pending-file "$PENDING_STATE_FILE" --state-file "$ALERT_STATE_FILE"
}

if [[ "$ALERT" != "yes" ]]; then
  printf 'health alert not sent: fault set unchanged decision=%s status=%s unit=%s stale=%s unconfirmed_days=%s\n' \
    "$DECISION" "$STATUS" "$UNIT_STATE" "$STALE" "$UNCONFIRMED_DAYS"
  commit_state || printf 'warning: alert state not committed; next run re-decides\n' >&2
  exit 0
fi

entry_reason="scheduled_audit"
if [[ "$UNIT_STATE" == "failed" ]]; then
  entry_reason="audit_failed"
fi
message="$("$PYTHON" "$HELPER" message --json "$HEALTH_JSON" --max-age-hours "$MAX_AGE_HOURS" \
           --unit-state "$UNIT_STATE" --host "$(hostname)" --entry-reason "$entry_reason" \
           --state-file "$ALERT_STATE_FILE")"
printf 'health alert status=%s unit=%s stale=%s reason=%s decision=%s new=%s worsened=%s recovered=%s\n' \
  "$STATUS" "$UNIT_STATE" "$STALE" "$REASON" "$DECISION" "$NEW_COUNT" "$WORSENED_COUNT" "$RECOVERED_COUNT" >&2
if ! send_telegram "$message"; then
  # Not delivered: keep the previous committed state so the change is retried.
  rm -f -- "$PENDING_STATE_FILE"
  exit 1
fi
if ! commit_state; then
  printf 'alert delivered but state not committed; the same change may be re-sent once\n' >&2
  exit 1
fi
