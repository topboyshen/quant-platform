# nnG8 NO_BACKUP contract

Policy decision: stop the Ledger and Phase2 hourly backup schedules; retain every existing
archive, checksum, last-success record, journal, and failed audit state. Business mode remains
FROZEN. This is an intentional reduction in recovery coverage, not a backup failure or improved
disaster recovery. Production rollout and natural acceptance must be recorded separately.

## Authority and compatibility

`/etc/research-ledger/backup-policy.mode` is a root-owned ordinary file, mode 0644, in a root-owned
non-group/world-writable directory. Its only supported contents are REQUIRED and NO_BACKUP.
Deployment provisions it explicitly. Missing, symlinked, unreadable, writable-by-group/other or
invalid values fail closed. Deleting the policy must never silently restore permission.
Tests use isolated `RESEARCH_BACKUP_POLICY_FILE` fixtures; production units do not set that override.

REQUIRED retains the original backup contents, validation, freshness thresholds and retention.
NO_BACKUP skips before archive/state/cleanup writes. Output is skipped, not success.
The runtime health check still validates API health, disk limits, business mode and all four
business timers/services. It replaces backup freshness checks with strict disabled/inactive
checks for both backup timers and no live backup process. Historical failed service states with
MainPID=0 are retained and distinguished from active work.

## Exact schedules

- research-ledger-backup.timer: disabled/inactive, no NEXT.
- research-system-phase2-backup.timer: disabled/inactive, no NEXT.
- Do not delete/mask units or reset-failed.
- producer, settler, research-ledger-phase2-import, worker remain disabled/inactive/FROZEN.

## Deployment and recovery

The full setup script performs database migrations and is not a backup-policy installer.
Its final schedule step enables Ledger backup only under explicit REQUIRED; NO_BACKUP cannot
quietly revive it. Do not run setup, backup or restore to apply or test this policy.
Restore runbooks preserve NO_BACKUP on return and rollback. Any future REQUIRED change,
manual backup or timer resume needs separate approval, including Persistent catch-up handling.

Apply only in an independently checked quiescent interval with no backup service active or
pending and at least 120 seconds before the next health/backup trigger. Stage content before
that window; reread unit jobs/PIDs/Invocation and file hashes immediately before commit.
Install the read-only parser, explicit policy and backup entry guards before disabling exactly
the two timers; publish runtime-health after timer stop. Preserve old script evidence locally.
If any service/job becomes active or either timer stop fails, stop the rollout and report the
partial state; do not kill services, retry business work, reset failed or automatically restore
REQUIRED. Next natural healthcheck, not a manual run, is the acceptance authority.
