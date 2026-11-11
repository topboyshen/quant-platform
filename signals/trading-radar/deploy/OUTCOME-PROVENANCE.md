# Swing outcome input evidence v1

Opt-in sidecar for future natural outcomes. It is not a database backup, price
provider, performance correction, exit-rule change, or historical replay task.

## Activation and scope

`swing.outcome_evaluation.provenance` defaults to absent/disabled. Activation
requires `enabled: true` and a fixed timezone-aware `decision_at_or_after`.
Deployment must set that timestamp at activation, never derive a rolling cutoff
on every run. Existing systemd units and schedules stay unchanged; do not start
the service to test capture.

Only decisions at/after the cutoff and outcomes whose original INSERT just
returned true are eligible. All older decisions, including their not-yet-mature
20-session outcomes, remain legacy; no evidence is backfilled. A new repeat of
an older signal is still a repeat: the sidecar does not select canonical signals.
Research must deduplicate against full history, not count evidence files as new
signals. Decision code-version and full EOD input provenance remain unknown in
v1: coverage is **outcome_inputs_only**, not the full signal lifecycle.

## Immutable evidence, unchanged business

The evaluator passes its actual already-normalised cached target/benchmark
frames to the sidecar after business insertion. No additional fetch, Parquet
read, DB query, outcome update, timer, retry, or notification is introduced.
HORIZONS, effective costs, fill hypotheses and 20-session validity stay unchanged.
Existing same-day stock-ETF exit assumptions are not silently fixed here.

Records contain bounded OHLC windows, effective prior-close input, original
outcome fields, explicit signal keys (unknown if missing), settings and hashes,
observed source/config-file hashes, and whitelisted lineage declarations.
Source/adjustment declarations are not independent market-data verification.
Environment credentials/config contents are never exported. INVOCATION_ID is
whitelisted, but cannot prove timer rather than manual systemd execution.

Files under `data/swing_outcome_evidence_v1/` use decision+horizon slots. A
fsynced temporary file is atomically linked without replacing an existing slot.
Identical retries do not rewrite; conflicts keep the first file. Payload and
envelope hashes detect corruption, not adversarial tamper-proof attestation.
Source changes during a process fail the evidence side, not the business result.

Cross-horizon shared OHLC/dates, lineage, entry, reference, settings and observed
code-version differences are recorded. Longer windows alone are not a drift.
**Never feed the earlier frozen price back into later business calculations.**

DB INSERT and file publication are not one transaction. A crash can leave a
business outcome without evidence; there is no automatic repair/sweep. Join by
decision/horizon and compare recorded outcome values before claiming linkage.

## Failure, budgets and acceptance

Business summary keys remain unchanged. Enabled runs append separate
`evidence_written/existing/conflict/failed/unavailable/skipped_pre_cutover`,
`evidence_basis_diff` and `evidence_provenance_unknown` counters. Evidence failures
do not roll back business rows or change the service exit code; inspect these
counters and files rather than relying only on exit 0. Missing/invalid source or
cutoff is explicit unavailable, never silently disabled. Valid cutoffs still
exclude legacy records even if recorder initialization fails.

Maximum file 64 KiB; maximum directory 50,000 files / 256 MiB; reserve 64 MiB of
free space. Each price path is at most 20 bars. No auto-cleanup, backup upload or
retention timer exists. Directory checks and fsync add bounded data-volume I/O,
not a hard latency guarantee; natural execution latency remains to be observed.

At same-day activation the new EOD decisions have no next-session outcome yet.
Zero files that day is expected. First eligible evidence can appear on the next
trading day's natural outcome run; this is not a promise of a new unique signal.
Accept source/settings/path hashes, original-output linkage, honest coverage,
no conflicts, and separate unknown/basis-diff status. Do not manufacture samples.

Rollback: disable only this optional config section; do not trigger outcomes.
If code rollback is required, restore only the reviewed source paths after hash
checks. Preserve all generated evidence and old business rows. Do not reset
failed units, restore backups, modify other config, or touch frozen Phase2.

Repository test: `python -B -m unittest discover -s tests -p test_swing_outcome_provenance.py -v`.
Additional predeployment business-equivalence/concurrency tests are retained in
the local `profit-evidence-forward-20260908` audit package.
