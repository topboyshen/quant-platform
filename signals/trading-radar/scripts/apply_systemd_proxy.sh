#!/usr/bin/env bash
set -euo pipefail

PROXY_URL="${RADAR_ALL_PROXY:-http://127.0.0.1:7890}"
MONITOR_SERVICE="${MONITOR_SERVICE:-}"
DASHBOARD_SERVICE="${DASHBOARD_SERVICE:-}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "请用 sudo 运行：sudo $0" >&2
  exit 1
fi

unit_exists() {
  systemctl cat "$1" >/dev/null 2>&1
}

unit_from_pid() {
  local pid="$1"
  [[ -n "${pid}" ]] || return 1
  systemctl status "${pid}" --no-pager 2>/dev/null \
    | sed -n 's/^.*Loaded: loaded (.*\/systemd\/system\/\([^;]*\.service\).*/\1/p' \
    | head -n 1
}

first_pid_matching() {
  local pattern="$1"
  ps -eo pid=,args= \
    | awk -v pat="${pattern}" '$0 ~ pat {print $1; exit}'
}

pick_existing_unit() {
  shift || true
  local candidate
  for candidate in "$@"; do
    if unit_exists "${candidate}"; then
      echo "${candidate}"
      return 0
    fi
  done
  return 1
}

if [[ -z "${MONITOR_SERVICE}" ]]; then
  monitor_pid="$(first_pid_matching 'radar_monitor_v3.py' || true)"
  MONITOR_SERVICE="$(unit_from_pid "${monitor_pid}" || true)"
fi
if [[ -z "${MONITOR_SERVICE}" ]]; then
  MONITOR_SERVICE="$(pick_existing_unit _ stock-radar.service radar-monitor.service trading-radar.service || true)"
fi

if [[ -z "${DASHBOARD_SERVICE}" ]]; then
  dashboard_pid="$(first_pid_matching 'dashboard/app.py|dashboard.wsgi|gunicorn.*dashboard' || true)"
  DASHBOARD_SERVICE="$(unit_from_pid "${dashboard_pid}" || true)"
fi
if [[ -z "${DASHBOARD_SERVICE}" ]]; then
  DASHBOARD_SERVICE="$(pick_existing_unit _ radar-dashboard.service dashboard.service trading-radar-dashboard.service stock-radar-dashboard.service || true)"
fi

if [[ -z "${MONITOR_SERVICE}" ]]; then
  echo "未找到监控 systemd 服务。请用 MONITOR_SERVICE=xxx.service 指定。" >&2
  exit 1
fi
if [[ -z "${DASHBOARD_SERVICE}" ]]; then
  echo "未找到 Dashboard systemd 服务。将只修复并重启 ${MONITOR_SERVICE}。" >&2
fi

echo "监控服务: ${MONITOR_SERVICE}"
if [[ -n "${DASHBOARD_SERVICE}" ]]; then
  echo "Dashboard 服务: ${DASHBOARD_SERVICE}"
fi

write_monitor_dropin() {
  local dir="/etc/systemd/system/${MONITOR_SERVICE}.d"
  mkdir -p "${dir}"
  cat > "${dir}/10-proxy.conf" <<EOF
[Service]
Environment=RADAR_ALL_PROXY=${PROXY_URL}
Environment=PYTHONUNBUFFERED=1
Environment=HEALTH_HOST=127.0.0.1
Environment=HTTP_PROXY=
Environment=HTTPS_PROXY=
Environment=ALL_PROXY=
Environment=http_proxy=
Environment=https_proxy=
Environment=all_proxy=
EOF
}

write_dashboard_dropin() {
  [[ -n "${DASHBOARD_SERVICE}" ]] || return 0
  local dir="/etc/systemd/system/${DASHBOARD_SERVICE}.d"
  mkdir -p "${dir}"
  cat > "${dir}/10-proxy.conf" <<EOF
[Service]
Environment=RADAR_ALL_PROXY=${PROXY_URL}
Environment=PYTHONUNBUFFERED=1
Environment=DASHBOARD_HOST=127.0.0.1
Environment=DASHBOARD_PORT=8081
Environment=HTTP_PROXY=
Environment=HTTPS_PROXY=
Environment=ALL_PROXY=
Environment=http_proxy=
Environment=https_proxy=
Environment=all_proxy=
EOF
}

write_monitor_dropin
write_dashboard_dropin

systemctl daemon-reload
systemctl restart "${MONITOR_SERVICE}"
if [[ -n "${DASHBOARD_SERVICE}" ]]; then
  systemctl restart "${DASHBOARD_SERVICE}"
fi

sleep 3
systemctl --no-pager --plain status "${MONITOR_SERVICE}" | sed -n '1,20p' || true
if [[ -n "${DASHBOARD_SERVICE}" ]]; then
  systemctl --no-pager --plain status "${DASHBOARD_SERVICE}" | sed -n '1,20p' || true
fi

ps -C python -C python3 -o pid,ppid,pgid,user,uid,stat,lstart,args \
  | grep -E '/(opt/trading_radar|home/ubuntu/trading-radar)/(radar_monitor_v3.py|dashboard/app.py)|gunicorn.*dashboard' \
  | grep -v grep || true
