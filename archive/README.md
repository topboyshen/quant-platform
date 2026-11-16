# archive/ (reserved)

**Nothing has been moved here yet.** Legacy modules remain at their repository
paths in Phase 1 so that no runtime path or import mapping changes.

## Policy

- `archive/` holds frozen historical snapshots only; nothing here is imported,
  executed, built or deployed.
- A module moves here only after its runtime is stopped (Phase 2 of
  [`app/deploy/DECOMMISSION-ROADMAP.md`](../app/deploy/DECOMMISSION-ROADMAP.md))
  and has passed the 30-day read-only retention.
- Each moved module keeps a manifest: original path, source repo/commit, import
  mode, move date, and the `qp decommission inventory` sha256.
- No secrets, databases, logs, `node_modules`, virtualenvs or files > 5 MB.
- Data deletion is explicitly out of scope; see Phase 4 of the roadmap.

## Why the directory exists now

The review asked for archive isolation; creating the policy early makes the
destination and rules explicit without a disruptive `git mv` that could break a
live unit or an import job.
