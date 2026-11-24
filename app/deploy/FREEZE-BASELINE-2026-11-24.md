# Freeze baseline — 2026-11-24

Status: **feature freeze active**. This document is the reference point for the
two-week stability window before any M9 Phase 2 stop is considered.

## 1. Freeze window

- Production release: `0.3.108-attribution-wow-cutoff-0341a9e4`
- Production deploy commit: `deedf4ea4e0bedb13ba1a7662a287f4267a4355b`
- Freeze start: **2026-11-24 12:00 CST**
- Earliest Phase 2 stop review: **2026-12-08** (14 full days, no rollback)
- Allowed during freeze: production incident fixes, security fixes, evidence
  corrections and documentation. New features and UI expansion are paused.

The 2026-11-24 business day is a **mixed-version day**: `.105` 09:02, `.106`
09:43, `.107` 15:12 and `.108` 15:20 CST were all activated after the 09:30
open. Do not count it as a clean baseline day for a single release. `.107/.108`
are the user-authorized C4 weekly-attribution exception; feature freeze remains. The 10-trading-day observation window
(2026-11-18 → 2026-12-01) continues separately.

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
