# Quant backup policy: NO_BACKUP

Decision date: 2026-09-01; scope clarified and executable entries retired 2026-09-16.

The owner has explicitly chosen not to run backups for Quant-Server.

Production contract:

- `stock-radar-sqlite-backup.timer` and `.service` must remain absent.
- No scheduled or manual business database backup may be started: SQLite,
  PostgreSQL, local copies, logical dumps, archive uploads and COS round-trips
  are all covered. A pre-release, migration or rollback-preparation label does
  not create an exception. Database restore/drill also needs separate approval.
- `trading-radar-sqlite-cos-backup.timer`, `quantdinger-pg-dump-daily.timer`
  and `quantdinger-pg-dump-monthly.timer` remain disabled/inactive.
  Legacy definitions may remain as history; do not enable or run them.
- Missing backup history is expected and must not be repaired or backfilled.
- A health check must not reinstall or enable backup units merely because they
  are absent.
- Restoring backup code or automation requires a new, explicit owner
  authorization that supersedes this decision.

Historical data:

- Existing local archives and COS objects were retained when automation was
  retired.
- They may be deleted only with separate, explicit authorization because that
  deletion is irreversible.

## Executable protection and limits (2026-09-16)

- `scripts/backup_quantdinger_postgres.sh` refuses with exit 78 before any
  file, Docker or cloud operation. The existing daily wrapper calls it under
  `set -e`, so no following retention deletion executes.
- `deploy/retired/backup_sqlite_to_cos.sh` is the tracked retired copy installed
  at `/home/ubuntu/scripts/backup_sqlite_to_cos.sh`. It refuses the same way.
- No environment variable or `--upload` flag bypasses these two entry guards.
  Re-enabling requires reviewed source changes and a new explicit decision,
  not simply re-enabling a timer.
- These guards cover the known entrypoints, not arbitrary root commands,
  direct `pg_dump`, old `.pre-*` snapshots, or historical custom release
  scripts. Agents must also read the repository AGENTS.md policy. This is not
  a system-wide security sandbox and must not be described as one.

## Release boundary

The 09/14 compatibility release evidence records an existing 2,014,116-byte
PostgreSQL dump. Its `prepare_release.py` requires/reuses it and
`deploy_backend.py` checks it; that proves a backup-dependent workflow, not
which invocation created the dump or the original user's approval. The ignored
`releases/backend-compat-20260914.1/` scripts are completed historical evidence,
NOT future templates. Leave the artifact and audit records intact. Future
releases may use retained immutable images, Git source/diffs, configuration
metadata, read-only schema descriptions and SELECT counts for verification;
they must not generate/export/restore business databases as an implicit step.
If a proposed migration cannot safely proceed under NO_BACKUP, stop and ask
for a separate decision rather than silently creating a backup.

Image/source/configuration manifests are release provenance, not business
database backups. Do not archive secrets or DB files together with them.
