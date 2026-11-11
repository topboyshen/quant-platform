import test from "node:test";
import assert from "node:assert/strict";
import { buildVerifiedBaselineReport } from "../src/baseline-report.mjs";
import { assessPublicationGate, normalizeReportOutput } from "../src/report.mjs";
import { buildStructuredAnalysisCandidates, parseStructuredAnalysisOutput } from "../src/structured-analysis.mjs";
import {
  source, contextFixture, semanticCandidateContext, buildOptions, normalizationOptions,
  TARGET_DATE, CUTOFF
} from "./helpers/multi-candidate-context.mjs";

function enrichedContext(context = contextFixture()) {
  for (const index of context.data.previousAShareSession.indices) {
    const turnoverId = `amount:${index.code}`;
    const priceIds = [...index.sourceIds];
    index.turnover = 123_000_000;
    index.units.turnover = "CNY";
    index.priceSourceIds = priceIds;
    index.turnoverSourceIds = [turnoverId];
    index.fieldSourceIds = {
      close: priceIds, previousClose: priceIds, change: priceIds,
      changePercent: priceIds, turnover: [turnoverId]
    };
    index.sourceIds = [...priceIds, turnoverId];
    context.sources.push(source(turnoverId, `${index.name}独立成交额来源`, index.dataAsOf, {
      fields: ["turnover"], units: { turnover: "CNY" }
    }));
  }
  return context;
}

function legacyPriceContext(context) {
  const legacy = structuredClone(context);
  for (const index of legacy.data.previousAShareSession.indices) {
    index.sourceIds = [...index.priceSourceIds];
    delete index.priceSourceIds;
    delete index.turnoverSourceIds;
    delete index.fieldSourceIds;
  }
  return legacy;
}

function audit(context) {
  return assessPublicationGate({ context, cutoffInstant: CUTOFF, targetDate: TARGET_DATE });
}

test("独立指数补额不污染核心价格来源，且不替代沪深成交额", () => {
  const context = enrichedContext();
  const publicationAudit = audit(context);
  assert.equal(publicationAudit.passed, true);
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  const legacy = legacyPriceContext(context);
  const legacyReport = buildVerifiedBaselineReport(buildOptions(legacy, audit(legacy)));
  assert.equal(report, legacyReport, "价格正文与来源标注应和仅有价格来源的旧输入一致");
  assert.doesNotMatch(report, /独立成交额来源/);
  assert.match(report, /沪深两市成交额：3000\.1 亿元/);
  assert.equal(normalizeReportOutput(report, normalizationOptions(context, publicationAudit)), report);
});

test("未引用的成交额来源错时不阻断已独立验证的价格事实", () => {
  const context = enrichedContext();
  for (const item of context.sources.filter((item) => item.id.startsWith("amount:"))) {
    item.dataAsOf = "2026-08-08T15:00:00+08:00";
  }
  const publicationAudit = audit(context);
  assert.equal(publicationAudit.passed, true);
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  assert.doesNotMatch(report, /独立成交额来源/);
  assert.equal(normalizeReportOutput(report, normalizationOptions(context, publicationAudit)), report);
});

test("显式价格来源缺失或损坏时不能回退合并来源冒充证明", () => {
  for (const priceIds of [[], null, "market", ["unknown-price"], ["market", "market"]]) {
    const context = enrichedContext();
    context.data.previousAShareSession.indices[0].priceSourceIds = priceIds;
    const publicationAudit = audit(context);
    assert.equal(publicationAudit.passed, false, JSON.stringify(priceIds));
    assert.ok(publicationAudit.blockers.some((item) => item.includes("上证指数") && item.includes("来源")));
    const forgedAudit = { ...audit(legacyPriceContext(enrichedContext())), passed: true };
    assert.throws(() => buildVerifiedBaselineReport(buildOptions(context, forgedAudit)), /来源/);
  }
});

test("指数方向分歧候选只使用价格来源，不使用独立成交额来源", () => {
  const context = enrichedContext(semanticCandidateContext());
  const candidates = buildStructuredAnalysisCandidates(context);
  const legacyCandidates = buildStructuredAnalysisCandidates(legacyPriceContext(context));
  assert.deepEqual(candidates, legacyCandidates);
  const candidate = candidates.find((item) => item.proofKind === "same_session_opposite_ashare_index_returns");
  assert.ok(candidate);
  assert.deepEqual(candidate.evidenceIds, ["market-index-up", "market-index-up-2"]);
  assert.deepEqual(candidate.counterEvidenceIds, ["market-index-down"]);
  for (const id of [...candidate.evidenceIds, ...candidate.counterEvidenceIds]) {
    assert.ok(!id.startsWith("amount:"));
  }
  const overlay = parseStructuredAnalysisOutput(JSON.stringify({
    schemaVersion: "2.0", candidateIds: [candidate.id]
  }), { context });
  const publicationAudit = audit(context);
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit, { analysisOverlay: overlay }));
  assert.doesNotMatch(report, /独立成交额来源/);
  assert.equal(normalizeReportOutput(report, normalizationOptions(context, publicationAudit)), report);
});

test("显式空价格来源不再借成交额来源生成指数分歧候选", () => {
  const context = enrichedContext(semanticCandidateContext());
  for (const index of context.data.previousAShareSession.indices) index.priceSourceIds = [];
  assert.ok(!buildStructuredAnalysisCandidates(context).some((item) =>
    item.proofKind === "same_session_opposite_ashare_index_returns"));
});

test("没有字段来源的历史格式仍保持原价格来源逻辑", () => {
  const context = contextFixture();
  const publicationAudit = audit(context);
  assert.equal(publicationAudit.passed, true);
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  assert.match(report, /上证指数：收盘/);
  assert.equal(normalizeReportOutput(report, normalizationOptions(context, publicationAudit)), report);
});
