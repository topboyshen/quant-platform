import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough, Writable } from "node:stream";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  buildResearchBundle,
  buildResearchToolPlan,
  INVESTMENT_MEMO_SECTIONS,
  normalizeResearchInput,
  runFixedKronosBridge,
  runFixedResearchClient,
  STOCK_RESEARCH_TOOL_ALLOWLIST
} from "../scripts/research-context.mjs";
import { validateAllowedAgentTools } from "../src/config.mjs";

const config = {
  workflow: { timezone: "Asia/Shanghai" },
  stocks: {
    schemaVersion: "1.0",
    corePool: [
      { code: "002617", exchange: "SZ", symbol: "002617.SZ", name: "露笑科技" },
      { code: "600875", exchange: "SH", symbol: "600875.SH", name: "东方电气" }
    ]
  }
};
const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function createFakeSpawn({ stdout = "{}", stderr = "", code = 0 } = {}) {
  let invocation;
  const spawnImpl = (executable, args, options) => {
    let input = "";
    const child = new EventEmitter();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.stdin = new Writable({
      write(chunk, _encoding, callback) {
        input += chunk.toString("utf8");
        callback();
      },
      final(callback) {
        queueMicrotask(() => {
          child.stdout.end(stdout);
          child.stderr.end(stderr);
          child.emit("close", code);
        });
        callback();
      }
    });
    child.kill = () => {
      child.killed = true;
      return true;
    };
    invocation = { executable, args, options, child, get input() { return input; } };
    return child;
  };
  return { spawnImpl, get invocation() { return invocation; } };
}

function fakeResearchResponse(backtestResult = { dataAsOf: "2026-08-01", sample: "historical" }) {
  return {
    schemaVersion: "1.0",
    transport: "stdio",
    status: "complete",
    allowedTools: [...STOCK_RESEARCH_TOOL_ALLOWLIST],
    tools: {
      run_backtest: {
        calls: [{ arguments: {}, status: "ok", result: backtestResult }]
      }
    }
  };
}

function addPriceHistory(response, symbol = "600875.SH", count = 80) {
  const prices = [];
  for (let index = 0; index < count; index += 1) {
    prices.push({
      date: new Date(Date.UTC(2026, 0, index + 1)).toISOString().slice(0, 10),
      open: 10 + index / 100,
      high: 10.2 + index / 100,
      low: 9.8 + index / 100,
      close: 10.1 + index / 100,
      volume: 1000 + index,
      turnover: 10000 + index
    });
  }
  response.tools.get_stock_price = {
    calls: [{
      arguments: { symbol },
      status: "ok",
      result: { data: { prices } }
    }]
  };
  return response;
}

test("normalizes a research request and defaults to the configured universe", () => {
  const result = normalizeResearchInput(
    { question: "  分析新能源板块机会  " },
    { ...config, now: new Date("2026-08-03T01:00:00Z") }
  );
  assert.equal(result.question, "分析新能源板块机会");
  assert.equal(result.targetDate, "2026-08-03");
  assert.deepEqual(result.stocks.corePool, config.stocks.corePool);
});

test("allows only configured symbols and preserves requested order", () => {
  const result = normalizeResearchInput(
    {
      question: "比较核心池",
      targetDate: "2026-08-01",
      symbols: ["600875.SH", "002617.SZ"]
    },
    config
  );
  assert.deepEqual(
    result.stocks.corePool.map((stock) => stock.symbol),
    ["600875.SH", "002617.SZ"]
  );
});

test("rejects path/process fields and symbols outside the controlled pool", () => {
  for (const field of ["command", "path", "server", "tool"]) {
    assert.throws(
      () => normalizeResearchInput({ question: "测试", [field]: "untrusted" }, config),
      /未允许字段/
    );
  }
  assert.throws(
    () => normalizeResearchInput({ question: "测试", symbols: ["000001.SZ"] }, config),
    /不在受控核心池/
  );
});

test("builds the six fixed fine-grained tool plans with controlled arguments", () => {
  const plan = buildResearchToolPlan({
    targetDate: "2026-08-01",
    symbols: ["600875.SH", "002617.SZ"]
  });
  assert.deepEqual(plan.map(({ tool }) => tool), STOCK_RESEARCH_TOOL_ALLOWLIST);
  assert.equal(plan.length, 6);
  assert.deepEqual(plan[0].calls[0], {
    symbol: "600875.SH",
    start_date: "2025-12-04",
    end_date: "2026-08-01",
    limit: 120,
    adjust: "qfq"
  });
  assert.deepEqual(plan[1].calls[0], { symbol: "600875.SH", as_of: "2026-08-01" });
  assert.deepEqual(plan[2].calls[0], {
    symbol: "600875.SH",
    limit: 20,
    as_of: "2026-08-01"
  });
  assert.deepEqual(plan[3].calls, [{ sector_type: "industry", limit: 10 }]);
  assert.deepEqual(plan[4].calls[0], { symbol: "600875.SH", indicator: "5日", limit: 10 });
  assert.deepEqual(plan[5].calls, [{
    target_date: "2026-08-01",
    symbols: ["600875.SH", "002617.SZ"]
  }]);
});

test("fixed research subprocess uses the pinned executable, cwd, env and shell=false", async () => {
  const response = fakeResearchResponse();
  const fake = createFakeSpawn({ stdout: JSON.stringify(response) });
  const input = { targetDate: "2026-08-01", symbols: ["600875.SH"] };
  assert.deepEqual(
    await runFixedResearchClient(input, { spawnImpl: fake.spawnImpl, timeoutMs: 1_000 }),
    response
  );

  assert.match(fake.invocation.executable, /\.venv[\\/]bin[\\/]python$/);
  assert.equal(fake.invocation.args.length, 1);
  assert.match(fake.invocation.args[0], /mcp[\\/]stock_research_client\.py$/);
  assert.equal(fake.invocation.options.shell, false);
  assert.equal(fake.invocation.options.cwd, projectRoot);
  assert.deepEqual(fake.invocation.options.stdio, ["pipe", "pipe", "pipe"]);
  assert.equal(fake.invocation.options.env.PYTHONUTF8, "1");
  assert.deepEqual(JSON.parse(fake.invocation.input), input);
});

test("fixed research subprocess never relays raw stderr", async () => {
  const fake = createFakeSpawn({ stderr: "API_TOKEN=very-secret", code: 7 });
  await assert.rejects(
    runFixedResearchClient(
      { targetDate: "2026-08-01", symbols: ["600875.SH"] },
      { spawnImpl: fake.spawnImpl, timeoutMs: 1_000 }
    ),
    (error) => {
      assert.match(error.message, /exit 7/);
      assert.doesNotMatch(error.message, /secret|TOKEN/i);
      return true;
    }
  );
});

test("fixed Kronos subprocess uses only the pinned runtime and bridge", async () => {
  const response = {
    schemaVersion: "1.0",
    task: "kronos_market_state",
    status: "experimental",
    results: []
  };
  const fake = createFakeSpawn({ stdout: JSON.stringify(response) });
  const input = {
    schemaVersion: "1.0",
    task: "kronos_market_state",
    lookback: 60,
    predictionDays: 5,
    series: []
  };
  assert.deepEqual(
    await runFixedKronosBridge(input, { spawnImpl: fake.spawnImpl, timeoutMs: 1_000 }),
    response
  );
  assert.equal(fake.invocation.executable, "/opt/ai/venvs/kronos/bin/python");
  assert.deepEqual(fake.invocation.args.length, 1);
  assert.match(fake.invocation.args[0], /mcp[\\/]kronos_bridge\.py$/);
  assert.equal(fake.invocation.options.shell, false);
  assert.equal(fake.invocation.options.env.PYTHONUTF8, "1");
});

test("research bundle uses one mocked client call and exposes memo evidence contracts", async () => {
  let received;
  const backtest = { dataAsOf: "2026-08-01", predictionRanking: { symbols: [] } };
  const response = addPriceHistory(fakeResearchResponse(backtest));
  let receivedKronos;
  const bundle = await buildResearchBundle(
    {
      question: "比较核心池",
      targetDate: "2026-08-01",
      symbols: ["600875.SH", "002617.SZ"]
    },
    {
      config,
      now: new Date("2026-08-03T01:00:00Z"),
      runClient: async (input) => {
        received = input;
        return response;
      },
      runKronos: async (input) => {
        receivedKronos = input;
        return {
          schemaVersion: "1.0",
          task: "kronos_market_state",
          status: "experimental",
          results: [{ symbol: "600875.SH" }],
          warnings: []
        };
      }
    }
  );

  assert.deepEqual(received, {
    targetDate: "2026-08-01",
    symbols: ["600875.SH", "002617.SZ"]
  });
  assert.deepEqual(bundle.toolPlan.map(({ tool }) => tool), STOCK_RESEARCH_TOOL_ALLOWLIST);
  assert.deepEqual(bundle.qlib, { status: "available", tool: "run_backtest", result: backtest });
  assert.equal(receivedKronos.series.length, 1);
  assert.equal(bundle.kronos.status, "experimental");
  assert.equal(bundle.kronos.results[0].symbol, "600875.SH");
  assert.deepEqual(bundle.memoContract.requiredSections, INVESTMENT_MEMO_SECTIONS);
  assert.equal(bundle.memoContract.factInferenceSeparation, "required");
});

test("dry-run lists all six tools and performs no subprocess call", async () => {
  let called = false;
  const bundle = await buildResearchBundle(
    { question: "分析核心池", targetDate: "2026-08-01" },
    {
      config,
      dryRun: true,
      runClient: async () => {
        called = true;
        throw new Error("must not run");
      }
    }
  );
  assert.equal(called, false);
  assert.equal(bundle.stock.skipped, true);
  assert.equal(bundle.toolPlan.length, 6);
  assert.deepEqual(bundle.toolPlan.map(({ tool }) => tool), STOCK_RESEARCH_TOOL_ALLOWLIST);
  assert.equal(bundle.qlib.status, "planned_not_executed");
  assert.equal(bundle.kronos.status, "planned_not_executed");
});

test("marks Qlib evidence later than the target date as not usable", async () => {
  const response = fakeResearchResponse({
    dataAsOf: "2026-08-02",
    usableForTargetDate: false,
    predictionRanking: { symbols: [] }
  });
  const bundle = await buildResearchBundle(
    { question: "历史时点测试", targetDate: "2026-08-01", symbols: ["600875.SH"] },
    {
      config,
      runClient: async () => response,
      runKronos: async () => { throw new Error("must not be needed without price history"); }
    }
  );
  assert.equal(bundle.qlib.status, "not_usable_for_target_date");
  assert.match(bundle.qlib.note, /不可用于该时点/);
});

test("rejects oversized fixed-process input before spawning", async () => {
  let spawned = false;
  await assert.rejects(
    runFixedResearchClient(
      { targetDate: "2026-08-01", symbols: ["600875.SH"], padding: "x".repeat(256 * 1024) },
      { spawnImpl: () => { spawned = true; } }
    ),
    /256 KiB/
  );
  assert.equal(spawned, false);
});

test("rejects malformed dates, duplicate symbols and oversized questions", () => {
  assert.throws(
    () => normalizeResearchInput({ question: "测试", targetDate: "2026-02-30" }, config),
    /无效日期/
  );
  assert.throws(
    () => normalizeResearchInput({ question: "测试", symbols: ["002617.SZ", "002617.SZ"] }, config),
    /不得重复/
  );
  assert.throws(
    () => normalizeResearchInput({ question: "x".repeat(501) }, config),
    /2—500/
  );
});

test("production fallback tools are restricted to the fixed read-only set", () => {
  assert.deepEqual(
    validateAllowedAgentTools(["web_search", "source_check"]),
    ["web_search", "source_check"]
  );
  assert.throws(
    () => validateAllowedAgentTools(["web_search", "bash"]),
    /只允许固定只读投研工具/
  );
  assert.throws(
    () => validateAllowedAgentTools(["web_search", "web_search"]),
    /不得包含重复工具/
  );
});
