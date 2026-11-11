#!/usr/bin/env node
/**
 * Research seal CLI: seals today's validated formal research independently of
 * the XHS draft, so a display-transformation failure cannot block the relay.
 * Read-only apart from the seal file itself; never sends, never edits reports.
 */
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { buildResearchSeal } from "../src/research-seal.mjs";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function usage() {
  return [
    "AStock 研究密封（与小红书草稿解耦）",
    "",
    "用法：",
    "  node scripts/research-seal.mjs [--date YYYY-MM-DD] [--require-current-date] [--max-age-minutes N] [--dry-run]",
    "",
    "只读取 work/last-run.json 与正式晨报，做日期/门禁/哈希/编号来源校验，",
    "写入 work/research-seal/<date>/<reportSha256>.json；不联网、不通知、不发布。"
  ].join("\n");
}

export function parseResearchSealArgs(argv) {
  if (!Array.isArray(argv)) throw new Error("参数必须是数组");
  if (argv.includes("--help") || argv.includes("-h")) return { command: "help" };
  const options = { desiredDate: null, requireCurrentDate: false, maxAgeMinutes: null, dryRun: false };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--dry-run") {
      options.dryRun = true;
      continue;
    }
    if (token === "--require-current-date") {
      options.requireCurrentDate = true;
      continue;
    }
    if (token === "--date" || token === "--max-age-minutes") {
      const value = argv[index + 1];
      if (value === undefined || value.startsWith("--")) throw new Error(`${token} 缺少取值`);
      index += 1;
      if (token === "--date") options.desiredDate = value;
      else options.maxAgeMinutes = Number(value);
      continue;
    }
    throw new Error(`未知参数: ${token}`);
  }
  if (options.maxAgeMinutes !== null && !Number.isInteger(options.maxAgeMinutes)) {
    throw new Error("--max-age-minutes 必须是整数");
  }
  return { command: "seal", ...options };
}

export async function main(argv = process.argv.slice(2)) {
  let options;
  try {
    options = parseResearchSealArgs(argv);
  } catch (error) {
    console.error(String(error?.message ?? error));
    return 2;
  }
  if (options.command === "help") {
    console.log(usage());
    return 0;
  }
  try {
    const result = await buildResearchSeal({
      projectRoot,
      desiredDate: options.desiredDate,
      requireCurrentDate: options.requireCurrentDate,
      maxAgeMinutes: options.maxAgeMinutes,
      dryRun: options.dryRun
    });
    console.log(JSON.stringify(result, null, 2));
    return result.status === "sealed" || result.status === "noop" || result.status === "ready" ? 0 : 1;
  } catch {
    // Integrity failures must not echo report content; only the fixed code is safe.
    console.log(JSON.stringify({
      schemaVersion: "1.0",
      status: "failed",
      reasonCodes: ["RESEARCH_SEAL_INVALID"],
      targetDate: options.desiredDate,
      sealFile: null,
      completedAt: new Date().toISOString()
    }, null, 2));
    return 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exitCode = await main();
}
