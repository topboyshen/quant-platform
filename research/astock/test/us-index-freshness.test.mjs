import test from "node:test";
import assert from "node:assert/strict";
import { classifySourceFreshness } from "../src/source-freshness.mjs";
import { latestCompletedUsSession, usEquityCloseTime } from "../src/us-equity-session.mjs";
import { buildVerifiedBaselineReport } from "../src/baseline-report.mjs";
import { assessPublicationGate, normalizeReportOutput } from "../src/report.mjs";
import { source, contextFixture, buildOptions, normalizationOptions, TARGET_DATE, CUTOFF } from "./helpers/multi-candidate-context.mjs";

function usSource(day, instant) {
  return source("overnight-us", "纳斯达克综合指数日线", instant, {
    marketCalendar: "us_equity_regular_v1", marketSymbol: ".IXIC", sessionDate: day
  });
}
function classify(evidence, cutoff = "2026-10-09T08:00:00+08:00") {
  return classifySourceFreshness(evidence, { cutoffInstant: cutoff, marketDataMaxAgeHours: 18, newsMaxAgeHours: 72 });
}

test("旧一天纳指即使仍在72小时内，也按交易日标记陈旧", () => {
  const result = classify(usSource("2026-10-07", "2026-10-08T04:00:00+08:00"));
  assert.equal(result.stale, true);
  assert.equal(result.expectedSessionDate, "2026-10-08");
  assert.equal(result.ageHours, 28);
  assert.equal(classify(usSource("2026-10-08", "2026-10-09T04:00:00+08:00")).stale, false);
});

test("周末及美股休市不把最新有效周五收盘误标陈旧", () => {
  const evidence = usSource("2026-09-04", "2026-09-05T04:00:00+08:00");
  for (const cutoff of ["2026-09-07T08:00:00+08:00", "2026-09-08T08:00:00+08:00"]) {
    assert.equal(classify(evidence, cutoff).stale, false);
    assert.equal(latestCompletedUsSession(cutoff), "2026-09-04");
  }
});

test("DST前后、早收市及精确收盘边界使用纽约时区", () => {
  assert.equal(latestCompletedUsSession("2026-03-09T19:59:59Z"), "2026-03-06");
  assert.equal(latestCompletedUsSession("2026-03-09T20:00:00Z"), "2026-03-09");
  assert.equal(latestCompletedUsSession("2026-11-02T20:59:59Z"), "2026-10-30");
  assert.equal(latestCompletedUsSession("2026-11-02T21:00:00Z"), "2026-11-02");
  assert.equal(latestCompletedUsSession("2026-11-27T17:59:59Z"), "2026-11-25");
  assert.equal(latestCompletedUsSession("2026-11-27T18:00:00Z"), "2026-11-27");
  assert.equal(classify(usSource("2026-11-27", "2026-11-27T18:00:00Z"), "2026-11-28T00:00:00Z").stale, false);
});

test("Good Friday和Juneteenth休市、闰日与2028元旦不泛化联邦银行规则", () => {
  assert.equal(latestCompletedUsSession("2026-04-04T00:00:00Z"), "2026-04-02");
  assert.equal(latestCompletedUsSession("2026-06-20T00:00:00Z"), "2026-06-18");
  assert.equal(usEquityCloseTime("2028-02-29"), "16:00");
  assert.equal(usEquityCloseTime("2027-12-31"), "16:00");
});

test("未知年份、伪造交易日/时间以及未来证据不放行", () => {
  assert.throws(() => latestCompletedUsSession("2029-01-03T08:00:00+08:00"), /unsupported/);
  assert.throws(() => classify(usSource("2026-10-08", "2026-10-09T05:00:00+08:00")), /timestamp invalid/);
  assert.throws(() => classify(usSource("2026-10-08", "2026-10-09T04:00:00.001+08:00")), /timestamp invalid/);
  assert.throws(() => classify(usSource("2026-10-04", "2026-10-05T04:00:00+08:00")), /timestamp invalid/);
  assert.throws(() => classify(usSource("2026-10-09", "2026-10-10T04:00:00+08:00")), /晚于固定截止时间/);
});

test("元数据旧绿灯不覆盖截止时重算结果，缺失元数据保留原小时校验", () => {
  assert.equal(classify({ ...usSource("2026-10-07", "2026-10-08T04:00:00+08:00"),
    freshnessStatus: "current", expectedSessionDate: "2026-10-07" }).stale, true);
  assert.equal(classify(source("legacy", "旧来源", "2026-10-08T04:00:00+08:00")).stale, true);
});

for (const [day, instant, stale] of [
  ["2026-08-07", "2026-08-08T04:00:00+08:00", false],
  ["2026-08-06", "2026-08-07T04:00:00+08:00", true]
]) {
  test(`正文与来源清单对 ${day} 的陈旧结论一致，规范化不改写正文`, () => {
    const context = contextFixture();
    context.data.overnightGlobal = [{ symbol: ".IXIC", name: "纳斯达克综合指数",
      sessionDate: day, close: 10100, change: 100, changePercent: 1,
      units: { close: "index_point", change: "index_point", changePercent: "percentage_point" },
      dataAsOf: instant, sourceIds: ["overnight-us"] }];
    context.sources.push(usSource(day, instant));
    const audit = assessPublicationGate({ context, cutoffInstant: CUTOFF, targetDate: TARGET_DATE });
    const report = buildVerifiedBaselineReport(buildOptions(context, audit));
    const body = report.split("\n").find((line) => line.includes(`纳斯达克综合指数（${day}）`));
    const evidence = report.split("\n").find((line) => /^\d+\. /.test(line) && line.includes("纳斯达克综合指数日线"));
    assert.equal(body.includes("[陈旧]"), stale);
    assert.equal(evidence.includes("[陈旧]"), stale);
    assert.equal(normalizeReportOutput(report, normalizationOptions(context, audit)), report);
  });
}
