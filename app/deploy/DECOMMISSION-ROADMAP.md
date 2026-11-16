# Legacy decommission roadmap (2026-11-16)

Status: **planning + freeze**. No legacy service, timer, container or data has
been stopped or deleted by this repository. M9 provides stop-only tooling and
requires separate authorization.

Related: [`M9-DECOMMISSION-RUNBOOK.md`](M9-DECOMMISSION-RUNBOOK.md),
[`ARCHITECTURE.md`](../../ARCHITECTURE.md), [`archive/README.md`](../../archive/README.md).

## 1. Principles

1. **Stop, observe, then archive; delete last.** Stopping, archiving and data
   deletion are three separate approvals.
2. **Runtime paths do not move first.** The repo path is not the runtime path;
   moving a directory in Git must never break a live unit or import job.
3. **Freeze legacy code now.** New functionality goes into `app/`; legacy
   snapshots receive security-only fixes until they are stopped.
4. **Evidence over deletion.** Each phase records a `qp decommission inventory`
   manifest (units, containers, path sizes, sha256) before and after.
5. **No silent scope creep.** A module is marked retired only when its runtime
   is actually stopped; "replaced in app/" alone is not retirement.

## 2. Inventory and status

Source of truth for runtime: `systemctl list-timers --all`, `docker ps`, and
`qp decommission inventory`. The table below is a 2026-11-16 snapshot.

| Module | Runtime path | Replacement | Status | Next action |
|---|---|---|---|---|
| `signals/trading-radar` | `~/trading-radar` | `app/` signals + `/next/` | **retired runtime**; only `radar-quantdinger-postgres/redis` remain | keep data; decide pg/redis retention at Phase 4 |
| `execution/quantdinger` | `~/QuantDinger` | none (vendor) | **retired runtime** | keep snapshot; no new work |
| `research/astock` | `~/AStock_AI_System` | `app/` morning shadow (M8) | **active legacy** | M9 stop authorization; confirm no report consumer |
| `signals/signal-bus` | `~/signal-bus` | `app/` signals/notify | **active legacy** | M9 stop authorization |
| `reports/daily-report-tracking` | `~/releases/daily-report-tracking-release-20261109.2/candidate` | `app/` reports + 17:40 combined | **frozen source**; files may still be read | stop producers first, then freeze imports |
| `market-data/research-data` | `~/research-data` | `app/market` imports | **active legacy** | replace remaining collectors; keep qlib history |
| `market-data/qlib-scripts` | `~/qlib/scripts` | external qlib; `app/market.qlib_check` reads status | **active external** | do not fold into repo; monitor only |
| `research/research-ledger` | `/opt/research-ledger` | read-only `research.import_ledger` | **frozen import-only** | keep as source until import retires |
| `research/research-system-phase2` | `/opt/research-system-phase2` | read-only `research.import_phase2` | **frozen import-only** | same |
| `ops/dashboard` | `~/ops/dashboard` | `app/frontend` `/next/` | **active ops** | not in this decommission wave |
| `ops/opt/*`, `ops/nginx` | `/opt/*`, `/etc/nginx` | mixed | **active ops** | not in this wave |
| `web/qd-frontend` | QD frontend build tree | `/next/` | **retired runtime** | keep snapshot for reference |
| `web/workbench` | `/opt/nng8-dashboard` | `/next/` | **frozen** | archive after dashboard retirement |
| `web/shell` | new entry shell | `/next/` | **frozen** | keep for historical layout |

## 3. Phases

### Phase 0 — kernel and guardrails (complete)

- M1–M15 kernel, schema migrations, job registry, backups, CI (pytest,
  frontend, smoke, ruff, gitleaks).
- Read-only imports for ledger/phase2/QuantDinger history.
- No legacy mutation.

### Phase 1 — freeze and inventory (current)

- All new code in `app/`; legacy snapshots security-only.
- Run `qp decommission inventory` weekly/monthly and store the manifest with
  its sha256.
- Confirm each "active legacy" consumer with its timer/unit owner.
- Exit: every active legacy module has a named replacement and a stop order.

### Phase 2 — stop-only (M9 execution, requires separate authorization)

- Follow `M9-DECOMMISSION-RUNBOOK.md` §3: disable timers, stop containers.
- No `docker rm`, no data deletion, no config removal.
- Record before/after inventory and unit backups.
- Exit: 2 weeks without rollback and with qp as the only live source.

### Phase 3 — archive isolation (new PR, after Phase 2 + 30-day retention)

- Move retired directories under `archive/<module>/` with a manifest, only
  after the runtime path is no longer referenced by any unit or import.
- Keep provenance: original path, source repo/commit, import mode.
- Never make `archive/` importable or executable at runtime.
- Exit: `archive/` holds only frozen snapshots; no unit references it.

### Phase 4 — data deletion (separate consent, never automatic)

- List each path, size, reason for deletion, and whether rollback is possible.
- One approval per item group; no bulk delete command in this repo.

## 4. Change classification

| Change | Allowed in Phase 1 |
|---|---|
| Security fix in a legacy snapshot | yes, with a note; does not restart runtime |
| New feature in a legacy module | **no** — implement in `app/` |
| Runtime path move | **no** until Phase 3 |
| Stopping a timer/container | only Phase 2 with authorization |
| Deleting data | only Phase 4 with separate authorization |

## 5. Verification

- `qp decommission inventory --out ...` and `sha256sum` the manifest.
- Compare `/opt/qp/current`, `qp db upgrade` head, and unit list before/after.
- Current kernel CI must be green before and after each phase.
