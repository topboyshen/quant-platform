#!/usr/bin/env node

import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  dateInTimeZone,
  loadProjectConfig,
  validateIsoDate
} from "../src/config.mjs";
import { buildMinimalBridgeEnvironment } from "../src/data.mjs";
import {
  buildKronosRequest,
  unavailableKronos
} from "../src/kronos.mjs";
import { isMainEntry } from "../src/main-entry.mjs";

const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PYTHON = path.join(PROJECT_ROOT, ".venv", "bin", "python");
const STOCK_RESEARCH_CLIENT = path.join(PROJECT_ROOT, "mcp", "stock_research_client.py");
const KRONOS_PYTHON = "/opt/ai/venvs/kronos/bin/python";
const KRONOS_BRIDGE = path.join(PROJECT_ROOT, "mcp", "kronos_bridge.py");
const MAX_STDIN_BYTES = 256 * 1024;
const MAX_OUTPUT_BYTES = 16 * 1024 * 1024;
const CLIENT_TIMEOUT_MS = 210_000;
const MAX_KRONOS_INPUT_BYTES = 4 * 1024 * 1024;
const MAX_KRONOS_OUTPUT_BYTES = 4 * 1024 * 1024;
const KRONOS_TIMEOUT_MS = 120_000;

export const STOCK_RESEARCH_TOOL_ALLOWLIST = Object.freeze([
  "get_stock_price",
  "get_financial_report",
  "get_news",
  "get_sector_rank",
  "get_capital_flow",
  "run_backtest"
]);

export const INVESTMENT_MEMO_SECTIONS = Object.freeze([
  "结论摘要",
  "行情与技术",
  "财务与机构",
  "行业与资金",
  "新闻催化",
  "Qlib评分与回测",
  "Kronos状态",
  "人工规则/反证",
  "风险与数据缺口"
]);

export function normalizeResearchInput(value, { stocks, workflow, now = new Date() }) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("研究请求必须是 JSON 对象");
  }
  const allowedKeys = new Set(["question", "targetDate", "symbols"]);
  const unexpected = Object.keys(value).filter((key) => !allowedKeys.has(key));
  if (unexpected.length > 0) {
    throw new Error("研究请求包含未允许字段");
  }

  const question = typeof value.question === "string" ? value.question.trim() : "";
  if (question.length < 2 || question.length > 500) {
    throw new Error("question 长度必须为 2—500 个字符");
  }
  const targetDate = value.targetDate
    ? validateIsoDate(value.targetDate)
    : dateInTimeZone(workflow.timezone, now);

  const configured = new Map(stocks.corePool.map((stock) => [stock.symbol, stock]));
  let selected = stocks.corePool;
  if (value.symbols !== undefined) {
    if (!Array.isArray(value.symbols) || value.symbols.length === 0 || value.symbols.length > 10) {
      throw new Error("symbols 必须是 1—10 个配置股票代码组成的数组");
    }
    const unique = [...new Set(value.symbols)];
    if (unique.length !== value.symbols.length) throw new Error("symbols 不得重复");
    selected = unique.map((symbol) => {
      if (typeof symbol !== "string" || !/^\d{6}\.(SH|SZ|BJ)$/.test(symbol)) {
        throw new Error("股票代码格式无效");
      }
      const stock = configured.get(symbol);
      if (!stock) throw new Error("股票不在受控核心池中");
      return stock;
    });
  }
  return { question, targetDate, stocks: { ...stocks, corePool: selected } };
}

function dateBefore(targetDate, days) {
  const value = new Date(`${targetDate}T00:00:00Z`);
  value.setUTCDate(value.getUTCDate() - days);
  return value.toISOString().slice(0, 10);
}

export function buildResearchToolPlan({ targetDate, symbols }) {
  const priceStart = dateBefore(targetDate, 240);
  return [
    {
      tool: "get_stock_price",
      calls: symbols.map((symbol) => ({
        symbol,
        start_date: priceStart,
        end_date: targetDate,
        limit: 120,
        adjust: "qfq"
      }))
    },
    {
      tool: "get_financial_report",
      calls: symbols.map((symbol) => ({ symbol, as_of: targetDate }))
    },
    {
      tool: "get_news",
      calls: symbols.map((symbol) => ({ symbol, limit: 20, as_of: targetDate }))
    },
    { tool: "get_sector_rank", calls: [{ sector_type: "industry", limit: 10 }] },
    {
      tool: "get_capital_flow",
      calls: symbols.map((symbol) => ({ symbol, indicator: "5日", limit: 10 }))
    },
    {
      tool: "run_backtest",
      calls: [{ target_date: targetDate, symbols }]
    }
  ];
}

function terminateChild(child) {
  try {
    child.kill();
  } catch {
    // The child may already have exited; retain the primary result.
  }
}

export function runFixedResearchClient(input, {
  spawnImpl = spawn,
  timeoutMs = CLIENT_TIMEOUT_MS,
  maxOutputBytes = MAX_OUTPUT_BYTES
} = {}) {
  if (!Number.isInteger(timeoutMs) || timeoutMs <= 0 || timeoutMs > CLIENT_TIMEOUT_MS) {
    return Promise.reject(new Error("只读研究子进程超时配置无效"));
  }
  let serialized;
  try {
    serialized = JSON.stringify(input);
  } catch {
    return Promise.reject(new Error("只读研究请求无法序列化"));
  }
  if (Buffer.byteLength(serialized, "utf8") > MAX_STDIN_BYTES) {
    return Promise.reject(new Error("只读研究请求超过 256 KiB"));
  }

  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawnImpl(PYTHON, [STOCK_RESEARCH_CLIENT], {
        cwd: PROJECT_ROOT,
        env: buildMinimalBridgeEnvironment(),
        shell: false,
        windowsHide: true,
        stdio: ["pipe", "pipe", "pipe"]
      });
    } catch {
      reject(new Error("无法启动只读研究子进程"));
      return;
    }

    let stdout = "";
    let settled = false;
    let timer;
    const finish = (callback) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      callback();
    };
    timer = setTimeout(() => {
      finish(() => {
        terminateChild(child);
        reject(new Error("只读研究子进程超时"));
      });
    }, timeoutMs);

    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      if (Buffer.byteLength(stdout, "utf8") > maxOutputBytes) {
        finish(() => {
          terminateChild(child);
          reject(new Error("只读研究子进程响应超过 16 MiB"));
        });
      }
    });
    // Drain but never retain or relay stderr; it may contain provider secrets.
    child.stderr.on("data", () => {});
    child.on("error", () => finish(() => reject(new Error("无法启动只读研究子进程"))));
    child.on("close", (code) => {
      finish(() => {
        if (code !== 0) {
          reject(new Error(
            `只读研究子进程失败（exit ${Number.isInteger(code) ? code : "unknown"}）`
          ));
          return;
        }
        let parsed;
        try {
          parsed = JSON.parse(stdout);
        } catch {
          reject(new Error("只读研究子进程未返回有效 JSON"));
          return;
        }
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          reject(new Error("只读研究子进程返回结构无效"));
          return;
        }
        resolve(parsed);
      });
    });
    child.stdin.on("error", () => {});
    child.stdin.end(serialized);
  });
}

export function runFixedKronosBridge(input, {
  spawnImpl = spawn,
  timeoutMs = KRONOS_TIMEOUT_MS,
  maxOutputBytes = MAX_KRONOS_OUTPUT_BYTES
} = {}) {
  if (!Number.isInteger(timeoutMs) || timeoutMs <= 0 || timeoutMs > KRONOS_TIMEOUT_MS) {
    return Promise.reject(new Error("Kronos 子进程超时配置无效"));
  }
  let serialized;
  try {
    serialized = JSON.stringify(input);
  } catch {
    return Promise.reject(new Error("Kronos 请求无法序列化"));
  }
  if (Buffer.byteLength(serialized, "utf8") > MAX_KRONOS_INPUT_BYTES) {
    return Promise.reject(new Error("Kronos 请求超过 4 MiB"));
  }

  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawnImpl(KRONOS_PYTHON, [KRONOS_BRIDGE], {
        cwd: PROJECT_ROOT,
        env: buildMinimalBridgeEnvironment(),
        shell: false,
        windowsHide: true,
        stdio: ["pipe", "pipe", "pipe"]
      });
    } catch {
      reject(new Error("无法启动 Kronos 子进程"));
      return;
    }
    let stdout = "";
    let settled = false;
    let timer;
    const finish = (callback) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      callback();
    };
    timer = setTimeout(() => finish(() => {
      terminateChild(child);
      reject(new Error("Kronos 子进程超时"));
    }), timeoutMs);
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      if (Buffer.byteLength(stdout, "utf8") > maxOutputBytes) {
        finish(() => {
          terminateChild(child);
          reject(new Error("Kronos 响应超过 4 MiB"));
        });
      }
    });
    // Model/runtime diagnostics are not relayed to Pi.
    child.stderr.on("data", () => {});
    child.on("error", () => finish(() => reject(new Error("无法启动 Kronos 子进程"))));
    child.on("close", (code) => finish(() => {
      if (code !== 0) {
        reject(new Error("Kronos 子进程失败"));
        return;
      }
      try {
        const parsed = JSON.parse(stdout);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          reject(new Error("Kronos 响应结构无效"));
          return;
        }
        resolve(parsed);
      } catch {
        reject(new Error("Kronos 未返回有效 JSON"));
      }
    }));
    child.stdin.on("error", () => {});
    child.stdin.end(serialized);
  });
}

function validateClientEnvelope(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("股票研究 MCP 返回结构无效");
  }
  const names = value.allowedTools;
  if (
    value.transport !== "stdio" ||
    !Array.isArray(names) ||
    names.length !== STOCK_RESEARCH_TOOL_ALLOWLIST.length ||
    new Set(names).size !== names.length ||
    STOCK_RESEARCH_TOOL_ALLOWLIST.some((name) => !names.includes(name))
  ) {
    throw new Error("股票研究 MCP 工具白名单不匹配");
  }
  return value;
}

function qlibFromStockResearch(stock, dryRun) {
  if (dryRun) {
    return {
      status: "planned_not_executed",
      tool: "run_backtest",
      note: "dry-run 未读取 Qlib 历史评分或回测产物。"
    };
  }
  const call = stock?.tools?.run_backtest?.calls?.[0];
  if (call?.status === "ok" && call.result && typeof call.result === "object") {
    if (call.result.usableForTargetDate === false) {
      return {
        status: "not_usable_for_target_date",
        tool: "run_backtest",
        result: call.result,
        note: "Qlib 产物晚于目标日期，只能披露为不可用于该时点的历史背景。"
      };
    }
    if (call.result.predictionRanking === null) {
      return {
        status: "partial",
        tool: "run_backtest",
        result: call.result,
        note: "Qlib 历史回测摘要可用，但指定股票的横截面评分/排名不可用。"
      };
    }
    return { status: "available", tool: "run_backtest", result: call.result };
  }
  return {
    status: "unavailable",
    tool: "run_backtest",
    error: call?.error?.code ?? "missing_result",
    note: "Qlib 评分/历史回测不可用，不得外推或补造结论。"
  };
}

function validateKronosEnvelope(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    value.schemaVersion !== "1.0" ||
    value.task !== "kronos_market_state" ||
    value.status !== "experimental" ||
    !Array.isArray(value.results)
  ) {
    throw new Error("Kronos 响应契约不匹配");
  }
  return value;
}

function manualRules(stocks) {
  return stocks.corePool.map((stock) => ({
    symbol: stock.symbol,
    name: stock.name,
    hypotheses: stock.researchTags ?? [],
    hypothesisStatus: stock.tagStatus ?? "user_hypothesis_to_verify",
    monitor: stock.monitor ?? [],
    counterEvidence: stock.invalidationSignals ?? []
  }));
}

export async function buildResearchBundle(value, {
  now = new Date(),
  dryRun = false,
  config: suppliedConfig,
  runClient = runFixedResearchClient,
  runKronos = runFixedKronosBridge
} = {}) {
  const config = suppliedConfig ?? await loadProjectConfig(PROJECT_ROOT);
  const normalized = normalizeResearchInput(value, { ...config, now });
  const symbols = normalized.stocks.corePool.map((stock) => stock.symbol);
  const clientRequest = { targetDate: normalized.targetDate, symbols };
  const toolPlan = buildResearchToolPlan(clientRequest);

  const stock = dryRun
    ? {
        schemaVersion: "1.0",
        transport: "stdio",
        status: "dry_run",
        skipped: true,
        allowedTools: [...STOCK_RESEARCH_TOOL_ALLOWLIST]
      }
    : validateClientEnvelope(await runClient(clientRequest));

  let kronos;
  if (dryRun) {
    kronos = {
      schemaVersion: "1.0",
      task: "kronos_market_state",
      status: "planned_not_executed",
      prediction: null,
      note: "dry-run 未加载本地 Kronos 模型。"
    };
  } else {
    const kronosRequest = buildKronosRequest(stock, { lookback: 100, predictionDays: 5 });
    if (kronosRequest === null) {
      kronos = unavailableKronos("insufficient_price_history");
    } else {
      try {
        kronos = validateKronosEnvelope(await runKronos(kronosRequest));
      } catch {
        kronos = unavailableKronos("model_or_runtime_unavailable");
      }
    }
  }

  return {
    schemaVersion: "1.0",
    generatedAt: now.toISOString(),
    query: {
      question: normalized.question,
      targetDate: normalized.targetDate,
      timezone: config.workflow.timezone,
      symbols
    },
    pipeline: [
      "fixed stdio MCP: mcp/stock_research_server.py",
      "six allowlisted fine-grained research tools",
      "Qlib fixed read-only scoring and historical backtest artifacts",
      "Kronos-mini fixed local experimental state inference",
      "Pi evidence-based Chinese investment memo"
    ],
    toolPlan,
    stock,
    qlib: qlibFromStockResearch(stock, dryRun),
    kronos,
    manualResearchRules: manualRules(normalized.stocks),
    memoContract: {
      language: "zh-CN",
      requiredSections: [...INVESTMENT_MEMO_SECTIONS],
      factInferenceSeparation: "required",
      requirements: [
        "事实必须附来源和数据截止时间；推断必须显式标注并说明依据。",
        "用户研究标签仅是假设，须以反证规则检验，不得写成已证实事实。",
        "缺失或不可用的数据必须原样披露，不得编造预测、收益或机构结论。"
      ]
    },
    guardrails: {
      externalState: "read_only",
      shellInputInterpolation: false,
      canTrade: false,
      canPublish: false,
      canSchedule: false,
      untrustedFields: ["stock.tools.*.calls[*].result", "qlib.result", "kronos.results"]
    }
  };
}

async function readStdin() {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of process.stdin) {
    bytes += chunk.length;
    if (bytes > MAX_STDIN_BYTES) throw new Error("stdin 超过 256 KiB");
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString("utf8");
  try {
    return JSON.parse(text || "{}");
  } catch {
    throw new Error("stdin 不是有效 JSON");
  }
}

async function main() {
  const allowedArgs = new Set(["--dry-run"]);
  const unexpected = process.argv.slice(2).filter((arg) => !allowedArgs.has(arg));
  if (unexpected.length > 0) throw new Error("存在不支持的参数");
  const result = await buildResearchBundle(await readStdin(), {
    dryRun: process.argv.includes("--dry-run")
  });
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

if (isMainEntry(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`research context failed: ${error.message}\n`);
    process.exitCode = 1;
  });
}
