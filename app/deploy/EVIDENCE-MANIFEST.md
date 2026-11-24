# External artifact evidence manifest (reviewed 2026-11-24)

The record chain stores `db_row` evidence for decisions. Model files, frozen
inputs and exported payloads are **external artifacts** and use this manifest
instead: each one is bound to a runtime identity (host, owner, mode), version,
byte size and SHA-256. A file that exists but has no expected identity is
`unknown`, never `verified`.

Last full scan: **2026-11-16 21:24 CST**. This document was reviewed on
2026-11-24 for the freeze baseline; a fresh scan is required before any M9
Phase 2 decision. The counts below are the last recorded scan, not a live claim.

Implementation: the read-only `batch4_acceptance.py` check 8 scans the artifacts
on nnG8 and writes them into `batch4-acceptance-*.json`. This document is the
contract; the acceptance script is the current scanner.

## 1. Required artifacts

| Role | Path | Expected identity source |
|---|---|---|
| `model_20d` | `/opt/qp-score/etf-score-model.joblib` | 20d meta `model_sha256` |
| `model_20d_meta` | `/opt/qp-score/etf-score-model-meta.json` | freeze `server_scripts` |
| `model_5d` | `/opt/qp-score/etf-score-5d-model.joblib` | freeze `five_day_shadow.model_sha256` |
| `model_5d_meta` | `/opt/qp-score/etf-score-5d-model-meta.json` | freeze `five_day_shadow.meta_sha256` |
| `score_daily` | `/opt/qp-score/score_daily.py` | freeze `server_scripts` |
| `research_train` | `/opt/qp-score/research_train.py` | freeze `server_scripts` |
| `etf_ml` | `/opt/qp-score/etf_ml.py` | freeze `server_scripts` |
| `freeze_meta_v2` | `/opt/qp-score/freeze_meta_v2.py` | freeze `server_scripts` |
| `calendar` | `/opt/qp-score/trading-calendar.tsv` | freeze `server_scripts` |
| `requirements_lock` | `/opt/qp/current/requirements.lock` | freeze `requirements_lock_sha256`; copied byte-for-byte from repo `app/backend/requirements-runtime.lock` by `qp-release-venv` |
| `master` | `/var/lib/qp-score/etf_master.sqlite` | **none** — mutable daily input; training snapshot is recorded in the model meta |
| `latest_20d` | `/var/lib/qp/etf-score/latest.json` | payload `model_sha256` + `model_version` = 20d model/meta |
| `latest_5d` | `/var/lib/qp/etf-score/latest-5d.json` | payload `model_sha256` + `model_version` = 5d model/meta |
| `dated_20d` | newest `/var/lib/qp/etf-score/etf-score-*.json` (excluding 5d) | same as `latest_20d` |
| `dated_5d` | newest `/var/lib/qp/etf-score/etf-score-5d-*.json` | same as `latest_5d` |

## 2. Fields recorded per artifact

`role`, `path`, `exists`, `owner` (`user:group`), `mode`, `bytes`, `mtime`,
`sha256`, `version`, `expected_sha256`, `expected_source`, `status`.

- The **actual** SHA-256 is always computed for an existing readable file.
- `version` is the model version for model files and the payload `model_version`
  for exports.
- Exports are also bound by `model_sha256` + `model_version` to the model file;
  their own content hash is recorded and compared with the imported DB rows by
  checks 2/3, not by a frozen file hash.

## 3. Status semantics

| Status | Meaning |
|---|---|
| `verified` | File exists and matches an expected hash / model identity |
| `unknown` | File exists, actual hash recorded, but no expected identity to compare against |
| `missing` | Required file does not exist |
| `mismatch` | File exists but differs from the expected hash or version |

Check 8 passes only when `missing == 0` and `mismatch == 0`; `unknown` is reported
explicitly and is not silently promoted to verified.

## 4. Current snapshot (2026-11-16 21:24 CST)

- `verified`: 14
- `unknown`: 1 (`master`, by design: it is appended daily)
- `missing`: 0
- `mismatch`: 0

## 5. Extending

1. Add the role and path to the scanner's artifact table.
2. Add the expected hash to `observation-freeze.json` (`server_scripts`,
   `five_day_shadow`, or a new section) only when the artifact is frozen.
3. Run the acceptance scan and keep the report; do not edit the status by hand.
4. A semantics-changing artifact (feature list, universe, amount unit,
   adjustment, model, threshold) is a new observation version, not a manifest
   update.
