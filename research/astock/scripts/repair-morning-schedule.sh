#!/usr/bin/env bash
# Retained CLI name: do not mix source updates, live collection or catch-up
# with schedule management. The existing user timer is the only owner.
set -euo pipefail
printf '%s\n' \
  'legacy_schedule_repair_disabled: inspect the existing user astock-xhs-source.timer; this command does not pull code, collect data, install a schedule or catch up a report.' >&2
exit 78
