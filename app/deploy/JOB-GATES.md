# Same-day upstream job gates (P1)

## Purpose

Timers still start jobs at fixed times, but selected downstream jobs now verify
that their upstream `ops.job_run` **succeeded on the same Asia/Shanghai business
day** before doing work. A missing, failed, stale or future upstream run returns
a structured block; the job runner records status `blocked` (not a silent
success).

## Enablement

```ini
QP_JOB_GATES=0   # default: gates report disabled and preserve legacy behavior
QP_JOB_GATES=1   # enforce in production
```

Keep the default off until the first natural-day check is complete. Rollback is
an env-only change to `0` plus a `qp-job@` restart; no schema change.

## Applied gates

| Downstream | Upstream | Max age |
|---|---|---:|
| `signals.rule_shadow` | `signals.eod` | 8h |
| `signals.chain_snapshot` | `signals.eod` | 8h |
| `signals.chain_window` | `signals.chain_snapshot` | 8h |
| `signals.acceptance` | `signals.outcomes` | 8h |

The gate only accepts a `succeeded` run that finished today and is within the
age window. It does not treat `skipped_locked`, `skipped_dup`, `failed` or a
previous day's success as upstream success.

## Evidence

- `blocked` is stored in `ops.job_run.status`; the overview failure count includes
  `failed` and `blocked`.
- `require_job_success()` returns the upstream run id, finish time, age and reason.
- Unit tests: `tests/test_job_gates.py`; job tests run with gates disabled by
  default, preserving existing fixtures.

## Rollout

1. Deploy code with `QP_JOB_GATES=0`; verify no behavior change.
2. Set `QP_JOB_GATES=1` on a trading day before 15:30, restart the job units.
3. Observe one full day: `signals.eod` succeeds, downstream jobs are not blocked;
   if a block appears, inspect the gate reason before retrying.
4. If a false block occurs, set `QP_JOB_GATES=0` immediately and keep the
   `blocked` run row as evidence.
