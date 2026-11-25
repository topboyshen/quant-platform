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
- Ubuntu crontab: qlib 22:00/06:30/09:15, research-data 22:30; AStock morning
  cron is commented as `SUPERSEDED_2026-09-08`; trading-radar sync and paper
  simulator lines were removed 2026-09-09.
- Paths absent/open: `/opt/nng8-dashboard` and `~/ops/dashboard` are absent
  (already retired/moved); `/home/ubuntu/trading-radar`, `QuantDinger`,
  `AStock_AI_System`, `signal-bus`, `research-data`, `/opt/research-ledger`,
  `/opt/research-system-phase2` exist.
- Nginx: `/next/` proxies to qp API; `quantdinger-8443` still documents the
  approved-signals intake; no nginx location directly serves a roadmap path.

## 2. Per-service evidence

| Service | Runtime / owner | Consumers (evidence) | Replacement | Rollback evidence | Status |
|---|---|---|---|---|---|
| `signals/trading-radar` | `quant-approved-signals.service` active (PID 4625, `127.0.0.1:8094`); `radar-quantdinger-*` containers up | nginx `quantdinger-8443` approved-signals intake; exact request consumer not captured | qp `signals.approved` + `/next/` | `systemctl start quant-approved-signals.service` (enabled); containers already running | **partial** — consumer path unconfirmed |
| `research/astock` | path exists; morning cron commented `SUPERSEDED_2026-09-08`; no active unit/process | old Telegram delivery disabled; no active timer found | qp M8 morning shadow | commented cron line can be restored; no active producer to restart | **partial** — inactive |
| `signals/signal-bus` | path exists; no unit/process/cron in scan | not confirmed | qp signals/notify | not confirmed | **missing** |
| `reports/daily-report-tracking` | frozen source path exists (root-owned candidate); no process/unit | not confirmed; files may still be read | qp reports | files retained; no producer to restart | **partial** |
| `market-data/research-data` | path exists; ubuntu cron 22:30 nightly snapshot | qlib / research consumers not individually confirmed | qp market imports | restore the 22:30 cron line | **partial** |
| `market-data/qlib-scripts` | ubuntu cron 22:00 update, 06:30 retry, 09:15 prune | active data producer; consumers not individually confirmed | qp `market.qlib_check` / market imports | restore the three cron lines | **partial** |
| `research/research-ledger` | `research-ledger.service` active, `127.0.0.1:18769`; healthcheck timer | qp `qp-research-ledger.timer` imports ledger at 09:40 | `research.import_ledger` | `systemctl start research-ledger.service` | **confirmed** for unit/consumer |
| `research/research-system-phase2` | `research-system-phase2.service` active, `127.0.0.1:18768` | qp `research.import_phase2` job | `research.import_phase2` | `systemctl start research-system-phase2.service` | **confirmed** for unit/consumer |
| `ops/dashboard` | path absent | none (moved) | `/next/` | none needed | **complete** |
| `web/qd-frontend` | no runtime path in scan | none active | `/next/` | none needed | **complete** |
| `web/workbench` | `/opt/nng8-dashboard` absent | none active | `/next/` | none needed | **complete** |
| `web/shell` | repo-only; no runtime | none | `/next/` | none needed | **complete** |
| legacy research timers (`research-trading-calendar-refresh`, `research-index-close-cache`, `research-ledger-healthcheck`) | systemd timers active | research stack / ledger | qp `market.calendar`, `market.index_close`, ledger import | `systemctl start <timer>`; definitions still installed | **partial** — replacements not fully cut over |

## 3. M9 checklist gaps

For every row still marked **partial** or **missing**, the corresponding
`M9-EXECUTION-CHECKLIST.md` fields are not complete:

- **Consumer switched**: no end-to-end proof that the named consumer reads qp
  only; `trading-radar`/`signal-bus`/`daily-report-tracking` consumers remain
  unconfirmed.
- **Rollback tested**: service/timer restart commands are recorded, but no mock
  stop/start drill was run for the legacy units.
- **Observation window / authorization**: the freeze window is still running
  (earliest Phase 2 review 2026-12-09); no stop authorization has been given.
- **Evidence**: the qp inventory snapshot is qp-scope only; containers and
  root-owned paths require the root/ubuntu scan above and, for Phase 2, a fresh
  inventory pass on the day of execution.

## 4. What this does not authorize

- No Phase 2 stop, disable, container stop or data deletion.
- No claim that the legacy services are retired; the roadmap status table still
  needs a per-unit sign-off on the day of the stop.
- The restore drill in [`RESTORE-DRILL-2026-11-25.md`](RESTORE-DRILL-2026-11-25.md)
  covers database/application recovery, not legacy-service rollback.
