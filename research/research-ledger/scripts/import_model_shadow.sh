#!/usr/bin/env bash
set -euo pipefail

APP=/opt/research-ledger
DATA=/var/lib/research-ledger
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
MANIFEST="$DATA/manifests/models/models-${STAMP}.json"

install -d -o root -g researchledger -m 2750 "$DATA/manifests/models"
"$APP/venv/bin/python" "$APP/app/model_shadow_adapter.py" --out "$MANIFEST" >/dev/null
chown root:researchledger "$MANIFEST"
chmod 0640 "$MANIFEST"
sudo -n -u researchledger env LEDGER_DB_NAME=research_ledger "$APP/venv/bin/python" "$APP/app/ledger_app.py" import-models --manifest "$MANIFEST"
