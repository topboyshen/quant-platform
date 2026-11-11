#!/usr/bin/env node
import { createHash } from "node:crypto";
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  dateInTimeZone,
  dateTimeInTimeZone,
  loadProjectConfig,
  resolveProjectPath,
  validateIsoDate
} from "../src/config.mjs";
import {
  buildDataRequest,
  collectMorningContext,
  unavailableContext
} from "../src/data.mjs";
import {
  STRUCTURED_ANALYSIS_ATTACHMENTS,
  resolveAgentDescriptor,
  runReportReviewer,
  runStructuredAnalyst
} from "../src/agent-executor.mjs";
import {
  assessPublicationGate,
  buildRunInstruction,
  DATA_BRIEF_PUBLICATION_LEVEL,
  normalizeReportOutput,
  requiresStaleMarketLabel
} from "../src/report.mjs";
import { buildReviewFeedback } from "../src/review-feedback.mjs";
import { runStructuredMorning } from "../src/structured-morning.mjs";
import {
  freezeRunInput, sealRunResult, loadArchivedCompilation,
  publishArchivedReport, captureReviewedReport
} from "../src/run-archive.mjs";
import { loadFrozenNotificationPresentation } from "../src/frozen-notification-presentation.mjs";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const RUN_PHASES = new Set(["collect", "generate", "review", "analysis", "validate", "publish"]);
const FAILURE_POLICY = Object.freeze({
  data_bridge_timeout: true,
  data_bridge_failed: true,
  trading_calendar_unknown: true,
  hermes_generation_failed: true,
  hermes_review_failed: true,
  pi_generation_failed: true,
  pi_review_failed: true,
  content_gate_exhausted: false,
  // A failed write may have left a partial report. Never advertise an
  // automatic retry until the artifact is independently checked or removed.
  report_publish_failed: false,
  workflow_internal_error: false
});
const MAX_RAW_ERROR_CHARS = 1000;
const MAX_PERSISTED_STDERR_BYTES = 64 * 1024;
let stateWriteSequence = 0;
const runtimeState = {
  recordPath: null,
  baseRecord: null,
  phase: null,
  failureCode: null
};

function assertRunPhase(phase) {
  if (!RUN_PHASES.has(phase)) {
    throw new Error(`Unsupported morning-report phase: ${phase}`);
  }
  return phase;
}

async function writeAtomicFile(filePath, content, mode = 0o600) {
  const temporaryPath = `${filePath}.${process.pid}.${++stateWriteSequence}.tmp`;
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

async function writeAtomicJson(filePath, value) {
  await writeAtomicFile(filePath, JSON.stringify(value, null, 2) + "\n");
}

async function writeRunningState(phase, patch = {}) {
  if (!runtimeState.recordPath || !runtimeState.baseRecord) return;
  runtimeState.phase = assertRunPhase(phase);
  runtimeState.baseRecord = { ...runtimeState.baseRecord, ...patch };
  await writeAtomicJson(
    runtimeState.recordPath,
    {
      ...runtimeState.baseRecord,
      status: "running",
      phase: runtimeState.phase,
      updatedAt: new Date().toISOString()
    }
  );
}

function tagFailure(error, failureCode) {
  if (!Object.hasOwn(FAILURE_POLICY, failureCode)) {
    throw new Error(`Unsupported morning-report failure code: ${failureCode}`);
  }
  const tagged = error instanceof Error ? error : new Error(String(error));
  tagged.failureCode = failureCode;
  tagged.retryable = FAILURE_POLICY[failureCode];
  return tagged;
}

function looksLikeTimeout(error) {
  return error?.code === "ETIMEDOUT" || /timeout|timed out|\u8d85\u65f6/i.test(String(error?.message ?? ""));
}

function failureMetadata(error) {
  if (Object.hasOwn(FAILURE_POLICY, error?.failureCode)) {
    return {
      failureCode: error.failureCode,
      retryable: FAILURE_POLICY[error.failureCode]
    };
  }
  if (Object.hasOwn(FAILURE_POLICY, runtimeState.failureCode)) {
    return {
      failureCode: runtimeState.failureCode,
      retryable: FAILURE_POLICY[runtimeState.failureCode]
    };
  }
  const fallbackByPhase = {
    collect: "workflow_internal_error",
    generate: runtimeState.baseRecord?.agentExecutor === "hermes"
      ? "hermes_generation_failed"
      : "pi_generation_failed",
    review: runtimeState.baseRecord?.agentExecutor === "hermes"
      ? "hermes_review_failed"
      : "pi_review_failed",
    analysis: "workflow_internal_error",
    validate: "content_gate_exhausted",
    publish: "report_publish_failed"
  };
  const failureCode = fallbackByPhase[runtimeState.phase] ?? "workflow_internal_error";
  return { failureCode, retryable: FAILURE_POLICY[failureCode] };
}

function sanitizeDiagnosticText(value, maxChars) {
  let text = String(value ?? "")
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  text = text
    .replace(/(https?:\/\/)[^/?#@\s]+@/gi, "$1[REDACTED]@")
    .replace(
      /(["']?)(api[_-]?key|client[_-]?secret|access[_-]?token|token|secret|password|passwd|authorization|cookie)\1(\s*[:=]\s*)(["'])(?:\\.|(?!\4).)*\4/gi,
      (_match, keyQuote, name, separator, valueQuote) => (
        `${keyQuote}${name}${keyQuote}${separator}${valueQuote}[REDACTED]${valueQuote}`
      )
    )
    .replace(
      /\b(bearer|basic)\s+[^\s,;"']+/gi,
      (_match, scheme) => `${scheme} [REDACTED]`
    )
    .replace(
      /(["']?)(api[_-]?key|client[_-]?secret|access[_-]?token|token|secret|password|passwd|authorization|cookie)\1(\s*[:=]\s*)(?!["'])([^\s,;&}\]]+)/gi,
      (_match, keyQuote, name, separator) => (
        `${keyQuote}${name}${keyQuote}${separator}[REDACTED]`
      )
    )
    .replace(/\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})\b/g, "[REDACTED]")
    .replace(
      /-----BEGIN [^-\r\n]{0,48}PRIVATE KEY-----[\s\S]*?(?:-----END [^-\r\n]{0,48}PRIVATE KEY-----|$)/gi,
      "[REDACTED PRIVATE KEY]"
    );
  return text.slice(0, maxChars);
}

function boundedLegacyError(error) {
  return sanitizeDiagnosticText(error?.message ?? error, 500);
}

function boundedRawError(error) {
  const raw = error instanceof Error
    ? `${error.name}: ${error.message}`
    : String(error);
  return sanitizeDiagnosticText(raw, MAX_RAW_ERROR_CHARS);
}

async function writeSanitizedStderr(filePath, value) {
  const normalized = sanitizeDiagnosticText(value, MAX_PERSISTED_STDERR_BYTES);
  const encoded = Buffer.from(normalized, "utf8");
  let end = Math.min(encoded.length, MAX_PERSISTED_STDERR_BYTES - 1);
  if (end < encoded.length) {
    while (end > 0 && (encoded[end] & 0xc0) === 0x80) end -= 1;
  }
  const sanitized = encoded.subarray(0, end).toString("utf8");
  // Always replace the occurrence's known stderr file, even when this run
  // produced only whitespace.  This prevents a prior unsanitized/stale log
  // from being mistaken for the current attempt.
  await writeAtomicFile(
    filePath,
    sanitized ? `${sanitized}\n` : "",
    0o600
  );
}

function usage() {
  return [
    "每日 A 股晨报工作流",
    "",
    "用法：",
    "  node scripts/morning-report.mjs [选项]",
    "",
    "选项：",
    "  --date YYYY-MM-DD      指定报告日期（默认 Asia/Shanghai 的今天）",
    "  --input FILE           使用指定的晨报数据 JSON",
    "  --dry-run              只准备并验证上下文，不调用模型",
    "  --prepare-only         同 dry-run，供交互式 Pi Prompt 使用",
    "  --strict-data          数据桥接失败时直接退出，不降级",
    "  --trading-days-only    非交易日跳过模型与报告，适合计划任务",
    "  --with-structured-analysis  显式运行结构化模型分析（须同时指定 --date 与 --input）；默认仅编译基础快报",
    "  --review-existing      跳过研究阶段，重新审校最近输出（需同时使用 --input）",
    "  --help                 显示帮助"
  ].join("\n");
}

export function parseArgs(argv) {
  const options = {
    dryRun: false,
    prepareOnly: false,
    strictData: false,
    tradingDaysOnly: false,
    withStructuredAnalysis: false,
    reviewExisting: false,
    date: null,
    input: null,
    frozenCutoff: null,
    frozenContextSha256: null
  };

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--dry-run") options.dryRun = true;
    else if (arg === "--prepare-only") options.prepareOnly = true;
    else if (arg === "--strict-data") options.strictData = true;
    else if (arg === "--trading-days-only") options.tradingDaysOnly = true;
    else if (arg === "--with-structured-analysis") options.withStructuredAnalysis = true;
    else if (arg === "--review-existing") options.reviewExisting = true;
    else if (arg === "--help") options.help = true;
    else if (arg === "--mcp-command") {
      throw new Error(
        "--mcp-command 已因安全原因禁用；数据桥接只能使用 config/workflow.json 中的固定描述符"
      );
    } else if ([
      "--date",
      "--input",
      "--frozen-cutoff",
      "--frozen-context-sha256"
    ].includes(arg)) {
      const value = argv[index + 1];
      if (!value || value.startsWith("--")) {
        throw new Error(`${arg} 缺少参数`);
      }
      index += 1;
      if (arg === "--date") options.date = value;
      if (arg === "--input") options.input = value;
      if (arg === "--frozen-cutoff") options.frozenCutoff = value;
      if (arg === "--frozen-context-sha256") options.frozenContextSha256 = value;
    } else {
      throw new Error(`未知参数: ${arg}`);
    }
  }
  return options;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    console.log(usage());
    return;
  }
  if (options.reviewExisting && !options.input) {
    throw new Error("--review-existing 必须与 --input 一起使用，以固定原数据包");
  }
  if (options.withStructuredAnalysis && (!options.date || !options.input || options.reviewExisting)) {
    throw new Error("--with-structured-analysis 必须同时指定 --date 与 --input，且不可与 --review-existing 混用");
  }

  if (options.frozenContextSha256) {
    const normalizedInput = path.normalize(options.input ?? "").replaceAll("\\", "/");
    if (
      !options.frozenCutoff
      || normalizedInput !== "work/morning-context.json"
      || !/^[0-9a-f]{64}$/.test(options.frozenContextSha256)
    ) {
      throw new Error("--frozen-context-sha256 requires the fixed work/morning-context.json input");
    }
  }

  const requiresAgent = options.withStructuredAnalysis || options.reviewExisting;
  const config = await loadProjectConfig(projectRoot, { requireAgent: requiresAgent });
  const { stocks, risk, workflow } = config;
  const agentDescriptor = requiresAgent ? resolveAgentDescriptor(workflow) : null;
  const runStartedAt = new Date();
  const cutoffInstant = options.frozenCutoff
    ? new Date(options.frozenCutoff)
    : runStartedAt;
  if (
    Number.isNaN(cutoffInstant.getTime())
    || (options.frozenCutoff && !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(options.frozenCutoff))
    || cutoffInstant.getTime() > runStartedAt.getTime() + 5_000
  ) {
    throw new Error("--frozen-cutoff must be a UTC ISO instant no later than this attempt");
  }
  const cutoffLocal = dateTimeInTimeZone(workflow.timezone, cutoffInstant);
  const targetDate = options.date
    ? validateIsoDate(options.date)
    : dateInTimeZone(workflow.timezone, cutoffInstant);
  if (cutoffLocal.date !== targetDate) {
    throw new Error("--frozen-cutoff and --date must identify the same business date");
  }
  const workDirectory = resolveProjectPath(projectRoot, workflow.workDirectory);
  const outputDirectory = resolveProjectPath(projectRoot, workflow.outputDirectory);
  const provisionalOutputDirectory = resolveProjectPath(
    projectRoot,
    workflow.provisionalOutputDirectory
  );
  await mkdir(workDirectory, { recursive: true });

  if (!options.dryRun && !options.prepareOnly) {
    runtimeState.recordPath = path.join(workDirectory, "last-run.json");
    runtimeState.baseRecord = {
      schemaVersion: "1.0",
      targetDate,
      startedAt: runStartedAt.toISOString(),
      frozenCutoffInstant: cutoffInstant.toISOString(),
      cutoffLocal: cutoffLocal.display,
      agentExecutor: agentDescriptor?.executor ?? "none",
      provider: agentDescriptor?.provider ?? null,
      model: agentDescriptor?.model ?? null,
      thinking: agentDescriptor?.reasoning ?? null,
      dataStatus: null,
      loadedFrom: null,
      publicationLevel: null,
      publicationGate: "pending",
      publicationBlockers: [],
      marketCoverage: null
    };
    // Persist this occurrence before the bridge starts so an early collection
    // failure can never leave yesterday's successful state behind.
    await writeRunningState("collect");
  }

  await mkdir(outputDirectory, { recursive: true });
  await mkdir(provisionalOutputDirectory, { recursive: true });

  const request = buildDataRequest({
    targetDate,
    stocks,
    workflow,
    now: cutoffInstant
  });
  let collected;
  try {
    collected = await collectMorningContext({
      projectRoot,
      request,
      workflow,
      explicitInput: options.input
    });
  } catch (error) {
    if (options.strictData || options.input) {
      throw tagFailure(
        error,
        looksLikeTimeout(error) ? "data_bridge_timeout" : "data_bridge_failed"
      );
    }
    collected = {
      context: unavailableContext({
        request,
        reason: "A 股数据桥接命令本次运行失败，工作流已安全降级。"
      }),
      loadedFrom: "bridge_error_fallback"
    };
    console.warn(`警告：${error.message}`);
  }

  await writeRunningState("collect", {
    dataStatus: collected.context.meta.status,
    loadedFrom: collected.loadedFrom
  });

  const requireStaleMarketLabel = requiresStaleMarketLabel({
    context: collected.context,
    cutoffInstant,
    marketDataMaxAgeHours: risk.marketDataMaxAgeHours
  });

  const hasValidatedDataPacket = ["live", "partial"].includes(
    collected.context.meta.status
  );
  const publicationAudit = assessPublicationGate({
    context: collected.context,
    cutoffInstant,
    targetDate
  });
  const publicationLevel = publicationAudit.passed
    ? DATA_BRIEF_PUBLICATION_LEVEL
    : "草稿（发布门禁未通过，禁止自动发送）";
  const reportDirectory = publicationAudit.passed
    ? outputDirectory
    : provisionalOutputDirectory;

  if (runtimeState.baseRecord) {
    runtimeState.baseRecord = {
      ...runtimeState.baseRecord,
      publicationLevel,
      publicationBlockers: publicationAudit.blockers,
      marketCoverage: publicationAudit.marketCoverage
    };
  }

  const requestPath = path.join(workDirectory, "morning-request.json");
  const contextPath = path.join(workDirectory, "morning-context.json");
  const instructionPath = path.join(workDirectory, "run-instruction.md");
  const serializedContext = JSON.stringify(collected.context, null, 2) + "\n";
  const contextSha256 = createHash("sha256").update(serializedContext).digest("hex");
  if (
    options.frozenContextSha256
    && contextSha256 !== options.frozenContextSha256
  ) {
    throw new Error("frozen morning context hash mismatch; recollection is forbidden");
  }
  await writeFile(requestPath, JSON.stringify(request, null, 2) + "\n", "utf8");
  await writeAtomicFile(contextPath, serializedContext, 0o600);
  await writeRunningState("collect", {
    contextFile: path.relative(projectRoot, contextPath).replaceAll("\\", "/"),
    contextSha256
  });

  const instruction = buildRunInstruction({
    targetDate,
    workflow,
    agentDescriptor,
    loadedFrom: collected.loadedFrom,
    dataStatus: collected.context.meta.status,
    disclaimer: risk.disclaimer,
    cutoffLocal: cutoffLocal.display,
    cutoffIso: cutoffInstant.toISOString(),
    requireStaleMarketLabel,
    marketDataMaxAgeHours: risk.marketDataMaxAgeHours,
    newsMaxAgeHours: risk.newsMaxAgeHours,
    publicationLevel,
    publicationAudit
  });
  await writeFile(instructionPath, instruction, "utf8");

  if (options.tradingDaysOnly) {
    const tradingStatus = collected.context.data?.tradingCalendar?.status ?? "unknown";
    if (tradingStatus === "non_trading_day") {
      const skipRecord = {
        schemaVersion: "1.0",
        targetDate,
        startedAt: runStartedAt.toISOString(),
        frozenCutoffInstant: cutoffInstant.toISOString(),
        cutoffLocal: cutoffLocal.display,
        contextFile: path.relative(projectRoot, contextPath).replaceAll("\\", "/"),
        contextSha256,
        completedAt: new Date().toISOString(),
        agentExecutor: agentDescriptor?.executor ?? "none",
        provider: agentDescriptor?.provider ?? null,
        model: agentDescriptor?.model ?? null,
        thinking: agentDescriptor?.reasoning ?? null,
        dataStatus: collected.context.meta.status,
        tradingStatus,
        publicationLevel,
        publicationGate: "not_applicable",
        status: "skipped",
        phase: "collect",
        reason: "目标日期经交易日历确认为非交易日，未调用模型。"
      };
      await writeAtomicJson(
        path.join(workDirectory, "last-run.json"),
        skipRecord
      );
      console.log(JSON.stringify(skipRecord, null, 2));
      return;
    }
    if (tradingStatus !== "trading_day") {
      runtimeState.failureCode = "trading_calendar_unknown";
      throw new Error("计划任务无法确认目标日期是否为交易日，已停止且未调用模型");
    }
  }

  if (options.dryRun || options.prepareOnly) {
    console.log(JSON.stringify({
      ok: true,
      mode: options.prepareOnly ? "prepare-only" : "dry-run",
      targetDate,
      cutoffLocal: cutoffLocal.display,
      frozenCutoffInstant: cutoffInstant.toISOString(),
      contextSha256,
      requireStaleMarketLabel,
      publicationLevel,
      publicationGate: publicationAudit.passed ? "passed" : "blocked",
      publicationBlockers: publicationAudit.blockers,
      marketCoverage: publicationAudit.marketCoverage,
      agentExecutor: agentDescriptor?.executor ?? "none",
      model: agentDescriptor ? `${agentDescriptor.provider}/${agentDescriptor.model}` : null,
      dataStatus: collected.context.meta.status,
      loadedFrom: collected.loadedFrom,
      requestFile: path.relative(projectRoot, requestPath),
      contextFile: path.relative(projectRoot, contextPath),
      instructionFile: path.relative(projectRoot, instructionPath)
    }, null, 2));
    return;
  }

  // A genuine core-data block is terminal and model-free for every execution
  // path, including explicit review recovery. A reviewer cannot repair the
  // frozen packet's missing or mismatched market evidence.
  if (!publicationAudit.passed) {
    await writeRunningState("validate", {
      reportMode: workflow.reportMode,
      reportVariant: "none",
      analysisStatus: "not_run",
      analysisAttempts: 0,
      reviewAttempts: 0
    });
    await writeAtomicJson(runtimeState.recordPath, {
      ...runtimeState.baseRecord,
      status: "blocked",
      phase: "validate",
      publicationGate: "blocked",
      completedAt: new Date().toISOString(),
      evidenceReview: "not_applicable",
      reportMode: workflow.reportMode,
      reportVariant: "none",
      analysisStatus: "not_run",
      analysisAttempts: 0,
      reviewAttempts: 0
    });
    console.warn(`核心数据发布门禁阻断，未调用模型或生成正式报告：${publicationAudit.blockers.join("；")}`);
    process.exitCode = 2;
    return;
  }

  const notificationPresentation = await loadFrozenNotificationPresentation(projectRoot);
  const frozenRun = await freezeRunInput({
    projectRoot, archiveRoot: path.join(workDirectory, "report-archives"), outputDirectory,
    rawContext: collected.context, configuration: { stocks, risk, workflow, notificationPresentation },
    targetDate, cutoffInstant, cutoffLocal: cutoffLocal.display
  });
  const compilerContext = frozenRun.input.compilerContext;
  await writeAtomicJson(path.join(workDirectory, "compiler-context.json"), compilerContext);
  await writeRunningState("validate", {
    compilerContextSha256: frozenRun.input.compilerContextSha256,
    archiveInputSha256: frozenRun.inputSha256,
    archiveDirectory: path.relative(projectRoot, frozenRun.archiveDirectory).replaceAll("\\", "/")
  });

  if (!options.reviewExisting) {
    if (workflow.reportMode !== "structured_compiler") {
      throw tagFailure(
        new Error("automatic morning report requires structured_compiler mode"),
        "workflow_internal_error"
      );
    }

    const verifiedBaselinePath = path.join(workDirectory, "verified-baseline.md");
    const analysisInstructionPath = path.join(workDirectory, "analysis-instruction.md");
    const analysisAuditPath = path.join(workDirectory, "structured-analysis.json");
    const structuredAttachments = STRUCTURED_ANALYSIS_ATTACHMENTS;
    let analysisLauncher = null;
    let compilation;
    await writeRunningState("validate", {
      reportMode: workflow.reportMode,
      reportVariant: "verified_baseline",
      analysisStatus: "pending",
      analysisAttempts: 0,
      reviewAttempts: 0
    });
    try {
      compilation = await loadArchivedCompilation(frozenRun) ?? await runStructuredMorning({
        context: compilerContext,
        targetDate,
        cutoffLocal: cutoffLocal.display,
        cutoffInstant,
        publicationLevel,
        publicationAudit,
        config: {
          maxAnalysisAttempts: options.withStructuredAnalysis
            ? workflow.maxAnalysisAttempts
            : 0,
          ...(options.withStructuredAnalysis ? { analysisTimeoutMs: workflow.analysisTimeoutMs } : {}),
          disclaimer: risk.disclaimer,
          marketDataMaxAgeHours: risk.marketDataMaxAgeHours,
          newsMaxAgeHours: risk.newsMaxAgeHours
        },
        onBaseline: async (baselineReport) => {
          await writeAtomicFile(verifiedBaselinePath, baselineReport, 0o600);
        },
        executeAnalysis: async ({ attempt, instruction: analysisInstruction }) => {
          await writeRunningState("analysis", {
            reportMode: workflow.reportMode,
            reportVariant: "verified_baseline",
            analysisStatus: "running",
            analysisAttempts: attempt,
            reviewAttempts: 0
          });
          await writeAtomicFile(analysisInstructionPath, analysisInstruction, 0o600);
          const stderrPath = path.join(
            workDirectory,
            `structured-analysis-${attempt}-stderr.log`
          );
          let result;
          try {
            result = await runStructuredAnalyst({
              projectRoot,
              workflow,
              attachments: structuredAttachments
            });
          } catch (error) {
            await writeSanitizedStderr(stderrPath, error);
            throw error;
          }
          analysisLauncher = result.launcher;
          await writeSanitizedStderr(stderrPath, result.stderr);
          return result;
        }
      });
    } catch (error) {
      throw tagFailure(error, "workflow_internal_error");
    }

    await writeAtomicJson(analysisAuditPath, {
      schemaVersion: "1.0",
      targetDate,
      frozenCutoffInstant: cutoffInstant.toISOString(),
      contextSha256,
      status: compilation.analysisStatus,
      semanticCandidateCount: compilation.semanticCandidateCount,
      attempts: compilation.analysisAttempts,
      failures: compilation.analysisFailures,
      overlay: compilation.overlay,
      compilerContextSha256: frozenRun.input.compilerContextSha256,
      archiveInputSha256: frozenRun.inputSha256
    });

    const sealedResult = await sealRunResult(frozenRun, compilation);
    const reportPath = path.join(outputDirectory, `${targetDate}.md`);
    await writeRunningState("publish", {
      compilationInputSha256: sealedResult.compilationInputSha256,
      reportMode: workflow.reportMode,
      reportVariant: compilation.mode,
      analysisStatus: compilation.analysisStatus,
      semanticCandidateCount: compilation.semanticCandidateCount,
      analysisAttempts: compilation.analysisAttempts,
      reviewAttempts: 0
    });
    await publishArchivedReport(frozenRun, reportPath);
    await writeAtomicJson(runtimeState.recordPath, {
      ...runtimeState.baseRecord,
      status: "success",
      phase: "publish",
      publicationGate: "passed",
      completedAt: new Date().toISOString(),
      evidenceReview: "passed",
      evidenceReviewScope: "structure_source_freshness_only",
      researchSemanticReview: "not_human_reviewed",
      reportMode: workflow.reportMode,
      reportVariant: compilation.mode,
      analysisStatus: compilation.analysisStatus,
      semanticCandidateCount: compilation.semanticCandidateCount,
      analysisAttempts: compilation.analysisAttempts,
      analysisFailures: compilation.analysisFailures,
      reviewAttempts: 0,
      reportFile: path.relative(projectRoot, reportPath).replaceAll("\\", "/"),
      agentLauncher: "deterministic-compiler",
      ...(analysisLauncher ? { analysisLauncher } : {})
    });

    if (compilation.mode === "structured_enhanced") {
      console.log(`晨报已由程序编译并通过发布门禁（含结构化分析）：${reportPath}`);
    } else if (compilation.analysisStatus === "disabled") {
      console.log(`结构化模型分析未启用，已发布程序核验基础版：${reportPath}`);
    } else if (compilation.analysisStatus === "skipped_no_semantic_candidate") {
      console.log(`没有程序证明合格的语义候选，已直接发布程序核验基础版：${reportPath}`);
    } else {
      console.warn(`结构化分析不可用或不合格，已自动发布程序核验基础版：${reportPath}`);
    }
    return;
  }

  // Explicit operator recovery keeps the legacy reviewer available for an
  // already frozen candidate. The scheduled path above can never reach the
  // free-form generator or reviewer.
  const agentResult = {
    stdout: "",
    stderr: "",
    launcher: "existing-reviewed-output"
  };
  let reviewInputPath = path.join(workDirectory, "last-pi-output.md");
  let report = null;
  let reviewAttempts = 0;
  const feedbackPath = path.join(workDirectory, "review-feedback.md");
  for (
    let attempt = 1;
    attempt <= workflow.maxReviewAttempts;
    attempt += 1
  ) {
    reviewAttempts = attempt;
    const reviewerAttachments = [
      "agents/report_reviewer.md",
      "agents/morning_report.md",
      "config/risk.json",
      "config/data-sources.json",
      path.relative(projectRoot, contextPath),
      path.relative(projectRoot, path.join(workDirectory, "compiler-context.json")),
      path.relative(projectRoot, instructionPath),
      path.relative(projectRoot, reviewInputPath)
    ];
    if (attempt > 1 || options.reviewExisting) {
      reviewerAttachments.push(path.relative(projectRoot, feedbackPath));
    }

    await writeRunningState("review", { reviewAttempts: attempt });
    let reviewerResult;
    try {
      reviewerResult = await runReportReviewer({
        projectRoot,
        workflow,
        attachments: reviewerAttachments
      });
    } catch (error) {
      throw tagFailure(
        error,
        agentDescriptor.executor === "hermes"
          ? "hermes_review_failed"
          : "pi_review_failed"
      );
    }
    const attemptOutputPath = path.join(
      workDirectory,
      "last-pi-review-" + attempt + ".md"
    );
    const reviewedOutputPath = path.join(workDirectory, "last-pi-output.md");
    await writeFile(attemptOutputPath, reviewerResult.stdout, "utf8");
    await writeFile(reviewedOutputPath, reviewerResult.stdout, "utf8");
    await writeSanitizedStderr(
      path.join(workDirectory, "last-pi-review-" + attempt + "-stderr.log"),
      reviewerResult.stderr
    );

    await writeRunningState("validate", { reviewAttempts: attempt });
    try {
      report = normalizeReportOutput(reviewerResult.stdout, {
        targetDate,
        disclaimer: risk.disclaimer,
        cutoffLocal: cutoffLocal.display,
        cutoffInstant,
        publicationLevel,
        requireStaleMarketLabel,
        context: compilerContext,
        strictContextSources: hasValidatedDataPacket,
        marketDataMaxAgeHours: risk.marketDataMaxAgeHours,
        newsMaxAgeHours: risk.newsMaxAgeHours,
        publicationAudit,
        enforcePublicationReady: publicationAudit.passed,
        requireDeterministicCompilerOutput: publicationAudit.passed
      });
      break;
    } catch (error) {
      if (attempt >= workflow.maxReviewAttempts) {
        throw tagFailure(error, "content_gate_exhausted");
      }
      const feedback = buildReviewFeedback(error);
      await writeFile(feedbackPath, feedback, "utf8");
      reviewInputPath = reviewedOutputPath;
    }
  }
  if (!report) {
    runtimeState.failureCode = "content_gate_exhausted";
    throw new Error("证据审校没有产生可发布报告");
  }
  const reportPath = path.join(reportDirectory, `${targetDate}.md`);
  await writeRunningState("publish", { reviewAttempts });
  // A failed final write must never leave a truncated formal report that a
  // later operator could mistake for a publishable artifact.
  const sealedResult = await captureReviewedReport(frozenRun, report);
  await publishArchivedReport(frozenRun, reportPath);

  const runRecord = {
    ...runtimeState.baseRecord,
    compilationInputSha256: sealedResult.compilationInputSha256,
    status: publicationAudit.passed ? "success" : "blocked",
    phase: "publish",
    publicationGate: publicationAudit.passed ? "passed" : "blocked",
    completedAt: new Date().toISOString(),
    evidenceReview: "passed",
    evidenceReviewScope: "structure_source_freshness_only",
    researchSemanticReview: "not_human_reviewed",
    reviewAttempts,
    reportFile: path.relative(projectRoot, reportPath),
    agentLauncher: agentResult.launcher,
    ...(agentDescriptor.executor === "pi"
      ? { piLauncher: agentResult.launcher }
      : {})
  };
  await writeAtomicJson(
    runtimeState.recordPath,
    runRecord
  );

  if (publicationAudit.passed) {
    console.log(`晨报已生成并通过发布门禁：${reportPath}`);
  } else {
    console.warn(`晨报草稿已生成，但发布门禁阻断自动发送：${reportPath}`);
    console.warn(`阻断原因：${publicationAudit.blockers.join("；")}`);
    process.exitCode = 2;
  }
  if (agentResult.stderr.trim()) {
    console.warn(`${agentDescriptor.executor} 执行器产生了提示信息，但晨报生成成功。`);
  }
}

main().catch(async (error) => {
  if (runtimeState.recordPath && runtimeState.baseRecord) {
    try {
      const failure = failureMetadata(error);
      await writeAtomicJson(
        runtimeState.recordPath,
        {
          ...runtimeState.baseRecord,
          status: "failed",
          phase: runtimeState.phase,
          publicationGate: "blocked",
          failedAt: new Date().toISOString(),
          failureCode: failure.failureCode,
          retryable: failure.retryable,
          error: boundedLegacyError(error),
          rawError: boundedRawError(error)
        }
      );
    } catch {
      // Keep the original failure as the primary error.
    }
  }
  console.error(`晨报工作流失败：${boundedLegacyError(error)}`);
  process.exitCode = 1;
});
