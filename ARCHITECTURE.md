# quant-platform architecture (2026-11-16)

This repository is a monorepo that imports the nnG8 trading-radar / QuantDinger /
research stacks as historical snapshots **and** carries the new unified kernel in
`app/`. Runtime paths on nnG8 are unchanged: services still run from their
original locations, and nothing in this repository writes to them.

- New kernel status: deployed; ETF research scores are a read-only shadow.
- Legacy module status: see [`app/deploy/DECOMMISSION-ROADMAP.md`](app/deploy/DECOMMISSION-ROADMAP.md).
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
- systemd timers live in `app/deploy/systemd`; the full production list is
  `systemctl list-timers --all` on nnG8.

### 2.3 Record chain (signals)

The ETF record chain is a controlled successor to `target_decision`:

- **Dual-write**: `signals.eod` writes `target_decision`, then
  `signals.chain.register_run` appends immutable `chain_*` rows.
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

### 2.4 Research shadows

- C13 rotation shadow (`signals.rotation_shadow`) — 250-day top-8, daily.
- Rule shadow v2 (`signals.rule_shadow`) — daily volume breakout, shadow only.
- ETF score shadow (`research.etf_score_run` / `etf_score`) — offline frozen
  model produces `latest.json` and `latest-5d.json`; qp imports with strict
  artifact/content checks. 20d and 5d are independent observation versions.
- Research imports (ledger, phase2, QuantDinger history) are read-only mirrors.

None of these writes an order, notification or formal decision by itself.

### 2.5 Reports and notifications

- 17:40 combined daily report: market/risk/signal from the watchdog plus the
  20d/5d ETF shadow and C13 block.
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

### 3.3 Daily report

```
signals.watchdog + market compare + chain window
  + ETF 20d/5d shadow + C13 rotation
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
| 15:50 | `qp-signals-rotation-shadow.timer` | C13 rotation |
| 15:55 | `qp-signals-chain-snapshot.timer` + export + index close | chain snapshot |
| 16:10 | `qp-signals-outcomes.timer` | outcome collection |
| 16:12 | `qp-signals-chain-window.timer` | 5-day window table |
| 16:15 / 16:17 | shadow/rule outcomes | shadow maturity |
| 16:30 | `qp-market-index_close.timer` | index close mirror |
| 16:40 | `qp-research-ledger.timer` | research ledger import |
| 16:45 | `qp-etf-score-daily.timer` | 20d/5d offline score |
| 16:55 | `qp-research-etf-score.timer` | score import |
| 17:30–18:10 | research backtests/AI/profit/projects/compare | read-only imports |
| 17:40 | `qp-research-etf-shadow.timer` | combined daily report |

A failed run is retried by the next slot; failures are recorded in
`ops.job_run` and are not removed from the observation denominator.

## 5. Failure semantics

- **Fail closed**: strict import contracts, artifact hashes, trading-calendar
  coverage, and missing-snapshot handling never silently advance state.
- **Immutable history**: chain rows and evidence links are append-only; a rerun
  adds an attempt/revision, not a rewrite.
- **Shadow-first**: new strategies run read-only with explicit metrics; no
  promotion without a pre-registered evaluation.
- **Secrets**: tokens/DSNs live in root:qp 0640 files, never in the repo.

## 6. Testing and CI

- Backend: `pytest` with an embedded PostgreSQL 16 (`pgserver`); 434 passed,
  1 skipped on 2026-11-16.
- Dependencies: `app/backend/requirements.lock` (uv-compiled) is the install
  source for CI and releases.
- Frontend: Vitest + `vue-tsc` + Vite build; Playwright smoke logs in and walks
  the top-level pages against a real migrated database.
- Security: ruff `E9/F63/F7/F82` + Gitleaks full-history scan.

## 7. Documentation index

- `app/README.md` — kernel quick start.
- `app/deploy/*-RUNBOOK.md` — M4–M15 operations.
- `app/deploy/CHAIN-SHADOW-RUNBOOK.md` — chain dual-write/read switch.
- `app/deploy/DECOMMISSION-ROADMAP.md` — legacy retirement.
- `archive/README.md` — archive policy.
