#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly APP_ROOT="${LEDGER_APP_ROOT:-/opt/research-ledger}"
readonly PG_DATABASE="${LEDGER_DB_NAME:-research_ledger}"
readonly PHASE2_DB="${PHASE2_DB_PATH:-/var/lib/research-system-phase2/phase2.db}"
readonly PROTOCOL="${PHASE2_PROTOCOL_PATH:-/opt/research-system-phase2/protocol/calibration_protocol_v1.json}"
readonly LEDGER_STATE="${LEDGER_STATE_ROOT:-/var/lib/research-ledger}"
readonly BACKUP_ROOT="${LEDGER_BACKUP_ROOT:-/var/backups/research-ledger}"
readonly HOURLY="$BACKUP_ROOT/hourly"
readonly STATE_ROOT="${LEDGER_BACKUP_STATE_ROOT:-/var/lib/research-ledger-backup}"
readonly STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly NAME="research-ledger-$STAMP.tar.gz"
readonly FINAL="$HOURLY/$NAME"
readonly TMP_ARCHIVE="$HOURLY/.$NAME.tmp"

[[ "$(id -u)" == "0" ]] || { printf 'backup must run as root\n' >&2; exit 1; }
for required in "$APP_ROOT/.git" "$APP_ROOT/app/ledger_app.py" "$PHASE2_DB" "$PROTOCOL" "$LEDGER_STATE/cas" "$LEDGER_STATE/manifests"; do
  [[ -e "$required" && ! -L "$required" ]] || { printf 'required backup input missing or symlinked: %s\n' "$required" >&2; exit 1; }
done
if [[ -n "$(find "$LEDGER_STATE" -type l -print -quit)" ]]; then
  printf 'Ledger state contains a symbolic link; refusing incomplete backup\n' >&2
  exit 1
fi

install -d -o root -g ubuntu -m 0750 "$BACKUP_ROOT" "$HOURLY"
install -d -o root -g researchledger -m 0750 "$STATE_ROOT"
exec 9>"/run/lock/research-ledger-backup.lock"
flock -n 9 || { printf 'backup already running\n' >&2; exit 75; }

stage="$(mktemp -d "$STATE_ROOT/.stage-$STAMP-XXXXXX")"
cleanup() {
  rm -rf -- "$stage"
  rm -f -- "$TMP_ARCHIVE"
}
trap cleanup EXIT

runuser -u postgres -- pg_dump -Fc --no-owner --no-privileges "$PG_DATABASE" > "$stage/research_ledger.dump"
runuser -u postgres -- pg_dumpall --globals-only --no-role-passwords > "$stage/postgres-globals.sql"

/usr/bin/python3 - "$PHASE2_DB" "$stage/phase2.db" <<'PY'
import sqlite3
import sys

source, target = sys.argv[1:]
with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
    with sqlite3.connect(target) as dst:
        src.backup(dst)
        result = dst.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise SystemExit(f"phase2 sqlite quick_check failed: {result}")
PY

install -m 0644 "$PROTOCOL" "$stage/calibration_protocol_v1.json"
install -d -m 0700 "$stage/ledger-state"
cp -a --no-preserve=ownership "$LEDGER_STATE/." "$stage/ledger-state/"

# Bind every file-backed snapshot in PostgreSQL to a present, hash-matching CAS
# object in both the live state and this archive. This makes a successful backup
# a recoverable evidence set rather than only a database dump.
runuser -u postgres -- psql -X -d "$PG_DATABASE" -At -F $'\t' \
  -c "SELECT storage_uri,content_hash FROM ledger.research_snapshot ORDER BY storage_uri,content_hash" \
  > "$stage/research-snapshot-storage.tsv"
snapshot_count=0
while IFS=$'\t' read -r storage_uri expected_hash; do
  [[ -n "$storage_uri" && "$expected_hash" =~ ^[0-9a-f]{64}$ ]] || {
    printf 'invalid research snapshot storage row\n' >&2
    exit 1
  }
  case "$storage_uri" in
    "file://$LEDGER_STATE/cas/"*) source_path="${storage_uri#file://}" ;;
    *) printf 'unsupported research snapshot storage URI: %s\n' "$storage_uri" >&2; exit 1 ;;
  esac
  [[ -f "$source_path" && ! -L "$source_path" ]] || {
    printf 'research snapshot CAS object missing: %s\n' "$source_path" >&2
    exit 1
  }
  relative_path="${source_path#"$LEDGER_STATE/"}"
  archived_path="$stage/ledger-state/$relative_path"
  [[ -f "$archived_path" && ! -L "$archived_path" ]] || {
    printf 'research snapshot CAS object missing from backup stage: %s\n' "$relative_path" >&2
    exit 1
  }
  [[ "$(sha256sum "$source_path" | awk '{print $1}')" == "$expected_hash" ]]
  [[ "$(sha256sum "$archived_path" | awk '{print $1}')" == "$expected_hash" ]]
  snapshot_count=$((snapshot_count + 1))
done < "$stage/research-snapshot-storage.tsv"
printf '%s\n' "$snapshot_count" > "$stage/research-snapshot-storage.count"
git -C "$APP_ROOT" bundle create "$stage/research-ledger.git.bundle" --all
git -C "$APP_ROOT" diff --binary --no-ext-diff > "$stage/research-ledger-worktree.patch"
git -C "$APP_ROOT" ls-files --others --exclude-standard -z > "$stage/untracked-files.list"
if [[ -s "$stage/untracked-files.list" ]]; then
  tar -C "$APP_ROOT" --null --files-from="$stage/untracked-files.list" -czf "$stage/research-ledger-untracked.tar.gz"
else
  tar -C "$APP_ROOT" --files-from=/dev/null -czf "$stage/research-ledger-untracked.tar.gz"
fi

install -d -m 0700 "$stage/systemd"
unit_count=0
while IFS= read -r -d '' unit; do
  install -m 0644 "$unit" "$stage/systemd/$(basename "$unit")"
  unit_count=$((unit_count + 1))
done < <(find /etc/systemd/system -maxdepth 1 -type f \( -name 'research-ledger*.service' -o -name 'research-ledger*.timer' -o -name 'research-system-phase2*.service' -o -name 'research-system-phase2*.timer' \) -print0)
(( unit_count > 0 )) || { printf 'no research systemd units found\n' >&2; exit 1; }

(
  cd "$stage"
  find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > SHA256SUMS
  sha256sum -c SHA256SUMS >/dev/null
)
tar -C "$stage" -czf "$TMP_ARCHIVE" .
archive_sha="$(sha256sum "$TMP_ARCHIVE" | awk '{print $1}')"
archive_bytes="$(stat -c '%s' "$TMP_ARCHIVE")"
[[ "$archive_sha" =~ ^[0-9a-f]{64}$ && "$archive_bytes" -gt 0 ]]
mv -T "$TMP_ARCHIVE" "$FINAL"
chown root:ubuntu "$FINAL"
chmod 0640 "$FINAL"

checksum_tmp="$HOURLY/.$NAME.sha256.tmp"
printf '%s  %s\n' "$archive_sha" "$NAME" > "$checksum_tmp"
chown root:ubuntu "$checksum_tmp"
chmod 0640 "$checksum_tmp"
mv -T "$checksum_tmp" "$FINAL.sha256"

state_tmp="$STATE_ROOT/.last-success.json.tmp"
printf '{"schemaVersion":1,"createdAt":"%s","file":"%s","sha256":"%s","bytes":%s}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$NAME" "$archive_sha" "$archive_bytes" > "$state_tmp"
chown root:researchledger "$state_tmp"
chmod 0640 "$state_tmp"
mv -T "$state_tmp" "$STATE_ROOT/last-success.json"
install -o root -g ubuntu -m 0640 "$STATE_ROOT/last-success.json" "$HOURLY/last-success.json"

find "$HOURLY" -maxdepth 1 -type f -mtime +14 \( -name 'research-ledger-*.tar.gz' -o -name 'research-ledger-*.tar.gz.sha256' \) -delete
printf 'BACKUP_STATUS=success file=%s sha256=%s bytes=%s\n' "$NAME" "$archive_sha" "$archive_bytes"
