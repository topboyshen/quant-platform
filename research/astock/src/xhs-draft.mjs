// Optional, local DRAFT_ONLY presentation. Mandatory seals have their own module.
import path from "node:path";
import { XhsDraftIntegrityError, integrity, isPlainObject, hash, validateIsoDate, readBoundedRegularFile, decodeUtf8, parseJsonBuffer, loadJsonConfig, stateEligibility, validateSuccessfulState, validateReportPath, validateAndParseReport, ensureDirectory, writeAtomicReplace, immutableFileState, publishImmutable, LAST_RUN_RELATIVE, MAX_STATE_BYTES } from "./research-artifacts.mjs";

// Preserve historical imports without making sealing depend on this writer.
export { buildResearchSeal } from "./research-seal.mjs";
export { XhsDraftIntegrityError } from "./research-artifacts.mjs";

const OUTPUT_ROOT_RELATIVE = "work/xhs";
const STATUS_RELATIVE = "work/xhs/last-run.json";
const MAX_STATUS_BYTES = 64 * 1024;
const ACTIONABLE_LANGUAGE = /(?:买入|卖出|加仓|减仓|增仓|仓位|目标价|止损|看多|看空|保证收益|收益承诺)/u;
const ABSTRACT_COVERAGE_LANGUAGE = /(?:数据桥模块.*评分|正式发布必要项覆盖|(?:行情|资金|新闻|财务)\s*[：:]\s*\d+(?:\.\d+)?%|X\/X)/u;
const CONCRETE_GAP_LANGUAGE = /(?:缺口|缺失|缺少|未提供|不可用|无法核验|未能.+核验|不完整|保持为空|不作补猜|不代表盘中实时)/u;

function assertInteger(value, fieldName, min, max) {
  if (!Number.isInteger(value) || value < min || value > max) {
    throw new Error(`${fieldName} 必须是 ${min}—${max} 的整数`);
  }
}

export function validateXhsConfig(config) {
  if (!isPlainObject(config) || config.schemaVersion !== "1.0") {
    throw new Error("config/xhs.json 缺少 schemaVersion 1.0");
  }
  if (config.draftEnabled !== true) throw new Error("XHS 草稿开关必须显式启用");
  if (config.autoPublishEnabled !== false) throw new Error("XHS 自动发布必须永久关闭");
  if (config.humanApprovalRequired !== true) throw new Error("XHS 草稿必须要求人工批准");
  if (config.outputRoot !== OUTPUT_ROOT_RELATIVE) {
    throw new Error(`XHS 输出目录必须固定为 ${OUTPUT_ROOT_RELATIVE}`);
  }
  assertInteger(config.maxReportBytes, "maxReportBytes", 1024, 1024 * 1024);
  assertInteger(config.maxTitleGraphemes, "maxTitleGraphemes", 8, 40);
  assertInteger(config.minBodyGraphemes, "minBodyGraphemes", 100, 1000);
  assertInteger(config.maxBodyGraphemes, "maxBodyGraphemes", 200, 2000);
  if (config.minBodyGraphemes > config.maxBodyGraphemes) {
    throw new Error("XHS 正文最小长度不得超过最大长度");
  }
  if (!Array.isArray(config.defaultTags) || config.defaultTags.length < 1 || config.defaultTags.length > 8) {
    throw new Error("XHS 默认标签必须包含 1—8 项");
  }
  const tags = config.defaultTags.map((tag) => {
    if (typeof tag !== "string" || !/^[\p{L}\p{N}_-]{1,16}$/u.test(tag)) {
      throw new Error("XHS 默认标签只能包含 1—16 个字母、数字、下划线或连字符");
    }
    return tag;
  });
  if (new Set(tags).size !== tags.length) throw new Error("XHS 默认标签不得重复");
  const sensitiveKey = Object.keys(config).find((key) => /token|cookie|secret|password|account|device|publishUrl/i.test(key));
  if (sensitiveKey) throw new Error(`XHS 配置不得包含敏感或发布字段: ${sensitiveKey}`);
  return config;
}

function extractSection(report, startHeading, endHeading) {
  const start = report.indexOf(startHeading);
  if (start < 0) return "";
  const contentStart = start + startHeading.length;
  const end = endHeading ? report.indexOf(endHeading, contentStart) : report.length;
  return report.slice(contentStart, end < 0 ? report.length : end).trim();
}

function normalizeFactLine(line) {
  let value = line.trim();
  if (!value || /^\|?\s*(?:---|类别\s*\|)/u.test(value)) return "";
  const tableRow = value.match(/^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$/u);
  if (tableRow) {
    value = `${tableRow[1].trim()}：${tableRow[2].trim()}`;
  }
  value = value
    .replace(/^#{1,6}\s+/u, "")
    .replace(/^[-*>]\s*/u, "")
    .replace(/^\[事实\]\s*/u, "")
    .replace(/\[来源\s+[1-9]\d*(?:、[1-9]\d*)*\]/gu, "")
    .replace(/\[陈旧\]/gu, "（陈旧）")
    .replace(/https?:\/\/\S+/giu, "")
    .replace(/[*_`]/gu, "")
    .replace(/\s+/gu, " ")
    .trim();
  if (!value || ACTIONABLE_LANGUAGE.test(value)) return "";
  return value;
}

function factLines(section, limit = 30) {
  const unique = [];
  for (const line of section.split(/\r?\n/)) {
    const normalized = normalizeFactLine(line);
    if (!normalized || unique.includes(normalized)) continue;
    unique.push(normalized);
    if (unique.length >= limit) break;
  }
  return unique;
}

function stockSummaryLines(section) {
  const summaries = [];
  let current = null;

  const finish = () => {
    if (!current) return;
    summaries.push(current.fact ? `${current.heading}：${current.fact}` : current.heading);
  };

  for (const rawLine of section.split(/\r?\n/)) {
    const heading = rawLine.match(/^#{3,6}\s+(\d{6}\s+\S.*?)\s*$/u);
    if (heading) {
      finish();
      const normalizedHeading = normalizeFactLine(rawLine);
      current = /^\d{6}\s+\S/u.test(normalizedHeading)
        ? { heading: normalizedHeading, fact: "" }
        : null;
      continue;
    }
    if (/^#{1,6}\s+/u.test(rawLine)) {
      finish();
      current = null;
      continue;
    }
    if (!current || current.fact || !/^\s*-\s+/u.test(rawLine)) continue;
    const fact = normalizeFactLine(rawLine);
    if (fact) current.fact = fact;
  }
  finish();
  return summaries;
}

function sectorFactLines(section, limit = 20) {
  const stockPoolStart = section.search(/^###\s+股票池\s*$/mu);
  const sectorOnly = stockPoolStart < 0 ? section : section.slice(0, stockPoolStart);
  return factLines(sectorOnly, limit).filter((line) => line !== "板块排行事实");
}

function concreteGapLines(sections, limit = 12) {
  const unique = [];
  for (const { section, requireConcreteLanguage } of sections) {
    for (const line of factLines(section, 100)) {
      if (ABSTRACT_COVERAGE_LANGUAGE.test(line) ||
          (requireConcreteLanguage && !CONCRETE_GAP_LANGUAGE.test(line)) ||
          unique.includes(line)) {
        continue;
      }
      unique.push(line);
      if (unique.length >= limit) return unique;
    }
  }
  return unique;
}

function graphemes(value) {
  return [...new Intl.Segmenter("zh-Hans", { granularity: "grapheme" }).segment(value)]
    .map((item) => item.segment);
}

function graphemeLength(value) {
  return graphemes(value).length;
}

function trimGraphemes(value, maximum) {
  const parts = graphemes(value);
  if (parts.length <= maximum) return value;
  return `${parts.slice(0, Math.max(0, maximum - 1)).join("")}…`;
}

function buildDraftContent(report, lastRun, config, disclaimer, sources) {
  const conclusion = factLines(extractSection(report, "## 一句话结论", "## 1. 数据覆盖与可用性"), 2);
  const marketHeadingMatch = report.match(/^## 2\. 上一交易日 A 股行情（(\d{4}-\d{2}-\d{2})）\s*$/m);
  const marketHeading = marketHeadingMatch?.[0];
  const marketDate = marketHeadingMatch?.[1];
  if (!marketHeading || !marketDate) {
    integrity("REPORT_CONTRACT_MISMATCH", "正式报告缺少可解析的上一交易日日期");
  }
  const parsedMarketDate = new Date(`${marketDate}T00:00:00Z`);
  if (Number.isNaN(parsedMarketDate.getTime()) ||
      parsedMarketDate.toISOString().slice(0, 10) !== marketDate ||
      marketDate >= lastRun.targetDate) {
    integrity("REPORT_CONTRACT_MISMATCH", "正式报告上一交易日日期必须有效且早于目标日期");
  }
  const market = marketHeading
    ? factLines(extractSection(report, marketHeading, "## 3."), 12)
    : [];
  const sectorAndStockSection = extractSection(report, "## 4. 板块与股票池", "## 5.");
  const sectors = sectorFactLines(sectorAndStockSection);
  const stockSummaries = stockSummaryLines(sectorAndStockSection);
  const gaps = concreteGapLines([
    {
      section: extractSection(report, "## 7. 风险提示与数据缺口", "## 来源"),
      requireConcreteLanguage: false
    },
    { section: sectorAndStockSection, requireConcreteLanguage: true }
  ]);
  const partialFallbackGap = "本次为部分数据；原报告未覆盖的字段保持缺失，不作补猜。";
  const requiredGap = lastRun.dataStatus === "partial" ? (gaps[0] ?? partialFallbackGap) : null;
  const optionalGaps = requiredGap === gaps[0] ? gaps.slice(1) : gaps;

  const monthDay = `${lastRun.targetDate.slice(5, 7)}月${lastRun.targetDate.slice(8, 10)}日`;
  const title = `A股数据快报｜${monthDay}`;
  if (graphemeLength(title) > config.maxTitleGraphemes) {
    integrity("CONTENT_CONTRACT_FAILED", "XHS 标题超过配置长度");
  }
  const dataLabel = lastRun.dataStatus === "live" ? "实时完整" : "部分可用";
  const requiredStart = [
    `数据截止：${lastRun.cutoffLocal}（Asia/Shanghai）`,
    `数据状态：${dataLabel}`,
    `【行情日期】以下行情均为 ${marketDate} 收盘数据。`,
    `【一句话结论】${conclusion[0] ?? "原报告只保留程序可复核事实，未生成因果判断。"}`,
    ...(requiredGap ? [`【数据缺口】${requiredGap}`] : [])
  ];
  const candidates = [
    ...market.map((line) => `【行情】${line}`),
    ...optionalGaps.map((line) => `【数据缺口】${line}`),
    ...sectors.map((line) => `【板块】${line}`)
  ];
  const requiredEnd = [
    `【来源】原报告列出 ${sources.length} 个带时间的固定数据来源，正文只重排已通过结构、来源与时效门禁的事实。`,
    "本草稿为基于固定数据包的程序化事实摘录，研究语义尚未由人工审校；不添加原因解释、价格预测或行动指令。",
    disclaimer
  ];
  const requiredStocks = stockSummaries.map((line) => `【股票池】${line}`);
  const requiredBody = [...requiredStart, ...requiredStocks, ...requiredEnd].join("\n\n");
  if (graphemeLength(requiredBody) > config.maxBodyGraphemes) {
    integrity("CONTENT_CONTRACT_FAILED", "XHS 正文上限不足以完整保留股票池摘要及必需披露");
  }

  const selected = [...requiredStart];
  for (const candidate of candidates) {
    const attempted = [...selected, candidate, ...requiredStocks, ...requiredEnd].join("\n\n");
    if (graphemeLength(attempted) <= config.maxBodyGraphemes) selected.push(candidate);
  }
  let body = [...selected, ...requiredStocks, ...requiredEnd].join("\n\n");
  if (graphemeLength(body) > config.maxBodyGraphemes) {
    integrity("CONTENT_CONTRACT_FAILED", "XHS 固定正文超过配置长度");
  }
  if (graphemeLength(body) < config.minBodyGraphemes) {
    integrity("CONTENT_CONTRACT_FAILED", "XHS 正文没有足够的可核验事实");
  }
  if (ACTIONABLE_LANGUAGE.test(body)) {
    integrity("CONTENT_CONTRACT_FAILED", "XHS 草稿包含交易行动语言");
  }
  body = trimGraphemes(body, config.maxBodyGraphemes);
  return { title, body };
}

async function ensureOutputDirectories(projectRoot, targetDate = null) {
  const work = path.join(projectRoot, "work");
  const output = path.join(work, "xhs");
  const drafts = path.join(output, "drafts");
  await ensureDirectory(work);
  await ensureDirectory(output);
  if (!targetDate) return output;
  await ensureDirectory(drafts);
  const dated = path.join(drafts, targetDate);
  await ensureDirectory(dated);
  return dated;
}

function statusRecord({ status, reasonCodes = [], targetDate = null, draftId = null, manifestFile = null, now }) {
  return {
    schemaVersion: "1.0",
    status,
    reasonCodes,
    targetDate,
    draftId,
    manifestFile,
    completedAt: now.toISOString()
  };
}

export async function writeXhsStatus(projectRoot, value) {
  const outputRoot = await ensureOutputDirectories(projectRoot);
  await writeAtomicReplace(path.join(outputRoot, "last-run.json"), `${JSON.stringify(value, null, 2)}\n`);
}

async function finishHeld(projectRoot, reasons, targetDate, now, dryRun) {
  const value = statusRecord({ status: "held", reasonCodes: [...new Set(reasons)], targetDate, now });
  if (!dryRun) await writeXhsStatus(projectRoot, value);
  return value;
}

export async function buildXhsDraft({
  projectRoot,
  desiredDate = null,
  requireCurrentDate = false,
  maxAgeMinutes = null,
  dryRun = false,
  now = new Date(),
  beforeCommit = null
} = {}) {
  if (typeof projectRoot !== "string" || !path.isAbsolute(projectRoot)) {
    throw new Error("projectRoot 必须是绝对路径");
  }
  if (desiredDate !== null) validateIsoDate(desiredDate, "--date");
  if (maxAgeMinutes !== null && (!Number.isInteger(maxAgeMinutes) || maxAgeMinutes < 1 || maxAgeMinutes > 24 * 60)) {
    throw new Error("--max-age-minutes 必须是 1—1440 的整数");
  }
  if (!(now instanceof Date) || Number.isNaN(now.getTime())) throw new Error("now 必须是有效时间");

  const xhsConfig = validateXhsConfig(await loadJsonConfig(projectRoot, "config/xhs.json", "XHS 配置"));
  const riskConfig = await loadJsonConfig(projectRoot, "config/risk.json", "风险配置");
  if (typeof riskConfig.disclaimer !== "string" || riskConfig.disclaimer.trim() === "") {
    throw new Error("风险配置缺少免责声明");
  }

  const lastRunPath = path.join(projectRoot, "work", "last-run.json");
  let stateSnapshot;
  try {
    stateSnapshot = await readBoundedRegularFile(lastRunPath, MAX_STATE_BYTES, "work/last-run.json");
  } catch (error) {
    if (error?.code === "ENOENT") {
      return finishHeld(projectRoot, ["LAST_RUN_MISSING"], desiredDate, now, dryRun);
    }
    throw error;
  }
  const lastRun = parseJsonBuffer(stateSnapshot.buffer, "work/last-run.json");
  if (typeof lastRun.targetDate === "string") validateIsoDate(lastRun.targetDate);
  const reasons = stateEligibility(lastRun, {
    desiredDate,
    requireCurrentDate,
    maxAgeMinutes,
    now
  });
  if (reasons.length > 0) {
    return finishHeld(projectRoot, reasons, lastRun.targetDate ?? desiredDate, now, dryRun);
  }
  validateSuccessfulState(lastRun);
  const reportPathCheck = validateReportPath(lastRun.reportFile, lastRun.targetDate);
  if (!reportPathCheck.eligible) {
    return finishHeld(projectRoot, [reportPathCheck.reason], lastRun.targetDate, now, dryRun);
  }

  const reportPath = path.join(projectRoot, "reports", "daily", `${lastRun.targetDate}.md`);
  let reportSnapshot;
  try {
    reportSnapshot = await readBoundedRegularFile(reportPath, xhsConfig.maxReportBytes, "正式晨报");
  } catch (error) {
    if (error?.code === "ENOENT") {
      return finishHeld(projectRoot, ["FORMAL_REPORT_MISSING"], lastRun.targetDate, now, dryRun);
    }
    throw error;
  }
  const report = decodeUtf8(reportSnapshot.buffer, "正式晨报");
  const { sources } = validateAndParseReport(report, lastRun, riskConfig.disclaimer.trim());
  const { title, body } = buildDraftContent(
    report,
    lastRun,
    xhsConfig,
    riskConfig.disclaimer.trim(),
    sources
  );
  const bodyBuffer = Buffer.from(body, "utf8");
  const bodySha256 = hash(bodyBuffer);
  const reportSha256 = reportSnapshot.sha256;
  const draftId = `astock-xhs-${lastRun.targetDate}-${reportSha256.slice(0, 16)}-v1`;
  const manifest = {
    schemaVersion: "astock.xhs-draft.v1",
    kind: "xhs_draft",
    draftId,
    idempotencyKey: `astock:xhs-draft:v1:${lastRun.targetDate}:${reportSha256}`,
    state: "DRAFT_ONLY",
    draftEligible: true,
    publishEligible: false,
    humanApprovalRequired: true,
    publishBlockers: ["AUTO_PUBLISH_DISABLED", "HUMAN_APPROVAL_REQUIRED"],
    targetDate: lastRun.targetDate,
    generatedAt: lastRun.completedAt,
    source: {
      lastRunFile: LAST_RUN_RELATIVE,
      lastRunSha256: stateSnapshot.sha256,
      reportFile: lastRun.reportFile,
      reportSha256,
      contextSha256: lastRun.contextSha256,
      dataStatus: lastRun.dataStatus,
      frozenCutoffInstant: lastRun.frozenCutoffInstant,
      cutoffLocal: lastRun.cutoffLocal,
      publicationGate: lastRun.publicationGate,
      evidenceReview: lastRun.evidenceReview,
      researchSemanticReview: lastRun.researchSemanticReview,
      semanticCandidateCount: lastRun.semanticCandidateCount ?? 0
    },
    contentMode: "facts_only",
    content: {
      title,
      body,
      tags: [...xhsConfig.defaultTags],
      cover: {
        status: "not_generated",
        headline: title,
        subtitle: `${lastRun.cutoffLocal}｜数据${lastRun.dataStatus === "live" ? "实时完整" : "部分可用"}`
      },
      sources,
      bodySha256,
      bodyBytes: bodyBuffer.length
    },
    disclaimer: riskConfig.disclaimer.trim()
  };
  const manifestBuffer = Buffer.from(`${JSON.stringify(manifest, null, 2)}\n`, "utf8");
  const markdownBuffer = Buffer.from([
    `# ${title}`,
    "",
    body,
    "",
    xhsConfig.defaultTags.map((tag) => `#${tag}`).join(" "),
    ""
  ].join("\n"), "utf8");

  if (typeof beforeCommit === "function") await beforeCommit();
  const [stateAfter, reportAfter] = await Promise.all([
    readBoundedRegularFile(lastRunPath, MAX_STATE_BYTES, "work/last-run.json"),
    readBoundedRegularFile(reportPath, xhsConfig.maxReportBytes, "正式晨报")
  ]);
  if (stateAfter.sha256 !== stateSnapshot.sha256 || reportAfter.sha256 !== reportSnapshot.sha256) {
    integrity("SOURCE_CHANGED_DURING_BUILD", "晨报或 last-run 在草稿构建期间发生变化");
  }

  const draftRelativeRoot = `work/xhs/drafts/${lastRun.targetDate}`;
  const manifestRelative = `${draftRelativeRoot}/${reportSha256}.json`;
  const markdownRelative = `${draftRelativeRoot}/${reportSha256}.md`;
  if (dryRun) {
    return {
      ...statusRecord({
        status: "draft_ready",
        targetDate: lastRun.targetDate,
        draftId,
        manifestFile: manifestRelative,
        now
      }),
      dryRun: true,
      manifest,
      markdownFile: markdownRelative
    };
  }

  const draftDirectory = await ensureOutputDirectories(projectRoot, lastRun.targetDate);
  const manifestPath = path.join(draftDirectory, `${reportSha256}.json`);
  const markdownPath = path.join(draftDirectory, `${reportSha256}.md`);
  const manifestState = await immutableFileState(manifestPath, manifestBuffer);
  const markdownState = await immutableFileState(markdownPath, markdownBuffer);
  await publishImmutable(manifestPath, manifestBuffer);
  await publishImmutable(markdownPath, markdownBuffer);
  const status = manifestState === "existing" && markdownState === "existing" ? "noop" : "draft_ready";
  const completed = statusRecord({
    status,
    targetDate: lastRun.targetDate,
    draftId,
    manifestFile: manifestRelative,
    now
  });
  await writeXhsStatus(projectRoot, completed);
  return { ...completed, markdownFile: markdownRelative, manifest };
}

export async function readXhsStatus(projectRoot) {
  const statusPath = path.join(projectRoot, "work", "xhs", "last-run.json");
  try {
    const snapshot = await readBoundedRegularFile(statusPath, MAX_STATUS_BYTES, STATUS_RELATIVE);
    return parseJsonBuffer(snapshot.buffer, STATUS_RELATIVE);
  } catch (error) {
    if (error?.code === "ENOENT") {
      return {
        schemaVersion: "1.0",
        status: "not_built",
        reasonCodes: ["STATUS_MISSING"],
        targetDate: null,
        draftId: null,
        manifestFile: null,
        completedAt: null
      };
    }
    throw error;
  }
}

export function safeFailureStatus(error, now = new Date()) {
  const reason = error instanceof XhsDraftIntegrityError
    ? error.code
    : "XHS_DRAFT_BUILD_FAILED";
  return statusRecord({ status: "failed", reasonCodes: [reason], now });
}
