import { disclosureEvidenceInstant } from "./disclosure-evidence.mjs";
import { classifyUsIndexSession } from "./us-equity-session.mjs";

const SOURCE_FRESHNESS_KIND = Object.freeze({
  trading_calendar_data: "market",
  market_data: "market",
  industry_market_data: "market",
  dragon_tiger_list_aggregation: "market",
  financial_data_aggregation: null,
  media_article: "news",
  media_aggregation: "news",
  official_announcement: null,
  official_disclosure_query: "news"
});

function positiveFinite(value, label) {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    throw new Error(`${label} 必须是正数`);
  }
  return value;
}

export function sourceEvidenceInstant(source) {
  return source?.sourceType === "official_announcement"
    ? disclosureEvidenceInstant(source)
    : source?.publishedAt ?? source?.dataAsOf ?? null;
}

export function classifySourceFreshness(source, {
  cutoffInstant,
  marketDataMaxAgeHours,
  newsMaxAgeHours
}) {
  const sourceType = source?.sourceType;
  if (!Object.hasOwn(SOURCE_FRESHNESS_KIND, sourceType)) {
    throw new Error(`来源类型无法确定时效规则: ${sourceType ?? "缺失"}`);
  }

  const kind = SOURCE_FRESHNESS_KIND[sourceType];
  const cutoff = new Date(cutoffInstant);
  const evidenceInstant = sourceEvidenceInstant(source);
  const evidenceTime = evidenceInstant == null ? null : new Date(evidenceInstant);
  if (Number.isNaN(cutoff.getTime()) || evidenceTime === null || Number.isNaN(evidenceTime.getTime())) {
    throw new Error(`来源 ${source?.id ?? source?.title ?? "未知"} 缺少有效时效时间`);
  }
  if (evidenceTime.getTime() > cutoff.getTime() + 60_000) {
    throw new Error(`来源 ${source?.id ?? source?.title ?? "未知"} 晚于固定截止时间`);
  }
  if (kind === null) {
    return Object.freeze({ tracked: false, kind: null, stale: false, ageHours: null });
  }
  if (source?.marketCalendar !== undefined) {
    if (sourceType !== "market_data") throw new Error("美股交易日规则仅适用于行情来源");
    return classifyUsIndexSession(source, cutoff);
  }
  const maxAgeHours = kind === "market"
    ? positiveFinite(marketDataMaxAgeHours, "行情时效阈值")
    : positiveFinite(newsMaxAgeHours, "新闻时效阈值");
  const ageHours = (cutoff.getTime() - evidenceTime.getTime()) / 3_600_000;
  return Object.freeze({
    tracked: true,
    kind,
    stale: ageHours > maxAgeHours,
    ageHours,
    maxAgeHours
  });
}
