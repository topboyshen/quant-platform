#!/usr/bin/env node
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  loadProjectConfig,
  readJsonFile,
  resolveProjectPath
} from "../src/config.mjs";
import { validateMorningContext } from "../src/data.mjs";
import { resolveAgentDescriptor } from "../src/agent-executor.mjs";
import { getTaskCatalog } from "../src/task-orchestrator.mjs";
import { validateXhsConfig } from "../src/xhs-draft.mjs";
import { validateDeliveryPolicy } from "../src/notification-contract.mjs";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function main() {
  const config = await loadProjectConfig(projectRoot);
  const agentDescriptor = resolveAgentDescriptor(config.workflow);
  if (agentDescriptor.executor === "pi") {
    const projectSettings = await readJsonFile(
      resolveProjectPath(projectRoot, ".pi/settings.json")
    );
    if (
      projectSettings.defaultProvider !== config.workflow.provider ||
      projectSettings.defaultModel !== config.workflow.model ||
      projectSettings.defaultThinkingLevel !== config.workflow.thinking
    ) {
      throw new Error("Pi 回滚配置与 config/workflow.json 不一致");
    }
  }

  const sample = await readJsonFile(
    resolveProjectPath(projectRoot, "data/samples/morning-context.example.json")
  );
  validateMorningContext(sample);

  await readJsonFile(resolveProjectPath(projectRoot, "mcp/contract.schema.json"));
  await readJsonFile(resolveProjectPath(projectRoot, "mcp/bridge.config.example.json"));
  const xhsConfig = validateXhsConfig(await readJsonFile(
    resolveProjectPath(projectRoot, "config/xhs.json")
  ));
  const xhsSchema = await readJsonFile(
    resolveProjectPath(projectRoot, "schemas/xhs-draft-v1.schema.json")
  );
  if (
    xhsSchema?.properties?.publishEligible?.const !== false ||
    xhsSchema?.properties?.humanApprovalRequired?.const !== true ||
    xhsSchema?.properties?.contentMode?.const !== "facts_only" ||
    xhsConfig.autoPublishEnabled !== false
  ) {
    throw new Error("XHS 草稿 schema 不再保证只产草稿、禁用自动发布和事实模式");
  }
  validateDeliveryPolicy(await readJsonFile(
    resolveProjectPath(projectRoot, "config/notifications.json")
  ));
  const taskRoutes = getTaskCatalog().routes.map((route) => `${route.origin}:${route.task}`);
  if (
    taskRoutes.length !== 2 ||
    taskRoutes[0] !== "cron:morning-report" ||
    taskRoutes[1] !== "pi:research-context"
  ) {
    throw new Error("任务编排白名单与固定 cron/Pi 路由不一致");
  }

  const prompt = await readFile(
    resolveProjectPath(projectRoot, "agents/morning_report.md"),
    "utf8"
  );
  const requiredPhrases = ["不得猜测", "数据缺口", "[事实]", "[推断]", "[情景]", "免责声明"];
  for (const phrase of requiredPhrases) {
    if (!prompt.includes(phrase)) {
      throw new Error(`Agent Prompt 缺少关键约束: ${phrase}`);
    }
  }
  const reviewerPrompt = await readFile(
    resolveProjectPath(projectRoot, "agents/report_reviewer.md"),
    "utf8"
  );
  const structuredPrompt = await readFile(
    resolveProjectPath(projectRoot, "agents/structured_analyst.md"),
    "utf8"
  );
  for (const phrase of [
    "只输出 `analysis-instruction.md` 指定的单个 JSON 对象",
    "不得写股票池中的名称、代码或 symbol",
    "`evidenceIds` 与 `counterEvidenceIds` 必须互不重复",
    "自动降级为程序核验基础版",
    "总数最多 8 项"
  ]) {
    if (!structuredPrompt.includes(phrase)) {
      throw new Error(`结构化分析 Prompt 缺少关键约束: ${phrase}`);
    }
  }
  if (structuredPrompt.includes("最多选择一个候选")) {
    throw new Error("结构化分析 Prompt 不得再把总数上限写成最多一个候选");
  }
  const presentationContracts = [
    ["研究 Prompt", prompt, ["轻量美化 v1", "移动端优先", "核心数据（固定格式）"]],
    ["审校 Prompt", reviewerPrompt, ["轻量美化 v1", "稳定机器锚点", "不得改成普通五列表格"]]
  ];
  for (const [label, content, phrases] of presentationContracts) {
    for (const phrase of phrases) {
      if (!content.includes(phrase)) {
        throw new Error(`${label} 缺少版式约束: ${phrase}`);
      }
    }
  }

  console.log("配置、Agent Prompt、示例数据与 MCP 契约验证通过。");
  console.log(`执行器固定为：${agentDescriptor.executor}`);
  console.log(`模型固定为：${agentDescriptor.provider}/${agentDescriptor.model}`);
  console.log(`股票池数量：${config.stocks.corePool.length}`);
  console.log("小红书适配器固定为：离线草稿 / 禁止自动发布 / 需人工批准");
}

main().catch((error) => {
  console.error(`验证失败：${error.message}`);
  process.exitCode = 1;
});
