# Release activation health gate (P1)

`app/deploy/bin/qp-release-activate` is the final switch step for a qp release.
It does **not** migrate the DB, run jobs, send notifications or touch business
data. The caller must take the DB backup and run `qp db upgrade` first.

## Preconditions

- New release has `backend/` and a working `venv/bin/qp`.
- Previous release is the current `/opt/qp/current` target.
- DB is already at the target migration and backed up.
- New release's runtime lock was installed with `qp-release-venv`.
- The database is at the release's Alembic head; activation checks this before
  switching and exits `DB_CHECK_FAILED` without moving `current` if it is not.

## Command

```bash
sudo app/deploy/bin/qp-release-activate \
  --release /opt/qp/releases/<new> \
  --previous /opt/qp/releases/<prev> \
  --health-url http://127.0.0.1:18800/api/health \
  --smoke 'cd /opt/qp/releases/<new>/frontend && npm run smoke'
```

The script:

1. validates both releases and the current symlink;
2. runs `qp db check` as the `qp` user (or `--db-check-cmd`) and refuses to
   switch on a revision mismatch;
3. atomically switches `current` with `ln -sfn ... current.tmp` + `mv -T`;
4. restarts `qp-api`;
5. polls `/api/health` up to 30 times;
6. optionally runs the smoke command;
7. on any failure switches `current` back to the previous release, restarts and
   re-checks health, then exits nonzero.

`--dry-run` validates the two release paths and current symlink without switching,
checking the DB, or restarting anything.  `--db-check-cmd` overrides the default
`runuser -u qp -- env QP_APP_DIR=<release>/backend <release>/venv/bin/qp db check`
for staging or nonstandard database names.  The explicit `QP_APP_DIR` keeps the
check on the new release's migrations even when the caller's cwd is the old release.

## Rollback semantics

The automatic rollback restores the previous **venv and code**; it does not
downgrade the database. If a migration was applied, use the DB backup/restore
runbook separately. A failed rollback health check is printed as
`ROLLBACK_HEALTH_FAILED` and remains a paging condition.

## Verification

- `bash -n app/deploy/bin/qp-release-activate`
- `tests/test_release_activation.py` covers dry-run symlink preservation and the
  atomic-switch/health/rollback markers.
- Existing deployment receipts should record the script's `ACTIVATE_OK` or
  `ROLLBACK_*` line.
