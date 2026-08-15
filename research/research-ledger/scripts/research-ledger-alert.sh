#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly UNIT="${1:?failed unit required}"
readonly STATE_ROOT="${LEDGER_ALERT_STATE_ROOT:-/var/lib/research-ledger-alert}"
readonly COOLDOWN="${LEDGER_ALERT_COOLDOWN_SECONDS:-3600}"
readonly HERMES_PYTHON="${HERMES_PYTHON:-/home/ubuntu/.hermes/hermes-agent/venv/bin/python}"

[[ "$UNIT" =~ ^[A-Za-z0-9_.@:-]+$ ]] || { printf 'invalid unit name\n' >&2; exit 2; }
[[ "$COOLDOWN" =~ ^[0-9]+$ ]] || { printf 'invalid cooldown\n' >&2; exit 2; }
install -d -m 0700 "$STATE_ROOT"
safe_unit="${UNIT//[^A-Za-z0-9_.-]/_}"
exec 9>"$STATE_ROOT/$safe_unit.lock"
flock -n 9 || exit 0
now="$(date +%s)"
last=0
[[ -f "$STATE_ROOT/$safe_unit.last" ]] && read -r last < "$STATE_ROOT/$safe_unit.last"
[[ "$last" =~ ^[0-9]+$ ]] || last=0
if (( now - last < COOLDOWN )); then
  exit 0
fi

message="Research Ledger 告警｜$(date '+%Y-%m-%d %H:%M:%S %Z')
单元：$UNIT
状态：失败或业务停摆检查未通过
主机：$(hostname)
请检查：journalctl -u $UNIT"

if [[ "${ALERT_DRY_RUN:-0}" == "1" ]]; then
  printf '%s\n' "$message"
  exit 0
fi
[[ -x "$HERMES_PYTHON" ]] || { printf 'Hermes Python unavailable\n' >&2; exit 1; }
if ! printf '%s\n' "$message" | timeout 60 "$HERMES_PYTHON" -m hermes_cli.main send --to telegram --file - --quiet >/dev/null; then
  printf 'Hermes alert delivery failed\n' >&2
  exit 1
fi
tmp="$STATE_ROOT/.$safe_unit.last.tmp"
printf '%s\n' "$now" > "$tmp"
chmod 0600 "$tmp"
mv -T "$tmp" "$STATE_ROOT/$safe_unit.last"
