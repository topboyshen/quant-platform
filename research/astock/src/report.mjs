import { classifySourceFreshness, sourceEvidenceInstant } from "./source-freshness.mjs";
import { buildVerifiedBaselineReport } from "./baseline-report.mjs";
import {
  buildStructuredAnalysisCandidates,
  canonicalStructuredAnalysisText,
  parseStructuredAnalysisOutput,
  STRUCTURED_ANALYSIS_SELECTION_LIMITS,
  structuredAnalysisCandidateSignature
} from "./structured-analysis.mjs";

export const DATA_BRIEF_PUBLICATION_LEVEL =
  "可发布（结构、来源与时效门禁通过；研究语义未审校）";
export const LEGACY_FORMAL_PUBLICATION_LEVEL =
  "正式（核心数据与证据门禁通过）";

export function isAcceptedPublicationLevel(value) {
  return value === DATA_BRIEF_PUBLICATION_LEVEL ||
    value === LEGACY_FORMAL_PUBLICATION_LEVEL;
}

export function buildRunInstruction({
  targetDate,
  workflow,
  agentDescriptor,
  loadedFrom,
  dataStatus,
  disclaimer,
  cutoffLocal,
  cutoffIso,
  requireStaleMarketLabel,
  marketDataMaxAgeHours,
  newsMaxAgeHours,
  publicationLevel,
  publicationAudit = null
}) {
  const canUseWebFallback =
    Boolean(agentDescriptor) && workflow.allowAgentWebFallback && !["live", "partial"].includes(dataStatus);
  const webRule = canUseWebFallback
    ? "数据包不可用时，可使用当前受控 Agent 环境已经提供的联网检索工具补充；每个补充事实必须附直接 URL 和时间。"
    : "本次禁止联网补充，只能使用附件数据包；缺失内容直接标记。";

  const publicationGate = publicationAudit?.passed ? "通过" : "阻断";
  const blockerLines = publicationAudit?.blockers?.length
    ? publicationAudit.blockers.map((item) => `  - ${item}`).join("\n")
    : "  - 无";
  const marketCoverage = publicationAudit?.marketCoverage
    ? `${publicationAudit.marketCoverage.obtained}/${publicationAudit.marketCoverage.required}`
    : "不可计算";
  const marketGapLines = publicationAudit?.marketCoverage?.gaps?.length
    ? publicationAudit.marketCoverage.gaps.map((item) => `  - ${item}`).join("\n")
    : "  - 无";

  return `# 本次运行指令

- 目标日期：${targetDate}
- 时区：${workflow.timezone}
- 数据载入路径：${loadedFrom}
- 数据包状态：${dataStatus}
- 固定执行器：${agentDescriptor?.executor ?? "程序事实编译"}
- 固定模型：${agentDescriptor ? `${agentDescriptor.provider}/${agentDescriptor.model}` : "未请求"}
- 固定数据截止（Asia/Shanghai）：${cutoffLocal}
- 固定数据截止（UTC）：${cutoffIso}
- 行情最大时效：${marketDataMaxAgeHours} 小时
- 新闻最大时效：${newsMaxAgeHours} 小时；超过阈值的新闻必须逐项标记 [陈旧]，且不得代表当前状态
- 必须标记陈旧行情：${requireStaleMarketLabel ? "是；标题区增加陈旧数据行，相关行情逐项加 [陈旧]" : "否；仍需逐项检查来源时间"}
- 发布级别：${publicationLevel}
- 程序发布门禁：${publicationGate}
- 正式发布必要项覆盖：${marketCoverage}（计算定义：三大指数组 1 项、沪深成交额与宽度 1 项、每只股票行情各 1 项；独立于数据桥模块评分；该比例不代表全部行情字段齐全）

程序发布阻断项：
${blockerLines}

正式发布必要项缺口：
${marketGapLines}

${webRule}

请严格遵守 agents/morning_report.md：

1. 只输出目标日期的最终 Markdown 晨报。
2. 标题区必须逐字写“发布级别：${publicationLevel}”。待核验报告不得表述为正式或已验证。
3. 不得把用户股票池中的研究标签写成已证实事实。
4. 未核验的数字、交易日状态、公告或新闻不得猜测。
5. 如果使用联网工具，优先一手来源；媒体报道与公司公告要分开表述。
6. 标题中的数据截止必须逐字写为 ${cutoffLocal}（Asia/Shanghai），不得自行读取或换算当前时间。
7. 发布时间、更新时间或数据时间晚于 ${cutoffLocal}（Asia/Shanghai）的来源一律删除。
8. 每条来源必须在同一行给出直接 URL、来源类型，以及完整时间或“时间未核验”。
9. 未来事件没有官方一手来源时必须删除；收盘后消息不得解释此前价格。
10. 数据时间晚于截止、来源不明、品种/行业/公司错配或口径冲突时，删除相关结论并列入数据缺口。
11. 报告末尾逐字包含免责声明：${disclaimer}
12. \`meta.dataCompleteness\` 只能称为“数据桥模块评分”，它衡量模块新鲜度与可用性，不代表字段齐全或数值正确；同时逐字披露上面的正式发布必要项覆盖及缺口。
13. 三大核心指数统一使用“指数名：收盘 X 点，前收 Y 点，涨跌 Z 点（P%）”格式；四个数字必须来自同一条数据记录并通过算术复核。
14. 每个行情、资金和新闻项目按自身 \`dataAsOf\`/\`publishedAt\` 与对应 18/72 小时阈值判断，不得把同一章节中的新鲜项目一并标成 [陈旧]。
15. 股票行情缺失时只能写“研究线索/待验证”，不得给出价格表现、涨跌原因、交易或仓位判断。
16. 缺少现金流字段时，不得宣称现金流条件已经触发，也不得判断现金流强弱或改善恶化。
`;
}

const CORE_INDEX_IDENTITIES = [
  { code: "sh000001", name: "上证指数" },
  { code: "sz399001", name: "深证成指" },
  { code: "sz399006", name: "创业板指" }
];
const STOCK_IDENTITY_FIELDS = Object.freeze(["code", "name", "symbol", "exchange"]);
const CORE_INDEX_UNITS = Object.freeze({
  close: "index_point",
  previousClose: "index_point",
  change: "index_point",
  changePercent: "percentage_point"
});
const WATCHLIST_MARKET_UNITS = Object.freeze({
  last: "CNY_per_share",
  previousClose: "CNY_per_share",
  change: "CNY_per_share",
  changePercent: "percentage_point"
});
const MARKET_SOURCE_TYPES = new Set(["market_data"]);
const CALENDAR_SOURCE_TYPES = new Set(["trading_calendar_data"]);

function isFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function isPositiveNumber(value) {
  return isFiniteNumber(value) && value > 0;
}

function isValidInstant(value) {
  return value != null && !Number.isNaN(new Date(value).getTime());
}

function hasExpectedUnits(record, expectedUnits) {
  return Object.entries(expectedUnits).every(
    ([field, expected]) => record?.units?.[field] === expected
  );
}

function instantDateInTimeZone(value, timezone = "Asia/Shanghai") {
  if (!isValidInstant(value)) return null;
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).format(new Date(value));
}

function arithmeticTolerance(expected, absolute = 0.02) {
  return Math.max(absolute, Math.abs(expected) * 0.000_01);
}

function valuesAgree(actual, expected, absolute = 0.02) {
  return Math.abs(actual - expected) <= arithmeticTolerance(expected, absolute);
}

function hasReferencedTimedSource(
  record,
  context,
  cutoffInstant,
  allowedSourceTypes = null
) {
  if (!Array.isArray(record?.sourceIds) || record.sourceIds.length === 0) return false;
  const byId = new Map((context?.sources ?? []).map((source) => [source?.id, source]));
  const recordInstant = isValidInstant(record?.dataAsOf)
    ? new Date(record.dataAsOf).getTime()
    : null;
  const validReferences = record.sourceIds.map((id) => {
    const source = byId.get(id);
    const instantValue = source?.publishedAt ?? source?.dataAsOf;
    if (
      !source?.url ||
      !isValidInstant(instantValue) ||
      (allowedSourceTypes && !allowedSourceTypes.has(source?.sourceType))
    ) return false;
    const sourceInstant = new Date(instantValue).getTime();
    return sourceInstant <= new Date(cutoffInstant).getTime() + 60_000 &&
      (recordInstant === null || Math.abs(sourceInstant - recordInstant) <= 60_000);
  });
  return allowedSourceTypes
    ? validReferences.every(Boolean)
    : validReferences.some(Boolean);
}

function declaredSourceIds(value) {
  if (!Array.isArray(value) || value.length === 0) return [];
  if (value.some((id) => typeof id !== "string" || id.trim() === "")) return [];
  if (new Set(value).size !== value.length) return [];
  return [...value];
}

function sameSourceIds(left, right) {
  return left.length === right.length && left.every((id) => right.includes(id));
}

function preciseAggregateSourceIds(session, kind) {
  const explicit = kind === "turnover"
    ? [session?.turnoverSourceIds, session?.turnoverByExchange?.sourceIds]
    : [session?.marketBreadth?.sourceIds, session?.marketBreadthSourceIds];
  const populated = explicit.map(declaredSourceIds).filter((ids) => ids.length > 0);
  if (populated.length > 1 && !sameSourceIds(populated[0], populated[1])) {
    return [];
  }
  if (populated.length > 0) return populated[0];
  const aggregateIds = declaredSourceIds(session?.sourceIds);
  if (kind === "breadth") {
    return aggregateIds.filter((id) => /(?:^|:)legulegu:market-activity(?::|$)/i.test(id));
  }
  const sse = aggregateIds.filter((id) => /(?:^|:)sse:daily-market-summary(?::|$)/i.test(id));
  const szse = aggregateIds.filter((id) => /(?:^|:)szse:daily-market-summary(?::|$)/i.test(id));
  return sse.length > 0 && szse.length > 0 ? [...sse, ...szse] : [];
}

function aggregateSourcesValid(session, kind, context, cutoffInstant) {
  return hasReferencedTimedSource({
    dataAsOf: session?.dataAsOf,
    sourceIds: preciseAggregateSourceIds(session, kind)
  }, context, cutoffInstant, MARKET_SOURCE_TYPES);
}

function validateMarketArithmetic(record, valueKey, label) {
  const issues = [];
  const current = record?.[valueKey];
  const previous = record?.previousClose;
  const change = record?.change;
  const changePercent = record?.changePercent;
  if (!isPositiveNumber(current)) issues.push(`${label} ${valueKey} 缺失或为非正数`);
  if (!isPositiveNumber(previous)) issues.push(`${label} previousClose 缺失或为非正数`);
  if (!isFiniteNumber(change)) issues.push(`${label} change 缺失`);
  if (!isFiniteNumber(changePercent)) issues.push(`${label} changePercent 缺失`);
  if (
    isPositiveNumber(current) &&
    isPositiveNumber(previous) &&
    isFiniteNumber(change) &&
    isFiniteNumber(changePercent)
  ) {
    const expectedChange = current - previous;
    const expectedPercent = expectedChange / previous * 100;
    if (!valuesAgree(change, expectedChange)) {
      issues.push(
        `${label} 涨跌额算术不一致：${current} - ${previous} 不等于 ${change}`
      );
    }
    if (!valuesAgree(changePercent, expectedPercent, 0.02)) {
      issues.push(
        `${label} 涨跌幅算术不一致：应约为 ${expectedPercent.toFixed(4)}%，实际为 ${changePercent}%`
      );
    }
  }
  return issues;
}

function validAggregateMarketFields(session) {
  const breadth = session?.marketBreadth;
  return isPositiveNumber(session?.totalTurnover) &&
    session?.units?.totalTurnover === "CNY" &&
    isFiniteNumber(breadth?.advancers) &&
    isFiniteNumber(breadth?.decliners) &&
    isFiniteNumber(breadth?.unchanged) &&
    breadth.advancers >= 0 &&
    breadth.decliners >= 0 &&
    breadth.unchanged >= 0 &&
    breadth.advancers + breadth.decliners > 0;
}

function hasShSzOnlyTurnoverScope(session) {
  const byExchange = session?.turnoverByExchange;
  if (!byExchange || typeof byExchange !== "object" || Array.isArray(byExchange)) return false;
  const exchanges = Object.keys(byExchange).sort();
  if (exchanges.length !== 2 || exchanges[0] !== "SH" || exchanges[1] !== "SZ") return false;
  if (!isPositiveNumber(byExchange.SH) || !isPositiveNumber(byExchange.SZ)) return false;
  return isPositiveNumber(session?.totalTurnover) &&
    valuesAgree(session.totalTurnover, byExchange.SH + byExchange.SZ);
}

function watchlistMarketIdentityIssues(item) {
  const issues = [];
  for (const field of STOCK_IDENTITY_FIELDS) {
    if (typeof item?.[field] !== "string" || item[field].trim() === "") {
      issues.push(`父项 ${field} 缺失`);
    }
  }
  const market = item?.marketData;
  if (market === null || market === undefined) return issues;
  if (typeof market !== "object" || Array.isArray(market)) {
    issues.push("marketData 必须是对象");
    return issues;
  }
  for (const field of ["code", "name"]) {
    if (typeof market[field] !== "string" || market[field].trim() === "") {
      issues.push(`marketData.${field} 缺失`);
    } else if (market[field] !== item?.[field]) {
      issues.push(`marketData.${field} 与父项不一致`);
    }
  }
  for (const field of ["symbol", "exchange"]) {
    if (Object.hasOwn(market, field) && market[field] !== item?.[field]) {
      issues.push(`marketData.${field} 与父项不一致`);
    }
  }
  return issues;
}

function validWatchlistMarketData(
  item,
  context,
  cutoffInstant,
  { enforceIdentity = false } = {}
) {
  const market = item?.marketData;
  if (!market || item?.status === "unavailable") return false;
  if (enforceIdentity && watchlistMarketIdentityIssues(item).length > 0) return false;
  const expectedDate = context?.data?.tradingCalendar?.previousTradingDate;
  const timezone = context?.meta?.timezone ?? "Asia/Shanghai";
  return validateMarketArithmetic(market, "last", item?.name ?? item?.code ?? "股票").length === 0 &&
    hasExpectedUnits(market, WATCHLIST_MARKET_UNITS) &&
    isValidInstant(market.dataAsOf) &&
    (!expectedDate || instantDateInTimeZone(market.dataAsOf, timezone) === expectedDate) &&
    new Date(market.dataAsOf).getTime() <= new Date(cutoffInstant).getTime() + 60_000 &&
    hasReferencedTimedSource(market, context, cutoffInstant, MARKET_SOURCE_TYPES);
}

/**
 * Separates a syntactically valid data packet from one that is safe to label
 * as a formal report. Module scores alone are deliberately not used here.
 */
export function assessPublicationGate({
  context,
  cutoffInstant,
  targetDate = context?.meta?.targetDate
}) {
  const blockers = [];
  const marketGaps = [];
  const cutoff = new Date(cutoffInstant);
  if (Number.isNaN(cutoff.getTime())) {
    throw new Error("发布门禁缺少有效 cutoffInstant");
  }
  if (!["live", "partial"].includes(context?.meta?.status)) {
    blockers.push("数据包不是 live/partial，不能正式发布");
  }
  if (context?.data?.tradingCalendar?.status !== "trading_day") {
    blockers.push("目标日期未被交易日历确认为交易日");
  }
  if (!hasReferencedTimedSource(
    context?.data?.tradingCalendar,
    context,
    cutoff,
    CALENDAR_SOURCE_TYPES
  )) {
    blockers.push("交易日历缺少截止前且类型正确的可核验来源");
  }

  const session = context?.data?.previousAShareSession;
  const expectedPreviousDate = context?.data?.tradingCalendar?.previousTradingDate;
  const timezone = context?.meta?.timezone ?? "Asia/Shanghai";
  if (!session) {
    blockers.push("缺少上一交易日 A 股行情");
  } else {
    if (!expectedPreviousDate || session.sessionDate !== expectedPreviousDate) {
      blockers.push(
        `上一交易日日期不一致：交易日历为 ${expectedPreviousDate ?? "缺失"}，行情为 ${session.sessionDate ?? "缺失"}`
      );
    }
    if (targetDate && session.sessionDate >= targetDate) {
      blockers.push("上一交易日日期必须早于目标日期");
    }
    if (!isValidInstant(session.dataAsOf) || new Date(session.dataAsOf).getTime() > cutoff.getTime() + 60_000) {
      blockers.push("上一交易日行情 dataAsOf 缺失、无效或晚于固定截止");
    } else if (expectedPreviousDate && instantDateInTimeZone(session.dataAsOf, timezone) !== expectedPreviousDate) {
      blockers.push("上一交易日行情 dataAsOf 日期与交易日历不一致");
    }
  }

  let coreIndexGroupValid = Boolean(session);
  for (const identity of CORE_INDEX_IDENTITIES) {
    const identityMatches = (session?.indices ?? []).filter(
      (item) => item?.code === identity.code || item?.name === identity.name
    );
    const index = identityMatches.find(
      (item) => item?.code === identity.code && item?.name === identity.name
    );
    if (identityMatches.length !== 1 || !index) {
      blockers.push(`核心指数身份缺失、重复或交叉错配：${identity.name}`);
      coreIndexGroupValid = false;
      continue;
    }
    const issues = validateMarketArithmetic(index, "close", identity.name);
    if (!hasExpectedUnits(index, CORE_INDEX_UNITS)) {
      issues.push(`${identity.name} 行情单位缺失或不一致`);
    }
    if (!isValidInstant(index.dataAsOf) || new Date(index.dataAsOf).getTime() > cutoff.getTime() + 60_000) {
      issues.push(`${identity.name} dataAsOf 缺失、无效或晚于固定截止`);
    } else if (expectedPreviousDate && instantDateInTimeZone(index.dataAsOf, timezone) !== expectedPreviousDate) {
      issues.push(`${identity.name} dataAsOf 日期与上一交易日不一致`);
    }
    const priceRecord = Object.hasOwn(index, "priceSourceIds")
      ? { ...index, sourceIds: declaredSourceIds(index.priceSourceIds) }
      : index;
    if (!hasReferencedTimedSource(priceRecord, context, cutoff, MARKET_SOURCE_TYPES)) {
      issues.push(`${identity.name} 缺少截止前可核验的来源与来源时间`);
    }
    if (issues.length > 0) coreIndexGroupValid = false;
    blockers.push(...issues);
  }

  if (isPositiveNumber(session?.totalTurnover) && session?.units?.totalTurnover !== "CNY") {
    blockers.push("沪深两市成交额单位缺失或不一致");
  }
  const aggregateValuesValid = validAggregateMarketFields(session);
  const turnoverSourcesValid = aggregateSourcesValid(session, "turnover", context, cutoff);
  const breadthSourcesValid = aggregateSourcesValid(session, "breadth", context, cutoff);
  if (aggregateValuesValid && !turnoverSourcesValid) {
    blockers.push("沪深两市成交额缺少截止前且类型正确的行情来源");
  }
  if (aggregateValuesValid && !breadthSourcesValid) {
    blockers.push("市场宽度缺少截止前且类型正确的行情来源");
  }
  const aggregateGroupValid = aggregateValuesValid && turnoverSourcesValid && breadthSourcesValid;
  if (!aggregateGroupValid) {
    marketGaps.push("沪深两市成交额或市场宽度缺失/异常");
  }

  const watchlist = Array.isArray(context?.data?.watchlist)
    ? context.data.watchlist
    : [];
  if (watchlist.length === 0) marketGaps.push("股票池行情为空");
  const watchCoverage = watchlist.map((item) => {
    const identityIssues = watchlistMarketIdentityIssues(item);
    if (identityIssues.length > 0) {
      blockers.push(
        `${item?.code ?? "未知代码"} ${item?.name ?? "未知股票"} 股票身份不一致：` +
        identityIssues.join("；")
      );
    }
    if (
      item?.status !== "unavailable" &&
      item?.marketData &&
      !hasExpectedUnits(item.marketData, WATCHLIST_MARKET_UNITS)
    ) {
      blockers.push(
        `${item?.code ?? "未知代码"} ${item?.name ?? "未知股票"} 行情单位缺失或不一致`
      );
    }
    if (
      item?.status !== "unavailable" &&
      item?.marketData &&
      !hasReferencedTimedSource(item.marketData, context, cutoff, MARKET_SOURCE_TYPES)
    ) {
      blockers.push(
        `${item?.code ?? "未知代码"} ${item?.name ?? "未知股票"} ` +
        "行情缺少截止前且类型正确的行情来源"
      );
    }
    const valid = identityIssues.length === 0 && validWatchlistMarketData(
      item,
      context,
      cutoff,
      { enforceIdentity: true }
    );
    if (!valid) marketGaps.push(`${item?.code ?? "未知代码"} ${item?.name ?? "未知股票"} 上一交易日行情不可核验`);
    return valid;
  });

  const required = 2 + watchlist.length;
  const obtained = Number(coreIndexGroupValid) + Number(aggregateGroupValid) +
    watchCoverage.filter(Boolean).length;
  return {
    passed: blockers.length === 0,
    blockers,
    marketCoverage: {
      obtained,
      required,
      score: required > 0 ? Math.round(obtained / required * 100) : 0,
      gaps: marketGaps
    }
  };
}

export function requiresStaleMarketLabel({
  context,
  cutoffInstant,
  marketDataMaxAgeHours
}) {
  const dataAsOf = context?.data?.previousAShareSession?.dataAsOf;
  if (!dataAsOf) return false;
  const dataInstant = new Date(dataAsOf);
  const cutoff = new Date(cutoffInstant);
  if (Number.isNaN(dataInstant.getTime()) || Number.isNaN(cutoff.getTime())) {
    throw new Error("无法根据 previousAShareSession.dataAsOf 判断行情时效");
  }
  const ageHours = (cutoff.getTime() - dataInstant.getTime()) / 3_600_000;
  return ageHours > marketDataMaxAgeHours;
}

// Kept as a compatibility export for existing callers. The decision is no
// longer tied to weekends; it is based on the actual previous-session timestamp.
export const requiresWeekendStaleLabel = requiresStaleMarketLabel;

function validateSourceTimes(report, cutoffInstant, context) {
  const sourceStart = report.lastIndexOf("## 来源");
  const sourceSection = sourceStart >= 0 ? report.slice(sourceStart) : "";
  const sourceLines = sourceSection
    .split(/\r?\n/)
    .filter((line) => /https?:\/\//i.test(line));

  for (const line of sourceLines) {
    if (!/^\s*\d+\.\s+/.test(line)) {
      throw new Error("来源 URL 必须放在编号来源清单的单行条目中");
    }
    const hasTimestamp =
      /(?:发布时间|更新时间|数据时间|实际采集时间|实际查询完成时间)\s*[：:]?\s*20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}/.test(line);
    if (!hasTimestamp && !line.includes("时间未核验")) {
      throw new Error("每条来源必须包含完整 Asia/Shanghai 时间或“时间未核验”");
    }

    for (const match of line.matchAll(/(20\d{2}-\d{2}-\d{2})\s+(\d{2}:\d{2})/g)) {
      const sourceInstant = new Date(`${match[1]}T${match[2]}:00+08:00`);
      if (sourceInstant.getTime() > new Date(cutoffInstant).getTime() + 60_000) {
        const provenDateOnlyObservation = (context?.sources ?? []).some((source) =>
          source.sourceType === "official_announcement" && source.timePrecision === "date" &&
          !source.publishedAt && line.includes(source.title) && line.includes(source.url) &&
          match[0] === formatSourceInstant(source.dataAsOf, "Asia/Shanghai") &&
          line.includes(`实际采集时间：${formatSourceInstant(source.dataAsOf, "Asia/Shanghai")}`) &&
          sourceEvidenceInstant(source) && Date.parse(sourceEvidenceInstant(source)) <= new Date(cutoffInstant).getTime());
        if (!provenDateOnlyObservation) throw new Error(`来源时间晚于固定截止时间: ${match[0]}`);
      }
    }
  }
}

function mergeSourceUrlLines(report) {
  const lines = report.split(/\r?\n/);
  const merged = [];
  let inSources = false;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (/^##\s+来源\s*$/.test(line.trim())) inSources = true;
    const next = lines[index + 1]?.trim();
    if (
      inSources &&
      /^\s*\d+\.\s+/.test(line) &&
      /^https?:\/\/\S+$/i.test(next ?? "")
    ) {
      merged.push(`${line.trimEnd()} — ${next}`);
      index += 1;
      continue;
    }
    merged.push(line);
  }
  return merged.join("\n");
}

function formatSourceInstant(value, timezone) {
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return "时间未核验";
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

function normalizeMissingSourceAppendixUrls(
  report,
  context,
  strictContextSources,
  cutoffInstant,
  marketDataMaxAgeHours,
  newsMaxAgeHours
) {
  if (!strictContextSources || !Array.isArray(context?.sources)) return report;
  if (report.includes(
    "> 生成方式：程序核验基础版"
  )) {
    // 结构化稿的正文引用编号由编译器确定；不得用全量数据包重排编号。
    return report;
  }
  const sourceStart = report.lastIndexOf("## 来源");
  if (sourceStart < 0) return report;
  const sourceSection = report.slice(sourceStart);
  const numberedLines = sourceSection
    .split(/\r?\n/)
    .filter((line) => /^\s*\d+\.\s+/.test(line));
  if (numberedLines.length === 0 || numberedLines.some((line) => /https?:\/\//i.test(line))) {
    return report;
  }

  const seen = new Set();
  const sources = context.sources.filter((source) => {
    if (!source?.title || !source?.url) return false;
    const key = source.id ?? `${source.title}|${source.url}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const timezone = context?.meta?.timezone ?? "Asia/Shanghai";
  const canonicalLines = sources.map((source, index) => {
    const title = String(source.title).replace(/\s+/g, " ").trim();
    let identity = source.sourceType ?? "unknown_source_type";
    if (source.sourceType === "media_aggregation") {
      const origin = source.originalPublisher
        ? `转引${source.originalPublisher}`
        : "原始发布方未核验";
      identity += `（${source.aggregator ?? "东方财富"}聚合，${origin}）`;
    } else if (source.publisher) {
      identity += `（${source.publisher}）`;
    }
    const instantValue = source.publishedAt ?? source.dataAsOf;
    const timeLabel = source.publishedAt ? "发布时间" : "数据时间";
    const displayTime = formatSourceInstant(instantValue, timezone);
    const timezoneSuffix = displayTime === "时间未核验" ? "" : `（${timezone}）`;
    const freshness = classifySourceFreshness(source, {
      cutoffInstant,
      marketDataMaxAgeHours,
      newsMaxAgeHours
    });
    const staleLabel = freshness.tracked && freshness.stale ? "[陈旧] " : "";
    return `${index + 1}. ${staleLabel}${title} — ${identity}；${timeLabel}：${displayTime}${timezoneSuffix}；${source.url}`;
  });
  const body = report.slice(0, sourceStart).trimEnd();
  return `${body}\n\n## 来源\n\n> 以下为本次固定数据包的完整来源清单；正文仅使用其中可核验内容。\n\n${canonicalLines.join("\n")}`;
}

function normalizeCoverageTerminology(report, publicationAudit) {
  const sectionMatch = report.match(/##\s+1\.[\s\S]*?(?=\n##\s+2\.|$)/);
  if (!sectionMatch) return report;
  const original = sectionMatch[0];
  const isCoverageSection = /数据完整度|数据覆盖|\|\s*类别\s*\|\s*完整度/.test(original);
  if (!isCoverageSection) return report;
  let normalized = original
    .replace(/(##\s+1\.)\s*数据完整度/g, "$1 数据覆盖与可用性")
    .replace(/\|\s*类别\s*\|\s*完整度\s*\|/g, "| 类别 | 数据桥模块评分（非准确性） |")
    .replace(/(行情|资金|新闻|财务)完整度/g, "$1数据桥模块评分")
    .replace(/行情关键字段覆盖/g, "正式发布必要项覆盖");
  const notes = [];
  if (!/模块评分[^\n]{0,40}不代表[^\n]{0,20}(?:正确|准确|字段齐全)/.test(normalized)) {
    notes.push("> 数据桥模块评分只衡量模块新鲜度与可用性，不代表字段齐全或数值准确。");
  }
  if (publicationAudit?.marketCoverage) {
    const { obtained, required, gaps } = publicationAudit.marketCoverage;
    const gapText = gaps.length > 0 ? gaps.join("；") : "无";
    if (!/门禁必要项/.test(normalized)) {
      const coverageLine = `> 门禁必要项：${obtained}/${required}；缺口：${gapText}`;
      const existingCoverageLine = /^(?:>\s*)?正式发布必要项覆盖[：:][^\n]*$/m;
      if (existingCoverageLine.test(normalized)) {
        normalized = normalized.replace(existingCoverageLine, coverageLine);
      } else {
        notes.push(coverageLine);
      }
    }
  }
  if (notes.length > 0) {
    const headingEnd = normalized.indexOf("\n");
    normalized = headingEnd >= 0
      ? normalized.slice(0, headingEnd) + "\n\n" + notes.join("\n") + normalized.slice(headingEnd)
      : normalized + "\n\n" + notes.join("\n");
  }
  return report.replace(original, normalized);
}

function validateForbiddenTechnicalLevels(report) {
  const forbidden = [
    /突破前高/,
    /守住\s*\d+(?:\.\d+)?\s*点/,
    /支撑位/,
    /压力位/,
    /目标价/,
    /止损价/
  ];
  for (const pattern of forbidden) {
    if (pattern.test(report)) {
      throw new Error("报告包含无计算方法的技术位或价格指令");
    }
  }
}

function validateForbiddenScenarioThresholds(report) {
  const sourceStart = report.lastIndexOf("## 来源");
  const body = sourceStart >= 0 ? report.slice(0, sourceStart) : report;
  let section = null;
  for (const originalLine of body.split(/\r?\n/)) {
    const line = originalLine.trim();
    const heading = line.match(/^##\s+(\d+)\./);
    if (heading) section = Number(heading[1]);
    const isPlanningLine =
      section === 6 ||
      section === 7 ||
      line.includes("[情景]") ||
      /今日观察条件/.test(line);
    if (!isPlanningLine) continue;
    const hasConditionalLanguage =
      /(?:情景|若|如果|是否|能否|观察|维持|突破|回落|高于|低于|以上|以下|附近|区间|阈值)/.test(line);
    const hasMarketTerm =
      /(?:股价|收盘|指数|上证|深证|创业板|成交额|成交量|换手|振幅|点位)/.test(line);
    const hasNumericMarketValue =
      /\d[\d,]*(?:\.\d+)?(?:\s*[-—至~]\s*\d[\d,]*(?:\.\d+)?)?\s*(?:%|％|点|亿元|万亿元|元|万股|万手|手)/.test(line);
    if (hasConditionalLanguage && hasMarketTerm && hasNumericMarketValue) {
      throw new Error("报告包含未配置或无计算依据的情景阈值: " + line.slice(0, 80));
    }
  }
}

function validateNorthboundUnavailableSemantics(report, context) {
  const northbound = context?.data?.capitalFlows?.find(
    (item) => item?.type === "northbound_realtime"
  );
  if (northbound?.status !== "unavailable") return;
  if (!report.includes("公开披露口径不再提供实时净流入")) {
    throw new Error("北向实时口径必须写明“公开披露口径不再提供实时净流入”（数据说明，不作为每日故障）");
  }
  if (report.includes("北向资金实时净流入不可用")) {
    throw new Error("北向实时口径不得再写成“实时净流入不可用”的每日故障");
  }
  for (const originalLine of report.split(/\r?\n/)) {
    if (!originalLine.includes("北向")) continue;
    const remainder = originalLine.replaceAll("公开披露口径不再提供实时净流入", "");
    if (/(?:恢复交易|实际流向|收盘后.*验证|连续.*净流出|大幅净流入|大幅净流出)/.test(remainder)) {
      throw new Error(
        "报告把不可用的北向实时口径写成了交易情景: " + originalLine.trim().slice(0, 80)
      );
    }
  }
}

function validateDragonTigerSemantics(report, context) {
  const hasDragonTiger = context?.data?.capitalFlows?.some(
    (item) => item?.type === "dragon_tiger_list"
  );
  if (!hasDragonTiger) return;
  for (const originalLine of report.split(/\r?\n/)) {
    if (/机构净(?:买入|卖出)[^\n]{0,30}\d/.test(originalLine)) {
      throw new Error(
        "龙虎榜净买额不得改写成机构净买入/卖出金额: " + originalLine.trim().slice(0, 80)
      );
    }
  }
}

function validateMediaCannotConfirm(report, context) {
  const officialAnnouncementsUnavailable = context?.meta?.warnings?.some(
    (warning) => /官方公告.*不可用/.test(warning)
  );
  if (!officialAnnouncementsUnavailable) return;
  const sourceStart = report.lastIndexOf("## 来源");
  const body = sourceStart >= 0 ? report.slice(0, sourceStart) : report;
  const forbidden = [
    /股东会[^。；\n]{0,16}(?:审议)?通过/,
    /(?:已经|已)(?:正式|实质性)?终止/,
    /正式终止/,
    /(?:已经|已)生效/
  ];
  for (const originalLine of body.split(/\r?\n/)) {
    const line = originalLine.trim();
    let previousClauseIsObservationLimit = false;
    for (const clause of line.split(/[。；，,]/).map((value) => value.trim()).filter(Boolean)) {
      const isConditionalOrUncertain =
        /(?:若|如果|一旦|则|可能|或将|拟|提议|计划|待核验|待审议|尚未|未确认|需等待|风险|条件)/.test(clause);
      const announcementAbsenceMatch = clause.match(
        /(?:(?:未发现|未见|没有|不存在|未查到|未检索到|未搜索到|未查询到|尚未发布|未发布|无(?!法|需))[^。；\n]{0,24}(?:官方|公司)?公告|(?:相关\s*)?(?:官方|公司)?公告\s*(?:未发现|未见|未查到|未检索到|未搜索到|未查询到)(?=$|\s|[（(])|(?:新增\s*)?(?:官方|公司)?公告\s*[：:]?\s*(?:0\s*条|零条|无)(?=$|\s|[（(])|(?:官方|公司)?公告\s*(?:数量\s*[：:]?\s*|共\s*)(?:0|零)\s*(?:条)?(?=$|\s|[（(])|(?:官方|公司)?公告列表\s*[：:]?\s*(?:为空|空|无)(?=$|\s|[（(])|(?:盘前|今日|当日)(?:官方|公司)?公告\s*[：:]?\s*(?:暂无|为空)(?=$|\s|[（(]))/
      );
      const observationLimitPrefix = announcementAbsenceMatch
        ? clause.slice(0, announcementAbsenceMatch.index)
        : "";
      const hasGoverningObservationLimit =
        previousClauseIsObservationLimit ||
        /(?:(?:无法|未能|不能)[^。；，,\n]{0,8}(?:检索|搜索|查询|确认|核验)|不可(?:检索|搜索|查询|确认|核验)(?:或核验)?|待核验|(?:当前)?数据包(?:中|内)?)[^。；，,\n]{0,16}$/.test(observationLimitPrefix);
      const clauseIsObservationLimit =
        /(?:(?:当前)?数据包|(?:官方)?公告?来源|公告源|源)[^。；，,\n]{0,20}(?:无法|未能|不能|不可)[^。；，,\n]{0,8}(?:检索|搜索|查询|确认|核验)|(?:无法|未能|不能|不可)[^。；，,\n]{0,8}(?:检索|搜索|查询|确认|核验)[^。；，,\n]{0,12}(?:公告|来源|数据包)/.test(clause);
      const logicInvalidClaim =
        /逻辑(?:状态)?[^。；\n]{0,16}(?:失效|已终止)/.test(clause);
      const isLabelOnly =
        /^(?:#+\s*)?(?:[-*]\s*)?(?:\*{1,2})?逻辑(?:状态)?(?:失效|已终止)(?:\*{1,2})?[：:]?$/.test(clause);
      const definiteOfficialEvent = forbidden.some((pattern) => pattern.test(clause));
      if (announcementAbsenceMatch && !hasGoverningObservationLimit) {
        throw new Error(
          "官方公告源不可用时，不得把无法观测公告写成公告不存在: " + line.slice(0, 80)
        );
      }
      if (
        (definiteOfficialEvent && !isConditionalOrUncertain) ||
        (logicInvalidClaim && !isConditionalOrUncertain && !isLabelOnly)
      ) {
        throw new Error(
          "官方公告源不可用时，媒体聚合内容不得确认公司事项已生效: " + line.slice(0, 80)
        );
      }
      previousClauseIsObservationLimit = clauseIsObservationLimit;
    }
  }
}

function validateTurnoverScopeTerminology(report, context) {
  const session = context?.data?.previousAShareSession;
  if (!hasShSzOnlyTurnoverScope(session)) return;
  const sourceStart = report.lastIndexOf("## 来源");
  const body = sourceStart >= 0 ? report.slice(0, sourceStart) : report;
  if (/全市场\s*成交额/.test(body)) {
    throw new Error(
      "固定数据包成交额仅含沪深两市时，正文必须使用“沪深两市成交额”或“沪深 A 股成交额”，不得称“全市场成交额”"
    );
  }
}

function validateNoInventedStockCompleteness(report) {
  const stockSection = report.match(/##\s+4\.[\s\S]*?(?=\n##\s+5\.|$)/)?.[0] ?? "";
  for (const line of stockSection.split(/\r?\n/)) {
    const hasExplicitCompletenessPercentage =
      /(?:证据完整度|证据字段覆盖|总体|整体|行情|资金|新闻|财务)\s*(?:为|达到|约为|[：:])?\s*\d+(?:\.\d+)?\s*[%％]/.test(line);
    if (/(?:证据完整度|证据字段覆盖)/.test(line) && hasExplicitCompletenessPercentage) {
      throw new Error("单股证据字段不得使用未配置算法的覆盖百分比: " + line.trim().slice(0, 80));
    }
  }
}

function validateNumericComparisonSemantics(report) {
  const sourceStart = report.lastIndexOf("## 来源");
  const body = sourceStart >= 0 ? report.slice(0, sourceStart) : report;
  for (const originalLine of body.split(/\r?\n/)) {
    for (const comparison of originalLine.matchAll(/(?:低于|弱于|高于|强于)/g)) {
      const before = originalLine.slice(0, comparison.index);
      const after = originalLine.slice(comparison.index + comparison[0].length);
      const leftValues = [...before.matchAll(/([+-]?\d+(?:\.\d+)?)\s*[%％]/g)];
      const rightValue = after.match(/([+-]?\d+(?:\.\d+)?)\s*[%％]/);
      if (leftValues.length === 0 || !rightValue) continue;
      const left = Number(leftValues.at(-1)[1]);
      const right = Number(rightValue[1]);
      const claimsLower = /(?:低于|弱于)/.test(comparison[0]);
      if ((claimsLower && left >= right) || (!claimsLower && left <= right)) {
        throw new Error(
          "涨跌幅比较与数值大小矛盾: " + originalLine.trim().slice(0, 100)
        );
      }
    }
  }
}

function validatePreviousSessionDates(report, context) {
  const asOf = context?.data?.previousAShareSession?.dataAsOf;
  if (!asOf) return;
  const instant = new Date(asOf);
  if (Number.isNaN(instant.getTime())) return;
  const expectedDate = new Intl.DateTimeFormat("en-CA", {
    timeZone: context?.meta?.timezone ?? "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).format(instant);
  const sourceStart = report.lastIndexOf("## 来源");
  const body = sourceStart >= 0 ? report.slice(0, sourceStart) : report;
  for (const originalLine of body.split(/\r?\n/)) {
    for (const clause of originalLine.split(/[。；]/)) {
      const previousSessionMentions = [...clause.matchAll(/上一交易日/g)];
      if (previousSessionMentions.length === 0) continue;
      for (const match of clause.matchAll(/\b(20\d{2}-\d{2}-\d{2})\b/g)) {
        const governingMention = previousSessionMentions
          .filter((mention) => mention.index < match.index)
          .at(-1);
        const followingMention = previousSessionMentions.find(
          (mention) => mention.index > match.index
        );
        const dateDefinesFollowingMention = followingMention
          ? /^\s*(?:为|是|系|即|作为)?\s*$/.test(
            clause.slice(match.index + match[0].length, followingMention.index)
          )
          : false;
        if (!governingMention && !dateDefinesFollowingMention) continue;
        if (dateDefinesFollowingMention) {
          if (match[1] !== expectedDate) {
            throw new Error(
              `“上一交易日”证据日期应为 ${expectedDate}，不能写成 ${match[1]}: ` +
              originalLine.trim().slice(0, 100)
            );
          }
          continue;
        }
        const commaIndex = Math.max(
          clause.lastIndexOf("，", match.index),
          clause.lastIndexOf(",", match.index)
        );
        const independentDateScope = commaIndex > governingMention.index
          ? clause.slice(commaIndex + 1, match.index)
          : "";
        if (
          /(?:财务|财报|报告期|年报|半年报|中报|季报|业绩|营收|净利润|现金流|发布时间|报道时间|新闻(?:时间|日期|发布日期|发布时间)|公告(?:披露|发布)(?:时间|日期|日)?|事件日|隔夜(?:美股|全球)|美股(?:指数|市场)|海外市场|全球市场)/.test(
            independentDateScope
          )
        ) continue;
        if (match[1] !== expectedDate) {
          throw new Error(
            `“上一交易日”证据日期应为 ${expectedDate}，不能写成 ${match[1]}: ` +
            originalLine.trim().slice(0, 100)
          );
        }
      }
    }
  }
}

function normalizeUrl(value) {
  try {
    const url = new URL(value);
    url.hash = "";
    if (url.protocol === "http:" || url.protocol === "https:") {
      return `${url.host.toLowerCase()}${url.pathname}${url.search}`.replace(/\/$/, "");
    }
    return url.toString().replace(/\/$/, "");
  } catch {
    return value;
  }
}

function validateStructuredEvidenceSourceFreshness(report, freshnessBySourceNumber, context) {
  const baselineGenerationLine = "> 生成方式：程序核验基础版";
  const structuredGenerationLine =
    "> 生成方式：程序核验基础版 + 结构化分析覆盖层";
  const generationLines = report
    .split(/\r?\n/)
    .filter((line) => /^>\s*生成方式[：:]/.test(line));
  if (generationLines.length > 1) {
    throw new Error("可发布稿只能声明一种程序生成方式");
  }
  const generationLine = generationLines[0] ?? null;
  const deterministicBaseline = generationLine === baselineGenerationLine;
  const deterministicStructured = generationLine === structuredGenerationLine;
  const isStructuredArtifactLine = (line) => {
    const withoutAllowedEmphasis = line.replaceAll("**", "").replaceAll("__", "");
    return line.includes("[推断]") ||
      /^\s{2}-\s+(?:支持证据|反证)[：:]/.test(withoutAllowedEmphasis);
  };
  const hasStructuredArtifacts = report
    .split(/\r?\n/)
    .some(isStructuredArtifactLine);
  const usesDataBriefLevel = report.includes(
    `> 发布级别：${DATA_BRIEF_PUBLICATION_LEVEL}`
  );
  if (hasStructuredArtifacts && !deterministicStructured) {
    throw new Error("含结构化推断或证据行的报告必须使用结构化覆盖层生成方式并通过语义白名单");
  }
  if (usesDataBriefLevel && !deterministicBaseline && !deterministicStructured) {
    throw new Error("基础数据快报必须使用程序固定的生成方式声明");
  }
  if (!deterministicStructured) return [];

  const section6Headers = [...report.matchAll(/^##\s+6\.[^\n]*$/gm)];
  const section7Headers = [...report.matchAll(/^##\s+7\.[^\n]*$/gm)];
  if (
    section6Headers.length !== 1 ||
    section7Headers.length !== 1 ||
    section6Headers[0][0] !== "## 6. 结构化分析覆盖层" ||
    section7Headers[0][0] !== "## 7. 风险提示与数据缺口" ||
    section7Headers[0].index <= section6Headers[0].index
  ) {
    throw new Error("结构化增强稿的第 6、7 节必须各唯一且使用固定标题顺序");
  }

  const section = report.slice(
    section6Headers[0].index + section6Headers[0][0].length,
    section7Headers[0].index
  );
  const outsideStructuredSection =
    report.slice(0, section6Headers[0].index) +
    report.slice(section7Headers[0].index);
  if (outsideStructuredSection
    .split(/\r?\n/)
    .some(isStructuredArtifactLine)) {
    throw new Error("结构化推断与支持反证行只能出现在固定的第 6 节");
  }
  let currentBlock = null;
  let blockCount = 0;
  let previousRoleRank = -1;
  const seenStockCodes = new Set();
  const watchlistCodes = new Set((context?.data?.watchlist ?? []).map((item) => String(item?.code ?? "")));
  const roleCounts = new Map();
  const semanticCandidates = buildStructuredAnalysisCandidates(context);
  const usedSemanticCandidateIds = new Set();
  const structuredBlocks = [];
  const stockEvidenceByCode = new Map();
  const addRecordSourceIds = (target, record) => {
    for (const id of Array.isArray(record?.sourceIds) ? record.sourceIds : []) {
      if (typeof id === "string") target.add(id);
    }
  };
  for (const stock of context?.data?.watchlist ?? []) {
    const code = String(stock?.code ?? "");
    if (!code) continue;
    const ids = new Set();
    addRecordSourceIds(ids, stock);
    addRecordSourceIds(ids, stock?.marketData);
    addRecordSourceIds(ids, stock?.financials);
    stockEvidenceByCode.set(code, ids);
  }
  for (const item of context?.data?.announcementsAndNews ?? []) {
    const code = String(item?.code ?? "");
    if (stockEvidenceByCode.has(code)) addRecordSourceIds(stockEvidenceByCode.get(code), item);
  }

  const finishBlock = () => {
    if (!currentBlock) return;
    if (currentBlock.supportCount < 1) {
      throw new Error("结构化分析的每个推断必须至少有一条支持证据");
    }
    const allowsAbsentCounter = new Set([
      "上涨家数占优且成交额可核验，仍需继续核验",
      "下跌家数占优且成交额可核验，仍需继续核验",
      "单日行情与报告期利润同比方向不同，期间不同，不足以认定同期背离，仍需继续核验"
    ]).has(currentBlock.text);
    if (allowsAbsentCounter) {
      if (currentBlock.counterCount !== 0 || currentBlock.inferenceLimit !== true) {
        throw new Error("未主张反证的推断不得把来源标成反证，且必须声明推断限制不否定已核事实");
      }
    } else if (currentBlock.role !== "风险" && currentBlock.counterCount < 1) {
      throw new Error("结构化分析的摘要、主题、个股与情景必须至少有一条反证");
    }
    if (
      currentBlock.role === "风险" &&
      currentBlock.counterCount === 0 &&
      currentBlock.placeholderCount !== 1
    ) {
      throw new Error("结构化分析的风险推断必须有反证或固定无反证占位");
    }
    if (currentBlock.placeholderCount > 0 && !allowsAbsentCounter && (
      currentBlock.role !== "风险" ||
      currentBlock.placeholderCount !== 1 ||
      currentBlock.counterCount !== 0
    )) {
      throw new Error("固定无反证占位只能在风险推断中单独使用");
    }
    if (currentBlock.supportCount > 6 || currentBlock.counterCount > 6) {
      throw new Error("结构化分析的单个推断最多各引用六条支持与反证来源");
    }
    const supportIds = [...currentBlock.supportSourceIds].toSorted();
    const counterIds = [...currentBlock.counterSourceIds].toSorted();
    structuredBlocks.push({
      kind: currentBlock.textRole,
      title: currentBlock.themeTitle,
      code: currentBlock.stockCode,
      text: currentBlock.text,
      evidenceIds: supportIds,
      counterEvidenceIds: counterIds,
      supportRoles: currentBlock.supportRoles,
      periodNote: currentBlock.periodNote
    });
  };

  for (const line of section.split(/\r?\n/)) {
    if (line === "") continue;
    if (/^-\s+\[推断\]\s+/.test(line)) {
      finishBlock();
      if (line.includes("[陈旧]") || line.includes("[来源")) {
        throw new Error("结构化分析推断父行不得包含来源或陈旧标记");
      }
      let role = null;
      let textRole = null;
      let text = null;
      let roleRank = -1;
      let stockCode = null;
      let themeTitle = null;
      let match = line.match(/^- \[推断\] (摘要|情景|风险)：(.+)$/);
      if (match) {
        role = match[1];
        textRole = ({ "摘要": "summary", "情景": "scenario", "风险": "risk" })[role];
        roleRank = ({ "摘要": 0, "情景": 2, "风险": 3 })[role];
        text = match[2];
      } else {
        match = line.match(/^- \[推断\] 股票 ([0-9]{6})：(.+)$/);
        if (match) {
          role = "股票";
          textRole = "stock";
          roleRank = 4;
          text = match[2];
          stockCode = match[1];
          if (!watchlistCodes.has(match[1]) || seenStockCodes.has(match[1])) {
            throw new Error("结构化分析股票推断必须唯一且属于固定股票池: " + match[1]);
          }
          seenStockCodes.add(match[1]);
        } else {
          match = line.match(/^- \[推断\] 主题 ([^：\n]+)：(.+)$/);
          if (match) {
            role = "主题";
            textRole = "theme";
            roleRank = 1;
            text = match[2];
            themeTitle = match[1];
            if (canonicalStructuredAnalysisText("theme_title", match[1]) !== match[1]) {
              throw new Error("结构化分析主题标题必须逐字使用程序固定模板");
            }
          }
        }
      }
      if (!role || canonicalStructuredAnalysisText(textRole, text) !== text) {
        throw new Error("结构化分析推断父行必须逐字使用程序固定模板: " + line.slice(0, 120));
      }
      if (roleRank < previousRoleRank) {
        throw new Error("结构化分析推断必须保持程序固定的角色顺序");
      }
      previousRoleRank = roleRank;
      roleCounts.set(role, (roleCounts.get(role) ?? 0) + 1);
      const roleLimit = STRUCTURED_ANALYSIS_SELECTION_LIMITS[textRole]
        ?? Math.max(1, watchlistCodes.size);
      if (roleCounts.get(role) > roleLimit) {
        throw new Error(`结构化分析的${role}推断超过程序固定上限`);
      }
      currentBlock = {
        role,
        textRole,
        text,
        stockCode,
        themeTitle,
        supportCount: 0,
        counterCount: 0,
        placeholderCount: 0,
        inferenceLimit: false,
        periodNote: null,
        supportRoles: new Map(),
        supportSourceIds: new Set(),
        counterSourceIds: new Set()
      };
      blockCount += 1;
      if (blockCount > STRUCTURED_ANALYSIS_SELECTION_LIMITS.total) {
        throw new Error("结构化分析推断超过程序固定总数上限");
      }
      continue;
    }
    if (line.includes("[推断]")) {
      throw new Error("结构化分析推断父行未使用固定格式: " + line.slice(0, 120));
    }
    if (!currentBlock) {
      throw new Error("结构化分析包含不属于任何推断的证据行");
    }
    if (line === "  - 推断限制：未提供结构化反证来源；此限制只约束本条推断，不否定已核验事实。") {
      currentBlock.placeholderCount += 1;
      currentBlock.inferenceLimit = true;
      continue;
    }
    const period = line.match(/^  - 期间：行情 (\d{4}-\d{2}-\d{2})；利润同比报告期 (\d{4}-\d{2}-\d{2})。$/);
    if (period) {
      if (currentBlock.periodNote) throw new Error("结构化分析期间说明不得重复");
      currentBlock.periodNote = `行情 ${period[1]}；利润同比报告期 ${period[2]}`;
      continue;
    }
    const evidence = line.match(
      /^  - (支持证据|反证)(?:（(上涨占优|下跌占优|成交额|单日行情|报告期利润同比)）)?：(?:\[来源 ([1-9]\d*)\](\[陈旧\])?|未提供结构化反证来源，不升级为事实结论。)$/
    );
    if (!evidence || (evidence[3] == null && evidence[1] !== "反证")) {
      throw new Error("结构化分析的每条支持或反证必须使用固定单来源格式: " + line.slice(0, 120));
    }
    if (evidence[3] == null) {
      currentBlock.placeholderCount += 1;
      continue;
    }
    if (evidence[1] === "支持证据") currentBlock.supportCount += 1;
    else currentBlock.counterCount += 1;

    const number = Number(evidence[3]);
    const state = freshnessBySourceNumber.get(number);
    if (!state) throw new Error(`结构化分析引用了无法映射的来源编号: ${number}`);
    if (currentBlock.stockCode && !stockEvidenceByCode.get(currentBlock.stockCode)?.has(state.sourceId)) {
      throw new Error(`结构化分析股票 ${currentBlock.stockCode} 引用了不属于该股票的来源`);
    }
    const ownSet = evidence[1] === "支持证据"
      ? currentBlock.supportSourceIds
      : currentBlock.counterSourceIds;
    const oppositeSet = evidence[1] === "支持证据"
      ? currentBlock.counterSourceIds
      : currentBlock.supportSourceIds;
    if (ownSet.has(state.sourceId)) {
      throw new Error("结构化分析的同一推断不得重复引用同一来源");
    }
    if (oppositeSet.has(state.sourceId)) {
      throw new Error("结构化分析的同一来源不得同时作为支持与反证");
    }
    ownSet.add(state.sourceId);
    if (evidence[2]) {
      currentBlock.supportRoles ??= new Map();
      if (evidence[1] !== "支持证据") throw new Error("反证行不得带证据角色");
      currentBlock.supportRoles.set(state.sourceId, evidence[2]);
    }
    if (!state.tracked) {
      if (evidence[4]) throw new Error(`不适用时效阈值的来源不得标记陈旧: [来源 ${number}]`);
      continue;
    }
    const hasStaleLabel = Boolean(evidence[4]);
    if (state.stale && !hasStaleLabel) {
      throw new Error(`结构化分析的超时来源未紧邻标记陈旧: [来源 ${number}]`);
    }
    if (!state.stale && hasStaleLabel) {
      throw new Error(`结构化分析的新鲜来源不得标记陈旧: [来源 ${number}]`);
    }
  }
  finishBlock();
  if (blockCount === 0) {
    throw new Error("结构化增强稿的第 6 节不得为空");
  }
  for (const block of structuredBlocks) {
    const signature = structuredAnalysisCandidateSignature(block);
    const matches = semanticCandidates.filter((item) =>
      structuredAnalysisCandidateSignature(item) === signature);
    if (matches.length !== 1 || usedSemanticCandidateIds.has(matches[0].id)) {
      throw new Error("结构化分析推断不属于程序证明的语义候选白名单");
    }
    const candidate = matches[0];
    if (candidate.evidenceRoles) {
      for (const [id, role] of Object.entries(candidate.evidenceRoles)) {
        if (block.supportRoles?.get(id) !== role) {
          throw new Error("结构化分析证据角色与程序候选不一致");
        }
      }
    }
    if (candidate.periodNote && block.periodNote !== candidate.periodNote) {
      throw new Error("结构化分析期间说明与程序候选不一致");
    }
    usedSemanticCandidateIds.add(candidate.id);
  }
  return [...usedSemanticCandidateIds];
}

function validateDeterministicCompilerOutput(report, {
  context,
  targetDate,
  cutoffLocal,
  cutoffInstant,
  publicationLevel,
  publicationAudit,
  disclaimer,
  marketDataMaxAgeHours,
  newsMaxAgeHours,
  validatedCandidateIds,
  requireStaleMarketLabel = false,
  strictContextSources = false
}) {
  if (!context || publicationAudit?.passed !== true) {
    throw new Error("生产发布缺少可重建的固定数据包或发布门禁");
  }
  const buildOptions = {
    context,
    targetDate,
    cutoffLocal,
    cutoffInstant,
    publicationLevel,
    publicationAudit,
    disclaimer,
    marketDataMaxAgeHours,
    newsMaxAgeHours
  };
  if (!Array.isArray(validatedCandidateIds)) {
    throw new Error("生产发布缺少已验证的语义候选选择");
  }
  const overlay = parseStructuredAnalysisOutput(JSON.stringify({
    schemaVersion: "2.0", candidateIds: validatedCandidateIds
  }), { context });
  const expectedReport = buildVerifiedBaselineReport({
    ...buildOptions, analysisOverlay: overlay
  });
  // 比较规范化后的固定编译输出，但规范化本身仍必须保留来源时效语义。
  // 特别是美股正文不能用小时阈值覆盖来源的交易日判定，否则两边同时
  // 删除真实 [陈旧] 标签也会错误地满足逐字一致。
  const normalizedExpected = normalizeReportOutput(expectedReport, {
    targetDate,
    disclaimer,
    cutoffLocal,
    cutoffInstant,
    publicationLevel,
    requireStaleMarketLabel,
    context,
    strictContextSources,
    marketDataMaxAgeHours,
    newsMaxAgeHours,
    publicationAudit,
    enforcePublicationReady: true,
    requireDeterministicCompilerOutput: false
  });
  if (normalizedExpected !== report) {
    throw new Error("生产发布只接受程序编译器逐字生成的基础快报或语义候选稿");
  }
}

function validateContextSources(
  report,
  context,
  strictContextSources,
  cutoffInstant,
  marketDataMaxAgeHours,
  newsMaxAgeHours
) {
  if (!strictContextSources || !Array.isArray(context?.sources)) return;
  const deterministicCompiled = report.includes(
    "> 生成方式：程序核验基础版"
  );
  const byUrl = new Map();
  for (const source of context.sources) {
    if (!source?.url) continue;
    const key = normalizeUrl(source.url);
    if (!byUrl.has(key)) byUrl.set(key, []);
    byUrl.get(key).push(source);
  }

  for (const match of report.matchAll(/https?:\/\/[^\s)>，。；、）】》"'<>]+/gi)) {
    const rawUrl = match[0].replace(/[，。；;,]+$/, "");
    if (!byUrl.has(normalizeUrl(rawUrl))) {
      throw new Error("报告引用了数据包之外的 URL: " + rawUrl.slice(0, 120));
    }
  }

  const sourceStart = report.lastIndexOf("## 来源");
  const sourceSection = sourceStart >= 0 ? report.slice(sourceStart) : "";
  const sourceLines = sourceSection
    .split(/\r?\n/)
    .filter((line) => /https?:\/\//i.test(line));
  const numberedSourceLines = sourceSection
    .split(/\r?\n/)
    .filter((line) => /^\s*\d+\.\s+/.test(line));
  const freshnessBySourceNumber = new Map();
  const seenSourceIds = new Set();
  if (numberedSourceLines.length === 0) {
    throw new Error("正式报告的来源清单不能为空");
  }
  for (const line of numberedSourceLines) {
    if (!/https?:\/\//i.test(line)) {
      throw new Error("正式报告的每条编号来源都必须包含直接 URL: " + line.slice(0, 100));
    }
  }
  for (const line of sourceLines) {
    const sourceNumber = Number(line.match(/^\s*(\d+)\.\s+/)?.[1]);
    if (!Number.isInteger(sourceNumber) || sourceNumber <= 0) {
      throw new Error("正式报告的来源编号必须是正整数: " + line.slice(0, 100));
    }
    if (freshnessBySourceNumber.has(sourceNumber)) {
      throw new Error(`正式报告包含重复来源编号: ${sourceNumber}`);
    }
    const urlMatches = [...line.matchAll(/https?:\/\/[^\s)>，。；、）】》"'<>]+/gi)];
    if (urlMatches.length !== 1) {
      throw new Error("正式报告的每条来源只能包含一个直接 URL: " + line.slice(0, 100));
    }
    const url = urlMatches[0][0].replace(/[，。；;,]+$/, "");
    const normalizedUrl = normalizeUrl(url ?? "");
    const candidates = byUrl.get(normalizedUrl) ?? [];
    if (candidates.length === 0) continue;
    if (!candidates.some((source) => source.title && line.includes(source.title))) {
      throw new Error("来源清单必须逐字使用数据包的来源标题: " + line.slice(0, 100));
    }
    const timezone = context?.meta?.timezone ?? "Asia/Shanghai";
    const expectedTimes = candidates
      .map((source) => source.publishedAt ?? source.dataAsOf)
      .filter(isValidInstant)
      .map((value) => formatSourceInstant(value, timezone));
    if (expectedTimes.length > 0 && !expectedTimes.some((value) => line.includes(value))) {
      throw new Error(
        "来源清单时间必须与数据包来源时间一致: " + line.slice(0, 100)
      );
    }
    const matchingCandidates = candidates.filter((source) => {
      if (!source?.title || !line.includes(source.title)) return false;
      const instant = source.publishedAt ?? source.dataAsOf;
      return !isValidInstant(instant) || line.includes(formatSourceInstant(instant, timezone));
    });
    if (matchingCandidates.length === 0) {
      throw new Error("来源清单无法映射到固定数据包来源: " + line.slice(0, 100));
    }
    if (matchingCandidates.length !== 1) {
      throw new Error("来源清单无法唯一映射到固定数据包来源: " + line.slice(0, 100));
    }
    const matchedSource = matchingCandidates[0];
    if (seenSourceIds.has(matchedSource.id)) {
      throw new Error("正式报告的来源清单不得重复引用同一固定来源: " + line.slice(0, 100));
    }
    seenSourceIds.add(matchedSource.id);
    let expectedIdentity = matchedSource.sourceType;
    if (matchedSource.sourceType === "media_aggregation") {
      const origin = matchedSource.originalPublisher
        ? `转引${matchedSource.originalPublisher}`
        : "原始发布方未核验";
      expectedIdentity += `（${matchedSource.aggregator ?? "东方财富"}聚合，${origin}）`;
    } else if (matchedSource.publisher) {
      expectedIdentity += `（${matchedSource.publisher}）`;
    }
    if (deterministicCompiled && !line.includes(`— ${expectedIdentity}；`)) {
      throw new Error("来源清单的类型与发布方必须与固定数据包一致: " + line.slice(0, 100));
    }
    const freshnessStates = matchingCandidates.map((source) => classifySourceFreshness(source, {
      cutoffInstant,
      marketDataMaxAgeHours,
      newsMaxAgeHours
    }));
    const trackedStates = freshnessStates.filter((state) => state.tracked);
    let sourceFreshness = Object.freeze({ sourceId: matchedSource.id, tracked: false, stale: false });
    const hasStaleLabel = /^\s*\d+\.\s+\[陈旧\]\s+/.test(line);
    const staleLabelCount = [...line.matchAll(/\[陈旧\]/g)].length;
    if (trackedStates.length > 0) {
      if (trackedStates.length !== freshnessStates.length ||
          new Set(trackedStates.map((state) => state.stale)).size !== 1) {
        throw new Error("来源清单 URL 对应多个不同时效状态，拒绝发布: " + line.slice(0, 100));
      }
      sourceFreshness = Object.freeze({
        sourceId: matchedSource.id,
        tracked: true,
        stale: trackedStates[0].stale
      });
      if (sourceFreshness.stale && !hasStaleLabel) {
        throw new Error("超过适用时效阈值的来源清单项未标记陈旧: " + line.slice(0, 100));
      }
      if (sourceFreshness.stale && staleLabelCount !== 1) {
        throw new Error("超时来源清单项必须恰好包含一个规范陈旧标记: " + line.slice(0, 100));
      }
      if (!sourceFreshness.stale && (hasStaleLabel || staleLabelCount !== 0)) {
        throw new Error("未超过适用时效阈值的来源清单项不得标记陈旧: " + line.slice(0, 100));
      }
    } else if (hasStaleLabel || staleLabelCount !== 0) {
      throw new Error("不适用时效阈值的来源清单项不得标记陈旧: " + line.slice(0, 100));
    }
    freshnessBySourceNumber.set(sourceNumber, sourceFreshness);
    const aggregation = matchedSource.sourceType === "media_aggregation" ? matchedSource : null;
    if (aggregation) {
      const provenanceIsValid = aggregation.originalPublisher
        ? line.includes(aggregation.originalPublisher)
        : line.includes("原始发布方未核验") &&
          !/聚合[（(][^）)]*转引/.test(line);
      const hasAggregationIdentity =
        line.includes(aggregation.aggregator ?? "东方财富") &&
        line.includes("聚合") &&
        provenanceIsValid;
      if (!hasAggregationIdentity) {
        throw new Error("媒体聚合来源必须同时标明聚合平台与原媒体: " + line.slice(0, 100));
      }
    }
  }
  return validateStructuredEvidenceSourceFreshness(report, freshnessBySourceNumber, context);
}

// This recovers only the selected whitelist proofs. Callers must still compile
// with the fixed inputs and compare the complete report byte-for-byte.
export function recoverStructuredAnalysisSelection(report, {
  context, cutoffInstant, marketDataMaxAgeHours, newsMaxAgeHours
}) {
  const candidateIds = validateContextSources(
    report, context, true, cutoffInstant, marketDataMaxAgeHours, newsMaxAgeHours
  );
  if (!Array.isArray(candidateIds)) throw new Error("缺少可验证的候选来源数据");
  return parseStructuredAnalysisOutput(JSON.stringify({
    schemaVersion: "2.0", candidateIds
  }), { context });
}

function freshnessAgeHours(value, cutoffInstant) {
  if (!isValidInstant(value)) return null;
  return (new Date(cutoffInstant).getTime() - new Date(value).getTime()) / 3_600_000;
}

function recordLabels(record, extras = []) {
  const values = [
    ...extras,
    record?.name,
    record?.title,
    record?.code,
    record?.symbol,
    record?.sectorName,
    record?.industryName
  ];
  const title = String(record?.title ?? "");
  if (/A50/i.test(title)) values.push("富时中国A50", "A50指数期货");
  return [...new Set(values
    .filter((value) => typeof value === "string")
    .map((value) => value.trim())
    .filter((value) => value.length >= 2))];
}

function buildFreshnessRules(context, cutoffInstant, marketDataMaxAgeHours, newsMaxAgeHours) {
  if (!context?.data) return [];
  const rules = [];
  const overnightSessionFreshness = (record) => {
    const ids = record?.sourceIds;
    const sources = Array.isArray(ids)
      ? ids.map((id) => (context.sources ?? []).filter((source) => source?.id === id))
      : [];
    const hasCalendar = record?.marketCalendar !== undefined ||
      sources.some((matches) => matches.some((source) => source.marketCalendar !== undefined));
    if (!hasCalendar) return null; // Preserve the existing hourly policy for legacy untyped records.
    if (!Array.isArray(ids) || ids.length === 0 || new Set(ids).size !== ids.length ||
        sources.some((matches) => matches.length !== 1)) {
      throw new Error("美股正文时效无法唯一绑定固定来源");
    }
    const states = sources.map(([source]) => {
      // Bind the close clock, not an optional independent publication clock.
      // A cached freshness label cannot replace the frozen source evidence.
      if (source.marketCalendar === undefined ||
          (record.marketCalendar !== undefined && record.marketCalendar !== source.marketCalendar) ||
          (source.marketSymbol ?? source.symbol) !== record.symbol ||
          source.sessionDate !== record.sessionDate ||
          !isValidInstant(record.dataAsOf) || !isValidInstant(source.dataAsOf) ||
          new Date(source.dataAsOf).getTime() !== new Date(record.dataAsOf).getTime()) {
        throw new Error("美股正文与固定来源的市场、交易日或时间不一致");
      }
      // The compiler and source-list validator use this exact policy too.
      return classifySourceFreshness(source, {
        cutoffInstant, marketDataMaxAgeHours, newsMaxAgeHours
      });
    });
    return states.some((state) => state.stale);
  };
  const add = (record, kind, extras = [], fallbackTime = null, options = {}) => {
    const timeValue = typeof options.sessionStale === "boolean"
      ? record.dataAsOf
      : sourceEvidenceInstant(record) ?? fallbackTime;
    const ageHours = freshnessAgeHours(timeValue, cutoffInstant);
    const labels = recordLabels(record, extras);
    if (ageHours === null || labels.length === 0) return;
    const nameLabel = typeof record?.name === "string" ? record.name.trim() : null;
    const titleLabel = typeof record?.title === "string" ? record.title.trim() : null;
    const companyNameLabel = typeof options.companyName === "string"
      ? options.companyName.trim()
      : null;
    const providerNameLabels = [...new Set([
      ...(kind === "news" ? [nameLabel] : []),
      companyNameLabel
    ].filter(Boolean))];
    const newsIdentityLabels = kind === "news"
      ? [...new Set([nameLabel, record?.code, record?.symbol]
        .filter((value) => typeof value === "string")
        .map((value) => value.trim())
        .filter(Boolean))]
      : [];
    rules.push({
      kind,
      scope: options.scope ?? null,
      sourceType: record?.sourceType,
      code: record?.code,
      disclosureDate: record?.disclosureDate,
      labels,
      nameLabel,
      titleLabel,
      providerNameLabels,
      newsIdentityLabels,
      dataAsOf: timeValue,
      ageHours,
      stale: options.neverStale === true
        ? false
        : typeof options.sessionStale === "boolean"
          ? options.sessionStale
          : ageHours > (Number.isFinite(options.maxAgeHours)
            ? options.maxAgeHours
            : (kind === "news" ? newsMaxAgeHours : marketDataMaxAgeHours))
    });
  };

  const session = context.data.previousAShareSession;
  for (const index of session?.indices ?? []) add(index, "market", [], session?.dataAsOf);
  if (session) {
    add(session, "market", [
      "上一交易日 A 股",
      "上一交易日A股",
      "全市场成交额",
      "沪深两市成交额",
      "沪深 A 股成交额",
      "总成交额",
      "市场宽度",
      "上涨家数",
      "下跌家数",
      "平盘家数"
    ]);
  }
  for (const item of context.data.overnightGlobal ?? []) {
    add(item, "market", [], null, {
      sessionStale: overnightSessionFreshness(item),
      maxAgeHours: Math.max(marketDataMaxAgeHours, 72)
    });
  }
  for (const item of context.data.watchlist ?? []) {
    add(item.marketData, "market", [item.code, item.symbol, item.name], item.dataAsOf, {
      scope: "watchlist_price",
      companyName: item.name
    });
  }
  for (const item of context.data.sectors ?? []) {
    add(item, "market");
    for (const entry of item?.entries ?? []) add(entry, "market", [], item.dataAsOf);
  }
  const capitalLabels = {
    northbound_realtime: ["北向资金"],
    dragon_tiger_list: ["龙虎榜"],
    sector_capital_flow: ["板块资金"],
    stock_capital_flow: ["个股资金"]
  };
  for (const item of context.data.capitalFlows ?? []) {
    add(item, "market", capitalLabels[item?.type] ?? []);
    for (const entry of item?.entries ?? []) {
      add(entry, "market", capitalLabels[item?.type] ?? [], item.dataAsOf);
    }
  }
  for (const item of context.data.policyAndCalendar ?? []) add(item, "news");
  for (const item of context.data.announcementsAndNews ?? []) {
    // Official filings enter rules with neverStale for precise document-line
    // matching only (see matchingFreshnessRules). Broad name/code matching
    // excludes them so stale same-name media cannot inherit immunity.
    if (item?.sourceType === "official_announcement") {
      add(item, "news", [], null, { neverStale: true });
      continue;
    }
    add(item, "news");
  }
  for (const stock of context.data.watchlist ?? []) {
    for (const item of stock.officialDisclosure?.recentBackground ?? []) {
      if (item?.sourceType === "official_announcement") {
        add(item, "news", [], null, { neverStale: true });
        continue;
      }
      add(item, "news");
    }
  }
  return rules;
}

const MARKET_CLAIM_TERM = /(?:涨|跌|成交|收盘|前收|指数|行情|资金|净流入|净流出|净买额|净卖出|买入额|卖出额|龙虎榜|点位|换手|振幅|股价|开盘|最高|最低)/;
const WATCHLIST_PRICE_EXPLICIT_TERM = /(?:股价|股票价格|行情|收盘|前收|最新价|开盘|涨停|跌停|换手|振幅|成交价)/;
const WATCHLIST_NON_PRICE_CAPITAL_TERM = /(?:融资|融券|资金|净买入|净卖出|净流入|净流出|净买额|净卖额|买入额|卖出额|余额)/;
const WATCHLIST_OTHER_MARKET_SUBJECT_TERM = /(?:板块|行业|指数|大盘|全市场|沪深两市|A\s*股|美股|港股|(?:上证|深证|创业板|科创板|沪深\s*\d*)指(?:数)?|道指|纳指|标普(?:\s*500)?|A50)/i;
const WATCHLIST_PRICE_FALLBACK_TERM = /(?:涨|跌|成交(?:量|额|金额|股数|手数|\s*[+-]?\d)|最高|最低|价格|点位)/;
const WATCHLIST_DIRECT_PRICE_MOVE_CLAIM = /(?:涨跌幅|涨幅|跌幅|上涨|下跌|收涨|收跌|涨|跌)\s*(?:为|[：:])?\s*[+-]?\d[\d,]*(?:\.\d+)?\s*[%％]/;
const COMPANY_FINANCIAL_TERM = /(?:营收|净利润|毛利率|业绩|分红|年报|季报|中报|Q[1-4]|H[12]|同比)/i;
const NUMERIC_CLAIM_PATTERN = /[+-]?\d[\d,]*(?:\.\d+)?\s*(?:%|％|点|亿元|万亿元|元|万股|万手|手|家)/g;
const NEWS_EVENT_VETO_TERM = /(?:发布|披露|宣布|宣告|获得|收到|签署|签订|中标|获批|立案|调查|留置|发生(?:安全)?事故|完成(?:收购|并购|重组|投资)|收购|并购|重组|回购|增持|减持|停牌|复牌|处罚|警示函|造假|投资(?:新)?产线)/g;
const NEWS_EVENT_NEGATION_PREFIX = /(?:未|尚未|并未|没有|无|未能|无法|不能|不可)[^。；，,\n]{0,8}$/;
const NEWS_EVENT_CONDITIONAL_PREFIX = /(?:若|如果|一旦|是否|能否)[^。；;，,：:\n]*$/;
const NEWS_EVENT_CONSEQUENT_PREFIX = /^(?![^，,：:]*?(?:已|已经|现已))(?:(?:公司|该公司|本公司|其)\s*)?(?:则|那么|将|可能|拟|预计|否则|届时)/;
const STRICT_FINANCIAL_NUMBER = "[+-]?\\d[\\d,.]*\\s*(?:%|％|亿元|万元|元|倍|点)?";
const STRICT_FINANCIAL_METRIC = "(?:营收|营业收入|收入|归母净利(?:润)?|扣非净利(?:润)?|净利润|利润|毛利率|净利率|ROE|EPS|每股收益|现金流|总资产|净资产|负债|业绩)";
const STRICT_FINANCIAL_FRAGMENT = new RegExp(
  `^(?:(?:财务|事实|当前|已有|数据包显示|20\\d{2}(?:年)?|(?:一|二|三|四)季报|年报|半年报|中报|Q[1-4]|H[12])[：:\\s-]*)*` +
  `${STRICT_FINANCIAL_METRIC}(?:同比|环比)?(?:为|是|达到|约|增长|下降|增加|减少|改善|恶化|[：:\\s])*` +
  `${STRICT_FINANCIAL_NUMBER}(?:\\s*(?:为|构成)?(?:反证性)?证据)?$`,
  "i"
);
const STRICT_FINANCIAL_COMPARISON_FRAGMENT = new RegExp(
  `^(?:同比|环比)(?:为|是|达到|约|增长|下降|增加|减少|[：:\\s])*${STRICT_FINANCIAL_NUMBER}$`,
  "i"
);
const STRICT_FINANCIAL_HEADER_FRAGMENT = /^(?:财务)?(?:20\d{2}年?)?(?:(?:一|二|三|四)季报|年报|半年报|中报|Q[1-4]|H[12])$/i;
const COMPANY_NAME_PROVIDER_QUALIFIER = /^[\s·:：/—-]*(?:(?:数据|资讯)(?:口径|终端|平台|统计|显示)?|终端|平台|口径|统计(?:口径)?|(?:行业|概念)(?:口径|分类|体系|指数|数据|[A-Za-z0-9\u4e00-\u9fff]{0,16}板块))/u;
const COMPANY_NAME_PROVIDER_PREFIX = /(?:数据来源|数据源|行情来源|资讯来源|数据提供方|来源)\s*[：:]?\s*$/u;
const NEWS_TITLE_MATCH_IGNORABLE = /[\s,，、。;；:：!?！？"'“”‘’《》〈〉【】\[\]()（）{}<>—–\-·…/\\|]+/gu;

function normalizeNewsTitleForMatch(value) {
  return String(value ?? "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(NEWS_TITLE_MATCH_IGNORABLE, "");
}

function normalizedNewsTextWithOffsets(value) {
  let text = "";
  const offsets = [];
  let originalIndex = 0;
  for (const character of String(value ?? "")) {
    const start = originalIndex;
    originalIndex += character.length;
    const normalizedCharacter = character
      .normalize("NFKC")
      .toLowerCase()
      .replace(NEWS_TITLE_MATCH_IGNORABLE, "");
    text += normalizedCharacter;
    for (let index = 0; index < normalizedCharacter.length; index += 1) {
      offsets.push({ start, end: originalIndex });
    }
  }
  return { text, offsets };
}

function newsRuleSpecificTitleMatchRanges(line, rule) {
  if (rule.kind !== "news" || !rule.titleLabel) return [];
  const foldedTitle = rule.titleLabel.toLowerCase();
  if (foldedTitle === String(rule.nameLabel ?? "").toLowerCase()) return [];
  const foldedLine = line.toLowerCase();
  const exactRanges = [];
  let searchFrom = 0;
  while (searchFrom < foldedLine.length) {
    const index = foldedLine.indexOf(foldedTitle, searchFrom);
    if (index < 0) break;
    exactRanges.push({ start: index, end: index + foldedTitle.length });
    searchFrom = index + foldedTitle.length;
  }
  if (exactRanges.length > 0) return exactRanges;
  const normalizedTitle = normalizeNewsTitleForMatch(rule.titleLabel);
  if (normalizedTitle.length < 8) return [];
  const normalizedLine = normalizedNewsTextWithOffsets(line);
  const ranges = [];
  searchFrom = 0;
  while (searchFrom < normalizedLine.text.length) {
    const index = normalizedLine.text.indexOf(normalizedTitle, searchFrom);
    if (index < 0) break;
    ranges.push({
      start: normalizedLine.offsets[index].start,
      end: normalizedLine.offsets[index + normalizedTitle.length - 1].end
    });
    searchFrom = index + normalizedTitle.length;
  }
  return ranges;
}

function newsRuleHasSpecificTitleMatch(line, rule) {
  return newsRuleSpecificTitleMatchRanges(line, rule).length > 0;
}

function companyNameHasSubjectOccurrence(line, label) {
  const foldedLine = line.toLowerCase();
  const foldedLabel = label.toLowerCase();
  let searchFrom = 0;
  while (searchFrom < foldedLine.length) {
    const index = foldedLine.indexOf(foldedLabel, searchFrom);
    if (index < 0) return false;
    const beforeLabel = line.slice(0, index);
    const afterLabel = line.slice(index + label.length);
    if (
      !COMPANY_NAME_PROVIDER_QUALIFIER.test(afterLabel) &&
      !COMPANY_NAME_PROVIDER_PREFIX.test(beforeLabel)
    ) return true;
    searchFrom = index + foldedLabel.length;
  }
  return false;
}

function watchlistPriceClaimText(line) {
  return line
    .replace(/发布于[^。；\n]{0,24}收盘后/g, "")
    .replace(/(?:不能|无法|不可|未能)[^。；\n]{0,16}(?:解释|归因于)[^。；\n]{0,16}涨跌/g, "");
}

function hasWatchlistPriceClaim(line) {
  const claimText = watchlistPriceClaimText(line);
  if (WATCHLIST_PRICE_EXPLICIT_TERM.test(claimText)) return true;
  if (WATCHLIST_NON_PRICE_CAPITAL_TERM.test(claimText)) return false;
  return WATCHLIST_PRICE_FALLBACK_TERM.test(claimText);
}

function isPureWatchlistPriceClaim(line) {
  const claimText = watchlistPriceClaimText(line);
  if (hasAffirmativeNewsEventAssertion(line)) return false;
  if (
    !WATCHLIST_PRICE_EXPLICIT_TERM.test(claimText) &&
    !WATCHLIST_DIRECT_PRICE_MOVE_CLAIM.test(claimText)
  ) return false;
  return [...claimText.matchAll(NUMERIC_CLAIM_PATTERN)].length > 0 ||
    /(?:收盘|前收|最新价|股价|点位)\s*[+-]?\d/.test(claimText);
}

function clauseHasNewsIdentity(clause, rule) {
  const foldedClause = clause.toLowerCase();
  return rule.newsIdentityLabels.some((label) =>
    foldedClause.includes(label.toLowerCase())
  );
}

function isConditionallyGovernedNewsEvent(clause, eventIndex) {
  const governingPrefix = clause.slice(0, eventIndex);
  if (NEWS_EVENT_CONDITIONAL_PREFIX.test(governingPrefix)) return true;

  const weakBoundaryIndex = Math.max(
    governingPrefix.lastIndexOf("，"),
    governingPrefix.lastIndexOf(","),
    governingPrefix.lastIndexOf("："),
    governingPrefix.lastIndexOf(":")
  );
  if (weakBoundaryIndex < 0) return false;

  const conditionPrefix = governingPrefix.slice(0, weakBoundaryIndex);
  const consequentPrefix = governingPrefix.slice(weakBoundaryIndex + 1).trim();
  return NEWS_EVENT_CONDITIONAL_PREFIX.test(conditionPrefix) &&
    NEWS_EVENT_CONSEQUENT_PREFIX.test(consequentPrefix);
}

function hasAffirmativeNewsEventAssertion(clause) {
  NEWS_EVENT_VETO_TERM.lastIndex = 0;
  for (const match of clause.matchAll(NEWS_EVENT_VETO_TERM)) {
    const prefix = clause.slice(Math.max(0, match.index - 12), match.index);
    if (
      !NEWS_EVENT_NEGATION_PREFIX.test(prefix) &&
      !isConditionallyGovernedNewsEvent(clause, match.index)
    ) return true;
  }
  return false;
}

function hasIndependentAffirmativeNewsEventAssertion(clause) {
  const cleaned = clause
    .replace(/[*_`#>“”]/g, "")
    .replace(/\[(?:事实|推断|情景)\]/g, "")
    .replace(/^[-+\s\d.、]+/, "")
    .trim();
  if (
    /^(?:影响股票|影响方向|证据链)\s*[：:]/.test(cleaned) ||
    /^(?:发布|披露|报道)?时间\s*[：:]/.test(cleaned) ||
    /^(?:该条|该报道|此报道|该消息)?\s*发布于/.test(cleaned) ||
    /^(?:该|此|上述)?(?:报道|消息|新闻)[^，,]{0,12}(?:发布|披露)于/.test(cleaned) ||
    /^(?:媒体报道的)?(?:增持|减持|回购|收购)(?:数量|金额|比例|股份数量|股数|价格|均价|占)/.test(cleaned)
  ) return false;
  const counterevidence = cleaned.match(/^反证\s*[：:]\s*(.*)$/)?.[1] ?? null;
  if (
    counterevidence &&
    /(?:若|如果|假如|可能|或许|未必|不一定|无法归因|不能归因|不可归因|替代解释)/.test(counterevidence) &&
    !/(?:已|已经|现已)[^。；;，,]{0,20}(?:完成|发布|披露|宣布|获得|收到|签署|签订|中标|获批|立案|调查|留置|收购|并购|重组|回购|增持|减持|停牌|复牌)/.test(counterevidence)
  ) return false;
  const withoutPublicationMetadata = cleaned
    .replace(/(?:发布|披露|报道)?时间\s*[：:][^。；;\n]*/g, "")
    .replace(/[（(]?\s*20\d{2}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?\s*(?:发布|披露)\s*[）)]?/g, "");
  return hasAffirmativeNewsEventAssertion(withoutPublicationMetadata);
}

function hasNewsEventOutsideSpecificTitleRanges(line, specificTitleRules) {
  const ranges = specificTitleRules
    .flatMap((rule) => newsRuleSpecificTitleMatchRanges(line, rule))
    .sort((left, right) => left.start - right.start);
  let cursor = 0;
  let withoutSpecificTitles = "";
  for (const range of ranges) {
    if (range.end <= cursor) continue;
    withoutSpecificTitles += line.slice(cursor, Math.max(cursor, range.start));
    withoutSpecificTitles += " ".repeat(range.end - Math.max(cursor, range.start));
    cursor = range.end;
  }
  withoutSpecificTitles += line.slice(cursor);
  return withoutSpecificTitles
    .split(/[。；;，,\n]/)
    .map((clause) => clause.trim())
    .filter(Boolean)
    .some(hasIndependentAffirmativeNewsEventAssertion);
}

function isStrictPureFinancialNumericStatement(line, rule) {
  let cleaned = line.replace(/[*_`#>"“”]/g, "");
  for (const label of [...rule.newsIdentityLabels].sort((a, b) => b.length - a.length)) {
    cleaned = cleaned.replace(new RegExp(escapeReportPattern(label), "gi"), "");
  }
  cleaned = cleaned.replace(/(\d),(?=\d{3}(?:\D|$))/g, "$1");
  const fragments = cleaned
    .replace(/\[(?:事实|推断)\]/g, "")
    .split(/[。；;，,]/)
    .map((value) => value
      .replace(/^[-+\s]+/, "")
      .replace(/^([^\s：:（）()]{0,12})\s*[（(]\s*[）)]\s*[：:]?\s*/, "$1")
      .replace(/^[：:]\s*/, "")
      .trim())
    .filter(Boolean);
  const financialFragments = fragments.filter((fragment) =>
    STRICT_FINANCIAL_FRAGMENT.test(fragment) ||
    STRICT_FINANCIAL_COMPARISON_FRAGMENT.test(fragment)
  );
  return financialFragments.length > 0 && fragments.every((fragment) =>
    STRICT_FINANCIAL_FRAGMENT.test(fragment) ||
    STRICT_FINANCIAL_COMPARISON_FRAGMENT.test(fragment) ||
    STRICT_FINANCIAL_HEADER_FRAGMENT.test(fragment)
  );
}

function isExplicitNonNewsCompanyContext(line, rule) {
  const identityStrongClauses = line
    .split(/[。；;\n]/)
    .filter((clause) => clauseHasNewsIdentity(clause, rule));
  if (
    identityStrongClauses.length === 0 ||
    identityStrongClauses.some(hasAffirmativeNewsEventAssertion)
  ) return false;

  const isConditionalLogicReview =
    /逻辑失效(?:条件)?/.test(line) &&
    /(?:若|如果|待观察|待核验|待验证|数据包未提供|无法核验|不能支持|缺乏|缺失)/.test(line);
  const isConditionalScenario = /\[情景\]/.test(line) && /(?:若|如果)/.test(line);
  const isTodayVerification = /今日需核验/.test(line);
  const isMissingEvidenceFields = /证据字段[^。；\n]{0,80}(?:缺失|未提供|不可用|无法核验)/.test(line);
  const isAnnouncementUnobservable = /(?:公告(?:数据|源)?(?:不存在|不可用|缺失)|无法核验(?:是否有)?公告|尚未发布公告|数据包未提供[^。；\n]{0,16}(?:公司|官方)?公告)/.test(line);
  return isConditionalLogicReview ||
    isConditionalScenario ||
    isTodayVerification ||
    isMissingEvidenceFields ||
    isAnnouncementUnobservable ||
    isStrictPureFinancialNumericStatement(line, rule);
}

function matchingFreshnessRules(line, rules) {
  // Exact deterministic disclosure lines describe one quoted document. The
  // words “新增披露/披露日期” are metadata, not a second media event about the
  // same company. Keep free-form/mixed claims on the existing broad matcher.
  const documentRules = rules.filter((rule) => {
    if (rule.sourceType !== "official_announcement" || !rule.code || !rule.nameLabel || !rule.disclosureDate || !rule.titleLabel) return false;
    const identity = `${rule.code} ${rule.nameLabel}（披露 ${rule.disclosureDate}）：${rule.titleLabel}。`;
    return new RegExp(`^- (?:\\[陈旧\\] )?\\[事实\\] (?:新增披露|巨潮公告|近期背景（非今日新增）) ${escapeReportPattern(identity)}\\[来源 \\d+(?:、\\d+)*\\]$`).test(line.trim());
  });
  if (documentRules.length > 0) return documentRules;
  if (/^- \[事实\] \d{6} [^：]+：当前数据包中无可引用的窗口内公告条目，仅代表本次有限查询范围。\[来源 \d+(?:、\d+)*\]$/.test(line.trim())) return [];
  // Official filings keep neverStale only for the precise document-line match
  // above. Exclude them from the broad name/code matcher so same-name stale
  // media cannot inherit official-announcement immunity.
  const broadRules = rules.filter((rule) => rule.sourceType !== "official_announcement");
  const folded = line.toLowerCase();
  const specificTitleMatches = new Map(
    broadRules.map((rule) => [rule, newsRuleHasSpecificTitleMatch(line, rule)])
  );
  const specificTitleRulesByName = new Map();
  for (const rule of rules) {
    if (!specificTitleMatches.get(rule) || !rule.nameLabel) continue;
    const foldedName = rule.nameLabel.toLowerCase();
    if (!specificTitleRulesByName.has(foldedName)) {
      specificTitleRulesByName.set(foldedName, []);
    }
    specificTitleRulesByName.get(foldedName).push(rule);
  }
  const matches = broadRules.filter((rule) => {
    if (rule.kind === "market" && !MARKET_CLAIM_TERM.test(line)) return false;
    if (rule.scope === "watchlist_price" && !hasWatchlistPriceClaim(line)) return false;
    if (
      rule.kind === "market" &&
      COMPANY_FINANCIAL_TERM.test(line) &&
      !/(?:股价|行情|收盘|前收|涨跌幅|指数|成交)/.test(line)
    ) return false;
    return rule.labels.some((label) => {
      if (rule.kind === "news" && label === rule.titleLabel) {
        return specificTitleMatches.get(rule);
      }
      if (!folded.includes(label.toLowerCase())) return false;
      if (rule.newsIdentityLabels.includes(label) && label !== rule.titleLabel) {
        if (
          !specificTitleMatches.get(rule) &&
          isPureWatchlistPriceClaim(line)
        ) return false;
        if (rule.nameLabel && !specificTitleMatches.get(rule)) {
          const sameNameSpecificTitleRules = specificTitleRulesByName.get(
            rule.nameLabel.toLowerCase()
          );
          if (
            sameNameSpecificTitleRules?.length > 0 &&
            !hasNewsEventOutsideSpecificTitleRanges(line, sameNameSpecificTitleRules)
          ) return false;
        }
        if (
          !specificTitleMatches.get(rule) &&
          isExplicitNonNewsCompanyContext(line, rule)
        ) return false;
      }
      if (
        label !== rule.titleLabel &&
        rule.providerNameLabels.includes(label)
      ) return companyNameHasSubjectOccurrence(line, label);
      return true;
    });
  });
  return matches;
}

function removeStaleLabelsFromLine(line) {
  return line
    .replace(/^(\s*(?:[-*+]\s+)?)\[陈旧\]\s+/, "$1")
    .replace(/\s*\[陈旧\]/g, "");
}

function addStaleLabelsToLine(originalLine) {
  let normalized = originalLine.replace(NUMERIC_CLAIM_PATTERN, (claim, offset, whole) => {
    const before = whole.slice(Math.max(0, offset - 6), offset);
    const after = whole.slice(offset + claim.length, offset + claim.length + 6);
    return before.includes("[陈旧]") || after.trimStart().startsWith("[陈旧]")
      ? claim
      : claim + "[陈旧]";
  });
  normalized = normalized.replace(
    /((?:收盘|前收|最新价|股价|点位)\s*)([+-]?\d[\d,]*(?:\.\d+)?)(?![\d,.])(?!(?:\s*)(?:%|％|点|亿元|万亿元|元|万股|万手|手|家))/g,
    (match, prefix, value, offset, whole) => {
      const after = whole.slice(offset + match.length, offset + match.length + 6);
      return after.trimStart().startsWith("[陈旧]") ? match : `${prefix}${value}[陈旧]`;
    }
  );
  return normalized.replace(/(?:\s*\[陈旧\]){2,}/g, " [陈旧]");
}

function hasLineLevelStaleLabel(line) {
  return /^\s*(?:[-*+]\s+)?\[陈旧\](?:\s|$)/.test(line);
}

function addLineLevelStaleLabel(line) {
  if (hasLineLevelStaleLabel(line)) return line;
  return line.replace(/^(\s*(?:[-*+]\s+)?)/, "$1[陈旧] ");
}

function freshnessLinesWithInheritedWatchlistIdentity(body, context) {
  const watchlist = context?.data?.watchlist ?? [];
  let inheritedIdentity = null;
  return body.split(/\r?\n/).map((originalLine) => {
    const heading = originalLine.match(/^\s*(#{1,})\s+(.+?)\s*$/);
    if (heading && heading[1].length <= 3) {
      inheritedIdentity = null;
      if (heading[1].length === 3) {
        const headingText = heading[2].replace(/[*_`]/g, "").replace(/\s+/g, " ").trim();
        const codeIdentities = watchlist.filter((item) => {
          const code = String(item?.code ?? "").trim();
          return code && new RegExp(
            `(?:^|[^\\p{L}\\p{N}])${escapeReportPattern(code)}(?=$|[^\\p{L}\\p{N}])`,
            "u"
          ).test(headingText);
        });
        const nameIdentities = watchlist.filter((item) => {
          const name = String(item?.name ?? "").trim();
          return name && headingText.includes(name);
        });
        if (codeIdentities.length > 0 && nameIdentities.length > 0) {
          const identityKeys = new Set(
            [...codeIdentities, ...nameIdentities].map((item) =>
              `${String(item?.code ?? "").trim()}\u0000${String(item?.name ?? "").trim()}`
            )
          );
          if (identityKeys.size > 1) {
            throw new Error("三级股票标题中的股票代码与公司名称身份冲突: " + headingText.slice(0, 100));
          }
        }
        const matchingIdentities = watchlist.filter((item) => {
          const code = String(item?.code ?? "").trim();
          const name = String(item?.name ?? "").trim();
          if (!code || !name) return false;
          const codeThenNamePair = new RegExp(
            `(?:^|\\s|[：:])${escapeReportPattern(code)}\\s+${escapeReportPattern(name)}(?=$|\\s|[^\\p{L}\\p{N}])`,
            "u"
          );
          const nameThenParenthesizedCodePair = new RegExp(
            `^${escapeReportPattern(name)}\\s*[（(]\\s*${escapeReportPattern(code)}\\s*[）)]$`,
            "u"
          );
          return codeThenNamePair.test(headingText) ||
            nameThenParenthesizedCodePair.test(headingText);
        });
        inheritedIdentity = matchingIdentities.length === 1
          ? matchingIdentities[0]
          : null;
      }
      return { originalLine, matchingLine: originalLine, inheritedIdentity };
    }

    if (!inheritedIdentity || !isPureWatchlistPriceClaim(originalLine)) {
      return { originalLine, matchingLine: originalLine, inheritedIdentity };
    }
    const hasExplicitIdentity = watchlist.some((item) => {
      const code = String(item?.code ?? "").trim();
      const name = String(item?.name ?? "").trim();
      return (code && originalLine.includes(code)) || (name && originalLine.includes(name));
    });
    if (hasExplicitIdentity) {
      return { originalLine, matchingLine: originalLine, inheritedIdentity };
    }
    return {
      originalLine,
      matchingLine: `${inheritedIdentity.code} ${inheritedIdentity.name} ${originalLine}`,
      inheritedIdentity
    };
  });
}

function normalizeContextFreshnessClaims(
  report,
  context,
  cutoffInstant,
  marketDataMaxAgeHours,
  newsMaxAgeHours
) {
  const sourceStart = report.lastIndexOf("## 来源");
  const body = sourceStart >= 0 ? report.slice(0, sourceStart) : report;
  const sources = sourceStart >= 0 ? report.slice(sourceStart) : "";
  const rules = buildFreshnessRules(
    context,
    cutoffInstant,
    marketDataMaxAgeHours,
    newsMaxAgeHours
  );
  const deterministicStructured = body.includes(
    "> 生成方式：程序核验基础版 + 结构化分析覆盖层"
  );
  let inStructuredAnalysis = false;
  const normalizedBody = freshnessLinesWithInheritedWatchlistIdentity(body, context)
    .map(({ originalLine, matchingLine }) => {
    const trimmed = originalLine.trim();
    if (deterministicStructured && /^##\s+6\.\s*结构化分析覆盖层\s*$/.test(trimmed)) {
      inStructuredAnalysis = true;
      return originalLine;
    }
    if (inStructuredAnalysis && /^##\s+7\./.test(trimmed)) {
      inStructuredAnalysis = false;
    }
    if (inStructuredAnalysis) return originalLine;
    if (!trimmed || trimmed.startsWith("#") || trimmed.startsWith(">")) return originalLine;
    const matches = matchingFreshnessRules(matchingLine, rules);
    if (matches.length === 0) return originalLine;
    if (!matches.some((rule) => rule.stale)) {
      // 程序持有时效判定权：生成阶段误加的 [陈旧] 必须在这里纠正，
      // 否则随后的一致性校验会以"新鲜数据被标陈旧"让整份晨报失败。
      return originalLine.includes("[陈旧]") ? removeStaleLabelsFromLine(originalLine) : originalLine;
    }
    if (matches.some((rule) => !rule.stale)) return originalLine;
    const newsOnly = matches.every((rule) => rule.kind === "news");
    if (newsOnly) {
      return addLineLevelStaleLabel(originalLine);
    }
    const hasNumericClaim = [...originalLine.matchAll(NUMERIC_CLAIM_PATTERN)].length > 0 ||
      /(?:收盘|前收|最新价|股价|点位)\s*[+-]?\d/.test(originalLine);
    if (hasNumericClaim) return addStaleLabelsToLine(originalLine);
    if (matches.some((rule) => rule.kind === "news") && !hasLineLevelStaleLabel(originalLine)) {
      return addLineLevelStaleLabel(originalLine);
    }
    return originalLine;
  }).join("\n");
  return sourceStart >= 0 ? normalizedBody + sources : normalizedBody;
}

function validateContextFreshnessLabels(
  report,
  context,
  cutoffInstant,
  marketDataMaxAgeHours,
  newsMaxAgeHours
) {
  const sourceStart = report.lastIndexOf("## 来源");
  const body = sourceStart >= 0 ? report.slice(0, sourceStart) : report;
  const rules = buildFreshnessRules(
    context,
    cutoffInstant,
    marketDataMaxAgeHours,
    newsMaxAgeHours
  );
  const analyzedLines = [];
  const deterministicStructured = body.includes(
    "> 生成方式：程序核验基础版 + 结构化分析覆盖层"
  );
  let inStructuredAnalysis = false;
  let lineNumber = 0;
  for (const { originalLine, matchingLine } of freshnessLinesWithInheritedWatchlistIdentity(body, context)) {
    lineNumber += 1;
    const line = originalLine.trim();
    if (deterministicStructured && /^##\s+6\.\s*结构化分析覆盖层\s*$/.test(line)) {
      inStructuredAnalysis = true;
      continue;
    }
    if (inStructuredAnalysis && /^##\s+7\./.test(line)) {
      inStructuredAnalysis = false;
    }
    if (inStructuredAnalysis) continue;
    if (!line || line.startsWith("#") || line.startsWith(">")) continue;
    const matches = matchingFreshnessRules(matchingLine, rules);
    if (matches.length === 0) continue;
    const stale = matches.some((rule) => rule.stale);
    const fresh = matches.some((rule) => !rule.stale);
    const newsOnly = matches.every((rule) => rule.kind === "news");
    const numericClaims = [...line.matchAll(NUMERIC_CLAIM_PATTERN)];
    analyzedLines.push({
      originalLine,
      line,
      lineNumber,
      matches,
      stale,
      fresh,
      newsOnly,
      numericClaims
    });
  }

  const mixedLines = analyzedLines
    .filter(({ stale, fresh, numericClaims, line }) => (
      stale && fresh && (numericClaims.length > 0 || line.includes("[陈旧]"))
    ))
    .map(({ originalLine, lineNumber: issueLineNumber }) => ({
      lineNumber: issueLineNumber,
      line: originalLine
    }));
  if (mixedLines.length > 0) {
    const error = new Error(
      "同一行混合了新鲜与陈旧项目，必须分行标记: " +
      mixedLines[0].line.trim().slice(0, 100) +
      (mixedLines.length > 1 ? `（共 ${mixedLines.length} 行）` : "")
    );
    error.reportIssue = Object.freeze({
      kind: "mixed_freshness_lines",
      lines: Object.freeze(mixedLines.map((item) => Object.freeze(item)))
    });
    throw error;
  }

  for (const {
    originalLine,
    line,
    matches,
    stale,
    fresh,
    newsOnly,
    numericClaims
  } of analyzedLines) {
    if (fresh && !stale && line.includes("[陈旧]")) {
      throw new Error("未超过自身时效阈值的数据不得标为陈旧: " + line.slice(0, 100));
    }
    if (stale && !fresh) {
      if (newsOnly) {
        if (!hasLineLevelStaleLabel(originalLine)) {
          throw new Error("超过新闻时效阈值的报道未在行首标记陈旧: " + line.slice(0, 100));
        }
      } else if (numericClaims.length > 0) {
        const labelCount = (line.match(/\[陈旧\]/g) ?? []).length;
        if (labelCount < numericClaims.length) {
          throw new Error("超过自身时效阈值的数据未逐项标记陈旧: " + line.slice(0, 100));
        }
      } else if (matches.some((rule) => rule.kind === "news") && !line.includes("[陈旧]")) {
        throw new Error("超过新闻时效阈值的报道未标记陈旧: " + line.slice(0, 100));
      }
    }
  }
}

function parseReportNumber(value) {
  return Number(String(value).replaceAll(",", ""));
}

function escapeReportPattern(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function explicitWatchlistIdentitiesInLine(line, watchlist) {
  return watchlist.filter((item) => {
    const code = String(item?.code ?? "").trim();
    const name = String(item?.name ?? "").trim();
    const hasCode = code && new RegExp(
      `(?:^|[^\\p{L}\\p{N}])${escapeReportPattern(code)}(?=$|[^\\p{L}\\p{N}])`,
      "u"
    ).test(line);
    const hasName = name && companyNameHasSubjectOccurrence(line, name);
    return hasCode || hasName;
  });
}

function lineHasSpecificContextNewsTitle(line, context) {
  return (context?.data?.announcementsAndNews ?? []).some((item) =>
    newsRuleHasSpecificTitleMatch(line, {
      kind: "news",
      nameLabel: typeof item?.name === "string" ? item.name.trim() : null,
      titleLabel: typeof item?.title === "string" ? item.title.trim() : null
    })
  );
}

function splitLocalPriceClaimClauses(line) {
  const clauses = [];
  let start = 0;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    const isStrongSeparator = /[。；;，、]/.test(character);
    const isAsciiCommaSeparator = character === "," &&
      !(/\d/.test(line[index - 1] ?? "") && /\d/.test(line[index + 1] ?? ""));
    if (!isStrongSeparator && !isAsciiCommaSeparator) continue;
    const text = line.slice(start, index).trim();
    if (text) clauses.push(text);
    start = index + 1;
  }
  const remainder = line.slice(start).trim();
  if (remainder) clauses.push(remainder);
  return clauses;
}

function localMetricClaimContext(clause, metricIndex, metricEnd) {
  const subjectBoundaries = [...clause.matchAll(/(?:并且|同时|以及|且|但|[（(])/g)];
  const precedingBoundary = subjectBoundaries
    .filter((match) => match.index < metricIndex)
    .at(-1);
  const start = precedingBoundary
    ? precedingBoundary.index + precedingBoundary[0].length
    : 0;
  return clause.slice(start, metricEnd).trim();
}

function validateWatchlistPriceClaimValues(report, context) {
  const watchlist = context?.data?.watchlist ?? [];
  if (watchlist.length === 0) return;
  const sourceStart = report.lastIndexOf("## 来源");
  const body = sourceStart >= 0 ? report.slice(0, sourceStart) : report;
  const number = "([+-]?(?:\\d{1,3}(?:,\\d{3})+|\\d+)(?:\\.\\d+)?)";
  const unit = "(元(?:\\s*/\\s*股)?|[%％]|点|个百分点)?";
  const claimEnd = "\\s*(?:左右|附近|上下)?\\s*(?=$|[，,。；;、（()）\\[\\]】])";
  const metricPatterns = [
    { key: "close", label: "收盘", unit: "yuan", pattern: new RegExp(`(?:收盘价|收盘|报收|收于)\\s*(?:约为|为|约|[：:])?\\s*${number}\\s*${unit}${claimEnd}`, "g") },
    { key: "previousClose", label: "前收", unit: "yuan", pattern: new RegExp(`前收\\s*(?:约为|为|约|[：:])?\\s*${number}\\s*${unit}${claimEnd}`, "g") },
    { key: "change", label: "涨跌额", unit: "yuan", pattern: new RegExp(`涨跌(?:额)?(?!幅)\\s*(?:约为|为|约|[：:])?\\s*${number}\\s*${unit}${claimEnd}`, "g") },
    { key: "changePercent", label: "涨跌幅", unit: "percent", pattern: new RegExp(`涨跌幅\\s*(?:约为|为|约|[：:])?\\s*${number}\\s*${unit}${claimEnd}`, "g") },
    { key: "changePercent", label: "上涨", unit: "percent", direction: "up", pattern: new RegExp(`(?:上涨|收涨|涨(?!跌|幅))\\s*(?:约为|为|约|[：:])?\\s*${number}\\s*${unit}${claimEnd}`, "g") },
    { key: "changePercent", label: "下跌", unit: "percent", direction: "down", pattern: new RegExp(`(?:下跌|收跌|(?<!涨)跌(?!幅))\\s*(?:约为|为|约|[：:])?\\s*${number}\\s*${unit}${claimEnd}`, "g") },
    { key: "changePercent", label: "涨幅", unit: "percent", direction: "up", pattern: new RegExp(`涨幅\\s*(?:约为|为|约|[：:])?\\s*${number}\\s*${unit}${claimEnd}`, "g") },
    { key: "changePercent", label: "跌幅", unit: "percent", direction: "down", pattern: new RegExp(`(?<!涨)跌幅\\s*(?:约为|为|约|[：:])?\\s*${number}\\s*${unit}${claimEnd}`, "g") }
  ];

  for (const { originalLine, inheritedIdentity } of freshnessLinesWithInheritedWatchlistIdentity(body, context)) {
    const line = originalLine.trim();
    if (!line || line.startsWith("#") || line.startsWith(">")) continue;

    const plainLine = line.replace(/[*_`]/g, "");
    const lineIdentities = explicitWatchlistIdentitiesInLine(plainLine, watchlist);
    const unambiguousLineIdentity = lineIdentities.length === 1
      ? lineIdentities[0]
      : null;
    for (const clause of splitLocalPriceClaimClauses(plainLine)) {
      const claims = [];
      for (const metric of metricPatterns) {
        metric.pattern.lastIndex = 0;
        for (const match of clause.matchAll(metric.pattern)) {
          claims.push({
            metric,
            reported: parseReportNumber(match[1]),
            reportedUnit: match[2] ?? "",
            localContext: localMetricClaimContext(
              clause,
              match.index,
              match.index + match[0].length
            )
          });
        }
      }
      if (claims.length === 0) continue;
      const clauseIdentities = explicitWatchlistIdentitiesInLine(clause, watchlist);
      const unambiguousClauseIdentity = clauseIdentities.length === 1
        ? clauseIdentities[0]
        : null;
      for (const { metric, reported, reportedUnit, localContext } of claims) {
        if (
          WATCHLIST_NON_PRICE_CAPITAL_TERM.test(localContext) ||
          WATCHLIST_OTHER_MARKET_SUBJECT_TERM.test(localContext) ||
          COMPANY_FINANCIAL_TERM.test(localContext) ||
          lineHasSpecificContextNewsTitle(localContext, context) ||
          hasAffirmativeNewsEventAssertion(localContext) ||
          /(?:若|如果|假如|可能|预计|预期|情景|观察条件|阈值|假设|能否|是否)/.test(localContext)
        ) continue;

        const localIdentities = explicitWatchlistIdentitiesInLine(localContext, watchlist);
        if (localIdentities.length > 1) {
          throw new Error("个股行情局部子句包含多个或冲突的股票身份: " + localContext.slice(0, 100));
        }
        if (
          localIdentities.length === 0 &&
          clauseIdentities.length > 1 &&
          lineIdentities.length > 1
        ) {
          throw new Error("个股行情数字无法从多个股票身份中唯一归属: " + localContext.slice(0, 100));
        }
        const identity = localIdentities[0] ??
          unambiguousClauseIdentity ??
          unambiguousLineIdentity ??
          inheritedIdentity;
        if (!identity) continue;
        const marketData = identity.marketData ?? identity;
        const expectedValues = {
          close: marketData?.last ?? marketData?.close,
          previousClose: marketData?.previousClose,
          change: marketData?.change,
          changePercent: marketData?.changePercent
        };
        const normalizedUnit = reportedUnit.replace(/\s+/g, "");
        const hasExpectedUnit = metric.unit === "yuan"
          ? normalizedUnit === "元" || normalizedUnit === "元/股"
          : normalizedUnit === "%" || normalizedUnit === "％";
        if (!hasExpectedUnit) {
          throw new Error(
            `${identity.code} ${identity.name} 正文中的${metric.label}数字缺少单位或单位错误`
          );
        }
        const expected = Number(expectedValues[metric.key]);
        if (!Number.isFinite(expected)) {
          throw new Error(`${identity.code} ${identity.name} 固定数据包缺少${metric.label}，不能核验正文数字`);
        }
        const agrees = metric.direction === "down"
          ? expected < 0 && valuesAgree(Math.abs(reported), Math.abs(expected), 0.02)
          : metric.direction === "up"
            ? expected > 0 && reported >= 0 && valuesAgree(reported, expected, 0.02)
            : valuesAgree(reported, expected, 0.02);
        if (!agrees) {
          throw new Error(
            `${identity.code} ${identity.name} 正文中的${metric.label}数字与固定数据包不一致`
          );
        }
      }
    }
  }
}

function validateCoreIndexReportClaims(report, context) {
  const sourceStart = report.lastIndexOf("## 来源");
  const body = sourceStart >= 0 ? report.slice(0, sourceStart) : report;
  const numberValue = "[+-]?[\\d,]+(?:\\.\\d+)?";
  const number = `(${numberValue})`;
  const stale = "(?:\\s*\\[陈旧\\])?";
  const entityTokens = [...new Set([
    ...CORE_INDEX_IDENTITIES.map((item) => item.name),
    ...(context?.data?.watchlist ?? []).flatMap((item) => [item?.code, item?.name])
  ].filter(Boolean))];
  const coreIndexNamePattern = CORE_INDEX_IDENTITIES
    .map((item) => escapeReportPattern(item.name))
    .join("|");
  const markdownEmphasis = "(?:\\*{1,2}|_{1,2})?";
  const groupedSeparator = "(?:与|和|、|,|，)";
  const groupedIndexToken = `${markdownEmphasis}(?:${coreIndexNamePattern})${markdownEmphasis}`;
  const groupedIndexListSuffix = new RegExp(
    `(${groupedIndexToken}(?:\\s*${groupedSeparator}\\s*${groupedIndexToken})+)\\s*$`,
    "u"
  );
  const groupedPercentValue = `${numberValue}\\s*[%％]${stale}`;
  const groupedPercentValues = new RegExp(
    `^\\s*(${groupedPercentValue}(?:\\s*${groupedSeparator}\\s*${groupedPercentValue})*)`,
    "u"
  );
  const groupedPercentValueCapture = new RegExp(`(${numberValue})\\s*[%％]${stale}`, "gu");
  const groupedDirection = /(?:分别\s*(收涨|上涨|涨|收跌|下跌|跌)|((?:涨|跌)幅)\s*分别\s*(?:为|是|[：:])?)/gu;
  const groupedIndexNameCapture = new RegExp(coreIndexNamePattern, "gu");
  const parseGroupedIndexPercentClaims = (line) => {
    const claims = [];
    for (const directionMatch of line.matchAll(groupedDirection)) {
      const beforeDirection = line.slice(0, directionMatch.index);
      const listMatch = beforeDirection.match(groupedIndexListSuffix);
      if (!listMatch) continue;
      const identities = [...listMatch[1].matchAll(groupedIndexNameCapture)]
        .map((match) => match[0]);
      const valuesStart = directionMatch.index + directionMatch[0].length;
      const valuesMatch = line.slice(valuesStart).match(groupedPercentValues);
      const reportedValues = valuesMatch
        ? [...valuesMatch[1].matchAll(groupedPercentValueCapture)].map((match) => match[1])
        : [];
      if (
        identities.length < 2 ||
        identities.length > 3 ||
        new Set(identities).size !== identities.length ||
        reportedValues.length !== identities.length
      ) {
        throw new Error("核心指数“分别”句必须列出 2 或 3 个不同指数及等量百分比");
      }
      claims.push({
        start: listMatch.index,
        end: valuesStart + valuesMatch[0].length,
        identities,
        direction: directionMatch[1] ?? directionMatch[2],
        reportedValues
      });
    }
    return claims;
  };
  const contextCoreIndices = context?.data?.previousAShareSession?.indices ?? [];
  const coreIndicesByName = new Map(CORE_INDEX_IDENTITIES.map((identity) => [
    identity.name,
    contextCoreIndices.find(
      (item) => item?.code === identity.code || item?.name === identity.name
    )
  ]));
  for (const line of body.split(/\r?\n/)) {
    for (const groupedClaim of parseGroupedIndexPercentClaims(line)) {
      const directionIsDown = groupedClaim.direction.includes("跌");
      for (let offset = 0; offset < groupedClaim.identities.length; offset += 1) {
        const name = groupedClaim.identities[offset];
        const reportedText = groupedClaim.reportedValues[offset];
        const expectedValue = Number(coreIndicesByName.get(name)?.changePercent);
        const reported = parseReportNumber(reportedText);
        const agrees = directionIsDown
          ? expectedValue < 0 && valuesAgree(Math.abs(reported), Math.abs(expectedValue), 0.02)
          : expectedValue > 0 && reported >= 0 && valuesAgree(reported, expectedValue, 0.02);
        if (!agrees) {
          throw new Error(`${name} 正文中的${directionIsDown ? "下跌" : "上涨"}数字与固定数据包不一致`);
        }
      }
    }
  }
  for (const identity of CORE_INDEX_IDENTITIES) {
    const index = context?.data?.previousAShareSession?.indices?.find(
      (item) => item?.code === identity.code || item?.name === identity.name
    );
    if (!index) continue;
    const identityLines = body.split(/\r?\n/).filter((item) => item.includes(identity.name));
    const fixedLines = identityLines.filter(
      (item) => item.includes(identity.name) && /收盘/.test(item) && /前收/.test(item)
    );
    if (fixedLines.length === 0) {
      throw new Error(`正式稿必须按固定格式列出 ${identity.name} 的收盘、前收、涨跌额和涨跌幅`);
    }
    const pattern = new RegExp(
      `收盘\\s*${number}\\s*点${stale}[^\\n]{0,24}前收\\s*${number}\\s*点${stale}` +
      `[^\\n]{0,24}涨跌\\s*${number}\\s*点${stale}[^\\n]{0,12}[（(]\\s*${number}\\s*[%％]${stale}\\s*[）)]`
    );
    const expected = [index.close, index.previousClose, index.change, index.changePercent];
    for (const line of fixedLines) {
      const match = line.match(pattern);
      if (!match) {
        throw new Error(`正式稿的 ${identity.name} 未使用“收盘/前收/涨跌/涨跌幅”固定格式`);
      }
      const actual = match.slice(1, 5).map(parseReportNumber);
      if (actual.some((value, offset) => !valuesAgree(value, expected[offset], 0.02))) {
        throw new Error(`${identity.name} 核心数字与固定数据包不一致`);
      }
      const reportExpectedChange = actual[0] - actual[1];
      const reportExpectedPercent = reportExpectedChange / actual[1] * 100;
      if (!valuesAgree(actual[2], reportExpectedChange) || !valuesAgree(actual[3], reportExpectedPercent, 0.02)) {
        throw new Error(`${identity.name} 报告内收盘、前收、涨跌额或涨跌幅算术不一致`);
      }
    }

    const escapedIdentity = escapeReportPattern(identity.name);
    const metricPatterns = [
      { key: "close", label: "收盘", pattern: new RegExp(`(?:收盘报|报收|收报|收于|报于|收盘|报)\\s*(?:为|[：:])?\\s*${number}\\s*点`, "g") },
      { key: "previousClose", label: "前收", pattern: new RegExp(`前收\\s*(?:为|[：:])?\\s*${number}\\s*点`, "g") },
      { key: "change", label: "涨跌", pattern: new RegExp(`涨跌(?!幅)\\s*(?:为|[：:])?\\s*${number}\\s*点`, "g") },
      { key: "changePercent", label: "涨跌幅", pattern: new RegExp(`涨跌幅\\s*(?:为|[：:])?\\s*${number}\\s*[%％]`, "g") },
      { key: "changePercent", label: "涨幅", pattern: new RegExp(`涨幅\\s*(?:为|[：:])?\\s*${number}\\s*[%％]`, "g") },
      { key: "changePercent", label: "跌幅", direction: "down", pattern: new RegExp(`(?<!涨)跌幅\\s*(?:为|[：:])?\\s*${number}\\s*[%％]`, "g") },
      { key: "changePercent", label: "上涨", direction: "up", pattern: new RegExp(`(?:上涨|收涨)\\s*(?:为|[：:])?\\s*${number}\\s*[%％]`, "g") },
      { key: "changePercent", label: "下跌", direction: "down", pattern: new RegExp(`(?:下跌|收跌)\\s*(?:为|[：:])?\\s*${number}\\s*[%％]`, "g") },
      { key: "change", label: "上涨", direction: "up", pattern: new RegExp(`(?:上涨|收涨|涨(?!跌|幅))\\s*(?:为|[：:])?\\s*${number}\\s*点`, "g") },
      { key: "change", label: "下跌", direction: "down", pattern: new RegExp(`(?:下跌|收跌|(?<!涨)跌(?!幅))\\s*(?:为|[：:])?\\s*${number}\\s*点`, "g") },
      { key: "changePercent", label: "上涨", direction: "up", pattern: new RegExp(`涨(?!跌|幅)\\s*(?:为|[：:])?\\s*${number}\\s*[%％]`, "g") },
      { key: "changePercent", label: "下跌", direction: "down", pattern: new RegExp(`(?<!涨)跌(?!幅)\\s*(?:为|[：:])?\\s*${number}\\s*[%％]`, "g") },
      { key: "changePercent", label: "涨跌幅", pattern: new RegExp(`^${escapedIdentity}\\s*[：:]?\\s*${number}\\s*[%％]`, "g") }
    ];
    for (const line of identityLines) {
      let individualLine = line;
      const groupedClaims = parseGroupedIndexPercentClaims(line)
        .sort((left, right) => right.start - left.start);
      for (const groupedClaim of groupedClaims) {
        individualLine = individualLine.slice(0, groupedClaim.start) +
          individualLine.slice(groupedClaim.end);
      }
      let searchFrom = 0;
      while (searchFrom < individualLine.length) {
        const identityStart = individualLine.indexOf(identity.name, searchFrom);
        if (identityStart < 0) break;
        const afterIdentity = identityStart + identity.name.length;
        let claimEnd = individualLine.length;
        for (const token of entityTokens) {
          const next = individualLine.indexOf(token, afterIdentity);
          if (next >= 0 && next < claimEnd) claimEnd = next;
        }
        const claimSpan = individualLine.slice(identityStart, claimEnd);
        for (const metric of metricPatterns) {
          metric.pattern.lastIndex = 0;
          for (const match of claimSpan.matchAll(metric.pattern)) {
            const reported = parseReportNumber(match[1]);
            const expectedValue = Number(index[metric.key]);
            let agrees;
            if (metric.direction === "down") {
              agrees = expectedValue < 0 && valuesAgree(Math.abs(reported), Math.abs(expectedValue), 0.02);
            } else if (metric.direction === "up") {
              agrees = expectedValue > 0 && reported >= 0 && valuesAgree(reported, expectedValue, 0.02);
            } else {
              agrees = valuesAgree(reported, expectedValue, 0.02);
            }
            if (!agrees) {
              throw new Error(`${identity.name} 正文中的${metric.label}数字与固定数据包不一致`);
            }
          }
        }
        searchFrom = afterIdentity;
      }
    }
  }
}

function hasCashFlowField(value) {
  if (!value || typeof value !== "object") return false;
  return Object.entries(value).some(([key, child]) => {
    if (/(?:cash.*flow|operating.*cash|net.*cash)/i.test(key) && child != null) return true;
    return child && typeof child === "object" && hasCashFlowField(child);
  });
}

function validateCashFlowClaims(report, context) {
  const watchlist = context?.data?.watchlist ?? [];
  if (watchlist.length === 0) return;
  const missingCashFlow = watchlist.filter((item) => !hasCashFlowField(item?.financials));
  if (missingCashFlow.length === 0) return;
  const sourceStart = report.lastIndexOf("## 来源");
  const body = sourceStart >= 0 ? report.slice(0, sourceStart) : report;
  const unsupportedClaim = /(?:健康|稳健|良好|充裕|紧张|承压|强劲|疲弱|偏强|偏弱|强于|弱于|优于|改善|恶化|增长|增强|下降|增加|减少|转正|转负|为正|为负|已触发|已经触发|触发条件|满足条件|不佳)/;
  const explicitUncertainty = /(?:无法|不能|不可|缺少|缺失|未提供|无数据|尚无|待核验|待验证|未观测|不具备|不作判断|不做判断|不得判断|是否)/;
  const explicitOtherMetricSubject = /(?:营收|营业收入|收入|归母净利(?:润)?|扣非净利(?:润)?|净利润|利润|毛利率|净利率|ROE|EPS|每股收益|订单|业绩|销量|产量|资产|负债)/i;
  const scopes = missingCashFlow.map((item) => {
    const label = `${item.code ?? "未知代码"} ${item.name ?? "未知股票"}`;
    const sectionLines = extractStockSection(body, item).split(/\r?\n/);
    const outsideLines = body.split(/\r?\n/).filter((line) =>
      line.includes("现金流") && (
        line.includes(String(item.code ?? "")) ||
        line.includes(String(item.name ?? "")) ||
        /(?:股票池|组合|个股|三只)/.test(line)
      )
    );
    const allMissingLines = missingCashFlow.length === watchlist.length
      ? body.split(/\r?\n/).filter((line) => line.includes("现金流"))
      : [];
    return { label, lines: [...new Set([...sectionLines, ...outsideLines, ...allMissingLines])] };
  });
  for (const scope of scopes) {
    for (const originalLine of scope.lines) {
      const line = originalLine.trim();
      if (!line.includes("现金流")) continue;
      for (const strongClause of line.split(/[。；;]/)) {
        let previousClauseHadCashFlow = false;
        let inheritsCashFlowSubject = false;
        const clauseTokens = strongClause.split(/([，,]|但是|不过|然而|反而|仍然|但|却|仍)/);
        for (const token of clauseTokens) {
          const clause = token.trim();
          if (!clause) continue;
          if (/^[，,]$/.test(clause)) {
            inheritsCashFlowSubject ||= previousClauseHadCashFlow;
            continue;
          }
          if (/^(?:但是|不过|然而|反而|仍然|但|却|仍)$/.test(clause)) {
            inheritsCashFlowSubject ||= previousClauseHadCashFlow;
            continue;
          }
          const hasCashFlowSubject = clause.includes("现金流");
          const hasOtherMetricSubject = !hasCashFlowSubject && explicitOtherMetricSubject.test(clause);
          const appliesToCashFlow = hasCashFlowSubject || (
            inheritsCashFlowSubject && !hasOtherMetricSubject
          );
          if (
            appliesToCashFlow &&
            unsupportedClaim.test(clause) &&
            !explicitUncertainty.test(clause)
          ) {
            throw new Error(`${scope.label} 缺少现金流字段，不得宣称现金流条件已触发或判断强弱: ${line.slice(0, 100)}`);
          }
          previousClauseHadCashFlow = hasCashFlowSubject;
          inheritsCashFlowSubject = false;
        }
      }
    }
  }
}

function extractStockSection(report, item) {
  const heading = new RegExp(
    `^###\\s+[^\\n]*(?:${String(item.code).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}|${String(item.name).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})[^\\n]*$`,
    "m"
  );
  const match = heading.exec(report);
  if (!match) return "";
  const tail = report.slice(match.index);
  const next = tail.slice(match[0].length).search(/\n(?:###\s+|##\s+5\.)/);
  return next < 0 ? tail : tail.slice(0, match[0].length + next);
}

function validateUnavailableWatchlistSemantics(report, context, cutoffInstant) {
  for (const item of context?.data?.watchlist ?? []) {
    if (validWatchlistMarketData(item, context, cutoffInstant)) continue;
    const section = extractStockSection(report, item);
    if (!section) continue;
    if (!/(?:研究线索|待验证|待核验)/.test(section)) {
      throw new Error(`${item.code} 行情不可核验时只能标为研究线索/待验证`);
    }
    if (!/行情[^\n]{0,30}(?:缺失|不可用|待核验|无法核验|未提供)/.test(section)) {
      throw new Error(`${item.code} 必须明确披露上一交易日行情不可核验`);
    }
    if (/(?:买入|卖出|增仓|减仓|持仓建议|仓位建议|目标价|止损|看多|看空|超配|低配)/.test(section)) {
      throw new Error(`${item.code} 行情不可核验时不得给出交易或仓位判断`);
    }
    for (const line of section.split(/\r?\n/)) {
      if (/逻辑状态/.test(line) && !/(?:待验证|待核验|无法判断|研究线索)/.test(line)) {
        throw new Error(`${item.code} 行情不可核验时逻辑状态只能待验证`);
      }
      if (/(?:行情变化|上涨\/下跌原因|上涨原因|下跌原因)/.test(line) &&
        !/(?:缺失|不可用|待核验|无法核验|无法判断|未提供)/.test(line)) {
        throw new Error(`${item.code} 行情不可核验时不得判断价格表现或涨跌原因`);
      }
    }
  }
}

function validateStaleMarketLines(report) {
  const sourceStart = report.lastIndexOf("## 来源");
  const body = sourceStart >= 0 ? report.slice(0, sourceStart) : report;
  for (const originalLine of body.split(/\r?\n/)) {
    const line = originalLine.trim();
    if (
      !line ||
      line.startsWith("#") ||
      line.startsWith(">") ||
      /^[-|:\s]+$/.test(line) ||
      /^\|\s*(?:行情|资金|新闻|财务)\s*\|\s*(?:\d+(?:\.\d+)?\s*[%％]|不可计算)\s*\|/.test(line)
    ) {
      continue;
    }
    const valueText = line
      .replace(/\bS&P\s*500\b/gi, "")
      .replace(/标普\s*500|纳斯达克\s*100|日经\s*225/gi, "")
      .replace(/(?:沪深|中证|上证|深证|科创|创业板|恒生|富时中国A?)\s*\d{2,4}/gi, "")
      .replace(/\b\d{6}(?:\.(?:SH|SZ|BJ))?\b/gi, "")
      .replace(/\b(?:SH|SZ|BJ)\.?\d{6}\b/gi, "");
    const hasMarketTerm =
      /(?:涨|跌|成交|收盘|指数|资金|净流入|净流出|净买额|净卖出|买入额|卖出额|龙虎榜|点位|换手|振幅)/.test(valueText);
    const hasUnitlessMarketValue =
      /(?:收盘|最新价|股价|点位)\s*[+-]?\d[\d,]*(?:\.\d+)?/.test(valueText);
    const hasNumericValue =
      /[+-]?\d[\d,]*(?:\.\d+)?\s*(?:%|％|点|亿元|万亿元|元|万股|万手|手|家)/.test(valueText) ||
      hasUnitlessMarketValue;
    const isCompanyFinancial =
      /(?:营收|净利润|毛利率|业绩|分红|年报|季报|中报|Q[1-4]|H[12]|同比)/i.test(line);
    if (hasMarketTerm && hasNumericValue && !isCompanyFinancial) {
      const claimPattern = /[+-]?\d[\d,]*(?:\.\d+)?\s*(?:%|％|点|亿元|万亿元|元|万股|万手|手|家)/g;
      const numericClaims = [...valueText.matchAll(claimPattern)];
      for (const clause of valueText.split(/[，,；;。/|]/)) {
        const claimsInClause = [...clause.matchAll(claimPattern)].length;
        if (claimsInClause === 0) continue;
        const labelsInClause = (clause.match(/\[陈旧\]/g) ?? []).length;
        if (labelsInClause < claimsInClause) {
          throw new Error("超过时效阈值的行情数值未紧邻标记 [陈旧]: " + line.slice(0, 80));
        }
      }
      if (numericClaims.length === 0 && !line.includes("[陈旧]")) {
        throw new Error("超过时效阈值的行情未逐项标记 [陈旧]: " + line.slice(0, 80));
      }
    }
  }
}

function normalizeStaleMarketClaims(report) {
  const sourceStart = report.lastIndexOf("## 来源");
  const body = sourceStart >= 0 ? report.slice(0, sourceStart) : report;
  const sources = sourceStart >= 0 ? report.slice(sourceStart) : "";
  const claimPattern = /[+-]?\d[\d,]*(?:\.\d+)?\s*(?:%|％|点|亿元|万亿元|元|万股|万手|手|家)/g;
  const unitlessMarketPattern = /((?:收盘|最新价|股价|点位)\s*)([+-]?\d[\d,]*(?:\.\d+)?)(?![\d,.])(?!(?:\s*)(?:%|％|点|亿元|万亿元|元|万股|万手|手|家))/g;
  const normalizedBody = body
    .split(/\r?\n/)
    .map((originalLine) => {
      const line = originalLine.trim();
      if (
        !line ||
        line.startsWith("#") ||
        line.startsWith(">") ||
        /^[-|:\s]+$/.test(line) ||
        /^\|\s*(?:行情|资金|新闻|财务)\s*\|\s*(?:\d+(?:\.\d+)?\s*[%％]|不可计算)\s*\|/.test(line) ||
        /(?:营收|净利润|毛利率|业绩|分红|年报|季报|中报|Q[1-4]|H[12]|同比)/i.test(line)
      ) {
        return originalLine;
      }
      if (!/(?:涨|跌|成交|收盘|指数|资金|净流入|净流出|净买额|净卖出|买入额|卖出额|龙虎榜|点位|换手|振幅|股价)/.test(line)) {
        return originalLine;
      }
      let normalized = originalLine.replace(claimPattern, (claim, offset, whole) => {
        const before = whole.slice(Math.max(0, offset - 6), offset);
        const after = whole.slice(offset + claim.length, offset + claim.length + 6);
        return before.includes("[陈旧]") || after.trimStart().startsWith("[陈旧]")
          ? claim
          : claim + "[陈旧]";
      });
      normalized = normalized.replace(
        unitlessMarketPattern,
        (match, prefix, value, offset, whole) => {
          const after = whole.slice(offset + match.length, offset + match.length + 6);
          return after.trimStart().startsWith("[陈旧]") ? match : `${prefix}${value}[陈旧]`;
        }
      );
      normalized = normalized.replace(/(?:\s*\[陈旧\]){2,}/g, " [陈旧]");
      return normalized;
    })
    .join("\n");
  return sourceStart >= 0 ? normalizedBody + sources : normalizedBody;
}

export function normalizeReportOutput(output, {
  targetDate,
  disclaimer,
  cutoffLocal,
  cutoffInstant,
  publicationLevel,
  requireStaleMarketLabel = false,
  context = null,
  strictContextSources = false,
  marketDataMaxAgeHours = 18,
  newsMaxAgeHours = 72,
  publicationAudit = null,
  enforcePublicationReady = false,
  requireDeterministicCompilerOutput = false
}) {
  let report = output.replace(/\u001b\[[0-9;]*m/g, "").trim();
  const codeFence = String.fromCharCode(96).repeat(3);
  if (report.startsWith(codeFence + "markdown") && report.endsWith(codeFence)) {
    report = report.slice((codeFence + "markdown").length, -codeFence.length).trim();
  } else if (report.startsWith(codeFence) && report.endsWith(codeFence)) {
    report = report.slice(codeFence.length, -codeFence.length).trim();
  }
  report = mergeSourceUrlLines(report);
  report = normalizeMissingSourceAppendixUrls(
    report,
    context,
    strictContextSources,
    cutoffInstant,
    marketDataMaxAgeHours,
    newsMaxAgeHours
  );
  report = normalizeCoverageTerminology(report, publicationAudit);
  report = report.replace(/^>\s*发布级别[：:]([^\n]*)$/m, (line, value) => {
    const sameIgnoringWhitespace =
      value.trim().replace(/\s+/g, "") === publicationLevel.replace(/\s+/g, "");
    return sameIgnoringWhitespace ? `> 发布级别：${publicationLevel}` : line;
  });
  if (context) {
    report = normalizeContextFreshnessClaims(
      report,
      context,
      cutoffInstant,
      marketDataMaxAgeHours,
      newsMaxAgeHours
    );
  } else if (requireStaleMarketLabel) {
    report = normalizeStaleMarketClaims(report);
  }

  if (report.length < 80) {
    throw new Error("Pi 返回的晨报过短，未写入报告文件");
  }
  if (!report.includes(targetDate)) {
    throw new Error(`Pi 输出未包含目标日期 ${targetDate}，未写入报告文件`);
  }
  if (!/^#\s+(?:AStock\s*数据快报（语义未审）|.*A\s*股(?:晨报|基础数据快报))/im.test(report)) {
    throw new Error("输出缺少 AStock 数据快报或 A 股晨报标题，未写入报告文件");
  }
  const escapedCutoff = cutoffLocal.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&");
  if (!new RegExp("数据截止[^\\n]*" + escapedCutoff).test(report)) {
    throw new Error("Pi 输出未使用固定数据截止 " + cutoffLocal);
  }
  const requiredMarkers = [
    "数据截止",
    "数据状态",
    "交易日状态",
    "发布级别",
    "## 一句话结论",
    "## 1.",
    "## 2.",
    "## 3.",
    "## 4.",
    "## 5.",
    "## 6.",
    "## 7.",
    "## 来源"
  ];
  for (const marker of requiredMarkers) {
    if (!report.includes(marker)) {
      throw new Error(`Pi 输出缺少必要结构“${marker}”，未写入报告文件`);
    }
  }
  const publicationLevelLines = [...report.matchAll(/^>\s*发布级别[：:]\s*([^\n]+)$/gm)];
  if (publicationLevelLines.length !== 1) {
    throw new Error("报告必须恰好包含一条发布级别声明");
  }
  if (publicationLevelLines[0][1].trim() !== publicationLevel) {
    throw new Error("Pi 输出未使用固定发布级别 " + publicationLevel);
  }
  if (requireStaleMarketLabel) {
    if (!/^>.*陈旧数据/im.test(report) || !report.includes("[陈旧]")) {
      throw new Error("超过时效阈值的行情未在标题区和正文标记为陈旧");
    }
    if (!context) validateStaleMarketLines(report);
  }
  if (context) {
    validateContextFreshnessLabels(
      report,
      context,
      cutoffInstant,
      marketDataMaxAgeHours,
      newsMaxAgeHours
    );
    validateWatchlistPriceClaimValues(report, context);
    validateCashFlowClaims(report, context);
    validateUnavailableWatchlistSemantics(report, context, cutoffInstant);
  }
  if (enforcePublicationReady) {
    if (publicationAudit && !publicationAudit.passed) {
      throw new Error("程序发布门禁未通过，不能规范化为正式稿");
    }
    validateCoreIndexReportClaims(report, context);
  }
  validateForbiddenTechnicalLevels(report);
  validateForbiddenScenarioThresholds(report);
  validateNorthboundUnavailableSemantics(report, context);
  validateDragonTigerSemantics(report, context);
  validateMediaCannotConfirm(report, context);
  validateTurnoverScopeTerminology(report, context);
  validateNoInventedStockCompleteness(report);
  validateNumericComparisonSemantics(report);
  validatePreviousSessionDates(report, context);
  const validatedCandidateIds = validateContextSources(
    report,
    context,
    strictContextSources || requireDeterministicCompilerOutput,
    cutoffInstant,
    marketDataMaxAgeHours,
    newsMaxAgeHours
  );
  validateSourceTimes(report, cutoffInstant, context);
  if (!report.includes(disclaimer)) {
    report += `\n\n---\n\n${disclaimer}`;
  }
  const finalReport = report + "\n";
  if (requireDeterministicCompilerOutput) {
    validateDeterministicCompilerOutput(finalReport, {
      context,
      targetDate,
      cutoffLocal,
      cutoffInstant,
      publicationLevel,
      publicationAudit,
      disclaimer,
      marketDataMaxAgeHours,
      newsMaxAgeHours,
      validatedCandidateIds,
      requireStaleMarketLabel,
      strictContextSources
    });
  }
  return finalReport;
}
