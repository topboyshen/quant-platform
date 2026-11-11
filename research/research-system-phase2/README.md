# Research System Phase2

Phase2 is a loopback-only research calibration service. It records forward-looking batches,
forecasts, independently reviewed outcomes, risk vetoes, and calibration suggestions in SQLite.
The HTTP surface is a read-only dashboard; producer and settler writes are separate command-line
jobs controlled by systemd timers.

The production policy is currently `FROZEN`. That state deliberately leaves the producer and
settler timers disabled/inactive while keeping the loopback dashboard available.

## Runtime contract

| Item | Production value |
|---|---|
| Python | 3.12; standard library only |
| Service user | `researchphase2:researchphase2` |
| Working directory | `/opt/research-system-phase2` |
| Database | `/var/lib/research-system-phase2/phase2.db` |
| Frozen protocol | `/opt/research-system-phase2/protocol/calibration_protocol_v1.json` |
| Listen address | `127.0.0.1:18768` |
| Dashboard unit | `research-system-phase2.service` |

The server calls schema initialization at startup, but the HTTP interface exposes no write route.
It has no authentication or TLS because loopback is the security boundary. Do not publish port
18768 directly to the public internet.

## HTTP interface

| Method | Path | Result |
|---|---|---|
| `GET` | `/healthz` | JSON health response |
| `GET` | `/api/dashboard` | JSON protocol, batch, and calibration summary |
| `GET` | `/` | Read-only HTML dashboard |
| `POST` | any path | `403 Forbidden` |
| any other `GET` | any path | `404 Not Found` |
| `PUT`, `DELETE`, `HEAD` | any path | `501 Not Implemented` |

Quick checks:

```bash
curl --fail --silent http://127.0.0.1:18768/healthz
curl --fail --silent http://127.0.0.1:18768/api/dashboard
systemctl status research-system-phase2.service --no-pager
```

## Repository layout

- `app/phase2.py`: schema, protocol enforcement, producer, settler, review, veto, and statistics.
- `app/server.py`: loopback dashboard and read-only HTTP routing.
- `app/worker.py`: producer and settler command-line entry point.
- `app/provision_manual_roles.py`: root-only manual capability provisioning.
- `protocol/`: frozen calibration protocol.
- `docs/DATABASE_SCHEMA.md`: authoritative table/constraint inventory and schema-change boundary.
- `docs/RESTORE_RUNBOOK.md`: checksum-first, fail-closed local recovery procedure.
- `tests/`: standard-library unit and HTTP endpoint tests.
- `systemd/`: source copies of the currently deployed Phase2 units and dashboard drop-ins.

Runtime state, credentials, virtual environments, bytecode, and SQLite data do not belong in Git.
The on-disk `releases/` directory is an incomplete, stale pre-Git deployment snapshot and is retained
outside version control only for historical inspection; it is not an authoritative source tree.

## Tests

```bash
cd /opt/research-system-phase2
PYTHONPATH=app .venv/bin/python -m unittest discover -s tests -v
```

The test suite uses temporary SQLite databases and does not touch production state.

## Schema and recovery

The database schema is documented in [`docs/DATABASE_SCHEMA.md`](docs/DATABASE_SCHEMA.md). The
source of truth remains `init()` in `app/phase2.py`; the project currently has no versioned
migration framework and does not set SQLite `PRAGMA user_version`. A schema change therefore needs
an explicit, reviewed migration before deployment. Starting the server is not a migration plan.

The production recovery procedure is documented in
[`docs/RESTORE_RUNBOOK.md`](docs/RESTORE_RUNBOOK.md). The current nnG8 backup policy is **NO_BACKUP**:
the independent `research-system-phase2-backup.timer` and Ledger backup timer remain
disabled/inactive. Their canonical scripts and units live in
`/opt/research-ledger`. Archives under `/var/backups/research-system-phase2` are on the same VPS as
the live database. They are useful local recovery points, but they are **not off-host disaster
recovery** and provide no protection from loss of the VPS, account, region, or attached storage.

The authoritative policy is `/etc/research-ledger/backup-policy.mode`, maintained by root.
Both backup entry points skip before any state/archive write under NO_BACKUP. Existing archives,
checksums, last-success records, and failed audit evidence are retained, but are not advertised
as current recovery points. Timer re-enablement, manual backup, and restore require separate
approval. A missing/invalid policy fails closed rather than reverting to REQUIRED.

Never exercise the production restore steps as a health check. A real restore requires an approved
maintenance window, a selected immutable archive, a stopped/quiescent database, and completion of
all acceptance checks before normal service is declared restored.

## Operational boundaries

- `research-system-phase2-producer.timer` and `research-system-phase2-settler.timer` must remain
  disabled/inactive while `/etc/research-ledger/runtime-health.mode` is `FROZEN`.
- Manual review, risk veto, and calibration approval use independent capabilities; they are not
  exposed by the HTTP server.
- Enabling business timers is a policy change, not a routine deployment step.
- The Phase2 dashboard, producer, and settler units under `systemd/` are canonical here. Independent
  backup and cross-system health units remain owned by the `/opt/research-ledger` repository.
- Producer code gates at 15:20 Asia/Shanghai and settler code gates at 15:40. The deployed timers
  retry at 15:35/15:50 and 15:42/15:57 respectively. Dashboard `next_*` values show code gates,
  not the later systemd retry schedule.
- `HOLIDAYS_2026` is hard-coded. The calendar and its tests must be updated before 2027.
- A protocol change requires a new version and content hash; never rewrite the frozen protocol in
  place. `PENDING_REVIEW` outcomes do not count toward formal calibration. Stable calibration needs
  10 independent batches, 300 approved forecasts, and at least 20 observations per displayed bin.

This repository is a local production-source baseline. No remote push is implied by a local commit.
