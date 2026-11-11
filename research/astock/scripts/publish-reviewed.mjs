#!/usr/bin/env node
import { readFile, readdir, rename, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  loadProjectConfig,
  resolveProjectPath,
  validateIsoDate
} from "../src/config.mjs";
import {
  assessPublicationGate,
  isAcceptedPublicationLevel,
  normalizeReportOutput,
  requiresStaleMarketLabel
} from "../src/report.mjs";

import { compilerIdentity, readFrozenInput, freezeRunInput, captureReviewedReport, publishArchivedReport } from "../src/run-archive.mjs";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
let atomicWriteSequence = 0;

async function writeAtomicFile(filePath, content, mode = 0o600) {
  const temporaryPath = `${filePath}.${process.pid}.${++atomicWriteSequence}.tmp`;
  try {
    await writeFile(temporaryPath, content, {
      encoding: "utf8",
      flag: "wx",
      mode
    });
    await rename(temporaryPath, filePath);
  } finally {
    await unlink(temporaryPath).catch(() => {});
  }
}

function parseArgs(argv) {
  if (argv.length === 0) return { date: null };
  if (argv.length === 2 && argv[0] === "--date") {
    return { date: validateIsoDate(argv[1]) };
  }
  throw new Error("用法：node scripts/publish-reviewed.mjs [--date YYYY-MM-DD]");
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const { stocks, risk, workflow } = await loadProjectConfig(projectRoot);
  const workDirectory = resolveProjectPath(projectRoot, workflow.workDirectory);
  const [lastRunText, contextText, reviewedOutput] = await Promise.all([
    readFile(path.join(workDirectory, "last-run.json"), "utf8"),
    readFile(path.join(workDirectory, "morning-context.json"), "utf8"),
    readFile(path.join(workDirectory, "last-pi-output.md"), "utf8")
  ]);
  const lastRun = JSON.parse(lastRunText);
  const rawContext = JSON.parse(contextText);

  if (!["failed", "blocked", "success"].includes(lastRun.status)) {
    throw new Error("仅允许发布最近一次已审校完成或质量门禁失败的运行");
  }
  const targetDate = validateIsoDate(lastRun.targetDate);
  if (options.date && options.date !== targetDate) {
    throw new Error(`恢复日期 ${options.date} 与最近运行 ${targetDate} 不一致`);
  }
  if (rawContext?.meta?.targetDate !== targetDate) {
    throw new Error("数据包目标日期与最近运行不一致");
  }
  // Recovery cannot create a baseline retroactively from rolling work files.
  const archived = await readFrozenInput(path.join(workDirectory, "report-archives", targetDate),
    { compilerCode: await compilerIdentity(projectRoot) });
  if (lastRun.archiveInputSha256 !== archived.inputSha256 || lastRun.frozenCutoffInstant !== archived.input.cutoffInstant || lastRun.cutoffLocal !== archived.input.cutoffLocal) {
    throw new Error("恢复发布缺少与最近运行匹配的完整冻结凭据");
  }
  const frozen = await freezeRunInput({
    projectRoot, archiveRoot: path.join(workDirectory, "report-archives"),
    outputDirectory: resolveProjectPath(projectRoot, workflow.outputDirectory),
    rawContext, rawContextText: contextText, configuration: { stocks, risk, workflow }, targetDate,
    cutoffInstant: archived.input.cutoffInstant, cutoffLocal: archived.input.cutoffLocal
  });
  const context = frozen.input.compilerContext;
  const runStartedAt = new Date(frozen.input.cutoffInstant);
  const hasValidatedDataPacket = ["live", "partial"].includes(context?.meta?.status);
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: runStartedAt,
    targetDate
  });
  if (!publicationAudit.passed) {
    throw new Error(
      "核心行情发布门禁仍未通过，不能恢复发布：" + publicationAudit.blockers.join("；")
    );
  }
  const expectedPublicationLevel = lastRun.publicationLevel;
  if (!isAcceptedPublicationLevel(expectedPublicationLevel)) {
    throw new Error("最近运行的发布级别与数据包状态不一致");
  }
  const requireStaleMarketLabel = requiresStaleMarketLabel({
    context,
    cutoffInstant: runStartedAt,
    marketDataMaxAgeHours: risk.marketDataMaxAgeHours
  });
  const report = normalizeReportOutput(reviewedOutput, {
    targetDate,
    disclaimer: risk.disclaimer,
    cutoffLocal: lastRun.cutoffLocal,
    cutoffInstant: runStartedAt,
    publicationLevel: expectedPublicationLevel,
    requireStaleMarketLabel,
    context,
    strictContextSources: hasValidatedDataPacket,
    marketDataMaxAgeHours: risk.marketDataMaxAgeHours,
    newsMaxAgeHours: risk.newsMaxAgeHours,
    publicationAudit,
    enforcePublicationReady: true,
    requireDeterministicCompilerOutput: true
  });

  const reportDirectory = resolveProjectPath(
    projectRoot,
    hasValidatedDataPacket
      ? workflow.outputDirectory
      : workflow.provisionalOutputDirectory
  );
  const reportPath = path.join(reportDirectory, `${targetDate}.md`);
  const sealedResult = await captureReviewedReport(frozen, report);
  await publishArchivedReport(frozen, reportPath);

  const reviewFiles = (await readdir(workDirectory)).filter(
    (name) => /^last-pi-review-\d+\.md$/.test(name)
  );
  const {
    status: _status,
    failedAt: _failedAt,
    error: _error,
    rawError: _rawError,
    failureCode: _failureCode,
    retryable: _retryable,
    ...baseRecord
  } = lastRun;
  const successRecord = {
    ...baseRecord,
    compilationInputSha256: sealedResult.compilationInputSha256,
    status: "success",
    phase: "publish",
    publicationGate: "passed",
    completedAt: new Date().toISOString(),
    evidenceReview: "passed",
    evidenceReviewScope: "structure_source_freshness_only",
    researchSemanticReview: "not_human_reviewed",
    reviewAttempts: reviewFiles.length,
    resumedAfterGateFix: true,
    republishedReviewedOutput: false,
    reportFile: path.relative(projectRoot, reportPath)
  };
  await writeAtomicFile(
    path.join(workDirectory, "last-run.json"),
    JSON.stringify(successRecord, null, 2) + "\n"
  );
  console.log(`已发布通过质量门禁的审校稿：${reportPath}`);
}

main().catch((error) => {
  console.error(`恢复发布失败：${error.message}`);
  process.exitCode = 1;
});
