BEGIN;

ALTER TABLE ledger.calibration_snapshot
  ADD COLUMN IF NOT EXISTS raw_due_forecast_count INTEGER CHECK (raw_due_forecast_count >= 0),
  ADD COLUMN IF NOT EXISTS raw_due_independent_batch_count INTEGER CHECK (raw_due_independent_batch_count >= 0);

CREATE INDEX IF NOT EXISTS idx_forecast_matured_binary
  ON ledger.forecast(window_end, batch_id)
  WHERE forecast_type = 'BINARY_EVENT'
    AND status = 'SEALED'
    AND prospective_eligible = true;

INSERT INTO ledger.schema_migration(version, artifact_hash)
VALUES ('004_calibration_dual_cohort_v1', :'migration_hash')
ON CONFLICT (version) DO NOTHING;

COMMIT;
