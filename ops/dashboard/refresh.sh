#!/usr/bin/env bash
# Dashboard-only refresh: read-only service snapshot, then the sanitized export.
# Never touches business timers, reports, market collection or delivery.
# Single instance (flock) + systemd TimeoutStartSec; a failure leaves the last
# good export in place and only appends a line to the log, so the page keeps
# showing the old content with its real (and ageing) timestamps.
set -euo pipefail
umask 077

readonly OPS="/home/ubuntu/ops/dashboard/exporter"
readonly OUT="/var/lib/nng8-dashboard"
readonly GROUP="nng8-web"
readonly LOG="/home/ubuntu/ops/dashboard/refresh.log"
readonly LOCK="/home/ubuntu/ops/dashboard/refresh.lock"
readonly PY="/usr/bin/python3"
readonly PER_STEP_TIMEOUT=60

exec 9>"$LOCK"
if ! flock -n 9; then
  printf '%s skipped=busy\n' "$(date -Is)" >>"$LOG"
  exit 0
fi

started="$(date -Is)"
set +e
collect_output="$(timeout "$PER_STEP_TIMEOUT" "$PY" -B "$OPS/collect_services.py" \
                    --out "$OUT/services.json" --group "$GROUP" 2>&1)"
collect_code=$?
export_output=""
export_code=1
if [ "$collect_code" -eq 0 ]; then
  export_output="$(timeout "$PER_STEP_TIMEOUT" "$PY" -B "$OPS/export_dashboard.py" \
                     --source-root /home/ubuntu --out "$OUT" \
                     --health-json /var/lib/research-data-health/last.json \
                     --services-json "$OUT/services.json" --group "$GROUP" --history 7 2>&1)"
  export_code=$?
fi
set -e

{
  printf '%s collect=%d export=%d ' "$started" "$collect_code" "$export_code"
  printf 'collect:%s ' "$(printf '%s' "$collect_output" | tail -1 | cut -c1-200)"
  printf 'export:%s\n' "$(printf '%s' "$export_output" | tail -1 | cut -c1-300)"
} >>"$LOG"

# Bounded logs.
if [ "$(wc -l <"$LOG")" -gt 2000 ]; then
  tail -n 1000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
if [ "$collect_code" -ne 0 ] || [ "$export_code" -ne 0 ]; then
  exit 1
fi
exit 0
