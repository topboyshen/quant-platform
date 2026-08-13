#!/usr/bin/env bash
set -euo pipefail

APP=/opt/research-ledger
DATA=/var/lib/research-ledger
mkdir -p "$DATA/manifests/astock" "$DATA/cas"
chown root:researchledger "$DATA" "$DATA/manifests" "$DATA/manifests/astock" "$DATA/cas"
chmod 2750 "$DATA" "$DATA/manifests" "$DATA/manifests/astock" "$DATA/cas"

# The adapter copies only bounded regular report files to CAS. Notification/orchestrator files are
# hashed in place and are represented only by strict allowlisted metadata in the manifest.
"$APP/venv/bin/python" "$APP/app/astock_shadow_adapter.py" --cas-root "$DATA/cas" --manifest-dir "$DATA/manifests/astock" >/dev/null
manifest=$(find "$DATA/manifests/astock" -maxdepth 1 -type f -name 'astock-*.json' -printf '%T@ %p\n' | sort -n | tail -n 1 | cut -d ' ' -f 2-)
test -n "$manifest"
chown root:researchledger "$manifest"
chmod 0640 "$manifest"
sudo -n -u researchledger env LEDGER_DB_NAME=research_ledger "$APP/venv/bin/python" "$APP/app/ledger_app.py" import-astock --manifest "$manifest"
