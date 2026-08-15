#!/usr/bin/env bash
set -euo pipefail

curl --fail --silent --show-error --max-time 5 http://127.0.0.1:18769/health >/dev/null
for timer in \
  research-system-phase2-producer.timer \
  research-system-phase2-settler.timer \
  research-ledger-phase2-import.timer \
  research-ledger-worker.timer \
  research-ledger-backup.timer; do
  systemctl is-active --quiet "$timer"
done
state=/var/lib/research-ledger-backup/last-success.json
[[ -f "$state" && ! -L "$state" ]]
age=$(( $(date +%s) - $(stat -c '%Y' "$state") ))
(( age >= 0 && age <= 9000 ))
printf 'RUNTIME_HEALTH=ok backup_age_seconds=%s\n' "$age"
