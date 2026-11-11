#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trading-radar"
cd "$ROOT"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

compose_cmd=()
if docker ps >/dev/null 2>&1; then
  compose_cmd=(docker compose)
elif sudo -n docker ps >/dev/null 2>&1; then
  log "Docker group is not active in this shell; using sudo docker for emergency rollback"
  compose_cmd=(sudo docker compose)
else
  log "Docker daemon not accessible; skipping compose shutdown and continuing legacy recovery"
fi

log "Saving compose logs before rollback"
mkdir -p "$ROOT/logs"
if [[ ${#compose_cmd[@]} -gt 0 ]]; then
  "${compose_cmd[@]}" logs --no-color > "$ROOT/logs/compose-rollback-$(date +%Y%m%d-%H%M%S).log" 2>&1 || true
else
  log "Compose logs skipped because Docker daemon is not accessible"
fi

log "Stopping compose stack"
if [[ ${#compose_cmd[@]} -gt 0 ]]; then
  "${compose_cmd[@]}" down --remove-orphans || true
else
  log "Compose shutdown skipped because Docker daemon is not accessible"
fi

start_systemd=false
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files >/dev/null 2>&1; then
  start_systemd=true
fi

unit_exists() {
  systemctl list-unit-files "$1" --no-legend 2>/dev/null | grep -q "^$1"
}

start_first_existing_unit() {
  local label="$1"
  shift
  local unit
  for unit in "$@"; do
    if unit_exists "$unit"; then
      log "Starting $label via systemd unit $unit"
      sudo systemctl start "$unit" || true
      return 0
    fi
  done
  log "No installed systemd unit found for $label; will rely on existing process or nohup fallback"
  return 1
}

start_nohup_fallbacks() {
  log "Ensuring legacy Python processes are running with nohup fallback"
  if ! pgrep -f "$ROOT/serve_ml.py" >/dev/null 2>&1; then
    nohup "$ROOT/venv/bin/python" "$ROOT/serve_ml.py" > "$ROOT/logs/ml-serve-rollback.log" 2>&1 &
  fi
  if ! pgrep -f "$ROOT/radar_monitor_v3.py" >/dev/null 2>&1; then
    nohup "$ROOT/venv/bin/python" "$ROOT/radar_monitor_v3.py" --config "$ROOT/config.json" > "$ROOT/logs/monitor-rollback.log" 2>&1 &
  fi
  if ! pgrep -f "$ROOT/dashboard/app.py" >/dev/null 2>&1; then
    nohup "$ROOT/venv/bin/python" "$ROOT/dashboard/app.py" > "$ROOT/logs/dashboard-rollback.log" 2>&1 &
  fi
}

if [[ "$start_systemd" == "true" ]]; then
  log "Starting legacy systemd services where installed"
  start_first_existing_unit "ml-serve" radar-ml-serve.service || true
  start_first_existing_unit "monitor" stock-radar.service || true
  start_first_existing_unit "dashboard" radar-dashboard.service stock-radar-dashboard.service || true
  start_nohup_fallbacks
else
  log "systemd unavailable; starting legacy Python processes with nohup"
  start_nohup_fallbacks
fi

log "Waiting for legacy services"
sleep 8

python3 - <<'PY'
from urllib.request import urlopen
checks = [
    ("monitor", "http://127.0.0.1:8080/health"),
    ("dashboard", "http://127.0.0.1:8081/api/status"),
    ("ml-serve", "http://127.0.0.1:8090/health"),
]
failed = []
for name, url in checks:
    try:
        with urlopen(url, timeout=5) as response:
            print(f"{name}: {response.status}")
            if response.status >= 400:
                failed.append(name)
    except Exception as exc:
        print(f"{name}: ERROR {type(exc).__name__}: {exc}")
        failed.append(name)
if failed:
    raise SystemExit(f"rollback health checks failed: {', '.join(failed)}")
PY

log "Rollback complete"
