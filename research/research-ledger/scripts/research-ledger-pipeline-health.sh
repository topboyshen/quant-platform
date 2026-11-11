#!/usr/bin/env bash
set -euo pipefail

readonly MODE="${1:-full}"
[[ "$MODE" == "full" || "$MODE" == "--preflight" ]] || { printf 'invalid mode\n' >&2; exit 2; }
readonly BUSINESS_MODE_FILE="${PIPELINE_HEALTH_MODE_FILE:-/etc/research-ledger/runtime-health.mode}"
readonly TODAY="$(date +%F)"
readonly SERVICES=(
  research-system-phase2-producer.service
  research-system-phase2-settler.service
  research-ledger-phase2-import.service
  research-ledger-worker.service
)
readonly TIMERS=(
  research-system-phase2-producer.timer
  research-system-phase2-settler.timer
  research-ledger-phase2-import.timer
  research-ledger-worker.timer
)

fail() {
  printf 'PIPELINE_HEALTH=fail reason=%s\n' "$1" >&2
  exit 1
}

unit_property() {
  local unit="$1"
  local property="$2"
  local value
  value="$(systemctl show "$unit" --property="$property" --value 2>/dev/null || true)"
  [[ -n "$value" ]] || value="unknown"
  printf '%s' "$value"
}

if [[ ! -f "$BUSINESS_MODE_FILE" || -L "$BUSINESS_MODE_FILE" ]]; then
  fail "mode_file_invalid"
fi
IFS= read -r business_mode < "$BUSINESS_MODE_FILE" || fail "mode_file_unreadable"
case "$business_mode" in
  RUNNING|FROZEN) ;;
  *) fail "mode_invalid" ;;
esac

if [[ "$business_mode" == "FROZEN" ]]; then
  for timer in "${TIMERS[@]}"; do
    load_state="$(unit_property "$timer" LoadState)"
    unit_file_state="$(unit_property "$timer" UnitFileState)"
    active_state="$(unit_property "$timer" ActiveState)"
    [[ "$load_state" == "loaded" && "$unit_file_state" == "disabled" && "$active_state" == "inactive" ]] \
      || fail "frozen_timer_invalid unit=$timer state=$load_state/$unit_file_state/$active_state"
  done
  for unit in "${SERVICES[@]}"; do
    load_state="$(unit_property "$unit" LoadState)"
    active_state="$(unit_property "$unit" ActiveState)"
    [[ "$load_state" == "loaded" && "$active_state" == "inactive" ]] \
      || fail "frozen_service_invalid unit=$unit state=$load_state/$active_state"
  done
  printf 'PIPELINE_HEALTH=ok mode=%s business_mode=%s date=%s\n' "$MODE" "$business_mode" "$TODAY"
  exit 0
fi

for timer in "${TIMERS[@]}"; do
  systemctl is-active --quiet "$timer" || fail "timer_not_active unit=$timer"
done
for unit in "${SERVICES[@]}"; do
  [[ "$(unit_property "$unit" LoadState)" == "loaded" ]] || fail "service_not_loaded unit=$unit"
  if [[ "$MODE" == "full" ]]; then
    [[ "$(unit_property "$unit" Result)" == "success" ]] || fail "service_result_not_success unit=$unit"
    [[ "$(unit_property "$unit" ExecMainStatus)" == "0" ]] || fail "service_exit_not_zero unit=$unit"
    completed="$(unit_property "$unit" InactiveEnterTimestamp)"
    [[ "$completed" != "unknown" ]] || fail "service_completion_missing unit=$unit"
    [[ "$(date -d "$completed" +%F)" == "$TODAY" ]] || fail "service_completion_not_today unit=$unit"
  fi
done
printf 'PIPELINE_HEALTH=ok mode=%s business_mode=%s date=%s\n' "$MODE" "$business_mode" "$TODAY"
