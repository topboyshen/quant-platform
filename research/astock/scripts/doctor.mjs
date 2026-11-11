#!/usr/bin/env node
import { access, readFile } from "node:fs/promises";
import { constants as fsConstants } from "node:fs";
import { execFile } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import { loadProjectConfig, resolveProjectPath } from "../src/config.mjs";
import {
  buildMinimalHermesEnvironment,
  HERMES_COMPUTE_PYTHON,
  HERMES_COMPUTE_SCRIPT_RELATIVE,
  runHermesCompute
} from "../src/agent-executor.mjs";
import {
  resolvePiLaunch,
  resolveTurnLimitExtension,
  runPiProcess
} from "../src/pi.mjs";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const live = process.argv.includes("--live");
const results = [];
const execFileAsync = promisify(execFile);
const requiredDataPackageVersions = Object.freeze({
  akshare: "1.18.81",
  mcp: "2.0.0"
});

function record(status, item, detail) {
  results.push({ status, item, detail });
}

async function exists(filePath) {
  try {
    await access(filePath, fsConstants.R_OK);
    return true;
  } catch {
    return false;
  }
}

function pinnedVersion(requirements, packageName) {
  const escaped = packageName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = requirements.match(new RegExp(`^${escaped}==([^\\s#]+)`, "mi"));
  return match?.[1] ?? null;
}

async function checkDataBridge(config) {
  const isWindows = process.platform === "win32";
  const expectedExecutable = isWindows ? ".venv/Scripts/python.exe" : ".venv/bin/python";
  const expectedRunner = isWindows ? "scripts/run-akshare-bridge.ps1" : "scripts/run-akshare-bridge.sh";
  const expectedArgs = ["mcp/akshare_bridge.py"];
  const descriptor = config.workflow.dataBridge;
  const descriptorMatches =
    descriptor?.executable?.replaceAll("\\", "/") === expectedExecutable &&
    Array.isArray(descriptor?.args) &&
    descriptor.args.length === expectedArgs.length &&
    descriptor.args.every(
      (value, index) => value.replaceAll("\\", "/") === expectedArgs[index]
    );
  record(
    descriptorMatches ? "PASS" : "FAIL",
    "固定数据桥配置",
    descriptorMatches
      ? `${expectedExecutable} ${expectedArgs.join(" ")}`
      : "config/workflow.json 必须固定指向项目 .venv 与 mcp/akshare_bridge.py"
  );

  const venvPython = resolveProjectPath(projectRoot, expectedExecutable);
  const bridgePath = resolveProjectPath(projectRoot, expectedArgs[0]);
  const mcpServerPath = resolveProjectPath(projectRoot, "mcp/akshare_server.py");
  const storagePath = resolveProjectPath(projectRoot, "mcp/storage.py");
  const bridgeRunnerPath = resolveProjectPath(projectRoot, expectedRunner);
  const [pythonReady, bridgeReady, serverReady, storageReady, runnerReady] = await Promise.all([
    exists(venvPython),
    exists(bridgePath),
    exists(mcpServerPath),
    exists(storagePath),
    exists(bridgeRunnerPath)
  ]);
  record(
    bridgeReady && runnerReady ? "PASS" : "FAIL",
    "AkShare stdin 桥",
    bridgeReady && runnerReady
      ? `固定入口与 ${isWindows ? "PowerShell" : "bash"} 包装器可读取`
      : "数据桥文件不完整"
  );
  record(
    serverReady ? "PASS" : "FAIL",
    "未来 MCP server",
    serverReady ? "同源 MCP stdio 入口可读取" : "缺少 mcp/akshare_server.py"
  );
  record(
    storageReady ? "PASS" : "WARN",
    "SQLite 持久化层",
    storageReady ? "mcp/storage.py 可读取" : "未找到 mcp/storage.py（历史记录将不可用）"
  );

  const requirementsPath = resolveProjectPath(projectRoot, "requirements.txt");
  if (!(await exists(requirementsPath))) {
    record("FAIL", "Python 数据依赖", "缺少 requirements.txt");
    return;
  }
  const requirements = await readFile(requirementsPath, "utf8");
  const expectedAkshare = pinnedVersion(requirements, "akshare");
  const expectedMcp = pinnedVersion(requirements, "mcp");
  if (
    expectedAkshare !== requiredDataPackageVersions.akshare ||
    expectedMcp !== requiredDataPackageVersions.mcp
  ) {
    record(
      "FAIL",
      "Python 数据依赖",
      `requirements.txt 必须固定为 akshare ${requiredDataPackageVersions.akshare} / mcp ${requiredDataPackageVersions.mcp}`
    );
    return;
  }
  if (!pythonReady) {
    record(
      "FAIL",
      "Python 数据依赖",
      "项目 .venv 不存在；先运行 npm run data:setup"
    );
    return;
  }

  try {
    const versionCode = [
      "import importlib.metadata as m",
      "print('akshare=' + m.version('akshare'))",
      "print('mcp=' + m.version('mcp'))"
    ].join("; ");
    const { stdout } = await execFileAsync(venvPython, ["-c", versionCode], {
      cwd: projectRoot,
      windowsHide: true,
      timeout: 30000,
      maxBuffer: 64 * 1024,
      encoding: "utf8"
    });
    const installed = Object.fromEntries(
      stdout
        .trim()
        .split(/\r?\n/)
        .map((line) => line.split("=", 2))
        .filter(([name, version]) => name && version)
    );
    const matches =
      installed.akshare === expectedAkshare && installed.mcp === expectedMcp;
    record(
      matches ? "PASS" : "FAIL",
      "Python 数据依赖",
      matches
        ? `akshare ${installed.akshare}；mcp ${installed.mcp}`
        : `需要 akshare ${expectedAkshare} / mcp ${expectedMcp}，当前为 ${installed.akshare ?? "未安装"} / ${installed.mcp ?? "未安装"}`
    );
  } catch (error) {
    record(
      "FAIL",
      "Python 数据依赖",
      `无法从项目 .venv 读取包版本：${error.message}`
    );
  }
}

async function checkHermesExecutor(config) {
  const adapterPath = resolveProjectPath(projectRoot, HERMES_COMPUTE_SCRIPT_RELATIVE);
  const [pythonReady, adapterReady] = await Promise.all([
    exists(HERMES_COMPUTE_PYTHON),
    exists(adapterPath)
  ]);
  if (!pythonReady || !adapterReady) {
    record(
      "FAIL",
      "Hermes 受限计算适配器",
      !pythonReady ? "固定 Hermes Python 不可读" : "scripts/hermes-compute.py 不可读"
    );
  } else {
    try {
      const { stdout } = await execFileAsync(
        HERMES_COMPUTE_PYTHON,
        ["-I", adapterPath, "--check"],
        {
          cwd: projectRoot,
          env: buildMinimalHermesEnvironment(),
          windowsHide: true,
          timeout: 30000,
          maxBuffer: 64 * 1024,
          encoding: "utf8"
        }
      );
      const status = JSON.parse(stdout);
      const ok =
        status.ok === true &&
        status.credentialReady === true &&
        status.safeMode === true &&
        status.provider === config.workflow.hermesProvider &&
        status.model === config.workflow.hermesModel &&
        Array.isArray(status.enabledToolsets) &&
        status.enabledToolsets.length === 1 &&
        status.enabledToolsets[0] === "todo";
      record(
        ok ? "PASS" : "FAIL",
        "Hermes 受限计算适配器",
        ok
          ? `${status.version}；${status.provider}/${status.model}；仅 todo 工具`
          : "适配器自检结果与固定安全契约不一致"
      );
    } catch (error) {
      record("FAIL", "Hermes 受限计算适配器", error.message);
    }
  }

  record(
    "INFO",
    "Pi 回滚路径",
    "未执行；Pi 已退出每日主链路，仅在显式切换 agentExecutor=pi 后检查"
  );

  if (live) {
    try {
      const smoke = await runHermesCompute({
        projectRoot,
        workflow: config.workflow,
        phase: "smoke",
        prompt: "仅回复：ASTOCK_HERMES_OK 中文正常",
        timeoutMs: 120000
      });
      const ok =
        smoke.code === 0 &&
        smoke.stdout.includes("ASTOCK_HERMES_OK") &&
        smoke.stdout.includes("中文正常");
      record(
        ok ? "PASS" : "FAIL",
        "云端最小推理",
        ok
          ? `${config.workflow.hermesModel} 返回正常`
          : "Hermes 返回内容不符合预期"
      );
    } catch (error) {
      record("FAIL", "云端最小推理", error.message);
    }
  } else {
    record(
      "INFO",
      "云端最小推理",
      "未执行；只有显式运行 npm run doctor:live 才会产生少量云用量"
    );
  }
}

async function checkPiExecutor(config) {
  try {
    const launch = resolvePiLaunch();
    record("PASS", "Pi 命令", launch.display);
  } catch (error) {
    record("FAIL", "Pi 命令", error.message);
    return;
  }

  const version = await runPiProcess(["--version"], {
    cwd: projectRoot,
    timeoutMs: 30000
  });
  record(
    version.code === 0 ? "PASS" : "FAIL",
    "Pi 版本",
    version.code === 0 ? version.stdout.trim() : (version.stderr.trim() || `exit ${version.code}`)
  );

  try {
    const turnLimitPath = resolveTurnLimitExtension();
    record("PASS", "工作流轮次保险丝", turnLimitPath);
  } catch (error) {
    record("FAIL", "工作流轮次保险丝", error.message);
  }

  const agentDir =
    process.env.PI_CODING_AGENT_DIR || path.join(os.homedir(), ".pi", "agent");
  const authPath = path.join(agentDir, "auth.json");
  let authReady = false;
  if (await exists(authPath)) {
    try {
      const auth = JSON.parse(await readFile(authPath, "utf8"));
      const entry = auth[config.workflow.provider];
      authReady =
        entry?.type === "api_key" &&
        typeof entry.key === "string" &&
        entry.key.length > 0;
    } catch {
      authReady = false;
    }
  }
  record(
    authReady ? "PASS" : "FAIL",
    "Ollama Cloud 认证",
    authReady ? "Pi 认证条目存在（值未显示）" : "未找到有效认证条目"
  );

  if (!live) {
    record("INFO", "模型目录", "默认 doctor 不联网；doctor:live 才检查 provider 目录");
    record("INFO", "云端最小推理", "未执行；doctor:live 才会产生少量云用量");
    return;
  }

  let catalogOk = false;
  let catalogDetail = "未找到指定模型";
  try {
    const auth = JSON.parse(await readFile(authPath, "utf8"));
    const key = auth?.[config.workflow.provider]?.key;
    const response = await fetch("https://ollama.com/v1/models", {
      headers: { Authorization: `Bearer ${key}` },
      signal: AbortSignal.timeout(20000)
    });
    if (response.ok) {
      const data = await response.json();
      const ids = Array.isArray(data?.data)
        ? data.data.map((model) => model?.id).filter(Boolean)
        : [];
      catalogOk = ids.includes(config.workflow.model);
      catalogDetail = catalogOk
        ? `${config.workflow.provider}/${config.workflow.model} 可解析`
        : `provider 目录未包含 ${config.workflow.model}`;
    } else {
      catalogDetail = `provider 目录查询失败：HTTP ${response.status}`;
    }
  } catch (error) {
    catalogDetail = `provider 目录查询异常：${error.message}`;
  }
  record(catalogOk ? "PASS" : "FAIL", "模型目录", catalogDetail);

  const turnLimitExtension = resolveTurnLimitExtension();
  const smoke = await runPiProcess([
    "--no-approve",
    "--provider",
    config.workflow.provider,
    "--model",
    config.workflow.model,
    "--thinking",
    "low",
    "--extension",
    turnLimitExtension,
    "--no-builtin-tools",
    "--tools",
    config.workflow.allowedTools.join(","),
    "--no-session",
    "--print",
    "仅回复：ASTOCK_PI_OK 中文正常"
  ], {
    cwd: projectRoot,
    timeoutMs: 120000,
    environment: { PI_MAX_TURNS: "3" }
  });
  const ok =
    smoke.code === 0 &&
    smoke.stdout.includes("ASTOCK_PI_OK") &&
    smoke.stdout.includes("中文正常");
  record(
    ok ? "PASS" : "FAIL",
    "云端最小推理",
    ok ? `${config.workflow.model} 返回正常` : (smoke.stderr.trim() || "返回内容不符合预期")
  );
}

async function main() {
  const major = Number(process.versions.node.split(".")[0]);
  record(major >= 22 ? "PASS" : "FAIL", "Node.js", process.version);

  let config;
  try {
    config = await loadProjectConfig(projectRoot);
    record("PASS", "项目配置", "JSON 配置完整且相互一致");
  } catch (error) {
    record("FAIL", "项目配置", error.message);
  }

  if (config) {
    await checkDataBridge(config);
    if (config.workflow.agentExecutor === "hermes") {
      await checkHermesExecutor(config);
    } else {
      await checkPiExecutor(config);
    }
  }

  for (const result of results) {
    console.log(`[${result.status}] ${result.item}: ${result.detail}`);
  }
  const failures = results.filter((result) => result.status === "FAIL");
  if (failures.length > 0) process.exitCode = 1;
}

main().catch((error) => {
  console.error(`环境诊断失败：${error.message}`);
  process.exitCode = 1;
});
