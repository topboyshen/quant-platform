#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly APP_ROOT="${PHASE2_APP_ROOT:-/opt/research-system-phase2}"
readonly PHASE2_DB="${PHASE2_DB_PATH:-/var/lib/research-system-phase2/phase2.db}"
readonly PROTOCOL="${PHASE2_PROTOCOL_PATH:-$APP_ROOT/protocol/calibration_protocol_v1.json}"
readonly SYSTEMD_ROOT="${PHASE2_SYSTEMD_ROOT:-/etc/systemd/system}"
readonly BACKUP_ROOT="${PHASE2_BACKUP_ROOT:-/var/backups/research-system-phase2}"
readonly HOURLY="$BACKUP_ROOT/hourly"
readonly STATE_ROOT="${PHASE2_BACKUP_STATE_ROOT:-/var/lib/research-system-phase2-backup}"
readonly STAMP="${PHASE2_BACKUP_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
readonly NAME="research-system-phase2-$STAMP.tar.gz"
readonly FINAL="$HOURLY/$NAME"
readonly TMP_ARCHIVE="$HOURLY/.$NAME.tmp"
readonly ALLOW_NON_ROOT="${PHASE2_BACKUP_ALLOW_NON_ROOT:-0}"

fail() {
  printf 'PHASE2_BACKUP_STATUS=fail reason=%s\n' "$1" >&2
  exit 1
}

if [[ "$(id -u)" != "0" && "$ALLOW_NON_ROOT" != "1" ]]; then
  fail "backup_must_run_as_root"
fi

for required in \
  "$PHASE2_DB" \
  "$PROTOCOL" \
  "$APP_ROOT/app/server.py" \
  "$SYSTEMD_ROOT/research-system-phase2.service"; do
  [[ -f "$required" && ! -L "$required" ]] \
    || fail "required_input_missing_or_symlinked path=$required"
done
[[ -x "$APP_ROOT/.venv/bin/python" ]] \
  || fail "python_runtime_missing path=$APP_ROOT/.venv/bin/python"

for component in app protocol tests releases; do
  source_dir="$APP_ROOT/$component"
  [[ -e "$source_dir" ]] || continue
  [[ -d "$source_dir" && ! -L "$source_dir" ]] \
    || fail "runtime_component_invalid path=$source_dir"
  if [[ -n "$(find "$source_dir" -type l -print -quit)" ]]; then
    fail "runtime_component_contains_symlink path=$source_dir"
  fi
done

if [[ "$ALLOW_NON_ROOT" == "1" ]]; then
  install -d -m 0750 "$BACKUP_ROOT" "$HOURLY" "$STATE_ROOT"
else
  install -d -o root -g researchledger -m 0750 "$BACKUP_ROOT" "$HOURLY"
  install -d -o root -g researchledger -m 0750 "$STATE_ROOT"
fi

[[ ! -e "$FINAL" && ! -e "$FINAL.sha256" ]] \
  || fail "backup_name_already_exists file=$NAME"

if [[ "$ALLOW_NON_ROOT" == "1" ]]; then
  exec 9>"$STATE_ROOT/research-system-phase2-backup.lock"
else
  exec 9>"/run/lock/research-system-phase2-backup.lock"
fi
flock -n 9 || { printf 'PHASE2_BACKUP_STATUS=busy\n' >&2; exit 75; }

stage="$(mktemp -d "$STATE_ROOT/.stage-$STAMP-XXXXXX")"
checksum_tmp="$HOURLY/.$NAME.sha256.tmp"
state_tmp="$STATE_ROOT/.last-success.json.tmp"
published=0
cleanup() {
  rm -rf -- "$stage"
  rm -f -- "$TMP_ARCHIVE" "$checksum_tmp" "$state_tmp"
  if [[ "$published" != "1" ]]; then
    rm -f -- "$FINAL" "$FINAL.sha256"
  fi
}
trap cleanup EXIT

/usr/bin/python3 - "$PHASE2_DB" "$stage/phase2.db" "$stage/phase2-db-check.json" "$PROTOCOL" <<'PY'
import json
import sqlite3
import sys

source, target, report_path, protocol_path = sys.argv[1:]
with open(protocol_path, "r", encoding="utf-8") as protocol_file:
    json.load(protocol_file)
src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
dst = sqlite3.connect(target)
try:
    src.backup(dst)
    quick_check = dst.execute("PRAGMA quick_check").fetchone()[0]
    integrity_check = dst.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_key_violations = len(dst.execute("PRAGMA foreign_key_check").fetchall())
    if quick_check != "ok" or integrity_check != "ok" or foreign_key_violations:
        raise SystemExit(
            "phase2 sqlite validation failed: "
            f"quick={quick_check} integrity={integrity_check} "
            f"foreign_key_violations={foreign_key_violations}"
        )
    table_names = [
        row[0]
        for row in dst.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    counts = {}
    for name in table_names:
        quoted_name = '"' + name.replace('"', '""') + '"'
        counts[name] = dst.execute(f"SELECT count(*) FROM {quoted_name}").fetchone()[0]
    with open(report_path, "w", encoding="utf-8") as report:
        json.dump(
            {
                "foreignKeyViolations": foreign_key_violations,
                "integrityCheck": integrity_check,
                "quickCheck": quick_check,
                "tableCounts": counts,
            },
            report,
            sort_keys=True,
        )
        report.write("\n")
finally:
    dst.close()
    src.close()
PY

install -m 0644 "$PROTOCOL" "$stage/calibration_protocol_v1.json"
install -d -m 0700 "$stage/runtime"
for component in app protocol tests releases; do
  [[ -d "$APP_ROOT/$component" ]] || continue
  cp -a --no-preserve=ownership "$APP_ROOT/$component" "$stage/runtime/$component"
done

"$APP_ROOT/.venv/bin/python" --version > "$stage/python-version.txt" 2>&1
"$APP_ROOT/.venv/bin/python" -c \
  'import importlib.metadata as m; print("\n".join(sorted(d.metadata["Name"] + "==" + d.version for d in m.distributions() if d.metadata.get("Name"))))' \
  > "$stage/python-packages.txt"

install -d -m 0700 "$stage/systemd"
unit_count=0
while IFS= read -r -d '' unit; do
  install -m 0644 "$unit" "$stage/systemd/$(basename "$unit")"
  unit_count=$((unit_count + 1))
done < <(find "$SYSTEMD_ROOT" -maxdepth 1 -type f \
  \( -name 'research-system-phase2*.service' -o -name 'research-system-phase2*.timer' \) \
  -print0)
(( unit_count > 0 )) || fail "no_phase2_systemd_units_found"

dropin_dir="$SYSTEMD_ROOT/research-system-phase2.service.d"
if [[ -d "$dropin_dir" && ! -L "$dropin_dir" ]]; then
  install -d -m 0700 "$stage/systemd/research-system-phase2.service.d"
  while IFS= read -r -d '' dropin; do
    [[ ! -L "$dropin" ]] || fail "systemd_dropin_is_symlink path=$dropin"
    install -m 0644 "$dropin" "$stage/systemd/research-system-phase2.service.d/$(basename "$dropin")"
  done < <(find "$dropin_dir" -maxdepth 1 -type f -print0)
fi

(
  cd "$stage"
  find . -type f ! -name SHA256SUMS -print0 \
    | LC_ALL=C sort -z \
    | xargs -0 sha256sum > SHA256SUMS
  sha256sum -c SHA256SUMS >/dev/null
)

db_sha="$(sha256sum "$stage/phase2.db" | awk '{print $1}')"
protocol_sha="$(sha256sum "$stage/calibration_protocol_v1.json" | awk '{print $1}')"
[[ "$db_sha" =~ ^[0-9a-f]{64}$ && "$protocol_sha" =~ ^[0-9a-f]{64}$ ]] \
  || fail "source_hash_invalid"

tar -C "$stage" -czf "$TMP_ARCHIVE" .
archive_sha="$(sha256sum "$TMP_ARCHIVE" | awk '{print $1}')"
archive_bytes="$(stat -c '%s' "$TMP_ARCHIVE")"
[[ "$archive_sha" =~ ^[0-9a-f]{64}$ && "$archive_bytes" -gt 0 ]] \
  || fail "archive_validation_failed"

mv -T "$TMP_ARCHIVE" "$FINAL"
if [[ "$ALLOW_NON_ROOT" == "1" ]]; then
  chmod 0600 "$FINAL"
else
  chown root:researchledger "$FINAL"
  chmod 0640 "$FINAL"
fi

printf '%s  %s\n' "$archive_sha" "$NAME" > "$checksum_tmp"
if [[ "$ALLOW_NON_ROOT" == "1" ]]; then
  chmod 0600 "$checksum_tmp"
else
  chown root:researchledger "$checksum_tmp"
  chmod 0640 "$checksum_tmp"
fi
mv -T "$checksum_tmp" "$FINAL.sha256"

database_bytes="$(stat -c '%s' "$stage/phase2.db")"
protocol_bytes="$(stat -c '%s' "$stage/calibration_protocol_v1.json")"
printf '{"schemaVersion":1,"createdAt":"%s","file":"%s","sha256":"%s","bytes":%s,"databaseSha256":"%s","databaseBytes":%s,"protocolSha256":"%s","protocolBytes":%s,"sqliteCheck":"ok"}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$NAME" "$archive_sha" "$archive_bytes" \
  "$db_sha" "$database_bytes" "$protocol_sha" "$protocol_bytes" \
  > "$state_tmp"
if [[ "$ALLOW_NON_ROOT" == "1" ]]; then
  chmod 0640 "$state_tmp"
else
  chown root:researchledger "$state_tmp"
  chmod 0640 "$state_tmp"
fi
mv -T "$state_tmp" "$STATE_ROOT/last-success.json"

if [[ "$ALLOW_NON_ROOT" == "1" ]]; then
  install -m 0640 "$STATE_ROOT/last-success.json" "$HOURLY/last-success.json"
else
  install -o root -g researchledger -m 0640 "$STATE_ROOT/last-success.json" "$HOURLY/last-success.json"
fi

find "$HOURLY" -maxdepth 1 -type f -mtime +14 \
  \( -name 'research-system-phase2-*.tar.gz' -o -name 'research-system-phase2-*.tar.gz.sha256' \) \
  -delete

published=1
printf 'PHASE2_BACKUP_STATUS=success file=%s sha256=%s bytes=%s database_sha256=%s protocol_sha256=%s\n' \
  "$NAME" "$archive_sha" "$archive_bytes" "$db_sha" "$protocol_sha"
