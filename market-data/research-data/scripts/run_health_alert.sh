#!/usr/bin/env bash
# Read the sandboxed research-data health JSON and Telegram-alert when it is
# critical/unknown/missing, or when the audit unit itself is in a failed state.
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
while IFS='=' read -r key value; do
  case "$key" in
    STATUS|CODES|GENERATED_AT|STALE|REASON|UNIT_STATE|ALERT)
      declare "$key=$value" ;;
  esac
done < <("$PYTHON" "$HELPER" describe --json "$HEALTH_JSON" --max-age-hours "$MAX_AGE_HOURS" \
           --unit-state "$(unit_state)" 2>/dev/null || true)

if [[ "$ALERT" != "yes" ]]; then
  printf 'health alert skipped status=%s unit=%s stale=%s\n' "$STATUS" "$UNIT_STATE" "$STALE"
  exit 0
fi

entry_reason="scheduled_audit"
if [[ "$UNIT_STATE" == "failed" ]]; then
  entry_reason="audit_failed"
fi
message="$("$PYTHON" "$HELPER" message --json "$HEALTH_JSON" --max-age-hours "$MAX_AGE_HOURS" \
           --unit-state "$UNIT_STATE" --host "$(hostname)" --entry-reason "$entry_reason")"
printf 'health alert status=%s unit=%s stale=%s reason=%s\n' "$STATUS" "$UNIT_STATE" "$STALE" "$REASON" >&2
send_telegram "$message"
