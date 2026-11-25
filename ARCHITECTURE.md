# quant-platform architecture (2026-11-25)

This repository is a monorepo that imports the nnG8 trading-radar / QuantDinger /
research stacks as historical snapshots **and** carries the new unified kernel in
`app/`. Runtime paths on nnG8 are unchanged: services still run from their
original locations, and nothing in this repository writes to them.

- New kernel status: `0.3.118-outcome-window-6f60f5a8` deployed; ETF research scores are a read-only shadow.
- Freeze/observation baseline: [`app/deploy/FREEZE-BASELINE-2026-11-24.md`](app/deploy/FREEZE-BASELINE-2026-11-24.md) (single source for the current release, freeze start and review date).
- Legacy module status: see [`app/deploy/DECOMMISSION-ROADMAP.md`](app/deploy/DECOMMISSION-ROADMAP.md).
- Feature freeze started at 2026-11-25 11:03 CST; `.117`/`.118` are in-freeze correctness fixes; no M9 Phase 2 stop has been executed.
- No strategy in `app/` is promoted to a trade signal; execution stays outside.

## 1. Repository map

| Path | Role | Status |
|---|---|---|
| `app/backend` | FastAPI + SQLAlchemy 2 + Alembic, package `qp`, CLI `qp` | **active kernel** |
| `app/frontend` | Vue 3 + TypeScript + Naive UI, served under `/next/` | **active kernel** |
| `app/deploy` | systemd units, runbooks, env example | **active docs** |
| `app/deploy/DECOMMISSION-ROADMAP.md` | legacy retirement plan | **new** |
| `market-data/research-data` | research/qlib data collectors | active legacy source |
| `market-data/qlib-scripts` | nightly qlib update | active legacy |
| `research/astock` | AStock morning chain | active legacy until M9 |
| `research/research-ledger` | research ledger source | frozen import-only source |
| `research/research-system-phase2` | phase2 run state | frozen import-only source |
| `signals/trading-radar` | QuantDinger/radar legacy stack | retired runtime, data retained |
| `signals/signal-bus` | legacy signal bus | active legacy until M9 |
| `execution/quantdinger` | vendor QuantDinger snapshot | retired runtime |
| `reports/daily-report-tracking` | legacy daily reports | frozen read-only source |
| `ops/*`, `web/*` | legacy ops/dashboard/web assets | mixed; see roadmap |
| `archive/` | reserved read-only archive | empty (nothing moved yet) |

## 2. The `app/` kernel

### 2.1 Backend

FastAPI application with a single PostgreSQL 16 database, split by schema:

| Schema | Purpose |
|---|---|
| `core` | users/sessions/service tokens |
| `market` | instruments, calendar, index close, source health |
| `signals` | EOD runs, target decisions, record chain, shadows |
| `research` | imported research, C13/ETF score shadows |
| `reports` | report snapshots |
| `notify` | outbox and delivery state |
| `ops` | job definitions/runs, backups, health, alerts, inventory |
| `execution` | reserved execution mirror |
| `archive` | reserved |

Migrations are Alembic (`qp db upgrade`); the current production head is `0017`.

### 2.2 Jobs and timers

- Job registry (`qp.jobs.registry`) writes one `ops.job_run` per invocation.
- Per-job PostgreSQL advisory locks prevent overlapping runs; optional dedup
  keys make retries idempotent.
- `QP_JOB_GATES=1` adds same-day upstream checks for the signal chain; a
  missing/failed/stale upstream records status `blocked`. Default is off.
- systemd timers live in `app/deploy/systemd`; the full production list is
  `systemctl list-timers --all` on nnG8.

### 2.3 Record chain (signals)

The ETF record chain is a controlled successor to `target_decision`:

- **Dual-write**: `signals.eod` writes `target_decision`, then
  `signals.chain.register_run` appends immutable `chain_*` rows.
- **Implementation**: `signals/chain.py` is a compatibility facade over
  `chain_base` (identity/primitives), `chain_writer` (dual-write/outcomes) and
  `chain_snapshot` (snapshot assembly); the historical `qp.signals.chain`
  import surface is unchanged.
- **Identity**: deterministic `decision_uid`, input-evidence digest, universe and
  batch versions.
- **Snapshots**: `chain_snapshot` is the read envelope; `chain_snapshot_item`
  carries funnel/sample/progress/loss buckets.
- **Evidence links**: `chain_evidence_link` records `sidecar_kind=db_row`,
  expected vs observed digest, and `present/verified`, `present/failed` or
  `missing/pending`; `register_run` also takes a per-run advisory lock so
  concurrent writers cannot duplicate decisions or links.
- **Read switch**: `QP_CHAIN_READS=1` reads the snapshot. A missing snapshot
  falls back to `target_decision` with `source=legacy_fallback`,
  `fallback=true`, `stale_or_fallback=true`; `QP_CHAIN_READS_FALLBACK=0`
  restores the strict empty response.
- **Performance attribution**: `GET /api/signals/chain/attribution` and
  `qp chain attribution` are read-only. They use the latest outcome revision,
  current eligibility/state, sector and known signal types, report matured vs
  pending coverage, and withhold a performance claim below the 30-sample
  threshold. `migrated_v1` rows without `raw_signals` are explicitly
  `unknown`, never inferred; if they also lack chain state, signal/risk state
  falls back to `target_decision` and the source is labelled in the payload.

External artifacts (model files, frozen inputs, exported score payloads) are
not decisions, so they use a separate **artifact evidence manifest** rather than
`chain_evidence_link`: every required file is bound to host/owner/mode/bytes,
version and SHA-256, with `verified` only on an expected-identity match,
`unknown` when no expectation exists, and `missing`/`mismatch` kept explicit.
See [`app/deploy/EVIDENCE-MANIFEST.md`](app/deploy/EVIDENCE-MANIFEST.md).

The decision detail endpoint uses the `db_row` evidence link to expand legacy
`target_decision` / `trend_signal` details on demand, labelled
`source=legacy_target_decision` and `chain_native=false`. The snapshot and list
endpoints never load that detail, so the chain stays light.

### 2.4 Research shadows

- C13 rotation shadow (`signals.rotation_shadow`) — 250-day top-8, daily.
- Rule shadow v2 (`signals.rule_shadow`) — daily volume breakout, shadow only.
- ETF score shadow (`research.etf_score_run` / `etf_score`) — offline frozen
  model produces `latest.json` and `latest-5d.json`; qp imports with strict
  artifact/content checks. 20d and 5d are independent observation versions.
- Research imports (ledger, phase2, QuantDinger history) are read-only mirrors.

None of these writes an order, notification or formal decision by itself.

### 2.5 Reports and notifications

- 15:50 EOD report: market context, risk gate, signal funnel and research
  eligibility from `signals.watchdog`.
- 17:40 ETF research observation: 20d/5d score names, C13 NAV/positions and
  data quality. It is explicitly research-only and separate from the EOD
  operational report.
- Delivery goes through `notify.outbox`; `QP_SIGNALS_NOTIFY_MODE=shadow|live`
  controls live sends, `QP_NOTIFY_KILL_SWITCH` and the disabled file are hard
  stops.

## 3. Data flows

### 3.1 EOD signal chain

```
market data (legacy source)
  → signals.eod (15:30)
  → target_decision
  → chain.register_run (dual-write)
  → chain_snapshot (15:55)
  → /api/signals/etf-pool + /api/signals/chain/*
```

### 3.2 ETF research score

```
offline scorer (16:45, separate venv; frozen joblib + trading calendar)
  → /var/lib/qp/etf-score/latest.json (+ latest-5d.json)
  → research.etf_score_import (16:55)
  → research.etf_score_run / etf_score
  → /api/research/etf-score + /next/research/etf-score
```

### 3.3 Daily reports

```
signals.watchdog (15:50) → EOD report → notify.outbox → Telegram
ETF 20d/5d shadow + C13 rotation
  → research.etf_shadow_report (17:40)
  → notify.outbox → Telegram (shadow or live per switch)
```

## 4. Production daily timeline (scheduled, CST)

| Time | Unit | Effect |
|---|---|---|
| 03:50 | `qp-backup.timer` | pg_dump, 14-day retention |
| 15:30 | `qp-signals-eod.timer` | EOD decisions |
| 15:36 | `qp-signals-shadow-record.timer` | shadow record |
| 15:37 | `qp-signals-rule-shadow.timer` | rule shadow v2 |
| 15:40 | `qp-signals-watchdog.timer` | market/risk/signal audit |
| 15:47 | `qp-signals-repair.timer` | turnover repair |
| 15:48 | `qp-signals-eod-retry.timer` | EOD retry |
| 15:50 | `qp-signals-watchdog.timer` (second slot) | EOD report |
| 15:50 | `qp-signals-rotation-shadow.timer` | C13 rotation |
| 15:55 | `qp-signals-chain-snapshot.timer` + export | chain snapshot |
| 16:10 | `qp-signals-outcomes.timer` | outcome collection |
| 16:12 | `qp-signals-chain-window.timer` | 5-day window table |
| 16:15 / 16:17 | shadow/rule outcomes | shadow maturity |
| 16:30 | `qp-market-index_close.timer` | index close mirror |
| 16:40 | `qp-research-ledger.timer` | research ledger import |
| 16:45 | `qp-etf-score-daily.timer` | 20d/5d offline score |
| 16:55 | `qp-research-etf-score.timer` | score import |
| 17:30–18:10 | research backtests/AI/profit/projects/compare | read-only imports |
| 17:40 | `qp-research-etf-shadow.timer` | ETF research observation |

A failed run is retried by the next slot; failures are recorded in
`ops.job_run` and are not removed from the observation denominator.

## 5. Failure semantics

- **Fail closed**: strict import contracts, artifact hashes, trading-calendar
  coverage, and missing-snapshot handling never silently advance state.
- **Immutable history**: chain rows and evidence links are append-only; a rerun
  adds an attempt/revision, not a rewrite.
- **Evidence honesty**: a file that exists but has no expected hash is
  `unknown`, not verified; a failed/skipped run stays in the observation
  denominator. CI green proves code, not natural samples.
- **Shadow-first**: new strategies run read-only with explicit metrics; no
  promotion without a pre-registered evaluation.
- **Secrets**: tokens/DSNs live in root:qp 0640 files, never in the repo.

## 6. Testing and CI

- Backend: `pytest` with an embedded PostgreSQL 16 (`pgserver`). On 2026-11-21
  the default unit suite was **425 passed, 45 skipped** (44 repo-asset tests +
  1 conditional skip); `pytest --run-assets` was **469 passed, 1 skipped**.
  Coverage floor is 80% (measured 85%).
- Test isolation: `tests/conftest.py` pins runtime switches to test defaults
  (`QP_JOB_GATES=0`, shadow notification modes, `QP_RECORD_CHAIN=0`, ...), so a
  production release tree cannot change assertions through its environment.
  Tests that read repo-level docs, lockfiles, deploy units or frontend source
  are marked `assets`; default `pytest` skips them and CI runs
  `pytest --run-assets -m assets` after the unit suite. A release tree can
  therefore run `pytest` without carrying docs, frontend source or lockfiles.
- Dependencies: `app/backend/requirements.lock` (test extra) is the CI source;
  `app/backend/requirements-runtime.lock` is the release source, installed by
  `app/deploy/bin/qp-release-venv`. The runtime pins are a subset of the test
  lock (`tests/test_lockfiles.py`).
- Frontend: Vitest + `vue-tsc` + Vite build; Playwright smoke logs in and walks
  the top-level pages against a real migrated database.
- Security: ruff `E/F/I/UP/B` + `ruff format --check` + Gitleaks full-history
  scan. Dependency audit is blocking under
  [`app/deploy/DEPENDENCY-POLICY.md`](app/deploy/DEPENDENCY-POLICY.md), with
  time-boxed exceptions. Mypy blocks on a whitelist of new critical modules;
  the full jobs/signals/models run remains report-only as an existing baseline.

## 7. Recovery and RTO

The recovery drill restores a nightly custom-format dump into a scratch database,
replays `qp db upgrade`, checks schema/table/trigger/FK invariants, prints the
recovery point and runs M4 acceptance. It never writes live data and drops the
scratch DB.

**Measured 2026-11-18 07:06 CST** (same host, scratch DB, no DNS/TLS/network
rebuild):

| Step | Measured |
|---|---:|
| dump | `qp-20261118T035002.dump`, 4,821,024 bytes, sha256 `358dbefc…` |
| restore | 4 s |
| migration replay | 1 s |
| total drill | **7 s** |
| schema/tables/triggers | 100 tables, 10 signals triggers, live=restored |
| recovery point | EOD 2026-11-17, 1,240 decisions / 3,700 outcomes / 1,240 chain |
| acceptance | WARN (known A8 historical notification check), drill PASS |

This is **database recovery RTO**, not full-host RTO. A full-host rebuild also
needs OS/release venv, env/secrets, TLS, timers and smoke; measure that separately
before claiming a host-level RTO. Report: `/home/ubuntu/qp-restore-drill-20261118/report.txt`.

## 8. Documentation index

- `app/README.md` — kernel quick start.
- `app/deploy/*-RUNBOOK.md` — M4–M15 operations.
- `app/deploy/CHAIN-SHADOW-RUNBOOK.md` — chain dual-write/read switch.
- `app/deploy/EVIDENCE-MANIFEST.md` — external model/input/export evidence.
- `app/deploy/DEPLOY-LOCK.md` — runtime/test locks and release venv install.
- `app/deploy/KNOWN-LIMITATIONS.md` — accepted limitations and monitoring.
- `app/deploy/DECOMMISSION-ROADMAP.md` — legacy retirement.
- `app/deploy/JOB-GATES.md` — same-day upstream gates and rollback.
- `app/deploy/RELEASE-ACTIVATION.md` — health/smoke-gated release switch.
- `archive/README.md` — archive policy.
