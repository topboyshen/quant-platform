// Version is part of each immutable notification event, not its daily identity.
// A policy change selects the view for new events only. Never re-render an old
// event using the active policy or silently fall back on a rendering error.
import { buildResultView, RESULT_VIEW_VERSION } from "./result-view.mjs";
import { buildV3Brief, V3_VERSION } from "./result-view-v3.mjs";

export function isResultPresentation(version) {
  return version === RESULT_VIEW_VERSION || version === V3_VERSION;
}

export function buildReportPresentation(source, { targetDate, presentation } = {}) {
  if (presentation === RESULT_VIEW_VERSION) return buildResultView(source, { targetDate });
  if (presentation === V3_VERSION) return buildV3Brief(source, { targetDate });
  throw new Error("unsupported_report_presentation");
}
