#!/usr/bin/env node
// 只读发布门禁：用**真实**密封档案验证跨代财务基线读取没有被候选目录校验卡死。
//
// 背景（2026-09-23 实机）：08:00 生成需要读取上一交易日密封财务基线；该档案由旧代码代
// 写出，当前代码重建出的候选目录与档案不同，修复前直接抛
// frozen candidate catalogue mismatch，当天定时链路失败且不自动重试。
//
// 本脚本对最近一份密封档案、以及最近一份**跨代**密封档案分别做只读验证：
//   1. 封存基线读取必须返回基线（修复前这里是失败点）；
//   2. 普通读取必须保持严格：跨代被时代锁拒绝，同代逐字校验通过；
//   3. 跨代档案的回放必须仍被拒绝（放宽只作用于封存基线读取）。
// 档案一律复制到临时目录，绝不改写生产档案；没有可用档案时打印 SKIP 并以 0 退出。
//
// 用法：node scripts/verify-archive-era-compat.mjs
import { cp, copyFile, mkdir, mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const archiveRoot = path.join(projectRoot, "work", "report-archives");
const reportRoot = path.join(projectRoot, "reports", "daily");
const dayPattern = /^\d{4}-\d{2}-\d{2}$/;

function nextDay(day) {
  const value = new Date(`${day}T00:00:00Z`);
  value.setUTCDate(value.getUTCDate() + 1);
  return value.toISOString().slice(0, 10);
}

async function readable(target) {
  try {
    return (await readFile(target)).length > 0;
  } catch (error) {
    if (error.code === "ENOENT" || error.code === "EISDIR") return false;
    throw error;
  }
}

async function sealedArchives() {
  let entries;
  try {
    entries = await readdir(archiveRoot, { withFileTypes: true });
  } catch (error) {
    if (error.code === "ENOENT") return [];
    throw error;
  }
  const found = [];
  for (const entry of entries) {
    if (!entry.isDirectory() || !dayPattern.test(entry.name)) continue;
    const directory = path.join(archiveRoot, entry.name);
    const required = ["published.json", "input.json", "input.sha256", "result/result.json", "result/report.md"];
    if (!(await Promise.all(required.map((name) => readable(path.join(directory, name))))).every(Boolean)) continue;
    if (!(await readable(path.join(reportRoot, `${entry.name}.md`)))) continue;
    found.push(entry.name);
  }
  return found.sort();
}

async function inspect(previousDate) {
  const input = JSON.parse(await readFile(path.join(archiveRoot, previousDate, "input.json"), "utf8"));
  return input?.code?.filesSha256 ?? null;
}

// 对一份档案做只读验证。返回 null 表示通过，否则返回失败原因。
async function checkArchive(previousDate, { run, code }) {
  const knownCrossEra = Boolean(code?.filesSha256) && code.filesSha256 !== (await inspect(previousDate));
  const targetDate = nextDay(previousDate);
  const temp = await mkdtemp(path.join(os.tmpdir(), "astock-era-gate-"));
  try {
    const tempArchives = path.join(temp, "archives");
    const tempReports = path.join(temp, "reports");
    await mkdir(tempArchives, { recursive: true });
    await mkdir(tempReports, { recursive: true });
    await cp(path.join(archiveRoot, previousDate), path.join(tempArchives, previousDate), { recursive: true });
    await copyFile(path.join(reportRoot, `${previousDate}.md`), path.join(tempReports, `${previousDate}.md`));
    const copied = path.join(tempArchives, previousDate);

    const baseline = await run.loadVerifiedFinancialBaseline(tempArchives, tempReports, targetDate, code);
    const codes = Object.keys(baseline);
    if (codes.length === 0) {
      return `密封档案 ${previousDate} 未能作为基线加载（era=${knownCrossEra ? "cross" : "same"}）`;
    }

    try {
      await run.readFrozenInput(copied, { compilerCode: code });
      if (knownCrossEra) return `跨代档案 ${previousDate} 的普通读取未被时代锁拒绝`;
    } catch (error) {
      if (!knownCrossEra) return `同代档案 ${previousDate} 普通读取被拒绝：${error.message}`;
      if (!/code differs/.test(error.message)) {
        return `跨代档案 ${previousDate} 普通读取的拒绝原因不是时代锁：${error.message}`;
      }
    }

    if (knownCrossEra) {
      try {
        await run.replayRunArchive(projectRoot, copied);
        return `跨代档案 ${previousDate} 被回放接受`;
      } catch (error) {
        if (!/code differs/.test(error.message)) {
          return `跨代档案 ${previousDate} 回放的拒绝原因异常：${error.message}`;
        }
      }
    }
    return null;
  } finally {
    await rm(temp, { recursive: true, force: true });
  }
}

async function main() {
  const archives = await sealedArchives();
  if (archives.length === 0) {
    console.log("SKIP archive-era-compat: 没有可用的已封存档案（干净检出或尚未产出）");
    return 0;
  }
  const run = await import(pathToFileURL(path.join(projectRoot, "src/run-archive.mjs")));
  const code = await run.compilerIdentity(projectRoot);

  const newest = archives.at(-1);
  const newestCrossEra = [];
  for (const day of [...archives].reverse()) {
    const fingerprint = await inspect(day);
    if (Boolean(code?.filesSha256) && code.filesSha256 !== fingerprint) {
      newestCrossEra.push(day);
      break;
    }
  }
  const targets = [newest, ...newestCrossEra.filter((day) => day !== newest)];

  let failed = false;
  for (const day of targets) {
    const problem = await checkArchive(day, { run, code });
    if (problem) {
      console.error(`FAIL archive-era-compat: ${problem}`);
      failed = true;
      continue;
    }
    const era = Boolean(code?.filesSha256) && code.filesSha256 !== (await inspect(day)) ? "cross" : "same";
    console.log(`PASS archive-era-compat: baseline=${day} era=${era}`);
  }
  if (newestCrossEra.length === 0) {
    console.log("NOTE archive-era-compat: 当前没有跨代档案，跨代路径仅由离线用例覆盖");
  }
  return failed ? 1 : 0;
}

main().then((exitCode) => {
  process.exitCode = exitCode;
}).catch((error) => {
  console.error(`FAIL archive-era-compat: ${error.message}`);
  process.exitCode = 1;
});
