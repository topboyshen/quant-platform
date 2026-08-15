#!/usr/bin/env bash
set -euo pipefail

readonly MODE="${1:-full}"
[[ "$MODE" == "full" || "$MODE" == "--preflight" ]] || { printf 'invalid mode\n' >&2; exit 2; }
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

for timer in "${TIMERS[@]}"; do
  systemctl is-active --quiet "$timer"
done
for unit in "${SERVICES[@]}"; do
  [[ "$(systemctl show "$unit" -p LoadState --value)" == "loaded" ]]
  if [[ "$MODE" == "full" ]]; then
    [[ "$(systemctl show "$unit" -p Result --value)" == "success" ]]
    [[ "$(systemctl show "$unit" -p ExecMainStatus --value)" == "0" ]]
    completed="$(systemctl show "$unit" -p InactiveEnterTimestamp --value)"
    [[ -n "$completed" ]]
    [[ "$(date -d "$completed" +%F)" == "$TODAY" ]]
  fi
done
printf 'PIPELINE_HEALTH=ok mode=%s date=%s\n' "$MODE" "$TODAY"
