import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { validateResearchDataQuality } from "../src/research-data-quality.mjs";
import { compilerIdentity } from "../src/run-archive.mjs";

function fixture() {
  const cutoff = "2026-10-09T08:00:00+08:00", observed = "2026-10-09T07:55:00+08:00";
  const symbols = [".DJI", ".INX", ".IXIC"];
  const row = {open: 100, high: 102, low: 99, close: 101, last: 101,
    previousClose: 100, volume: 10, turnover: 1000};
  const coverage = {kind: "window", complete: true, reason: "all_query_pages_collected",
    windowStartAt: "2026-10-08T00:00:00+08:00", cutoffAt: cutoff, queriedAt: observed,
    observedThroughAt: observed, pageSize: 30, maxPages: 4, pagesFetched: 1, total: 0, returned: 0};
  return {meta: {status: "live", dataQuality: {
    schemaVersion: "research-data-quality-v1", scope: "declared_morning_inputs_not_full_research",
    requestedCutoffAt: cutoff,
    scoreMeaning: "validated_content_and_source_state_not_confidence",
    sourceAvailability: {"us_index_.IXIC": "live", cninfo: "live"},
    requiredFields: {marketScore: 100, basicFinancialScore: 100, missing: []},
    freshness: {status: "current", items: symbols.map(symbol => ({symbol, sessionDate: "2026-10-08", status: "current"}))},
    officialWindowCoverage: [{code: "002617", requestedCutoffAt: cutoff, queriedAt: observed, observedThroughAt: observed,
      status: "complete_as_observed", completeAsObserved: true, finalCutoffCovered: false}],
    policyUnavailable: [{field: "northbound_realtime", status: "not_disclosed", required: false, reason: "未公开披露"}],
    sourceCredibility: {mediaNews: {kind: "media_not_official", affectsCoverageScore: false},
      financial: {kind: "aggregated_basic_fields_not_full_filing_verification", affectsCoverageScore: false}},
    limitations: ["不是完整研究，不是公告原子快照"], declaredInputsComplete: true
  }}, data: {sectionCompleteness: {market: 100}, tradingCalendar: {previousTradingDate: "2026-10-08"},
    previousAShareSession: {indices: ["sh000001", "sz399001", "sz399006"].map(code => ({code, ...row}))},
    overnightGlobal: symbols.map(symbol => ({...row, previousCloseDate: "2026-10-07", change: 1, changePercent: 1,
      symbol, marketSymbol: symbol, marketCalendar: "us_equity_regular_v1",
      sessionDate: "2026-10-08", freshnessStatus: "current", dataAsOf: "2026-10-09T04:00:00+08:00"})),
    watchlist: [{code: "002617", marketData: {...row}, financials: {reportDate: "2026-06-30", noticeAt: observed,
      revenue: 1, revenueYoY: 1, parentNetProfit: 1, parentNetProfitYoY: 1, deductedNetProfit: 1,
      deductedNetProfitYoY: 1, grossMargin: 1, netMargin: 1, roeWeighted: 1, eps: 1},
      officialDisclosure: {status: "available", queriedAt: observed, queryCoverage: coverage, sourceIds: ["query"]}}]},
    sources: [{id: "query", sourceType: "official_disclosure_query", dataAsOf: observed}]};
}

test("new scope distinguishes usable declared inputs from complete research, legacy is untouched", () => {
  assert.doesNotThrow(() => validateResearchDataQuality(fixture()));
  const legacy = {meta: {status: "partial"}, data: {}};
  const before = JSON.stringify(legacy);
  validateResearchDataQuality(legacy);
  assert.equal(JSON.stringify(legacy), before);
});

test("new complete claim rejects real gaps, stale sources, unverified windows or confidence conflation", () => {
  for (const mutate of [
    q => q.requiredFields.missing.push("indices.sh000001.turnover"),
    q => q.sourceAvailability["us_index_.IXIC"] = "stale",
    q => q.freshness.items[0].status = "unverified",
    q => { q.officialWindowCoverage[0].status = "incomplete"; q.officialWindowCoverage[0].completeAsObserved = false; },
    q => q.sourceCredibility.mediaNews.affectsCoverageScore = true,
    q => q.policyUnavailable[0].required = true,
    q => q.declaredInputsComplete = false,
  ]) {
    const c = fixture(); mutate(c.meta.dataQuality);
    assert.throws(() => validateResearchDataQuality(c), /dataQuality invalid/);
  }
});

test("honest partial context can retain all limitations without fabricating policy data", () => {
  const c = fixture(); c.meta.status = "partial";
  c.meta.dataQuality.declaredInputsComplete = false;
  c.meta.dataQuality.requiredFields.missing = ["indices.sh000001.turnover"];
  c.meta.dataQuality.requiredFields.marketScore = 90;
  assert.doesNotThrow(() => validateResearchDataQuality(c));
});

test("quality metadata cannot contradict actual failed queries, stale rows or missing fields", () => {
  for (const mutate of [
    c => c.data.watchlist[0].officialDisclosure.queryCoverage.complete = false,
    c => c.data.watchlist[0].officialDisclosure.queryCoverage.reason = "query_failed",
    c => c.data.overnightGlobal[2].sessionDate = "2026-10-07",
    c => c.data.overnightGlobal[2].previousCloseDate = "2026-10-06",
    c => c.data.overnightGlobal[2].changePercent = 999,
    c => c.data.previousAShareSession.indices[0].turnover = null,
    c => c.data.watchlist[0].financials.eps = null,
    c => c.meta.dataQuality.freshness.items.pop(),
    c => c.meta.dataQuality.officialWindowCoverage[0].code = "600875",
  ]) {
    const c = fixture(); mutate(c);
    assert.throws(() => validateResearchDataQuality(c), /dataQuality invalid/);
  }
  assert.throws(() => validateResearchDataQuality(fixture(), {requestedAt: "2026-10-09T08:01:00+08:00"}), /requestedCutoffAt/);
});

test("calendar and new provider validators are frozen into compiler identity", async t => {
  const root = path.resolve(import.meta.dirname, "..");
  const identity = await compilerIdentity(root);
  for (const name of ["config/us-equity-calendar.json", "mcp/us_equity_calendar.py",
    "mcp/research_data_quality.py", "mcp/cninfo_staging.py", "mcp/precollect_cninfo.py",
    "src/us-equity-session.mjs", "src/research-data-quality.mjs"]) {
    assert.match(identity.files[name], /^[a-f0-9]{64}$/);
  }
  const temp = await mkdtemp(path.join(os.tmpdir(), "quality-identity-"));
  t.after(() => rm(temp, {recursive: true, force: true}));
  await mkdir(path.join(temp, "src")); await mkdir(path.join(temp, "config"));
  await writeFile(path.join(temp, "src", "fixture.mjs"), "export const x=1;");
  const calendar = path.join(temp, "config", "us-equity-calendar.json");
  await writeFile(calendar, '{"reviewed":1}');
  const before = await compilerIdentity(temp);
  await writeFile(calendar, '{"reviewed":2}');
  const after = await compilerIdentity(temp);
  assert.notEqual(after.filesSha256, before.filesSha256);
});
