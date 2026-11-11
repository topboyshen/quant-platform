#!/usr/bin/env bash
set -euo pipefail

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BASE="/var/backups/research-ledger/privacy-repair-${STAMP}"
install -d -o root -g root -m 0750 "$BASE" "$BASE/manifests" "$BASE/cas-index"

PG_TMP=$(sudo -n -u postgres mktemp /tmp/research-ledger-privacy.XXXXXX)
sudo -n -u postgres pg_dump -Fc --no-owner --no-privileges -f "$PG_TMP" research_ledger
install -o root -g root -m 0640 "$PG_TMP" "$BASE/research_ledger.pre-repair.dump"
rm -f "$PG_TMP"

# Manifest is metadata-only by design; preserve it for rollback without touching AStock originals.
cp -a /var/lib/research-ledger/manifests/astock/. "$BASE/manifests/"
find /var/lib/research-ledger/cas -type f -printf '%P\t%s\n' 2>/dev/null | sort > "$BASE/cas-index/files.tsv"
find /var/lib/research-ledger/cas -type f -print0 2>/dev/null | sort -z | xargs -0 -r sha256sum > "$BASE/cas-index/files.sha256"
find "$BASE" -type f -print0 | sort -z | xargs -0 sha256sum > "$BASE/SHA256SUMS"
chmod 0640 "$BASE/SHA256SUMS" "$BASE/research_ledger.pre-repair.dump"
printf 'PRIVACY_REPAIR_BACKUP_DIR=%s\n' "$BASE"
printf 'MANIFEST_FILES=%s\n' "$(find "$BASE/manifests" -type f | wc -l)"
printf 'CAS_INDEX_ENTRIES=%s\n' "$(wc -l < "$BASE/cas-index/files.tsv")"
printf 'CHECKSUM_LINES=%s\n' "$(wc -l < "$BASE/SHA256SUMS")"
