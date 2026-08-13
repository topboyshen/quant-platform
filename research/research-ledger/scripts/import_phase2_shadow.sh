#!/usr/bin/env bash
set -euo pipefail

APP=/opt/research-ledger
DATA=/var/lib/research-ledger
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
SRC=/var/lib/research-system-phase2/phase2.db
COPY="$DATA/phase2-source/phase2-${STAMP}.db"
MANIFEST="$DATA/manifests/phase2/phase2-${STAMP}.json"

install -d -o root -g researchledger -m 2750 "$DATA/phase2-source" "$DATA/manifests/phase2"
/usr/bin/python3 /usr/local/lib/research-ledger/sqlite_consistent_backup.py "$SRC" "$COPY" >/dev/null
chown root:researchledger "$COPY"
chmod 0640 "$COPY"
"$APP/venv/bin/python" "$APP/app/phase2_shadow_adapter.py" --source-db "$COPY" --out "$MANIFEST" >/dev/null
chown root:researchledger "$MANIFEST"
chmod 0640 "$MANIFEST"
sudo -n -u researchledger env LEDGER_DB_NAME=research_ledger "$APP/venv/bin/python" "$APP/app/ledger_app.py" import-phase2 --manifest "$MANIFEST"
