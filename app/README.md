# app/ — quant-platform application (`qp`)

Current application kernel: FastAPI backend, PostgreSQL 16, Vue 3 frontend at
`/next/`, and systemd/nginx deployment. The current production release is
recorded in the deployment receipts; do not rely on this file for a version
number.

## Layout

- `backend/` — FastAPI + SQLAlchemy 2 + Alembic; package `qp`, CLI `qp`.
- `frontend/` — Vue 3 + TypeScript + Vite + Naive UI; base `/next/`.
- `deploy/` — systemd units, nginx locations, release/activation scripts and runbooks.

Schemas in database `qp`: `core`, `market`, `research`, `signals`, `execution`,
`reports`, `ops`, `notify`, `archive`. Auth is a single admin account plus
service tokens (`qpst_…`, sha256 stored). The session cookie is HttpOnly, Secure,
SameSite=Lax, Path=/next/.

## What runs

- **Market** — collection, Qlib freshness/read, source probes, calendar and
  health comparison. `market.health` is the aggregate status; Qlib is read-only
  from qp's perspective.
- **Signals** — EOD at 15:30, retry slot, watchdog, total-amount repair,
  outcome collection, execution digest and acceptance. The ETF record chain
  dual-writes `signals.chain_*`, builds the 15:55 snapshot and the 16:12
  five-session window. `signals/chain.py` is a compatibility facade over
  `chain_base`, `chain_writer` and `chain_snapshot`.
- **Research** — read-only imports (ledger, phase2, backtests, AI, profit,
  projects), ETF score import (20d/5d), research comparison and the read-only
  ETF/rotation/rule shadows. Research never creates orders or formal decisions.
- **Reports** — 15:50 EOD daily report and 17:40 ETF research shadow report are
  separate production deliveries. The combined-report mode exists but its
  production switch is off.
- **Notifications** — `notify.outbox` + `notify.delivery_attempt`. General
  notifications default to `shadow`; signal/report delivery is controlled by
  `QP_SIGNALS_NOTIFY_MODE` and is live in production. A failed live delivery is
  not silently rewritten: the outbox and attempt history keep the failure, and
  recovery is an explicit operator action.
- **Ops** — `ops.job_run` (advisory locks, dedup keys), same-day upstream gates,
  healthcheck, inventory/alerts, backup, offsite push and restore drill.

## Modes and boundaries

- `QP_RECORD_CHAIN=1` enables the record-chain dual-write.
- `QP_CHAIN_READS=1` reads the chain snapshot; `QP_CHAIN_READS_FALLBACK=0`
  disables the legacy fallback.
- `QP_JOB_GATES=1` enables same-day upstream gates. A gate currently proves a
  recent successful upstream run; it does not by itself prove the downstream
  consumed the right artifact.
- `QP_NOTIFY_MODE` controls general outbox rows; `QP_SIGNALS_NOTIFY_MODE`
  controls signal/report rows.
- ETF scores, rotation, rule-v2 and attribution are read-only research views.
  Attribution below 30 eligible samples is explicitly labelled insufficient.

## Tests and CI

```bash
cd app/backend
pytest                         # unit suite; repo-asset tests are skipped
pytest --run-assets -m assets  # docs, lockfiles, deploy units, frontend source
```

CI runs both suites, ruff check/format, frontend lint/Vitest/build, Playwright
smoke, Gitleaks and a blocking dependency audit. The coverage floor is 85%
(current coverage is above 88%). Runtime pins
live in `backend/requirements-runtime.lock`; the test lock additionally carries
`pytest`/`httpx`/`pgserver`.

## Releases and recovery

- Activation: `deploy/bin/qp-release-activate` checks the release and database
  head, switches `current`, restarts `qp-api`, polls health and optionally runs
  smoke; failure rolls the code/symlink back.
- Automatic rollback restores code and venv only. It does not downgrade the
  database; a migration rollback needs the DB backup/restore runbook.
- Build release venvs with `deploy/bin/qp-release-venv`; never copy the previous
  release's lock.
- Backup: `ops.backup` (`pg_dump -Fc`, 14 days). Offsite copy and restore drill
  are separate units; a DB restore time is not a whole-platform RTO.

## Documentation index

- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — architecture and daily timeline.
- [`deploy/CHAIN-SHADOW-RUNBOOK.md`](deploy/CHAIN-SHADOW-RUNBOOK.md) — chain
  window, compare and attribution commands.
- [`deploy/M4-LIVE-RUNBOOK.md`](deploy/M4-LIVE-RUNBOOK.md) — live notification
  switch/stop and rollback.
- [`deploy/M5-SHADOW-RUNBOOK.md`](deploy/M5-SHADOW-RUNBOOK.md) — research imports
  and R1–R10 comparison.
- [`deploy/RELEASE-ACTIVATION.md`](deploy/RELEASE-ACTIVATION.md),
  [`deploy/DEPLOY-LOCK.md`](deploy/DEPLOY-LOCK.md),
  [`deploy/BACKUP-RESTORE-RUNBOOK.md`](deploy/BACKUP-RESTORE-RUNBOOK.md).
- [`deploy/KNOWN-LIMITATIONS.md`](deploy/KNOWN-LIMITATIONS.md) — source and
  egress limits.
- [`deploy/DEPENDENCY-POLICY.md`](deploy/DEPENDENCY-POLICY.md) — blocking
  dependency audit, time-boxed exceptions and investigation items.
- [`deploy/EVIDENCE-MANIFEST.md`](deploy/EVIDENCE-MANIFEST.md) — external
  artifact identity checks.
