BEGIN;

ALTER TABLE ledger.research_snapshot
  DROP CONSTRAINT IF EXISTS research_snapshot_storage_uri_content_hash_key;

INSERT INTO ledger.schema_migration(version, artifact_hash)
VALUES ('002_snapshot_cas_dedup', :'migration_hash')
ON CONFLICT (version) DO NOTHING;

COMMIT;
