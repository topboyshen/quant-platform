BEGIN;

CREATE TABLE IF NOT EXISTS ledger.daily_candidate_run (
    run_id UUID PRIMARY KEY,
    manifest_id UUID NOT NULL UNIQUE,
    manifest_receipt_id UUID NOT NULL UNIQUE,
    target_session DATE NOT NULL,
    data_cutoff TIMESTAMPTZ NOT NULL,
    policy_version TEXT NOT NULL,
    universe_snapshot_hash TEXT NOT NULL,
    context_snapshot_hash TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    supersedes_manifest_id UUID,
    decision TEXT NOT NULL CHECK (decision IN ('CANDIDATE_SET','ABSTAINED','WITHDRAWN')),
    publication_status TEXT NOT NULL CHECK (publication_status IN ('PROVISIONAL','CONFIRMED','WITHDRAWN')),
    candidate_count INTEGER NOT NULL CHECK (candidate_count BETWEEN 0 AND 3),
    execution_cap INTEGER NOT NULL CHECK (execution_cap = 0),
    calibration_status TEXT NOT NULL CHECK (calibration_status = 'INSUFFICIENT_PROSPECTIVE_SAMPLES'),
    payload_hash TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE (target_session, revision)
);

CREATE TABLE IF NOT EXISTS ledger.daily_candidate_entry (
    candidate_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES ledger.daily_candidate_run(run_id),
    package_id UUID NOT NULL UNIQUE,
    package_receipt_id UUID NOT NULL UNIQUE,
    symbol TEXT NOT NULL CHECK (symbol ~ '^[0-9]{6}\.(SH|SZ|BJ)$'),
    rank_no INTEGER NOT NULL CHECK (rank_no BETWEEN 1 AND 3),
    rank_strength NUMERIC NOT NULL CHECK (rank_strength >= 0 AND rank_strength <= 1),
    qlib_cross_section NUMERIC NOT NULL CHECK (qlib_cross_section >= 0 AND qlib_cross_section <= 1),
    kronos_return_percent NUMERIC NOT NULL,
    model_agreement_stability NUMERIC NOT NULL CHECK (model_agreement_stability >= 0 AND model_agreement_stability <= 1),
    score_type TEXT NOT NULL CHECK (score_type = 'UNCALIBRATED_RANK_STRENGTH'),
    package_payload_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (run_id, rank_no),
    UNIQUE (run_id, symbol)
);

DO $$ BEGIN
  CREATE TRIGGER no_mutation_daily_candidate_run BEFORE UPDATE OR DELETE ON ledger.daily_candidate_run FOR EACH ROW EXECUTE FUNCTION ledger.prevent_mutation();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE TRIGGER no_mutation_daily_candidate_entry BEFORE UPDATE OR DELETE ON ledger.daily_candidate_entry FOR EACH ROW EXECUTE FUNCTION ledger.prevent_mutation();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS idx_daily_candidate_target ON ledger.daily_candidate_run(target_session, revision);
CREATE INDEX IF NOT EXISTS idx_daily_candidate_entry_run ON ledger.daily_candidate_entry(run_id, rank_no);

INSERT INTO ledger.schema_migration(version, artifact_hash)
VALUES ('003_daily_candidate_v1', :'migration_hash')
ON CONFLICT (version) DO NOTHING;

COMMIT;
