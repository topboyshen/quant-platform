#!/usr/bin/env bash
# Run sealed research relay; route alerts through the unified research-chain alert.
# Does not loosen HMAC/validation. Does not catch-up send outside window.
set -euo pipefail
umask 077

readonly BUS_ROOT="${BUS_ROOT:-/home/ubuntu/signal-bus}"
readonly RELAY="${BUS_ROOT}/scripts/research_relay.py"
readonly ALERT="${BUS_ROOT}/scripts/research_chain_alert.py"
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
    outside_send_window|already_confirmed|delivery_busy)
      echo "research_relay benign=$r_reason; no alert needed"
      ;;
    *)
      echo "research_relay held=$r_reason; triggering unified alert"
      "$PYTHON" -B "$ALERT" --phase post --json || true
      ;;
  esac
elif [[ "$r_status" == "confirmed" ]]; then
  echo "research_relay confirmed; no alert needed"
fi
exit 0
