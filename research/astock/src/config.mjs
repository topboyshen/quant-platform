import { readFile } from "node:fs/promises";
import path from "node:path";

const ALLOWED_DATA_BRIDGES = new Map([
  [".venv/scripts/python.exe", "mcp/akshare_bridge.py"],
  [".venv/bin/python", "mcp/akshare_bridge.py"]
]);
const READ_ONLY_AGENT_TOOLS = new Set([
  "web_search",
  "source_check",
  "fetch_content",
  "get_search_content"
]);

export async function readJsonFile(filePath) {
  let text;
  try {
    text = await readFile(filePath, "utf8");
  } catch (error) {
    throw new Error(`无法读取 JSON 文件 ${filePath}: ${error.message}`);
  }

  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`JSON 格式错误 ${filePath}: ${error.message}`);
  }
}

export function resolveProjectPath(projectRoot, relativePath) {
  const root = path.resolve(projectRoot);
  const resolved = path.resolve(root, relativePath);
  if (resolved !== root && !resolved.startsWith(root + path.sep)) {
    throw new Error(`路径必须位于项目内: ${relativePath}`);
  }
  return resolved;
}

function normalizeProjectRelativePath(value, fieldName) {
  if (typeof value !== "string" || value.trim() === "" || path.isAbsolute(value)) {
    throw new Error(`${fieldName} 必须是项目内相对路径`);
  }
  const normalized = path.normalize(value).replaceAll("\\", "/");
  if (normalized === ".." || normalized.startsWith("../")) {
    throw new Error(`${fieldName} 不得离开项目目录`);
  }
  return normalized;
}

export function resolveDataBridgeDescriptor(projectRoot, workflow) {
  const descriptor = workflow?.dataBridge;
  if (!descriptor || typeof descriptor !== "object" || Array.isArray(descriptor)) {
    throw new Error("workflow.dataBridge 必须是固定 executable + args 描述符");
  }

  const executableRelative = normalizeProjectRelativePath(
    descriptor.executable,
    "workflow.dataBridge.executable"
  );
  if (!Array.isArray(descriptor.args) || descriptor.args.length !== 1) {
    throw new Error("workflow.dataBridge.args 必须只包含获准的桥接脚本");
  }
  const scriptRelative = normalizeProjectRelativePath(
    descriptor.args[0],
    "workflow.dataBridge.args[0]"
  );
  const allowedScript = ALLOWED_DATA_BRIDGES.get(executableRelative.toLowerCase());
  if (!allowedScript || scriptRelative.toLowerCase() !== allowedScript) {
    throw new Error(
      "数据桥接仅允许项目内 .venv Python 执行 mcp/akshare_bridge.py"
    );
  }

  return {
    executable: resolveProjectPath(projectRoot, executableRelative),
    args: [resolveProjectPath(projectRoot, scriptRelative)],
    display: `${executableRelative} ${scriptRelative}`
  };
}

export function validateIsoDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new Error(`日期必须是 YYYY-MM-DD: ${value}`);
  }
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== value) {
    throw new Error(`无效日期: ${value}`);
  }
  return value;
}

export function dateInTimeZone(timeZone, now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(now);
  const get = (type) => parts.find((part) => part.type === type)?.value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

export function dateTimeInTimeZone(timeZone, now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23"
  }).formatToParts(now);
  const get = (type) => parts.find((part) => part.type === type)?.value;
  return {
    date: `${get("year")}-${get("month")}-${get("day")}`,
    time: `${get("hour")}:${get("minute")}`,
    display: `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}`
  };
}

export async function loadProjectConfig(projectRoot, options = {}) {
  const names = ["stocks", "strategy", "risk", "workflow", "data-sources"];
  const loaded = {};
  for (const name of names) {
    loaded[name.replace("-", "_")] = await readJsonFile(
      resolveProjectPath(projectRoot, `config/${name}.json`)
    );
  }
  validateProjectConfig(loaded, options);
  return loaded;
}

export function validateAllowedAgentTools(allowedTools) {
  if (!Array.isArray(allowedTools) || allowedTools.length === 0) {
    throw new Error("workflow.allowedTools 必须显式限制 Agent 可用工具");
  }
  if (new Set(allowedTools).size !== allowedTools.length) {
    throw new Error("workflow.allowedTools 不得包含重复工具");
  }
  const forbidden = allowedTools.filter((tool) => !READ_ONLY_AGENT_TOOLS.has(tool));
  if (forbidden.length > 0) {
    throw new Error(`workflow.allowedTools 只允许固定只读投研工具；拒绝: ${forbidden.join(", ")}`);
  }
  return allowedTools;
}

function validateIntegerRange(value, fieldName, minimum, maximum) {
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${fieldName} 必须是 ${minimum}—${maximum} 的整数`);
  }
}

export function validateAgentWorkflowConfig(workflow) {
  if (!workflow || typeof workflow !== "object" || Array.isArray(workflow)) {
    throw new Error("workflow 必须是配置对象");
  }
  if (!new Set(["hermes", "pi"]).has(workflow.agentExecutor)) {
    throw new Error("workflow.agentExecutor 只允许 hermes 或 pi");
  }
  if (workflow.agentFallback !== "none") {
    throw new Error("workflow.agentFallback 必须固定为 none；执行器失败时禁止自动双跑");
  }
  if (workflow.reportMode !== "structured_compiler") {
    throw new Error("workflow.reportMode 必须固定为 structured_compiler");
  }
  validateIntegerRange(
    workflow.maxAnalysisAttempts,
    "workflow.maxAnalysisAttempts",
    1,
    2
  );
  validateIntegerRange(
    workflow.scheduledAnalysisAttempts,
    "workflow.scheduledAnalysisAttempts",
    0,
    workflow.maxAnalysisAttempts
  );
  validateIntegerRange(
    workflow.analysisTimeoutMs,
    "workflow.analysisTimeoutMs",
    1000,
    600000
  );

  // Keep the former Pi backend pinned and validated as an explicit rollback
  // path, even when Hermes is the selected production executor.
  if (workflow.provider !== "ollama-cloud" || workflow.model !== "deepseek-v4-pro") {
    throw new Error("Pi 回滚后端必须固定为 ollama-cloud/deepseek-v4-pro");
  }
  if (!new Set(["off", "minimal", "low", "medium", "high", "xhigh", "max"]).has(workflow.thinking)) {
    throw new Error(`不支持的 Pi thinking 级别: ${workflow.thinking}`);
  }
  validateIntegerRange(workflow.maxAgentTurns, "workflow.maxAgentTurns", 5, 100);
  validateIntegerRange(workflow.piTimeoutMs, "workflow.piTimeoutMs", 1000, 1800000);
  validateIntegerRange(workflow.reviewerTimeoutMs, "workflow.reviewerTimeoutMs", 1000, 1800000);
  validateAllowedAgentTools(workflow.allowedTools);

  if (typeof workflow.allowAgentWebFallback !== "boolean") {
    throw new Error("workflow.allowAgentWebFallback 必须是显式布尔值");
  }
  if (workflow.agentExecutor === "hermes") {
    if (workflow.hermesProvider !== "kimi-coding" || workflow.hermesModel !== "k3-256k") {
      throw new Error("Hermes 计算后端必须固定为 kimi-coding/k3-256k");
    }
    if (workflow.hermesReasoning !== "high") {
      throw new Error("workflow.hermesReasoning 必须固定为 high");
    }
    validateIntegerRange(
      workflow.hermesMaxAgentTurns,
      "workflow.hermesMaxAgentTurns",
      1,
      12
    );
    validateIntegerRange(
      workflow.hermesReviewerMaxTurns,
      "workflow.hermesReviewerMaxTurns",
      1,
      12
    );
    validateIntegerRange(
      workflow.hermesAnalysisMaxTurns,
      "workflow.hermesAnalysisMaxTurns",
      1,
      5
    );
    validateIntegerRange(
      workflow.hermesTimeoutMs,
      "workflow.hermesTimeoutMs",
      1000,
      1800000
    );
    validateIntegerRange(
      workflow.hermesReviewerTimeoutMs,
      "workflow.hermesReviewerTimeoutMs",
      1000,
      1800000
    );
    if (workflow.allowAgentWebFallback !== false) {
      throw new Error("Hermes 晨报执行器必须关闭 Agent 联网降级");
    }
  }
}

export function validateProjectConfig(config, { requireAgent = true } = {}) {
  const { stocks, strategy, risk, workflow } = config;
  if (!Array.isArray(stocks?.corePool) || stocks.corePool.length === 0) {
    throw new Error("config/stocks.json 的 corePool 不能为空");
  }

  const codes = new Set();
  for (const stock of stocks.corePool) {
    if (!/^\d{6}$/.test(stock.code ?? "")) {
      throw new Error(`股票代码无效: ${stock.code}`);
    }
    if (!["SH", "SZ", "BJ"].includes(stock.exchange)) {
      throw new Error(`交易所代码无效: ${stock.exchange}`);
    }
    if (!stock.name || !stock.symbol) {
      throw new Error(`股票 ${stock.code} 缺少 name 或 symbol`);
    }
    if (codes.has(stock.code)) {
      throw new Error(`股票代码重复: ${stock.code}`);
    }
    codes.add(stock.code);
  }

  if (!workflow || typeof workflow !== "object" || Array.isArray(workflow)) {
    throw new Error("workflow 必须是配置对象");
  }
  if (workflow.reportMode !== "structured_compiler") {
    throw new Error("workflow.reportMode 必须固定为 structured_compiler");
  }
  if (requireAgent) validateAgentWorkflowConfig(workflow);
  if (workflow.timezone !== "Asia/Shanghai" || workflow.scheduledTime !== "08:00") {
    throw new Error("晨报时区与计划时间必须固定为 Asia/Shanghai 08:00");
  }
  // Validate the fixed bridge descriptor without requiring the virtual
  // environment to be installed during static configuration checks.
  resolveDataBridgeDescriptor(process.cwd(), workflow);
  validateIntegerRange(workflow.bridgeTimeoutMs, "workflow.bridgeTimeoutMs", 1000, 600000);
  if (requireAgent && (
    !Number.isInteger(workflow.maxReviewAttempts) ||
    workflow.maxReviewAttempts < 1 ||
    workflow.maxReviewAttempts > 3
  )) {
    throw new Error("workflow.maxReviewAttempts 必须是 1—3 的整数");
  }
  if (!strategy?.timezone || strategy.timezone !== workflow.timezone) {
    throw new Error("strategy 与 workflow 的时区必须一致");
  }
  if (!risk?.disclaimer || risk.neverInventMissingData !== true) {
    throw new Error("risk 配置必须包含免责声明并禁止编造数据");
  }
}
