import test from "node:test";
import assert from "node:assert/strict";
import { disclosureQueryState } from "../src/disclosure-evidence.mjs";
import { buildVerifiedBaselineReport } from "../src/baseline-report.mjs";
import { assessPublicationGate } from "../src/report.mjs";
import {
  contextFixture, buildOptions, CUTOFF, TARGET_DATE
} from "./helpers/multi-candidate-context.mjs";

const CUTOFF_AT = "2026-10-09T08:00:00+08:00";
const PREVIOUS_DATE = "2026-10-08";
const QUERY_AT = "2026-10-09T07:55:00+08:00";
const OBSERVED_AT = "2026-10-09T07:50:00+08:00";

// These fixtures describe a paginated query observed before a frozen cutoff,
// not an atomic copy of the upstream announcement index at that cutoff.
function fixture({ query = {}, coverage = {}, source = {} } = {}) {
  const officialDisclosure = {
    status: "available",
    queriedAt: QUERY_AT,
    sourceIds: ["cninfo-window-query"],
    records: [],
    queryCoverage: {
      kind: "window",
      windowStartAt: `${PREVIOUS_DATE}T00:00:00+08:00`,
      cutoffAt: CUTOFF_AT,
      queriedAt: QUERY_AT,
      observedThroughAt: OBSERVED_AT,
      pageSize: 30,
      maxPages: 4,
      pagesFetched: 2,
      total: 40,
      returned: 40,
      complete: true,
      reason: "all_query_pages_collected",
      ...coverage
    },
    ...query
  };
  const sources = [{
    id: "cninfo-window-query",
    title: "巨潮当日窗口查询（合成测试）",
    url: "https://example.com/cninfo-query",
    publisher: "测试官方查询源",
    sourceType: "official_disclosure_query",
    dataAsOf: officialDisclosure.queriedAt,
    ...source
  }];
  return { item: { officialDisclosure }, sources };
}

function classify(value = fixture()) {
  return disclosureQueryState(value.item, value.sources, CUTOFF_AT, { previousDate: PREVIOUS_DATE });
}

function assertIncomplete(value, message) {
  const state = classify(value);
  assert.notEqual(state.coverage, "declared_complete", message);
  assert.equal(state.state, "partial", message);
  return state;
}

test("完整窗口只声明所观测分页完整，保留真实最早页面时间且不是原子快照", () => {
  const value = fixture();
  const before = structuredClone(value);
  const state = classify(value);
  assert.equal(state.state, "available");
  assert.equal(state.coverage, "declared_complete");
  assert.equal(state.queriedAt, QUERY_AT);
  assert.equal(state.observedThroughAt, OBSERVED_AT);
  assert.equal(state.atomicSnapshot, false);
  assert.ok(Date.parse(state.observedThroughAt) < Date.parse(state.queriedAt));
  assert.ok(Date.parse(state.queriedAt) < Date.parse(CUTOFF_AT));
  assert.deepEqual(value, before, "分类不得改写输入中的观测时间或覆盖证明");
});

test("空结果只有完整查询证明才能接受，失败空结果不能冒充没有新公告", () => {
  const empty = classify(fixture({ coverage: { pagesFetched: 1, total: 0, returned: 0 } }));
  assert.equal(empty.state, "available");
  assert.equal(empty.coverage, "declared_complete");
  assert.equal(empty.atomicSnapshot, false);
  assertIncomplete(fixture({ coverage: {
    pagesFetched: 0, total: null, returned: 0, complete: false, reason: "query_failed"
  } }), "未成功取得任何页面不构成完整空查询");
});

test("失败、分页截断及条目过滤都保留不完整状态", () => {
  const cases = [
    { complete: false, reason: "query_failed" },
    { pagesFetched: 4, total: 121, returned: 120, complete: false, reason: "page_limit_reached" },
    { total: 41, returned: 40, complete: false, reason: "record_validation_failed" },
    { complete: true, reason: "page_limit_reached" }
  ];
  for (const coverage of cases) assertIncomplete(fixture({ coverage }), coverage.reason);
});

test("查询窗口必须覆盖上一交易日开始，不能把较窄窗口宣称查全", () => {
  assertIncomplete(fixture({ coverage: { windowStartAt: `${PREVIOUS_DATE}T12:00:00+08:00` } }));
  assertIncomplete(fixture({ coverage: { cutoffAt: "2026-10-09T07:59:00+08:00" } }));
  const wider = classify(fixture({ coverage: { windowStartAt: "2026-10-07T00:00:00+08:00" } }));
  assert.equal(wider.coverage, "declared_complete", "更宽但真实受界的窗口可以覆盖要求");
});

test("最早页面观测必须在窗口内，且不能晚于实际查询完成时间", () => {
  for (const observedThroughAt of [
    null,
    "not-an-instant",
    "2026-10-09T07:56:00+08:00",
    "2026-10-07T23:59:59+08:00"
  ]) assertIncomplete(fixture({ coverage: { observedThroughAt } }), String(observedThroughAt));
  assert.equal(classify(fixture({ coverage: { observedThroughAt: QUERY_AT } })).coverage,
    "declared_complete", "单页查询的观测时间可以等于完成时间");
});

test("窗口内完成时间必须与查询及来源的真实时间绑定", () => {
  assertIncomplete(fixture({ coverage: { queriedAt: "2026-10-09T07:54:00+08:00" } }));
  assert.equal(classify(fixture({ source: { dataAsOf: "2026-10-09T07:54:00+08:00" } })).state,
    "unverified");
  assert.equal(classify(fixture({ query: { queriedAt: null }, source: { dataAsOf: null } })).state,
    "unverified");
});

test("截止后才获得的查询即便自称完整也不能证明截止前的状态", () => {
  const late = "2026-10-09T08:01:00+08:00";
  const state = classify(fixture({ query: { queriedAt: late },
    coverage: { queriedAt: late, observedThroughAt: late } }));
  assert.equal(state.state, "after_cutoff");
  assert.notEqual(state.coverage, "declared_complete");
});

test("完整声明要求整数、可核对且受限的分页计数", () => {
  const invalidCounts = [
    { pagesFetched: 0 }, { pagesFetched: 5, maxPages: 4 },
    { pagesFetched: 1, total: 40, returned: 40 },
    { total: 41 }, { returned: -1 }, { returned: "40" },
    { pagesFetched: 1.5 }, { maxPages: 0 }, { maxPages: 9 },
    { pageSize: 0 }, { pageSize: 101 }
  ];
  for (const coverage of invalidCounts) assertIncomplete(fixture({ coverage }), JSON.stringify(coverage));
});

test("查询来源角色与实际时间必须充分，公告正文不能代替查询证明", () => {
  const missing = fixture();
  missing.sources = [];
  assert.equal(classify(missing).state, "unverified");
  assert.equal(classify(fixture({ query: { sourceIds: [] } })).state, "unverified");
  assert.equal(classify(fixture({ source: { sourceType: "official_announcement" } })).state, "unverified");
  const mixed = fixture();
  mixed.item.officialDisclosure.sourceIds.push("bad-query");
  mixed.sources.push({ id: "bad-query", sourceType: "official_disclosure_query", dataAsOf: null });
  assert.equal(classify(mixed).state, "unverified", "多来源中任何一项时间不足都不得放行");
});

test("partial 与 unavailable 查询不能因附带完整字段升级成可用", () => {
  assert.equal(classify(fixture({ query: { status: "partial" } })).state, "partial");
  assert.equal(classify(fixture({ query: { status: "unavailable" } })).state, "unavailable");
});

test("旧首屏 bounded 查询仍是 limited，不借 complete 字段升级为窗口查全", () => {
  for (const complete of [false, true]) {
    const state = classify(fixture({ query: { queryCoverage: {
      kind: "first_page", limit: 30, returned: 30, complete
    } } }));
    assert.equal(state.state, "available");
    assert.equal(state.coverage, "limited", `旧首屏 complete=${complete} 不是窗口完整证明`);
  }
});

test("未知覆盖类型不能仅靠 complete=true 声称已查全", () => {
  const state = classify(fixture({ query: { queryCoverage: { kind: "unknown", complete: true } } }));
  assert.notEqual(state.coverage, "declared_complete");
});

function baselineWithQuery(complete) {
  const context = contextFixture();
  const value = fixture({ query: { queriedAt: "2026-08-10T07:55:00+08:00" }, coverage: {
    windowStartAt: "2026-08-07T00:00:00+08:00",
    cutoffAt: CUTOFF.toISOString(),
    queriedAt: "2026-08-10T07:55:00+08:00",
    observedThroughAt: "2026-08-10T07:50:00+08:00",
    pagesFetched: 1, total: 0, returned: 0, complete,
    reason: complete ? "all_query_pages_collected" : "query_failed"
  } });
  context.data.watchlist[0].officialDisclosure = value.item.officialDisclosure;
  context.sources.push(...value.sources);
  context.meta.dataQuality = { schemaVersion: "research-data-quality-v1" };
  const audit = assessPublicationGate({ context, cutoffInstant: CUTOFF, targetDate: TARGET_DATE });
  return buildVerifiedBaselineReport(buildOptions(context, audit));
}

test("正文保留非原子查询边界与状态覆盖限制，不把状态通过解释为完整研究", () => {
  const report = baselineWithQuery(true);
  assert.match(report, /窗口分页已核验；非原子快照，最早页面观测 2026-08-10 07:50/);
  assert.match(report, /查询后至数据截止 2026-08-10 08:00 的新增披露仍未知/);
  assert.match(report, /状态判定仅覆盖声明的晨报输入/);
  assert.match(report, /来源可信度与覆盖分数分开，不表示完整研究或语义已审/);
  assert.match(report, /公告分页不是原子快照/);
});

test("空但失败的窗口在正文仍为不完整，不输出窗口查全措辞", () => {
  const report = baselineWithQuery(false);
  assert.match(report, /官方查询限制：部分查询成功，覆盖不完整/);
  assert.match(report, /官方公告查询未完成或结果不适用于截止，不判断窗口内有无新增披露/);
  assert.doesNotMatch(report, /窗口分页已核验/);
});
