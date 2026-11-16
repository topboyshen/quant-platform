# app/ — quant-platform new application (M1 skeleton)

- `backend/` FastAPI + SQLAlchemy 2 + Alembic, package `qp`, CLI `qp` (`qp db upgrade`, `qp job list|run <name>`, `qp user import-legacy --stdin`).
  DB: host PostgreSQL 16, database `qp`, schemas core/market/research/signals/execution/reports/ops/notify/archive (M1: core, ops, notify populated; others empty stubs).
  Auth: single account (seeded from the QuantDinger admin; legacy bcrypt verified then rehashed to argon2id on first login), opaque session cookie `qp_session` (HttpOnly, Secure, SameSite=Lax, Path=/next/), service tokens `qpst_…` (sha256 stored).
  Jobs: registry + `ops.job_run` records, dedup key (partial unique index), pg advisory lock per job. Built-ins: `ops.healthcheck`, `ops.backup` (pg_dump -Fc, 14 days), `notify.send` (shadow only — never sends).
  M4 signals (shadow, not deployed): `signals.eod`, `signals.eod_retry`, `signals.watchdog`, `signals.total_amount_repair`, `signals.outcomes`, `signals.execution_digest`; read-only exporter `python -m qp.collect.signals`; comparison `qp shadow compare`. See `deploy/M4-SHADOW-RUNBOOK.md`; deployment is gated on M3 PASS.
  Tests: `pytest` (uses an embedded PostgreSQL 16 via `pgserver`). CI installs `requirements.lock` (test extra included). Releases install `requirements-runtime.lock` through `app/deploy/bin/qp-release-venv`; see `app/deploy/DEPLOY-LOCK.md`. Neither uses an unpinned install.
- `frontend/` Vue 3 + TypeScript + Vite + Naive UI, base `/next/`. `npm ci && npm run build` (build on the box, not on nnG8).
- `deploy/` systemd units (qp-api, qp-job@, qp-healthcheck.timer, qp-backup.timer), nginx `/next/` locations, env example.
