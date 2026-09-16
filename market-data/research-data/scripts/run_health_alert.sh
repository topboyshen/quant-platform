#!/usr/bin/env bash
# Read the sandboxed research-data health JSON and Telegram-alert on critical/unknown.
# Does not collect data, mutate snapshots, or claim Qlib recovered.
set -euo pipefail
umask 077

readonly HEALTH_JSON="${RESEARCH_DATA_HEALTH_JSON:-/var/lib/research-data-health/last.json}"
readonly PYTHON="${RESEARCH_DATA_PYTHON:-/home/ubuntu/research-data/.venv/bin/python}"
readonly HERMES_PYTHON="${HERMES_PYTHON:-/home/ubuntu/.hermes/hermes-agent/venv/bin/python}"

send_telegram() {
  local message="$1"
  if [[ "${ALERT_DRY_RUN:-0}" == "1" ]]; then
    printf '%s\n' "$message"
    return 0
  fi
  if [[ ! -x "$HERMES_PYTHON" ]]; then
    printf 'Hermes Python unavailable\n' >&2
    return 1
  fi
  printf '%s\n' "$message" | timeout 60 "$HERMES_PYTHON" -m hermes_cli.main send --to telegram --file - --quiet
}

if [[ "${1:-}" == "--dry-run-notify" ]]; then
  ALERT_DRY_RUN=1 send_telegram "research-data health 告警（dry-run）"
  exit 0
fi

if [[ ! -f "$HEALTH_JSON" ]]; then
  echo "health json missing" >&2
  exit 2
fi

eval "$("$PYTHON" - "$HEALTH_JSON" <<'PY'
import json, re, sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
status = data.get("status") if isinstance(data, dict) else "unknown"
if not isinstance(status, str) or not re.fullmatch(r"[a-z_]{1,32}", status):
    status = "unknown"
codes = []
if isinstance(data, dict):
    for issue in data.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        code = issue.get("code")
        if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code) and code not in codes:
            codes.append(code)
        if len(codes) >= 8:
            break
print("STATUS=" + status)
print("CODES=" + ",".join(codes))
PY
)"

case "$STATUS" in
  critical|unknown)
    msg="research-data 健康告警｜$(date '+%Y-%m-%d %H:%M:%S %Z')
状态：${STATUS}
问题：${CODES:-无}
文件：health last.json
说明：只读审计，不自动恢复。"
    send_telegram "$msg" || printf 'notify failed status=%s\n' "$STATUS" >&2
    ;;
esac
exit 0
