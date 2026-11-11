#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
if (( $# != 1 )); then exit 64; fi
case "$1" in profit|health) ;; *) exit 64 ;; esac
cd /home/ubuntu/trading-radar
# Same fixed configuration origin and transport policy as the daily sender.
# Never echo configuration values or use a second gateway transport.
if [ -f /etc/systemd/system/stock-radar.env ]; then
  set -a
  source /etc/systemd/system/stock-radar.env
  set +a
fi
export RADAR_ALL_PROXY="${RADAR_ALL_PROXY:-http://127.0.0.1:7890}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=/home/ubuntu/trading-radar
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
exec /home/ubuntu/trading-radar/runtime/current-swing/bin/python -B \
  /home/ubuntu/releases/daily-report-tracking-release-20261109.2/candidate/transport_envelope.py "$1"
