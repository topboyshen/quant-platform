import {
  buildStructuredAnalysisCandidates,
  canonicalStructuredAnalysisText,
  containsNumericSemantics,
} from "./structured-analysis.mjs";
import {
  classifySourceFreshness,
  sourceEvidenceInstant
} from "./source-freshness.mjs";
import { disclosureQueryState, filingIsEligible } from "./disclosure-evidence.mjs";
import { validDate } from "./snapshot-integrity.mjs";
import { verifiedFinancialDocument, documentComparisonLabel, aggregatedRevenueDisagrees,
  financialFieldComparison } from "./document-evidence.mjs";
export {
  financialComparableFields, previousFinancialPeriodsFromArchive,
  collectCurrentFinancialSnapshots, attachPreviousFinancialPeriods
} from "./financial-comparison.mjs";

const CORE_INDICES = Object.freeze([
  { code: "sh000001", name: "上证指数" },
  { code: "sz399001", name: "深证成指" },
  { code: "sz399006", name: "创业板指" }
]);

const OVERLAY_FORBIDDEN = /(?:https?:\/\/|\[陈旧\]|买入|卖出|加仓|减仓|仓位|目标价|止损|看多|看空|保证|必然|确定上涨|确定下跌|发布|披露|宣布|签署|获批|中标|回购|增持|减持)/i;
const MARKET_SOURCE_TYPES = new Set(["market_data"]);
const CALENDAR_SOURCE_TYPES = new Set(["trading_calendar_data"]);
const SECTOR_SOURCE_TYPES = new Set(["industry_market_data"]);
const FINANCIAL_SOURCE_TYPES = new Set(["financial_data_aggregation"]);
const DRAGON_SOURCE_TYPES = new Set(["dragon_tiger_list_aggregation"]);
const MEDIA_SOURCE_TYPES = new Set(["media_aggregation"]);
const OFFICIAL_SOURCE_TYPES = new Set(["official_announcement"]);
const QUERY_SOURCE_TYPES = new Set(["official_disclosure_query"]);
const OVERNIGHT_ORDER = Object.freeze([".DJI", ".INX", ".IXIC", ".N225", ".GDAXI", ".FTSE"]);
const MAX_MEDIA_LINES = 5;
const MAX_OFFICIAL_LINES = 8;
const MAX_DRAGON_LINES = 5;
const MAX_BACKGROUND_LINES = 8;
const DRAGON_EXTREME_CHANGE_PERCENT = 30;
const MEDIA_MAX_AGE_HOURS = 24;
const MEDIA_MARKET_WIDE_PATTERN = /(?:行业.{0,20}资金|资金流入榜|资金流出榜|百元股|板块日报|资金流向日报|主力资金今日|净流入资金居前|净流出资金超)/;

function isFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function isPositiveNumber(value) {
  return isFiniteNumber(value) && value > 0;
}

function notableChangePercent(market) {
  if (!market || !isFiniteNumber(market.changePercent)) return null;
  if (Math.abs(market.changePercent) < 5) return null;
  return market.changePercent;
}

function assertIsoDate(value, fieldName) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new Error(`${fieldName} 必须是 YYYY-MM-DD`);
  }
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) {
    throw new Error(`${fieldName} 不是有效日期`);
  }
  return value;
}

function safeSingleLine(value, fieldName) {
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`${fieldName} 必须是非空字符串`);
  }
  if (/[\r\n\u2028\u2029\p{Cc}\p{Cf}]/u.test(value)) {
    throw new Error(`${fieldName} 只能包含单行可打印文本`);
  }
  return value.trim().replace(/\s+/g, " ");
}

function safeInline(value, fieldName) {
  const text = safeSingleLine(value, fieldName);
  if (/(?:https?:\/\/|www\.)|[\[\]<>*`#{}|~\\]/i.test(text)) {
    throw new Error(`${fieldName} 不得包含 Markdown 控制符、链接、HTML 或自动链接片段`);
  }
  return text;
}

function safeUrl(value, fieldName) {
  const url = safeSingleLine(value, fieldName);
  if (/[\s`<>\[\]]/.test(url)) {
    throw new Error(`${fieldName} 包含不安全的 URL 字符`);
  }
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error(`${fieldName} 不是有效 URL`);
  }
  if (!new Set(["http:", "https:"]).has(parsed.protocol)) {
    throw new Error(`${fieldName} 只允许 http/https`);
  }
  if (parsed.username || parsed.password) {
    throw new Error(`${fieldName} 不得包含 URL 用户信息`);
  }
  return parsed.href;
}

function resolveCutoffInstant(cutoffInstant, cutoffLocal, timezone) {
  if (cutoffInstant !== undefined && cutoffInstant !== null) {
    const parsed = new Date(cutoffInstant);
    if (!Number.isNaN(parsed.getTime())) return parsed;
    throw new Error("基础版晨报缺少有效 cutoffInstant");
  }
  if (timezone !== "Asia/Shanghai") {
    throw new Error("非 Asia/Shanghai 时区必须显式传入 cutoffInstant");
  }
  const match = String(cutoffLocal).match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})$/);
  if (!match) throw new Error("cutoffLocal 必须是 YYYY-MM-DD HH:mm");
  const parsed = new Date(`${match[1]}T${match[2]}:${match[3]}:00+08:00`);
  if (Number.isNaN(parsed.getTime())) throw new Error("cutoffLocal 不是有效时间");
  return parsed;
}

function localDate(value, timezone) {
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return null;
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).format(instant);
}

function localDateTime(value, timezone) {
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) throw new Error("来源缺少有效时间");
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23"
    }).formatToParts(instant).map((part) => [part.type, part.value])
  );
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

function sourceInstant(source) {
  return sourceEvidenceInstant(source);
}

function recordInstant(record) {
  return record?.publishedAt ?? record?.dataAsOf ?? null;
}

function sourceIds(record) {
  const ids = Array.isArray(record?.sourceIds)
    ? record.sourceIds
    : record?.sourceId
      ? [record.sourceId]
      : [];
  return [...new Set(ids)];
}

function declaredSourceIds(value, fieldName) {
  if (value === undefined || value === null) return null;
  if (!Array.isArray(value)) throw new Error(`${fieldName} 必须是来源 ID 数组`);
  const ids = value.map((id) => {
    if (typeof id !== "string" || id.trim() === "") {
      throw new Error(`${fieldName} 只能包含非空来源 ID`);
    }
    return id;
  });
  if (new Set(ids).size !== ids.length) throw new Error(`${fieldName} 不得包含重复来源 ID`);
  return ids;
}

function sameSourceIds(left, right) {
  return left.length === right.length && left.every((id) => right.includes(id));
}

function explicitAggregateSourceIds(session, kind) {
  const declarations = kind === "turnover"
    ? [
        [session?.turnoverSourceIds, "previousAShareSession.turnoverSourceIds"],
        [session?.turnoverByExchange?.sourceIds, "previousAShareSession.turnoverByExchange.sourceIds"]
      ]
    : [
        [session?.marketBreadth?.sourceIds, "previousAShareSession.marketBreadth.sourceIds"],
        [session?.marketBreadthSourceIds, "previousAShareSession.marketBreadthSourceIds"]
      ];
  const populated = declarations
    .map(([value, label]) => ({ ids: declaredSourceIds(value, label), label }))
    .filter(({ ids }) => ids && ids.length > 0);
  if (populated.length > 1 && !sameSourceIds(populated[0].ids, populated[1].ids)) {
    throw new Error(`${kind === "turnover" ? "成交额" : "市场宽度"}精确来源字段不一致`);
  }
  return populated[0]?.ids ?? [];
}

function preciseAggregateSourceIds(session, kind) {
  const explicit = explicitAggregateSourceIds(session, kind);
  if (explicit.length > 0) return explicit;

  const aggregateIds = sourceIds(session);
  if (kind === "breadth") {
    return aggregateIds.filter((id) => /(?:^|:)legulegu:market-activity(?::|$)/i.test(id));
  }

  const sse = aggregateIds.filter((id) => /(?:^|:)sse:daily-market-summary(?::|$)/i.test(id));
  const szse = aggregateIds.filter((id) => /(?:^|:)szse:daily-market-summary(?::|$)/i.test(id));
  return sse.length > 0 && szse.length > 0 ? [...sse, ...szse] : [];
}

function formatNumber(value, maximumFractionDigits = 6) {
  if (!isFiniteNumber(value)) throw new Error("尝试渲染非有限数值");
  const normalized = Object.is(value, -0) ? 0 : value;
  return normalized.toLocaleString("en-US", {
    useGrouping: false,
    minimumFractionDigits: 0,
    maximumFractionDigits
  });
}

function signedNumber(value, maximumFractionDigits = 6) {
  const rendered = formatNumber(value, maximumFractionDigits);
  return value > 0 ? `+${rendered}` : rendered;
}

function trySafeInline(value, fieldName) {
  try {
    return safeInline(value, fieldName);
  } catch {
    return null;
  }
}

function formatCny(value) {
  const magnitude = Math.abs(value);
  if (magnitude >= 100_000_000) return `${formatNumber(value / 100_000_000, 2)} 亿元`;
  if (magnitude >= 10_000) return `${formatNumber(value / 10_000, 2)} 万元`;
  return `${formatNumber(value, 2)} 元`;
}

function overnightRecordIsUsable(record, targetDate, cutoff) {
  const close = record?.close;
  const change = record?.change;
  const percent = record?.changePercent;
  if (!isPositiveNumber(close) || !isFiniteNumber(change) || !isFiniteNumber(percent)) return false;
  const previous = isPositiveNumber(record?.previousClose) ? record.previousClose : close - change;
  if (!isPositiveNumber(previous)) return false;
  const expectedChange = close - previous;
  const expectedPercent = expectedChange / previous * 100;
  if (!valuesAgree(change, expectedChange) || !valuesAgree(percent, expectedPercent)) return false;
  const parsed = new Date(record?.dataAsOf);
  if (Number.isNaN(parsed.getTime()) || parsed.getTime() > cutoff.getTime() + 60_000) return false;
  if (record?.units?.close !== "index_point" ||
      record?.units?.change !== "index_point" ||
      record?.units?.changePercent !== "percentage_point") return false;
  const sessionDate = record?.sessionDate;
  if (typeof sessionDate !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(sessionDate)) return false;
  return sessionDate < targetDate;
}

function staleSuffix(stale) {
  return stale ? "[陈旧]" : "";
}

function ageHours(value, cutoff) {
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return null;
  return (cutoff.getTime() - instant.getTime()) / 3_600_000;
}

function isStale(value, cutoff, maxAgeHours) {
  const age = ageHours(value, cutoff);
  return age !== null && age > maxAgeHours;
}

function valuesAgree(actual, expected, absolute = 0.02) {
  return Math.abs(actual - expected) <= Math.max(absolute, Math.abs(expected) * 0.00001);
}

function validArithmetic(record, valueKey) {
  const current = record?.[valueKey];
  const previous = record?.previousClose;
  const change = record?.change;
  const percent = record?.changePercent;
  if (!isPositiveNumber(current) || !isPositiveNumber(previous) ||
      !isFiniteNumber(change) || !isFiniteNumber(percent)) return false;
  const expectedChange = current - previous;
  const expectedPercent = expectedChange / previous * 100;
  return valuesAgree(change, expectedChange) && valuesAgree(percent, expectedPercent);
}

class SourceRegistry {
  constructor(context, cutoff, timezone, { marketDataMaxAgeHours, newsMaxAgeHours }) {
    this.byId = new Map((context?.sources ?? []).map((source) => [source?.id, source]));
    this.cutoff = cutoff;
    this.timezone = timezone;
    this.marketDataMaxAgeHours = marketDataMaxAgeHours;
    this.newsMaxAgeHours = newsMaxAgeHours;
    this.orderedIds = [];
    this.numberById = new Map();
  }

  freshnessForSource(source) {
    return classifySourceFreshness(source, {
      cutoffInstant: this.cutoff,
      marketDataMaxAgeHours: this.marketDataMaxAgeHours,
      newsMaxAgeHours: this.newsMaxAgeHours
    });
  }

  usableIds(record, { alignTime = true, allowedSourceTypes = null } = {}) {
    const ids = sourceIds(record);
    const itemTime = recordInstant(record);
    const itemMs = itemTime ? new Date(itemTime).getTime() : null;
    const usable = [];
    for (const id of ids) {
      if (typeof id !== "string" || id.trim() === "") {
        throw new Error("事实记录包含无效来源 id");
      }
      const source = this.byId.get(id);
      if (!source) throw new Error(`事实记录引用未知来源 id: ${id}`);
      if (allowedSourceTypes && !allowedSourceTypes.has(source?.sourceType)) continue;
      const time = sourceInstant(source);
      const sourceMs = time ? new Date(time).getTime() : Number.NaN;
      if (Number.isNaN(sourceMs) || sourceMs > this.cutoff.getTime() + 60_000) continue;
      if (alignTime && itemMs !== null && !Number.isNaN(itemMs) &&
          Math.abs(sourceMs - itemMs) > 60_000) continue;
      usable.push(id);
    }
    return usable;
  }

  registerIds(ids) {
    const numbers = [];
    for (const id of ids) {
      if (!this.numberById.has(id)) {
        this.orderedIds.push(id);
        this.numberById.set(id, this.orderedIds.length);
      }
      numbers.push(this.numberById.get(id));
    }
    return numbers;
  }

  marker(record, options = {}) {
    const declared = sourceIds(record);
    const ids = this.usableIds(record, options);
    if (options.requireAll && ids.length !== declared.length) return null;
    if (ids.length === 0) return null;
    return `[来源 ${this.registerIds(ids).join("、")}]`;
  }

  markerForExactIds(ids, dataAsOf, { allowedSourceTypes = null } = {}) {
    if (!Array.isArray(ids) || ids.length === 0) return null;
    const usable = this.usableIds(
      { sourceIds: ids },
      { alignTime: false, allowedSourceTypes }
    );
    if (usable.length !== ids.length) return null;
    const expectedDate = localDate(dataAsOf, this.timezone);
    if (!expectedDate || usable.some((id) => (
      localDate(sourceInstant(this.byId.get(id)), this.timezone) !== expectedDate
    ))) return null;
    return `[来源 ${this.registerIds(usable).join("、")}]`;
  }

  markerForIds(ids) {
    const usable = [];
    for (const id of ids) {
      const source = this.byId.get(id);
      if (!source) throw new Error(`analysisOverlay 引用了未知来源 id: ${id}`);
      const time = sourceInstant(source);
      const sourceMs = time ? new Date(time).getTime() : Number.NaN;
      if (Number.isNaN(sourceMs) || sourceMs > this.cutoff.getTime() + 60_000) {
        throw new Error(`analysisOverlay 引用了截止后或时间无效的来源 id: ${id}`);
      }
      usable.push(id);
    }
    const numbers = this.registerIds(usable);
    return usable.map((id, index) => {
      const freshness = this.freshnessForSource(this.byId.get(id));
      return `[来源 ${numbers[index]}]${freshness.tracked && freshness.stale ? "[陈旧]" : ""}`;
    }).join("、");
  }

  hasStaleTrackedSource() {
    return this.orderedIds.some((id) => {
      const freshness = this.freshnessForSource(this.byId.get(id));
      return freshness.tracked && freshness.stale;
    });
  }

  lines() {
    return this.orderedIds.map((id, index) => {
      const source = this.byId.get(id);
      const title = safeInline(source?.title, `sources[${id}].title`);
      if (/https?:\/\//i.test(title)) {
        throw new Error(`sources[${id}].title 不得嵌入 URL`);
      }
      const url = safeUrl(source?.url, `sources[${id}].url`);
      const type = safeInline(source?.sourceType ?? "unknown_source_type", `sources[${id}].sourceType`);
      let identity = type;
      if (type === "media_aggregation") {
        const aggregator = safeInline(source?.aggregator ?? "东方财富", `sources[${id}].aggregator`);
        const provenance = source?.originalPublisher
          ? `转引${safeInline(source.originalPublisher, `sources[${id}].originalPublisher`)}`
          : "原始发布方未核验";
        identity += `（${aggregator}聚合，${provenance}）`;
      } else if (source?.publisher) {
        identity += `（${safeInline(source.publisher, `sources[${id}].publisher`)}）`;
      }
      const time = source.publishedAt ?? source.dataAsOf;
      const dateOnly = source.timePrecision === "date";
      const timeLabel = dateOnly ? "实际采集时间" : type === "official_disclosure_query" ? "实际查询完成时间" : source?.publishedAt ? "发布时间" : "数据时间";
      const dateLabel = dateOnly ? `披露日期：${safeInline(source.disclosureDate, "disclosureDate")}（仅日期，发布时间未核实）；` : "";
      const freshness = this.freshnessForSource(source);
      const staleLabel = freshness.tracked && freshness.stale ? "[陈旧] " : "";
      return `${index + 1}. ${staleLabel}${title} — ${identity}；${dateLabel}${timeLabel}：${localDateTime(time, this.timezone)}（${this.timezone}）；${url}`;
    });
  }
}

function marketRecordIsUsable(record, valueKey, expectedDate, cutoff, timezone, units) {
  if (!validArithmetic(record, valueKey)) return false;
  const time = record?.dataAsOf;
  const parsed = new Date(time);
  if (Number.isNaN(parsed.getTime()) || parsed.getTime() > cutoff.getTime() + 60_000) return false;
  if (expectedDate && localDate(time, timezone) !== expectedDate) return false;
  return Object.entries(units).every(([key, expected]) => record?.units?.[key] === expected);
}

function validateOverlayText(value, label, textRole = null) {
  const text = safeInline(value, label);
  if (containsNumericSemantics(text) || OVERLAY_FORBIDDEN.test(text) || /[*_`#<>\[\]]/.test(text)) {
    throw new Error(`${label} 含数字、Markdown、事实事件或交易指令`);
  }
  const canonicalText = canonicalStructuredAnalysisText(textRole, text);
  if (!canonicalText) {
    throw new Error(`${label} 必须逐字使用程序固定模板`);
  }
  return canonicalText;
}

function validateOverlayIds(value, label, { required = true, allowedIds = null } = {}) {
  if (!Array.isArray(value) || (required && value.length === 0)) {
    throw new Error(`${label} 必须是${required ? "非空" : ""}来源 ID 数组`);
  }
  if (new Set(value).size !== value.length) throw new Error(`${label} 不得包含重复来源 ID`);
  for (const id of value) {
    if (typeof id !== "string" || id.trim() === "") throw new Error(`${label} 包含无效来源 ID`);
    if (allowedIds && !allowedIds.has(id)) throw new Error(`${label} 引用了不属于该股票的来源 ID: ${id}`);
  }
  return [...value];
}

function stockEvidenceIds(context, code) {
  const ids = new Set();
  const add = (record) => sourceIds(record).forEach((id) => ids.add(id));
  const stock = (context?.data?.watchlist ?? []).find((item) => String(item?.code) === code);
  if (!stock) return null;
  add(stock);
  add(stock.marketData);
  add(stock.financials);
  for (const item of context?.data?.announcementsAndNews ?? []) {
    if (String(item?.code ?? "") === code) add(item);
  }
  return ids;
}

function flattenOverlay(analysisOverlay, context) {
  if (Array.isArray(analysisOverlay)) {
    return analysisOverlay.map((item, index) => ({
      item,
      label: null,
      allowedKeys: new Set(["text", "evidenceIds", "counterEvidenceIds"]),
      path: `analysisOverlay[${index}]`,
      allowedEvidenceIds: null,
      textRole: null
    }));
  }
  if (!analysisOverlay || typeof analysisOverlay !== "object") {
    throw new Error("analysisOverlay 必须是结构化对象、非空数组或 null");
  }
  const rootKeys = new Set(["schemaVersion", "summary", "themes", "stocks", "scenarios", "risks"]);
  const unknownRoot = Object.keys(analysisOverlay).filter((key) => !rootKeys.has(key));
  if (unknownRoot.length > 0) {
    throw new Error(`analysisOverlay 包含未允许字段: ${unknownRoot.join(", ")}`);
  }
  if (analysisOverlay.schemaVersion !== "1.0") {
    throw new Error("analysisOverlay.schemaVersion 必须是 1.0");
  }
  const flattened = [];
  if (analysisOverlay.summary != null) {
    flattened.push({
      item: analysisOverlay.summary,
      label: "摘要",
      allowedKeys: new Set(["text", "evidenceIds", "counterEvidenceIds"]),
      path: "analysisOverlay.summary",
      allowedEvidenceIds: null,
      textRole: "summary"
    });
  }
  for (const [key, label] of [["themes", "主题"], ["scenarios", "情景"], ["risks", "风险"]]) {
    const collection = analysisOverlay[key] ?? [];
    if (!Array.isArray(collection)) throw new Error(`analysisOverlay.${key} 必须是数组`);
    collection.forEach((item, index) => {
      const title = key === "themes"
        ? validateOverlayText(item?.title, `analysisOverlay.${key}[${index}].title`, "theme_title")
        : null;
      flattened.push({
        item,
        label: title ? `${label} ${title}` : label,
        allowedKeys: new Set([
          "text",
          "evidenceIds",
          "counterEvidenceIds",
          ...(key === "themes" ? ["title"] : [])
        ]),
        path: `analysisOverlay.${key}[${index}]`,
        allowedEvidenceIds: null,
        textRole: ({ themes: "theme", scenarios: "scenario", risks: "risk" })[key]
      });
    });
  }
  const stocks = analysisOverlay.stocks ?? [];
  if (!Array.isArray(stocks)) throw new Error("analysisOverlay.stocks 必须是数组");
  stocks.forEach((item, index) => {
    const code = String(item?.code ?? "");
    if (!/^\d{6}$/.test(code)) throw new Error(`analysisOverlay.stocks[${index}].code 无效`);
    const allowedEvidenceIds = stockEvidenceIds(context, code);
    if (!allowedEvidenceIds) throw new Error(`analysisOverlay.stocks[${index}].code 不在股票池`);
    flattened.push({
      item,
      label: `股票 ${code}`,
      allowedKeys: new Set(["code", "text", "evidenceIds", "counterEvidenceIds"]),
      path: `analysisOverlay.stocks[${index}]`,
      allowedEvidenceIds,
      textRole: "stock"
    });
  });
  return flattened;
}


function findRenderedCandidate(context, item, textRole) {
  const kind = { theme: "theme", stock: "stock", summary: "summary", scenario: "scenario", risk: "risk" }[textRole];
  if (!kind) return null;
  let candidates = [];
  try {
    candidates = buildStructuredAnalysisCandidates(context);
  } catch {
    return null;
  }
  const same = (left, right) => [...left].toSorted().join("\0") === [...right].toSorted().join("\0");
  return candidates.find((candidate) => candidate.kind === kind && candidate.text === item.text &&
    (!item.code || candidate.code === item.code) &&
    same(candidate.evidenceIds, item.evidenceIds ?? []) &&
    same(candidate.counterEvidenceIds, item.counterEvidenceIds ?? [])) ?? null;
}

function renderOverlay(analysisOverlay, registry, context) {
  if (analysisOverlay == null) {
    return {
      hasContent: false,
      lines: ["程序核验基础版不生成因果推断、价格目标、买卖或仓位建议。"],
      items: []
    };
  }
  const lines = [];
  const items = [];
  const flattened = flattenOverlay(analysisOverlay, context);
  if (flattened.length === 0) {
    return {
      hasContent: false,
      lines: ["结构化分析覆盖层为空；本次仅发布程序核验基础版。"],
      items: []
    };
  }
  for (const { item, label, allowedKeys, path, allowedEvidenceIds, textRole } of flattened) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new Error(`${path} 必须是对象`);
    }
    const unknown = Object.keys(item).filter((key) => !allowedKeys.has(key));
    if (unknown.length > 0) {
      throw new Error(`${path} 包含未允许字段: ${unknown.join(", ")}`);
    }
    const text = validateOverlayText(item.text, `${path}.text`, textRole);
    const evidenceIds = validateOverlayIds(item.evidenceIds, `${path}.evidenceIds`, {
      allowedIds: allowedEvidenceIds
    });
    const counterEvidenceIds = validateOverlayIds(
      item.counterEvidenceIds ?? [],
      `${path}.counterEvidenceIds`,
      { required: false, allowedIds: allowedEvidenceIds }
    );
    lines.push(`- [推断] ${label ? `${label}：` : ""}${text}`);
    const matchedCandidate = findRenderedCandidate(context, item, textRole);
    const roles = matchedCandidate?.evidenceRoles ?? null;
    const evidence = evidenceIds.map((id) => {
      const marker = registry.markerForIds([id]);
      // Identity assertion: the rendered number must be the number this registry
      // assigned to this exact source id, so the digest cannot carry a marker
      // that points at a different source than the id it is bound to.
      const assigned = registry.numberById.get(id);
      const rendered = Number(/^\[来源 (\d+)\]/.exec(marker)?.[1]);
      if (!Number.isInteger(assigned) || rendered !== assigned) {
        throw new Error(`证据编号与注册表不一致: ${id}`);
      }
      return { id, role: roles?.[id] ?? null, marker };
    });
    for (const { role, marker } of evidence) {
      lines.push(role
        ? `  - 支持证据（${role}）：${marker}`
        : `  - 支持证据：${marker}`);
    }
    if (matchedCandidate?.periodNote) {
      lines.push(`  - 期间：${matchedCandidate.periodNote}。`);
    }
    const counterEvidence = counterEvidenceIds.map((id) => {
      const marker = registry.markerForIds([id]);
      const assigned = registry.numberById.get(id);
      const rendered = Number(/^\[来源 (\d+)\]/.exec(marker)?.[1]);
      if (!Number.isInteger(assigned) || rendered !== assigned) {
        throw new Error(`反证编号与注册表不一致: ${id}`);
      }
      return { id, marker };
    });
    let limitation = null;
    if (counterEvidenceIds.length === 0) {
      limitation = roles
        ? "未提供结构化反证来源；此限制只约束本条推断，不否定已核验事实。"
        : "未提供结构化反证来源，不升级为事实结论。";
      lines.push(roles ? `  - 推断限制：${limitation}` : `  - 反证：${limitation}`);
    } else {
      for (const { marker } of counterEvidence) {
        lines.push(`  - 反证：${marker}`);
      }
    }
    items.push({
      kind: overlayItemKind(path),
      label: label ?? null,
      code: typeof item.code === "string" ? item.code : null,
      inference: text,
      evidence,
      counterEvidence,
      periodNote: matchedCandidate?.periodNote ?? null,
      limitation
    });
  }
  return { hasContent: true, lines, items };
}

function overlayItemKind(path) {
  if (path.includes(".themes[")) return "theme";
  if (path.includes(".stocks[")) return "stock";
  if (path.includes(".risks[")) return "risk";
  if (path.includes(".scenarios[")) return "scenario";
  if (path.endsWith(".summary")) return "summary";
  return "item";
}

function financialDocumentLines(stock, sources, cutoff, registry, previousDate) {
  const verified = verifiedFinancialDocument(stock, sources, cutoff);
  if (!verified) {
    const attempted = stock.officialDisclosure?.financialSummary;
    return { lines: attempted ? ["- 官方财报原文：未取得适用于截止的已核验摘录，不补造现金流。"] : [], hasCashFlow: false, wroteNumbers: false, used: false };
  }
  const { doc, source } = verified;
  // The document validator binds actual PDF observation separately from the
  // conservative date-only availability boundary used by the registry.
  const marker = registry.marker(doc, { allowedSourceTypes: OFFICIAL_SOURCE_TYPES, requireAll: true, alignTime: false });
  if (!marker) throw new Error("官方财报原文无法绑定来源");
  const prefix = "- [事实] ";
  const cash = doc.metrics.find((m) => m.field === "operatingCashFlow");
  const unchanged = stock.previousOfficialDocument?.pdfSha256 === doc.pdfSha256;
  const linkChanged = unchanged && stock.previousOfficialDocument.url !== doc.url;
  const lines = [`${prefix}官方现金流原文（PDF 第 ${cash.pageNumber} 页，合并排版空白）：“${cash.quote}”。${marker}`];
  if (unchanged) {
    lines.push(`- 与已封存证据（${stock.previousOfficialDocument.targetDate}）一致${linkChanged ? "；来源链接变化，需核验修订" : ""}。`);
  }
  const windowNote = doc.disclosureDate < previousDate ? "非窗口新增披露" : "披露日期位于本次窗口，首次取数不等于首次披露";
  lines.push(`- 对应列：本报告期 ${doc.reportDate} 累计 / 上年同期 ${doc.comparisonReportDate} 累计；同份半年报摘要，原文单位：元。披露 ${doc.disclosureDate}（仅日期），实际 PDF 采集时间见来源；${windowNote}。`);
  if (!unchanged) lines.push(`- 前档比较：${documentComparisonLabel(stock, doc)}。`);
  const revenueDiffers = aggregatedRevenueDisagrees(stock, doc);
  const revenue = doc.metrics.find((m) => m.field === "revenue");
  const comparison = revenue ? financialFieldComparison(stock, doc) : null;
  if (revenue && comparison) {
    // 公告原文优先：官方列示在前，供应商数值只作对照；逐维度标注核验状态。
    lines.push(`${prefix}官方营业收入原文（PDF 第 ${revenue.pageNumber} 页）：“${revenue.quote}”。${marker}`);
    const officialYoy = comparison.amounts.officialYoy;
    lines.push(`- 官方列示（同份摘要原列）：本报告期 ${revenue.currentToken} 元，上年同期 ${revenue.previousToken} 元`
      + `${officialYoy === null ? "" : `（同比 ${formatNumber(officialYoy, 2)}%）`}。`);
    const supplier = comparison.amounts.provider;
    if (comparison.dimensions.unit.state === "unverified") {
      lines.push(`- 对照（供应商口径）：单位未声明，暂不直接比较；逐字段核验：${comparison.label}（${comparison.dimensionSummary}）。`);
      if (supplier !== null) {
        lines.push(`- 证据详情：供应商原始数值 ${formatNumber(supplier, 2)}（单位未声明），不与公告原列直接比较。`);
      }
    } else {
      const supplierUnit = comparison.dimensions.unit.state === "matched" ? " 元" : "";
      lines.push(`- 对照（供应商口径）：${supplier === null ? "未提供营收金额" : `${formatNumber(supplier, 2)}${supplierUnit}`}`
        + `${comparison.amounts.providerYoy === null ? "" : `（同比 ${formatNumber(comparison.amounts.providerYoy, 2)}%）`}`
        + `；逐字段核验：${comparison.label}（${comparison.dimensionSummary}）。`);
    }
    for (const note of comparison.notes) lines.push(`- 核验说明：${note}。`);
    if (comparison.unverifiedDimensions.length > 0) {
      lines.push(`- 未核验维度：${comparison.unverifiedDimensions.join("；")}。`);
    }
    if (comparison.amounts.delta !== null && !comparison.amounts.amountsEqual) {
      lines.push(`- 差异：供应商较公告原列 ${formatNumber(comparison.amounts.delta, 2)} 元；不自动归并、不把聚合同比当作公告口径。`);
    }
  } else if (revenueDiffers && revenue) {
    lines.push(`${prefix}官方营业收入原文（PDF 第 ${revenue.pageNumber} 页）：“${revenue.quote}”。${marker}`);
    lines.push("- 口径待核：现有聚合营收金额或供应商同比与官方摘要原列不同，不自动归并、不把聚合同比当作公告口径。");
  }
  return { lines, hasCashFlow: doc.reportDate === stock.financials?.reportDate, wroteNumbers: !unchanged || revenueDiffers, used: true };
}

function watchlistMonitorStatuses({
  labels,
  code,
  marketUsable,
  marketMarker,
  hasProfit,
  hasMargin,
  hasCashFlow,
  officialQueried,
  hasWindowFilings
}) {
  const seen = new Set();
  const lines = [];
  const add = (line) => {
    if (seen.has(line)) return;
    seen.add(line);
    lines.push(line);
  };
  for (const label of labels) {
    const text = trySafeInline(label, `watchlist.${code}.monitor`);
    if (!text) continue;
    if (/量价|流动性|成交活跃/.test(text) && marketUsable && marketMarker) {
      add("量价：上一交易日行情已提供");
    }
    if (/毛利率/.test(text) && hasMargin) {
      add("毛利率：数据包已提供");
    }
    if (/盈利|财务/.test(text) && hasProfit) {
      add("归母净利润：数据包已提供");
    }
    if (/现金流/.test(text) && hasCashFlow) {
      add("现金流：数据包已提供");
    }
    if (/公告|监管/.test(text) && officialQueried) {
      add(hasWindowFilings
        ? "巨潮查询：完成，窗口内有披露条目"
        : "巨潮查询：完成，仅代表有限查询结果");
    }
  }
  return lines;
}

function renderOfficialFactLine(item, registry, prefix, fieldPrefix) {
  const title = trySafeInline(item.title, `${fieldPrefix}.title`);
  const marker = registry.marker(item, {
    allowedSourceTypes: OFFICIAL_SOURCE_TYPES,
    requireAll: true,
    alignTime: false
  });
  if (!title || !marker) return null;
  const code = trySafeInline(String(item.code ?? ""), `${fieldPrefix}.code`);
  const name = trySafeInline(item.name, `${fieldPrefix}.name`);
  const disclosed = trySafeInline(String(item.disclosureDate ?? ""), `${fieldPrefix}.disclosureDate`);
  const subject = code && name ? `${code} ${name}` : "股票池";
  const when = disclosed ? `（披露 ${disclosed}）` : "";
  return `- [事实] ${prefix} ${subject}${when}：${title}。${marker}`;
}

function buildIncrementalDigest({
  watchlist,
  highlights,
  officialFilingLines,
  queryCompleteCount,
  queryMissingCount
}) {
  const lines = [];
  const add = (line) => {
    if (lines.length >= 5 || !line) return;
    lines.push(line);
  };
  for (const line of highlights ?? []) add(line);
  for (const line of officialFilingLines.slice(0, 3)) {
    add(line.replace("[事实] 巨潮公告 ", "[事实] 新增披露 "));
  }
  if (lines.length === 0 && officialFilingLines.length === 0 && queryMissingCount === 0 && queryCompleteCount > 0) {
    add("- [事实] 当前数据包中无可引用的窗口内公告条目，仅代表本次有限查询范围。");
  }
  if (lines.length === 0 && queryMissingCount > 0) {
    add("- [事实] 股票池官方公告查询未完成或结果不适用于截止，不判断窗口内有无新增披露。");
  }
  if (watchlist.some((item) => !item.financialComparison || item.financialComparison.state === "none") &&
    lines.length < 5 && highlights.length === 0) {
    add("- 财务增量：缺少已核验的历史比较基准，不判断是否更新。");
  }
  if (lines.length === 0) {
    add(watchlist.length > 0
      ? "- [事实] 本次无新增可核验事项；关键数字见第 4 节。"
      : "- 本次仅列示已核验字段，不作新增事项结论。");
  }
  return lines;
}

function statusLabel(status) {
  return ({
    live: "实时数据包",
    partial: "部分可用",
    demo: "示例数据",
    unavailable: "不可用"
  })[status] ?? safeInline(String(status ?? "未知"), "meta.status");
}

/**
 * Build a deterministic, evidence-only fallback report. The function never
 * asks a model to write Markdown and refuses to run unless the caller's core
 * publication gate has already passed.
 */
export function buildVerifiedBaselineReport({
  context,
  targetDate,
  cutoffLocal,
  cutoffInstant = null,
  publicationLevel,
  publicationAudit,
  disclaimer,
  marketDataMaxAgeHours = 18,
  newsMaxAgeHours = 72,
  analysisOverlay = null,
  capture = null
}) {
  if (!context || typeof context !== "object" || Array.isArray(context)) {
    throw new Error("基础版晨报缺少结构化 context");
  }
  assertIsoDate(targetDate, "targetDate");
  if (context?.meta?.targetDate !== targetDate) {
    throw new Error(`context 目标日期不匹配: ${context?.meta?.targetDate ?? "缺失"}`);
  }
  const timezone = safeInline(context?.meta?.timezone, "meta.timezone");
  if (timezone !== "Asia/Shanghai") {
    throw new Error("A 股程序核验基础版只接受 Asia/Shanghai 数据包");
  }
  const cutoffLocalMatch = String(cutoffLocal).match(
    /^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})$/
  );
  if (!cutoffLocalMatch || cutoffLocalMatch[1] !== targetDate) {
    throw new Error("cutoffLocal 必须是目标日期的 YYYY-MM-DD HH:mm");
  }
  const cutoffFromLabel = new Date(
    `${cutoffLocalMatch[1]}T${cutoffLocalMatch[2]}:${cutoffLocalMatch[3]}:00+08:00`
  );
  if (Number.isNaN(cutoffFromLabel.getTime())) throw new Error("cutoffLocal 不是有效时间");
  const cutoff = resolveCutoffInstant(cutoffInstant, cutoffLocal, timezone);
  if (Math.abs(cutoff.getTime() - cutoffFromLabel.getTime()) > 60_000) {
    throw new Error("cutoffInstant 与 cutoffLocal 不一致");
  }
  safeInline(cutoffLocal, "cutoffLocal");
  const level = safeInline(publicationLevel, "publicationLevel");
  if (!publicationAudit || publicationAudit.passed !== true ||
      (Array.isArray(publicationAudit.blockers) && publicationAudit.blockers.length > 0)) {
    throw new Error("核心发布门禁未通过，拒绝构建程序核验基础版");
  }
  const auditedCoverage = publicationAudit.marketCoverage;
  if (!auditedCoverage || !Number.isInteger(auditedCoverage.obtained) ||
      !Number.isInteger(auditedCoverage.required) || auditedCoverage.obtained < 0 ||
      auditedCoverage.required <= 0 || auditedCoverage.obtained > auditedCoverage.required ||
      !Array.isArray(auditedCoverage.gaps)) {
    throw new Error("核心发布门禁缺少有效的必要项覆盖审计");
  }
  if (!["live", "partial"].includes(context?.meta?.status)) {
    throw new Error("程序核验基础版只接受 live/partial 数据包");
  }
  if (!isPositiveNumber(marketDataMaxAgeHours) || !isPositiveNumber(newsMaxAgeHours)) {
    throw new Error("时效阈值必须是正数");
  }
  if (typeof disclaimer !== "string" || disclaimer.trim() === "") {
    throw new Error("基础版晨报缺少免责声明");
  }

  const data = context.data ?? {};
  const calendar = data.tradingCalendar;
  const session = data.previousAShareSession;
  if (calendar?.status !== "trading_day" || calendar?.targetDate !== targetDate) {
    throw new Error("交易日历未确认目标日期为交易日");
  }
  const previousDate = assertIsoDate(calendar.previousTradingDate, "previousTradingDate");
  if (!session || session.sessionDate !== previousDate || session.sessionDate >= targetDate) {
    throw new Error("上一交易日行情日期与交易日历不一致");
  }

  const registry = new SourceRegistry(context, cutoff, timezone, {
    marketDataMaxAgeHours,
    newsMaxAgeHours
  });
  const calendarMarker = registry.marker(calendar, {
    allowedSourceTypes: CALENDAR_SOURCE_TYPES,
    requireAll: true
  });
  if (!calendarMarker) throw new Error("交易日历缺少截止前可核验来源");

  let anyStaleMarketFact = false;
  const marketLines = [];
  for (const identity of CORE_INDICES) {
    const matches = (session.indices ?? []).filter(
      (item) => item?.code === identity.code || item?.name === identity.name
    );
    const record = matches.find(
      (item) => item?.code === identity.code && item?.name === identity.name
    );
    if (matches.length !== 1 || !record || !marketRecordIsUsable(
      record,
      "close",
      previousDate,
      cutoff,
      timezone,
      {
        close: "index_point",
        previousClose: "index_point",
        change: "index_point",
        changePercent: "percentage_point"
      }
    )) {
      throw new Error(`核心指数 ${identity.name} 无法安全编译`);
    }
    // A supplementary amount source must not become proof for price facts.
    // Only legacy records without field-level provenance use the merged IDs.
    const priceRecord = Object.hasOwn(record, "priceSourceIds")
      ? {
          ...record,
          sourceIds: declaredSourceIds(record.priceSourceIds, `${identity.name}.priceSourceIds`) ?? []
        }
      : record;
    const marker = registry.marker(priceRecord, {
      allowedSourceTypes: MARKET_SOURCE_TYPES,
      requireAll: true
    });
    if (!marker) throw new Error(`核心指数 ${identity.name} 缺少对时来源`);
    const stale = isStale(record.dataAsOf, cutoff, marketDataMaxAgeHours);
    anyStaleMarketFact ||= stale;
    const suffix = staleSuffix(stale);
    marketLines.push(
      `- [事实] ${identity.name}：收盘 ${formatNumber(record.close, 3)} 点${suffix}，` +
      `前收 ${formatNumber(record.previousClose, 3)} 点${suffix}，` +
      `涨跌 ${signedNumber(record.change, 3)} 点${suffix}（${signedNumber(record.changePercent, 2)}%${suffix}）。${marker}`
    );
  }

  const turnoverMarker = registry.markerForExactIds(
    preciseAggregateSourceIds(session, "turnover"),
    session.dataAsOf,
    { allowedSourceTypes: MARKET_SOURCE_TYPES }
  );
  const sessionStale = isStale(session.dataAsOf, cutoff, marketDataMaxAgeHours);
  const sessionSuffix = staleSuffix(sessionStale);
  if (isPositiveNumber(session.totalTurnover) &&
      session?.units?.totalTurnover === "CNY" && turnoverMarker) {
    anyStaleMarketFact ||= sessionStale;
    marketLines.push(
      `- [事实] 沪深两市成交额：${formatNumber(session.totalTurnover / 100_000_000, 2)} 亿元${sessionSuffix}。${turnoverMarker}`
    );
  } else {
    marketLines.push("- 沪深两市成交额：单位或来源不可核验。");
  }
  const breadth = session.marketBreadth;
  const breadthMarker = registry.markerForExactIds(
    preciseAggregateSourceIds(session, "breadth"),
    session.dataAsOf,
    { allowedSourceTypes: MARKET_SOURCE_TYPES }
  );
  if (isFiniteNumber(breadth?.advancers) && breadth.advancers >= 0 &&
      isFiniteNumber(breadth?.decliners) && breadth.decliners >= 0 &&
      isFiniteNumber(breadth?.unchanged) && breadth.unchanged >= 0 &&
      breadth.advancers + breadth.decliners > 0 && breadthMarker) {
    anyStaleMarketFact ||= sessionStale;
    marketLines.push(
      `- [事实] 市场宽度：上涨 ${formatNumber(breadth.advancers, 0)} 家${sessionSuffix}，` +
      `下跌 ${formatNumber(breadth.decliners, 0)} 家${sessionSuffix}，` +
      `平盘 ${formatNumber(breadth.unchanged, 0)} 家${sessionSuffix}。${breadthMarker}`
    );
  } else {
    marketLines.push("- 市场宽度：家数或来源不可核验。");
  }

  const validSectors = (Array.isArray(data.sectors) ? data.sectors : [])
    .filter((item) => {
      if (!item || !isFiniteNumber(item.changePercent) ||
          item?.units?.changePercent !== "percentage_point") return false;
      const time = new Date(item.dataAsOf);
      return !Number.isNaN(time.getTime()) && time.getTime() <= cutoff.getTime() + 60_000 &&
        registry.usableIds(item, { allowedSourceTypes: SECTOR_SOURCE_TYPES }).length ===
          sourceIds(item).length;
    });
  const sectorSort = (left, right) =>
    (Number(left.rank) || Number.MAX_SAFE_INTEGER) -
    (Number(right.rank) || Number.MAX_SAFE_INTEGER) ||
    String(left.name).localeCompare(String(right.name), "zh-CN");
  const upSectors = validSectors.filter((item) => item.direction === "up")
    .toSorted(sectorSort).slice(0, 3);
  const downSectors = validSectors.filter((item) => item.direction === "down")
    .toSorted(sectorSort).slice(-3).reverse();
  const sectorLines = [];
  for (const item of [...upSectors, ...downSectors]) {
    const marker = registry.marker(item, {
      allowedSourceTypes: SECTOR_SOURCE_TYPES,
      requireAll: true
    });
    const stale = isStale(item.dataAsOf, cutoff, marketDataMaxAgeHours);
    anyStaleMarketFact ||= stale;
    const extras = [];
    if (isFiniteNumber(item.netInflow) && item.units?.netInflow === "CNY_100_million") {
      extras.push(`供应商净流入指标 ${signedNumber(item.netInflow, 2)} 亿元${staleSuffix(stale)}`);
    }
    const leader = trySafeInline(item.leader, "sector.leader");
    if (leader && isFiniteNumber(item.leaderChangePercent) &&
        item.units?.leaderChangePercent === "percentage_point") {
      extras.push(`供应商列首个股 ${leader} ${signedNumber(item.leaderChangePercent, 2)}%${staleSuffix(stale)}`);
    }
    const extraText = extras.length > 0 ? `，${extras.join("，")}` : "";
    sectorLines.push(
      `- [事实] ${safeInline(item.name, "sector.name")}：涨跌幅 ${signedNumber(item.changePercent, 2)}%${staleSuffix(stale)}${extraText}。${marker}`
    );
  }
  if (sectorLines.length === 0) sectorLines.push("板块排行缺少可核验数值与来源。");

  const watchlistLines = [];
  const watchlist = Array.isArray(data.watchlist) ? data.watchlist : [];
  let wroteFinancialNumbers = false;
  let queryCompleteCount = 0;
  let queryMissingCount = 0;
  const digestHighlights = [];
  let unverifiedRevenue = false;
  for (const item of watchlist) {
    const code = safeInline(String(item?.code ?? ""), "watchlist.code");
    const name = safeInline(item?.name, `watchlist.${code}.name`);
    const market = item?.marketData;
    if (market != null) {
      const marketCode = safeInline(
        String(market?.code ?? ""),
        `watchlist.${code}.marketData.code`
      );
      const marketName = safeInline(market?.name, `watchlist.${code}.marketData.name`);
      if (marketCode !== code || marketName !== name) {
        throw new Error(`股票池 ${code} ${name} 与 marketData 身份不一致`);
      }
    }
    watchlistLines.push(`### ${code} ${name}`);
    const marketUsable = item?.status !== "unavailable" && marketRecordIsUsable(
      market,
      "last",
      previousDate,
      cutoff,
      timezone,
      {
        last: "CNY_per_share",
        previousClose: "CNY_per_share",
        change: "CNY_per_share",
        changePercent: "percentage_point"
      }
    );
    const marketMarker = marketUsable ? registry.marker(market, {
      allowedSourceTypes: MARKET_SOURCE_TYPES,
      requireAll: true
    }) : null;
    if (marketUsable && marketMarker) {
      const stale = isStale(market.dataAsOf, cutoff, marketDataMaxAgeHours);
      anyStaleMarketFact ||= stale;
      const suffix = staleSuffix(stale);
      watchlistLines.push(
        `- [事实] 行情变化：收盘 ${formatNumber(market.last, 2)} 元${suffix}，` +
        `前收 ${formatNumber(market.previousClose, 2)} 元${suffix}，` +
        `涨跌额 ${signedNumber(market.change, 2)} 元${suffix}，` +
        `涨跌幅 ${signedNumber(market.changePercent, 2)}%${suffix}。${marketMarker}`
      );
      const detailParts = [];
      const rangeUnitsOk = ["open", "high", "low"].every(
        (key) => market.units?.[key] === undefined || market.units[key] === "CNY_per_share"
      );
      if (rangeUnitsOk) {
        if (isPositiveNumber(market.open)) detailParts.push(`开 ${formatNumber(market.open, 2)} 元${suffix}`);
        if (isPositiveNumber(market.high)) detailParts.push(`高 ${formatNumber(market.high, 2)} 元${suffix}`);
        if (isPositiveNumber(market.low)) detailParts.push(`低 ${formatNumber(market.low, 2)} 元${suffix}`);
      }
      if (isPositiveNumber(market.high) && isPositiveNumber(market.low) && market.high < market.low) {
        detailParts.length = 0;
      }
      if (isPositiveNumber(market.turnover) && market.units?.turnover === "CNY") {
        detailParts.push(`成交额 ${formatCny(market.turnover)}${suffix}`);
      }
      if (detailParts.length > 0) {
        watchlistLines.push(`- [事实] 行情细节：${detailParts.join("，")}。${marketMarker}`);
      }
      const subjectName = trySafeInline(item?.name, `watchlist.${code}.digestName`);
      const notable = notableChangePercent(market);
      if (subjectName && notable !== null) {
        digestHighlights.push(`- [事实] ${code} ${subjectName}：涨跌幅 ${signedNumber(notable, 2)}%${suffix}（未核验为涨跌停）。${marketMarker}`);
      }
    } else {
      watchlistLines.push("- 行情变化：上一交易日行情无法核验。");
      watchlistLines.push("- 逻辑状态：研究线索，待验证。");
      watchlistLines.push("- 观察条件：下一次数据采集后重新核验行情与证据字段。");
    }
    const financialPeriodUsable = validDate(item?.financials?.reportDate) && item.financials.reportDate <= targetDate;
    const financialMarker = financialPeriodUsable ? registry.marker(item.financials, {
      allowedSourceTypes: FINANCIAL_SOURCE_TYPES,
      requireAll: true
    }) : null;
    if (financialMarker && /^\d{4}-\d{2}-\d{2}$/.test(item.financials.reportDate ?? "")) {
      const fin = item.financials;
      const financialParts = [];
      const verifiedFiling = verifiedFinancialDocument(item, context.sources ?? [], cutoff);
      const filingComparison = verifiedFiling ? financialFieldComparison(item, verifiedFiling.doc) : null;
      const revenueConflict = filingComparison?.label === "数值不一致，差异原因待核" &&
        filingComparison.amounts.official !== null && filingComparison.amounts.provider !== null;
      const unitPending = filingComparison?.label === "供应商单位待核，暂不直接比较" &&
        filingComparison.amounts.official !== null;
      if (unitPending) {
        const amounts = filingComparison.amounts;
        let revenueText = `公告原列营收 ${formatCny(amounts.official)}`;
        if (amounts.officialYoy !== null) revenueText += `（同比 ${signedNumber(amounts.officialYoy, 2)}%）`;
        revenueText += "；供应商单位待核，暂不直接比较";
        financialParts.push(revenueText);
      } else if (revenueConflict && filingComparison.dimensions.unit.state === "matched") {
        const amounts = filingComparison.amounts;
        let revenueText = `公告原列营收 ${formatCny(amounts.official)}`;
        if (amounts.officialYoy !== null) revenueText += `（同比 ${signedNumber(amounts.officialYoy, 2)}%）`;
        revenueText += `；供应商口径 ${formatCny(amounts.provider)}`;
        if (amounts.providerYoy !== null) revenueText += `（供应商同比 ${signedNumber(amounts.providerYoy, 2)}%）`;
        revenueText += "；数值不一致，差异原因待核";
        financialParts.push(revenueText);
      } else if (isFiniteNumber(fin.revenue) && fin.units?.revenue === "CNY") {
        let revenueText = `聚合营收 ${formatCny(fin.revenue)}`;
        if (isFiniteNumber(fin.revenueYoY) && fin.units?.revenueYoY === "percentage_point") {
          revenueText += `（供应商同比 ${signedNumber(fin.revenueYoY, 2)}%）`;
        }
        financialParts.push(revenueText);
      }
      if (isFiniteNumber(fin.parentNetProfit) && fin.units?.parentNetProfit === "CNY") {
        let profitText = `归母净利润 ${formatCny(fin.parentNetProfit)}`;
        if (isFiniteNumber(fin.parentNetProfitYoY) && fin.units?.parentNetProfitYoY === "percentage_point") {
          profitText += `（同比 ${signedNumber(fin.parentNetProfitYoY, 2)}%）`;
        }
        financialParts.push(profitText);
      }
      if (isFiniteNumber(fin.deductedNetProfit) && fin.units?.deductedNetProfit === "CNY") {
        let deductedText = `扣非归母净利润 ${formatCny(fin.deductedNetProfit)}`;
        if (isFiniteNumber(fin.deductedNetProfitYoY) && fin.units?.deductedNetProfitYoY === "percentage_point") {
          deductedText += `（同比 ${signedNumber(fin.deductedNetProfitYoY, 2)}%）`;
        }
        financialParts.push(deductedText);
      }
      if (isFiniteNumber(fin.grossMargin) && fin.units?.grossMargin === "percentage_point") {
        financialParts.push(`毛利率 ${formatNumber(fin.grossMargin, 2)}%`);
      }
      if (isFiniteNumber(fin.roeWeighted) && fin.units?.roeWeighted === "percentage_point") {
        financialParts.push(`加权 ROE ${formatNumber(fin.roeWeighted, 2)}%`);
      }
      if (isFiniteNumber(fin.eps) && fin.units?.eps === "CNY_per_share") {
        financialParts.push(`EPS ${formatNumber(fin.eps, 4)} 元`);
      }
      const season = trySafeInline(fin.seasonLabel, `watchlist.${code}.financials.seasonLabel`);
      const period = season ? `${fin.reportDate}，${season}` : fin.reportDate;
      const comparison = item.financialComparison?.state ?? "none";
      const baselineDate = trySafeInline(item.financialComparison?.baselineTargetDate, "financial.baselineTargetDate");
      if (comparison === "same_period" && baselineDate && financialParts.length > 0) {
        wroteFinancialNumbers = true;
        watchlistLines.push(
          `- [事实] 财务（${period}）：${revenueConflict ? financialParts[0] + "。" + financialParts.slice(1).join("，") : financialParts.join("，")}。与已封存快报（${baselineDate}）一致。${financialMarker}`
        );
      } else if (comparison === "values_changed" && baselineDate && financialParts.length > 0) {
        wroteFinancialNumbers = true;
        watchlistLines.push(
          `- [事实] 财务同报告期字段较已封存快报（${baselineDate}）变化（${period}）：${revenueConflict ? financialParts[0] + "。" + financialParts.slice(1).join("，") : financialParts.join("，")}。${financialMarker}`
        );
      } else if (financialParts.length > 0) {
        wroteFinancialNumbers = true;
        watchlistLines.push(
          `- [事实] 财务（${period}）：${revenueConflict ? financialParts[0] + "。" + financialParts.slice(1).join("，") : financialParts.join("，")}。${financialMarker}`
        );
      } else {
        watchlistLines.push(
          `- [事实] 财务字段：数据包包含报告期 ${fin.reportDate}。${financialMarker}`
        );
      }
      if (isFiniteNumber(fin.revenue) && fin.units?.revenue === "CNY") {
        unverifiedRevenue = true;
      }
      const subject = trySafeInline(item?.name, `watchlist.${code}.digestFinName`);
      if (subject && comparison === "new_period" && /^\d{4}-\d{2}-\d{2}$/.test(item.financials.reportDate ?? "")) {
        digestHighlights.push(`- [事实] ${code} ${subject}：财务报告期已更新为 ${item.financials.reportDate}。${financialMarker}`);
      } else if (subject && comparison === "values_changed") {
        digestHighlights.push(`- [事实] ${code} ${subject}：同报告期财务字段较已封存基准变化，详见股票卡。${financialMarker}`);
      }
      if (comparison === "none") {
        watchlistLines.push("- 财务比较：缺少已核验的历史比较基准。");
      } else if (comparison === "units_changed") {
        watchlistLines.push(`- 财务比较：单位发生变化，不作数值增量判断；基准快报 ${baselineDate}。`);
      } else if (comparison === "metric_changed") {
        watchlistLines.push(`- 财务比较：营收字段定义或核验状态变化，不作同口径数值增量判断；基准快报 ${baselineDate}。`);
      } else if (comparison === "version_changed") {
        watchlistLines.push(`- 财务比较：来源版本或时间发生变化，数值未变；基准快报 ${baselineDate}。`);
      } else if (comparison === "period_regressed") {
        watchlistLines.push(`- 财务比较：报告期倒退，不能视为更新；基准快报 ${baselineDate}。`);
      }
    } else {
      watchlistLines.push("- 财务字段：缺少可引用的报告期来源。");
    }
    const evidenceCard = financialDocumentLines(item, context.sources ?? [], cutoff, registry, previousDate);
    watchlistLines.push(...evidenceCard.lines);
    wroteFinancialNumbers ||= evidenceCard.wroteNumbers;
    if (evidenceCard.lines.some((line) => line.includes("口径待核"))) {
      const disagreeName = trySafeInline(item?.name, `watchlist.${code}.digestDisagreeName`);
      if (disagreeName) {
        digestHighlights.push(`- [事实] ${code} ${disagreeName}：聚合营收与官方摘要原列不一致，口径待核。`);
      }
    }
    const hasCashFlow = evidenceCard.hasCashFlow || Boolean(financialMarker && isFiniteNumber(item?.financials?.operatingCashFlow) &&
      item.financials.units?.operatingCashFlow === "CNY");
    if (!hasCashFlow && (evidenceCard.used || evidenceCard.lines.length === 0)) {
      watchlistLines.push(evidenceCard.used
        ? "- 当前财务期现金流：缺少同报告期的可核验字段，不作判断。"
        : "- 现金流：数据包未提供现金流字段，不作判断。");
    }
    const monitorLabels = Array.isArray(item?.monitor) ? item.monitor : [];
    const query = disclosureQueryState(item, context.sources ?? [], cutoff, { previousDate });
    const queryUsable = query.state === "available";
    if (queryUsable && query.coverage === "declared_complete") queryCompleteCount += 1;
    else queryMissingCount += 1;
    const monitorStatuses = watchlistMonitorStatuses({
      labels: monitorLabels,
      code,
      marketUsable,
      marketMarker,
      hasProfit: Boolean(financialMarker && isFiniteNumber(item?.financials?.parentNetProfit) && item.financials.units?.parentNetProfit === "CNY"),
      hasMargin: Boolean(financialMarker && isFiniteNumber(item?.financials?.grossMargin) && item.financials.units?.grossMargin === "percentage_point"),
      hasCashFlow,
      officialQueried: queryUsable,
      hasWindowFilings: (data.announcementsAndNews ?? []).some((entry) =>
        filingIsEligible(entry, context.sources ?? [], { cutoffInstant: cutoff, previousDate, code }))
    });
    if (monitorStatuses.length > 0) {
      watchlistLines.push("- 研究监控：");
      for (const text of monitorStatuses) {
        watchlistLines.push(`  - ${text}`);
      }
    }
    if (query.state === "unavailable") {
      watchlistLines.push("- 官方源缺失：未提供巨潮公告查询结果。");
    } else if (query.state === "after_cutoff") {
      watchlistLines.push("- 官方查询限制：结果在截止后取得，不据此确认截止前有无披露。");
    } else if (query.state === "unverified") {
      watchlistLines.push("- 官方查询限制：查询来源或实际时间无法核验。");
    } else if (query.state === "available") {
      // Make the real coverage boundary explicit: the query finished before the
      // frozen cutoff, so window filings can be judged up to this instant only.
      watchlistLines.push(
        query.coverage === "declared_complete"
          ? `- 官方查询：完成于 ${localDateTime(query.queriedAt, timezone)}（${timezone}），窗口分页已核验；非原子快照，最早页面观测 ${localDateTime(query.observedThroughAt ?? query.queriedAt, timezone)}；查询后至数据截止 ${safeInline(cutoffLocal, "cutoffLocal")} 的新增披露仍未知。`
          : `- 官方查询：完成于 ${localDateTime(query.queriedAt, timezone)}（${timezone}），仅有限查询，未证明窗口查全；数据截止 ${safeInline(cutoffLocal, "cutoffLocal")} 不变。`
      );
    } else if (query.state === "partial") {
      watchlistLines.push("- 官方查询限制：部分查询成功，覆盖不完整。");
    }
    watchlistLines.push("");
  }
  if (watchlist.length === 0) watchlistLines.push("股票池为空，基础版未生成个股段落。");

  const overnightItems = (Array.isArray(data.overnightGlobal) ? data.overnightGlobal : [])
    .filter((item) => overnightRecordIsUsable(item, targetDate, cutoff))
    .toSorted((left, right) => {
      const leftRank = OVERNIGHT_ORDER.indexOf(String(left?.symbol ?? ""));
      const rightRank = OVERNIGHT_ORDER.indexOf(String(right?.symbol ?? ""));
      return (leftRank === -1 ? OVERNIGHT_ORDER.length : leftRank) -
        (rightRank === -1 ? OVERNIGHT_ORDER.length : rightRank);
    });
  const overnightLines = [];
  for (const item of overnightItems.slice(0, 6)) {
    const name = trySafeInline(item.name, "overnight.name");
    const marker = registry.marker(item, {
      allowedSourceTypes: MARKET_SOURCE_TYPES,
      requireAll: true
    });
    if (!name || !marker) continue;
    // Use exactly the same evidence policy as the source list: newly collected
    // US indices are checked against the last completed US trading session,
    // while legacy untyped evidence retains the configured hourly threshold.
    const stale = registry.usableIds(item, {
      allowedSourceTypes: MARKET_SOURCE_TYPES, requireAll: true
    }).some((id) => registry.freshnessForSource(registry.byId.get(id)).stale);
    anyStaleMarketFact ||= stale;
    const suffix = staleSuffix(stale);
    overnightLines.push(
      `- [事实] ${name}（${item.sessionDate}）：收盘 ${formatNumber(item.close, 2)} 点${suffix}，` +
      `涨跌 ${signedNumber(item.change, 2)} 点${suffix}（${signedNumber(item.changePercent, 2)}%${suffix}）。${marker}`
    );
  }
  const overnightSection = overnightLines.length > 0
    ? [
      ...overnightLines,
      "",
      "程序核验基础版未生成跨市场因果解释；以上仅列示可核验收盘事实。"
    ]
    : ["程序核验基础版未生成跨市场因果解释；相关数据留待分析层处理。"];

  const watchlistCodes = new Set(watchlist.map((item) => String(item?.code ?? "")));
  const dragon = (Array.isArray(data.capitalFlows) ? data.capitalFlows : [])
    .find((item) => item?.type === "dragon_tiger_list" && item?.status === "available");
  const dragonLines = [];
  if (dragon && dragon.sessionDate === previousDate && Array.isArray(dragon.entries)) {
    const ranked = dragon.entries
      .filter((entry) => isFiniteNumber(entry?.netBuy) && entry.units?.netBuy === "CNY" &&
        isFiniteNumber(entry?.changePercent) && entry.units?.changePercent === "percentage_point")
      .toSorted((left, right) => Math.abs(right.netBuy) - Math.abs(left.netBuy));
    const selected = ranked
      .filter((entry) => watchlistCodes.has(String(entry?.code ?? "")))
      .slice(0, MAX_DRAGON_LINES);
    for (const entry of selected) {
      const code = trySafeInline(String(entry.code ?? ""), "dragon.code");
      const name = trySafeInline(entry.name, "dragon.name");
      const marker = registry.marker(entry, {
        allowedSourceTypes: DRAGON_SOURCE_TYPES,
        requireAll: true
      });
      if (!code || !name || !marker) continue;
      const stale = isStale(entry.dataAsOf, cutoff, marketDataMaxAgeHours);
      anyStaleMarketFact ||= stale;
      const suffix = staleSuffix(stale);
      const extreme = Math.abs(entry.changePercent) > DRAGON_EXTREME_CHANGE_PERCENT;
      const extremeLabel = extreme ? "，待核验口径" : "";
      dragonLines.push(
        `- [事实] 龙虎榜 ${code} ${name}：涨跌幅 ${signedNumber(entry.changePercent, 2)}%${suffix}，` +
        `净买入额 ${formatCny(entry.netBuy)}${suffix}${extremeLabel}。${marker}`
      );
    }
  }
  const officialCandidates = (Array.isArray(data.announcementsAndNews) ? data.announcementsAndNews : [])
    .filter((item) => watchlistCodes.has(String(item?.code ?? "")) &&
      filingIsEligible(item, context.sources ?? [], { cutoffInstant: cutoff, previousDate }))
    .toSorted((left, right) => String(right?.disclosureDate ?? right?.publishedAt ?? "")
      .localeCompare(String(left?.disclosureDate ?? left?.publishedAt ?? "")));
  const officialFilingLines = [];
  for (const item of officialCandidates) {
    if (officialFilingLines.length >= MAX_OFFICIAL_LINES) break;
    const line = renderOfficialFactLine(item, registry, "巨潮公告", "official");
    if (line) officialFilingLines.push(line);
  }
  const officialEmptyLines = [];
  if (officialFilingLines.length === 0) {
    for (const item of watchlist) {
      if (officialEmptyLines.length >= watchlist.length) break;
      const query = disclosureQueryState(item, context.sources ?? [], cutoff, { previousDate });
      if (query.state !== "available") continue;
      const code = trySafeInline(String(item.code ?? ""), "official.empty.code");
      const name = trySafeInline(item.name, "official.empty.name");
      const queryInstant = query.queriedAt;
      const marker = registry.marker({
        sourceIds: item.officialDisclosure.sourceIds,
        dataAsOf: queryInstant
      }, {
        allowedSourceTypes: QUERY_SOURCE_TYPES,
        requireAll: true,
        alignTime: false
      });
      if (!code || !name || !marker) continue;
      officialEmptyLines.push(
        `- [事实] ${code} ${name}：当前数据包中无可引用的窗口内公告条目，仅代表本次有限查询范围。${marker}`
      );
    }
  }
  const officialLines = officialFilingLines.length > 0 ? officialFilingLines : officialEmptyLines;
  const backgroundLines = [];
  for (const item of watchlist) {
    const rows = Array.isArray(item?.officialDisclosure?.recentBackground)
      ? item.officialDisclosure.recentBackground
      : [];
    for (const row of rows) {
      if (backgroundLines.length >= MAX_BACKGROUND_LINES) break;
      if (!filingIsEligible(row, context.sources ?? [], { cutoffInstant: cutoff, previousDate, background: true, code: item.code })) continue;
      const line = renderOfficialFactLine(
        { ...row, code: row.code ?? item.code, name: row.name ?? item.name },
        registry,
        "近期背景（非今日新增）",
        "official.background"
      );
      if (line) backgroundLines.push(line);
    }
  }
  const mediaCandidates = (Array.isArray(data.announcementsAndNews) ? data.announcementsAndNews : [])
    .filter((item) => (item?.type === "news" || item?.type === undefined) &&
      item?.sourceType !== "official_announcement" &&
      watchlistCodes.has(String(item?.code ?? "")))
    .toSorted((left, right) => String(right?.publishedAt ?? "").localeCompare(String(left?.publishedAt ?? "")));
  const mediaLines = [];
  for (const item of mediaCandidates) {
    if (mediaLines.length >= MAX_MEDIA_LINES) break;
    if (isStale(item.publishedAt ?? item.dataAsOf, cutoff, MEDIA_MAX_AGE_HOURS)) continue;
    const title = trySafeInline(item.title, "media.title");
    const code = trySafeInline(String(item.code ?? ""), "media.code");
    const name = trySafeInline(item.name, "media.name");
    if (!title || !code || !name) continue;
    if (MEDIA_MARKET_WIDE_PATTERN.test(title)) continue;
    if (!title.includes(code) && !title.includes(name)) continue;
    const marker = registry.marker(item, {
      allowedSourceTypes: MEDIA_SOURCE_TYPES,
      requireAll: true
    });
    if (!marker) continue;
    mediaLines.push(`- [媒体线索，非公司公告] ${code} ${name}：${title}。${marker}`);
  }
  const eventSection = [
    "### 窗口内新增",
    "",
    ...(officialLines.length > 0
      ? officialLines
      : queryCompleteCount > 0
        ? ["当前数据包中无可引用的窗口内公告条目，仅代表本次有限查询范围。"]
        : ["官方公告查询未完成或不适用于截止，不判断窗口内有无新增披露。"]),
    "",
    "### 近期背景（非今日新增）",
    "",
    ...(backgroundLines.length > 0 ? backgroundLines : ["未列入窗口外的可引用披露条目。"]),
    ""
  ];
  if (mediaLines.length > 0) {
    eventSection.push(...mediaLines, "");
  }
  eventSection.push("程序核验基础版未把媒体聚合内容写成公司或交易所官方事实。");

  const overlay = renderOverlay(analysisOverlay, registry, context);
  const digestLines = buildIncrementalDigest({
    watchlist,
    highlights: digestHighlights,
    officialFilingLines,
    queryCompleteCount,
    queryMissingCount
  });
  if (capture && typeof capture === "object") {
    // Structured by-product for the sealed research digest. It is rendered by the
    // same registry instance that numbers the report, so [来源 N] is identical.
    capture.overlay = overlay.items;
    capture.overlayHasContent = overlay.hasContent;
    capture.conclusionLines = [...digestLines];
    // Previous-session market facts, already rendered with the same registry. The
    // baseline digest uses them as referenced facts when there is no inference item.
    capture.marketFacts = [...marketLines];
    // Full number -> source mapping of this same rendering pass, so a consumer can
    // resolve any [来源 N] the report used (including baseline fact lines).
    capture.sources = registry.orderedIds.map((id, index) => {
      const source = registry.byId.get(id) ?? {};
      return {
        number: index + 1,
        sourceId: id,
        title: typeof source.title === "string" ? source.title : null,
        url: typeof source.url === "string" ? source.url : null
      };
    });
  }

  const hasOfficialSource = officialFilingLines.length > 0 || backgroundLines.length > 0 ||
    watchlist.some((item) => verifiedFinancialDocument(item, context.sources ?? [], cutoff));
  const gapLines = [];
  if (context.meta?.dataQuality?.schemaVersion === "research-data-quality-v1") {
    gapLines.push("- 状态判定仅覆盖声明的晨报输入；来源可信度与覆盖分数分开，不表示完整研究或语义已审。");
    gapLines.push("- 公告分页不是原子快照；真实查询之后至固定截止的新增披露仍未知，不据此断言没有新公告。");
  }
  if ((session.indices ?? []).some((item) => item?.turnover == null)) {
    gapLines.push("- 三大指数各自成交额字段缺失；本报告未据此作指数成交额比较。");
  }
  const northbound = (data.capitalFlows ?? []).find(
    (item) => item?.type === "northbound_realtime"
  );
  if (unverifiedRevenue) {
    gapLines.push("- 财务口径：营收金额及同比为供应商口径，尚未与公告核对，不等同于已核验的营业收入；不据此推断经营杠杆。");
  }
  const uniqueGaps = [...new Set(gapLines)];
  if (uniqueGaps.length === 0) {
    uniqueGaps.push("- 其余缺口已在第 1 节覆盖率三行中列示。");
  }

  const completeness = context?.meta?.dataCompleteness ?? {};
  const coverage = publicationAudit.marketCoverage ?? {};
  const coverageGapText = (coverage.gaps ?? []).length > 0
    ? coverage.gaps.map((item) => safeInline(String(item), "marketCoverage.gap")).join("；")
    : "无";
  const score = (key) => isFiniteNumber(completeness?.[key])
    ? `${formatNumber(completeness[key], 2)}%`
    : "不可计算";
  const staleHeader = anyStaleMarketFact
    ? ["> 陈旧数据：超过行情时效阈值的数值已逐项标记 [陈旧]"]
    : [];
  const latestSessionHeader = sessionStale
    ? [
      `> 行情口径：${previousDate} 已由交易日历确认为最近有效交易日；` +
      `[陈旧] 仅表示距固定截止超过 ${formatNumber(marketDataMaxAgeHours, 2)} 小时、不是实时行情，不表示错用更早交易日。`
    ]
    : [];
  const staleEvidenceHeader = registry.hasStaleTrackedSource()
    ? ["> 陈旧证据：超过适用时效阈值的来源已在引用处及来源清单逐项标记 [陈旧]"]
    : [];
  const sourceLines = registry.lines();
  if (sourceLines.length === 0) throw new Error("基础版晨报没有可核验来源");

  return [
    `# AStock 数据快报（语义未审）｜${targetDate}`,
    "",
    ...(context.meta?.documentReconstruction ? ["> 验证用途：事后资料重建，含截止后取得的财报原文；非当日密封件，不证明当时已知信息，也不构成生产发布许可。"] : []),
    `> 数据截止：${safeInline(cutoffLocal, "cutoffLocal")}（${timezone}）`,
    `> 数据状态：${statusLabel(context?.meta?.status)}`,
    `> 交易日状态：交易日 ${calendarMarker}`,
    `> 发布级别：${level}`,
    `> 生成方式：${overlay.hasContent ? "程序核验基础版 + 结构化分析覆盖层" : "程序核验基础版"}`,
    ...latestSessionHeader,
    ...staleHeader,
    ...staleEvidenceHeader,
    "",
    "## 一句话结论",
    "",
    ...digestLines,
    "",
    "## 1. 数据覆盖与可用性",
    "",
    "> 数据桥模块评分只衡量模块新鲜度与可用性，不代表字段齐全或数值准确。",
    `> 门禁必要项：${formatNumber(coverage.obtained ?? 0, 0)}/${formatNumber(coverage.required ?? 0, 0)}；缺口：${coverageGapText}`,
    `> 模块状态分数（非字段完整度）：行情 ${score("market")} / 资金 ${score("capital")} / 新闻 ${score("news")} / 财务 ${score("financial")}`,
    `> 研究可用项：${overnightLines.length > 0 ? "隔夜已写入" : "隔夜未写入"}；${wroteFinancialNumbers ? "财务数字已写入" : "财务数字未写入"}；${hasOfficialSource ? "公告官方源有" : "公告官方源无"}`,
    ...(northbound?.status === "unavailable"
      ? ["> 北向资金：公开披露口径不再提供实时净流入（交易所 2024-04-12 说明）；本报告不填估算值，也不以成交金额替代净流入。"]
      : []),
    "",
    `## 2. 上一交易日 A 股行情（${previousDate}）`,
    "",
    ...marketLines,
    "",
    "## 3. 隔夜数据与事件",
    "",
    ...overnightSection,
    "",
    "## 4. 板块与股票池",
    "",
    "### 股票池",
    "",
    ...watchlistLines,
    ...(dragonLines.length > 0
      ? ["### 龙虎榜（席位统计，非公告）", "", ...dragonLines, ""]
      : []),
    "## 5. 今日政策、公告与事件",
    "",
    ...eventSection,
    "",
    "## 6. 结构化分析覆盖层",
    "",
    ...overlay.lines,
    "",
    "## 7. 风险提示与数据缺口",
    "",
    ...uniqueGaps,
    "",
    "## 附录",
    "",
    "### 板块排行事实",
    "",
    "供应商板块分类及资金指标仅作统计线索；不等于机构账户流水，也不能证明资金在板块间迁移。",
    "",
    ...sectorLines,
    "",
    "## 来源",
    "",
    "> 以下仅列出本报告实际引用的固定数据包来源。",
    "",
    ...sourceLines,
    "",
    "---",
    "",
    disclaimer.trim(),
    ""
  ].join("\n");
}

export function attachWatchlistResearchPlan(context, corePool) {
  if (!context || typeof context !== "object" || !Array.isArray(context?.data?.watchlist)) {
    return context;
  }
  const byCode = new Map((Array.isArray(corePool) ? corePool : []).map((item) => [String(item?.code ?? ""), item]));
  return {
    ...context,
    data: {
      ...context.data,
      watchlist: context.data.watchlist.map((item) => {
        const plan = byCode.get(String(item?.code ?? ""));
        if (!plan) return item;
        return {
          ...item,
          ...(Array.isArray(plan.monitor) ? { monitor: plan.monitor } : {}),
          ...(Array.isArray(plan.invalidationSignals) ? { invalidationSignals: plan.invalidationSignals } : {}),
          ...(typeof plan.sector === "string" ? { sector: plan.sector } : {})
        };
      })
    }
  };
}
