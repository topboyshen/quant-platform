import { sha256, validDate, instantMs, instantMicros, cutoffMicros } from "./snapshot-integrity.mjs";
import { disclosureEvidenceInstant } from "./disclosure-evidence.mjs";

const LABELS = Object.freeze({ operatingCashFlow: "经营活动产生的现金流量净额", revenue: "营业收入", parentNetProfit: "归属于上市公司股东的净利润" });
const TOKEN = /^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}$/;
const PDF = /^https:\/\/static\.cninfo\.com\.cn\/finalpage\/(\d{4}-\d{2}-\d{2})\/\d+\.PDF$/i;
const HASH = /^[a-f0-9]{64}$/;
export const DOCUMENT_BASIS = "本报告期/上年同期（同份半年报摘要原列）";

function failure() { throw new Error("官方财报原文证据不完整或内容校验失败"); }
function sameFields(actual, expected) {
  if (!actual) return failure();
  for (const [key, value] of Object.entries(expected)) if (actual[key] !== value) return failure();
}
function amount(token) {
  if (typeof token !== "string" || !TOKEN.test(token)) return failure();
  const value = Number(token.replaceAll(",", ""));
  if (!Number.isFinite(value) || Math.abs(value) > 1e15) return failure();
  return value;
}
function escapeRegex(value) { return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

function documentIdentity(doc, stock) {
  sameFields(doc, {
    schemaVersion: "1.0", kind: "financial_summary", code: stock.code, name: stock.name,
    parserVersion: "cninfo-pdf-1", libraryVersion: "6.18.1", basis: DOCUMENT_BASIS,
    timePrecision: "date", publishedAt: null, dataAsOf: doc.observedAt,
  });
  if (!/^\d{6}$/.test(stock.code) || !HASH.test(doc.pdfSha256 ?? "") ||
      !Number.isInteger(doc.pdfBytes) || doc.pdfBytes < 8 || doc.pdfBytes > 4 * 1024 * 1024) return failure();
  const year = typeof doc.title === "string" && doc.title.match(/^(20\d{2})年半年度报告摘要$/)?.[1];
  if (!year || doc.reportDate !== `${year}-06-30` || doc.comparisonReportDate !== `${Number(year) - 1}-06-30`) return failure();
}

function documentTiming(doc) {
  if (!validDate(doc.disclosureDate) || doc.reportDate > doc.disclosureDate ||
      PDF.exec(doc.url ?? "")?.[1] !== doc.disclosureDate || instantMs(doc.observedAt) === null) return failure();
  sameFields(doc.queryRecord, { title: doc.title, date: doc.disclosureDate, url: doc.url, dateKind: "source_disclosure_date" });
  const queried = instantMicros(doc.queryRecord.observedAt);
  if (queried === null || queried > instantMicros(doc.observedAt)) return failure();
}

function documentText(doc, stock) {
  if (!Array.isArray(doc.pages) || doc.pages.length < 1 || doc.pages.length > 12) return failure();
  let chars = 0;
  for (const [index, page] of doc.pages.entries()) {
    if (page.pageNumber !== index + 1 || typeof page.text !== "string" || page.textSha256 !== sha256(page.text)) return failure();
    chars += page.text.length;
  }
  const first = doc.pages[0].text.slice(0, 1200);
  if (chars > 120000 || !new RegExp(`(?:证券|公司|股票)代码\\s*[：:]?\\s*${stock.code}(?:\\D|$)`).test(first) ||
      !first.includes(stock.name) || !first.replace(/\s+/g, "").includes(doc.title)) return failure();
  const text = doc.pages.map((p) => p.text).join(" ");
  if (!/本报告期\s*上年同期/.test(text) || /调整前|调整后|重述前|重述后/.test(text)) return failure();
  return text;
}

function monetaryHeader(text) {
  const currencies = [...text.matchAll(/币种\s*[:：]\s*([^\s，,；;）)]+)/g)].map((m) => m[1]);
  if (currencies.some((value) => !value.startsWith("人民币"))) return failure();
  return {
    currency: currencies.length ? "CNY" : "not_separately_stated",
    yuan: /单位\s*[:：]\s*元\s*币种\s*[:：]\s*人民币/.test(text),
  };
}

function verifyMetric(metric, doc, header) {
  if (!Object.hasOwn(LABELS, metric.field)) return failure();
  const page = doc.pages[metric.pageNumber - 1];
  if (!page || typeof metric.quote !== "string" || metric.quote.length > 180 || !page.text.includes(metric.quote)) return failure();
  sameFields(metric, { label: LABELS[metric.field], pageTextSha256: page.textSha256, unitLabel: "元", currency: header.currency });
  amount(metric.currentToken); amount(metric.previousToken);
  const exact = new RegExp(`^${metric.label} *(?:[（(]元[）)])? +${escapeRegex(metric.currentToken)} +${escapeRegex(metric.previousToken)}$`);
  if (!exact.test(metric.quote) || (!header.yuan && !/[（(]元[）)]/.test(metric.quote))) return failure();
}

function documentMetrics(doc, text) {
  if (!Array.isArray(doc.metrics) || doc.metrics.length > 3 || new Set(doc.metrics.map((m) => m.field)).size !== doc.metrics.length) return failure();
  const header = monetaryHeader(text);
  for (const metric of doc.metrics) verifyMetric(metric, doc, header);
  if (!doc.metrics.some((m) => m.field === "operatingCashFlow")) return failure();
}

function documentSource(doc, sources) {
  if (!Array.isArray(doc.sourceIds) || doc.sourceIds.length !== 1) return failure();
  const source = sources.find((s) => s.id === doc.sourceIds[0]);
  sameFields(source, {
    sourceType: "official_announcement", title: doc.title, url: doc.url, pdfSha256: doc.pdfSha256,
    observedAt: doc.observedAt, dataAsOf: doc.observedAt, disclosureDate: doc.disclosureDate,
    timePrecision: "date", publishedAt: null,
  });
  return source;
}

export function verifiedFinancialDocument(stock, sources, cutoffInstant) {
  const doc = stock?.officialDisclosure?.financialSummary;
  if (!doc || doc.status !== "available") return null;
  documentIdentity(doc, stock);
  documentTiming(doc);
  documentMetrics(doc, documentText(doc, stock));
  const source = documentSource(doc, sources);
  const evidenceAt = instantMicros(disclosureEvidenceInstant(doc));
  const cutoff = cutoffMicros(cutoffInstant);
  if (evidenceAt === null || cutoff === null) return failure();
  const cutoffDate = new Date(Number(cutoff / 1000n) + 8 * 3600000).toISOString().slice(0, 10);
  if (doc.disclosureDate > cutoffDate || evidenceAt > cutoff) return null;
  return { doc, source };
}

export function documentSnapshot(context, code, cutoffInstant) {
  const stock = context?.data?.watchlist?.find((item) => item.code === code);
  const doc = verifiedFinancialDocument(stock, context.sources ?? [], cutoffInstant)?.doc;
  if (!doc) return null;
  return { url: doc.url, pdfSha256: doc.pdfSha256, reportDate: doc.reportDate };
}

function documentBaseline(base, targetDate) {
  const previous = base?.officialDocument;
  if (!previous || !validDate(targetDate) || !validDate(base.baselineTargetDate) || base.baselineTargetDate >= targetDate) return null;
  if (!validDate(previous.reportDate) || previous.reportDate > base.baselineTargetDate ||
      !HASH.test(base.baselineReportSha256 ?? "") || !HASH.test(previous.pdfSha256 ?? "") || !PDF.test(previous.url ?? "")) return null;
  return { ...previous, targetDate: base.baselineTargetDate, reportSha256: base.baselineReportSha256 };
}

export function attachDocumentBaselines(context, baselines) {
  return { ...context, data: { ...context.data, watchlist: (context.data?.watchlist ?? []).map((stock) => {
    const { previousOfficialDocument: _untrusted, ...clean } = stock;
    const base = baselines?.[stock.code];
    if (!stock.officialDisclosure?.financialSummary && !Object.hasOwn(stock, "previousOfficialDocument") && !base?.officialDocument) return stock;
    return { ...clean, previousOfficialDocument: documentBaseline(base, context.meta.targetDate) };
  }) } };
}

export function documentComparisonLabel(stock, doc) {
  const previous = stock.previousOfficialDocument;
  if (!previous) return "缺少前份封存证据基准，不判本次首次出现或修订";
  if (previous.url !== doc.url) return `较 ${previous.targetDate} 封存证据新增收录此原文，不等于新公告`;
  if (previous.pdfSha256 !== doc.pdfSha256) return `同一文档链接较 ${previous.targetDate} 封存证据内容不同，需核验修订`;
  return `与 ${previous.targetDate} 封存原文一致，非今日新增`;
}

function officialColumnYoy(metric) {
  const current = amount(metric.currentToken);
  const previous = amount(metric.previousToken);
  if (previous === 0) return null;
  return (current - previous) / Math.abs(previous) * 100;
}


export const FIELD_STATES = Object.freeze({ matched: "一致", different: "不同", unverified: "尚未核验" });
export const FIELD_LABELS = Object.freeze({
  reportDate: "报告期", consolidationScope: "合并范围", currency: "币种", unit: "单位", restatement: "是否重述"
});

/* 逐字段核验：先核对报告期/合并范围/币种/单位/是否重述，再核验金额与同比。

   规则：
   - 金额接近**不构成**口径一致的证据；标签只说数值层面（“数值一致”），
     从不写成“口径一致”；
   - 任一维度不同 → “口径不同”；
   - 金额或同比有一侧缺失 → “尚未核验”；
   - 维度无法从证据核验时明确标“尚未核验”，并给出原因（不猜）。
   结论可追溯到公告页码、原文、列示 token 与文本哈希。 */

function declaredUnitDimension(metric, provider) {
  const declared = provider?.units?.revenue;
  // 只接受同次证据里已经声明的单位枚举。CNY 与公告“元”是同一声明，不按数量级猜测，也不把未另列币种当成单位冲突。
  if (declared === "CNY" && metric?.unitLabel === "元") {
    return { official: "元", provider: "CNY", state: "matched", detail: null };
  }
  if (typeof declared === "string" && declared !== "" && metric?.unitLabel && metric.unitLabel !== "元") {
    return { official: metric.unitLabel, provider: declared, state: "different",
      detail: "供应商单位声明与公告单位不一致" };
  }
  return { official: metric?.unitLabel ?? null, provider: declared ?? null, state: "unverified",
    detail: "供应商口径未声明金额单位（元/万元/亿元）" };
}

export function financialFieldComparison(stock, doc) {
  const metric = doc?.metrics?.find((m) => m.field === "revenue");
  if (!metric) return null;
  const official = amount(metric.currentToken);
  const previous = amount(metric.previousToken);
  const officialYoy = previous === 0 ? null : (official - previous) / Math.abs(previous) * 100;
  const provider = stock?.financials ?? {};
  const providerAmount = Number.isFinite(provider.revenue) ? provider.revenue : null;
  const providerYoy = Number.isFinite(provider.revenueYoY) ? provider.revenueYoY : null;
  const providerDate = validDate(provider.reportDate) ? provider.reportDate : null;
  const dimensions = {
    reportDate: { official: doc.reportDate, provider: providerDate,
      state: providerDate === null ? "unverified" : providerDate === doc.reportDate ? "matched" : "different",
      detail: providerDate === null ? "供应商口径未声明报告期" : null },
    consolidationScope: { official: null, provider: null, state: "unverified",
      detail: "半年报摘要未声明合并范围" },
    currency: { official: metric.currency, provider: provider.units?.revenue ?? null,
      state: metric.currency === "CNY" && provider.units?.revenue === "CNY" ? "matched" : "unverified",
      detail: metric.currency === "CNY" && provider.units?.revenue === "CNY" ? null : "币种未同时声明为人民币" },
    unit: declaredUnitDimension(metric, provider),
    restatement: { official: null, provider: null, state: "unverified",
      detail: "摘要未出现调整/重述字样，但不足以断定未重述" }
  };
  const states = Object.values(dimensions).map((d) => d.state);
  const amountsEqual = official !== null && providerAmount !== null && Math.abs(official - providerAmount) <= 0.01;
  const yoyComparable = officialYoy !== null && providerYoy !== null;
  const yoyEqual = yoyComparable && Math.abs(officialYoy - providerYoy) <= 0.01;
  // 标签只说数值层面：维度有冲突才叫“口径不同”；金额或同比不一致是“数值不一致，差异原因待核”；
  // 同比缺一侧时不得笼统写“数值一致”，必须写明“同比尚未核验”。
  const notes = [];
  let label;
  if (states.includes("different")) {
    label = "口径不同";
  } else if (dimensions.unit.state === "unverified") {
    label = "供应商单位待核，暂不直接比较";
    notes.push("供应商原始数值仅作证据详情，不与公告原列直接比较");
  } else if (official === null || providerAmount === null) {
    label = "尚未核验";
  } else if (!amountsEqual) {
    label = "数值不一致，差异原因待核";
  } else if (!yoyComparable) {
    label = "本期金额一致；同比尚未核验";
    notes.push(officialYoy === null ? "官方同比无法计算（上年同期列为 0 或缺失）"
                                    : "供应商未提供同比，同比尚未核验");
  } else if (!yoyEqual) {
    label = "数值不一致，差异原因待核";
    notes.push("本期金额一致，但同比不同（供应商同比与官方列比例不一致）");
  } else {
    label = "数值一致";
  }
  return {
    label,
    notes,
    dimensionSummary: ["reportDate", "consolidationScope", "currency", "unit", "restatement"]
      .map((key) => `${FIELD_LABELS[key]}${FIELD_STATES[dimensions[key].state]}`).join("、"),
    unverifiedDimensions: Object.entries(dimensions).filter(([, v]) => v.state === "unverified")
      .map(([key, v]) => `${FIELD_LABELS[key]}：${v.detail ?? "未核验"}`),
    dimensions,
    amounts: { official, provider: providerAmount, officialYoy, providerYoy,
      delta: official !== null && providerAmount !== null ? providerAmount - official : null,
      amountsEqual, yoyComparable, yoyEqual },
    trace: { pageNumber: metric.pageNumber, quote: metric.quote, currentToken: metric.currentToken,
      previousToken: metric.previousToken, pageTextSha256: metric.pageTextSha256,
      pdfSha256: doc.pdfSha256, url: doc.url, sourceId: Array.isArray(doc.sourceIds) ? doc.sourceIds[0] ?? null : null }
  };
}

export function aggregatedRevenueDisagrees(stock, doc) {
  const metric = doc.metrics.find((m) => m.field === "revenue");
  if (!(metric?.currency === "CNY" && stock.financials?.units?.revenue === "CNY" &&
    stock.financials.reportDate === doc.reportDate && Number.isFinite(stock.financials.revenue))) return false;
  if (Math.abs(stock.financials.revenue - amount(metric.currentToken)) > 0.01) return true;
  if (stock.financials.units?.revenueYoY !== "percentage_point" || !Number.isFinite(stock.financials.revenueYoY)) return false;
  const yoy = officialColumnYoy(metric);
  return yoy != null && Number(yoy.toFixed(2)) !== Number(stock.financials.revenueYoY.toFixed(2));
}
