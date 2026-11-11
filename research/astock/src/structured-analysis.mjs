import { createHash } from "node:crypto";

export const STRUCTURED_ANALYSIS_MAX_OUTPUT_BYTES = 64 * 1024;
export const STRUCTURED_ANALYSIS_SELECTION_LIMITS = Object.freeze({
  total: 8,
  summary: 1,
  theme: 3,
  scenario: 3,
  risk: 5,
  stockPerCode: 1
});
const MAX_TEXT_CHARS = 320;
const MAX_TITLE_CHARS = 48;

const ROOT_KEYS = new Set(["schemaVersion", "candidateIds"]);
const BLOCKED_OVERLAY_SOURCE_TYPES = new Set(["media_aggregation", "media_article"]);
const SUMMARY_DIVERGENCE_TEXT = "股指涨跌方向冲突，仍需观察反证";
const BREADTH_ADVANCE_TEXT = "上涨家数占优且成交额可核验，仍需继续核验";
const BREADTH_DECLINE_TEXT = "下跌家数占优且成交额可核验，仍需继续核验";
const STOCK_PROFIT_PRICE_TEXT = "单日行情与报告期利润同比方向不同，期间不同，不足以认定同期背离，仍需继续核验";
const STOCK_SECTOR_TEXT = "个股与已对齐板块的方向可能背离，仍需观察反证";
const CORE_ASHARE_INDICES = new Map([
  ["sh000001", "上证指数"],
  ["sz399001", "深证成指"],
  ["sz399006", "创业板指"]
]);
export const STRUCTURED_ANALYSIS_TEXT_CHOICES = Object.freeze({
  summary: Object.freeze([
    "股指涨跌方向冲突，仍需观察反证",
    "市场线索可能分化，仍需观察反证",
    "市场线索可能延续，仍需观察反证",
    "现有证据方向冲突，仍需继续核验"
  ]),
  theme_title: Object.freeze(["待验证主题", "观察主题", "风险主题"]),
  theme: Object.freeze([
    "上涨家数占优且成交额可核验，仍需继续核验",
    "下跌家数占优且成交额可核验，仍需继续核验",
    STOCK_SECTOR_TEXT,
    "若行业线索延续，仍需观察反向风险",
    "若行业线索转弱，需要重新核验证据",
    "现有行业证据冲突，仍需观察风险"
  ]),
  stock: Object.freeze([
    "单日行情与报告期利润同比方向不同，期间不同，不足以认定同期背离，仍需继续核验",
    STOCK_SECTOR_TEXT,
    "公司线索可能变化，财务风险仍待核验",
    "公司证据可能分化，仍需观察反证",
    "当前公司证据不足，仍需继续核验"
  ]),
  scenario: Object.freeze([
    "若行业线索转弱，需要重新观察风险",
    "若市场线索延续，仍需继续核验反证",
    "若证据方向变化，需要重新评估假设"
  ]),
  risk: Object.freeze([
    "来源覆盖存在不确定风险，仍需观察",
    "数据时效可能影响判断，仍需继续核验",
    "支持证据与反证可能冲突，仍需观察"
  ])
});
const STRUCTURED_ANALYSIS_TEXT_MAPS = new Map(
  Object.entries(STRUCTURED_ANALYSIS_TEXT_CHOICES).map(([role, values]) => [
    role,
    new Map(values.map((value) => [value.normalize("NFKC"), value]))
  ])
);
const UNCERTAINTY_PATTERN = /(?:可能|或有|若|如果|需|需要|待|观察|不确定|风险|假设|条件|尚不能|无法确认)/;
const FORBIDDEN_TEXT_PATTERN = /(?:https?:\/\/|买入|買入|卖出|賣出|加仓|加倉|减仓|減倉|仓位|倉位|目标价|目標價|止损|止損|止盈|做多|做空|开仓|開倉|建仓|建倉|清仓|清倉|平仓|平倉|持仓|持倉|持有|配置|增配|减配|減配|超配|低配|申购|申購|抄底|追涨|追漲|逃顶|逃頂|看多|看空|看涨|看漲|看跌|多头|多頭|空头|空頭|可考虑|可考慮|建议|建議|必然|保证|保證|肯定|无疑|無疑|势必|勢必|必将|必將|铁定|鐵定|稳赚|穩賺|确定上涨|確定上漲|确定下跌|確定下跌|已经证实|已經證實|已(?:经)?确认|已(?:經)?確認|确认无误|確認無誤|正式落地|坐实|坐實|板上钉钉|板上釘釘|核心供应商|核心供應商|直接受益)/i;
const PROMPT_INJECTION_PATTERN = /(?:(?:忽略|无视|無視|绕过|繞過|覆盖|覆蓋|改变|改變|取代|撤销|撤銷|跳过|跳過)[^。；，,]{0,20}(?:规则|規則|指令|提示|要求|约束|約束|门禁|門禁|系统|系統|开发者|開發者)|系统提示|系統提示|开发者消息|開發者訊息|隐藏提示|隱藏提示|内部指令|內部指令|提示词|提示詞|(?:调用|調用|使用|启用|啟用)[^。；，,]{0,12}(?:工具|命令|shell|终端|終端|浏览器|瀏覽器)|(?:执行|執行|运行|運行)[^。；，,]{0,12}(?:命令|代码|代碼|脚本|腳本)|(?:输出|輸出|返回|打印)[^。；，,]{0,12}(?:JSON|Markdown|代码|代碼|系统提示|系統提示)|(?:泄露|显示|顯示|透露)[^。；，,]{0,12}(?:秘密|密钥|密鑰|令牌|凭据|憑據|系统提示|系統提示)|\b(?:ignore|override|bypass|replace)\b[^.!?]{0,40}\b(?:instruction|prompt|rule|policy|system|developer)\b|\b(?:system prompt|developer message|call tools?|execute commands?|reveal secrets?)\b)/i;
const MARKDOWN_STRUCTURE_PATTERN = /(?:^|\n)\s*(?:#{1,6}|[-*+]\s|\d+\.\s|\|)|```|<\/?[a-z][^>]*>/i;
const ANY_NUMERIC_PATTERN = /[零〇一二两三四五六七八九十百千万亿兆]|\p{N}|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|percent|percentage|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\b/iu;
const FORMAL_CHINESE_NUMERIC_PATTERN = /[兩两壹贰貳叁參肆伍陆陸柒捌玖拾佰仟萬億廿卅卌圓圆點点半幺]/u;
const ROMAN_NUMERIC_PATTERN = /[\u2160-\u2188]|\b[ivxlcdm]+\b/iu;
const IMPLICIT_RATIO_PATTERN = /(?:翻倍|倍增|成倍|倍数|倍數|数倍|數倍|几倍|幾倍|对半|對半|过半|過半|半数|半數|折半)/u;
const INLINE_MARKDOWN_PATTERN = /[*_`#<>\[\]{}|~\\]/;
const CONTROL_OR_FORMAT_PATTERN = /[\r\n\u2028\u2029\p{Cc}\p{Cf}]/u;

export function containsNumericSemantics(value) {
  if (typeof value !== "string") return false;
  const text = value.normalize("NFKC");
  return ANY_NUMERIC_PATTERN.test(text) ||
    FORMAL_CHINESE_NUMERIC_PATTERN.test(text) ||
    ROMAN_NUMERIC_PATTERN.test(text) ||
    IMPLICIT_RATIO_PATTERN.test(text);
}

export function canonicalStructuredAnalysisText(role, value) {
  if (typeof value !== "string") return null;
  const normalized = value.normalize("NFKC");
  if (role === null || role === undefined) {
    for (const choices of STRUCTURED_ANALYSIS_TEXT_MAPS.values()) {
      const canonical = choices.get(normalized);
      if (canonical) return canonical;
    }
    return null;
  }
  return STRUCTURED_ANALYSIS_TEXT_MAPS.get(role)?.get(normalized) ?? null;
}

export function isAllowedStructuredAnalysisText(role, value) {
  return canonicalStructuredAnalysisText(role, value) !== null;
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function assertOnlyKeys(value, allowed, label) {
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw new Error(`${label} 包含不允许的字段: ${key}`);
  }
}

function compactIdentity(value) {
  return String(value ?? "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[\s\p{P}\p{S}]/gu, "");
}

function watchlistIdentityTerms(context) {
  const terms = [];
  for (const stock of context?.data?.watchlist ?? []) {
    for (const value of [
      stock?.code,
      stock?.symbol,
      stock?.name,
      stock?.marketData?.code,
      stock?.marketData?.symbol,
      stock?.marketData?.name
    ]) {
      const normalized = compactIdentity(value);
      if (normalized) terms.push(normalized);
    }
  }
  return [...new Set(terms)];
}

function normalizeText(
  value,
  label,
  maxChars = MAX_TEXT_CHARS,
  forbiddenIdentities = [],
  textRole = null
) {
  if (typeof value !== "string") throw new Error(`${label} 必须是字符串`);
  const text = value.normalize("NFKC").trim();
  if (text.length < 2 || text.length > maxChars) {
    throw new Error(`${label} 长度必须是 2—${maxChars} 字符`);
  }
  if (CONTROL_OR_FORMAT_PATTERN.test(text)) {
    throw new Error(`${label} 不得包含控制字符或不可见格式字符`);
  }
  if (MARKDOWN_STRUCTURE_PATTERN.test(text) || INLINE_MARKDOWN_PATTERN.test(text)) {
    throw new Error(`${label} 不得控制 Markdown 结构`);
  }
  if (containsNumericSemantics(text)) {
    throw new Error(`${label} 不得包含数字、数词或日期`);
  }
  if (FORBIDDEN_TEXT_PATTERN.test(text)) throw new Error(`${label} 包含禁止的确定性或交易表述`);
  if (PROMPT_INJECTION_PATTERN.test(text)) {
    throw new Error(`${label} 包含提示注入或命令式元指令`);
  }
  const compactText = compactIdentity(text);
  if (forbiddenIdentities.some((identity) => compactText.includes(identity))) {
    throw new Error(`${label} 不得包含股票池身份；公司身份只能由 code 绑定`);
  }
  if (!UNCERTAINTY_PATTERN.test(text)) {
    throw new Error(`${label} 必须使用可能、若、待核验、观察或风险等不确定性表述`);
  }
  const canonicalText = canonicalStructuredAnalysisText(textRole, text);
  if (!canonicalText) {
    throw new Error(`${label} 必须逐字使用程序固定的 ${textRole ?? "分析"} 模板`);
  }
  return canonicalText;
}

function sourceIdSet(context) {
  return new Set((context?.sources ?? []).map((source) => source?.id).filter((id) => typeof id === "string"));
}

function addSourceIds(target, value) {
  if (!value || typeof value !== "object") return;
  if (Array.isArray(value.sourceIds)) {
    for (const id of value.sourceIds) {
      if (typeof id === "string") target.add(id);
    }
  }
}

function stockEvidenceByCode(context) {
  const result = new Map();
  const watchlist = context?.data?.watchlist ?? [];
  for (const stock of watchlist) {
    if (!stock?.code) continue;
    const ids = new Set();
    addSourceIds(ids, stock);
    addSourceIds(ids, stock.marketData);
    addSourceIds(ids, stock.financials);
    result.set(String(stock.code), ids);
  }
  for (const item of context?.data?.announcementsAndNews ?? []) {
    const code = item?.code === undefined ? null : String(item.code);
    if (!code || !result.has(code)) continue;
    addSourceIds(result.get(code), item);
  }
  return result;
}

function normalizeEvidenceIds(value, label, allowedIds, { min = 1, max = 6 } = {}) {
  if (value === undefined && min === 0) return [];
  if (!Array.isArray(value) || value.length < min || value.length > max) {
    throw new Error(`${label} 必须包含 ${min}—${max} 个来源 ID`);
  }
  const normalized = value.map((id) => {
    if (typeof id !== "string" || id.length < 3 || id.length > 256) {
      throw new Error(`${label} 包含无效来源 ID`);
    }
    if (!allowedIds.has(id)) throw new Error(`${label} 引用了数据包之外的来源 ID: ${id}`);
    return id;
  });
  if (new Set(normalized).size !== normalized.length) throw new Error(`${label} 不得重复来源 ID`);
  return normalized;
}

function normalizeAnalysisItem(value, label, {
  allowedIds,
  requireTitle = false,
  requireCode = false,
  allowedCodes = null,
  stockIds = null,
  requireCounterEvidence = true,
  forbiddenIdentities = [],
  textRole = null,
  titleRole = null
}) {
  if (!isPlainObject(value)) throw new Error(`${label} 必须是对象`);
  assertOnlyKeys(value, new Set([
    "text",
    "evidenceIds",
    "counterEvidenceIds",
    ...(requireTitle ? ["title"] : []),
    ...(requireCode ? ["code"] : [])
  ]), label);
  const item = {
    text: normalizeText(
      value.text,
      `${label}.text`,
      MAX_TEXT_CHARS,
      forbiddenIdentities,
      textRole
    )
  };
  if (requireTitle) {
    item.title = normalizeText(
      value.title,
      `${label}.title`,
      MAX_TITLE_CHARS,
      forbiddenIdentities,
      titleRole
    );
  }
  if (requireCode) {
    if (typeof value.code !== "string" || !/^\d{6}$/.test(value.code) || !allowedCodes.has(value.code)) {
      throw new Error(`${label}.code 必须是股票池中的六位代码`);
    }
    item.code = value.code;
    const directStockIds = stockIds.get(value.code) ?? new Set();
    allowedIds = new Set([...directStockIds].filter((id) => allowedIds.has(id)));
  }
  item.evidenceIds = normalizeEvidenceIds(value.evidenceIds, `${label}.evidenceIds`, allowedIds);
  item.counterEvidenceIds = normalizeEvidenceIds(
    value.counterEvidenceIds,
    `${label}.counterEvidenceIds`,
    allowedIds,
    { min: requireCounterEvidence ? 1 : 0, max: 4 }
  );
  const overlappingIds = item.evidenceIds.filter((id) => item.counterEvidenceIds.includes(id));
  if (overlappingIds.length > 0) {
    throw new Error(`${label} 的支持证据与反证来源不得重叠: ${overlappingIds.join(", ")}`);
  }
  return item;
}

function stripSingleJsonFence(output) {
  const trimmed = output.trim();
  const match = trimmed.match(/^```(?:json)?\s*\n([\s\S]*?)\n```$/i);
  return match ? match[1].trim() : trimmed;
}

function localDatePart(value) {
  if (typeof value !== "string") return null;
  return value.match(/^(\d{4}-\d{2}-\d{2})T/)?.[1] ?? null;
}

function indexReturnPolarity(index) {
  const value = Number.isFinite(index?.changePercent)
    ? index.changePercent
    : Number.isFinite(index?.change)
      ? index.change
      : null;
  if (value === null || value === 0) return null;
  return value > 0 ? "positive" : "negative";
}

function sourceCatalog(context) {
  return new Map((context?.sources ?? [])
    .filter((source) => typeof source?.id === "string")
    .map((source) => [source.id, source]));
}

function isEligibleIndexSource(source, sessionDate) {
  if (!source || typeof source !== "object") return false;
  const sourceType = String(source.sourceType ?? "").trim().toLowerCase();
  if (BLOCKED_OVERLAY_SOURCE_TYPES.has(sourceType)) return false;
  if (sourceType !== "market_data") return false;
  return localDatePart(source.dataAsOf) === sessionDate;
}

function stableCandidateId(proofKind, payload) {
  const canonical = JSON.stringify({ proofKind, ...payload });
  const digest = createHash("sha256").update(canonical, "utf8").digest("hex").slice(0, 20);
  const prefix = proofKind === "same_session_opposite_ashare_index_returns"
    ? "summary:ashare-index-divergence"
    : proofKind === "previous_session_breadth_and_turnover"
      ? "theme:breadth-turnover"
      : proofKind === "watchlist_profit_yoy_versus_price"
        ? "stock:profit-price"
        : proofKind === "watchlist_versus_named_sector"
          ? "theme:named-sector"
          : "candidate";
  return `${prefix}:${payload.sessionDate}:${digest}`;
}

function freezeCandidate(candidate) {
  Object.freeze(candidate.evidenceIds);
  Object.freeze(candidate.counterEvidenceIds);
  if (candidate.evidenceRoles) Object.freeze(candidate.evidenceRoles);
  return Object.freeze(candidate);
}

// Recover only identities actually rendered in the fixed report. Shared source
// IDs alone cannot identify a company, and a theme's title is part of its proof.
export function structuredAnalysisCandidateSignature(candidate) {
  return JSON.stringify({
    kind: candidate.kind,
    title: candidate.kind === "theme" ? candidate.title : null,
    code: candidate.kind === "stock" ? candidate.code : null,
    text: candidate.text,
    evidenceIds: [...candidate.evidenceIds].toSorted(),
    counterEvidenceIds: [...candidate.counterEvidenceIds].toSorted()
  });
}

function uniqueRenderableCandidates(candidates) {
  const signatures = new Map();
  const ids = new Map();
  for (const candidate of candidates) {
    const signature = structuredAnalysisCandidateSignature(candidate);
    signatures.set(signature, (signatures.get(signature) ?? 0) + 1);
    ids.set(candidate.id, (ids.get(candidate.id) ?? 0) + 1);
  }
  // Do not arbitrarily select a proof when different proofs render identically.
  // Instructions, parsing and recovery must use the same unambiguous whitelist.
  return candidates.filter((candidate) =>
    signatures.get(structuredAnalysisCandidateSignature(candidate)) === 1 &&
    ids.get(candidate.id) === 1
  );
}

function validateSelectionCapacity(selected) {
  const counts = new Map();
  const stockCodes = new Set();
  for (const candidate of selected) {
    const count = (counts.get(candidate.kind) ?? 0) + 1;
    counts.set(candidate.kind, count);
    if (candidate.kind === "stock") {
      if (stockCodes.has(candidate.code)) {
        throw new Error("candidateIds 每只股票最多选择一个候选");
      }
      stockCodes.add(candidate.code);
    } else {
      const limit = STRUCTURED_ANALYSIS_SELECTION_LIMITS[candidate.kind];
      if (!Number.isInteger(limit) || count > limit) {
        throw new Error(`candidateIds 的 ${candidate.kind} 候选超过角色上限`);
      }
    }
  }
}

/**
 * Build the only facts the model is allowed to select. A candidate is emitted
 * only when two distinct, first-party context records prove opposite returns
 * for A-share indices in the same completed session. Titles and news prose are
 * deliberately ignored; media sources can never become formal overlay input.
 */
export function buildStructuredAnalysisCandidates(context) {
  const session = context?.data?.previousAShareSession;
  const sessionDate = session?.sessionDate;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(sessionDate ?? "") || !Array.isArray(session?.indices)) {
    return Object.freeze([]);
  }
  if (localDatePart(session.dataAsOf) !== sessionDate) return Object.freeze([]);

  const catalog = sourceCatalog(context);
  const polaritiesBySource = new Map();
  for (const index of session.indices) {
    if (CORE_ASHARE_INDICES.get(index?.code) !== index?.name) continue;
    const polarity = indexReturnPolarity(index);
    if (!polarity || localDatePart(index?.dataAsOf) !== sessionDate) continue;
    const priceIds = Object.hasOwn(index, "priceSourceIds")
      ? (Array.isArray(index.priceSourceIds) ? index.priceSourceIds : [])
      : index?.sourceIds ?? [];
    for (const id of priceIds) {
      if (typeof id !== "string" || !isEligibleIndexSource(catalog.get(id), sessionDate)) continue;
      if (!polaritiesBySource.has(id)) polaritiesBySource.set(id, new Set());
      polaritiesBySource.get(id).add(polarity);
    }
  }

  const evidenceIds = [];
  const counterEvidenceIds = [];
  for (const [id, polarities] of polaritiesBySource) {
    if (polarities.size !== 1) continue;
    if (polarities.has("positive")) evidenceIds.push(id);
    if (polarities.has("negative")) counterEvidenceIds.push(id);
  }
  evidenceIds.sort();
  counterEvidenceIds.sort();
  const candidates = [];
  if (evidenceIds.length > 0 && counterEvidenceIds.length > 0) {
    candidates.push(freezeCandidate({
      id: stableCandidateId("same_session_opposite_ashare_index_returns", {
        sessionDate,
        evidenceIds,
        counterEvidenceIds
      }),
      kind: "summary",
      proofKind: "same_session_opposite_ashare_index_returns",
      sessionDate,
      text: SUMMARY_DIVERGENCE_TEXT,
      evidenceIds,
      counterEvidenceIds
    }));
  }

  const breadth = session.marketBreadth;
  const turnoverIds = [...new Set((session.turnoverSourceIds ?? session.turnoverByExchange?.sourceIds ?? [])
    .filter((id) => typeof id === "string" && isEligibleIndexSource(catalog.get(id), sessionDate)))].toSorted();
  const breadthIds = [...new Set((breadth?.sourceIds ?? [])
    .filter((id) => typeof id === "string" && isEligibleIndexSource(catalog.get(id), sessionDate)))].toSorted();
  if (
    Number.isFinite(session.totalTurnover) && session.totalTurnover > 0 &&
    session?.units?.totalTurnover === "CNY" &&
    Number.isFinite(breadth?.advancers) && Number.isFinite(breadth?.decliners) &&
    breadth.advancers + breadth.decliners > 0 &&
    breadth.advancers !== breadth.decliners &&
    turnoverIds.length > 0 && breadthIds.length > 0 &&
    !turnoverIds.some((id) => breadthIds.includes(id))
  ) {
    candidates.push(freezeCandidate({
      id: stableCandidateId("previous_session_breadth_and_turnover", {
        sessionDate,
        evidenceIds: [...breadthIds, ...turnoverIds].toSorted(),
        counterEvidenceIds: []
      }),
      kind: "theme",
      proofKind: "previous_session_breadth_and_turnover",
      sessionDate,
      title: "观察主题",
      text: breadth.advancers > breadth.decliners ? BREADTH_ADVANCE_TEXT : BREADTH_DECLINE_TEXT,
      evidenceIds: [...breadthIds, ...turnoverIds].toSorted(),
      counterEvidenceIds: [],
      evidenceRoles: Object.fromEntries([
        ...breadthIds.map((id) => [id, breadth.advancers > breadth.decliners ? "上涨占优" : "下跌占优"]),
        ...turnoverIds.map((id) => [id, "成交额"])
      ])
    }));
  }

  const sectors = Array.isArray(context?.data?.sectors) ? context.data.sectors : [];
  for (const stock of context?.data?.watchlist ?? []) {
    const code = String(stock?.code ?? "");
    if (!/^\d{6}$/.test(code)) continue;
    const market = stock.marketData;
    const financials = stock.financials;
    const marketIds = [...new Set((market?.sourceIds ?? [])
      .filter((id) => typeof id === "string" && isEligibleIndexSource(catalog.get(id), sessionDate)))].toSorted();
    const financialIds = [...new Set((financials?.sourceIds ?? [])
      .filter((id) => typeof id === "string" && catalog.get(id)?.sourceType === "financial_data_aggregation"))].toSorted();
    const pricePolarity = indexReturnPolarity({
      changePercent: market?.changePercent,
      change: market?.change
    });
    const profitPolarity = indexReturnPolarity({ changePercent: financials?.parentNetProfitYoY });
    if (
      pricePolarity && profitPolarity && pricePolarity !== profitPolarity &&
      marketIds.length > 0 && financialIds.length > 0 &&
      financials?.units?.parentNetProfitYoY === "percentage_point" &&
      market?.units?.changePercent === "percentage_point"
    ) {
      candidates.push(freezeCandidate({
        id: stableCandidateId("watchlist_profit_yoy_versus_price", {
          sessionDate,
          code,
          evidenceIds: [...marketIds, ...financialIds].toSorted(),
          counterEvidenceIds: []
        }),
        kind: "stock",
        proofKind: "watchlist_profit_yoy_versus_price",
        sessionDate,
        code,
        text: STOCK_PROFIT_PRICE_TEXT,
        evidenceIds: [...marketIds, ...financialIds].toSorted(),
        counterEvidenceIds: [],
        evidenceRoles: Object.fromEntries([
          ...marketIds.map((id) => [id, "单日行情"]),
          ...financialIds.map((id) => [id, "报告期利润同比"])
        ]),
        periodNote: /^\d{4}-\d{2}-\d{2}$/.test(financials?.reportDate ?? "")
          ? `行情 ${sessionDate}；利润同比报告期 ${financials.reportDate}`
          : null
      }));
    }
    const sectorName = typeof stock.sector === "string" ? stock.sector : null;
    const sector = sectorName ? sectors.find((item) => item?.name === sectorName) : null;
    const sectorIds = [...new Set((sector?.sourceIds ?? [])
      .filter((id) => typeof id === "string" && catalog.get(id)?.sourceType === "industry_market_data" &&
        localDatePart(catalog.get(id)?.dataAsOf) === sessionDate))].toSorted();
    const sectorPolarity = indexReturnPolarity({
      changePercent: sector?.changePercent
    });
    if (
      sector && pricePolarity && sectorPolarity && pricePolarity !== sectorPolarity &&
      marketIds.length > 0 && sectorIds.length > 0 &&
      sector?.units?.changePercent === "percentage_point" &&
      market?.units?.changePercent === "percentage_point"
    ) {
      candidates.push(freezeCandidate({
        id: stableCandidateId("watchlist_versus_named_sector", {
          sessionDate,
          code,
          sectorName,
          evidenceIds: marketIds,
          counterEvidenceIds: sectorIds
        }),
        kind: "theme",
        proofKind: "watchlist_versus_named_sector",
        sessionDate,
        title: "观察主题",
        text: STOCK_SECTOR_TEXT,
        evidenceIds: marketIds,
        counterEvidenceIds: sectorIds
      }));
    }
  }

  return Object.freeze(uniqueRenderableCandidates(candidates));
}

function expandCandidates(selected) {
  const overlay = {
    schemaVersion: "1.0",
    themes: [],
    stocks: [],
    scenarios: [],
    risks: []
  };
  for (const candidate of selected) {
    const item = {
      text: candidate.text,
      evidenceIds: [...candidate.evidenceIds],
      counterEvidenceIds: [...candidate.counterEvidenceIds]
    };
    if (candidate.kind === "summary" && overlay.summary == null) {
      overlay.summary = item;
    } else if (candidate.kind === "theme") {
      overlay.themes.push({ title: candidate.title ?? "观察主题", ...item });
    } else if (candidate.kind === "stock") {
      overlay.stocks.push({ code: candidate.code, ...item });
    } else if (candidate.kind === "risk") {
      overlay.risks.push(item);
    } else if (candidate.kind === "summary") {
      overlay.themes.push({ title: "观察主题", ...item });
    } else {
      throw new Error("候选类型当前不可发布");
    }
  }
  return overlay;
}

export function parseStructuredAnalysisOutput(output, { context } = {}) {
  if (
    typeof output !== "string" ||
    Buffer.byteLength(output, "utf8") > STRUCTURED_ANALYSIS_MAX_OUTPUT_BYTES
  ) {
    throw new Error("结构化分析输出必须是 1—64 KiB 的 UTF-8 文本");
  }
  let value;
  try {
    value = JSON.parse(stripSingleJsonFence(output));
  } catch {
    throw new Error("结构化分析输出不是有效 JSON");
  }
  if (!isPlainObject(value)) throw new Error("结构化分析根节点必须是对象");
  assertOnlyKeys(value, ROOT_KEYS, "结构化分析");
  if (value.schemaVersion !== "2.0") throw new Error("结构化分析 schemaVersion 必须是 2.0");
  if (!Array.isArray(value.candidateIds) ||
      value.candidateIds.length > STRUCTURED_ANALYSIS_SELECTION_LIMITS.total) {
    throw new Error("candidateIds 必须是最多八项的数组");
  }
  if (value.candidateIds.length === 0) return null;
  if (new Set(value.candidateIds).size !== value.candidateIds.length) {
    throw new Error("candidateIds 不得重复");
  }
  const candidates = buildStructuredAnalysisCandidates(context);
  const selected = [];
  for (const candidateId of value.candidateIds) {
    if (typeof candidateId !== "string" || candidateId.length < 3 || candidateId.length > 256) {
      throw new Error("candidateIds 包含无效候选 ID");
    }
    const candidate = candidates.find((item) => item.id === candidateId);
    if (!candidate) throw new Error(`candidateIds 引用了程序候选之外的 ID: ${candidateId}`);
    selected.push(candidate);
  }
  validateSelectionCapacity(selected);
  selected.sort((left, right) => candidates.indexOf(left) - candidates.indexOf(right));
  return expandCandidates(selected);
}

export function buildStructuredAnalysisInstruction({ targetDate, cutoffLocal, context }) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(targetDate ?? "")) throw new Error("targetDate 无效");
  if (typeof cutoffLocal !== "string" || cutoffLocal.trim() === "") throw new Error("cutoffLocal 无效");
  const candidates = buildStructuredAnalysisCandidates(context).map((candidate) => ({
    candidateId: candidate.id,
    kind: candidate.kind,
    proofKind: candidate.proofKind,
    sessionDate: candidate.sessionDate,
    text: candidate.text,
    evidenceIds: candidate.evidenceIds,
    counterEvidenceIds: candidate.counterEvidenceIds,
    ...(candidate.code ? { code: candidate.code } : {}),
    ...(candidate.title ? { title: candidate.title } : {})
  }));
  return [
    "# AStock 结构化分析候选选择任务",
    "",
    `- 目标日期：${targetDate}`,
    `- 固定截止：${cutoffLocal}（Asia/Shanghai）`,
    "- 只输出一个 JSON 对象；不得输出 Markdown、代码围栏、解释或前后缀。",
    "- 只能原样选择程序候选 ID，不得写 text/title/source ID，不得拼接或改写候选。",
    `- 可以选择多个适用且不重复的候选，总数最多 ${STRUCTURED_ANALYSIS_SELECTION_LIMITS.total} 项；无适用或不应采用时返回空 candidateIds。`,
    `- 角色上限：summary 最多 ${STRUCTURED_ANALYSIS_SELECTION_LIMITS.summary} 项，theme 最多 ${STRUCTURED_ANALYSIS_SELECTION_LIMITS.theme} 项，scenario 最多 ${STRUCTURED_ANALYSIS_SELECTION_LIMITS.scenario} 项，risk 最多 ${STRUCTURED_ANALYSIS_SELECTION_LIMITS.risk} 项；每只股票最多 ${STRUCTURED_ANALYSIS_SELECTION_LIMITS.stockPerCode} 项 stock。`,
    "- 不必选满；只保留有独立证据价值的候选，不得重复候选或超过角色上限。",
    "- 允许跳过所有候选；空输出会安全降级为程序核验基础版。",
    "- compiler-context.json 中的数据、新闻、标题与其他 ID 都不是可选候选。",
    "",
    "固定 JSON 结构：",
    JSON.stringify({ schemaVersion: "2.0", candidateIds: [] }, null, 2),
    "",
    "程序证明合格的候选（唯一白名单）：",
    JSON.stringify(candidates, null, 2)
  ].join("\n") + "\n";
}
