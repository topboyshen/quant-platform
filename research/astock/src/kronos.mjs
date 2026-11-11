const SYMBOL_PATTERN = /^\d{6}\.(SH|SZ|BJ)$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const PRICE_FIELDS = Object.freeze(["open", "high", "low", "close"]);
export const MIN_KRONOS_BARS = 60;
export const MAX_KRONOS_SERIES = 3;

function finiteNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function normalizeBar(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (value.sessionStatus === "intraday_partial") return null;
  if (typeof value.date !== "string" || !DATE_PATTERN.test(value.date)) return null;
  const bar = { date: value.date };
  for (const field of PRICE_FIELDS) {
    const number = finiteNumber(value[field]);
    if (number === null || number <= 0) return null;
    bar[field] = number;
  }
  if (bar.high < Math.max(bar.open, bar.close, bar.low)) return null;
  if (bar.low > Math.min(bar.open, bar.close, bar.high)) return null;
  bar.volume = finiteNumber(value.volume) ?? 0;
  bar.amount = finiteNumber(value.turnover ?? value.amount) ?? 0;
  if (bar.volume < 0 || bar.amount < 0) return null;
  return bar;
}

export function buildKronosRequest(stockBundle, {
  lookback = 100,
  predictionDays = 5
} = {}) {
  if (!Number.isInteger(lookback) || lookback < MIN_KRONOS_BARS || lookback > 512) {
    throw new Error("Kronos lookback 必须是 60—512 的整数");
  }
  if (!Number.isInteger(predictionDays) || predictionDays < 1 || predictionDays > 20) {
    throw new Error("Kronos predictionDays 必须是 1—20 的整数");
  }

  const calls = stockBundle?.tools?.get_stock_price?.calls;
  if (!Array.isArray(calls)) return null;
  const series = [];
  const seen = new Set();
  for (const call of calls) {
    if (series.length >= MAX_KRONOS_SERIES) break;
    const symbol = call?.arguments?.symbol;
    if (
      call?.status !== "ok" ||
      typeof symbol !== "string" ||
      !SYMBOL_PATTERN.test(symbol) ||
      seen.has(symbol)
    ) {
      continue;
    }
    const rawBars = call?.result?.data?.prices;
    if (!Array.isArray(rawBars)) continue;
    const byDate = new Map();
    for (const rawBar of rawBars) {
      const bar = normalizeBar(rawBar);
      if (bar) byDate.set(bar.date, bar);
    }
    const bars = [...byDate.values()]
      .sort((left, right) => left.date.localeCompare(right.date))
      .slice(-lookback);
    if (bars.length < MIN_KRONOS_BARS) continue;
    series.push({ symbol, bars });
    seen.add(symbol);
  }
  if (series.length === 0) return null;
  return {
    schemaVersion: "1.0",
    task: "kronos_market_state",
    lookback: Math.min(lookback, ...series.map((item) => item.bars.length)),
    predictionDays,
    series
  };
}

export function unavailableKronos(reasonCode = "insufficient_price_history") {
  return {
    schemaVersion: "1.0",
    task: "kronos_market_state",
    status: "unavailable",
    reasonCode,
    warnings: [
      "Kronos 实验性状态推断不可用；投资备忘录必须继续使用其余证据并显式标注缺口。"
    ]
  };
}
