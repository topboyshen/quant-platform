#!/usr/bin/env bash
set -euo pipefail

APP=/opt/research-ledger
DATA=/var/lib/research-ledger
P0=$(find /var/backups/research-ledger -maxdepth 1 -mindepth 1 -type d -name 'p0-*' -print | sort | tail -n 1)
test -n "$P0"
install -d -o root -g researchledger -m 2750 "$DATA/manifests/legacy"
for name in v01 v02; do
  source_db="$P0/sqlite/${name}.db"
  test -f "$source_db"
  manifest="$DATA/manifests/legacy/${name}-$(date -u +%Y%m%dT%H%M%SZ).json"
  "$APP/venv/bin/python" "$APP/app/legacy_shadow_adapter.py" --source-db "$source_db" --source-system "RESEARCH_SYSTEM_${name^^}" --out "$manifest" >/dev/null
  chown root:researchledger "$manifest"
  chmod 0640 "$manifest"
  sudo -n -u researchledger env LEDGER_DB_NAME=research_ledger "$APP/venv/bin/python" "$APP/app/ledger_app.py" import-legacy --manifest "$manifest"
done
