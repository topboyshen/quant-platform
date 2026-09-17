#!/usr/bin/env bash
# Morning seal/relay failure alerts via Hermes Telegram (research-data pattern).
# Categories: 报告已生成 / 密封失败 / 未发送. Never alerts on clean success.
# Usage: morning_seal_alert.sh draft_failure|relay_held|--dry-run-notify
set -euo pipefail
umask 077

readonly ASTOCK_ROOT="${ASTOCK_ROOT:-/home/ubuntu/AStock_AI_System}"
readonly HERMES_PYTHON="${HERMES_PYTHON:-/home/ubuntu/.hermes/hermes-agent/venv/bin/python}"
readonly STATE_DIR="${HOME}/.local/state/signal-bus"
readonly DEDUPE_FILE="${STATE_DIR}/morning-seal-alert-dedupe.json"
readonly PYTHON="${PYTHON:-python3}"

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR" 2>/dev/null || true

usage() { echo "Usage: $0 draft_failure|relay_held|--dry-run-notify" >&2; }

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

shanghai_today() { TZ=Asia/Shanghai date '+%Y-%m-%d'; }

read_json_field() {
  local file="$1" field="$2"
  if [[ ! -f "$file" ]]; then
    printf ''
    return 0
  fi
  "$PYTHON" - "$file" "$field" <<'PY'
import json, sys
path, field = sys.argv[1], sys.argv[2]
try:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    print("")
    raise SystemExit(0)
cur = data
for part in field.split("."):
    if not isinstance(cur, dict) or part not in cur:
        print("")
        raise SystemExit(0)
    cur = cur[part]
print(cur if isinstance(cur, str) else ("" if cur is None else str(cur)))
PY
}

already_alerted() {
  local day="$1" kind="$2"
  "$PYTHON" - "$DEDUPE_FILE" "$day" "$kind" <<'PY'
import json, sys
path, day, kind = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    data = {}
print("1" if data.get(f"{day}:{kind}") else "0")
PY
}

mark_alerted() {
  local day="$1" kind="$2"
  "$PYTHON" - "$DEDUPE_FILE" "$day" "$kind" <<'PY'
import json, os, sys
path, day, kind = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    data = {}
data[f"{day}:{kind}"] = True
keys = sorted(data)
if len(keys) > 60:
    for old in keys[:-40]:
        data.pop(old, None)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, sort_keys=True)
    fh.write("\n")
os.replace(tmp, path)
os.chmod(path, 0o600)
PY
}

classify_report() {
  local day="$1"
  local run="${ASTOCK_ROOT}/work/last-run.json"
  local occ="${ASTOCK_ROOT}/work/orchestrator/states/morning-report-${day}.json"
  local status target occ_status
  status="$(read_json_field "$run" status)"
  target="$(read_json_field "$run" targetDate)"
  occ_status="$(read_json_field "$occ" status)"
  if [[ "$status" == "success" && "$target" == "$day" && "$occ_status" == "success" ]]; then
    echo "报告已生成"
  elif [[ "$status" == "success" && "$target" == "$day" ]]; then
    echo "报告已生成(occurrence未对齐)"
  else
    echo "报告未确认成功(status=${status:-missing})"
  fi
}

classify_seal() {
  local day="$1"
  local xhs="${ASTOCK_ROOT}/work/xhs/last-run.json"
  local status target
  status="$(read_json_field "$xhs" status)"
  target="$(read_json_field "$xhs" targetDate)"
  if [[ "$status" == "draft_ready" && "$target" == "$day" ]]; then
    echo "密封成功"
  elif [[ -z "$status" ]]; then
    echo "密封失败(无xhs状态)"
  else
    echo "密封失败(status=${status}, target=${target:-none})"
  fi
}

MODE="${1:-}"
if [[ "$MODE" == "--dry-run-notify" ]]; then
  ALERT_DRY_RUN=1 send_telegram "AStock 晨间密封告警（dry-run）｜$(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S %Z')"
  exit 0
fi

if [[ "$MODE" != "draft_failure" && "$MODE" != "relay_held" ]]; then
  usage
  exit 2
fi

DAY="$(shanghai_today)"
REPORT_LINE="$(classify_report "$DAY")"
SEAL_LINE="$(classify_seal "$DAY")"

if [[ "$MODE" == "relay_held" && "$REPORT_LINE" != 报告已生成* ]]; then
  exit 0
fi

if [[ "$(already_alerted "$DAY" "$MODE")" == "1" ]]; then
  exit 0
fi

TS="$(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S %Z')"
if [[ "$MODE" == "draft_failure" ]]; then
  CATEGORY="密封失败"
  DETAIL="xhs-draft OnFailure：草稿密封失败，OnSuccess 通知/中继不会触发。"
else
  CATEGORY="未发送"
  DETAIL="research_relay held / sent=0（窗内未送达）。不声称已发送。"
fi

MSG="AStock 晨间链路告警｜${TS}
类别：${CATEGORY}
${REPORT_LINE}
${SEAL_LINE}
发送：未发送
说明：${DETAIL}
主机：$(hostname)
日：${DAY}"

if send_telegram "$MSG"; then
  if [[ "${ALERT_DRY_RUN:-0}" != "1" ]]; then
    mark_alerted "$DAY" "$MODE"
  fi
else
  printf 'notify failed mode=%s day=%s\n' "$MODE" "$DAY" >&2
  exit 1
fi
