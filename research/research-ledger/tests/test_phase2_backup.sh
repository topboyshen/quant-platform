#!/usr/bin/env bash
set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly SUBJECT="${PHASE2_BACKUP_SUBJECT:-$PROJECT_ROOT/scripts/research-system-phase2-backup.sh}"
readonly TEST_TMP="$(mktemp -d)"
trap 'rm -rf "$TEST_TMP"' EXIT

# Preserve original REQUIRED assertions without relying on production policy.
export RESEARCH_BACKUP_POLICY_FILE="$TEST_TMP/backup-policy.mode"
printf 'REQUIRED\n' > "$RESEARCH_BACKUP_POLICY_FILE"
chmod 0600 "$RESEARCH_BACKUP_POLICY_FILE"

readonly APP_ROOT="$TEST_TMP/app-root"
readonly DATA_ROOT="$TEST_TMP/data"
readonly SYSTEMD_ROOT="$TEST_TMP/systemd"
readonly BACKUP_ROOT="$TEST_TMP/backups"
readonly STATE_ROOT="$TEST_TMP/state"

install -d \
  "$APP_ROOT/app" \
  "$APP_ROOT/protocol" \
  "$APP_ROOT/tests" \
  "$APP_ROOT/.venv/bin" \
  "$DATA_ROOT" \
  "$SYSTEMD_ROOT/research-system-phase2.service.d"

printf 'print("phase2 fixture")\n' > "$APP_ROOT/app/server.py"
printf 'fixture test\n' > "$APP_ROOT/tests/test_phase2.py"
printf '{"schemaVersion":1,"policy":"frozen"}\n' > "$APP_ROOT/protocol/calibration_protocol_v1.json"
printf '[Service]\nExecStart=/fixture\n' > "$SYSTEMD_ROOT/research-system-phase2.service"
printf '[Unit]\nStartLimitBurst=5\n' > "$SYSTEMD_ROOT/research-system-phase2.service.d/restart-limit.conf"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ "${1:-}" == "--version" ]]; then' \
  '  printf "Python 3.12.3\n"' \
  'elif [[ "${1:-}" == "-c" ]]; then' \
  '  printf "pip==24.0\n"' \
  'else' \
  '  exit 2' \
  'fi' \
  > "$APP_ROOT/.venv/bin/python"
chmod 0755 "$APP_ROOT/.venv/bin/python"

/usr/bin/python3 - "$DATA_ROOT/phase2.db" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as db:
    db.execute("CREATE TABLE batch (id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
    db.execute("INSERT INTO batch(status) VALUES ('complete')")
PY

run_backup() {
  local stamp="$1"
  PHASE2_APP_ROOT="$APP_ROOT" \
  PHASE2_DB_PATH="$DATA_ROOT/phase2.db" \
  PHASE2_SYSTEMD_ROOT="$SYSTEMD_ROOT" \
  PHASE2_BACKUP_ROOT="$BACKUP_ROOT" \
  PHASE2_BACKUP_STATE_ROOT="$STATE_ROOT" \
  PHASE2_BACKUP_STAMP="$stamp" \
  PHASE2_BACKUP_ALLOW_NON_ROOT=1 \
  bash "$SUBJECT"
}

readonly GOOD_STAMP=20260823T000000Z
good_output="$(run_backup "$GOOD_STAMP")"
[[ "$good_output" == *'PHASE2_BACKUP_STATUS=success'* ]]

readonly ARCHIVE_NAME="research-system-phase2-$GOOD_STAMP.tar.gz"
readonly ARCHIVE="$BACKUP_ROOT/hourly/$ARCHIVE_NAME"
[[ -s "$ARCHIVE" && -s "$ARCHIVE.sha256" && -s "$STATE_ROOT/last-success.json" ]]
(
  cd "$BACKUP_ROOT/hourly"
  sha256sum -c "$ARCHIVE_NAME.sha256" >/dev/null
)

install -d "$TEST_TMP/restored"
tar -C "$TEST_TMP/restored" -xzf "$ARCHIVE"
(
  cd "$TEST_TMP/restored"
  sha256sum -c SHA256SUMS >/dev/null
)

/usr/bin/python3 - "$TEST_TMP/restored/phase2.db" <<'PY'
import sqlite3
import sys

with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as db:
    assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("SELECT count(*) FROM batch").fetchone()[0] == 1
PY

grep -Fq '"sqliteCheck":"ok"' "$STATE_ROOT/last-success.json"
state_before="$(sha256sum "$STATE_ROOT/last-success.json" | awk '{print $1}')"

cp "$APP_ROOT/protocol/calibration_protocol_v1.json" "$TEST_TMP/protocol.valid.json"
printf '{invalid json\n' > "$APP_ROOT/protocol/calibration_protocol_v1.json"
if run_backup 20260823T010000Z >/dev/null 2>&1; then
  printf 'invalid protocol JSON unexpectedly produced a backup\n' >&2
  exit 1
fi
[[ ! -e "$BACKUP_ROOT/hourly/research-system-phase2-20260823T010000Z.tar.gz" ]]
install -m 0644 "$TEST_TMP/protocol.valid.json" "$APP_ROOT/protocol/calibration_protocol_v1.json"

mv "$APP_ROOT/protocol/calibration_protocol_v1.json" "$TEST_TMP/protocol.missing.json"
if run_backup 20260823T020000Z >/dev/null 2>&1; then
  printf 'missing protocol unexpectedly produced a backup\n' >&2
  exit 1
fi
[[ ! -e "$BACKUP_ROOT/hourly/research-system-phase2-20260823T020000Z.tar.gz" ]]
mv "$TEST_TMP/protocol.missing.json" "$APP_ROOT/protocol/calibration_protocol_v1.json"

mv "$DATA_ROOT/phase2.db" "$TEST_TMP/phase2.valid.db"
printf 'not a sqlite database\n' > "$DATA_ROOT/phase2.db"
if run_backup 20260823T030000Z >/dev/null 2>&1; then
  printf 'invalid database unexpectedly produced a backup\n' >&2
  exit 1
fi
[[ ! -e "$BACKUP_ROOT/hourly/research-system-phase2-20260823T030000Z.tar.gz" ]]
mv "$TEST_TMP/phase2.valid.db" "$DATA_ROOT/phase2.db"

state_after="$(sha256sum "$STATE_ROOT/last-success.json" | awk '{print $1}')"
[[ "$state_before" == "$state_after" ]]
[[ -z "$(find "$STATE_ROOT" -maxdepth 1 -type d -name '.stage-*' -print -quit)" ]]

printf 'PHASE2_BACKUP_TESTS=passed\n'
