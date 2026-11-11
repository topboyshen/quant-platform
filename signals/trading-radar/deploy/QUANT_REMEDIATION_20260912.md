# Quant EOD / release gate remediation — 2026-09-12

Base: `fdf0fc3`. Scope is the defects reproduced in the same-day inspection;
no strategy change, database migration/write, QuantDinger rebuild, backup,
pruning or reset-failed is part of this rollout.

## Fixes and invariants

- AkShare returns rejected-frame **metadata** through a caller-owned observation
  list; stale prices remain rejected. A real Sina stale response can now trigger
  the one-time retry even if Eastmoney/Tencent are unavailable.
- Waiting is justified by **at least one provider** on the exact previous trading
  session in the existing `data/trading_calendar.json`, not an arbitrary 1–3
  natural-day lag. Missing/invalid calendar disables waiting; exact-date data
  validation remains fail-closed. Default wait 45s, configurable 0..180s, one
  wait per fetcher/decision date in the sequential EOD batch.
- Standard-library `runtime_contracts` supplies shared calendar and EOD JSON
  contracts. Calendar dates are never fetched or modified by these checks.
- Watchdog distinguishes awaiting_retry, running, failed_final, missing and
  unknown. Incomplete evidence never declares business success. JSON is atomically
  published with a unique temporary file before notification transport is used.
- Exporter validates the complete bounded JSON before generating metrics;
  invalid booleans/counts/nested objects/timestamps produce one unknown set, not
  duplicate samples or partial success. It does not open a business database.
- `evidence_valid`, `calendar_valid`, `evidence_current`, `terminal_due` express
  validity and calendar-aware completion requirements separately. At 15:30 the
  required session advances; the final deadline remains 15:50. Weekends/holidays
  retain the preceding session. Recorded final failure does not expire after
  18 hours. Missing/invalid/stale evidence has a dedicated Prometheus rule.
- `systemd_first_failed` is retained for compatibility but reflects first **audit**
  status, not a live systemd probe. No systemd failed flag is cleared.
- Production pin validates all three mandatory application services and full
  image IDs; runtime verification also requires running/healthy. CLI arguments
  work; preflight and postflight are wired into the canonical Compose wrapper.
- `runtime_contracts*` is included in package discovery for future wheel/image
  builds; existing images are untouched by this host-side deployment.

## Regression coverage

Targeted tests run from `/tmp/quant-remediation.b84NU0` in a new network namespace,
with `/home/ubuntu/trading-radar` bind-mounted read-only in that mount namespace,
non-root user, empty environment, isolated HOME and no pytest cache/bytecode.
No production config/credentials are imported for these tests.

Validation before rollout: **314 tests passed, no skips** under host Python;
Prometheus rule check passed (21 rules), original rule tests passed, and the new
15-case PromQL matrix passed. Actual Swing runtime Python 3.12.3 / pandas 3.0.3
also passed a fake-network, fake-persistence real-adapter stale→retry→fresh check.
Its environment does not contain pytest, so this is a compatibility probe, not
a claim that the full pytest suite ran under that venv. Existing production JSON
and shared calendar passed the new validator in a separate read-only preview.

Suites cover:

- Real adapter boundary: Sina stale -> fresh on second attempt; still stale;
  empty/network-error/future/short/too-old data; holiday gap; one wait for batch.
- JSON faults: string booleans, malformed counts/objects, oversize/deep/invalid
  JSON, naive/future/cross-date times and contradictory day status.
- Monday old success, valid Friday evidence on weekends, holiday calendar,
  publication before Telegram failure and atomic replacement failure.
- Pin CLI fixtures, stopped/unhealthy/missing containers, malformed pins,
  resolved Compose/tag/image-ID drift; fake wrapper preflight/mutation/postflight
  order and nonzero propagation. No test mutates a real container.
- PromQL matrix for missing/invalid/stale/absent metrics, deferral, still-running,
  failure and recovery. Original rule tests also retain the >18h failure case.

The `promtool` binary used for validation is copied from the running Prometheus
container to the isolated directory; no image pull/new image/container is needed.

## Existing worktree disposition — not silently approved

The inspection reported 15 unexpected records before this repair. They are not
included in this patch except for **ignore policy**, and must not be swept into
`git add .`, reset or clean:

| Paths | Disposition |
|---|---|
| `config.swing.json` | Preserve live operator configuration; not owned by this patch |
| `deploy/grafana/dashboards/quantdinger-radar-runtime.json` | Preserve existing dashboard change |
| `scripts/prune_radar_images.sh` | Preserve existing retention change; do not run prune |
| `deploy/approved-signals/app.py`, `tls_proxy.py` | Existing host integration; preserve, separate release review |
| `deploy/approved-signals/tls/{cert,key}.pem` | Preserve in place, do not read/copy contents; new exact-directory .gitignore prevents accidental commit |
| Four untracked pg-dump/SQLite backup service/timer files | Preserve dormant files; NO_BACKUP timers remain disabled |
| `docker-compose.macro-risk-restore.yml` | Preserve existing recovery artifact; do not execute |
| `scripts/export_execution_digest.py` | Preserve existing host job source; separate ownership review |
| `scripts/quantdinger-pg-dump-daily.sh`, `quantdinger-pg-restore-drill.sh` | Preserve dormant scripts; do not execute |

The two TLS files stop appearing as untracked due to ignore policy; the remaining
13 pre-existing unexpected records still cause hygiene exit 1. This is an honest
pending-review result, **not** an excuse to hide/approve source drift. Runtime and
historical backup dirt retains its existing informational classification.

## Deployment / rollback

Deploy only the explicitly listed patch files after comparing their live hashes
to the base commit; abort on unexpected drift. Install `runtime_contracts` before
consumers and the adapter before its caller. Do not stage any existing dirty
business configuration, data, dashboard, private key, backup or prune script.

Prometheus uses a **single-file bind mount** for alerts.yml. Preserve its inode
when writing validated rules, verify host/container hashes, then POST `/-/reload`.
No container restart is needed. The exporter is a host script; its next timer
invocation loads the updated code. A deliberate exporter run only refreshes
textfile metrics, never the business database or historical watchdog JSON.

Rollback restores only this patch's paths from the base commit (new modules can
remain unused), restores the previously captured canonical wrapper, rewrites the
old rule text in place and reloads Prometheus. Never use a whole-worktree reset or
restore historical failed database records. The source candidate directory holds
the original wrapper for this rollback; it is not a business-data backup.
