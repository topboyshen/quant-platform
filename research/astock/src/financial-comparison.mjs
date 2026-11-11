import { canonicalJson, jsonHash, validDate } from "./snapshot-integrity.mjs";

const FIELDS = Object.freeze([
  "revenue", "revenueYoY", "parentNetProfit", "parentNetProfitYoY",
  "deductedNetProfit", "deductedNetProfitYoY", "grossMargin", "netMargin",
  "roeWeighted", "roeDiluted", "eps", "operatingCashFlow",
  "accountsReceivable", "inventory", "assetImpairment"
]);
const text = (value) => typeof value === "string" && value.trim() ? value.trim() : null;

export function financialComparableFields(financials) {
  if (!financials || !validDate(financials.reportDate)) return null;
  return {
    reportDate: financials.reportDate,
    values: Object.fromEntries(FIELDS.map((key) => [key,
      typeof financials[key] === "number" && Number.isFinite(financials[key]) ? financials[key] : null
    ])),
    units: Object.fromEntries(FIELDS.map((key) => [key, text(financials.units?.[key])])),
    revenueBasis: financials.revenueBasis ? Object.fromEntries(
      ["provider", "amountField", "yoyField", "verification"].map((key) => [key, text(financials.revenueBasis[key])])
    ) : null,
    version: {
      noticeAt: text(financials.noticeAt), updatedAt: text(financials.updatedAt),
      dataAsOf: text(financials.dataAsOf), seasonLabel: text(financials.seasonLabel),
      sourceIds: [...new Set((financials.sourceIds ?? []).filter((id) => typeof id === "string"))].sort()
    }
  };
}

function makeEntry(financials) {
  const fields = financialComparableFields(financials);
  return fields ? { reportDate: fields.reportDate, fields, fingerprint: jsonHash(fields) } : null;
}

function validEntry(entry) {
  if (!entry || !validDate(entry.reportDate) || entry.fields?.reportDate !== entry.reportDate) return false;
  if (!entry.fields.values || !entry.fields.units || !entry.fields.version) return false;
  try { return entry.fingerprint === jsonHash(entry.fields); } catch { return false; }
}

export function collectCurrentFinancialSnapshots(context) {
  const entries = {};
  for (const item of context?.data?.watchlist ?? []) {
    if (!/^\d{6}$/.test(item?.code ?? "")) continue;
    const entry = makeEntry(item.financials);
    if (entry && validDate(context.meta?.targetDate) && entry.reportDate <= context.meta.targetDate) entries[item.code] = entry;
  }
  return entries;
}

// Only the archive loader can establish the on-disk report/hash proof. Legacy
// financial-periods.json is deliberately insufficient for a same-values claim.
export function previousFinancialPeriodsFromArchive(parsed, currentTargetDate) {
  if (parsed?.schemaVersion !== "2.0" || !validDate(currentTargetDate) ||
      !validDate(parsed.targetDate) || parsed.targetDate >= currentTargetDate ||
      !/^[a-f0-9]{64}$/.test(parsed.reportSha256 ?? "")) return {};
  const entries = {};
  for (const [code, entry] of Object.entries(parsed.periods ?? {})) {
    if (/^\d{6}$/.test(code) && validEntry(entry) && entry.reportDate <= parsed.targetDate) {
      entries[code] = { ...entry, baselineTargetDate: parsed.targetDate, baselineReportSha256: parsed.reportSha256 };
    }
  }
  return entries;
}

function comparisonState(current, previous) {
  if (previous.reportDate > current.reportDate) return "period_regressed";
  if (previous.reportDate < current.reportDate) return "new_period";
  if (canonicalJson(previous.fields.revenueBasis ?? null) !== canonicalJson(current.fields.revenueBasis ?? null)) return "metric_changed";
  if (canonicalJson(previous.fields.units) !== canonicalJson(current.fields.units)) return "units_changed";
  if (canonicalJson(previous.fields.values) !== canonicalJson(current.fields.values)) return "values_changed";
  if (canonicalJson(previous.fields.version) !== canonicalJson(current.fields.version)) return "version_changed";
  return "same_period";
}

export function attachPreviousFinancialPeriods(context, periodsByCode = {}) {
  if (!Array.isArray(context?.data?.watchlist)) return context;
  const targetDate = context.meta?.targetDate;
  return {
    ...context,
    data: {
      ...context.data,
      watchlist: context.data.watchlist.map((item) => {
        if (!item?.financials) return item;
        const current = makeEntry(item.financials);
        const previous = periodsByCode?.[item.code];
        const trusted = current && validEntry(previous) &&
          validDate(previous.baselineTargetDate) && validDate(targetDate) && previous.baselineTargetDate < targetDate &&
          previous.reportDate <= previous.baselineTargetDate && current.reportDate <= targetDate &&
          /^[a-f0-9]{64}$/.test(previous.baselineReportSha256 ?? "");
        const state = trusted ? comparisonState(current, previous) : "none";
        const financials = { ...item.financials };
        delete financials.samePeriodAsPreviousSession;
        return {
          ...item, financials,
          financialComparisonBaseline: state,
          financialComparison: trusted ? {
            state, baselineTargetDate: previous.baselineTargetDate,
            baselineReportSha256: previous.baselineReportSha256,
            previousFingerprint: previous.fingerprint, currentFingerprint: current.fingerprint
          } : { state: "none" }
        };
      })
    }
  };
}
