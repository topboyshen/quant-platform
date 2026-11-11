# Read-only health-check compatibility

The three scoped code/test files were installed in `/home/ubuntu/research-data`
on 2026-09-09 at approximately 17:01 CST after baseline CAS checks. All 77
offline tests passed on the VPS. Tests used temporary fixtures only: no production
database connection, snapshot execution, model call, or message delivery occurred.
Qlib promotion and its scheduled entry have separate acceptance records in
`QLIB_INCREMENTAL_DELIVERY_2026-09-09.md` in the companion local outputs directory.

## Scope

The original health check rejected reference dates after the canonical release
tag. A same-source incremental generation deliberately keeps that true base
release tag, so a validated 09-08 calendar/reference tail with base release 09-06
needs a distinct provenance path, not a forged tag.

This patch reads `.qlib-dolt-incremental.json` with a 64 KiB bound and strict JSON.
It requires the producer's version/kind/validated status, repository, syntactically
valid commit and generation IDs, true base release tag, advancing base/target
dates, complete calendarAdded suffix, source-row digest, and reasonably shaped
unchanged historical-prefix claim. Calendar and all four reference fields/dates
must agree with the target. Reference physical tails cannot exceed or lag the
incremental calendar. The reader checks producer validation summaries, not the
underlying full-market feature files or individual source price rows.

The controlled producer must include `sourceAbsence`, `priceValidation`,
`legacyUnquotedCSI`, and `indexPriceValidation`. Current-inventory daily counts,
ratios, invalid-symbol sets and the two allowed missing-price/prior-close reasons
must agree. Every disclosed new-day absence (and the next day's missing prior
close) must appear in the matching daily invalid claim. The same commit is
required throughout. Old tail-gap proof is calendar-bounded and at most ten old
days per symbol; sparse `lastQuotedDate` covers only source-absence symbols.
This keeps the manifest within the unchanged 64 KiB limit.

Optional `storedShortHistoryEvidence` separately describes previously stored
short-IPO missing slots; it never relabels them as physical tail gaps. When
present, it must use the same source commit, contain only per-symbol subsets of
`sourceAbsence`, and list 1–10 unique sorted old calendar dates within the ten
trading days ending at `baseDate` (including base). A last-quoted date cannot be
declared absent. Without the field, the evidence map is empty and no additional
old dates are authorized. This bounded local proof shape does not independently
re-run the producer's complete same-commit short-history source query.

CSI historical unquoted inventory has a separate proof: unchanged pre-base
last-quote dates, every new date absent, and no overlap with current-inventory
absence. CSI quote/usable-return counts, per-index count bounds and explicit
missing-member lists must be mutually consistent. A partial claim without its
matching nonempty evidence is rejected. These are claim checks, not an independent
CSI membership or market-wide scan.

`generation(root)` retains the release tag at tuple index 1 and adds the exact
manifest-byte SHA-256, validated identity and device. Changes, disappearance or
replacement of the manifest during either Qlib reading or the subsequent audit
are rejected even when the root inode and release tag stay unchanged.

No manifest means the original legacy rules still apply, including allowance for
a calendar leading its reference data. A missing manifest cannot authorize a
reference date after the release tag. Malformed, oversized, symlinked or missing-
field manifests never downgrade to legacy. The output adds a whitelisted
`qlib.incremental` description and keeps `releaseTag` unchanged. `dataQuality` is
`partial` when either inventory view is partial, otherwise the validated current
incremental claim is `complete`; legacy releases without a manifest remain
`unknown`. `qlib_source_absence` warns about current-inventory/new-price gaps,
while `qlib_legacy_unquoted_csi` separately identifies original CSI unquoted
inventory. A source absence is never labeled a confirmed suspension by this
reader. Earlier new-day partial coverage remains partial even after the final
day has recovered.

## Important limits

The manifest is local evidence, not a signed attestation. Shape checks and a
concurrency digest do not authenticate an already forged, internally consistent
manifest, recompute the original full source-row digest, or rerun historical
prefix/full-market validation. Output therefore retains `fullMarketCoverage:
not_scanned`, independent upstream freshness unknown, and automatic recovery
unauthorized. No snapshot compatibility changes are included.

## Regression evidence

```sh
python3 -B -m unittest discover -s tests -v
```

77 passed: all 35 original audit tests plus five incremental/quality audit tests,
and 37 dedicated incremental Qlib tests. The original audit tests create their SQLite
files only under TemporaryDirectory. Dedicated Qlib tests prohibit all SQLite,
socket and subprocess access. Read-only fingerprints verify unchanged fixture
files and databases. Covered cases include legacy behavior, missing/malformed
manifests, bad core fields and prefix claims, incomplete calendar suffixes,
reference tail/date mismatch, and manifest changes/removal during reads/audits.
Quality cases cover complete/partial/legacy, daily and combined status arithmetic,
recovery with missing prior close, bidirectional proof/reason checks, old tail
gaps, contradictory or missing nested fields, separate CSI legacy inventory,
source-commit mismatches, and source-quality changes during reads. Short-history
cases cover the inclusive ten-day bound, absent/invalid proof fields, mapping
subsets, calendar/future/last-quote violations, and proof removal during reading.
The unchanged parent `test_candidate.py` also passes all 13 precommit-admission
tests against this consumer revision.

## Deployment CAS inputs (read-only captured)

Repository `/home/ubuntu/research-data`, HEAD
`8308a1aeb05367a0f0a4b32f9d4bd69d41dbb221`.

- Original `data_health.py` SHA-256:
  `91c7f80563fc6d350b9d23260c08c89ee80fd98e2bd3f22b59569260fd5f1d6e`
- Original `tests/test_data_health.py` SHA-256:
  `3891851e7d379b27dc457c5572bb378d054fba336edf2105959fbd505ae0ba9d`
- `tests/test_qlib_incremental.py` is new; deployment must require that target to
  be absent or already match the intended candidate, rather than overwrite it.

Machine-readable original-file metadata: `deployment-base.json`. A deployer must
recheck current file hashes/HEAD and preserve unrelated parallel edits; these
recorded values are an observation, not proof that production remains unchanged.
