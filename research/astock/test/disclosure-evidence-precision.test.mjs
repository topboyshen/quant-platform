import test from "node:test";
import assert from "node:assert/strict";
import { cutoffMicros, instantMicros, isoFromMicros, instantMs } from "../src/snapshot-integrity.mjs";
import { disclosureEvidenceInstant, disclosureQueryState, filingIsEligible } from "../src/disclosure-evidence.mjs";

const CUTOFF = "2026-10-12T08:00:00+08:00";

test("instantMicros keeps what Date.parse drops and agrees with it otherwise", () => {
  assert.equal(instantMs("2026-10-12T08:00:00.000001+08:00"), instantMs(CUTOFF)); // the ms blind spot
  assert.equal(instantMicros("2026-10-12T08:00:00.000001+08:00") - instantMicros(CUTOFF), 1n);
  assert.equal(instantMicros("2026-10-12T07:50:02.926549+08:00") % 1_000_000n, 926549n);
  assert.equal(instantMicros("2026-10-12T00:00:00Z"), instantMicros("2026-10-12T08:00:00+08:00"));
  assert.equal(instantMicros("2026-10-12T07:50:02.9+08:00") % 1_000_000n, 900000n);
  assert.equal(instantMicros("2026-10-12T07:50:02.1234569+08:00") % 1_000_000n, 123456n); // truncates like Python
  for (const bad of [null, undefined, "", "2026-10-12", "2026-13-12T00:00:00+08:00", "2026-10-12T08:00:00", 5]) {
    assert.equal(instantMicros(bad), null);
  }
});

test("cutoffMicros accepts ISO strings and Dates, rejects garbage", () => {
  assert.equal(cutoffMicros(CUTOFF), instantMicros(CUTOFF));
  assert.equal(cutoffMicros(new Date(CUTOFF)), instantMicros(CUTOFF));
  assert.equal(cutoffMicros("nonsense"), null);
});

test("isoFromMicros round-trips and only adds sub-millisecond digits when needed", () => {
  const whole = instantMicros("2026-10-12T08:00:00+08:00");
  assert.equal(isoFromMicros(whole), "2026-10-12T00:00:00.000Z");
  const fine = instantMicros("2026-10-12T08:00:00.000001+08:00");
  assert.equal(isoFromMicros(fine), "2026-10-12T00:00:00.000001Z");
  assert.equal(instantMicros(isoFromMicros(fine)), fine);
});

function item({ queried, through = queried, cutoff = CUTOFF, moduleQueried = queried, sourceAsOf = moduleQueried }) {
  return {
    stock: { code: "002617", officialDisclosure: { status: "available", queriedAt: moduleQueried, sourceIds: ["q"],
      queryCoverage: { kind: "window", windowStartAt: "2026-09-28T00:00:00+08:00", cutoffAt: cutoff,
        queriedAt: queried, observedThroughAt: through, pageSize: 30, maxPages: 4, pagesFetched: 1,
        total: 0, returned: 0, complete: true, reason: "all_query_pages_collected" } } },
    sources: [{ id: "q", sourceType: "official_disclosure_query", dataAsOf: sourceAsOf }],
  };
}
const verdict = (opts) => { const { stock, sources } = item(opts);
  const r = disclosureQueryState(stock, sources, CUTOFF, { previousDate: "2026-10-09" }); return r.coverage ?? r.state; };

test("1us after cutoff is not complete (was 'declared_complete' with millisecond parsing)", () => {
  assert.notEqual(verdict({ queried: "2026-10-12T08:00:00.000001+08:00" }), "declared_complete");
});
test("exactly the cutoff and 1us before stay complete", () => {
  assert.equal(verdict({ queried: CUTOFF }), "declared_complete");
  assert.equal(verdict({ queried: "2026-10-12T07:59:59.999999+08:00" }), "declared_complete");
});
test("observedThroughAt 1us after queriedAt is not complete; equal or earlier stays complete", () => {
  const q = "2026-10-12T07:50:02.926549+08:00";
  assert.notEqual(verdict({ queried: q, through: "2026-10-12T07:50:02.926550+08:00" }), "declared_complete");
  assert.equal(verdict({ queried: q, through: q }), "declared_complete");
  assert.equal(verdict({ queried: q, through: "2026-10-12T07:50:02.926548+08:00" }), "declared_complete");
});
test("module-level and coverage queriedAt differing by 1us are not treated as equal", () => {
  const q = "2026-10-12T07:50:02.926549+08:00";
  assert.notEqual(verdict({ queried: q, moduleQueried: "2026-10-12T07:50:02.926550+08:00" }), "declared_complete");
  assert.notEqual(verdict({ queried: q, moduleQueried: "2026-10-12T07:50:02.926548+08:00" }), "declared_complete");
});
test("a whole-second module time against a microsecond coverage time is not equal (the 10/12 defect)", () => {
  assert.notEqual(verdict({ queried: "2026-10-12T07:50:02.926549+08:00", moduleQueried: "2026-10-12T07:50:02+08:00" }), "declared_complete");
});

test("date-only evidence instant keeps microseconds and filingIsEligible respects the 1us boundary", () => {
  const record = (observedAt) => ({ id: "s", sourceType: "official_announcement", code: "002617", title: "t", disclosureDate: "2026-10-12",
    timePrecision: "date", observedAt, dataAsOf: observedAt, sourceIds: ["s"] });
  const at = "2026-10-12T07:59:59.999999+08:00", after = "2026-10-12T08:00:00.000001+08:00";
  assert.equal(disclosureEvidenceInstant(record(at)), "2026-10-11T23:59:59.999999Z");
  for (const [observed, eligible] of [[at, true], [CUTOFF, true], [after, false]]) {
    const row = record(observed);
    assert.equal(filingIsEligible(row, [row], { cutoffInstant: CUTOFF, previousDate: "2026-10-09" }), eligible, observed);
  }
});

// ---- invalid / empty cutoff must be rejected up front (never null === null) ----
const BAD_CUTOFFS = [["empty string", ""], ["whitespace", "  "], ["garbage string", "nonsense"], ["no offset", "2026-10-12T08:00:00"],
  ["null", null], ["undefined", undefined], ["number", 1_760_000_000_000], ["boolean", true], ["object", {}], ["array", []],
  ["invalid Date", new Date("nope")]];
const BAD_WINDOW_CUTOFFS = [["valid", CUTOFF], ["empty", ""], ["null", null], ["garbage", "nonsense"], ["missing", undefined]];

test("disclosureQueryState rejects every invalid cutoff, also when the window cutoff is invalid too", () => {
  for (const [label, bad] of BAD_CUTOFFS) {
    for (const [windowLabel, windowCutoff] of BAD_WINDOW_CUTOFFS) {
      const { stock, sources } = item({ queried: "2026-10-12T07:50:02.926549+08:00", cutoff: windowCutoff });
      const result = disclosureQueryState(stock, sources, bad, { previousDate: "2026-10-09" });
      assert.equal(result.state, "unverified", `cutoff=${label} windowCutoff=${windowLabel}`);
      assert.equal(result.coverage, undefined, `cutoff=${label} windowCutoff=${windowLabel}`);
      assert.equal(result.queriedAt, null);
    }
  }
});

test("a valid Date or ISO-string cutoff is still accepted", () => {
  const { stock, sources } = item({ queried: "2026-10-12T07:50:02.926549+08:00" });
  for (const cutoff of [CUTOFF, new Date(CUTOFF)]) {
    assert.equal(disclosureQueryState(stock, sources, cutoff, { previousDate: "2026-10-09" }).coverage, "declared_complete");
  }
});

test("filingIsEligible and cutoffMicros also reject invalid cutoffs", () => {
  const row = { id: "s", sourceType: "official_announcement", code: "002617", title: "t", disclosureDate: "2026-10-12",
    timePrecision: "date", observedAt: "2026-10-12T07:00:00+08:00", dataAsOf: "2026-10-12T07:00:00+08:00", sourceIds: ["s"] };
  for (const [label, bad] of BAD_CUTOFFS) {
    assert.equal(cutoffMicros(bad), null, label);
    assert.equal(filingIsEligible(row, [row], { cutoffInstant: bad, previousDate: "2026-10-09" }), false, label);
  }
});
