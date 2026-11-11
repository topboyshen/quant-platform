import test from "node:test";
import assert from "node:assert/strict";
import { classifySourceFreshness } from "../src/source-freshness.mjs";

const CUTOFF = "2026-08-11T00:00:00Z";
const options = {
  cutoffInstant: CUTOFF,
  marketDataMaxAgeHours: 18,
  newsMaxAgeHours: 72
};

function source(sourceType, time, published = false) {
  return {
    id: `${sourceType}:${time}`,
    sourceType,
    ...(published ? { publishedAt: time } : { dataAsOf: time })
  };
}

test("来源级行情与新闻时效在阈值上不陈旧，超过一毫秒即陈旧", () => {
  assert.equal(
    classifySourceFreshness(source("market_data", "2026-08-10T06:00:00Z"), options).stale,
    false
  );
  assert.equal(
    classifySourceFreshness(source("market_data", "2026-08-10T05:59:59.999Z"), options).stale,
    true
  );
  assert.equal(
    classifySourceFreshness(source("media_aggregation", "2026-08-08T00:00:00Z", true), options).stale,
    false
  );
  assert.equal(
    classifySourceFreshness(source("media_aggregation", "2026-08-07T23:59:59.999Z", true), options).stale,
    true
  );
});

test("官方公告不作新闻陈旧，仍要求有效且不晚于截止", () => {
  const oldFiling = classifySourceFreshness(
    source("official_announcement", "2026-06-30T00:00:00Z"),
    options
  );
  assert.deepEqual(oldFiling, {
    tracked: false,
    kind: null,
    stale: false,
    ageHours: null
  });
});

test("财务来源不套用行情新闻阈值但仍要求有效且不晚于截止", () => {
  const oldFinancial = classifySourceFreshness(
    source("financial_data_aggregation", "2025-12-31T00:00:00Z"),
    options
  );
  assert.deepEqual(oldFinancial, {
    tracked: false,
    kind: null,
    stale: false,
    ageHours: null
  });
  assert.throws(
    () => classifySourceFreshness(source("financial_data_aggregation", "not-a-time"), options),
    /缺少有效时效时间/
  );
  assert.throws(
    () => classifySourceFreshness(
      source("financial_data_aggregation", "2026-08-11T00:01:00.001Z"),
      options
    ),
    /晚于固定截止时间/
  );
});

test("新增或未知来源类型必须先声明时效策略", () => {
  assert.throws(
    () => classifySourceFreshness(source("future_source_type", "2026-08-10T00:00:00Z"), options),
    /来源类型无法确定时效规则/
  );
});
