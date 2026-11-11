import test from "node:test";
import assert from "node:assert/strict";
import { disclosureEvidenceInstant, disclosureQueryState, filingIsEligible } from "../src/disclosure-evidence.mjs";
import { classifySourceFreshness } from "../src/source-freshness.mjs";

const cutoff = "2026-09-11T00:00:00Z";
const options = { cutoffInstant: cutoff, previousDate: "2026-09-10", code: "002617" };
function record(date, observedAt = "2026-09-11T17:08:00+08:00") {
  return { code: "002617", name: "露笑科技", title: "测试披露", sourceType: "official_announcement", disclosureDate: date,
    timePrecision: "date", publishedAt: null, observedAt, dataAsOf: observedAt, sourceIds: ["doc"] };
}
function source(item) { return { ...item, id: "doc" }; }

test("只有日期时使用保守可用时间边界，不伪造发布时间", () => {
  const item = record("2026-09-10");
  assert.equal(disclosureEvidenceInstant(item), "2026-09-10T16:00:00.000Z");
  assert.equal(filingIsEligible(item, [source(item)], options), true);
  assert.equal(item.publishedAt, null);
  assert.equal(filingIsEligible(record("2026-09-11"), [source(record("2026-09-11"))], options), false);
  const observedBeforeCutoff = record("2026-09-11", "2026-09-11T07:55:00+08:00");
  assert.equal(filingIsEligible(observedBeforeCutoff, [source(observedBeforeCutoff)], options), true);
});

test("近期背景不能冒充窗口内新增，未来日期不能进入任意分栏", () => {
  const background = record("2026-09-08");
  assert.equal(filingIsEligible(background, [source(background)], options), false);
  assert.equal(filingIsEligible(background, [source(background)], { ...options, background: true }), true);
  const future = record("2026-09-12", "2026-09-11T07:55:00+08:00");
  assert.equal(filingIsEligible(future, [source(future)], options), false);
  const contradictory = record("2026-09-11", "2026-09-10T15:00:00+08:00");
  assert.equal(disclosureEvidenceInstant(contradictory), null);
  const item = record("2026-09-10");
  assert.equal(filingIsEligible(item, [{ ...source(item), disclosureDate: "2026-09-09" }], options), false);
});

test("查询角色、实际时间和源必须完整且一致", () => {
  const item = { officialDisclosure: { status: "available", queriedAt: "2026-09-11T07:55:00+08:00", sourceIds: ["query"] } };
  const query = { id: "query", sourceType: "official_disclosure_query", dataAsOf: item.officialDisclosure.queriedAt };
  assert.equal(disclosureQueryState(item, [query], cutoff).state, "available");
  assert.equal(disclosureQueryState(item, [query], cutoff).coverage, "limited");
  assert.equal(disclosureQueryState({ officialDisclosure: { status: "partial" } }, [], cutoff).state, "partial");
  assert.equal(disclosureQueryState(item, [], cutoff).state, "unverified");
  assert.equal(disclosureQueryState(item, [{ ...query, sourceType: "official_announcement" }], cutoff).state, "unverified");
  const late = "2026-09-11T17:08:00+08:00";
  assert.equal(disclosureQueryState({ officialDisclosure: { ...item.officialDisclosure, queriedAt: late } }, [{ ...query, dataAsOf: late }], cutoff).state, "after_cutoff");
  assert.equal(disclosureQueryState({ officialDisclosure: { status: "unavailable" } }, [], cutoff).state, "unavailable");
});

test("旧披露不作新闻陈旧，仍用披露日期区分是否今日新增", () => {
  const state = classifySourceFreshness(source(record("2026-09-07")), { cutoffInstant: cutoff, marketDataMaxAgeHours: 18, newsMaxAgeHours: 72 });
  assert.equal(state.tracked, false);
  assert.equal(state.stale, false);
});
