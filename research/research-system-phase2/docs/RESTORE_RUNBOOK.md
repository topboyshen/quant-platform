# Phase2 local restore runbook

## Scope and authorization

This runbook restores the Phase2 SQLite database from an existing hourly archive. It is a production
change procedure, not a health check. Do not execute it without an approved maintenance window and
an explicitly selected archive.

The archives live under `/var/backups/research-system-phase2` on the **same VPS** as the live
database. They are local recovery points only. They do not protect against loss or compromise of the
VPS, account, region or attached storage, and they do not constitute off-host disaster recovery.
Creating an encrypted off-host copy, retention policy and independent restore drill is a separate
project requiring a destination and credential decision.

This procedure does not enable business timers, call an external model/source, create forecasts or
publish anything. It assumes the business mode remains `FROZEN` throughout.

## Recovery artifacts and ownership

| Item | Production contract |
|---|---|
| Live database | `/var/lib/research-system-phase2/phase2.db` |
| Live owner | `researchphase2:researchphase2` |
| State directory mode | `0700` |
| Database mode | `0644` inside the private state directory |
| Hourly archives | `/var/backups/research-system-phase2/hourly/` |
| Archive/checksum owner and mode | `root:researchledger`, `0640` |
| Backup state | `/var/lib/research-system-phase2-backup/last-success.json` |
| Backup implementation | `/opt/research-ledger/scripts/research-system-phase2-backup.sh` |

An archive contains at least `phase2.db`, `phase2-db-check.json`,
`calibration_protocol_v1.json`, `runtime/`, `systemd/`, Python version/package inventories and an
internal `SHA256SUMS` manifest. Archives older than 14 days are removed by the backup job.

## 1. Select and authenticate one archive

Use an exact filename; do not use a wildcard for a restore target. The example value below must be
replaced with the approved archive name.

```bash
set -euo pipefail

readonly BACKUP_DIR=/var/backups/research-system-phase2/hourly
readonly ARCHIVE_NAME=research-system-phase2-YYYYMMDDTHHMMSSZ.tar.gz
readonly ARCHIVE_PATH="$BACKUP_DIR/$ARCHIVE_NAME"
readonly CHECKSUM_PATH="$ARCHIVE_PATH.sha256"

case "$ARCHIVE_NAME" in
  research-system-phase2-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z.tar.gz) ;;
  *) printf '%s\n' 'invalid archive name' >&2; exit 64 ;;
esac

sudo test -f "$ARCHIVE_PATH" -a ! -L "$ARCHIVE_PATH"
sudo test -f "$CHECKSUM_PATH" -a ! -L "$CHECKSUM_PATH"
sudo -u root /bin/bash -c \
  'cd "$1" && /usr/bin/sha256sum -c -- "$2"' \
  restore-check "$BACKUP_DIR" "$ARCHIVE_NAME.sha256"
```

Stop immediately if the external checksum fails. Do not regenerate the checksum from the archive
being recovered.

## 2. Validate in an isolated staging directory

Create staging outside the live state directory. The following archive-member check rejects absolute
paths, traversal, links and device nodes before extraction.

```bash
readonly RESTORE_STAGE="$(sudo mktemp -d /var/tmp/phase2-restore.XXXXXXXX)"
sudo chmod 0700 "$RESTORE_STAGE"

sudo /usr/bin/python3 - "$ARCHIVE_PATH" <<'PY'
import pathlib
import sys
import tarfile

archive = sys.argv[1]
with tarfile.open(archive, "r:gz") as bundle:
    for member in bundle.getmembers():
        name = pathlib.PurePosixPath(member.name)
        if (
            name.is_absolute()
            or ".." in name.parts
            or member.issym()
            or member.islnk()
            or member.isdev()
        ):
            raise SystemExit(f"unsafe archive member: {member.name}")
PY

sudo tar --no-same-owner --no-same-permissions \
  -C "$RESTORE_STAGE" -xzf "$ARCHIVE_PATH"
sudo -u root /bin/bash -c \
  'cd "$1" && /usr/bin/sha256sum -c -- SHA256SUMS' \
  restore-internal "$RESTORE_STAGE"
sudo /usr/bin/python3 -m json.tool \
  "$RESTORE_STAGE/calibration_protocol_v1.json" >/dev/null
sudo /usr/bin/python3 -m json.tool \
  "$RESTORE_STAGE/phase2-db-check.json" >/dev/null
```

Validate SQLite without opening the production file:

```bash
sudo /usr/bin/python3 - "$RESTORE_STAGE/phase2.db" <<'PY'
import sqlite3
import sys

expected_tables = {
    "audit", "batch", "calibration_suggestion", "exclusion", "forecast",
    "manual_capability", "outcome", "protocol", "risk_veto", "run_state",
    "source_snapshot",
}
with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as db:
    quick = db.execute("PRAGMA quick_check").fetchone()[0]
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    user_version = db.execute("PRAGMA user_version").fetchone()[0]
if quick != "ok" or integrity != "ok" or foreign_keys:
    raise SystemExit(
        f"sqlite validation failed: quick={quick!r} integrity={integrity!r} "
        f"foreign_key_violations={len(foreign_keys)}"
    )
if tables != expected_tables:
    raise SystemExit(f"schema table mismatch: {sorted(tables)}")
if user_version != 0:
    raise SystemExit(f"unexpected schema user_version: {user_version}")
print("STAGED_SQLITE_VALIDATION=ok")
PY
```

Because the project currently has no versioned migration mechanism, compare the archived protocol
and runtime source with the deployed copies before a database-only restore:

```bash
sudo cmp -- "$RESTORE_STAGE/calibration_protocol_v1.json" \
  /opt/research-system-phase2/protocol/calibration_protocol_v1.json
sudo cmp -- "$RESTORE_STAGE/runtime/app/phase2.py" \
  /opt/research-system-phase2/app/phase2.py
```

If either comparison differs, stop. Restoring archived runtime/systemd definitions is a separate
reviewed deployment and must not be improvised inside a database restore.

## 3. Establish the quiescent cutover boundary

All checks below must pass before stopping the dashboard:

```bash
test "$(sudo tr -d '\n' </etc/research-ledger/runtime-health.mode)" = FROZEN
test "$(systemctl is-enabled research-system-phase2-producer.timer)" = disabled
test "$(systemctl is-active research-system-phase2-producer.timer)" = inactive
test "$(systemctl is-enabled research-system-phase2-settler.timer)" = disabled
test "$(systemctl is-active research-system-phase2-settler.timer)" = inactive
test "$(systemctl is-enabled research-system-phase2-backup.timer)" = enabled
test "$(systemctl is-active research-system-phase2-backup.timer)" = active
```

Abort if a producer, settler, manual-role provisioner or any other writer is running. Do not change
business mode or enable/trigger either business timer to make the checks pass.

Prevent a backup from overlapping cutover, then stop the read-only dashboard so its startup schema
initialization and SQLite handle are quiescent:

```bash
sudo systemctl stop research-system-phase2-backup.timer
sudo systemctl stop research-system-phase2.service

! sudo systemctl is-active --quiet research-system-phase2-backup.service
! sudo systemctl is-active --quiet research-system-phase2-producer.service
! sudo systemctl is-active --quiet research-system-phase2-settler.service
! sudo systemctl is-active --quiet research-system-phase2.service

if sudo fuser /var/lib/research-system-phase2/phase2.db; then
  printf '%s\n' 'database still has an open process; aborting' >&2
  exit 1
fi
```

If the procedure stops here, restart the dashboard and backup timer; no database cutover has
occurred.

## 4. Preserve the pre-restore state and cut over atomically

The rollback directory is mandatory. It is on the live filesystem so the original database and any
SQLite sidecars can be moved without overwriting them. This is a short-lived rollback point, not an
off-host backup.

```bash
readonly LIVE_DIR=/var/lib/research-system-phase2
readonly LIVE_DB="$LIVE_DIR/phase2.db"
readonly RESTORE_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly ROLLBACK_DIR="$LIVE_DIR/restore-rollback-$RESTORE_STAMP"
readonly CANDIDATE_DB="$LIVE_DIR/.phase2.db.restore-$RESTORE_STAMP"

sudo install -d -o researchphase2 -g researchphase2 -m 0700 "$ROLLBACK_DIR"
sudo mv -T -- "$LIVE_DB" "$ROLLBACK_DIR/phase2.db"
for sidecar in phase2.db-wal phase2.db-shm; do
  if sudo test -e "$LIVE_DIR/$sidecar"; then
    sudo mv -T -- "$LIVE_DIR/$sidecar" "$ROLLBACK_DIR/$sidecar"
  fi
done

sudo install -o researchphase2 -g researchphase2 -m 0644 \
  "$RESTORE_STAGE/phase2.db" "$CANDIDATE_DB"
sudo mv -T -- "$CANDIDATE_DB" "$LIVE_DB"
sudo sync -f "$LIVE_DB"
sudo stat -c '%U:%G %a %n' "$LIVE_DIR" "$LIVE_DB"
```

Required result: the directory is `researchphase2:researchphase2 700` and the database is
`researchphase2:researchphase2 644`. Do not copy archived credentials or automatically overwrite the
live protocol, Python environment, source tree or systemd units.

## 5. Validate and return the read-only service

Repeat integrity checks against the installed database as the service user, then start only the
dashboard:

```bash
sudo -u researchphase2 /usr/bin/python3 - "$LIVE_DB" <<'PY'
import sqlite3
import sys

with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as db:
    assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
print("LIVE_SQLITE_VALIDATION=ok")
PY

sudo systemctl start research-system-phase2.service
systemctl is-active --quiet research-system-phase2.service
curl --fail --silent http://127.0.0.1:18768/healthz
curl --fail --silent http://127.0.0.1:18768/api/dashboard >/dev/null
systemctl show research-system-phase2.service \
  -p ActiveState -p SubState -p MainPID -p NRestarts -p ExecMainStatus --no-pager
```

Confirm the governance boundary did not change, then return the independent backup schedule:

```bash
test "$(sudo tr -d '\n' </etc/research-ledger/runtime-health.mode)" = FROZEN
test "$(systemctl is-enabled research-system-phase2-producer.timer)" = disabled
test "$(systemctl is-active research-system-phase2-producer.timer)" = inactive
test "$(systemctl is-enabled research-system-phase2-settler.timer)" = disabled
test "$(systemctl is-active research-system-phase2-settler.timer)" = inactive

sudo systemctl start research-system-phase2-backup.timer
test "$(systemctl is-enabled research-system-phase2-backup.timer)" = enabled
test "$(systemctl is-active research-system-phase2-backup.timer)" = active
systemctl list-timers --all research-system-phase2-backup.timer --no-pager
```

Do not start the producer or settler services/timers. Review the dashboard journal for startup errors:

```bash
journalctl -u research-system-phase2.service --since '-10 minutes' \
  --priority=warning --no-pager
```

Keep the rollback directory until the maintenance change is accepted. Removing it is a separate,
explicit cleanup decision.

## 6. Failure rollback boundary

Rollback is safe only while the restored database has received no new producer, settler or manual
governance writes. Under `FROZEN`, with both business timers disabled and only the read-only dashboard
started, this boundary should remain intact. If any writer ran after cutover, do not blindly restore
the old file because that would discard new records; stop and perform a record-level reconciliation.

For a failure inside the safe boundary:

```bash
sudo systemctl stop research-system-phase2-backup.timer
sudo systemctl stop research-system-phase2.service

readonly FAILED_DIR="$LIVE_DIR/restore-failed-$RESTORE_STAMP"
sudo install -d -o researchphase2 -g researchphase2 -m 0700 "$FAILED_DIR"
sudo mv -T -- "$LIVE_DB" "$FAILED_DIR/phase2.db"
for sidecar in phase2.db-wal phase2.db-shm; do
  if sudo test -e "$LIVE_DIR/$sidecar"; then
    sudo mv -T -- "$LIVE_DIR/$sidecar" "$FAILED_DIR/$sidecar"
  fi
done

sudo mv -T -- "$ROLLBACK_DIR/phase2.db" "$LIVE_DB"
for sidecar in phase2.db-wal phase2.db-shm; do
  if sudo test -e "$ROLLBACK_DIR/$sidecar"; then
    sudo mv -T -- "$ROLLBACK_DIR/$sidecar" "$LIVE_DIR/$sidecar"
  fi
done
sudo chown researchphase2:researchphase2 "$LIVE_DB"
sudo chmod 0644 "$LIVE_DB"

sudo systemctl start research-system-phase2.service
curl --fail --silent http://127.0.0.1:18768/healthz
sudo systemctl start research-system-phase2-backup.timer
```

Re-run the live SQLite checks and all governance-boundary checks from section 5. Preserve the failed
candidate and the remaining rollback metadata for investigation; do not delete evidence during the
incident.

## Restore completion record

Record, outside the database, at least:

- approved archive filename and external SHA-256 result;
- internal `SHA256SUMS` result and staged SQLite checks;
- pre/post table counts from `phase2-db-check.json` and the live database;
- cutover and acceptance timestamps;
- restored file owner/mode;
- dashboard PID/status and `/healthz` result;
- `FROZEN` plus producer/settler timer states;
- backup timer restoration; and
- rollback directory disposition.

Do not record manual capability tokens or other secrets.
