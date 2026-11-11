#!/usr/bin/env bash
# One 06:30 retry for a target persisted by the nightly fallback trigger.
# It refuses if the exact failure runId is no longer the current record.
set -u
umask 022

readonly QLIB_ROOT="/home/ubuntu/qlib"
readonly TRIGGER="${QLIB_ROOT}/scripts/release_fallback/fallback_trigger.py"
readonly LOCK="${QLIB_ROOT}/logs/release-fallback.lock"
readonly PROXY_ENV="/etc/qlib-release-proxy.env"

# Same release-only proxy bridge as run_nightly.sh: cron has no ambient HTTPS
# proxy, so promote the dedicated file only for the trigger call.
if [ -r "$PROXY_ENV" ]; then
  set -a
  . "$PROXY_ENV"
  set +a
fi

/usr/bin/flock -n "$LOCK" /usr/bin/python3 -B "$TRIGGER" --phase morning
rc=$?
printf 'release_fallback_morning_exit=%s finished=%s\n' \
  "$rc" "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)"
exit 0
