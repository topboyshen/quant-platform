#!/usr/bin/env bash
# Run sealed research relay; route alerts through the unified research-chain alert.
# Does not loosen HMAC/validation. Does not catch-up send outside window.
set -euo pipefail
umask 077

readonly BUS_ROOT="${BUS_ROOT:-/home/ubuntu/signal-bus}"
readonly RELAY="${BUS_ROOT}/scripts/research_relay.py"
readonly ALERT="${BUS_ROOT}/scripts/research_chain_alert.py"
readonly PYTHON="${PYTHON:-/usr/bin/python3}"
readonly ENV_FILE="${RESEARCH_CHAIN_ENV_FILE:-${HOME}/.config/systemd/user/research-chain-alert.env}"

# Same target config the generation/post units load. Direct wrapper calls otherwise
# leave RESEARCH_CHAIN_TELEGRAM_CHAT unset and the sender stays uncertain.
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

set +e
out="$("$PYTHON" -B "$RELAY" --json 2>&1)"
rc=$?
set -e
printf '%s\n' "$out"

fields=()
while IFS= read -r field; do fields+=("$field"); done < <("$PYTHON" -c 'import json,sys
d={}
try:
  parsed=json.loads(sys.argv[1])
except (ValueError,TypeError):
  parsed=None
if isinstance(parsed,dict):
  d=parsed
for line in (() if d else sys.argv[1].splitlines()):
  try:
    candidate=json.loads(line)
  except (ValueError,TypeError):
    continue
  if isinstance(candidate,dict):
    d=candidate
print(d.get("status",""))
print(d.get("sent",""))
print(d.get("reason",""))
' "$out" 2>/dev/null || true)

r_status="${fields[0]:-}"
r_sent="${fields[1]:-}"
r_reason="${fields[2]:-}"

case "$r_status" in
  confirmed|already_confirmed|confirmed_from_query|skipped)
    echo "research_relay confirmed_or_not_scheduled; no alert needed"
    ;;
  held)
    case "$r_reason" in
      outside_send_window|already_confirmed|delivery_busy)
        echo "research_relay benign=$r_reason; no alert needed"
        ;;
      *)
        echo "research_relay held=$r_reason; triggering unified alert"
        "$PYTHON" -B "$ALERT" --phase post --relay-status held --json || true
        ;;
    esac
    ;;
  *)
    # Failures, uncertain HTTP acceptance, or malformed output all need the
    # same evidence-aware classifier. It owns deduplication, never POST retries.
    echo "research_relay unresolved_result; triggering unified alert"
    case "$r_status" in
      rejected|not_sent|failed|already_failed|ambiguous|accepted|already_accepted) alert_status="$r_status" ;;
      already_recorded_unverified|confirmed_unverified) alert_status="unverified" ;;
      *) alert_status="unknown" ;;
    esac
    "$PYTHON" -B "$ALERT" --phase post --relay-status "$alert_status" --json || true
    ;;
esac
exit "$rc"
