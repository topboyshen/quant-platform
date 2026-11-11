#!/usr/bin/env node
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  buildXhsDraft,
  readXhsStatus,
  safeFailureStatus,
  writeXhsStatus
} from "../src/xhs-draft.mjs";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
let activeOptions = null;

function usage() {
  return [
    "AStock 小红书离线草稿适配器",
    "",
    "用法：",
    "  node scripts/xhs-draft.mjs build [--date YYYY-MM-DD] [--require-current-date] [--max-age-minutes N] [--dry-run]",
    "  node scripts/xhs-draft.mjs status",
    "",
    "该命令只读取固定的 work/last-run.json 与正式晨报，不联网、不通知、不发布。"
  ].join("\n");
}

export function parseXhsDraftArgs(argv) {
  if (!Array.isArray(argv) || argv.length === 0) throw new Error("缺少命令");
  if (argv.includes("--help") || argv.includes("-h")) return { command: "help" };
  const command = argv[0];
  if (!new Set(["build", "status"]).has(command)) throw new Error(`未知命令: ${command}`);
  if (command === "status") {
    if (argv.length !== 1) throw new Error("status 不接受其他参数");
    return { command };
  }
  const options = {
    command,
    desiredDate: null,
    requireCurrentDate: false,
    maxAgeMinutes: null,
    dryRun: false
  };
  const seen = new Set();
  for (let index = 1; index < argv.length; index += 1) {
    const argument = argv[index];
    if (seen.has(argument)) throw new Error(`参数不得重复: ${argument}`);
    seen.add(argument);
    if (argument === "--require-current-date") {
      options.requireCurrentDate = true;
    } else if (argument === "--dry-run") {
      options.dryRun = true;
    } else if (argument === "--date" || argument === "--max-age-minutes") {
      const value = argv[index + 1];
      if (!value || value.startsWith("--")) throw new Error(`${argument} 缺少参数`);
      index += 1;
      if (argument === "--date") {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error("--date 必须是 YYYY-MM-DD");
        options.desiredDate = value;
      } else {
        if (!/^[1-9]\d{0,3}$/.test(value)) throw new Error("--max-age-minutes 必须是正整数");
        options.maxAgeMinutes = Number(value);
      }
    } else {
      throw new Error(`未知参数: ${argument}`);
    }
  }
  return options;
}

async function main() {
  const options = parseXhsDraftArgs(process.argv.slice(2));
  activeOptions = options;
  if (options.command === "help") {
    console.log(usage());
    return;
  }
  if (options.command === "status") {
    console.log(JSON.stringify(await readXhsStatus(projectRoot), null, 2));
    return;
  }
  const result = await buildXhsDraft({ projectRoot, ...options });
  console.log(JSON.stringify(result, null, 2));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(async (error) => {
    if (activeOptions?.command === "build" && activeOptions.dryRun !== true) {
      try {
        await writeXhsStatus(projectRoot, safeFailureStatus(error));
      } catch {
        // Preserve the original error if even the local status path is unsafe.
      }
    }
    console.error(`XHS 草稿构建失败：${error.message}`);
    process.exitCode = 1;
  });
}
