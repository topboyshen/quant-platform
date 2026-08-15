# Calibration cohorts

The Ledger reports two separate denominators for binary forecasts.

- `raw_cohort` contains every matured, sealed, prospective forecast from a non-vetoed batch. Its coverage includes every latest observable outcome, including `PENDING_REVIEW`; it is always `DIAGNOSTIC_ONLY` and never publishes Brier/ECE.
- `reviewed_cohort` contains only forecasts whose latest outcome is `REVIEWED` and whose latest `REVIEWED` settlement points to that same outcome revision.

The legacy top-level fields (`eligible_forecast_count`, `independent_batch_count`, `brier`, `ece`, `bins`) remain the formal reviewed-settlement view. Unreviewed outcomes never become settlements and never gain formal standing.

`coverage` and `coverage_bins` expose review coverage so that human approval cannot silently change the denominator. Independent batches are counted by target trading session, not by retry/batch ID. `maturity_reason_code` distinguishes no matured forecasts, missing outcomes, pending review, partial review, and sample accumulation.

Calibration snapshots are content-idempotent. An unchanged cohort reuses the existing snapshot instead of appending duplicate history.
