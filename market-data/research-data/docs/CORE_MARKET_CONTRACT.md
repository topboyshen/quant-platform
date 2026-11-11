# Batch 3: isolated explicit-contract cutover preparation

2026-10-02 supersedes the single-boundary policy and runtime dependency details:
see [ESSENTIAL_CORE_20261002.md](ESSENTIAL_CORE_20261002.md). Legacy v1 remains readable;
v2 uses append-only future transitions and preserves core history after full resumes.

> Current isolated-candidate guide: [LIGHTWEIGHT_RUNTIME.md](LIGHTWEIGHT_RUNTIME.md).
> This document records earlier contract preparation; superseded behavior is
> marked below. No production cutover is implied by either document.

Production baseline: research-data d83c42d20331a8f016b74d8a9b81ff15a6462de4.
No production deployment, schedule, policy, service, database or historical
baseline is changed. Initial commit 428bc4d is superseded by this Qlib-only
design; use the final branch, not the earlier stage-dependent contract.

## Honest artifact scope

- Core: research.core-market.v1, scope=core_market, fullSnapshot=false,
  artifactKind=qlib_market_summary, includesCompletePriceArchive=false.
  Separate core-market.db / core_market table. Existing qlib_market(None)
  scans installed prices but serializes only coverage, breadth, counts and
  bounded rankings/limit lists plus generation tag/inode. It is NOT a complete
  per-security price archive and tag/inode is not an upstream commit.
- Appendix: research.appendix.v1, fullSnapshot=false, separate
  research-appendix.db / research_appendix table. Explicitly requested source
  groups observed at actual request time, bound to same-day core hash.
- Legacy full: existing snapshot.db and historical rows remain unchanged.
  The combined candidate changes its calendar/execution/notification guards;
  it is not the byte-identical old writer. Reduced records cannot make old rows valid.

The core performs no AkShare/research network calls. Original Qlib generation
before/after, reference, coverage >= 0.995 / eligible >= 5000, count/finite
checks remain. Partial quotes remain partial; no fake price or zero fallback.
AkShare market stage joins 14 other source groups in the on-demand appendix.
No-tables failure there cannot block core; it is a recorded failed source.
Unrequested is not_requested, not pass. Collected is collected_unreviewed,
not research-approved. Core rows are insert-only; conflicting dates fail;
exact retry is idempotent. Separate database collisions are refused.

## Calendar evidence and remaining dependency

A possibly lagging Qlib calendar cannot prove that no new day is due.
core_market.py --calendar-evidence PATH reads only existing local artifacts.
PATH can be the current sealed archive or AStock project root. No AStock
workflow, model, collector or service is invoked.

For AStock root, use the actual current Shanghai date:

1. An existing work/report-archives/<today> must pass regular-file/size bounds,
   input.sha256 and published.inputSha256 versus raw input hash,
   published.status=sealed, directory/input date, identical compiler/raw
   calendars, trusted source ID akshare:sina:trade-calendar, ordered dates,
   timezone-aware same-day observed/cutoff freshness. An invalid present
   archive blocks; it cannot fall back to a skip tuple.
2. If archive is absent, closed-day proof is the existing
   work/orchestrator/states/morning-report-<today>.json + work/last-run.json
   + raw-hash-bound context. Check task/date, successful noninterrupted skip,
   phase=collect, tradingStatus=non_trading_day,
   publicationGate=not_applicable, safe context path/hash, completed times,
   source/calendar identity. A marker alone is never proof.
3. Missing, stale, wrong-date/source or contradictory evidence => deferred.
   An open-day Qlib target must equal the verified calendar target and be no
   earlier than policy effectiveTradeDate, before any database write.
   Closed => skipped_non_trading, target=previousTradingDate; no Qlib scan
   and no fabricated core row.

The earlier successful-publication dependency is superseded in this candidate:
validated terminal context can supply calendar evidence even after report failure,
and bounded closed intervals cover days with no new publication occurrence.
See the current guide for exact identity and rejection rules. Missing, overwritten,
unbound or contradictory evidence still defers. Calendar collection still shares
AStock's producer, so two fully independent availability domains are NOT proven.
No hand-entered date or arbitrary failed raw context is accepted. Code preparation
does not authorize activation of a core policy.

## Preinstallable cutover, default legacy

No config/snapshot-contract.json is created or supplied. Policy must contain
exactly activeContract and effectiveTradeDate. Missing policy defaults to old
full; a future effective date remains full until Shanghai midnight on that date.
Malformed/symlinked present policy starts no writer (exit 78), not fallback.

Existing cron still calls scripts/run_daily_snapshot_with_alert.sh at 22:30;
the schedule is unchanged by this isolated work. No policy => full contract,
with the candidate's common calendar/bounded-execution guards and no wrapper
direct-send. Active core => core_market.py, independent core-market.db and
.local/state/research-data/core-market/last-run.json. Calendar root comes from
RESEARCH_DATA_CORE_CALENDAR_PROJECT_ROOT, default /home/ubuntu/AStock_AI_System.
There is no historical target argument. Legacy full entrypoint stays available
for an explicitly reviewed rollback, never an automatic core-failure fallback.

The common wrapper preserves nonzero writer exit and only records it; neither
full nor core sends Hermes/Telegram directly. The candidate's health path owns
data-chain notification. Any additional supervision wiring remains subject to
separate runtime installation and review; it has not been installed here.

The health service template adds --contract-policy/--core-db/--core-last-run.
Absent policy preserves the old audit. No actual unit/reload or switch policy
has been installed.

## Audit and consumer interface

snapshot, lastRun and ledger remain legacy evidence. ledger.expectedUnderContract
is true before cutover. Original critical gaps, invalid rows and row-hash
baselines persist. coreMarket has distinct ledger, row hashes and lastRun.
activeContract/effectiveTradeDate/contractPolicy/activeLastRun and
legacyFullSnapshot.status explicitly identify selection.

In the combined candidate, `activeRunDecision` is the authoritative current-run
verdict consumed by health alerts and the dashboard. The fields below remain
evidence and legacy compatibility interfaces, not an invitation for downstream
consumers to recompute competing coverage/date verdicts.

coreMarket.quality is returned only for a successful lastRun bound by hash to
a validated core row: targetDate/payloadSha256, priceStatus, valid/invalid/
eligible/ratio, generationTag, scope=sealed_core_qlib_summary. Never borrow a
newer installed Qlib statistic to describe an older core artifact.

recent_core_run accepts skipped_non_trading only with hash-bound closed
calendar, matching previous target, same-day times and no payload hash claim.
It exports calendarClosedVerified, calendarTargetDate, previousTradingDate and
calendarEvidenceSha256; dashboard shows not_required, not trading-day pass.
Closed on a known Qlib trading date is core_calendar_schedule_conflict critical.

After cutover missing optional current AkShare stage is not a core fault.
Historical mandatory-stage evidence/anomalies stay labeled old full. A full
row at/after effective date is legacy_full_rows_after_cutover critical.
Missing core rows/runs remain explicit unresolved_core_market_gaps /
scheduled_core_market_missing, without masking any old 9+10 historical facts.

scheduled_core_market_missing and last_core_market_not_successful are explicit
date-keyed alert atoms (ordinary warnings are not globally promoted). Same
code/dueDate deduplicates; a new dueDate/worsening or real recovery notifies.
Blind/stale audits preserve faults, not fake recovery. A core failure does not
erase old full criticals.

qlib_source_absence threshold now pairs the full-market coverage row's
invalid and eligible. CSI quote/previous-close union is disclosed separately;
it is never divided by the full-market denominator.

## Deployment / rollback checklist (not executed)

1. Recheck real production HEAD/worktree and batch-1 natural evidence.
2. Coordinate dashboard and signal-bus active-contract adapters. review.py and
   screen.py remain legacy consumers; do not label old full data current or
   assume absent appendix fields zero.
3. Preinstall only with separate authorization and no active policy; verify the
   full-contract path and the common guards. Review the shared-calendar residual
   dependency, then separately choose and approve a future effective trading date
   and calendar root.
   Never retroactively choose a date to hide a historic gap.
4. Natural acceptance must prove actual target/hash/quality/skip evidence and
   old row hashes, plus unified alert decisions; offline tests are not this.
5. Before activation, restore prior files/template. After core writes exist,
   rollback requires a new explicit contract/schedule decision, retaining all
   core/appendix/full records and the effective-date audit trail. Do not erase
   policy and manufacture full gaps, or merge core rows into old full storage.

## Validation boundary

Fixed fixtures deny network/processes/production files and cover scope,
coverage, No-tables isolation, exact retries, both calendar adapters, stale and
pre-effective targets, closed spoofing, cutover stages, scoped counts, actual
CLI argparse help, alert blind periods and old row integrity. Separate
tools/verify_contract_wrapper_offline.py runs only a path-relocated wrapper and
fixed stubs. Historical test counts and older full direct-alert behavior are
superseded by the final combined validation described in LIGHTWEIGHT_RUNTIME.md.
No source recovery, natural holiday success, upstream uptime or historical
repair is claimed.
