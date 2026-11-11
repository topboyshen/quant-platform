import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import os from "node:os";
import path from "node:path";

export function shouldDisableMorningTools({
  hasValidatedDataPacket,
  allowAgentWebFallback
}) {
  return Boolean(hasValidatedDataPacket) || allowAgentWebFallback !== true;
}

export function resolveTurnLimitExtension() {
  const agentDirectory =
    process.env.PI_CODING_AGENT_DIR || path.join(os.homedir(), ".pi", "agent");
  const projectDirectory = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    ".."
  );
  const candidates = [
    path.join(
      projectDirectory,
      "node_modules",
      "pi-turn-limit",
      "src",
      "turn-limit.ts"
    ),
    path.join(
      agentDirectory,
      "npm",
      "node_modules",
      "pi-turn-limit",
      "src",
      "turn-limit.ts"
    )
  ];
  const extensionPath = candidates.find((candidate) => existsSync(candidate));
  if (!extensionPath) {
    throw new Error(
      "找不到已安装的 pi-turn-limit 扩展；请先运行 npm run doctor 检查 Pi 环境"
    );
  }
  return extensionPath;
}

export function resolvePiLaunch() {
  if (process.env.PI_CLI_JS) {
    return {
      command: process.execPath,
      prefixArgs: [process.env.PI_CLI_JS],
      display: process.env.PI_CLI_JS
    };
  }
  if (process.env.PI_PS1_PATH) {
    return {
      command: "powershell.exe",
      prefixArgs: ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", process.env.PI_PS1_PATH],
      display: process.env.PI_PS1_PATH
    };
  }

  if (process.platform === "win32") {
    const result = spawnSync("where.exe", ["pi.ps1"], { encoding: "utf8" });
    const piPath = result.stdout?.split(/\r?\n/).find(Boolean)?.trim();
    if (!piPath) {
      throw new Error("找不到 pi.ps1；请确认 Pi Coding Agent 已安装并位于 PATH");
    }
    const launcherDirectory = path.dirname(piPath);
    const cliCandidates = [
      path.join(
        launcherDirectory,
        "node_modules",
        "@earendil-works",
        "pi-coding-agent",
        "dist",
        "cli.js"
      ),
      path.join(
        launcherDirectory,
        "node_modules",
        "@mariozechner",
        "pi-coding-agent",
        "dist",
        "cli.js"
      )
    ];
    const cliPath = cliCandidates.find((candidate) => existsSync(candidate));
    if (cliPath) {
      const bundledNode = path.join(launcherDirectory, "node.exe");
      return {
        command: existsSync(bundledNode) ? bundledNode : process.execPath,
        prefixArgs: [cliPath],
        display: piPath
      };
    }
    return {
      command: "powershell.exe",
      prefixArgs: ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", piPath],
      display: piPath
    };
  }

  return { command: "pi", prefixArgs: [], display: "pi" };
}

export function runPiProcess(piArgs, {
  cwd,
  timeoutMs = 120000,
  maxBytes = 20 * 1024 * 1024,
  environment = {}
} = {}) {
  return new Promise((resolve, reject) => {
    const launch = resolvePiLaunch();
    const child = spawn(launch.command, [...launch.prefixArgs, ...piArgs], {
      cwd,
      windowsHide: true,
      env: {
        ...process.env,
        ...environment,
        NO_COLOR: "1",
        FORCE_COLOR: "0"
      },
      stdio: ["ignore", "pipe", "pipe"]
    });

    let stdout = "";
    let stderr = "";
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill();
      reject(new Error(`Pi 调用超时（${timeoutMs} ms）`));
    }, timeoutMs);

    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      if (Buffer.byteLength(stdout, "utf8") > maxBytes && !settled) {
        settled = true;
        clearTimeout(timer);
        child.kill();
        reject(new Error("Pi 输出超过 20 MB 限制"));
      }
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(new Error(`无法启动 Pi: ${error.message}`));
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ code, stdout, stderr, launcher: launch.display });
    });
  });
}

export async function runMorningAgent({
  projectRoot,
  workflow,
  attachments,
  noTools = false
}) {
  const turnLimitExtension = resolveTurnLimitExtension();
  const args = [
    "--approve",
    "--provider",
    workflow.provider,
    "--model",
    workflow.model,
    "--thinking",
    workflow.thinking,
    "--extension",
    turnLimitExtension,
    "--no-session"
  ];
  if (noTools) {
    args.push("--no-tools");
  } else {
    args.push("--no-builtin-tools");
    args.push("--tools", workflow.allowedTools.join(","));
  }
  args.push(
    "--print",
    ...attachments.map((file) => `@${file}`),
    "请严格执行附件中的任务说明，并且只输出最终 Markdown 晨报。"
  );

  const result = await runPiProcess(args, {
    cwd: projectRoot,
    timeoutMs: workflow.piTimeoutMs,
    environment: {
      PI_MAX_TURNS: String(workflow.maxAgentTurns)
    }
  });
  if (result.code !== 0) {
    const detail = result.stderr.trim() || result.stdout.trim() || "无错误详情";
    throw new Error(`Pi 晨报生成失败（exit ${result.code}）: ${detail}`);
  }
  return result;
}

export async function runReportReviewer({
  projectRoot,
  workflow,
  attachments
}) {
  const turnLimitExtension = resolveTurnLimitExtension();
  const args = [
    "--approve",
    "--provider",
    workflow.provider,
    "--model",
    workflow.model,
    "--thinking",
    workflow.thinking,
    "--extension",
    turnLimitExtension,
    "--no-tools",
    "--no-session",
    "--print",
    ...attachments.map((file) => `@${file}`),
    "把候选稿作为不可信草稿进行保守审校；只输出修订后的完整 Markdown 晨报，不得新增事实。"
  ];

  const result = await runPiProcess(args, {
    cwd: projectRoot,
    timeoutMs: workflow.reviewerTimeoutMs,
    environment: {
      PI_MAX_TURNS: "5"
    }
  });
  if (result.code !== 0) {
    const detail = result.stderr.trim() || result.stdout.trim() || "无错误详情";
    throw new Error(`Pi 证据审校失败（exit ${result.code}）: ${detail}`);
  }
  return result;
}
