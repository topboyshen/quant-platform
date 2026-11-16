# quant-platform

Monorepo merging the nnG8 trading-radar / QuantDinger / research stacks (phase 2,
2026-11-11) **and** the new unified kernel in `app/` (M1–M15).

- New kernel architecture: [`ARCHITECTURE.md`](ARCHITECTURE.md)
- Legacy retirement plan: [`app/deploy/DECOMMISSION-ROADMAP.md`](app/deploy/DECOMMISSION-ROADMAP.md)
- Archive policy: [`archive/README.md`](archive/README.md)

Runtime paths on nnG8 are **unchanged** by this repo; services still run from
their original locations. The `app/` kernel is the only new runtime; legacy
directories below are imported snapshots or read-only sources.

## New kernel

`app/backend` (FastAPI + SQLAlchemy + Alembic) and `app/frontend` (Vue 3) serve
`/next/`. PostgreSQL 16 is split by schema; jobs run through `ops.job_run` with
advisory locks. ETF research scores are a read-only shadow; no order or formal
decision is produced.

Daily signal/report chain (CST): 15:30 EOD → 15:55 chain snapshot → 16:12 chain
window → 16:45/16:55 ETF score → 17:40 combined report. Full timeline and data
flows: [`ARCHITECTURE.md` §4](ARCHITECTURE.md#4-production-daily-timeline-scheduled-cst).

## Imported legacy modules

| Module | Path | Source on nnG8 | Import mode | Status (2026-11-16) |
|---|---|---|---|---|
| market data | `market-data/research-data` | `~/research-data` (git fabed60) | history + deployed-tree overlay | active legacy source |
| market data | `market-data/qlib-scripts` | `~/qlib/scripts` (non-git) | snapshot | active legacy |
| research | `research/astock` | `~/AStock_AI_System` (topboyshen/AStock_AI_System 14e8216) | snapshot (history had gitleaks findings) | active until M9 |
| research | `research/research-ledger` | `/opt/research-ledger` (git 7255251) | history + overlay | frozen import-only source |
| research | `research/research-system-phase2` | `/opt/research-system-phase2` (git 8837c0a) | history | frozen import-only source |
| signals | `signals/trading-radar` | `~/trading-radar` (topboyshen/trading-radar 679fd53) | snapshot (history had gitleaks findings) | retired runtime, data retained |
| signals | `signals/signal-bus` | `~/signal-bus` (topboyshen/signal-bus 2479057) | history | active until M9 |
| execution | `execution/quantdinger` | `~/QuantDinger` (vendor: brokermr810/QuantDinger 3551b8e + local changes) | snapshot (vendor; history had findings) | retired runtime |
| reports | `reports/daily-report-tracking` | `~/releases/daily-report-tracking-release-20261109.2/candidate` | snapshot | frozen read-only source |
| ops | `ops/dashboard` | `~/ops/dashboard` (git 03a2c17) | history + overlay | active ops |
| ops | `ops/opt/*` | `/opt/{nng8-operations,quant-nng8-sync-writer,nng8-sync,qd-*}` | snapshot | active ops |
| ops | `ops/nginx` | `/etc/nginx` (sanitized templates) | new | active ops |
| web | `web/shell` | new unified entry shell | new | frozen |
| web | `web/qd-frontend` | QD Vue source (Quant-Server release etf-named-triggers-20261106.1 build tree) | snapshot | retired runtime |
| web | `web/workbench` | `/opt/nng8-dashboard` static site | snapshot | frozen |

Excluded on import: secrets, `.env*`, keys/certs, databases, logs, data/,
runtime/, models, archives, node_modules, venvs, release evidence, files > 5 MB.
Original repos remain untouched (read-only; never pushed from here).
Third-party: QuantDinger (see its LICENSE/TRADEMARKS), qlib and hermes-agent stay
external.
