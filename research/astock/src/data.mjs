import { access, readFile } from "node:fs/promises";
import { constants as fsConstants } from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";
import { validateResearchDataQuality } from "./research-data-quality.mjs";
import {
  readJsonFile,
  resolveDataBridgeDescriptor
} from "./config.mjs";

const VALID_STATUSES = new Set(["live", "partial", "demo", "unavailable"]);
const VALID_SOURCE_TYPES = new Set([
  "trading_calendar_data",
  "market_data",
  "industry_market_data",
  "dragon_tiger_list_aggregation",
  "financial_data_aggregation",
  "media_article",
  "media_aggregation",
  "official_announcement",
  "official_disclosure_query"
]);
const STOCK_IDENTITY_FIELDS = Object.freeze(["code", "name", "symbol", "exchange"]);
export const REQUIRED_DATA_SECTIONS = Object.freeze([
  "tradingCalendar",
  "overnightGlobal",
  "previousAShareSession",
  "policyAndCalendar",
  "sectors",
  "capitalFlows",
  "watchlist",
  "announcementsAndNews"
]);

export function buildDataRequest({ targetDate, stocks, workflow, now = new Date() }) {
  return {
    schemaVersion: "1.0",
    task: "a_share_morning_context",
    targetDate,
    timezone: workflow.timezone,
    requestedAt: now.toISOString(),
    universe: stocks.corePool.map(({ code, exchange, symbol, name }) => ({
      code,
      exchange,
      symbol,
      name
    })),
    requiredSections: [
      "trading_calendar",
      "overnight_global",
      "previous_a_share_session",
      "policy_and_calendar",
      "sectors",
      "capital_flows",
      "watchlist_market_data",
      "announcements_and_news"
    ],
    evidenceRequirements: {
      includeSourceId: true,
      includeDirectUrlWhenAvailable: true,
      includePublishedAt: true,
      includeDataAsOf: true
    }
  };
}

function emptySectionCompleteness() {
  return Object.fromEntries(REQUIRED_DATA_SECTIONS.map((section) => [section, 0]));
}

export function unavailableContext({ request, reason, now = new Date() }) {
  return {
    schemaVersion: "1.0",
    meta: {
      status: "unavailable",
      provider: "none",
      sourceType: "none",
      targetDate: request.targetDate,
      asOf: now.toISOString(),
      timezone: request.timezone,
      isLive: false,
      dataCompleteness: {
        market: 0,
        capital: 0,
        news: 0,
        financial: 0,
        overall: 0
      },
      warnings: [
        reason,
        "允许 Agent 在有联网检索工具时补充一手资料；仍无法核验的内容必须标记为缺失。"
      ]
    },
    data: {
      tradingCalendar: null,
      overnightGlobal: [],
      previousAShareSession: null,
      policyAndCalendar: [],
      sectors: [],
      capitalFlows: [],
      watchlist: [],
      announcementsAndNews: [],
      sectionCompleteness: emptySectionCompleteness()
    },
    sources: []
  };
}

function assertIsoDate(value, fieldName) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new Error(`${fieldName} 必须是 YYYY-MM-DD`);
  }
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) {
    throw new Error(`${fieldName} 不是有效日期`);
  }
}

function assertDateTime(value, fieldName) {
  const isoWithZone =
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/;
  if (typeof value !== "string" || !isoWithZone.test(value)) {
    throw new Error(`${fieldName} 必须是带时区的 ISO 8601 时间`);
  }
  if (Number.isNaN(Date.parse(value))) {
    throw new Error(`${fieldName} 不是有效时间`);
  }
}

function assertPercentage(value, fieldName) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 100) {
    throw new Error(`${fieldName} 必须是 0—100 的数值`);
  }
}

function getOverallCompleteness(meta, required) {
  const completeness = meta.dataCompleteness;
  if (completeness === undefined && !required) return null;
  if (typeof completeness === "number") {
    assertPercentage(completeness, "meta.dataCompleteness");
    return completeness;
  }
  if (!completeness || typeof completeness !== "object" || Array.isArray(completeness)) {
    throw new Error("meta.dataCompleteness 必须是 0—100 数值或含 overall 的对象");
  }
  for (const key of ["market", "capital", "news", "financial", "overall"]) {
    if (!(key in completeness)) {
      throw new Error(`meta.dataCompleteness 缺少 ${key}`);
    }
  }
  for (const [key, value] of Object.entries(completeness)) {
    assertPercentage(value, `meta.dataCompleteness.${key}`);
  }
  return completeness.overall;
}

function validateSectionCompleteness(context, required) {
  const completeness = context.data.sectionCompleteness ?? context.meta.sectionCompleteness;
  if (completeness === undefined && !required) return null;
  if (!completeness || typeof completeness !== "object" || Array.isArray(completeness)) {
    throw new Error("sectionCompleteness 必须是对象");
  }
  for (const section of REQUIRED_DATA_SECTIONS) {
    if (!(section in completeness)) {
      throw new Error(`sectionCompleteness 缺少 ${section}`);
    }
    assertPercentage(completeness[section], `sectionCompleteness.${section}`);
  }
  for (const key of Object.keys(completeness)) {
    if (!REQUIRED_DATA_SECTIONS.includes(key)) {
      throw new Error(`sectionCompleteness 包含未知区块 ${key}`);
    }
  }
  return completeness;
}

function validateDataTimes(value, location = "data") {
  if (Array.isArray(value)) {
    value.forEach((item, index) => validateDataTimes(item, `${location}[${index}]`));
    return;
  }
  if (!value || typeof value !== "object") return;
  for (const [key, child] of Object.entries(value)) {
    if (["dataAsOf", "publishedAt", "queriedAt", "observedAt"].includes(key) && child !== null) {
      assertDateTime(child, `${location}.${key}`);
    } else {
      validateDataTimes(child, `${location}.${key}`);
    }
  }
}

function collectSourceReferences(value, references, location = "data") {
  if (Array.isArray(value)) {
    value.forEach((item, index) => collectSourceReferences(item, references, `${location}[${index}]`));
    return;
  }
  if (!value || typeof value !== "object") return;

  if (Object.hasOwn(value, "sourceId")) {
    if (typeof value.sourceId !== "string" || value.sourceId.trim() === "") {
      throw new Error(`${location}.sourceId 必须是非空字符串`);
    }
    references.add(value.sourceId);
  }
  if (Object.hasOwn(value, "sourceIds")) {
    if (!Array.isArray(value.sourceIds)) {
      throw new Error(`${location}.sourceIds 必须是数组`);
    }
    const localIds = new Set();
    for (const sourceId of value.sourceIds) {
      if (typeof sourceId !== "string" || sourceId.trim() === "") {
        throw new Error(`${location}.sourceIds 只能包含非空字符串`);
      }
      if (localIds.has(sourceId)) {
        throw new Error(`${location}.sourceIds 不得包含重复 id: ${sourceId}`);
      }
      localIds.add(sourceId);
      references.add(sourceId);
    }
  }
  for (const [key, child] of Object.entries(value)) {
    if (key !== "sourceId" && key !== "sourceIds") {
      collectSourceReferences(child, references, `${location}.${key}`);
    }
  }
}

function validateSources(sources) {
  const sourceIds = new Set();
  for (const [index, source] of sources.entries()) {
    const location = `sources[${index}]`;
    if (!source || typeof source !== "object" || Array.isArray(source)) {
      throw new Error(`${location} 必须是对象`);
    }
    if (typeof source.id !== "string" || source.id.trim() === "") {
      throw new Error(`${location}.id 必须是非空字符串`);
    }
    if (sourceIds.has(source.id)) {
      throw new Error(`来源 id 重复: ${source.id}`);
    }
    sourceIds.add(source.id);
    if (typeof source.title !== "string" || source.title.trim() === "") {
      throw new Error(`${location}.title 必须是非空字符串`);
    }
    if (!VALID_SOURCE_TYPES.has(source.sourceType)) {
      throw new Error(`${location}.sourceType 缺失或不在固定证据类型白名单中`);
    }
    for (const field of ["publisher", "originalPublisher", "aggregator"]) {
      if (
        source[field] !== undefined &&
        source[field] !== null &&
        (typeof source[field] !== "string" || source[field].trim() === "")
      ) {
        throw new Error(`${location}.${field} 必须是 null 或非空字符串`);
      }
    }
    if (
      source.sourceType === "media_aggregation" &&
      (
        typeof source.aggregator !== "string" ||
        source.aggregator.trim() === "" ||
        !Object.hasOwn(source, "originalPublisher") ||
        !(
          source.originalPublisher === null ||
          (typeof source.originalPublisher === "string" && source.originalPublisher.trim() !== "")
        )
      )
    ) {
      throw new Error(
        `${location} 的 media_aggregation 必须声明 aggregator 与 originalPublisher（可为 null）`
      );
    }

    if (typeof source.url !== "string" || source.url.trim() === "") {
      throw new Error(`${location}.url 是来源记录的必填项`);
    }
    if (source.url !== undefined && source.url !== null) {
      let parsed;
      try {
        parsed = new URL(source.url);
      } catch {
        throw new Error(`${location}.url 不是有效 URL`);
      }
      if (!new Set(["http:", "https:"]).has(parsed.protocol)) {
        throw new Error(`${location}.url 只允许 http/https`);
      }
    }

    if (source.sourceType === "official_announcement" && source.timePrecision === "date") {
      assertIsoDate(source.disclosureDate, `${location}.disclosureDate`);
      assertDateTime(source.observedAt, `${location}.observedAt`);
      if (source.publishedAt || source.dataAsOf !== source.observedAt) {
        throw new Error(`${location} 日期精度公告不得伪造发布时间，采集时间必须一致`);
      }
    }
    const timeFields = ["publishedAt", "dataAsOf"].filter(
      (field) => source[field] !== undefined && source[field] !== null
    );
    if (timeFields.length === 0) {
      throw new Error(`${location} 至少需要 publishedAt 或 dataAsOf`);
    }
    for (const field of timeFields) {
      assertDateTime(source[field], `${location}.${field}`);
    }
  }
  return sourceIds;
}

function requiredStockIdentity(value, location) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${location} 必须是股票身份对象`);
  }
  const identity = {};
  for (const field of STOCK_IDENTITY_FIELDS) {
    if (typeof value[field] !== "string" || value[field].trim() === "") {
      throw new Error(`${location}.${field} 必须是非空字符串`);
    }
    identity[field] = value[field];
  }
  return identity;
}

function identityMap(items, location) {
  if (!Array.isArray(items)) throw new Error(`${location} 必须是数组`);
  const byCode = new Map();
  const symbols = new Set();
  items.forEach((item, index) => {
    const itemLocation = `${location}[${index}]`;
    const identity = requiredStockIdentity(item, itemLocation);
    if (byCode.has(identity.code)) {
      throw new Error(`${location} 包含重复股票代码: ${identity.code}`);
    }
    if (symbols.has(identity.symbol)) {
      throw new Error(`${location} 包含重复股票 symbol: ${identity.symbol}`);
    }
    byCode.set(identity.code, identity);
    symbols.add(identity.symbol);
  });
  return byCode;
}

function validateWatchlistIdentities(watchlist, expectedUniverse, active) {
  for (const [index, item] of watchlist.entries()) {
    const marketData = item?.marketData;
    if (marketData === null || marketData === undefined) continue;
    if (typeof marketData !== "object" || Array.isArray(marketData)) {
      throw new Error(`data.watchlist[${index}].marketData 必须是对象或 null`);
    }
    for (const field of ["code", "name"]) {
      if (typeof marketData[field] !== "string" || marketData[field].trim() === "") {
        throw new Error(
          `data.watchlist[${index}].marketData.${field} 必须是非空字符串`
        );
      }
      if (marketData[field] !== item?.[field]) {
        throw new Error(
          `data.watchlist[${index}].marketData.${field} 与父项身份不一致`
        );
      }
    }
    for (const field of ["symbol", "exchange"]) {
      if (Object.hasOwn(marketData, field) && marketData[field] !== item?.[field]) {
        throw new Error(
          `data.watchlist[${index}].marketData.${field} 与父项身份不一致`
        );
      }
    }
  }

  if (!active || expectedUniverse === undefined) return;
  const expectedByCode = identityMap(expectedUniverse, "expected.universe");
  const actualByCode = identityMap(watchlist, "data.watchlist");
  for (const [code, expectedIdentity] of expectedByCode) {
    const actualIdentity = actualByCode.get(code);
    if (!actualIdentity) {
      throw new Error(`data.watchlist 缺少请求股票: ${code} ${expectedIdentity.name}`);
    }
    for (const field of STOCK_IDENTITY_FIELDS) {
      if (actualIdentity[field] !== expectedIdentity[field]) {
        throw new Error(
          `data.watchlist 股票 ${code} 的 ${field} 与请求 universe 不一致`
        );
      }
    }
  }
  for (const [code, actualIdentity] of actualByCode) {
    if (!expectedByCode.has(code)) {
      throw new Error(`data.watchlist 包含请求之外的股票: ${code} ${actualIdentity.name}`);
    }
  }
}

export function validateMorningContext(context, expected = {}) {
  if (!context || typeof context !== "object" || Array.isArray(context)) {
    throw new Error("晨报数据响应必须是 JSON 对象");
  }
  if (context.schemaVersion !== "1.0") {
    throw new Error(`不支持的数据 schemaVersion: ${context.schemaVersion}`);
  }
  if (!VALID_STATUSES.has(context.meta?.status)) {
    throw new Error(`无效的数据状态: ${context.meta?.status}`);
  }
  if (typeof context.meta.provider !== "string" || context.meta.provider.trim() === "") {
    throw new Error("数据 meta 必须包含 provider");
  }
  assertIsoDate(context.meta.targetDate, "meta.targetDate");
  assertDateTime(context.meta.asOf, "meta.asOf");
  if (typeof context.meta.timezone !== "string" || context.meta.timezone.trim() === "") {
    throw new Error("数据 meta 必须包含 timezone");
  }
  if (typeof context.meta.isLive !== "boolean") {
    throw new Error("meta.isLive 必须是布尔值");
  }
  if (expected.targetDate && context.meta.targetDate !== expected.targetDate) {
    throw new Error(
      `数据目标日期不匹配: 期望 ${expected.targetDate}，实际 ${context.meta.targetDate}`
    );
  }
  if (expected.timezone && context.meta.timezone !== expected.timezone) {
    throw new Error(
      `数据时区不匹配: 期望 ${expected.timezone}，实际 ${context.meta.timezone}`
    );
  }
  if (!context.data || typeof context.data !== "object" || Array.isArray(context.data)) {
    throw new Error("数据响应必须包含 data 对象");
  }
  for (const section of REQUIRED_DATA_SECTIONS) {
    if (!Object.hasOwn(context.data, section)) {
      throw new Error(`数据响应缺少 data.${section}`);
    }
  }
  for (const section of [
    "overnightGlobal",
    "policyAndCalendar",
    "sectors",
    "capitalFlows",
    "watchlist",
    "announcementsAndNews"
  ]) {
    if (!Array.isArray(context.data[section])) {
      throw new Error(`data.${section} 必须是数组`);
    }
  }
  for (const section of ["tradingCalendar", "previousAShareSession"]) {
    const value = context.data[section];
    if (value !== null && (!value || typeof value !== "object" || Array.isArray(value))) {
      throw new Error(`data.${section} 必须是对象或 null`);
    }
  }
  const active = ["live", "partial"].includes(context.meta.status);
  validateWatchlistIdentities(context.data.watchlist, expected.universe, active);
  if (
    context.data.previousAShareSession !== null &&
    !context.data.previousAShareSession.dataAsOf
  ) {
    throw new Error("data.previousAShareSession 非空时必须包含 dataAsOf");
  }
  if (context.data.tradingCalendar !== null) {
    assertIsoDate(
      context.data.tradingCalendar.targetDate,
      "data.tradingCalendar.targetDate"
    );
    if (context.data.tradingCalendar.targetDate !== context.meta.targetDate) {
      throw new Error("data.tradingCalendar.targetDate 与 meta.targetDate 不一致");
    }
  }
  validateDataTimes(context.data);

  if (!Array.isArray(context.sources)) {
    throw new Error("数据响应必须包含 sources 数组");
  }
  const declaredSourceIds = validateSources(context.sources);
  const referencedSourceIds = new Set();
  collectSourceReferences(context.data, referencedSourceIds);
  for (const sourceId of referencedSourceIds) {
    if (!declaredSourceIds.has(sourceId)) {
      throw new Error(`数据引用了未声明的来源 id: ${sourceId}`);
    }
  }
  if (active && (declaredSourceIds.size === 0 || referencedSourceIds.size === 0)) {
    throw new Error("live/partial 数据必须至少包含一个已引用来源");
  }

  const completenessRequired = active || context.meta.status === "unavailable";
  const overall = getOverallCompleteness(context.meta, completenessRequired);
  const sectionCompleteness = validateSectionCompleteness(context, completenessRequired);
  if (context.meta.status === "live") {
    if (!context.meta.isLive || overall !== 100) {
      throw new Error("live 数据必须 isLive=true 且 dataCompleteness.overall=100");
    }
    if (
      typeof context.meta.dataCompleteness === "object" &&
      Object.values(context.meta.dataCompleteness).some((value) => value !== 100)
    ) {
      throw new Error("live 数据的各类 dataCompleteness 必须全部为 100");
    }
    if (Object.values(sectionCompleteness).some((value) => value !== 100)) {
      throw new Error("live 数据的 8 个 sectionCompleteness 必须全部为 100");
    }
  } else if (context.meta.status === "partial") {
    if (!(overall > 0 && overall < 100)) {
      throw new Error("partial 数据的 dataCompleteness.overall 必须大于 0 且小于 100");
    }
    const values = REQUIRED_DATA_SECTIONS.map((section) => sectionCompleteness[section]);
    if (!values.some((value) => value > 0) || !values.some((value) => value < 100)) {
      throw new Error("partial 数据的 sectionCompleteness 必须同时体现已有数据和缺口");
    }
  } else {
    if (context.meta.isLive) {
      throw new Error(`${context.meta.status} 数据不得标记 isLive=true`);
    }
    if (context.meta.status === "unavailable" && overall !== null && overall !== 0) {
      throw new Error("unavailable 数据的 dataCompleteness.overall 必须为 0");
    }
    if (
      context.meta.status === "unavailable" &&
      typeof context.meta.dataCompleteness === "object" &&
      Object.values(context.meta.dataCompleteness).some((value) => value !== 0)
    ) {
      throw new Error("unavailable 数据的各类 dataCompleteness 必须全部为 0");
    }
    if (
      context.meta.status === "unavailable" &&
      Object.values(sectionCompleteness).some((value) => value !== 0)
    ) {
      throw new Error("unavailable 数据的 8 个 sectionCompleteness 必须全部为 0");
    }
  }
  validateResearchDataQuality(context, expected);
  return context;
}

async function fileExists(filePath) {
  try {
    await access(filePath, fsConstants.R_OK);
    return true;
  } catch {
    return false;
  }
}

export function buildMinimalBridgeEnvironment() {
  const environment = {
    PYTHONIOENCODING: "utf-8",
    PYTHONUTF8: "1"
  };
  const allowedHostVariables = process.platform === "win32"
    ? ["SystemRoot", "WINDIR", "TEMP", "TMP"]
    : ["LANG", "LC_ALL", "TMPDIR"];
  for (const key of allowedHostVariables) {
    if (typeof process.env[key] === "string") environment[key] = process.env[key];
  }
  return environment;
}

function runDataBridge(descriptor, request, {
  cwd,
  timeoutMs,
  maxBytes = 10 * 1024 * 1024
}) {
  return new Promise((resolve, reject) => {
    const child = spawn(descriptor.executable, descriptor.args, {
      cwd,
      env: buildMinimalBridgeEnvironment(),
      shell: false,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"]
    });

    let stdout = "";
    let stderr = "";
    let settled = false;
    const finish = (callback) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      callback();
    };
    const timer = setTimeout(() => {
      finish(() => {
        child.kill();
        reject(new Error(`数据桥接超时（${timeoutMs} ms）`));
      });
    }, timeoutMs);

    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      if (Buffer.byteLength(stdout, "utf8") > maxBytes) {
        finish(() => {
          child.kill();
          reject(new Error("数据桥接响应超过 10 MB 限制"));
        });
      }
    });
    child.stderr.on("data", (chunk) => {
      if (Buffer.byteLength(stderr, "utf8") <= 64 * 1024) stderr += chunk;
    });
    child.on("error", (error) => {
      finish(() => reject(new Error(`无法启动数据桥接: ${error.message}`)));
    });
    child.on("close", (code) => {
      finish(() => {
        if (code !== 0) {
          reject(new Error(`数据桥接失败（exit ${code}）: ${stderr.trim()}`));
          return;
        }
        try {
          resolve(JSON.parse(stdout));
        } catch (error) {
          reject(new Error(`数据桥接未返回有效 JSON: ${error.message}`));
        }
      });
    });
    child.stdin.on("error", () => {
      // The close/error handlers above retain the bridge's primary failure.
    });
    child.stdin.end(JSON.stringify(request));
  });
}

export async function collectMorningContext({
  projectRoot,
  request,
  workflow,
  explicitInput
}) {
  let context;
  let loadedFrom;

  if (explicitInput) {
    const inputPath = path.isAbsolute(explicitInput)
      ? explicitInput
      : path.resolve(projectRoot, explicitInput);
    context = await readJsonFile(inputPath);
    loadedFrom = `explicit_input:${inputPath}`;
  } else {
    const descriptor = resolveDataBridgeDescriptor(projectRoot, workflow);
    context = await runDataBridge(descriptor, request, {
      cwd: projectRoot,
      timeoutMs: workflow.bridgeTimeoutMs
    });
    loadedFrom = `data_bridge:${descriptor.display}`;
  }

  validateMorningContext(context, {
    targetDate: request.targetDate,
    timezone: request.timezone,
    requestedAt: request.requestedAt,
    ...(["live", "partial"].includes(context?.meta?.status)
      ? { universe: request.universe }
      : {})
  });
  return { context, loadedFrom };
}

export async function readTextIfPresent(filePath) {
  if (!(await fileExists(filePath))) return null;
  return readFile(filePath, "utf8");
}
