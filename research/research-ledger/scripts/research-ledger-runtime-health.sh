#!/usr/bin/env bash
set -euo pipefail

readonly MODE_FILE="${RUNTIME_HEALTH_MODE_FILE:-/etc/research-ledger/runtime-health.mode}"
readonly BACKUP_STATE="/var/lib/research-ledger-backup/last-success.json"
readonly MAX_BACKUP_AGE_SECONDS=9000

readonly BUSINESS_TIMERS=(
  research-system-phase2-producer.timer
  research-system-phase2-settler.timer
  research-ledger-phase2-import.timer
  research-ledger-worker.timer
)

readonly BUSINESS_SERVICES=(
  research-system-phase2-producer.service
  research-system-phase2-settler.service
  research-ledger-phase2-import.service
  research-ledger-worker.service
)

fail() {
  printf 'RUNTIME_HEALTH=fail reason=%s\n' "$1" >&2
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

if [[ ! -f "$MODE_FILE" || -L "$MODE_FILE" ]]; then
  fail "mode_file_invalid"
fi

IFS= read -r expected_mode < "$MODE_FILE" || fail "mode_file_unreadable"
case "$expected_mode" in
  RUNNING|FROZEN) ;;
  *) fail "mode_invalid" ;;
esac

health_payload="$(curl --fail --silent --show-error --max-time 5 http://127.0.0.1:18769/health)" \
  || fail "ledger_api_unreachable"
if ! /usr/bin/python3 -c 'import json, sys; payload = json.load(sys.stdin); raise SystemExit(0 if payload.get("status") == "ok" else 1)' \
  <<<"$health_payload"; then
  fail "ledger_api_status_not_ok"
fi

timer_states=()
timer_mode=""
for timer in "${BUSINESS_TIMERS[@]}"; do
  load_state="$(unit_property "$timer" LoadState)"
  unit_file_state="$(unit_property "$timer" UnitFileState)"
  active_state="$(unit_property "$timer" ActiveState)"
  timer_states+=("$timer=$load_state/$unit_file_state/$active_state")

  [[ "$load_state" == "loaded" ]] \
    || fail "business_timer_not_loaded unit=$timer state=$load_state/$unit_file_state/$active_state"

  current_mode=""
  if [[ "$unit_file_state" == "enabled" && "$active_state" == "active" ]]; then
    current_mode="RUNNING"
  elif [[ "$unit_file_state" == "disabled" && "$active_state" == "inactive" ]]; then
    current_mode="FROZEN"
  else
    fail "business_timer_invalid unit=$timer state=$load_state/$unit_file_state/$active_state expected_mode=$expected_mode"
  fi

  if [[ -z "$timer_mode" ]]; then
    timer_mode="$current_mode"
  elif [[ "$timer_mode" != "$current_mode" ]]; then
    fail "business_timer_group_mixed expected_mode=$expected_mode"
  fi
done

[[ "$timer_mode" == "$expected_mode" ]] \
  || fail "business_timer_mode_mismatch expected=$expected_mode actual=$timer_mode"

if [[ "$expected_mode" == "FROZEN" ]]; then
  for service in "${BUSINESS_SERVICES[@]}"; do
    load_state="$(unit_property "$service" LoadState)"
    active_state="$(unit_property "$service" ActiveState)"
    [[ "$load_state" == "loaded" && "$active_state" == "inactive" ]] \
      || fail "frozen_service_not_inactive unit=$service state=$load_state/$active_state"
  done
fi

backup_load_state="$(unit_property research-ledger-backup.timer LoadState)"
backup_unit_file_state="$(unit_property research-ledger-backup.timer UnitFileState)"
backup_active_state="$(unit_property research-ledger-backup.timer ActiveState)"
[[ "$backup_load_state" == "loaded" && "$backup_unit_file_state" == "enabled" && "$backup_active_state" == "active" ]] \
  || fail "backup_timer_invalid state=$backup_load_state/$backup_unit_file_state/$backup_active_state"

if [[ ! -f "$BACKUP_STATE" || -L "$BACKUP_STATE" ]]; then
  fail "backup_state_invalid"
fi

backup_mtime="$(stat -c '%Y' "$BACKUP_STATE")" || fail "backup_state_stat_failed"
now="$(date +%s)"
[[ "$backup_mtime" =~ ^[0-9]+$ && "$now" =~ ^[0-9]+$ ]] \
  || fail "backup_state_timestamp_invalid"
backup_age=$(( now - backup_mtime ))
(( backup_age >= 0 && backup_age <= MAX_BACKUP_AGE_SECONDS )) \
  || fail "backup_state_stale age_seconds=$backup_age max_seconds=$MAX_BACKUP_AGE_SECONDS"

timer_state_summary="$(IFS=,; printf '%s' "${timer_states[*]}")"
printf 'RUNTIME_HEALTH=ok business_mode=%s backup_age_seconds=%s timers=%s\n' \
  "$expected_mode" "$backup_age" "$timer_state_summary"
