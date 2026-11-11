// Additive contract for new inputs; legacy frozen inputs are never relabelled.
import { disclosureQueryState } from "./disclosure-evidence.mjs";
import { classifyUsIndexSession, latestCompletedUsSession } from "./us-equity-session.mjs";
import { instantMicros, instantMs } from "./snapshot-integrity.mjs";

export function validateResearchDataQuality(context, expected = {}) {
  const q = context.meta?.dataQuality;
  if (q === undefined) return;
  const fail = (field) => { throw new Error(`meta.dataQuality invalid: ${field}`); };
  const object = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  if (!object(q) || q.schemaVersion !== "research-data-quality-v1" ||
      q.scope !== "declared_morning_inputs_not_full_research" ||
      q.scoreMeaning !== "validated_content_and_source_state_not_confidence") fail("version/scope");
  const cutoff = instantMicros(q.requestedCutoffAt);
  if (cutoff === null || (expected.requestedAt && instantMicros(expected.requestedAt) !== cutoff)) fail("requestedCutoffAt");
  if (!object(q.sourceAvailability) || !Object.keys(q.sourceAvailability).length ||
      Object.values(q.sourceAvailability).some(v => !["live", "cache", "stale", "unavailable"].includes(v))) fail("sourceAvailability");
  if (!object(q.requiredFields) || !Array.isArray(q.requiredFields.missing) ||
      q.requiredFields.missing.some(v => typeof v !== "string") ||
      [q.requiredFields.marketScore, q.requiredFields.basicFinancialScore].some(v =>
        typeof v !== "number" || !Number.isFinite(v) || v < 0 || v > 100)) fail("requiredFields");
  if (!object(q.freshness) || !["current", "unverified_or_stale"].includes(q.freshness.status) ||
      !Array.isArray(q.freshness.items) || q.freshness.items.some(v => !object(v) ||
        !["current", "stale", "unverified"].includes(v.status))) fail("freshness");
  if (!Array.isArray(q.officialWindowCoverage) || q.officialWindowCoverage.some(v => !object(v) ||
      !["complete_as_observed", "incomplete", "unavailable"].includes(v.status) ||
      typeof v.completeAsObserved !== "boolean" || typeof v.finalCutoffCovered !== "boolean" ||
      v.completeAsObserved !== (v.status === "complete_as_observed"))) fail("officialWindowCoverage");
  if (!Array.isArray(q.policyUnavailable) || q.policyUnavailable.some(v => !object(v) ||
      v.status !== "not_disclosed" || v.required !== false || typeof v.reason !== "string")) fail("policyUnavailable");
  if (!object(q.sourceCredibility) || q.sourceCredibility.mediaNews?.kind !== "media_not_official" ||
      q.sourceCredibility.mediaNews?.affectsCoverageScore !== false ||
      q.sourceCredibility.financial?.kind !== "aggregated_basic_fields_not_full_filing_verification" ||
      q.sourceCredibility.financial?.affectsCoverageScore !== false) fail("sourceCredibility");
  if (!Array.isArray(q.limitations) || !q.limitations.length ||
      q.limitations.some(v => typeof v !== "string" || !v.trim()) ||
      typeof q.declaredInputsComplete !== "boolean") fail("limitations/completeness");
  if (q.declaredInputsComplete && (q.requiredFields.missing.length ||
      q.requiredFields.marketScore !== 100 || q.requiredFields.basicFinancialScore !== 100 ||
      q.freshness.status !== "current" || !q.freshness.items.length ||
      q.freshness.items.some(v => v.status !== "current") ||
      !q.officialWindowCoverage.length || q.officialWindowCoverage.some(v => !v.completeAsObserved) ||
      Object.values(q.sourceAvailability).some(v => v !== "live") ||
      Object.values(context.data?.sectionCompleteness ?? {}).some(v => v !== 100))) fail("false complete claim");
  if (context.meta.status === "live" && !q.declaredInputsComplete) fail("live requires declared complete inputs");
  // Do not let a self-reported green summary override its underlying evidence.
  const stocks = context.data?.watchlist ?? [];
  const byCode = new Map(q.officialWindowCoverage.map(v => [v.code, v]));
  if (byCode.size !== q.officialWindowCoverage.length || byCode.size !== stocks.length) fail("official code binding");
  for (const stock of stocks) {
    const recorded = byCode.get(stock.code);
    const actual = disclosureQueryState(stock, context.sources ?? [], q.requestedCutoffAt,
      {previousDate: context.data?.tradingCalendar?.previousTradingDate});
    const complete = actual.state === "available" && actual.coverage === "declared_complete";
    if (!recorded || recorded.completeAsObserved !== complete ||
        instantMicros(recorded.requestedCutoffAt) !== cutoff ||
        recorded.finalCutoffCovered !== (complete && instantMicros(actual.observedThroughAt) === cutoff)) fail("official evidence binding");
    if (complete && (instantMicros(recorded.queriedAt) !== instantMicros(actual.queriedAt) ||
        instantMicros(recorded.observedThroughAt) !== instantMicros(actual.observedThroughAt))) fail("official observation binding");
  }
  const expectedSymbols = [".DJI", ".INX", ".IXIC"];
  const freshness = new Map(q.freshness.items.map(v => [v.symbol, v]));
  if (freshness.size !== 3 || q.freshness.items.length !== 3 || expectedSymbols.some(s => !freshness.has(s))) fail("US symbol coverage");
  for (const symbol of expectedSymbols) {
    const records = (context.data?.overnightGlobal ?? []).filter(v => v.symbol === symbol);
    if (records.length > 1) fail("duplicate US identity");
    const row = records[0];
    const item = freshness.get(symbol);
    let status = "unverified";
    if (row && row.freshnessStatus !== "unverified") {
      try {
        const numeric = k => typeof row[k] === "number" && Number.isFinite(row[k]);
        if (row.marketSymbol !== symbol || !["open", "high", "low", "close", "previousClose"].every(k => numeric(k) && row[k] > 0) ||
            !["change", "changePercent"].every(numeric) || row.high < Math.max(row.open, row.close) ||
            row.low > Math.min(row.open, row.close) ||
            row.previousCloseDate !== latestCompletedUsSession(new Date(instantMs(row.dataAsOf) - 1).toISOString()) ||
            Math.abs(row.change - (row.close - row.previousClose)) > 1e-6 ||
            Math.abs(row.changePercent - (row.close - row.previousClose) / row.previousClose * 100) > 1e-6) throw new Error("US return unverified");
        status = classifyUsIndexSession(row, q.requestedCutoffAt).stale ? "stale" : "current";
      }
      catch { /* A malformed row can remain visible only as unverified. */ }
    }
    if (item.status !== status || item.sessionDate !== (row?.sessionDate ?? null)) fail("US evidence binding");
  }
  if (q.freshness.status !== (q.freshness.items.every(v => v.status === "current") ? "current" : "unverified_or_stale")) fail("US aggregate binding");
  if (q.declaredInputsComplete) {
    const positive = v => typeof v === "number" && Number.isFinite(v) && v > 0;
    const marketValid = (v, closeKey) => v && ["open", "high", "low", closeKey, "previousClose", "volume", "turnover"].every(k => positive(v[k]));
    const indices = context.data?.previousAShareSession?.indices ?? [];
    if (["sh000001", "sz399001", "sz399006"].some(code => {
      const rows = indices.filter(v => v.code === code);
      return rows.length !== 1 || !marketValid(rows[0], "close");
    })) fail("actual index field gap");
    const groups = [["reportDate"], ["noticeAt", "updatedAt"], ["revenue"], ["revenueYoY"],
      ["parentNetProfit"], ["parentNetProfitYoY"], ["deductedNetProfit"], ["deductedNetProfitYoY"],
      ["grossMargin"], ["netMargin"], ["roeWeighted", "roeDiluted"], ["eps"]];
    if (!stocks.length || stocks.some(v => !marketValid(v.marketData, "last") ||
      groups.some(group => !group.some(k => v.financials?.[k] !== undefined && v.financials[k] !== null)))) fail("actual watchlist field gap");
  }
}
