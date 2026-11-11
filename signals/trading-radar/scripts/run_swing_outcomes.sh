#!/bin/bash
set -euo pipefail
umask 077
cd /home/ubuntu/trading-radar || exit 1

if [ -r /etc/systemd/system/stock-radar.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/systemd/system/stock-radar.env
    set +a
fi

runtime_python="${RADAR_RUNTIME_PYTHON:-/home/ubuntu/trading-radar/runtime/current-swing/bin/python}"
if [ ! -x "$runtime_python" ]; then
    echo "Swing runtime is not executable: $runtime_python" >&2
    exit 1
fi

export RADAR_ALL_PROXY="${RADAR_ALL_PROXY:-http://127.0.0.1:7890}"
export PYTHONUNBUFFERED=1
export PYTHONPATH=/home/ubuntu/trading-radar
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
exec timeout --signal=TERM --kill-after=2m 30m \
    "$runtime_python" \
    /home/ubuntu/trading-radar/scripts/evaluate_swing_outcomes.py \
    --config /home/ubuntu/trading-radar/config.swing.json "$@"
