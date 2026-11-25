# M9 exit evidence — 2026-11-25

Read-only evidence for the services listed in
[`DECOMMISSION-ROADMAP.md`](DECOMMISSION-ROADMAP.md). **No legacy unit, timer,
container or data was stopped, disabled or deleted for this document.**

Sources:

- `qp decommission inventory` snapshot 2026-11-24
  (`/var/lib/qp/decommission-2026-11-24.json`, sha256 `e98f4846…`): 5 system
  timers, 0 containers, 1 path. The qp user scope only captured
  `/opt/research-system-phase2`; the live scan below fills the gap.
- Read-only scan on `VM-0-3-ubuntu` (nnG8) 2026-11-25 22:2x CST:
  `systemctl list-units/list-timers`, `docker ps`, user/root crontabs, `ps`,
  nginx references and path existence.
- [`M9-EXECUTION-CHECKLIST.md`](M9-EXECUTION-CHECKLIST.md) per-unit fields.

## 1. Inventory additions from the live scan

- Containers: `radar-quantdinger-postgres` (healthy, `127.0.0.1:55432`) and
  `radar-quantdinger-redis` (healthy) are still running.
- Active legacy units: `quant-approved-signals.service` (trading-radar),
  `research-ledger.service`, `research-system-phase2.service`.
- Defined but inactive: `quant-research-daily-summary.service` (static; no timer,
  no journal entries in 30 days) still points at
  `/home/ubuntu/releases/daily-report-tracking-release-20261109.2/candidate/run_scheduled.sh`.
- Ubuntu crontab: qlib 22:00/06:30/09:15, research-data 22:30; AStock morning
  cron is commented as `SUPERSEDED_2026-09-08`; trading-radar sync and paper
  simulator lines were removed 2026-09-09.
- Paths absent/open: `/opt/nng8-dashboard` and `~/ops/dashboard` are absent
  (already retired/moved); `/home/ubuntu/trading-radar`, `QuantDinger`,
  `AStock_AI_System`, `signal-bus`, `research-data`, `/opt/research-ledger`,
  `/opt/research-system-phase2` exist.
- Nginx: `/next/` proxies to qp API; `quantdinger-8443` proxies its
  approved-signals location to `127.0.0.1:8094`; no nginx location directly
  serves a roadmap path.
- Legacy research timers `research-trading-calendar-refresh.timer`,
  `research-index-close-cache.timer` and `research-ledger-healthcheck.timer` are
  enabled and active; their oneshot services are inactive between runs.
- qp is a confirmed consumer of the legacy research data:
  `qp-market-read.service` (06:45/17:00/23:25) runs
  `python -m qp.collect.market`, which reads `research-data/snapshot.db`,
  `core-market.db`, the research-data health file and Qlib `.day.bin`; the
  subsequent `market.ingest_legacy` run reports 10 items and 0 mismatches.
  Qlib reference dates in the latest snapshot are **2026-11-24**.
- The `research-data` producer is **degraded**: its health file is `critical` /
  `FROZEN` (generated 2026-11-12, effective 2026-10-08), `snapshot.db` was last
  written 2026-09-29 and `core-market.db` 2026-11-12; the 22:30 cron has been
  `deferred` since 2026-11-16 with `independent trading calendar evidence
  invalid or stale`.

## 2. Per-service evidence

| Service | Runtime / owner | Consumers (evidence) | Replacement | Rollback evidence | Status |
|---|---|---|---|---|---|
| `signals/trading-radar` | `quant-approved-signals.service` active (PID 4625, `127.0.0.1:8094`); `radar-quantdinger-*` containers up | **confirmed path**: nginx `quantdinger-8443` approved-signals location proxies to `127.0.0.1:8094`; no active connection or journal entry at scan time | qp `signals.approved` + `/next/` | `systemctl start quant-approved-signals.service` (enabled); containers already running | **active** — consumer path confirmed; recent traffic not observed |
| `research/astock` | path exists; morning cron commented `SUPERSEDED_2026-09-08`; no active unit/process | old Telegram delivery disabled; no active timer found | qp M8 morning shadow | commented cron line can be restored; no active producer to restart | **inactive** — no runtime to stop |
| `signals/signal-bus` | path exists; no unit/process/timer/cron; references only in archived work dirs | no active consumer found in systemd/nginx/cron | qp signals/notify | not applicable while inactive; archived scripts reference `~/signal-bus/scripts` | **inactive** — historical references only |
| `reports/daily-report-tracking` | frozen source path exists; **consumer defined**: `quant-research-daily-summary.service` (static, inactive, no timer, no journal in 30 days) runs `run_scheduled.sh` | consumer service exists but is not scheduled or running | qp reports (`reports.import_daily`, manual only) | `systemctl start quant-research-daily-summary.service`; files retained | **inactive** — consumer defined but not scheduled |
| `market-data/research-data` | path exists; ubuntu cron 22:30 active but **degraded**: `snapshot.db` 2026-09-29, `core-market.db` 2026-11-12, health `critical`/`FROZEN` (generated 2026-11-12); runs deferred since 2026-11-16 (`independent trading calendar evidence invalid or stale`) | **qp consumer confirmed**: `qp-market-read.service` (06:45/17:00/23:25) reads `snapshot.db` / `core-market.db` / health; `market.ingest_legacy` 10 items / 0 mismatch; Qlib reference dates 2026-11-24 | qp Qlib/market mirror; producer not replaced; AStock calendar is the dependency | restore the 22:30 cron line; fix independent calendar evidence before retirement | **partial** — producer degraded, consumer confirmed |
| `market-data/qlib-scripts` | ubuntu cron 22:00 update, 06:30 retry, 09:15 prune; Qlib data mtime 2026-11-24 | **qp consumer confirmed**: `market.read` reads `.day.bin`; reference dates 2026-11-24; `market.qlib_check` latest EOD 2026-11-24 | qp read-only mirror (no data copy) | restore the three cron lines | **partial** — source active, mirror current |
| `research/research-ledger` | `research-ledger.service` active, `127.0.0.1:18769`; healthcheck timer | qp `qp-research-ledger.timer` imports ledger at 09:40 | `research.import_ledger` | `systemctl start research-ledger.service` | **confirmed** for unit/consumer |
| `research/research-system-phase2` | `research-system-phase2.service` active, `127.0.0.1:18768` | qp `research.import_phase2` job | `research.import_phase2` | `systemctl start research-system-phase2.service` | **confirmed** for unit/consumer |
| `ops/dashboard` | path absent | none (moved) | `/next/` | none needed | **complete** |
| `web/qd-frontend` | no runtime path in scan | none active | `/next/` | none needed | **complete** |
| `web/workbench` | `/opt/nng8-dashboard` absent | none active | `/next/` | none needed | **complete** |
| `web/shell` | repo-only; no runtime | none | `/next/` | none needed | **complete** |
| legacy research timers (`research-trading-calendar-refresh`, `research-index-close-cache`, `research-ledger-healthcheck`) | timers enabled/active; services static/oneshot; calendar/index commands run from `~/trading-radar` | research stack / ledger | qp `market.calendar` (06:30), `market.index_close` (16:30/16:40), `research.import_ledger`; no qp equivalent for the ledger source healthcheck | `systemctl start <timer>`; unit files under `/etc/systemd/system` | **partial** — calendar/index replacements exist; ledger healthcheck remains source-specific |

## 3. Gap-fill results

The 2026-11-25 follow-up scan closed or narrowed the previous gaps:

- `signals/trading-radar`: consumer path is confirmed — nginx
  `quantdinger-8443` proxies the approved-signals location to
  `127.0.0.1:8094`; the unit is enabled and the containers are up. No active
  connection or journal entry was seen at scan time.
- `signals/signal-bus`: no unit, timer, nginx location, cron line or process
  exists; only archived scripts under `~/b2-work-*` and evidence directories
  reference it. No runtime to stop.
- `reports/daily-report-tracking`: the consumer is
  `quant-research-daily-summary.service` (static, inactive, no timer, no journal
  in 30 days); it points at the frozen `run_scheduled.sh`. Rollback command is
  `systemctl start quant-research-daily-summary.service`.
- Legacy research timers are enabled/active; calendar and index-close have qp
  replacements, while the ledger healthcheck remains source-specific.
- `research-data` / `qlib`: qp is a confirmed consumer via
  `qp-market-read.service` and `market.ingest_legacy` (10 items, 0 mismatches);
  Qlib reference dates are 2026-11-24. The `research-data` producer itself is
  degraded (health `critical`/`FROZEN`, snapshot 2026-09-29, core DB 2026-11-12,
  deferred since 2026-11-16), so the qp mirror is only as fresh as its legacy
  inputs. The AStock calendar is the stale dependency; qp `market.calendar`
  exists but is not wired into the research-data replacement.

## 4. Remaining M9 checklist gaps

For every row still marked **partial** or **inactive**, the corresponding
`M9-EXECUTION-CHECKLIST.md` fields are not complete:

- **Consumer switched**: qp is a confirmed consumer of `research-data` / `qlib`,
  but the replacement producers are not qp-owned; the qp replacements for the
  legacy research timers are not fully cut over. `trading-radar`'s intake still
  receives whatever the QuantDinger side sends.
- **Data freshness**: `research-data` health is `critical` / `FROZEN`
  (2026-11-12), `snapshot.db` is from 2026-09-29 and `core-market.db` from
  2026-11-12; Qlib is current to 2026-11-24. Retirement is blocked until an
  independent calendar/health source replaces the stale AStock dependency.
- **Rollback tested**: restart commands, unit files, enabled/static states and
  container status are verified, but no actual stop/start or mock-unit drill was
  run for the legacy units (requires separate authorization).
- **Observation window / authorization**: the freeze window is still running
  (earliest Phase 2 review 2026-12-09); no stop authorization has been given.
- **Evidence**: the qp inventory snapshot is qp-scope only; containers and
  root-owned paths require the root/ubuntu scan above and, for Phase 2, a fresh
  inventory pass on the day of execution.

## 5. What this does not authorize

- No Phase 2 stop, disable, container stop or data deletion.
- No claim that the legacy services are retired; the roadmap status table still
  needs a per-unit sign-off on the day of the stop.
- The restore drill in [`RESTORE-DRILL-2026-11-25.md`](RESTORE-DRILL-2026-11-25.md)
  covers database/application recovery, not legacy-service rollback.
