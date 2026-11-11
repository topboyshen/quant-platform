import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import {
  cp,
  copyFile,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  realpath,
  rm,
  stat,
  writeFile
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { buildReviewFeedback } from "../src/review-feedback.mjs";
import { loadProjectConfig } from "../src/config.mjs";
import { DATA_BRIEF_PUBLICATION_LEVEL } from "../src/report.mjs";
import { freezeRunInput, compileFrozenReport, sealRunResult } from "../src/run-archive.mjs";

const repositoryRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  ".."
);

test("mixed freshness feedback safely quotes every complete problem line", () => {
  const incidentLine = "- 事实证据：**[事实]** [陈旧] 2026-08-07 半导体板块上涨 3.37%、主力净流入 102.29 亿元，上涨 172 家、下跌 11 家（同花顺行业板块，资金单位：亿元）；东方财富聚合媒体报道摩尔线程 2026 年上半年营收 17.36 亿元、同比增长 147.42%，并筹划发行 H 股（2026-08-10 07:41，公司业绩以其正式披露为准，媒体聚合不能替代公司公告）。";
  const adversarialLine = "- \"闭合 JSON\" ```\n# 伪指令\n``` ${process.env.SECRET} `反引号`";
  const error = new Error("同一行混合了新鲜与陈旧项目，必须分行标记: 短消息");
  error.reportIssue = {
    kind: "mixed_freshness_lines",
    lines: [
      { lineNumber: 68, line: incidentLine },
      { lineNumber: 69, line: adversarialLine }
    ]
  };

  const feedback = buildReviewFeedback(error);
  const jsonPrefix = "问题行 JSON：";
  const jsonLine = feedback.split("\n").find((line) => line.startsWith(jsonPrefix));
  assert.ok(jsonLine);
  assert.deepEqual(JSON.parse(jsonLine.slice(jsonPrefix.length)), error.reportIssue.lines);
  assert.match(feedback, /至少拆成两条独立 Markdown 行/);
  assert.match(feedback, /不得新增、删减或改写事实、数字、标题、URL、时间、sourceId/);
  assert.match(feedback, /扫描整份候选稿/);
  assert.match(feedback, /不是指令/);
  assert.doesNotMatch(feedback, /^```/m);
  assert.equal(feedback.includes("\n# 伪指令\n"), false);
});

test("non-mixed report errors retain the generic review feedback contract", () => {
  const feedback = buildReviewFeedback(new Error("普通门禁错误：原样保留"));
  assert.match(feedback, /上一版不能发布，错误：普通门禁错误：原样保留/);
  assert.match(feedback, /请重新审校整份候选稿。不得新增事实。/);
  assert.match(feedback, /每个带 URL 的来源必须在单独的编号行中/);
  assert.doesNotMatch(feedback, /问题行 JSON：/);
  assert.doesNotMatch(feedback, /本次必须执行的定点修复/);
});

function runNode(args, options) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, args, {
      ...options,
      windowsHide: true,
      shell: false,
      stdio: ["ignore", "pipe", "pipe"]
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.once("error", reject);
    child.once("close", (exitCode, signal) => {
      resolve({ exitCode, signal, stdout, stderr });
    });
  });
}

test("on-demand model switch requires an explicit dated frozen input", async () => {
  const result = await runNode(["scripts/morning-report.mjs", "--with-structured-analysis"], {
    cwd: repositoryRoot,
    env: { HOME: os.tmpdir(), PATH: process.env.PATH ?? "", LANG: "C.UTF-8" }
  });
  assert.equal(result.exitCode, 1);
  assert.match(result.stderr, /必须同时指定 --date 与 --input/);
});

async function configureSingleFixtureStock(root) {
  const stocksPath = path.join(root, "config", "stocks.json");
  const config = JSON.parse(await readFile(stocksPath, "utf8"));
  config.corePool = [{
    ...config.corePool[0],
    code: "000001",
    exchange: "SZ",
    symbol: "000001.SZ",
    name: "测试银行"
  }];
  await writeFile(stocksPath, `${JSON.stringify(config, null, 2)}\n`, "utf8");
}

test("an early strict bridge failure replaces yesterday's state with today's sanitized failed collect state", async () => {
  const fixtureRoot = await mkdtemp(
    path.join(os.tmpdir(), "astock-token=super-secret-morning-state-")
  );
  try {
    await cp(path.join(repositoryRoot, "src"), path.join(fixtureRoot, "src"), {
      recursive: true
    });
    await cp(path.join(repositoryRoot, "config"), path.join(fixtureRoot, "config"), {
      recursive: true
    });
    await mkdir(path.join(fixtureRoot, "scripts"), { recursive: true });
    await copyFile(
      path.join(repositoryRoot, "scripts", "morning-report.mjs"),
      path.join(fixtureRoot, "scripts", "morning-report.mjs")
    );
    await mkdir(path.join(fixtureRoot, "work"), { recursive: true });
    await writeFile(
      path.join(fixtureRoot, "work", "last-run.json"),
      `${JSON.stringify({
        schemaVersion: "1.0",
        targetDate: "2026-08-07",
        startedAt: "2026-08-07T00:00:00.000Z",
        completedAt: "2026-08-07T00:10:00.000Z",
        status: "success",
        publicationGate: "passed"
      }, null, 2)}\n`,
      "utf8"
    );

    // The fixture intentionally has no .venv executable, so collection fails
    // immediately after the current occurrence has been persisted.
    const result = await runNode([
      "scripts/morning-report.mjs",
      "--date",
      "2026-08-10",
      "--frozen-cutoff",
      "2026-08-10T00:00:00Z",
      "--strict-data",
      "--trading-days-only",
      "--input",
      "missing client_secret = last-run-secret.json"
    ], { cwd: fixtureRoot });
    assert.equal(result.signal, null);
    assert.equal(result.exitCode, 1);

    const lastRun = JSON.parse(
      await readFile(path.join(fixtureRoot, "work", "last-run.json"), "utf8")
    );
    assert.equal(lastRun.targetDate, "2026-08-10");
    assert.equal(lastRun.status, "failed");
    assert.equal(lastRun.phase, "collect");
    assert.equal(lastRun.failureCode, "data_bridge_failed");
    assert.equal(lastRun.retryable, true);
    assert.equal(lastRun.publicationGate, "blocked");
    assert.equal(typeof lastRun.startedAt, "string");
    assert.equal(typeof lastRun.failedAt, "string");
    assert.equal(typeof lastRun.rawError, "string");
    assert.ok(lastRun.rawError.length > 0 && lastRun.rawError.length <= 1000);
    assert.equal(typeof lastRun.error, "string");
    assert.ok(lastRun.error.length > 0 && lastRun.error.length <= 500);
    assert.doesNotMatch(lastRun.rawError, /super-secret/);
    assert.doesNotMatch(lastRun.error, /super-secret/);
    assert.doesNotMatch(lastRun.rawError, /last-run-secret/);
    assert.doesNotMatch(lastRun.error, /last-run-secret/);
    assert.match(lastRun.rawError, /\[REDACTED\]/);
    assert.match(lastRun.error, /\[REDACTED\]/);
    assert.doesNotMatch(result.stderr, /super-secret/);
    assert.doesNotMatch(result.stderr, /last-run-secret/);
    assert.deepEqual(
      (await readdir(path.join(fixtureRoot, "work")))
        .filter((name) => name.includes("last-run.json.") && name.endsWith(".tmp")),
      []
    );
  } finally {
    await rm(fixtureRoot, { recursive: true, force: true });
  }
});

test("diagnostic stderr redacts quoted JSON credentials and URL credentials", async () => {
  const secretArgument = [
    '--payload={"client_secret": "json secret value"',
    '"api_key":"key-secret"',
    '"authorization":"Basic auth-secret"',
    '"cookie":"session=json-cookie-secret"',
    '"url":"https://user:pass@example.test/news?token=query-secret&safe=kept"}'
  ].join(",");

  const result = await runNode([
    "scripts/morning-report.mjs",
    secretArgument
  ], { cwd: repositoryRoot });

  assert.equal(result.signal, null);
  assert.equal(result.exitCode, 1);
  for (const secret of [
    "json secret value",
    "key-secret",
    "auth-secret",
    "json-cookie-secret",
    "user:pass",
    "query-secret"
  ]) {
    assert.doesNotMatch(result.stderr, new RegExp(secret.replaceAll("-", "\\-")));
  }
  assert.match(result.stderr, /\[REDACTED\]/);
  assert.match(result.stderr, /example\.test\/news/);
  assert.match(result.stderr, /safe=kept/);
  assert.ok(result.stderr.length <= 650);
});

test("blocked manual recovery skips the reviewer while valid recovery keeps stderr private", async () => {
  const fixtureRoot = await mkdtemp(
    path.join(os.tmpdir(), "astock-sanitized-agent-stderr-")
  );
  try {
    for (const directory of ["src", "config", "agents"]) {
      await cp(
        path.join(repositoryRoot, directory),
        path.join(fixtureRoot, directory),
        { recursive: true }
      );
    }
    await configureSingleFixtureStock(fixtureRoot);
    await mkdir(path.join(fixtureRoot, "scripts"), { recursive: true });
    await copyFile(
      path.join(repositoryRoot, "scripts", "morning-report.mjs"),
      path.join(fixtureRoot, "scripts", "morning-report.mjs")
    );
    await mkdir(
      path.join(fixtureRoot, "node_modules", "pi-turn-limit", "src"),
      { recursive: true }
    );
    await writeFile(
      path.join(fixtureRoot, "node_modules", "pi-turn-limit", "src", "turn-limit.ts"),
      "export default {};\n",
      { encoding: "utf8", mode: 0o600 }
    );

    const workflowPath = path.join(fixtureRoot, "config", "workflow.json");
    const workflow = JSON.parse(await readFile(workflowPath, "utf8"));
    workflow.agentExecutor = "pi";
    workflow.agentFallback = "none";
    workflow.maxReviewAttempts = 1;
    await writeFile(
      workflowPath,
      `${JSON.stringify(workflow, null, 2)}\n`,
      { encoding: "utf8", mode: 0o600 }
    );

    const fakePiPath = path.join(fixtureRoot, "fake-pi.mjs");
    const modelMarker = path.join(fixtureRoot, "reviewer-was-called.txt");
    const fakePiSource = [
      'import { readFileSync, writeFileSync } from "node:fs";',
      `writeFileSync(${JSON.stringify(modelMarker)}, "called");`,
      'const instruction = readFileSync("work/run-instruction.md", "utf8");',
      'const request = JSON.parse(readFileSync("work/morning-request.json", "utf8"));',
      'const cutoffPrefix = "- 固定数据截止（Asia/Shanghai）：";',
      'const levelPrefix = "- 发布级别：";',
      'const cutoff = instruction.split("\\n").find((line) => line.startsWith(cutoffPrefix)).slice(cutoffPrefix.length).trim();',
      'const level = instruction.split("\\n").find((line) => line.startsWith(levelPrefix)).slice(levelPrefix.length).trim();',
      'const report = [',
      '  "# A 股晨报 | " + request.targetDate,',
      '  "",',
      '  "> 数据截止：" + cutoff + "（Asia/Shanghai）",',
      '  "> 数据状态：不可用",',
      '  "> 交易日状态：待核验",',
      '  "> 发布级别：" + level,',
      '  "",',
      '  "## 一句话结论",',
      '  "[事实] 数据不可用，本测试文本仅验证日志安全，不代表任何行情或投资观点。",',
      '  "继续保留非敏感占位摘要，以满足晨报最小长度与必要结构要求。",',
      '  "## 1. 隔夜全球与宏观", "暂无可靠数据。",',
      '  "## 2. 上一交易日 A 股复盘", "暂无可靠数据。",',
      '  "## 3. 今日政策、事件与日历", "暂无可靠数据。",',
      '  "## 4. 板块与资金线索", "暂无可靠数据。",',
      '  "## 5. 核心股票池", "暂无可靠数据。",',
      '  "## 6. 今日情景与研究计划", "暂无可靠数据。",',
      '  "## 7. 风险提示与数据缺口", "数据不可用。",',
      '  "## 来源", "无。"',
      '].join("\\n");',
      'const phase = process.argv.some((arg) => arg === "@agents/report_reviewer.md") ? "review" : "generation";',
      'process.stderr.write([',
      '  phase + "-summary",',
      '  "{\\\"client_secret\\\": \\\"json " + phase + " secret\\\"}",',
      '  "Authorization: Bearer " + phase + "-bearer-secret",',
      '  "https://user:pass@example.test/news?api_key=" + phase + "-query-secret&safe=kept",',
      '  "-----BEGIN PRIVATE KEY-----" + phase + "-private-material-----END PRIVATE KEY-----",',
      '  "x".repeat(70000)',
      '].join(" "));',
      'process.stdout.write(report);'
    ].join("\n");
    await writeFile(fakePiPath, fakePiSource, { encoding: "utf8", mode: 0o600 });

    const validContext = validStructuredMorningContext();
    const blockedContext = structuredClone(validContext);
    blockedContext.data.previousAShareSession.indices[0].close += 10;
    await writeFile(
      path.join(fixtureRoot, "blocked-context.json"),
      `${JSON.stringify(blockedContext, null, 2)}\n`,
      "utf8"
    );
    await writeFile(
      path.join(fixtureRoot, "valid-context.json"),
      `${JSON.stringify(validContext, null, 2)}\n`,
      "utf8"
    );
    await mkdir(path.join(fixtureRoot, "work"), { recursive: true });
    await writeFile(path.join(fixtureRoot, "work", "last-pi-output.md"), "冻结候选稿\n", "utf8");
    await writeFile(path.join(fixtureRoot, "work", "review-feedback.md"), "保守复核\n", "utf8");

    const blockedResult = await runNode([
      "scripts/morning-report.mjs",
      "--date",
      "2026-08-10",
      "--input",
      "blocked-context.json",
      "--review-existing",
      "--frozen-cutoff",
      "2026-08-10T00:00:00Z"
    ], {
      cwd: fixtureRoot,
      env: { ...process.env, PI_CLI_JS: fakePiPath }
    });
    assert.equal(blockedResult.signal, null);
    assert.equal(blockedResult.exitCode, 2, blockedResult.stderr);
    assert.equal((await readdir(fixtureRoot)).includes("reviewer-was-called.txt"), false);
    const blockedState = JSON.parse(await readFile(
      path.join(fixtureRoot, "work", "last-run.json"),
      "utf8"
    ));
    assert.equal(blockedState.status, "blocked");
    assert.equal(blockedState.publicationGate, "blocked");
    assert.equal(blockedState.evidenceReview, "not_applicable");
    assert.equal(blockedState.analysisStatus, "not_run");
    assert.equal(blockedState.analysisAttempts, 0);
    assert.equal(blockedState.reviewAttempts, 0);
    await assert.rejects(
      readFile(path.join(fixtureRoot, "reports", "daily", "2026-08-10.md"), "utf8"),
      /ENOENT/
    );

    const result = await runNode([
      "scripts/morning-report.mjs",
      "--date",
      "2026-08-10",
      "--input",
      "valid-context.json",
      "--review-existing",
      "--frozen-cutoff",
      "2026-08-10T00:00:00Z"
    ], {
      cwd: fixtureRoot,
      env: { ...process.env, PI_CLI_JS: fakePiPath }
    });
    assert.equal(result.signal, null);
    assert.equal(result.exitCode, 1);
    assert.equal((await readdir(fixtureRoot)).includes("reviewer-was-called.txt"), true);

    const logExpectations = [
      ["last-pi-review-1-stderr.log", "review"]
    ];
    for (const [fileName, phase] of logExpectations) {
      const filePath = path.join(fixtureRoot, "work", fileName);
      const content = await readFile(filePath, "utf8");
      assert.ok(content.length > 0 && Buffer.byteLength(content, "utf8") <= 64 * 1024);
      assert.match(content, new RegExp(`${phase}-summary`));
      assert.match(content, /example\.test\/news/);
      assert.match(content, /safe=kept/);
      assert.match(content, /\[REDACTED/);
      for (const secret of [
        `json ${phase} secret`,
        `${phase}-bearer-secret`,
        "user:pass",
        `${phase}-query-secret`,
        `${phase}-private-material`
      ]) {
        assert.equal(content.includes(secret), false, `${fileName} leaked ${secret}`);
      }
      if (process.platform !== "win32") {
        assert.equal((await stat(filePath)).mode & 0o777, 0o600);
      }
    }
    assert.deepEqual(
      (await readdir(path.join(fixtureRoot, "work")))
        .filter((name) => name.includes("stderr.log.") && name.endsWith(".tmp")),
      []
    );
  } finally {
    await rm(fixtureRoot, { recursive: true, force: true });
  }
});

function validStructuredMorningContext() {
  const marketTime = "2026-08-07T15:00:00+08:00";
  const indexRecord = (code, name, close, previousClose) => ({
    code,
    name,
    close,
    previousClose,
    change: close - previousClose,
    changePercent: (close - previousClose) / previousClose * 100,
    units: {
      close: "index_point",
      previousClose: "index_point",
      change: "index_point",
      changePercent: "percentage_point"
    },
    dataAsOf: marketTime,
    sourceIds: ["market"]
  });
  return {
    schemaVersion: "1.0",
    meta: {
      status: "partial",
      provider: "test-fixture",
      sourceType: "fixture",
      targetDate: "2026-08-10",
      asOf: "2026-08-10T08:00:00+08:00",
      timezone: "Asia/Shanghai",
      isLive: false,
      dataCompleteness: {
        market: 100,
        capital: 0,
        news: 0,
        financial: 100,
        overall: 80
      }
    },
    data: {
      tradingCalendar: {
        targetDate: "2026-08-10",
        status: "trading_day",
        previousTradingDate: "2026-08-07",
        dataAsOf: "2026-08-10T08:00:00+08:00",
        sourceIds: ["calendar"]
      },
      overnightGlobal: [],
      previousAShareSession: {
        sessionDate: "2026-08-07",
        indices: [
          indexRecord("sh000001", "上证指数", 3940.037, 3900.352),
          indexRecord("sz399001", "深证成指", 14311.008, 14110.121),
          indexRecord("sz399006", "创业板指", 3563.116, 3515.56)
        ],
        totalTurnover: 300_010_000_000,
        turnoverSourceIds: ["market"],
        marketBreadth: {
          advancers: 2670,
          decliners: 2399,
          unchanged: 133,
          sourceIds: ["market"]
        },
        units: { totalTurnover: "CNY" },
        dataAsOf: marketTime,
        sourceIds: ["market"]
      },
      policyAndCalendar: [],
      sectors: [],
      capitalFlows: [],
      watchlist: [{
        code: "000001",
        exchange: "SZ",
        symbol: "000001.SZ",
        name: "测试银行",
        status: "available",
        marketData: {
          code: "000001",
          name: "测试银行",
          last: 12.3,
          previousClose: 12,
          change: 0.3,
          changePercent: 2.5,
          units: {
            last: "CNY_per_share",
            previousClose: "CNY_per_share",
            change: "CNY_per_share",
            changePercent: "percentage_point"
          },
          dataAsOf: marketTime,
          sourceIds: ["market"]
        },
        financials: {
          reportDate: "2026-03-31",
          parentNetProfit: 100,
          dataAsOf: "2026-04-30T23:59:59+08:00",
          sourceIds: ["financial"]
        }
      }],
      announcementsAndNews: [],
      sectionCompleteness: {
        tradingCalendar: 100,
        overnightGlobal: 0,
        previousAShareSession: 100,
        policyAndCalendar: 0,
        sectors: 0,
        capitalFlows: 0,
        watchlist: 100,
        announcementsAndNews: 0
      }
    },
    sources: [
      {
        id: "calendar",
        title: "交易日历数据",
        url: "https://example.com/calendar",
        publisher: "测试数据源",
        sourceType: "trading_calendar_data",
        dataAsOf: "2026-08-10T08:00:00+08:00"
      },
      {
        id: "market",
        title: "上一交易日行情数据",
        url: "https://example.com/market",
        publisher: "测试数据源",
        sourceType: "market_data",
        dataAsOf: marketTime
      },
      {
        id: "financial",
        title: "季度财务数据",
        url: "https://example.com/financial",
        publisher: "测试数据源",
        sourceType: "financial_data_aggregation",
        dataAsOf: "2026-04-30T23:59:59+08:00"
      }
    ]
  };
}

test("scheduled structured mode publishes the verified baseline without invoking legacy Pi", async () => {
  const fixtureRoot = await mkdtemp(path.join(os.tmpdir(), "astock-structured-production-"));
  try {
    for (const directory of ["src", "config", "agents"]) {
      await cp(path.join(repositoryRoot, directory), path.join(fixtureRoot, directory), {
        recursive: true
      });
    }
    await configureSingleFixtureStock(fixtureRoot);
    await mkdir(path.join(fixtureRoot, "scripts"), { recursive: true });
    await copyFile(
      path.join(repositoryRoot, "scripts", "morning-report.mjs"),
      path.join(fixtureRoot, "scripts", "morning-report.mjs")
    );
    await mkdir(path.join(fixtureRoot, "node_modules", "pi-turn-limit", "src"), {
      recursive: true
    });
    await writeFile(
      path.join(fixtureRoot, "node_modules", "pi-turn-limit", "src", "turn-limit.ts"),
      "export default {};\n",
      { encoding: "utf8", mode: 0o600 }
    );

    const workflowPath = path.join(fixtureRoot, "config", "workflow.json");
    const workflow = JSON.parse(await readFile(workflowPath, "utf8"));
    workflow.agentExecutor = "pi";
    workflow.agentFallback = "none";
    await writeFile(workflowPath, `${JSON.stringify(workflow, null, 2)}\n`, "utf8");

    const inputPath = path.join(fixtureRoot, "valid-context.json");
    await writeFile(
      inputPath,
      `${JSON.stringify(validStructuredMorningContext(), null, 2)}\n`,
      "utf8"
    );
    const legacyMarker = path.join(fixtureRoot, "legacy-pi-was-called.txt");
    const fakePiPath = path.join(fixtureRoot, "fake-pi.mjs");
    await writeFile(
      fakePiPath,
      `import { writeFileSync } from "node:fs"; writeFileSync(${JSON.stringify(legacyMarker)}, "called"); process.exitCode = 91;\n`,
      { encoding: "utf8", mode: 0o600 }
    );

    const mismatchedContext = validStructuredMorningContext();
    mismatchedContext.data.watchlist[0].marketData.code = "600000";
    await writeFile(
      path.join(fixtureRoot, "mismatched-context.json"),
      `${JSON.stringify(mismatchedContext, null, 2)}\n`,
      "utf8"
    );
    const mismatchedResult = await runNode([
      "scripts/morning-report.mjs",
      "--date",
      "2026-08-10",
      "--input",
      "mismatched-context.json",
      "--strict-data",
      "--trading-days-only",
      "--frozen-cutoff",
      "2026-08-10T00:00:00Z"
    ], {
      cwd: fixtureRoot,
      env: { ...process.env, PI_CLI_JS: fakePiPath }
    });
    assert.equal(mismatchedResult.signal, null);
    assert.equal(mismatchedResult.exitCode, 1);
    assert.equal((await readdir(fixtureRoot)).includes("legacy-pi-was-called.txt"), false);
    const mismatchedState = JSON.parse(await readFile(
      path.join(fixtureRoot, "work", "last-run.json"),
      "utf8"
    ));
    assert.equal(mismatchedState.status, "failed");
    assert.equal(mismatchedState.phase, "collect");
    assert.equal(mismatchedState.failureCode, "data_bridge_failed");
    assert.match(mismatchedState.error, /marketData\.code/);
    await assert.rejects(
      readFile(path.join(fixtureRoot, "reports", "daily", "2026-08-10.md"), "utf8"),
      /ENOENT/
    );

    const result = await runNode([
      "scripts/morning-report.mjs",
      "--date",
      "2026-08-10",
      "--input",
      "valid-context.json",
      "--strict-data",
      "--trading-days-only",
      "--with-structured-analysis",
      "--frozen-cutoff",
      "2026-08-10T00:00:00Z"
    ], {
      cwd: fixtureRoot,
      env: { ...process.env, PI_CLI_JS: fakePiPath }
    });
    assert.equal(result.signal, null);
    assert.equal(result.exitCode, 0, result.stderr);
    assert.equal((await readdir(fixtureRoot)).includes("legacy-pi-was-called.txt"), false);

    const lastRun = JSON.parse(await readFile(
      path.join(fixtureRoot, "work", "last-run.json"),
      "utf8"
    ));
    assert.equal(lastRun.status, "success");
    assert.equal(lastRun.publicationGate, "passed");
    assert.equal(lastRun.reportMode, "structured_compiler");
    assert.equal(lastRun.reportVariant, "verified_baseline");
    assert.equal(lastRun.analysisStatus, "skipped_no_semantic_candidate");
    assert.equal(lastRun.semanticCandidateCount, 0);
    assert.equal(lastRun.analysisAttempts, 0);
    assert.equal(lastRun.reviewAttempts, 0);
    assert.equal(lastRun.reportFile, "reports/daily/2026-08-10.md");

    const report = await readFile(
      path.join(fixtureRoot, "reports", "daily", "2026-08-10.md"),
      "utf8"
    );
    assert.match(report, /生成方式：程序核验基础版/);
    assert.match(report, /### 000001 测试银行/);
    assert.match(report, /\[陈旧\]/);
    assert.doesNotMatch(report, /legacy-pi-was-called/);
    assert.equal(
      await readFile(path.join(fixtureRoot, "work", "verified-baseline.md"), "utf8"),
      report
    );
  } finally {
    await rm(fixtureRoot, { recursive: true, force: true });
  }
});

test("the real morning CLI passes its sealed compiler context through the real Hermes adapter", async (t) => {
  const fixtureRoot = await realpath(await mkdtemp(path.join(os.tmpdir(), "astock-hermes-attachment-contract-")));
  t.after(() => rm(fixtureRoot, { recursive: true, force: true }));
  for (const directory of ["src", "config", "agents"]) {
    await cp(path.join(repositoryRoot, directory), path.join(fixtureRoot, directory), {
      recursive: true
    });
  }
  await configureSingleFixtureStock(fixtureRoot);
  await mkdir(path.join(fixtureRoot, "scripts"));
  await copyFile(
    path.join(repositoryRoot, "scripts", "morning-report.mjs"),
    path.join(fixtureRoot, "scripts", "morning-report.mjs")
  );
  await mkdir(path.join(fixtureRoot, "data", "inbox"), { recursive: true });

  const workflowPath = path.join(fixtureRoot, "config", "workflow.json");
  const workflow = JSON.parse(await readFile(workflowPath, "utf8"));
  workflow.agentExecutor = "hermes";
  workflow.agentFallback = "none";
  await writeFile(workflowPath, `${JSON.stringify(workflow, null, 2)}\n`);

  const rawContext = validStructuredMorningContext();
  // Independent turnover and breadth evidence creates a real candidate, so the
  // workflow cannot pass this test by skipping the adapter altogether.
  rawContext.sources.push({
    ...rawContext.sources.find((source) => source.id === "market"),
    id: "turnover",
    title: "独立成交额数据",
    url: "https://example.com/turnover"
  });
  rawContext.data.previousAShareSession.turnoverSourceIds = ["turnover"];
  const inputText = `${JSON.stringify(rawContext, null, 2)}\n`;
  const inputPath = path.join(fixtureRoot, "data", "inbox", "morning-context.json");
  await writeFile(inputPath, inputText, { mode: 0o600 });

  const adapterPath = path.join(fixtureRoot, "src", "agent-executor.mjs");
  const adapterSource = await readFile(adapterPath, "utf8");
  const computeStart = adapterSource.indexOf("export function runHermesCompute(");
  const computeEnd = adapterSource.indexOf("export async function runMorningAgent(", computeStart);
  assert.ok(computeStart >= 0 && computeEnd > computeStart, "locate only the compute boundary");
  // Only the copied fixture's external model execution is replaced. The real
  // orchestrator, fixed attachment whitelist, bounded prompt construction and
  // runStructuredAnalyst remain intact; no production path or model is used.
  // Exclusive marker creation also catches any unexpected second invocation.
  const computeStub = [
    "export async function runHermesCompute(request) {",
    `  if (request.projectRoot !== ${JSON.stringify(fixtureRoot)} || request.phase !== "analysis") throw new Error("unexpected offline compute request");`,
    '  const { writeFile } = await import("node:fs/promises");',
    '  await writeFile(path.join(request.projectRoot, "work", "offline-compute-request.json"), JSON.stringify(request), { flag: "wx", mode: 0o600 });',
    '  return { code: 0, stdout: JSON.stringify({ schemaVersion: "2.0", candidateIds: [] }), stderr: "", launcher: "offline-hermes-contract-stub" };',
    "}",
    ""
  ].join("\n");
  await writeFile(adapterPath, adapterSource.slice(0, computeStart) + computeStub + adapterSource.slice(computeEnd));

  const result = await runNode([
    "scripts/morning-report.mjs",
    "--date", "2026-08-10",
    "--input", "data/inbox/morning-context.json",
    "--strict-data",
    "--trading-days-only",
    "--with-structured-analysis",
    "--frozen-cutoff", "2026-08-10T00:00:00Z"
  ], {
    cwd: fixtureRoot,
    // Do not pass the user's credentials, Hermes home or model configuration.
    env: { HOME: fixtureRoot, PATH: process.env.PATH ?? "", LANG: "C.UTF-8" }
  });
  assert.equal(result.signal, null);
  assert.equal(result.exitCode, 0, result.stderr);
  assert.doesNotMatch(result.stderr, /attachments must be exactly|requires exactly three fixed attachments/);

  const workDirectory = path.join(fixtureRoot, "work");
  const lastRun = JSON.parse(await readFile(path.join(workDirectory, "last-run.json"), "utf8"));
  assert.equal(lastRun.status, "success");
  assert.equal(lastRun.publicationGate, "passed");
  assert.equal(lastRun.semanticCandidateCount, 1);
  assert.equal(lastRun.analysisStatus, "skipped_by_analyst", JSON.stringify({
    failures: lastRun.analysisFailures,
    stderr: await readFile(path.join(workDirectory, "structured-analysis-1-stderr.log"), "utf8")
  }));
  assert.equal(lastRun.analysisAttempts, 1);
  assert.deepEqual(lastRun.analysisFailures, []);
  assert.equal(lastRun.analysisLauncher, "offline-hermes-contract-stub");
  assert.equal(lastRun.reportVariant, "verified_baseline");
  assert.equal(await readFile(path.join(workDirectory, "structured-analysis-1-stderr.log"), "utf8"), "");

  const request = JSON.parse(await readFile(path.join(workDirectory, "offline-compute-request.json"), "utf8"));
  assert.equal(request.phase, "analysis");
  assert.equal(request.projectRoot, fixtureRoot);
  const attachments = [...request.prompt.matchAll(
    /<<<ASTOCK_ATTACHMENT_(\d+)_([a-f0-9]{64}) path=("[^"]+") bytes=(\d+)>>>\n([\s\S]*?)\n<<<END_ASTOCK_ATTACHMENT_\1_\2>>>/g
  )];
  // Literal paths are intentional: deriving both the fixture and expectation
  // from STRUCTURED_ANALYSIS_ATTACHMENTS would miss the original regression.
  assert.deepEqual(attachments.map((match) => JSON.parse(match[3])), [
    "agents/structured_analyst.md",
    "work/analysis-instruction.md",
    "work/compiler-context.json"
  ]);
  for (const attachment of attachments) {
    assert.equal(Number(attachment[4]), Buffer.byteLength(attachment[5], "utf8"));
    assert.equal(attachment[2], createHash("sha256").update(attachment[5], "utf8").digest("hex"));
  }
  const frozenInput = JSON.parse(await readFile(path.join(workDirectory, "report-archives", "2026-08-10", "input.json"), "utf8"));
  assert.deepEqual(frozenInput.configuration.notificationPresentation,
    { reportView: "research_brief_v3" }, "the run archives only the presentation choice, not the live policy");
  const compilerText = await readFile(path.join(workDirectory, "compiler-context.json"), "utf8");
  assert.equal(attachments[2][5], compilerText);
  assert.deepEqual(JSON.parse(attachments[2][5]), frozenInput.compilerContext);
  assert.notDeepEqual(frozenInput.compilerContext, frozenInput.rawContext);
  assert.deepEqual(frozenInput.rawContext, rawContext);
  assert.equal(await readFile(inputPath, "utf8"), inputText);
  assert.equal(await readFile(path.join(workDirectory, "morning-context.json"), "utf8"), frozenInput.rawContextText);
  assert.equal(
    await readFile(path.join(fixtureRoot, "reports", "daily", "2026-08-10.md"), "utf8"),
    await readFile(path.join(workDirectory, "report-archives", "2026-08-10", "result", "report.md"), "utf8")
  );
  await assert.rejects(readdir(path.join(workDirectory, "notifications")), /ENOENT/);

  await t.test("facts compile with bad model settings and ignore a scheduled model request", async (scheduledTest) => {
    const scheduledRoot = await realpath(await mkdtemp(path.join(os.tmpdir(), "astock-baseline-only-")));
    scheduledTest.after(() => rm(scheduledRoot, { recursive: true, force: true }));
    for (const directory of ["src", "config", "agents", "scripts", "data"]) {
      await cp(path.join(fixtureRoot, directory), path.join(scheduledRoot, directory), { recursive: true });
    }
    const scheduledWorkflowPath = path.join(scheduledRoot, "config/workflow.json");
    const scheduledWorkflow = JSON.parse(await readFile(scheduledWorkflowPath, "utf8"));
    Object.assign(scheduledWorkflow, {
      agentExecutor: "broken-model-settings", provider: null, model: null,
      hermesProvider: null, hermesModel: null, allowedTools: [],
      maxReviewAttempts: 0, maxAnalysisAttempts: 99,
      scheduledAnalysisAttempts: 2, analysisTimeoutMs: "invalid"
    });
    await writeFile(scheduledWorkflowPath, JSON.stringify(scheduledWorkflow));
    const scheduledAdapterPath = path.join(scheduledRoot, "src/agent-executor.mjs");
    const scheduledAdapter = await readFile(scheduledAdapterPath, "utf8");
    await writeFile(scheduledAdapterPath, scheduledAdapter.replace(
      "export function resolveAgentDescriptor(workflow) {",
      'export function resolveAgentDescriptor(workflow) { throw new Error("FACTS_MUST_NOT_RESOLVE_AN_AGENT");'
    ));
    await writeFile(path.join(scheduledRoot, "fixed-clock.mjs"), [
      'const NativeDate = Date;',
      'globalThis.Date = class extends NativeDate { constructor(...args) { super(...(args.length ? args : ["2026-08-10T00:02:00.000Z"])); } static now() { return NativeDate.parse("2026-08-10T00:02:00.000Z"); } };',
      'globalThis.fetch = () => { throw new Error("OFFLINE_TEST_NETWORK_FORBIDDEN"); };'
    ].join("\n"));
    const scheduled = await runNode([
      "--import", "./fixed-clock.mjs",
      "scripts/morning-report.mjs", "--date", "2026-08-10",
      "--input", "data/inbox/morning-context.json", "--strict-data",
      "--trading-days-only", "--frozen-cutoff", "2026-08-10T00:00:00Z"
    ], {
      cwd: scheduledRoot,
      env: { HOME: scheduledRoot, PATH: process.env.PATH ?? "", LANG: "C.UTF-8" }
    });
    assert.equal(scheduled.exitCode, 0, scheduled.stderr);
    const state = JSON.parse(await readFile(path.join(scheduledRoot, "work", "last-run.json"), "utf8"));
    assert.equal(state.status, "success");
    assert.equal(state.semanticCandidateCount, 1);
    assert.equal(state.analysisStatus, "disabled");
    assert.equal(state.analysisAttempts, 0);
    assert.equal(state.agentExecutor, "none");
    assert.equal(state.model, null);
    await assert.rejects(readFile(path.join(scheduledRoot, "work", "offline-compute-request.json")), /ENOENT/);
    // The seal must execute the same copied compiler era as the real CLI, not
    // this test runner's separate tree (the model boundary is stubbed there).
    const { buildResearchSeal } = await import(pathToFileURL(path.join(scheduledRoot, "src", "xhs-draft.mjs")));
    const sealed = await buildResearchSeal({ projectRoot: scheduledRoot, now: new Date("2026-08-10T00:03:00.000Z") });
    assert.equal(sealed.status, "sealed", JSON.stringify(sealed));
    const proof = JSON.parse(await readFile(path.join(scheduledRoot, sealed.sealFile), "utf8"));
    const digest = JSON.parse(await readFile(path.join(scheduledRoot, proof.source.digestFile), "utf8"));
    assert.equal(digest.items.length, 0);
    assert.ok(digest.limitations.length > 0);
    await mkdir(path.join(scheduledRoot, "work/orchestrator/states"), { recursive: true });
    await writeFile(path.join(scheduledRoot, "work/orchestrator/states/morning-report-2026-08-10.json"), JSON.stringify({
      schemaVersion: "1.0", task: "morning-report", targetDate: "2026-08-10",
      startedAt: "2026-08-10T00:01:00.000Z", completedAt: "2026-08-10T00:02:30.000Z",
      status: "success", childExitCode: 0, childSignal: null, timedOut: false, interrupted: false
    }));
    await scheduledTest.test("structured digest reaches the packaged relay contract without network", async (relayTest) => {
      const consumerRoot = path.resolve(process.env.ASTOCK_CONSUMER_TEST_ROOT
        ?? path.join(repositoryRoot, "..", "signal-bus"));
      try {
        assert.equal((await stat(path.join(consumerRoot, "scripts/research_relay.py"))).isFile(), true);
      } catch (error) {
        if (error?.code !== "ENOENT" || process.env.ASTOCK_CONSUMER_TEST_ROOT) throw error;
        relayTest.skip("cross-repository relay needs ASTOCK_CONSUMER_TEST_ROOT or sibling signal-bus");
        return;
      }
      const consumer = await new Promise((resolve, reject) => {
        const child = spawn("python3", [path.join(repositoryRoot, "test/helpers/consume-lean-digest.py"), scheduledRoot], {
          cwd: repositoryRoot, env: {
            PATH: process.env.PATH ?? "", LANG: "C.UTF-8", PYTHONDONTWRITEBYTECODE: "1",
            ASTOCK_CONSUMER_TEST_ROOT: consumerRoot
          }, stdio: ["ignore", "pipe", "pipe"]
        });
        let stdout = "", stderr = "";
        child.stdout.on("data", (data) => { stdout += data; });
        child.stderr.on("data", (data) => { stderr += data; });
        child.once("error", reject);
        child.once("close", (code) => resolve({ code, stdout, stderr }));
      });
      assert.equal(consumer.code, 0, consumer.stderr);
      const received = JSON.parse(consumer.stdout);
      assert.equal(received.status, "confirmed");
      assert.equal(received.senderCalls, 1);
      assert.equal(received.retry, "already_confirmed");
      console.log("LEAN_DIGEST_RESULT " + JSON.stringify({
        analysisStatus: state.analysisStatus, semanticCandidateCount: state.semanticCandidateCount,
        analysisAttempts: state.analysisAttempts, sealed: sealed.status, digestItems: digest.items.length,
        digestSha256: proof.source.digestSha256, consumer: received
      }));
    });
    for (const explicitSwitch of ["--with-structured-analysis", "--review-existing"]) {
      const explicit = await runNode([
        "--import", "./fixed-clock.mjs", "scripts/morning-report.mjs",
        "--date", "2026-08-10", "--input", "data/inbox/morning-context.json",
        explicitSwitch
      ], {
        cwd: scheduledRoot,
        env: { HOME: scheduledRoot, PATH: process.env.PATH ?? "", LANG: "C.UTF-8" }
      });
      assert.equal(explicit.exitCode, 1, explicit.stdout);
      assert.match(explicit.stderr, /workflow.agentExecutor/);
      assert.doesNotMatch(explicit.stderr, /FACTS_MUST_NOT_RESOLVE_AN_AGENT/);
    }
  });

  await t.test("on-demand overlay preserves unknown-calendar failure before any model or report", async (calendarTest) => {
    const calendarRoot = await realpath(await mkdtemp(path.join(os.tmpdir(), "astock-lean-unknown-calendar-")));
    calendarTest.after(() => rm(calendarRoot, { recursive: true, force: true }));
    for (const directory of ["src", "config", "agents", "scripts", "data"]) {
      await cp(path.join(fixtureRoot, directory), path.join(calendarRoot, directory), { recursive: true });
    }
    const unknown = structuredClone(rawContext);
    unknown.data.tradingCalendar.status = "unknown";
    await writeFile(path.join(calendarRoot, "data/inbox/morning-context.json"), JSON.stringify(unknown));
    const outcome = await runNode([
      "scripts/morning-report.mjs", "--date", "2026-08-10",
      "--input", "data/inbox/morning-context.json", "--strict-data",
      "--trading-days-only", "--with-structured-analysis", "--frozen-cutoff", "2026-08-10T00:00:00Z"
    ], {
      cwd: calendarRoot,
      env: { HOME: calendarRoot, PATH: process.env.PATH ?? "", LANG: "C.UTF-8" }
    });
    assert.equal(outcome.exitCode, 1, outcome.stderr);
    const failed = JSON.parse(await readFile(path.join(calendarRoot, "work/last-run.json"), "utf8"));
    assert.equal(failed.status, "failed");
    assert.equal(failed.failureCode, "trading_calendar_unknown");
    await assert.rejects(readFile(path.join(calendarRoot, "work/offline-compute-request.json")), /ENOENT/);
    await assert.rejects(readFile(path.join(calendarRoot, "reports/daily/2026-08-10.md")), /ENOENT/);
  });

  await t.test("the original caller/allowlist mismatch fails before compute", async (negativeTest) => {
    const negativeRoot = await realpath(await mkdtemp(path.join(os.tmpdir(), "astock-hermes-old-contract-")));
    negativeTest.after(() => rm(negativeRoot, { recursive: true, force: true }));
    // Copy only the synthetic fixture inputs/code, not its successful archive.
    for (const directory of ["src", "config", "agents", "scripts", "data"]) {
      await cp(path.join(fixtureRoot, directory), path.join(negativeRoot, directory), { recursive: true });
    }
    const negativeAdapterPath = path.join(negativeRoot, "src", "agent-executor.mjs");
    const negativeAdapter = await readFile(negativeAdapterPath, "utf8");
    assert.ok(negativeAdapter.includes(JSON.stringify(fixtureRoot)));
    await writeFile(negativeAdapterPath, negativeAdapter
      .replace(JSON.stringify(fixtureRoot), JSON.stringify(negativeRoot))
      .replace('"work/compiler-context.json"', '"work/morning-context.json"'));

    // The fixed CLI shares the adapter constant, so reverting that constant
    // alone would change both sides. Restore the incident's independent caller
    // list in this fixture to reproduce the actual disagreement deliberately.
    const negativeCliPath = path.join(negativeRoot, "scripts", "morning-report.mjs");
    const negativeCli = await readFile(negativeCliPath, "utf8");
    const sharedCaller = "const structuredAttachments = STRUCTURED_ANALYSIS_ATTACHMENTS;";
    assert.ok(negativeCli.includes(sharedCaller));
    await writeFile(negativeCliPath, negativeCli.replace(sharedCaller,
      'const structuredAttachments = ["agents/structured_analyst.md", "work/analysis-instruction.md", "work/compiler-context.json"];'));

    const negativeResult = await runNode([
      "scripts/morning-report.mjs", "--date", "2026-08-10",
      "--input", "data/inbox/morning-context.json", "--strict-data",
      "--trading-days-only", "--with-structured-analysis", "--frozen-cutoff", "2026-08-10T00:00:00Z"
    ], {
      cwd: negativeRoot,
      env: { HOME: negativeRoot, PATH: process.env.PATH ?? "", LANG: "C.UTF-8" }
    });
    // The outer workflow still succeeds via its baseline, as in the incident;
    // that exit code must not be mistaken for a successful analysis invocation.
    assert.equal(negativeResult.exitCode, 0, negativeResult.stderr);
    const negativeWork = path.join(negativeRoot, "work");
    const negativeState = JSON.parse(await readFile(path.join(negativeWork, "last-run.json"), "utf8"));
    assert.equal(negativeState.analysisStatus, "degraded");
    assert.equal(negativeState.analysisAttempts, 2);
    assert.deepEqual(negativeState.analysisFailures, [
      { attempt: 1, reasonCode: "analysis_execution_failed" },
      { attempt: 2, reasonCode: "analysis_execution_failed" }
    ]);
    for (const attempt of [1, 2]) {
      assert.match(await readFile(path.join(negativeWork, `structured-analysis-${attempt}-stderr.log`), "utf8"),
        /Hermes analysis attachments must be exactly:.*work\/morning-context\.json/);
    }
    await assert.rejects(readFile(path.join(negativeWork, "offline-compute-request.json")), /ENOENT/);
    assert.equal(await readFile(path.join(negativeRoot, "data", "inbox", "morning-context.json"), "utf8"), inputText);
  });
});

test("publish-reviewed uses the archived cutoff/context and cannot replace a sealed result", async (t) => {
  const fixtureRoot = await mkdtemp(path.join(os.tmpdir(), "astock-reviewed-archive-"));
  t.after(() => rm(fixtureRoot, { recursive: true, force: true }));
  for (const name of ["src", "config"]) await cp(path.join(repositoryRoot, name), path.join(fixtureRoot, name), { recursive: true });
  await mkdir(path.join(fixtureRoot, "scripts"));
  await copyFile(path.join(repositoryRoot, "scripts/publish-reviewed.mjs"), path.join(fixtureRoot, "scripts/publish-reviewed.mjs"));
  await configureSingleFixtureStock(fixtureRoot);
  const { stocks, risk, workflow } = await loadProjectConfig(fixtureRoot);
  const rawContext = validStructuredMorningContext();
  const frozen = await freezeRunInput({ projectRoot: fixtureRoot, archiveRoot: path.join(fixtureRoot, "work/report-archives"),
    outputDirectory: path.join(fixtureRoot, "reports/daily"), rawContext, configuration: { stocks, risk, workflow },
    targetDate: "2026-08-10", cutoffInstant: "2026-08-10T00:00:00Z", cutoffLocal: "2026-08-10 08:00" });
  const report = compileFrozenReport(frozen.input);
  await sealRunResult(frozen, { report, analysisStatus: "skipped_no_semantic_candidate", mode: "verified_baseline" });
  await writeFile(path.join(fixtureRoot, "work/morning-context.json"), frozen.input.rawContextText);
  await writeFile(path.join(fixtureRoot, "work/last-pi-output.md"), report);
  await writeFile(path.join(fixtureRoot, "work/last-run.json"), JSON.stringify({
    targetDate: "2026-08-10", status: "failed", startedAt: "2026-08-10T01:00:00Z",
    frozenCutoffInstant: frozen.input.cutoffInstant, cutoffLocal: frozen.input.cutoffLocal,
    archiveInputSha256: frozen.inputSha256, publicationLevel: DATA_BRIEF_PUBLICATION_LEVEL
  }));
  const first = await runNode(["scripts/publish-reviewed.mjs"], { cwd: fixtureRoot, env: process.env });
  assert.equal(first.exitCode, 0, first.stderr);
  const output = path.join(fixtureRoot, "reports/daily/2026-08-10.md");
  assert.equal(await readFile(output, "utf8"), report);
  const retry = await runNode(["scripts/publish-reviewed.mjs"], { cwd: fixtureRoot, env: process.env });
  assert.equal(retry.exitCode, 0, retry.stderr);
  rawContext.meta.provider = "changed-input";
  await writeFile(path.join(fixtureRoot, "work/morning-context.json"), JSON.stringify(rawContext));
  const changed = await runNode(["scripts/publish-reviewed.mjs"], { cwd: fixtureRoot, env: process.env });
  assert.notEqual(changed.exitCode, 0);
  assert.match(changed.stderr, /immutable run inputs changed/);
  assert.equal(await readFile(output, "utf8"), report);
});
