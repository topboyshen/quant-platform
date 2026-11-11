# Batch 3 isolated validation — 2026-09-28

Baseline: research-data `d83c42d20331a8f016b74d8a9b81ff15a6462de4`.
Local clone: `/private/tmp/rd-core-split-20260928`.
Branch: `batch3-core-contract-20260928`. No production writes or live sends.

## Results

- Final full fixed-fixture suite: **464 tests, OK, zero failures/skips**;
  `OFFLINE_TESTS=464 success=True`, 4.678 s (00:46:44 +08:00 run).
- Path-relocated snapshot-wrapper fixed-stub integration: **7/7 OK**.
  Active core deferred returns exit 75 on two days; no direct Hermes branch;
  old full state untouched. Missing policy retains legacy argv and alert.
- Actual health-alert shell + helper, fixed clock and fake Hermes: **1/1 OK**.
  New core deferred issue: one fake message, receipt/state committed; same
  dueDate repeat: zero additional fake messages. No external messages.
- Real argparse `--help` smoke included for data_health/core_market.
- `git diff --check`: clean. `daily_snapshot.py`: unchanged from baseline.

## Guarded environment

Bundled cached Python/dependencies reused; no installation. Existing offline
runner kept network/subprocess/production-file/external-write guards. Host-only
test harness adaptations in memory: Linux `/proc/self/fd` resolution to macOS
F_GETPATH; Path.home mocked to disposable scratch for mootdx import config;
verbosity lowered. No HOME/CODEX_HOME mutation. Separate shell integration
tools relocate literal paths and invoke fixed stubs only; no real services.

## Covered regressions

Qlib-only summary scope, strict coverage/finite counts, optional AkShare
No-tables failure, old targets, pre-effective targets, same-day changed-Qlib
immutable conflict, two hash-bound calendar forms, wrong source/day/times,
symlinked archive parents, corrupt archive no skip fallback, genuine holiday
with no core DB, forged holiday rejection, no-policy legacy behavior,
old historical critical and row baseline retained, cutover dual-write critical,
optional-stage cutover boundaries, same-object full-market invalid/eligible
versus separately disclosed CSI union, core due-run/failure alert identities,
blind-period fault preservation and real recovery, current core quality hash.

## Remaining activation blocker

The adapter currently needs a sealed AStock archive on open days. AStock report
failure with an otherwise valid raw calendar deliberately still defers (fixed
test included). This is NOT two independent chains yet. An independent bound
calendar producer or verified precollection adapter must be reviewed before
policy activation. Default-off preinstallation is separable. No natural run,
historical repair, total data completeness or full price archive is claimed.
