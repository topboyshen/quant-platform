# Total amount source diagnosis — 2026-08-26

This note records the 2026-08-25 Swing EOD degradation and the isolated
transport-only candidate fix. It does not change a strategy threshold,
provider priority, amount/unit extraction, or production schedule. It does add
a completeness guard requiring canonical, matching dates for both components.

## Evidence

- The natural 15:30 EOD run evaluated all 10 targets and completed its service
  successfully, but its immutable run status was `partial` with
  `decision_outcome=data_unavailable` and `reason_codes=[macro_data_partial]`.
- Tencent's total-amount request returned HTTP 501. The AkShare/Eastmoney path
  then ended with a remote disconnect. `total_amount` was therefore unavailable
  while margin and market-breadth inputs remained valid.
- Tencent used a process-global request instead of the collector's shared HTTP
  session, so it bypassed `RADAR_ALL_PROXY` after the wrapper deliberately
  cleared ambient proxy variables. The candidate reuses that existing session;
  it does not add a source, retry, or persistence path.
- In this run, the stored audit retained the failure evidence, the run was
  classified `data_unavailable`, and zero strategy signals were emitted. This
  is an observation of the 2026-08-25 run, not a claim that one missing factor
  universally gates every strategy. No historical percentile threshold was
  weakened.

## Proposed follow-up

1. Reproduce each total-amount adapter with an exact-date, no-write source smoke
   around 15:20 and after 15:30. Record provider, URL family, HTTP status,
   observed trade date, units, and bounded latency without logging payloads.
2. Replace the unsupported Tencent URL only after an offline fixture proves the
   response contract. Add an independent exact-date fallback and reject mixed
   dates, partial components, non-finite values, and implausible units.
3. Add bounded post-close retries rather than changing signal thresholds or
   treating a missing factor as neutral. A repair must remain idempotent and
   preserve the original `partial` audit record instead of rewriting history.
4. Accept the change only when fixtures cover primary success, fallback success,
   both-source failure, stale dates, unit conversion, and fail-closed behavior.

Until that work is reviewed, `macro_data_partial` remains the honest production
result for a missing total-amount factor.
