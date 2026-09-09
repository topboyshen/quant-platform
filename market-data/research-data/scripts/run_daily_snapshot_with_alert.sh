#!/usr/bin/env bash
# research-data nightly snapshot wrapper with failure Telegram alert via Hermes.
# Reuses the same delivery path as research-ledger-alert.sh:
#   hermes_cli.main send --to telegram
set -euo pipefail
umask 077

readonly RD_ROOT="/home/ubuntu/research-data"
readonly PYTHON="${RD_ROOT}/.venv/bin/python"
readonly SNAPSHOT="${RD_ROOT}/daily_snapshot.py"
readonly LOG="${RD_ROOT}/snapshot.log"
readonly LAST_RUN="${HOME}/.local/state/research-data/last-run.json"
readonly HERMES_PYTHON="${HERMES_PYTHON:-/home/ubuntu/.hermes/hermes-agent/venv/bin/python}"

# Alert only on clear failures (match task policy).
readonly ALERT_STATUSES="rejected failed temporary_failure usage_error error"

usage() {
  echo "Usage: $0 [--notify-only MSG] [--dry-run-notify]" >&2
}

send_telegram() {
  local message="$1"
  if [[ "${ALERT_DRY_RUN:-0}" == "1" ]]; then
    printf '%s\n' "$message"
    return 0
  fi
  if [[ ! -x "$HERMES_PYTHON" ]]; then
    printf 'Hermes Python unavailable: %s\n' "$HERMES_PYTHON" >&2
    return 1
  fi
  printf '%s\n' "$message" | timeout 60 "$HERMES_PYTHON" -m hermes_cli.main send --to telegram --file - --quiet
}

read_last_run_field() {
  local field="$1"
  if [[ ! -f "$LAST_RUN" ]]; then
    printf ''
    return 0
  fi
  "$PYTHON" - "$LAST_RUN" "$field" <<'PY'
import json, sys
path, field = sys.argv[1], sys.argv[2]
try:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    print("")
    raise SystemExit(0)
val = data.get(field, "")
if val is None:
    val = ""
print(val if isinstance(val, str) else str(val))
PY
}

should_alert_status() {
  local status="$1"
  case " $ALERT_STATUSES " in
    *" $status "*) return 0 ;;
    *) return 1 ;;
  esac
}

if [[ "${1:-}" == "--notify-only" ]]; then
  shift
  msg="${*:-research-data alert wired}"
  send_telegram "$msg"
  exit $?
fi

if [[ "${1:-}" == "--dry-run-notify" ]]; then
  ALERT_DRY_RUN=1 send_telegram "research-data 告警（dry-run）｜$(date '+%Y-%m-%d %H:%M:%S %Z')"
  exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "python missing or not executable: $PYTHON" >&2
  exit 127
fi
if [[ ! -f "$SNAPSHOT" ]]; then
  echo "snapshot script missing: $SNAPSHOT" >&2
  exit 127
fi

set +e
(
  cd "$RD_ROOT" || exit 127
  "$PYTHON" "$SNAPSHOT"
) >>"$LOG" 2>&1
exit_code=$?
set -e

status="$(read_last_run_field status)"
reason="$(read_last_run_field reason)"
timestamp="$(read_last_run_field timestamp)"
run_id="$(read_last_run_field runId)"

alert=0
if [[ "$exit_code" -ne 0 ]]; then
  alert=1
fi
if should_alert_status "$status"; then
  alert=1
fi

# Never alert solely on skipped_existing / deferred / success / idempotent.
if [[ "$alert" -eq 1 ]]; then
  ts="${timestamp:-$(date '+%Y-%m-%d %H:%M:%S %Z')}"
  msg="research-data 告警｜${ts}
状态：${status:-unknown}
退出码：${exit_code}
原因：${reason:-无}
runId：${run_id:-无}
主机：$(hostname)"
  # Best-effort notify; do not mask original exit code on notify failure.
  send_telegram "$msg" || printf 'notify failed (exit=%s status=%s)\n' "$exit_code" "$status" >>"$LOG"
fi

exit "$exit_code"
