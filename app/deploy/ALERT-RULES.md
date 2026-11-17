# Operational alert rules (P1)

These rules are evaluated by `ops.healthcheck` (every 5 minutes) and enqueue
through the existing `notify.outbox` path. They do not add a timer, run other
jobs, or send directly.

| Rule | Trigger | Dedup |
|---|---|---|
| `job_consecutive:<job>` | latest two non-skipped runs are both `failed` or `blocked` | one row per key per hour |
| `backup_failure` | latest `ops.backup` run is `failed` or `blocked` | one row per hour |
| `market_source_failure` | latest successful `market.sources` result has failed sources other than eastmoney | one row per hour |

## Delivery controls

`enqueue()` uses the normal notification mode. In the default `shadow` mode the
row is recorded but not sent; with `QP_NOTIFY_MODE=live` the normal
`notify.send` path applies `QP_NOTIFY_KILL_SWITCH` and the disabled file. The
alert rules never bypass those controls.

## Exclusions

- `eastmoney_names` is the documented fallback source (`KL-001`); an eastmoney-only
  failure is not a paging condition.
- `ops.healthcheck` and `ops.backup` are excluded from the generic
  consecutive-failure rule; backup has its own rule.
- Skipped/locked/duplicate runs do not count as failure attempts.

## Verification

- `tests/test_alert_rules.py`: consecutive failures, backup failure, source
  failure, eastmoney-only exclusion.
- Full backend: 454 passed, 1 skipped on 2026-11-18.

Rollback is code-only (remove the `evaluate_alerts` call or revert the commit);
there is no schema or timer change.
