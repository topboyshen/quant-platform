#!/usr/bin/env bash
set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly SUBJECT="${RUNTIME_HEALTH_SUBJECT:-$PROJECT_ROOT/scripts/research-ledger-runtime-health.sh}"
readonly TEST_TMP="$(mktemp -d)"
trap 'rm -rf "$TEST_TMP"' EXIT

readonly FIXTURE_PATH="$TEST_TMP/bin"
readonly MODE_FILE="$TEST_TMP/runtime-health.mode"
readonly LEDGER_STATE="$TEST_TMP/ledger-last-success.json"
readonly PHASE2_ROOT="$TEST_TMP/phase2-backups"
readonly PHASE2_STATE="$TEST_TMP/phase2-last-success.json"
readonly ARCHIVE_NAME="research-system-phase2-20260823T000000Z.tar.gz"
readonly ARCHIVE="$PHASE2_ROOT/hourly/$ARCHIVE_NAME"

install -d "$FIXTURE_PATH" "$PHASE2_ROOT/hourly"
printf 'FROZEN\n' > "$MODE_FILE"
printf '{}\n' > "$LEDGER_STATE"
printf 'phase2 archive fixture\n' > "$ARCHIVE"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  '[[ "${1:-}" == "show" ]] || exit 2' \
  'unit="${2:-}"' \
  'shift 2' \
  'property=""' \
  'while (( $# > 0 )); do' \
  '  case "$1" in' \
  '    -p|--property) property="${2:-}"; shift 2 ;;' \
  '    --property=*) property="${1#--property=}"; shift ;;' \
  '    *) shift ;;' \
  '  esac' \
  'done' \
  'case "$property" in' \
  '  LoadState) printf "loaded\n" ;;' \
  '  UnitFileState)' \
  '    if [[ "$unit" == research-ledger-backup.timer ]]; then' \
  '      printf "enabled\n"' \
  '    elif [[ "$unit" == research-system-phase2-backup.timer ]]; then' \
  '      printf "%s\n" "${FAKE_PHASE2_BACKUP_UNIT_FILE_STATE:-enabled}"' \
  '    else' \
  '      printf "disabled\n"' \
  '    fi' \
  '    ;;' \
  '  ActiveState)' \
  '    if [[ "$unit" == research-ledger-backup.timer ]]; then' \
  '      printf "active\n"' \
  '    elif [[ "$unit" == research-system-phase2-backup.timer ]]; then' \
  '      printf "%s\n" "${FAKE_PHASE2_BACKUP_ACTIVE_STATE:-active}"' \
  '    else' \
  '      printf "inactive\n"' \
  '    fi' \
  '    ;;' \
  '  *) exit 2 ;;' \
  'esac' \
  > "$FIXTURE_PATH/systemctl"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "{\"status\":\"ok\"}\n"' \
  > "$FIXTURE_PATH/curl"
chmod 0755 "$FIXTURE_PATH/systemctl" "$FIXTURE_PATH/curl"

write_state() {
  local created_at="$1"
  local archive_sha archive_bytes
  archive_sha="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
  archive_bytes="$(stat -c '%s' "$ARCHIVE")"
  printf '{"schemaVersion":1,"createdAt":"%s","file":"%s","sha256":"%s","bytes":%s,"sqliteCheck":"ok"}\n' \
    "$created_at" "$ARCHIVE_NAME" "$archive_sha" "$archive_bytes" > "$PHASE2_STATE"
}

run_health() {
  PATH="$FIXTURE_PATH:/usr/bin:/bin" \
  RUNTIME_HEALTH_MODE_FILE="$MODE_FILE" \
  RUNTIME_HEALTH_LEDGER_BACKUP_STATE="$LEDGER_STATE" \
  RUNTIME_HEALTH_PHASE2_BACKUP_STATE="$PHASE2_STATE" \
  RUNTIME_HEALTH_PHASE2_BACKUP_ROOT="$PHASE2_ROOT" \
  RUNTIME_HEALTH_MAX_BACKUP_AGE_SECONDS=9000 \
  bash "$SUBJECT"
}

write_state "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
healthy_output="$(run_health)"
[[ "$healthy_output" == *'RUNTIME_HEALTH=ok'* ]]
[[ "$healthy_output" == *'business_mode=FROZEN'* ]]
[[ "$healthy_output" == *"phase2_backup_file=$ARCHIVE_NAME"* ]]

write_state '2000-01-01T00:00:00Z'
if run_health >/dev/null 2>&1; then
  printf 'stale Phase2 backup state unexpectedly passed runtime health\n' >&2
  exit 1
fi

write_state "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'tampered\n' >> "$ARCHIVE"
if run_health >/dev/null 2>&1; then
  printf 'tampered Phase2 archive unexpectedly passed runtime health\n' >&2
  exit 1
fi

printf 'phase2 archive fixture\n' > "$ARCHIVE"
write_state "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if FAKE_PHASE2_BACKUP_UNIT_FILE_STATE=disabled \
  FAKE_PHASE2_BACKUP_ACTIVE_STATE=inactive \
  run_health >/dev/null 2>&1; then
  printf 'disabled Phase2 backup timer unexpectedly passed runtime health\n' >&2
  exit 1
fi

mv "$ARCHIVE" "$TEST_TMP/real-archive"
ln -s "$TEST_TMP/real-archive" "$ARCHIVE"
write_state "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if run_health >/dev/null 2>&1; then
  printf 'symlinked Phase2 archive unexpectedly passed runtime health\n' >&2
  exit 1
fi

printf 'RUNTIME_HEALTH_TESTS=passed\n'
