#!/usr/bin/env bash
set -euo pipefail

readonly MODE_FILE="${RUNTIME_HEALTH_MODE_FILE:-/etc/research-ledger/runtime-health.mode}"
readonly LEDGER_BACKUP_STATE="${RUNTIME_HEALTH_LEDGER_BACKUP_STATE:-/var/lib/research-ledger-backup/last-success.json}"
readonly PHASE2_BACKUP_STATE="${RUNTIME_HEALTH_PHASE2_BACKUP_STATE:-/var/lib/research-system-phase2-backup/last-success.json}"
readonly PHASE2_BACKUP_ROOT="${RUNTIME_HEALTH_PHASE2_BACKUP_ROOT:-/var/backups/research-system-phase2}"
readonly MAX_BACKUP_AGE_SECONDS="${RUNTIME_HEALTH_MAX_BACKUP_AGE_SECONDS:-9000}"

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

for backup_timer in research-ledger-backup.timer research-system-phase2-backup.timer; do
  backup_load_state="$(unit_property "$backup_timer" LoadState)"
  backup_unit_file_state="$(unit_property "$backup_timer" UnitFileState)"
  backup_active_state="$(unit_property "$backup_timer" ActiveState)"
  [[ "$backup_load_state" == "loaded" && "$backup_unit_file_state" == "enabled" && "$backup_active_state" == "active" ]] \
    || fail "backup_timer_invalid unit=$backup_timer state=$backup_load_state/$backup_unit_file_state/$backup_active_state"
done

if [[ ! -f "$LEDGER_BACKUP_STATE" || -L "$LEDGER_BACKUP_STATE" ]]; then
  fail "ledger_backup_state_invalid"
fi

backup_mtime="$(stat -c '%Y' "$LEDGER_BACKUP_STATE")" || fail "ledger_backup_state_stat_failed"
now="$(date +%s)"
[[ "$backup_mtime" =~ ^[0-9]+$ && "$now" =~ ^[0-9]+$ ]] \
  || fail "ledger_backup_state_timestamp_invalid"
ledger_backup_age=$(( now - backup_mtime ))
(( ledger_backup_age >= 0 && ledger_backup_age <= MAX_BACKUP_AGE_SECONDS )) \
  || fail "ledger_backup_state_stale age_seconds=$ledger_backup_age max_seconds=$MAX_BACKUP_AGE_SECONDS"

if [[ ! -f "$PHASE2_BACKUP_STATE" || -L "$PHASE2_BACKUP_STATE" ]]; then
  fail "phase2_backup_state_invalid"
fi

phase2_backup_summary="$(/usr/bin/python3 - "$PHASE2_BACKUP_STATE" "$PHASE2_BACKUP_ROOT" "$MAX_BACKUP_AGE_SECONDS" <<'PY'
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys

state_path = pathlib.Path(sys.argv[1])
backup_root = pathlib.Path(sys.argv[2])
max_age = int(sys.argv[3])

with state_path.open("r", encoding="utf-8") as state_file:
    state = json.load(state_file)

filename = state.get("file")
expected_sha = state.get("sha256")
expected_bytes = state.get("bytes")
created_at = state.get("createdAt")
if not isinstance(filename, str) or pathlib.Path(filename).name != filename:
    raise SystemExit("invalid backup filename")
if not re.fullmatch(r"research-system-phase2-[0-9]{8}T[0-9]{6}Z[.]tar[.]gz", filename):
    raise SystemExit("unexpected backup filename")
if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
    raise SystemExit("invalid backup sha256")
if not isinstance(expected_bytes, int) or expected_bytes <= 0:
    raise SystemExit("invalid backup byte count")
if state.get("sqliteCheck") != "ok":
    raise SystemExit("sqlite check not ok")

created = dt.datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
now = dt.datetime.now(dt.timezone.utc)
age = int((now - created).total_seconds())
if age < 0 or age > max_age:
    raise SystemExit("backup state stale")

archive = backup_root / "hourly" / filename
if not archive.is_file() or archive.is_symlink():
    raise SystemExit("backup archive missing or symlinked")
if archive.stat().st_size != expected_bytes:
    raise SystemExit("backup archive size mismatch")

digest = hashlib.sha256()
with archive.open("rb") as archive_file:
    for chunk in iter(lambda: archive_file.read(1024 * 1024), b""):
        digest.update(chunk)
if digest.hexdigest() != expected_sha:
    raise SystemExit("backup archive hash mismatch")

print(f"{age}\t{filename}")
PY
)" || fail "phase2_backup_validation_failed"
IFS=$'\t' read -r phase2_backup_age phase2_backup_file <<< "$phase2_backup_summary"
[[ "$phase2_backup_age" =~ ^[0-9]+$ && -n "$phase2_backup_file" ]] \
  || fail "phase2_backup_summary_invalid"

timer_state_summary="$(IFS=,; printf '%s' "${timer_states[*]}")"
printf 'RUNTIME_HEALTH=ok business_mode=%s ledger_backup_age_seconds=%s phase2_backup_age_seconds=%s phase2_backup_file=%s timers=%s\n' \
  "$expected_mode" "$ledger_backup_age" "$phase2_backup_age" "$phase2_backup_file" "$timer_state_summary"
