#!/usr/bin/env bash
# Run sealed research relay; alert once when held for missing/invalid seal artifacts.
# Does not loosen HMAC/validation. Does not catch-up send outside window.
set -euo pipefail
umask 077

readonly BUS_ROOT="${BUS_ROOT:-/home/ubuntu/signal-bus}"
readonly RELAY="${BUS_ROOT}/scripts/research_relay.py"
readonly ALERT="${BUS_ROOT}/scripts/morning_seal_alert.sh"
readonly PYTHON="${PYTHON:-/usr/bin/python3}"

set +e
out="$("$PYTHON" -B "$RELAY" --json 2>&1)"
rc=$?
set -e
printf '%s\n' "$out"

mapfile -t fields < <("$PYTHON" -c 'import json,sys
try:
  d=json.loads(sys.argv[1])
except Exception:
  d={}
print(d.get("status",""))
print(d.get("sent",""))
print(d.get("reason",""))
' "$out" 2>/dev/null || true)

r_status="${fields[0]:-}"
r_sent="${fields[1]:-}"
r_reason="${fields[2]:-}"

if [[ "$r_status" == "held" && "$r_sent" == "0" ]]; then
  case "$r_reason" in
    required_artifact_or_config_invalid|sealed_report_proof_invalid|today_run_missing|formal_report_path_invalid)
      ALERT_DRY_RUN="${ALERT_DRY_RUN:-0}" "$ALERT" relay_held || true
      ;;
  esac
fi

exit "$rc"
