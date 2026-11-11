#!/usr/bin/env bash
# QLib nightly SQL update followed by one bounded release fallback attempt.
# The SQL command is byte-for-byte the previously scheduled command. A
# fallback is considered only when that command exits non-zero.
set -u
umask 022

readonly QLIB_ROOT="/home/ubuntu/qlib"
readonly TRIGGER="${QLIB_ROOT}/scripts/release_fallback/fallback_trigger.py"
readonly LOCK="${QLIB_ROOT}/logs/release-fallback.lock"
readonly PROXY_ENV="/etc/qlib-release-proxy.env"

started="$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)"
/usr/bin/timeout --signal=TERM --kill-after=30s 25m \
  /usr/bin/nice -n 10 /usr/bin/ionice -c2 -n7 \
  /usr/bin/python3 -B "${QLIB_ROOT}/scripts/qlib_dolt_incremental/run.py"
rc=$?
printf 'qlib_nightly_run_exit=%s started=%s finished=%s\n' \
  "$rc" "$started" "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)"

if [ "$rc" -eq 0 ]; then
  exit 0
fi
if [ "$rc" -eq 75 ]; then
  # The shared status belongs to another live updater; never fallback on it.
  exit 0
fi

# Cron receives no HTTPS proxy: pam_env reads only /etc/environment, while the
# host proxy lives in ~/.bashrc. Load the release-only proxy here, after run.py
# has exited, so the SQL update keeps its previous no-proxy environment.
if [ -r "$PROXY_ENV" ]; then
  set -a
  . "$PROXY_ENV"
  set +a
fi

/usr/bin/flock -n "$LOCK" /usr/bin/python3 -B "$TRIGGER" \
  --phase nightly --run-rc "$rc" --run-started-at "$started"
trigger_rc=$?
printf 'release_fallback_nightly_exit=%s\n' "$trigger_rc"
exit 0
