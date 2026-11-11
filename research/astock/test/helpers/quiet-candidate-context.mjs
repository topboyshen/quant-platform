// Explicitly synthetic, offline comparison material. This helper does not
// classify quiet days and has no production config, history, I/O or send path.
import { buildVerifiedBaselineReport } from "../../src/baseline-report.mjs";
import { assessPublicationGate, normalizeReportOutput } from "../../src/report.mjs";
import {
  contextFixture, buildOptions, normalizationOptions, TARGET_DATE,
} from "./multi-candidate-context.mjs";

export function buildQuietCandidateReport({ breadth = {} } = {}) {
  const counts = { advancers: 2500, decliners: 2500, unchanged: 0, ...breadth };
  for (const key of ["advancers", "decliners", "unchanged"]) {
    if (!Number.isSafeInteger(counts[key]) || counts[key] < 0) {
      throw new TypeError(`invalid_synthetic_breadth:${key}`);
    }
  }
  const context = contextFixture();
  const session = context.data.previousAShareSession;
  for (const index of session.indices) {
    index.close = index.previousClose;
    index.change = 0;
    index.changePercent = 0;
  }
  for (const stock of context.data.watchlist) {
    stock.marketData.last = stock.marketData.previousClose;
    stock.marketData.change = 0;
    stock.marketData.changePercent = 0;
  }
  for (const sector of context.data.sectors) sector.changePercent = 0;
  // Preserve the compiler fixture's source identity and observation time.
  Object.assign(session.marketBreadth, {
    advancers: counts.advancers, decliners: counts.decliners, unchanged: counts.unchanged,
  });
  const audit = assessPublicationGate(buildOptions(context));
  const raw = buildVerifiedBaselineReport(buildOptions(context, audit));
  const report = normalizeReportOutput(raw, normalizationOptions(context, audit));
  return {
    report,
    targetDate: TARGET_DATE,
    synthetic: true,
    label: "构造静日候选，非真实报告；缺少跨日比较证据，未判定静日",
  };
}
