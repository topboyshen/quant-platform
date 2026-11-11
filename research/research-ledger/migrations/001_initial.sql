BEGIN;

CREATE SCHEMA IF NOT EXISTS ledger AUTHORIZATION researchledger;
REVOKE ALL ON SCHEMA ledger FROM PUBLIC;
GRANT USAGE ON SCHEMA ledger TO researchledger;

CREATE TABLE IF NOT EXISTS ledger.schema_migration (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    artifact_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger.research_case (
    case_id UUID PRIMARY KEY,
    external_key TEXT UNIQUE,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('DRAFT','UNDER_REVIEW','RISK_VETOED','APPROVED','ARCHIVED','IMPORTED_SHADOW')),
    classification TEXT NOT NULL CHECK (classification IN ('FACT','INFERENCE','SCENARIO','MIXED','RETROSPECTIVE_EXTRACTED')),
    market_timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    created_at TIMESTAMPTZ NOT NULL,
    data_cutoff TIMESTAMPTZ,
    created_by TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    parent_case_id UUID REFERENCES ledger.research_case(case_id),
    amendment_reason TEXT
);

CREATE TABLE IF NOT EXISTS ledger.source_record (
    source_id UUID PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    publisher TEXT,
    published_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ,
    data_cutoff TIMESTAMPTZ,
    license_class TEXT NOT NULL DEFAULT 'UNVERIFIED',
    reliability_tier TEXT NOT NULL DEFAULT 'UNVERIFIED',
    content_hash TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    captured_by TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source_locator, content_hash)
);

CREATE TABLE IF NOT EXISTS ledger.research_snapshot (
    snapshot_id UUID PRIMARY KEY,
    case_id UUID NOT NULL REFERENCES ledger.research_case(case_id),
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    snapshot_type TEXT NOT NULL,
    classification TEXT NOT NULL CHECK (classification IN ('FACT','INFERENCE','SCENARIO','MIXED','RETROSPECTIVE_EXTRACTED')),
    observed_at TIMESTAMPTZ,
    data_cutoff TIMESTAMPTZ,
    sealed_at TIMESTAMPTZ,
    storage_uri TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    prior_snapshot_id UUID REFERENCES ledger.research_snapshot(snapshot_id),
    amendment_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE (case_id, version_no),
    UNIQUE (storage_uri, content_hash)
);

CREATE TABLE IF NOT EXISTS ledger.evidence_snapshot (
    evidence_id UUID PRIMARY KEY,
    case_id UUID NOT NULL REFERENCES ledger.research_case(case_id),
    source_id UUID REFERENCES ledger.source_record(source_id),
    snapshot_id UUID REFERENCES ledger.research_snapshot(snapshot_id),
    evidence_role TEXT NOT NULL CHECK (evidence_role IN ('FACT','INFERENCE_SUPPORT','SCENARIO_SUPPORT','COUNTER_EVIDENCE','MODEL_ARTIFACT','DELIVERY_RECEIPT')),
    claim_scope_hash TEXT NOT NULL,
    observed_at TIMESTAMPTZ,
    data_cutoff TIMESTAMPTZ,
    content_hash TEXT NOT NULL,
    storage_uri TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger.review_round (
    round_id UUID PRIMARY KEY,
    case_id UUID NOT NULL REFERENCES ledger.research_case(case_id),
    forecast_id UUID,
    status TEXT NOT NULL CHECK (status IN ('BLIND','REVEALED','CLOSED','CANCELLED')),
    deadline_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL,
    round_hash TEXT NOT NULL,
    UNIQUE (case_id, deadline_at)
);

CREATE TABLE IF NOT EXISTS ledger.review_submission (
    submission_id UUID PRIMARY KEY,
    round_id UUID NOT NULL REFERENCES ledger.review_round(round_id),
    role_name TEXT NOT NULL CHECK (role_name IN ('BULL','BEAR','FORENSIC_FINANCE','INDUSTRY','SOURCE_AUDIT','RISK_OFFICER')),
    actor_id TEXT NOT NULL,
    classification TEXT NOT NULL CHECK (classification IN ('FACT','INFERENCE','SCENARIO','MIXED')),
    content_hash TEXT NOT NULL,
    storage_uri TEXT,
    submitted_at TIMESTAMPTZ NOT NULL,
    UNIQUE (round_id, role_name)
);

CREATE TABLE IF NOT EXISTS ledger.risk_decision (
    risk_decision_id UUID PRIMARY KEY,
    round_id UUID NOT NULL REFERENCES ledger.review_round(round_id),
    case_id UUID NOT NULL REFERENCES ledger.research_case(case_id),
    decision TEXT NOT NULL CHECK (decision IN ('VETO','CLEAR','OVERRIDE_REQUESTED','OVERRIDDEN')),
    reason_hash TEXT NOT NULL,
    evidence_gap_code TEXT,
    decided_at TIMESTAMPTZ NOT NULL,
    actor_id TEXT NOT NULL,
    override_approval_id UUID
);

CREATE TABLE IF NOT EXISTS ledger.forecast_protocol (
    protocol_id UUID PRIMARY KEY,
    protocol_version TEXT NOT NULL,
    protocol_hash TEXT NOT NULL UNIQUE,
    universe_rule_hash TEXT NOT NULL,
    target_definition_hash TEXT NOT NULL,
    source_policy_hash TEXT NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('FROZEN','RETIRED'))
);

CREATE TABLE IF NOT EXISTS ledger.forecast_batch (
    batch_id UUID PRIMARY KEY,
    protocol_id UUID REFERENCES ledger.forecast_protocol(protocol_id),
    external_batch_key TEXT UNIQUE,
    target_session DATE,
    correlation_group TEXT NOT NULL,
    expected_universe_size INTEGER,
    qualified_coverage INTEGER,
    batch_status TEXT NOT NULL CHECK (batch_status IN ('PENDING','SEALED','BLOCKED','VETOED','SETTLING','SETTLED','IMPORTED_SHADOW')),
    created_at TIMESTAMPTZ NOT NULL,
    sealed_at TIMESTAMPTZ,
    content_hash TEXT NOT NULL,
    imported_from TEXT
);

CREATE TABLE IF NOT EXISTS ledger.forecast (
    forecast_id UUID PRIMARY KEY,
    case_id UUID REFERENCES ledger.research_case(case_id),
    batch_id UUID REFERENCES ledger.forecast_batch(batch_id),
    protocol_id UUID REFERENCES ledger.forecast_protocol(protocol_id),
    parent_forecast_id UUID REFERENCES ledger.forecast(forecast_id),
    forecast_type TEXT NOT NULL CHECK (forecast_type IN ('BINARY_EVENT','NUMERIC_RANGE')),
    status TEXT NOT NULL CHECK (status IN ('DRAFT','SEALED','SUPERSEDED','SETTLED','VOID','IMPORTED_SHADOW')),
    target_key TEXT NOT NULL,
    metric_definition_hash TEXT NOT NULL,
    target_session DATE,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    probability NUMERIC,
    confidence_level NUMERIC,
    lower_bound NUMERIC,
    upper_bound NUMERIC,
    baseline_hash TEXT NOT NULL,
    invalidation_hash TEXT NOT NULL,
    settlement_rule_hash TEXT NOT NULL,
    model_version TEXT NOT NULL,
    feature_snapshot_hash TEXT NOT NULL,
    source_hashes JSONB NOT NULL DEFAULT '[]'::jsonb,
    data_cutoff TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    sealed_at TIMESTAMPTZ,
    prospective_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    retrospective_reason TEXT,
    content_hash TEXT NOT NULL,
    CHECK ((forecast_type = 'BINARY_EVENT' AND probability IS NOT NULL AND confidence_level IS NULL AND lower_bound IS NULL AND upper_bound IS NULL)
        OR (forecast_type = 'NUMERIC_RANGE' AND probability IS NULL AND confidence_level > 0 AND confidence_level < 1 AND lower_bound <= upper_bound)),
    CHECK ((forecast_type <> 'BINARY_EVENT') OR (probability >= 0 AND probability <= 1)),
    CHECK (window_end > window_start)
);

ALTER TABLE ledger.review_round
  ADD CONSTRAINT review_round_forecast_fk FOREIGN KEY (forecast_id) REFERENCES ledger.forecast(forecast_id);

CREATE TABLE IF NOT EXISTS ledger.outcome_observation (
    outcome_id UUID PRIMARY KEY,
    forecast_id UUID NOT NULL REFERENCES ledger.forecast(forecast_id),
    revision_no INTEGER NOT NULL CHECK (revision_no > 0),
    outcome_status TEXT NOT NULL CHECK (outcome_status IN ('PENDING_REVIEW','REVIEWED','CONFLICTED','MISSING','VOID')),
    observed_value NUMERIC,
    observed_at TIMESTAMPTZ,
    source_id UUID REFERENCES ledger.source_record(source_id),
    input_hash TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    recorded_by TEXT NOT NULL,
    prior_outcome_id UUID REFERENCES ledger.outcome_observation(outcome_id),
    UNIQUE (forecast_id, revision_no)
);

CREATE TABLE IF NOT EXISTS ledger.settlement (
    settlement_id UUID PRIMARY KEY,
    forecast_id UUID NOT NULL REFERENCES ledger.forecast(forecast_id),
    outcome_id UUID NOT NULL REFERENCES ledger.outcome_observation(outcome_id),
    revision_no INTEGER NOT NULL CHECK (revision_no > 0),
    settlement_status TEXT NOT NULL CHECK (settlement_status IN ('PENDING_REVIEW','REVIEWED','REJECTED','VOID')),
    research_correctness_score NUMERIC,
    market_performance_score NUMERIC,
    investment_return NUMERIC,
    investment_return_status TEXT NOT NULL CHECK (investment_return_status IN ('NOT_APPLICABLE','PENDING','AVAILABLE')),
    settlement_hash TEXT NOT NULL,
    settled_at TIMESTAMPTZ NOT NULL,
    reviewed_at TIMESTAMPTZ,
    reviewed_by TEXT,
    prior_settlement_id UUID REFERENCES ledger.settlement(settlement_id),
    UNIQUE (forecast_id, revision_no)
);

CREATE TABLE IF NOT EXISTS ledger.evaluation (
    evaluation_id UUID PRIMARY KEY,
    settlement_id UUID NOT NULL REFERENCES ledger.settlement(settlement_id),
    evaluation_type TEXT NOT NULL CHECK (evaluation_type IN ('RESEARCH_CORRECTNESS','MARKET_PERFORMANCE','INVESTMENT_RETURN')),
    score NUMERIC,
    rationale_hash TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL,
    evaluated_by TEXT NOT NULL,
    UNIQUE (settlement_id, evaluation_type)
);

CREATE TABLE IF NOT EXISTS ledger.calibration_snapshot (
    calibration_snapshot_id UUID PRIMARY KEY,
    metric_type TEXT NOT NULL CHECK (metric_type IN ('BINARY_BRIER','NUMERIC_RANGE')),
    status TEXT NOT NULL CHECK (status IN ('INSUFFICIENT_PROSPECTIVE_SAMPLES','PROVISIONAL','STABLE')),
    eligible_forecast_count INTEGER NOT NULL,
    independent_batch_count INTEGER NOT NULL,
    excluded_count INTEGER NOT NULL,
    input_filter_hash TEXT NOT NULL,
    metric_payload JSONB NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL,
    computed_by TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger.calibration_suggestion (
    suggestion_id UUID PRIMARY KEY,
    calibration_snapshot_id UUID NOT NULL REFERENCES ledger.calibration_snapshot(calibration_snapshot_id),
    status TEXT NOT NULL CHECK (status IN ('PENDING_HUMAN_APPROVAL','APPROVED_CANDIDATE','REJECTED','REPLAY_REQUIRED','ACTIVATED','REVOKED')),
    candidate_hash TEXT NOT NULL,
    champion_hash TEXT NOT NULL,
    rationale_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger.model_artifact (
    model_artifact_id UUID PRIMARY KEY,
    source_system TEXT NOT NULL CHECK (source_system IN ('QLIB','MLFLOW','KRONOS','OTHER')),
    artifact_uri TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    artifact_role TEXT NOT NULL CHECK (artifact_role IN ('EXPERIMENT','CANDIDATE_MODEL','SCENARIO','BASELINE')),
    data_cutoff TIMESTAMPTZ,
    universe_hash TEXT NOT NULL,
    feature_hash TEXT NOT NULL,
    label_hash TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    random_seed TEXT,
    split_hash TEXT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL,
    registered_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('REGISTERED','SCENARIO_ONLY','APPROVED_FOR_REPLAY','REJECTED')),
    UNIQUE (source_system, artifact_hash)
);

CREATE TABLE IF NOT EXISTS ledger.model_change_proposal (
    proposal_id UUID PRIMARY KEY,
    model_artifact_id UUID NOT NULL REFERENCES ledger.model_artifact(model_artifact_id),
    suggestion_id UUID REFERENCES ledger.calibration_suggestion(suggestion_id),
    status TEXT NOT NULL CHECK (status IN ('PROPOSAL','AWAITING_USER_REPLAY_APPROVAL','REPLAYED','AWAITING_USER_ACTIVATION_APPROVAL','ACTIVATED','REJECTED','REVOKED')),
    candidate_hash TEXT NOT NULL,
    champion_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL,
    rationale_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger.human_approval (
    approval_id UUID PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_id UUID NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('CASE_APPROVE','VETO_OVERRIDE','SUGGESTION_APPROVE','REPLAY_APPROVE','MODEL_ACTIVATE','DISPATCH_APPROVE')),
    decision TEXT NOT NULL CHECK (decision IN ('APPROVED','REJECTED')),
    reason_hash TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    approved_at TIMESTAMPTZ NOT NULL,
    prior_approval_id UUID REFERENCES ledger.human_approval(approval_id)
);

ALTER TABLE ledger.risk_decision
  ADD CONSTRAINT risk_decision_override_fk FOREIGN KEY (override_approval_id) REFERENCES ledger.human_approval(approval_id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE IF NOT EXISTS ledger.replay_run (
    replay_id UUID PRIMARY KEY,
    proposal_id UUID REFERENCES ledger.model_change_proposal(proposal_id),
    suggestion_id UUID REFERENCES ledger.calibration_suggestion(suggestion_id),
    cutoff_at TIMESTAMPTZ NOT NULL,
    split_hash TEXT NOT NULL,
    champion_hash TEXT NOT NULL,
    candidate_hash TEXT NOT NULL,
    formal_snapshot_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('RUNNING','COMPLETED','INSUFFICIENT','UNSUPPORTED','INVALIDATED')),
    result_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger.delivery_event (
    delivery_id UUID PRIMARY KEY,
    case_id UUID REFERENCES ledger.research_case(case_id),
    snapshot_id UUID REFERENCES ledger.research_snapshot(snapshot_id),
    approval_id UUID REFERENCES ledger.human_approval(approval_id),
    channel TEXT NOT NULL CHECK (channel IN ('HERMES','TELEGRAM','OTHER')),
    direction TEXT NOT NULL CHECK (direction IN ('DISPATCH_REQUEST','DELIVERY_RECEIPT','IMPORTED_METADATA_ONLY')),
    status TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    external_reference_hash TEXT,
    occurred_at TIMESTAMPTZ NOT NULL,
    recorded_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger.import_ledger (
    import_id UUID PRIMARY KEY,
    source_system TEXT NOT NULL,
    source_database_hash TEXT,
    source_table TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_record_hash TEXT NOT NULL,
    original_recorded_at TIMESTAMPTZ,
    original_sealed_at TIMESTAMPTZ,
    import_batch_id UUID NOT NULL,
    import_status TEXT NOT NULL CHECK (import_status IN ('IMPORTED_SHADOW','RETROSPECTIVE_EXTRACTED','EXCLUDED','ERROR')),
    target_object_type TEXT,
    target_object_id UUID,
    imported_at TIMESTAMPTZ NOT NULL,
    imported_by TEXT NOT NULL,
    reason_code TEXT,
    UNIQUE (source_system, source_database_hash, source_table, source_record_id, source_record_hash)
);

CREATE TABLE IF NOT EXISTS ledger.capability (
    capability_id UUID PRIMARY KEY,
    capability_hash TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    role_name TEXT NOT NULL CHECK (role_name IN ('ADMIN','ANALYST','BULL','BEAR','FORENSIC_FINANCE','INDUSTRY','SOURCE_AUDIT','RISK_OFFICER','PRODUCER','SETTLER','REVIEWER','MODEL_REGISTRAR','DISPATCHER')),
    scope_type TEXT NOT NULL,
    scope_id UUID,
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL,
    expires_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS ledger.capability_revocation (
    revocation_id UUID PRIMARY KEY,
    capability_id UUID NOT NULL REFERENCES ledger.capability(capability_id),
    revoked_at TIMESTAMPTZ NOT NULL,
    revoked_by TEXT NOT NULL,
    reason_hash TEXT NOT NULL,
    UNIQUE(capability_id)
);

CREATE TABLE IF NOT EXISTS ledger.audit_event (
    audit_id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id UUID,
    input_hash TEXT NOT NULL,
    result_hash TEXT,
    correlation_id UUID,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE OR REPLACE FUNCTION ledger.prevent_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ledger records are append-only; create a new revision or event instead';
END;
$$;

DO $$
DECLARE tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
      'research_case','source_record','research_snapshot','evidence_snapshot','review_round','review_submission',
      'risk_decision','forecast_protocol','forecast_batch','forecast','outcome_observation','settlement','evaluation',
      'calibration_snapshot','calibration_suggestion','model_artifact','model_change_proposal','human_approval',
      'replay_run','delivery_event','import_ledger','capability','capability_revocation','audit_event'
    ] LOOP
      EXECUTE format('DROP TRIGGER IF EXISTS %I ON ledger.%I', 'no_mutation_' || tbl, tbl);
      EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON ledger.%I FOR EACH ROW EXECUTE FUNCTION ledger.prevent_mutation()', 'no_mutation_' || tbl, tbl);
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS idx_forecast_formal ON ledger.forecast (prospective_eligible, status, forecast_type);
CREATE INDEX IF NOT EXISTS idx_outcome_forecast ON ledger.outcome_observation (forecast_id, outcome_status);
CREATE INDEX IF NOT EXISTS idx_review_submission_round ON ledger.review_submission (round_id, role_name);
CREATE INDEX IF NOT EXISTS idx_import_source ON ledger.import_ledger (source_system, import_batch_id);
CREATE INDEX IF NOT EXISTS idx_audit_object ON ledger.audit_event (object_type, object_id, occurred_at);

INSERT INTO ledger.schema_migration(version, artifact_hash)
VALUES ('001_initial', :'migration_hash')
ON CONFLICT (version) DO NOTHING;

COMMIT;
