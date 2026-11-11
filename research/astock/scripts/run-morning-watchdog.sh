#!/usr/bin/env bash
# Retained CLI name only: missing runs are observed, never automatically replayed.
set -Eeuo pipefail
printf '%s\n' 'legacy_watchdog_disabled: use the existing user astock-xhs-source.timer; missing-run inspection belongs to the read-only status/alert path. This entry does not run catch-up or send messages.' >&2
exit 78
