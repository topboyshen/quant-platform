import test from "node:test";
import assert from "node:assert/strict";
import { attachPreviousFinancialPeriods, collectCurrentFinancialSnapshots, previousFinancialPeriodsFromArchive } from "../src/financial-comparison.mjs";

function context() {
  return { meta: { targetDate: "2026-09-11" }, data: { watchlist: [{ code: "002617", financials: {
    reportDate: "2026-06-30", revenue: 100, parentNetProfit: 10,
    units: { revenue: "CNY", parentNetProfit: "CNY" },
    noticeAt: "2026-08-26T18:00:00+08:00", updatedAt: "2026-08-26T18:00:00+08:00",
    sourceIds: ["financial"]
  } }] } };
}
function baseline(value = context()) {
  return previousFinancialPeriodsFromArchive({ schemaVersion: "2.0", targetDate: "2026-09-10", reportSha256: "a".repeat(64), periods: collectCurrentFinancialSnapshots(value) }, "2026-09-11");
}
function status(current, previous = baseline()) {
  return attachPreviousFinancialPeriods(current, previous).data.watchlist[0].financialComparison.state;
}

test("财务字段顺序不改变规范化指纹", () => {
  const previous = baseline();
  const entry = previous["002617"];
  entry.fields.values = Object.fromEntries(Object.entries(entry.fields.values).reverse());
  entry.fields.units = Object.fromEntries(Object.entries(entry.fields.units).reverse());
  assert.equal(status(context(), previous), "same_period");
});

for (const [name, mutate, expected] of [
  ["单位变化", (f) => { f.units.revenue = "USD"; }, "units_changed"],
  ["营收口径增加", (f) => { f.revenueBasis = { provider: "eastmoney", amountField: "TOTALOPERATEREVE", yoyField: "TOTALOPERATEREVETZ", verification: "unverified_against_filing" }; }, "metric_changed"],
  ["来源版本变化", (f) => { f.updatedAt = "2026-09-10T17:00:00+08:00"; }, "version_changed"],
  ["同报告期字段变化", (f) => { f.revenue = 101; }, "values_changed"],
  ["新报告期", (f) => { f.reportDate = "2026-07-31"; }, "new_period"],
  ["报告期倒退", (f) => { f.reportDate = "2026-03-31"; }, "period_regressed"]
]) {
  test(`财务比较区分${name}`, () => {
    const current = context();
    mutate(current.data.watchlist[0].financials);
    assert.equal(status(current), expected);
  });
}

test("相同金额但营收同比字段或核验状态变化，不得当作同口径不变", () => {
  const prior = context();
  prior.data.watchlist[0].financials.revenueBasis = { provider: "eastmoney", amountField: "TOTALOPERATEREVE", yoyField: "TOTALOPERATEREVETZ", verification: "unverified_against_filing" };
  const previous = baseline(prior);
  assert.equal(status(prior, previous), "same_period");
  for (const key of ["provider", "amountField", "yoyField", "verification"]) {
    const current = structuredClone(prior);
    current.data.watchlist[0].financials.revenueBasis[key] = "different";
    assert.equal(status(current, previous), "metric_changed");
  }
  const unknown = structuredClone(prior);
  delete unknown.data.watchlist[0].financials.revenueBasis;
  assert.equal(status(unknown, previous), "metric_changed");
});

test("无留档、同日留档、非法日期或被改写的指纹不得压缩数字", () => {
  assert.equal(status(context(), {}), "none");
  assert.equal(status(context(), { "002617": "2026-06-30" }), "none");
  const archive = { schemaVersion: "2.0", targetDate: "2026-09-11", reportSha256: "a".repeat(64), periods: collectCurrentFinancialSnapshots(context()) };
  assert.deepEqual(previousFinancialPeriodsFromArchive(archive, "2026-09-11"), {});
  archive.targetDate = "2026-09-12";
  assert.deepEqual(previousFinancialPeriodsFromArchive(archive, "2026-09-11"), {});
  archive.targetDate = "2026-02-30";
  assert.deepEqual(previousFinancialPeriodsFromArchive(archive, "2026-09-11"), {});
  const future = context();
  future.data.watchlist[0].financials.reportDate = "2026-09-30";
  assert.equal(status(future), "none");
  assert.deepEqual(collectCurrentFinancialSnapshots(future), {});
  const previous = baseline();
  previous["002617"].fields.values.revenue = 999;
  assert.equal(status(context(), previous), "none");
});

test("重新准备上下文清理旧压缩标记且不修改原包", () => {
  const current = context();
  current.data.watchlist[0].financials.samePeriodAsPreviousSession = true;
  current.data.watchlist[0].financialComparisonBaseline = "same_period";
  const prepared = attachPreviousFinancialPeriods(current, {});
  assert.equal(prepared.data.watchlist[0].financialComparison.state, "none");
  assert.equal(prepared.data.watchlist[0].financials.samePeriodAsPreviousSession, undefined);
  assert.equal(current.data.watchlist[0].financials.samePeriodAsPreviousSession, true);
});
