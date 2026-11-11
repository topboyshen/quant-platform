#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trading-radar"
cd "$ROOT"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log "ERROR: required command not found: $1"
    exit 1
  fi
}

docker_cmd=()
compose_cmd=()

setup_docker_access() {
  if docker ps >/dev/null 2>&1; then
    docker_cmd=(docker)
    compose_cmd=(docker compose)
    return 0
  fi
  if sudo -n docker ps >/dev/null 2>&1; then
    log "Docker group is not active in this shell; using sudo docker for cutover"
    docker_cmd=(sudo docker)
    compose_cmd=(sudo docker compose)
    return 0
  fi
  log "ERROR: docker daemon is not accessible without sudo, and sudo docker is unavailable"
  exit 1
}

save_state() {
  mkdir -p "$ROOT/logs"
  local ts
  ts="$(date +%Y%m%d-%H%M%S)"
  log "Saving pre-cutover compose logs and process state"
  "${compose_cmd[@]}" logs --no-color > "$ROOT/logs/compose-before-cutover-$ts.log" 2>&1 || true
  {
    echo "# ps snapshot"
    ps -ef | grep -E 'radar_monitor_v3.py|dashboard/app.py|serve_ml.py' | grep -v grep || true
    echo
    echo "# listeners snapshot"
    ss -ltnp | grep -E ':8080|:8081|:8090|:8091|:6379|:9091|:3000' || true
    echo
    echo "# compose snapshot"
    "${compose_cmd[@]}" ps || true
  } > "$ROOT/logs/cutover-state-$ts.log" 2>&1
}

stop_unit_if_present() {
  local unit="$1"
  if systemctl list-unit-files "$unit" --no-legend 2>/dev/null | grep -q "^$unit"; then
    log "Stopping legacy systemd unit $unit"
    sudo systemctl stop "$unit" || true
  fi
}

terminate_python_processes_matching() {
  local pattern="$1"
  local pid
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    log "Sending SIGTERM to legacy process pid=$pid pattern=$pattern"
    kill -TERM "$pid" || true
  done < <(ps -C python -C python3 -o pid=,args= | grep -F "$pattern" | awk '{print $1}')
}

stop_legacy_services() {
  log "Stopping legacy monitor/dashboard/ml-serve gracefully"
  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files >/dev/null 2>&1; then
    stop_unit_if_present stock-radar.service
    stop_unit_if_present stock-radar-dashboard.service
    stop_unit_if_present radar-dashboard.service
    stop_unit_if_present radar-ml-serve.service
  fi

  terminate_python_processes_matching "$ROOT/radar_monitor_v3.py --config $ROOT/config.json"
  terminate_python_processes_matching "$ROOT/dashboard/app.py"
  terminate_python_processes_matching "$ROOT/serve_ml.py"
  cleanup_container_name_conflicts

  for _ in $(seq 1 15); do
    if ! ss -ltn | grep -Eq '127\.0\.0\.1:(8080|8081|8090|8091)\b'; then
      log "Legacy app ports 8080/8081/8090/8091 released"
      return 0
    fi
    sleep 1
  done

  log "ERROR: legacy app ports 8080/8081/8090/8091 are still occupied after graceful stop"
  ss -ltnp | grep -E ':8080|:8081|:8090|:8091' || true
  exit 1
}

cleanup_container_name_conflicts() {
  local project="trading-radar"
  local name label
  log "Checking for orphan containers that would conflict with Compose names"
  for name in radar-redis radar-prometheus radar-grafana radar-ml-serve radar-monitor radar-dashboard; do
    if ! "${docker_cmd[@]}" container inspect "$name" >/dev/null 2>&1; then
      continue
    fi
    label="$("${docker_cmd[@]}" inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$name" 2>/dev/null || true)"
    if [[ "$label" == "$project" ]]; then
      continue
    fi
    log "Removing orphan container $name before cutover; host data directories are preserved"
    "${docker_cmd[@]}" rm -f "$name"
  done
}

prepare_observability_data_dirs() {
  log "Preparing Prometheus/Grafana data directory ownership"
  mkdir -p "$ROOT/data/prometheus" "$ROOT/data/grafana"
  if command -v sudo >/dev/null 2>&1; then
    sudo chown -R 65534:65534 "$ROOT/data/prometheus" || true
    sudo chown -R 472:472 "$ROOT/data/grafana" || true
  else
    chown -R 65534:65534 "$ROOT/data/prometheus" || true
    chown -R 472:472 "$ROOT/data/grafana" || true
  fi
}

start_compose() {
  log "Starting full Docker Compose stack"
  if ! "${compose_cmd[@]}" up -d --build; then
    log "ERROR: docker compose up failed"
    "${compose_cmd[@]}" ps || true
    return 1
  fi
  "${compose_cmd[@]}" ps
}

check_url() {
  local name="$1"
  local url="$2"
  if curl -fsS --max-time 8 "$url" >/dev/null; then
    log "Health check OK: $name $url"
    return 0
  fi
  log "ERROR: health check failed: $name $url"
  return 1
}

verify_stack() {
  log "Waiting for compose services to become healthy"
  sleep 10
  "${compose_cmd[@]}" ps
  local failed=0
  check_url monitor http://127.0.0.1:8080/health || failed=1
  check_url dashboard http://127.0.0.1:8081/api/status || failed=1
  check_url ml-serve http://127.0.0.1:8090/health || failed=1
  check_url prometheus http://127.0.0.1:9091/-/healthy || failed=1
  check_url grafana http://127.0.0.1:3000/api/health || failed=1
  return "$failed"
}

rollback() {
  log "Cutover failed; starting one-minute rollback"
  "$ROOT/scripts/compose_rollback.sh" || true
}

compose_core_is_running() {
  local services
  services="$({ "${compose_cmd[@]}" ps --services --status running || true; } | sort | tr '\n' ' ')"
  [[ " $services " == *" monitor "* && " $services " == *" dashboard "* && " $services " == *" ml-serve "* ]]
}

main() {
  require_cmd docker
  require_cmd curl
  require_cmd ss
  setup_docker_access

  save_state
  if compose_core_is_running; then
    log "Compose core services already running; skipping legacy shutdown and reconciling stack"
  else
    stop_legacy_services
  fi
  prepare_observability_data_dirs

  if ! start_compose; then
    rollback
    exit 1
  fi

  if ! verify_stack; then
    log "Compose health verification failed"
    "${compose_cmd[@]}" logs --no-color --tail=160 monitor dashboard ml-serve prometheus grafana || true
    rollback
    exit 1
  fi

  log "Compose cutover complete"
}

main "$@"
