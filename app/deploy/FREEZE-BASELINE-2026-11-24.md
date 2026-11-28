# Freeze baseline — 2026-11-24 (re-baselined 2026-11-25)

Status: **feature freeze active**. This document is the reference point for the
two-week stability window before any M9 Phase 2 stop is considered, and the
single source for the current production release, freeze start and earliest
Phase 2 review.

## 1. Freeze window

- Current production release: `0.3.122-backtest-search-recovery-ad12fb37`
- Current production deploy commit: `bf45bc2cccb02afce121ab1afc0b7cadb15d7528`
- Release record: [`RELEASE-NOTES-2026-11-28-0.3.122-zh-CN.md`](RELEASE-NOTES-2026-11-28-0.3.122-zh-CN.md)
- Freeze start: **2026-11-25 11:03 CST** (unchanged since the `.115` re-baseline;
  the freeze-start production release was `0.3.116-health-inventory-8375f5c8`,
  deploy commit `0a48377e5e5b677480484fa39f6289abec06da18`)
- Earliest Phase 2 stop review: **2026-12-09** (14 full days from the
  2026-11-25 11:03 start)
- Allowed during freeze: production incident fixes, security fixes, evidence
  corrections and documentation. New features and UI expansion are paused.

`0.3.117-correctness-batch1-f531e552` (2026-11-25 15:18 CST; PR #72),
`0.3.118-outcome-window-6f60f5a8` (2026-11-25 21:28 CST; PR #74, with the PR #73
documents in the release bundle), `0.3.119-acceptance-after-daily-report-6e5e111d`
(2026-11-26 10:12 CST; PR #84), `0.3.120-market-generation-bce0f98e`
(2026-11-26 19:17 CST; PR #86), `0.3.121-backtest-latest-7dfcb452`
(2026-11-28 14:22 CST; PR #93) and `0.3.122-backtest-search-recovery-ad12fb37`
(2026-11-28 16:30 CST; PR #94) are in-freeze correctness fixes. They do not
change the freeze start or the earliest review date.

The 2026-11-24 business day is a **mixed-version day**: `.105` 09:02, `.106`
09:43, `.107` 15:12, `.108` 15:20, `.109` 16:50, `.110` 18:51 and `.111`
19:09 CST were all activated after the 09:30 open. 2026-11-25 added `.112`–`.118`
before/after the open: `.112`–`.116` incident/report-correctness fixes, `.117`
correctness batch 1, `.118` outcome-window/OHLC fix. 2026-11-26 added `.119`
acceptance-after-daily-report and `.120` market-generation compare; 2026-11-28
added `.121` backtest latest-trades selection and `.122` backtest search
recovery. None of
these days is a clean single-release baseline. `.107/.108` were the user-authorized C4
exception. No cosmetic or feature release is allowed before the 2026-12-09
review. The 10-trading-day observation window (2026-11-18 → 2026-12-01)
continues separately.

## 2. Inventory snapshot

Generated read-only on nnG8:

```bash
sudo -u qp env QP_APP_DIR=/opt/qp/current/backend \
  /opt/qp/current/venv/bin/qp decommission inventory \
  --out /var/lib/qp/decommission-2026-11-24.json
```

- File: `/var/lib/qp/decommission-2026-11-24.json`
- SHA256: `e98f48460bad6e083309a7736e208b58356eab2286f3e6fdce44ad252de2c66a`
- Totals recorded by the tool: **5 system timers, 0 containers, 13,537,346 bytes**
  of captured paths.

System timers in the snapshot:

- `nng8-dashboard-cert-renew.timer`
- `quant-notify-maintenance-monitor.timer`
- `research-index-close-cache.timer`
- `research-ledger-healthcheck.timer`
- `research-trading-calendar-refresh.timer`

User timers are outside the qp service user's bus; the read-only `ubuntu` user
scan on the same day showed:

- `hermes-telegram-watchdog.timer`
- `research-data-bundle.timer`
- `launchpadlib-cache-clean.timer`

Scope caveat: the qp inventory captured `/opt/research-system-phase2` only.
Root-owned paths and any consumer not visible to `qp` must be captured again as
root before a Phase 2 decision; this snapshot is a baseline, not an execution
authorization.

## 3. What is explicitly not happening

- No legacy unit, timer or container has been stopped by this document.
- No data is being deleted or moved.
- Phase 2 still requires the per-unit evidence row in
  [`M9-EXECUTION-CHECKLIST.md`](M9-EXECUTION-CHECKLIST.md) and separate user
  authorization.
- A green qp health check is not a substitute for the per-unit replacement,
  consumer and rollback evidence.
