#!/usr/bin/env bash
# research-data nightly snapshot dispatcher. Scheduled failures are notified
# only through the existing health-alert state machine. Explicit manual notify
# flags remain compatible, but the scheduled writer path never calls them.
set -euo pipefail
umask 077

readonly RD_ROOT="/home/ubuntu/research-data"
readonly PYTHON="${RD_ROOT}/.venv/bin/python"
SNAPSHOT="${RD_ROOT}/daily_snapshot.py"
readonly LOG="${RD_ROOT}/snapshot.log"
LAST_RUN="${HOME}/.local/state/research-data/last-run.json"
readonly CONTRACT_POLICY="${RD_ROOT}/config/snapshot-contract.json"
readonly CALENDAR_PROJECT_ROOT="${RESEARCH_DATA_CORE_CALENDAR_PROJECT_ROOT:-/home/ubuntu/AStock_AI_System}"
readonly HERMES_PYTHON="${HERMES_PYTHON:-/home/ubuntu/.hermes/hermes-agent/venv/bin/python}"

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
# No policy: keep the full contract and its original paths. Both contracts now
# use the bounded CLI entry and the same scheduled health-alert owner.
# A present but invalid policy blocks selection; it never silently falls back.
  # Always run the state guard: an accidentally deleted policy must not hide
  # existing core history or silently reactivate full after a cutover.
  if ! selected_contract="$("$PYTHON" "${RD_ROOT}/snapshot_contract.py" --policy "$CONTRACT_POLICY" --core-db "${RD_ROOT}/core-market.db")"; then
    printf 'snapshot contract policy invalid; no writer started\n' >>"$LOG"
    exit 78
  fi
  case "$selected_contract" in
    core)
      SNAPSHOT="${RD_ROOT}/core_market.py"
      LAST_RUN="${HOME}/.local/state/research-data/core-market/last-run.json"
      ;;
    full) ;;
    *) printf 'unknown snapshot contract selection; no writer started\n' >>"$LOG"; exit 78 ;;
  esac
readonly SNAPSHOT LAST_RUN
if [[ ! -f "$SNAPSHOT" ]]; then
  echo "snapshot script missing: $SNAPSHOT" >&2
  exit 127
fi

set +e
(
  cd "$RD_ROOT" || exit 127
  if [[ "$SNAPSHOT" == "${RD_ROOT}/core_market.py" ]]; then
    "$PYTHON" "$SNAPSHOT" --contract-policy "$CONTRACT_POLICY" --db "${RD_ROOT}/core-market.db" \
      --state-dir "${HOME}/.local/state/research-data/core-market" \
      --calendar-evidence "$CALENDAR_PROJECT_ROOT"
  else
    "$PYTHON" "$SNAPSHOT"
  fi
) >>"$LOG" 2>&1
exit_code=$?
set -e

# Both contracts report through the existing health-alert state machine.
# The independent 23:00 evidence check covers a missed/failed audit itself.
# Preserve the writer's exit code and same-run artifact; never send here.
exit "$exit_code"
