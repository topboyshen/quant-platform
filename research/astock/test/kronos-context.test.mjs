import test from "node:test";
import assert from "node:assert/strict";

import {
  buildKronosRequest,
  unavailableKronos
} from "../src/kronos.mjs";

function priceCall(symbol, count = 80) {
  const prices = [];
  for (let index = 0; index < count; index += 1) {
    const date = new Date(Date.UTC(2026, 0, index + 1)).toISOString().slice(0, 10);
    prices.push({
      date,
      open: 10 + index / 100,
      high: 10.2 + index / 100,
      low: 9.8 + index / 100,
      close: 10.1 + index / 100,
      volume: 1000 + index,
      turnover: 10000 + index
    });
  }
  return {
    arguments: { symbol },
    status: "ok",
    result: { data: { prices } }
  };
}

test("builds a bounded Kronos request from successful fixed price calls", () => {
  const request = buildKronosRequest({
    tools: {
      get_stock_price: {
        calls: [priceCall("600875.SH"), priceCall("002617.SZ")]
      }
    }
  }, { lookback: 60, predictionDays: 5 });
  assert.equal(request.series.length, 2);
  assert.equal(request.series[0].bars.length, 60);
  assert.equal(request.series[0].bars[0].amount > 0, true);
  assert.equal(request.task, "kronos_market_state");
});

test("skips unavailable, malformed and short price series", () => {
  const malformed = priceCall("600875.SH");
  malformed.result.data.prices[0].close = "instruction";
  const short = priceCall("002617.SZ", 20);
  const unavailable = priceCall("300033.SZ");
  unavailable.status = "unavailable";
  const request = buildKronosRequest({
    tools: { get_stock_price: { calls: [malformed, short, unavailable] } }
  }, { lookback: 60 });
  assert.equal(request.series.length, 1);
  assert.equal(request.series[0].symbol, "600875.SH");
});

test("returns null without enough evidence and builds a safe degraded response", () => {
  assert.equal(buildKronosRequest({ tools: {} }), null);
  const degraded = unavailableKronos();
  assert.equal(degraded.status, "unavailable");
  assert.match(degraded.warnings[0], /显式标注缺口/);
});

test("excludes an unfinished intraday daily bar from Kronos input", () => {
  const call = priceCall("600875.SH", 61);
  call.result.data.prices.at(-1).sessionStatus = "intraday_partial";
  const request = buildKronosRequest({
    tools: { get_stock_price: { calls: [call] } }
  }, { lookback: 60 });
  assert.equal(request.series[0].bars.length, 60);
  assert.notEqual(request.series[0].bars.at(-1).date, call.result.data.prices.at(-1).date);
});

test("excludes OHLC-inconsistent and negative-volume bars from Kronos input", () => {
  const call = priceCall("600875.SH", 64);
  call.result.data.prices[0].open = call.result.data.prices[0].high + 1;
  call.result.data.prices[1].close = call.result.data.prices[1].low - 1;
  call.result.data.prices[2].volume = -1;
  call.result.data.prices[3].turnover = -1;
  const request = buildKronosRequest({
    tools: { get_stock_price: { calls: [call] } }
  }, { lookback: 60 });
  assert.equal(request.series[0].bars.length, 60);
  assert.equal(request.series[0].bars.some((bar) => bar.volume < 0 || bar.amount < 0), false);
  assert.equal(request.series[0].bars.every((bar) => (
    bar.high >= Math.max(bar.open, bar.close, bar.low) &&
    bar.low <= Math.min(bar.open, bar.close, bar.high)
  )), true);
});
