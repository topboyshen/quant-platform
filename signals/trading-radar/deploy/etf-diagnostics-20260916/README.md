# ETF diagnostics release — 2026-09-16

This release adds run-bound explanations and separately qualified history. It does
not loosen strategy gates or turn historical triggers into current buy signals.
Older runs show `not_recorded`; new diagnostics require a future natural EOD run.

## Evidence

- Linux host-runtime isolated targeted regression: 270 passed, no failures/skips.
- Local frontend targeted regression: 218 passed. Two tests that require external
  sibling backend Compose fixtures were explicitly excluded from the frozen archive.
- 55 original/candidate strategy-equivalence cases passed; synthetic browser UI
  checks passed. These are not proof of profitable trading or natural EOD execution.
- Candidate-image and live read-only API checks: health/ready/context 200,
  unauthorized 401, 10 configured ETFs. SQLite table counts unchanged during checks.
- Original/candidate API package-list hash identical:
  `2b8611139da13e868657265535b940901dd979928380a8c5b5ffe7e65f37d364`.
- Exact image IDs, source/archive hashes and retained rollback images are recorded
  in `release.json`; deployment invariants are recorded in `deployed.json`.

Two older notification tests fail on the untouched base as well because they expect
immediate Telegram delivery rather than the existing daily-report path. They are
outside the 270-test list; notification policy was not changed to satisfy them.

## Source and reproduction

Host/API source is tracked in the repository; `frontend-overlay/` records precisely
the five changed frontend source/test files. The complete frozen frontend source
archive and compiled dist archive are retained under
`releases/etf-diagnostics-20260916.1/`, with SHA-256 in the manifest. Do not rebuild
from the divergent `~/QuantDinger` checkout. Apple `._` metadata is never installed
as application source. The source archive may contain packaging metadata.

The dependency lock is unchanged. The production build used
`APP_VERSION=personal-2026.09.16-etf-diagnostics.1`, real-API/no-mock production
settings, and no new dependency installation. All frontend assets from the previous
image were retained for already-open tabs. The new index references the new build.

API image is a one-file overlay on the pinned 9/12 image. The tracked integration
Dockerfile uses repository-root `COPY dashboard/etf_pool.py`; the minimal staging
build used the identical bytes as `COPY etf_pool.py`. `SOURCE_SHA256` is the exact
API source hash. Frontend `SOURCE_SHA256` is its complete source-archive hash.
Any future build must use a new version tag and explicit source-hash argument;
never silently move the current tag. Use the canonical release wrapper to deploy.

## Scope and rollback

Only integration-api and quantdinger-frontend were recreated. The host's diagnostic
producer code changed only while related natural tasks were inactive. No strategy,
configuration, timers, notification, proxy, backend, Hermes, or database changes
were made. No database backups, replay, backfill, historical recomputation, extra
Telegram messages, push, cleanup of previous audit failures, or image pruning.

For a container rollback, change ONLY its image in its Compose definition and its
matching entry in `deploy/production_pin.json` to the retained old values in
`release.json`; then use `/home/ubuntu/bin/trading-radar-compose up -d --no-deps
--no-build --pull never <that-service>`. Preserve every other pin entry. Do not
replace the entire old pin file when rolling back only one component.

For host-producer rollback, first wait until related natural tasks are inactive;
restore `radar/swing/trend_strategy.py` and `radar_swing_eod.py` together from base
`b7e5c7b5fe4a07f278ef9ad34ab198b9b49c43b8`, after checking for subsequent edits.
Reverting the host API source alone does not change the running API image.
No database restore/copy is needed or authorized. Existing diagnostic JSON is
additive; older code ignores it. Update release provenance after any rollback.

`deploy.py` is a one-use guarded record, not an idempotent generic deployment tool.
Do not rerun it after a partial failure without inspecting the actual state.
