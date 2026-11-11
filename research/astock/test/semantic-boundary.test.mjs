import test from "node:test";
import assert from "node:assert/strict";
import { financialFieldComparison } from "../src/document-evidence.mjs";
import { buildStructuredAnalysisCandidates } from "../src/structured-analysis.mjs";
import { documentFixture } from "./helpers/document-fixture.mjs";

const SESSION_DATE = "2026-09-21";

function breadthContext() {
  return {
    data: {
      previousAShareSession: {
        sessionDate: SESSION_DATE,
        dataAsOf: `${SESSION_DATE}T15:00:00+08:00`,
        indices: [],
        marketBreadth: { advancers: 4216, decliners: 914, unchanged: 79, sourceIds: ["breadth"] },
        totalTurnover: 2033759165319.21,
        units: { totalTurnover: "CNY" },
        turnoverSourceIds: ["sse", "szse"]
      },
      watchlist: [{
        code: "002617",
        marketData: {
          changePercent: 2.58,
          units: { changePercent: "percentage_point" },
          dataAsOf: `${SESSION_DATE}T15:00:00+08:00`,
          sourceIds: ["quote"]
        },
        financials: {
          reportDate: "2026-06-30",
          parentNetProfitYoY: -23.76,
          units: { parentNetProfitYoY: "percentage_point" },
          sourceIds: ["filing"]
        }
      }],
      announcementsAndNews: []
    },
    sources: ["breadth", "sse", "szse", "quote"].map((id) => ({
      id, sourceType: "market_data", dataAsOf: `${SESSION_DATE}T15:00:00+08:00`
    })).concat([{ id: "filing", sourceType: "financial_data_aggregation", dataAsOf: "2026-08-26T23:59:59+08:00" }])
  };
}

test("成交额来源支持成交额，缺反证不否定已核事实", () => {
  const theme = buildStructuredAnalysisCandidates(breadthContext())
    .find((item) => item.proofKind === "previous_session_breadth_and_turnover");
  assert.equal(theme.evidenceRoles.breadth, "上涨占优");
  assert.equal(theme.evidenceRoles.sse, "成交额");
  assert.equal(theme.evidenceRoles.szse, "成交额");
  assert.deepEqual(theme.counterEvidenceIds, []);
  assert.doesNotMatch(theme.text, /不升级为事实结论|不否定/);
});

test("期间对照写出具体日期和报告期，不认定同期背离", () => {
  const stock = buildStructuredAnalysisCandidates(breadthContext())
    .find((item) => item.proofKind === "watchlist_profit_yoy_versus_price");
  assert.equal(stock.periodNote, "行情 2026-09-21；利润同比报告期 2026-06-30");
  assert.equal(stock.evidenceRoles.quote, "单日行情");
  assert.equal(stock.evidenceRoles.filing, "报告期利润同比");
  assert.match(stock.text, /期间不同，不足以认定同期背离/);
  assert.doesNotMatch(stock.text, /\d/);
});

test("未声明单位时不把原始数字判成金额不一致", () => {
  const { stock, doc } = documentFixture();
  delete stock.financials.units.revenue;
  const comparison = financialFieldComparison(stock, doc);
  assert.equal(comparison.label, "供应商单位待核，暂不直接比较");
  assert.equal(comparison.dimensions.unit.state, "unverified");
  assert.notEqual(comparison.label, "数值不一致，差异原因待核");
});
