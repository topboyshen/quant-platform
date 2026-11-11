// 只读验证工具：把真实历史归档复制到临时目录，验证跨代加载是否被候选目录校验阻断。
// 用法：node scripts/verify-legacy-archive-compat.mjs <projectRoot> <archiveDir> <reportFile> <targetDate>
import { cp, copyFile, mkdir, mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [projectRoot, archiveDir, reportFile, targetDate] = process.argv.slice(2);
if (!projectRoot || !archiveDir || !reportFile || !targetDate) {
  console.error("usage: node verify-legacy-archive-compat.mjs <projectRoot> <archiveDir> <reportFile> <targetDate>");
  process.exit(2);
}
const run = await import(pathToFileURL(path.join(projectRoot, "src/run-archive.mjs")));
const previousDate = path.basename(archiveDir.replace(/\/$/, ""));
const temp = await mkdtemp(path.join(os.tmpdir(), "legacy-archive-verify-"));
try {
  const archiveRoot = path.join(temp, "archives");
  await mkdir(archiveRoot, { recursive: true });
  await cp(archiveDir, path.join(archiveRoot, previousDate), { recursive: true });
  const outputDirectory = path.join(temp, "reports");
  await mkdir(outputDirectory, { recursive: true });
  await copyFile(reportFile, path.join(outputDirectory, `${previousDate}.md`));
  const code = await run.compilerIdentity(projectRoot);
  const baseline = await run.loadVerifiedFinancialBaseline(archiveRoot, outputDirectory, targetDate, code);
  const first = Object.keys(baseline)[0];
  console.log(`PASS era-compat: baseline codes=${JSON.stringify(Object.keys(baseline))} baselineTargetDate=${baseline[first]?.baselineTargetDate ?? "n/a"}`);
} finally {
  await rm(temp, { recursive: true, force: true });
}
