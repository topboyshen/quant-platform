#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
cd /home/ubuntu/trading-radar
if [ -f /etc/systemd/system/stock-radar.env ]; then
  set -a
  source /etc/systemd/system/stock-radar.env
  set +a
fi
export RADAR_ALL_PROXY="${RADAR_ALL_PROXY:-http://127.0.0.1:7890}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=/home/ubuntu/trading-radar
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
if [[ "${1:-}" == "fault" ]]; then
  exec /home/ubuntu/trading-radar/runtime/current-swing/bin/python -B \
    /home/ubuntu/releases/daily-report-tracking-release-20261109.2/candidate/notify_fault.py
fi
if (( $# != 0 )); then exit 64; fi
exec /home/ubuntu/trading-radar/runtime/current-swing/bin/python -B \
  /home/ubuntu/releases/daily-report-tracking-release-20261109.2/candidate/run_summary.py scheduled
