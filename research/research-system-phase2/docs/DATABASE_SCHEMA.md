# Phase2 database schema

## Authority and version boundary

The production database is SQLite at `/var/lib/research-system-phase2/phase2.db`. The schema source
of truth is the `init(path)` function in `app/phase2.py`. The dashboard, worker, statistics code and
manual-role provisioning call `init()` before use.

Current schema management is deliberately simple but has an important limitation:

- `init()` runs `CREATE TABLE IF NOT EXISTS` for the eleven tables below.
- There is no migration directory, migration ledger or `PRAGMA user_version` assignment.
- `PROTOCOL_SCHEMA = "phase2-v1"` and protocol JSON version `1.0.0` describe the research protocol;
  they are not database migration versions.
- Existing columns, constraints and data are not upgraded by `CREATE TABLE IF NOT EXISTS`.

Consequently, adding or changing a column, key or constraint requires a separately reviewed,
idempotent migration with pre/post integrity checks and a rollback plan. Do not treat a dashboard
restart as a database migration.

## Connection contract

`db(path)` enables `PRAGMA foreign_keys=ON` on each application connection and returns rows by name.
The current DDL declares logical relationships but no SQL `FOREIGN KEY` clauses. Referential
relationships are therefore enforced by application flow and unique keys, not by SQLite foreign-key
constraints. `PRAGMA foreign_key_check` remains part of backup and restore validation, but an empty
result does not prove that every logical parent exists.

Production ownership is:

| Path | Owner | Mode | Purpose |
|---|---|---:|---|
| `/var/lib/research-system-phase2` | `researchphase2:researchphase2` | `0700` | Private state directory |
| `phase2.db` | `researchphase2:researchphase2` | `0644` inside the private directory | Live SQLite database |

The service unit grants write access only to `/var/lib/research-system-phase2`; the HTTP listener is
loopback-only and exposes no write route.

## Logical relationship map

```text
protocol.protocol_hash
  ├─ batch.protocol_hash
  │    ├─ exclusion.batch_id
  │    ├─ source_snapshot.batch_id
  │    ├─ forecast.batch_id ── outcome.forecast_id
  │    └─ risk_veto.batch_id
  └─ calibration_suggestion.protocol_hash

manual_capability  (independent role-token hashes)
audit              (append-only application events)
run_state          (latest producer/settler result by name)
```

These arrows are logical relationships only; they are not declared SQL foreign keys.

## Table dictionary

### `protocol`

One immutable JSON snapshot per canonical protocol hash.

| Column | Contract |
|---|---|
| `protocol_hash TEXT PRIMARY KEY` | SHA-256 of canonical JSON |
| `protocol_id TEXT NOT NULL` | Stable protocol identifier |
| `version TEXT NOT NULL` | Protocol document version |
| `protocol_json TEXT NOT NULL` | Sorted JSON snapshot |
| `frozen_at TEXT NOT NULL` | Protocol freeze time |
| `created_at TEXT NOT NULL` | Registration time |

Registration uses `INSERT OR IGNORE`; rewriting an existing protocol hash is not supported.

### `audit`

Application audit events. Each row contains a hash of its canonical payload plus the JSON payload.

| Column | Contract |
|---|---|
| `event_id TEXT PRIMARY KEY` | Generated `audit-*` identifier |
| `action TEXT NOT NULL` | Fixed application action |
| `actor TEXT NOT NULL` | Producer, settler or manual role |
| `occurred_at TEXT NOT NULL` | Asia/Shanghai ISO timestamp |
| `payload_hash TEXT NOT NULL` | Canonical payload SHA-256 |
| `payload_json TEXT NOT NULL` | Canonical JSON payload |

The application only inserts audit rows; no update/delete path is exposed.

### `batch`

One prospective cohort for a protocol and target session.

| Column | Contract |
|---|---|
| `batch_id TEXT PRIMARY KEY` | Generated batch identifier |
| `cohort_id TEXT NOT NULL` | Protocol/data-cutoff cohort |
| `protocol_hash TEXT NOT NULL` | Logical link to `protocol` |
| `data_cutoff TEXT NOT NULL` | Source session used for features |
| `target_session TEXT NOT NULL` | Forward session being forecast |
| `status TEXT NOT NULL` | `COLLECTING`, `BLOCKED`, `SEALED` or `VETOED` |
| `expected_n INTEGER NOT NULL` | Frozen expected cohort size |
| `nominal_n INTEGER NOT NULL DEFAULT 0` | Sealed forecast count |
| `qualified_n INTEGER NOT NULL DEFAULT 0` | Qualified symbol count |
| `independent_batch_count INTEGER NOT NULL DEFAULT 1` | Reporting field |
| `block_reason TEXT` | Fail-closed reason when blocked/vetoed |
| `created_at TEXT NOT NULL` | Creation time |
| `sealed_at TEXT` | Seal time |

Unique key: `(protocol_hash, target_session)`. A blocked batch is retried in place; a sealed batch is
idempotent for the same key.

### `exclusion`

Per-symbol reasons that a source item was excluded from a batch.

| Column | Contract |
|---|---|
| `exclusion_id TEXT PRIMARY KEY` | Generated identifier |
| `batch_id TEXT NOT NULL` | Logical link to `batch` |
| `symbol TEXT NOT NULL` | Frozen public-universe symbol |
| `reason TEXT NOT NULL` | Deterministic exclusion reason |
| `source_hash TEXT` | Optional source evidence hash |
| `created_at TEXT NOT NULL` | Record time |

Unique key: `(batch_id, symbol)`.

### `source_snapshot`

Hashes and provenance for each fetched public-source response.

| Column | Contract |
|---|---|
| `snapshot_id TEXT PRIMARY KEY` | Generated identifier |
| `batch_id TEXT NOT NULL` | Logical link to `batch` |
| `symbol TEXT NOT NULL` | Source symbol |
| `source_url TEXT NOT NULL` | Fixed public endpoint |
| `fetched_at TEXT NOT NULL` | Fetch time |
| `content_hash TEXT NOT NULL` | Raw response SHA-256 |
| `normalized_hash TEXT NOT NULL` | Canonical history-row hash |
| `latest_session TEXT NOT NULL` | Latest session in the response |

Unique key: `(batch_id, symbol, content_hash)`.

### `forecast`

Sealed, forward-looking probabilities. Forecast timestamps must precede `target_session`.

| Column | Contract |
|---|---|
| `forecast_id TEXT PRIMARY KEY` | Generated identifier |
| `batch_id TEXT NOT NULL` | Logical link to `batch` |
| `cohort_id TEXT NOT NULL` | Correlated cohort identifier |
| `symbol TEXT NOT NULL` | Forecast symbol |
| `target_session TEXT NOT NULL` | Future evaluation session |
| `data_cutoff TEXT NOT NULL` | Last permissible feature session |
| `created_at`, `registered_at`, `sealed_at TEXT NOT NULL` | Lifecycle timestamps |
| `probability REAL NOT NULL` | Clamped probability in application logic |
| `model_version TEXT NOT NULL` | Frozen model identifier |
| `feature_snapshot_hash TEXT NOT NULL` | Canonical feature/protocol hash |
| `source_hashes_json TEXT NOT NULL` | JSON source-hash list |
| `correlation_group TEXT NOT NULL` | Same-cutoff dependence group |
| `status TEXT NOT NULL DEFAULT 'SEALED'` | Current forecast state |

Unique key: `(batch_id, symbol)`.

### `outcome`

Observed target-session result, initially excluded from formal calibration until independent review.

| Column | Contract |
|---|---|
| `outcome_id TEXT PRIMARY KEY` | Generated identifier |
| `forecast_id TEXT NOT NULL UNIQUE` | At most one outcome per forecast |
| `observed_close`, `prior_close REAL NOT NULL` | Target/prior closes |
| `outcome INTEGER NOT NULL` | Application emits binary `0` or `1` |
| `source_url`, `source_as_of`, `content_hash TEXT NOT NULL` | Provenance |
| `settled_at TEXT NOT NULL` | Settlement time |
| `status TEXT NOT NULL DEFAULT 'PENDING_REVIEW'` | `PENDING_REVIEW` or `APPROVED` |
| `settler TEXT NOT NULL` | Settler identity |
| `reviewed_at`, `reviewed_by`, `review_hash TEXT` | Independent review evidence |

The database has no SQL `CHECK` constraint for the binary/status domains; application gates are the
current enforcement layer.

### `run_state`

Latest structured worker result per job.

| Column | Contract |
|---|---|
| `name TEXT PRIMARY KEY` | `producer` or `settler` |
| `value TEXT NOT NULL` | Sorted JSON result |
| `updated_at TEXT NOT NULL` | Last update time |

Workers update this row with `ON CONFLICT(name) DO UPDATE`.

### `manual_capability`

Independent manual-role credentials. Only token hashes are stored in SQLite.

| Column | Contract |
|---|---|
| `role_name TEXT PRIMARY KEY` | `reviewer`, `risk_officer` or `calibration_approver` |
| `token_hash TEXT UNIQUE NOT NULL` | SHA-256 of the secret token |
| `created_at TEXT NOT NULL` | Provision time |
| `revoked_at TEXT` | Null while usable |

Provisioning is root-only and writes plaintext tokens only to a separately protected output file.

### `risk_veto`

Independent risk-officer veto records.

| Column | Contract |
|---|---|
| `veto_id TEXT PRIMARY KEY` | Generated identifier |
| `batch_id TEXT NOT NULL` | Logical link to `batch` |
| `reason TEXT NOT NULL` | Required reason |
| `evidence_hash TEXT NOT NULL` | Expected 64-character evidence hash |
| `created_at TEXT NOT NULL` | Veto time |
| `reviewer TEXT NOT NULL` | Risk role identity |
| `overridden_at`, `overridden_by`, `override_reason TEXT` | Reserved override evidence |

The current code creates vetoes but exposes no automatic override path.

### `calibration_suggestion`

Human-governed suggestions only; no service or timer may activate them.

| Column | Contract |
|---|---|
| `suggestion_id TEXT PRIMARY KEY` | Generated identifier |
| `protocol_hash TEXT NOT NULL` | Logical link to `protocol` |
| `proposed_change TEXT NOT NULL` | Suggested change |
| `status TEXT NOT NULL DEFAULT 'PENDING_HUMAN_APPROVAL'` | Non-activated state |
| `created_at TEXT NOT NULL` | Creation time |
| `created_by TEXT NOT NULL` | Manual approver role |

## Backup and validation implications

The independent backup job uses the SQLite backup API rather than copying a live database file. It
then records `PRAGMA quick_check`, `PRAGMA integrity_check`, `PRAGMA foreign_key_check` and per-table
row counts in `phase2-db-check.json`. The archive also contains `SHA256SUMS`, the frozen protocol,
runtime source, Python inventory and deployed systemd definitions.

These checks detect corruption and archive drift. They do not replace semantic checks for the
logical relationships above or establish off-host disaster recovery. Follow `RESTORE_RUNBOOK.md`
for a staged validation and controlled cutover.
