# Legacy decommission roadmap (2026-11-24)

Status: **feature freeze active; Phase 2 not started**. No legacy service,
timer, container or data has been stopped or deleted by this repository. M9
provides stop-only tooling and requires separate authorization.

Current baseline: [`FREEZE-BASELINE-2026-11-24.md`](FREEZE-BASELINE-2026-11-24.md)
— the single source for the current production release, freeze start and earliest
Phase 2 review (currently `0.3.123-index-close-union-5f698f20`, freeze
2026-11-25 11:03 CST → earliest review 2026-12-09).

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
`qp decommission inventory`. The table below is a 2026-11-16 module snapshot; the runtime inventory is refreshed
in the freeze baseline above.

| Module | Runtime path | Replacement | Status | Next action |
|---|---|---|---|---|
| `signals/trading-radar` | `~/trading-radar` | `app/` signals + `/next/` | **mostly retired**; `quant-approved-signals.service` still active (8094); `radar-quantdinger-*` containers up | intake path confirmed via nginx `quantdinger-8443` (no recent traffic); decide pg/redis retention at Phase 4 |
| `execution/quantdinger` | `~/QuantDinger` | none (vendor) | **retired runtime** | keep snapshot; no new work |
| `research/astock` | `~/AStock_AI_System` | `app/` morning shadow (M8) | **inactive producer**; morning cron commented `SUPERSEDED_2026-09-08` | M9 stop authorization; confirm no report consumer |
| `signals/signal-bus` | `~/signal-bus` | `app/` signals/notify | **no active unit/process/timer/cron** (2026-11-25 scan); historical references only | no stop action unless reactivated |
| `reports/daily-report-tracking` | `~/releases/daily-report-tracking-release-20261109.2/candidate` | `app/` reports + 17:40 combined | **frozen source**; `quant-research-daily-summary.service` defined but inactive/not scheduled (no journal in 30 days) | retain files until Phase 4; no active producer to stop |
| `market-data/research-data` | `~/research-data` | `app/market` imports | **active legacy, degraded**: 22:30 cron deferred since 2026-11-16; snapshot 2026-09-29, core DB 2026-11-12, health `critical`/`FROZEN`; qp `market.read` mirrors | fix independent calendar evidence or replace the producer; keep qlib history |
| `market-data/qlib-scripts` | `~/qlib/scripts` | external qlib; `app/market.qlib_check` reads status; `market.read` mirrors `.day.bin` | **active external**: data 2026-11-24; qp mirror current | do not fold into repo; monitor only |
| `research/research-ledger` | `/opt/research-ledger` | read-only `research.import_ledger` | **active import source** (`research-ledger.service`, 18769); qp timer 09:40 | keep as source until import retires |
| `research/research-system-phase2` | `/opt/research-system-phase2` | read-only `research.import_phase2` | **active import source** (`research-system-phase2.service`, 18768) | same |
| `ops/dashboard` | `~/ops/dashboard` | `app/frontend` `/next/` | **absent on nnG8** (already moved); `/next/` replacement | no stop action |
| `ops/opt/*`, `ops/nginx` | `/opt/*`, `/etc/nginx` | mixed | **active ops** | not in this wave |
| `web/qd-frontend` | QD frontend build tree | `/next/` | **retired runtime** | keep snapshot for reference |
| `web/workbench` | `/opt/nng8-dashboard` | `/next/` | **absent** (path not present on nnG8) | archive after dashboard retirement |
| `web/shell` | new entry shell | `/next/` | **frozen**; repo-only | keep for historical layout |

## 3. Phases

### Phase 0 — kernel and guardrails (complete)

- M1–M15 kernel, schema migrations, job registry, backups, CI (pytest,
  frontend, smoke, ruff, gitleaks).
- Read-only imports for ledger/phase2/QuantDinger history.
- No legacy mutation.

### Phase 1 — freeze and inventory (current)

- All new code in `app/`; legacy snapshots security-only.
- **2026-11-24:** feature freeze re-baselined on production `0.3.106`; `.105`/`.106`
  were activated after the open (mixed-version day).
- **2026-11-25:** freeze window re-baselined at `0.3.115` 11:03 CST; `.112`–`.118`
  are in-freeze correctness fixes. Current production is
  `0.3.123-index-close-union-5f698f20`; `.119`–`.123` are also in-freeze
  correctness fixes. Earliest Phase 2 review is 2026-12-09
  (see the freeze baseline for the single current figure).
- **2026-11-28:** `.121` backtest latest-trades selection activated 14:22 CST
  (PR #93), `.122` backtest search recovery at 16:30 CST (PR #94) and `.123`
  index-close origin union at 18:31 CST (PR #100); observation window unchanged.
- Run `qp decommission inventory` weekly/monthly and store the manifest with
  its sha256. Latest: `/var/lib/qp/decommission-2026-11-28.json`,
  sha256 `b745f73e…` (5 qp-scope system timers, 0 containers, 13.5 MB captured
  paths); root/ubuntu scope is supplemented by the 2026-11-28 prep dossier.
- 2026-11-25 read-only scan (units, timers, containers, cron, processes, nginx,
  paths) is recorded in
  [`M9-EXIT-EVIDENCE-2026-11-25.md`](M9-EXIT-EVIDENCE-2026-11-25.md); several
  statuses above were corrected from it. The 2026-11-28 refresh, unit archive
  and mock rollback drill are in
  [`M9-PHASE2-PREP-2026-11-28.md`](M9-PHASE2-PREP-2026-11-28.md).
- User timers must be scanned separately as `ubuntu`; root-owned paths need a
  root scan before Phase 2 evidence is complete.
- Confirm each "active legacy" consumer with its timer/unit owner.
- Exit: every active legacy module has a named replacement and a stop order.

### Phase 2 — stop-only (M9 execution, requires separate authorization)

- Use [`M9-EXECUTION-CHECKLIST.md`](M9-EXECUTION-CHECKLIST.md): **one unit at a
  time**, with proof that the replacement owns the responsibility, consumers
  have switched, independent supervision is live, the observation window has
  elapsed, and rollback was tested.
- **No prefix batches** (`astock*`, `quant-*`, `trading-radar*`); no group
  `docker stop`. A blank checklist field is a hard stop.
- No `docker rm`, no data deletion, no config removal.
- Archive with the member-list verification in `M9-DECOMMISSION-RUNBOOK.md` §4;
  a tarball hash alone does not prove completeness.
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
