# Quant-Server operational constraints

Read `deploy/QUANT_BACKUP_POLICY.md` and `deploy/PRODUCTION_PIN.md` before
deployment, cleanup, recovery or backup-related work in this repository.

- Owner policy is **NO_BACKUP** for Quant, including SQLite/PostgreSQL,
  local/offsite, scheduled/manual and pre-release/migration copies. Do not
  create, upload, restore or drill business database backups without new
  explicit authorization. Do not infer an exception from “deploy” or “fix”.
- Missing or disabled backup jobs are intentional, not a health failure.
  Do not re-enable timers or reuse ignored historical release scripts that
  require dumps. Existing files are retained; deletion needs separate approval.
- Current application image identities are in `deploy/production_pin.json`;
  use the canonical `deploy/trading-radar-compose` entrypoint for authorized
  deployment. A host Git HEAD is not necessarily the running image source.
- Preserve unrelated dirty files and runtime artifacts. Never blanket-add,
  reset, restore deleted data or run broad prune/cleanup to make Git clean.
- Do not manually trigger, replay, backfill or recompute natural close tasks,
  change production proxies, restart services, push commits, or clear audit
  failed units unless the current request explicitly authorizes that action.
- Source/image/config manifests may be recorded without copying databases or
  secrets. If NO_BACKUP makes a requested change unsafe, report the blocker.

## Release drift prevention (accepted 2026-10-16)

- Read `deploy/RELEASE-WORKFLOW.md` before any source/config/image publication.
- Host Git HEAD and old release trees are not the accepted deployed baseline.
- Business-source changes use `/home/ubuntu/bin/trading-radar-publish` with an
  exact tested write-set and before/after hashes. Do not use broad live-tree
  checkout/restore/reset, rsync --delete, or whole-file conflict copying.
- The independent accepted-files baseline is outside Git. A mismatch means
  stop and reconcile; do not regenerate it or downgrade healthy images.
- Pin/Compose/guards/policy changes require a separately authorized, locked,
  exact baseline transition. The business publisher cannot authorize them.
