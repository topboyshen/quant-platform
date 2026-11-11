import { buildVerifiedBaselineReport } from "./baseline-report.mjs";
import {
  buildStructuredAnalysisCandidates,
  buildStructuredAnalysisInstruction,
  parseStructuredAnalysisOutput
} from "./structured-analysis.mjs";
import {
  normalizeReportOutput,
  requiresStaleMarketLabel
} from "./report.mjs";

const MAX_ANALYSIS_ATTEMPTS = 2;
const FAILURE_CODES = Object.freeze({
  timeout: "analysis_timeout",
  execution: "analysis_execution_failed",
  invalid: "analysis_invalid",
  compile: "enhanced_compile_failed",
  instruction: "analysis_instruction_failed",
  unavailable: "analysis_executor_unavailable"
});

function isPositiveNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function cloneAndFreeze(value, label) {
  let cloned;
  try {
    cloned = structuredClone(value);
  } catch {
    throw new Error(`${label} 必须是可冻结的结构化数据`);
  }
  const seen = new WeakSet();
  const freeze = (item) => {
    if (!item || typeof item !== "object" || seen.has(item)) return item;
    seen.add(item);
    for (const child of Object.values(item)) freeze(child);
    return Object.freeze(item);
  };
  return freeze(cloned);
}

function validateConfig(config) {
  if (!config || typeof config !== "object" || Array.isArray(config)) {
    throw new Error("structured morning config 必须是对象");
  }
  const maxAnalysisAttempts = config.maxAnalysisAttempts;
  if (!Number.isInteger(maxAnalysisAttempts) ||
      maxAnalysisAttempts < 0 || maxAnalysisAttempts > MAX_ANALYSIS_ATTEMPTS) {
    throw new Error(`maxAnalysisAttempts 必须是 0—${MAX_ANALYSIS_ATTEMPTS} 的整数`);
  }
  if (config.analysisTimeoutMs !== undefined &&
      (!Number.isInteger(config.analysisTimeoutMs) || config.analysisTimeoutMs < 1000)) {
    throw new Error("analysisTimeoutMs 必须是至少 1000 ms 的整数");
  }
  if (typeof config.disclaimer !== "string" || config.disclaimer.trim() === "") {
    throw new Error("structured morning config 缺少免责声明");
  }
  if (!isPositiveNumber(config.marketDataMaxAgeHours) ||
      !isPositiveNumber(config.newsMaxAgeHours)) {
    throw new Error("structured morning 时效阈值必须是正数");
  }
  return Object.freeze({
    maxAnalysisAttempts,
    analysisTimeoutMs: config.analysisTimeoutMs ?? null,
    disclaimer: config.disclaimer,
    marketDataMaxAgeHours: config.marketDataMaxAgeHours,
    newsMaxAgeHours: config.newsMaxAgeHours
  });
}

function resolveCutoff(cutoffInstant) {
  const cutoff = new Date(cutoffInstant);
  if (Number.isNaN(cutoff.getTime())) {
    throw new Error("structured morning 缺少有效 cutoffInstant");
  }
  return cutoff;
}

function normalizationOptions({
  context,
  targetDate,
  cutoffLocal,
  cutoff,
  publicationLevel,
  publicationAudit,
  config,
  requireStaleMarketLabel
}) {
  return {
    targetDate,
    disclaimer: config.disclaimer,
    cutoffLocal,
    cutoffInstant: cutoff,
    publicationLevel,
    requireStaleMarketLabel,
    context,
    strictContextSources: true,
    marketDataMaxAgeHours: config.marketDataMaxAgeHours,
    newsMaxAgeHours: config.newsMaxAgeHours,
    publicationAudit,
    enforcePublicationReady: true,
    requireDeterministicCompilerOutput: true
  };
}

function compileReport({
  context,
  targetDate,
  cutoffLocal,
  cutoff,
  publicationLevel,
  publicationAudit,
  config,
  requireStaleMarketLabel,
  analysisOverlay
}) {
  const compiled = buildVerifiedBaselineReport({
    context,
    targetDate,
    cutoffLocal,
    cutoffInstant: cutoff,
    publicationLevel,
    publicationAudit,
    disclaimer: config.disclaimer,
    marketDataMaxAgeHours: config.marketDataMaxAgeHours,
    newsMaxAgeHours: config.newsMaxAgeHours,
    analysisOverlay
  });
  return normalizeReportOutput(compiled, normalizationOptions({
    context,
    targetDate,
    cutoffLocal,
    cutoff,
    publicationLevel,
    publicationAudit,
    config,
    requireStaleMarketLabel
  }));
}

function analysisText(result) {
  if (typeof result === "string") return result;
  if (result && typeof result === "object" && typeof result.stdout === "string") {
    return result.stdout;
  }
  throw new Error("analysis executor 未返回字符串或 stdout");
}

function isTimeout(error) {
  return error?.code === "ETIMEDOUT" ||
    error?.name === "AbortError" ||
    /(?:timeout|timed out|超时)/i.test(String(error?.message ?? ""));
}

async function emitAttempt(onAttempt, event, observerState) {
  if (typeof onAttempt !== "function") return;
  try {
    await onAttempt(Object.freeze({ ...event }));
  } catch {
    observerState.failures += 1;
  }
}

function baselineResult({
  baselineReport,
  analysisAttempts,
  analysisFailures,
  observerFailures,
  analysisStatus,
  semanticCandidateCount
}) {
  return {
    mode: "verified_baseline",
    analysisStatus,
    semanticCandidateCount,
    report: baselineReport,
    baselineReport,
    overlay: null,
    analysisAttempts,
    analysisFailures,
    observerFailures
  };
}

/**
 * Compile the publishable baseline before any model call, then optionally add
 * a schema-checked analysis overlay. No legacy prose generator is reachable
 * from this orchestration layer.
 */
export async function runStructuredMorning({
  context,
  targetDate,
  cutoffLocal,
  cutoffInstant,
  publicationLevel,
  publicationAudit,
  config,
  executeAnalysis,
  onBaseline = null,
  onAttempt = null
}) {
  const frozenContext = cloneAndFreeze(context, "context");
  const frozenAudit = cloneAndFreeze(publicationAudit, "publicationAudit");
  const normalizedConfig = validateConfig(config);
  const cutoff = resolveCutoff(cutoffInstant);
  const requireStale = requiresStaleMarketLabel({
    context: frozenContext,
    cutoffInstant: cutoff,
    marketDataMaxAgeHours: normalizedConfig.marketDataMaxAgeHours
  });
  const compileArgs = Object.freeze({
    context: frozenContext,
    targetDate,
    cutoffLocal,
    cutoff,
    publicationLevel,
    publicationAudit: frozenAudit,
    config: normalizedConfig,
    requireStaleMarketLabel: requireStale
  });

  // This is intentionally outside every analysis catch. If the deterministic
  // report is not publishable, no analyst is called and the caller gets the
  // actual baseline failure.
  const baselineReport = compileReport({
    ...compileArgs,
    analysisOverlay: null
  });
  if (onBaseline !== null) {
    if (typeof onBaseline !== "function") {
      throw new Error("onBaseline 必须是函数或 null");
    }
    // Persisting the already validated deterministic fallback is part of the
    // production handoff. Unlike diagnostic attempt observers, this callback
    // is fail-closed and runs before any model invocation.
    await onBaseline(baselineReport);
  }
  const observerState = { failures: 0 };
  const analysisFailures = [];
  const candidates = buildStructuredAnalysisCandidates(frozenContext);

  if (normalizedConfig.maxAnalysisAttempts === 0) {
    return baselineResult({
      baselineReport,
      analysisAttempts: 0,
      analysisFailures,
      observerFailures: observerState.failures,
      analysisStatus: "disabled",
      semanticCandidateCount: candidates.length
    });
  }
  if (candidates.length === 0) {
    return baselineResult({
      baselineReport,
      analysisAttempts: 0,
      analysisFailures,
      observerFailures: observerState.failures,
      analysisStatus: "skipped_no_semantic_candidate",
      semanticCandidateCount: 0
    });
  }
  if (typeof executeAnalysis !== "function") {
    analysisFailures.push({ attempt: 0, reasonCode: FAILURE_CODES.unavailable });
    return baselineResult({
      baselineReport,
      analysisAttempts: 0,
      analysisFailures,
      observerFailures: observerState.failures,
      analysisStatus: "degraded",
      semanticCandidateCount: candidates.length
    });
  }

  let instruction;
  try {
    instruction = buildStructuredAnalysisInstruction({
      targetDate,
      cutoffLocal,
      context: frozenContext
    });
  } catch {
    analysisFailures.push({ attempt: 0, reasonCode: FAILURE_CODES.instruction });
    return baselineResult({
      baselineReport,
      analysisAttempts: 0,
      analysisFailures,
      observerFailures: observerState.failures,
      analysisStatus: "degraded",
      semanticCandidateCount: candidates.length
    });
  }

  let analysisAttempts = 0;
  for (let attempt = 1; attempt <= normalizedConfig.maxAnalysisAttempts; attempt += 1) {
    analysisAttempts = attempt;
    await emitAttempt(onAttempt, {
      attempt,
      maxAttempts: normalizedConfig.maxAnalysisAttempts,
      status: "started"
    }, observerState);

    let output;
    try {
      const result = await executeAnalysis(Object.freeze({
        attempt,
        maxAttempts: normalizedConfig.maxAnalysisAttempts,
        targetDate,
        cutoffLocal,
        cutoffInstant: cutoff.toISOString(),
        timeoutMs: normalizedConfig.analysisTimeoutMs,
        instruction,
        context: frozenContext,
        previousFailureReason: analysisFailures.at(-1)?.reasonCode ?? null
      }));
      output = analysisText(result);
    } catch (error) {
      const reasonCode = isTimeout(error) ? FAILURE_CODES.timeout : FAILURE_CODES.execution;
      analysisFailures.push({ attempt, reasonCode });
      await emitAttempt(onAttempt, {
        attempt,
        maxAttempts: normalizedConfig.maxAnalysisAttempts,
        status: "failed",
        reasonCode
      }, observerState);
      continue;
    }

    let overlay;
    try {
      overlay = parseStructuredAnalysisOutput(output, { context: frozenContext });
    } catch {
      const reasonCode = FAILURE_CODES.invalid;
      analysisFailures.push({ attempt, reasonCode });
      await emitAttempt(onAttempt, {
        attempt,
        maxAttempts: normalizedConfig.maxAnalysisAttempts,
        status: "failed",
        reasonCode
      }, observerState);
      continue;
    }

    if (overlay === null) {
      await emitAttempt(onAttempt, {
        attempt,
        maxAttempts: normalizedConfig.maxAnalysisAttempts,
        status: "skipped"
      }, observerState);
      return baselineResult({
        baselineReport,
        analysisAttempts,
        analysisFailures,
        observerFailures: observerState.failures,
        analysisStatus: "skipped_by_analyst",
        semanticCandidateCount: candidates.length
      });
    }

    try {
      const report = compileReport({
        ...compileArgs,
        analysisOverlay: overlay
      });
      await emitAttempt(onAttempt, {
        attempt,
        maxAttempts: normalizedConfig.maxAnalysisAttempts,
        status: "succeeded"
      }, observerState);
      return {
        mode: "structured_enhanced",
        analysisStatus: "passed",
        semanticCandidateCount: candidates.length,
        report,
        baselineReport,
        overlay,
        analysisAttempts,
        analysisFailures,
        observerFailures: observerState.failures
      };
    } catch {
      const reasonCode = FAILURE_CODES.compile;
      analysisFailures.push({ attempt, reasonCode });
      await emitAttempt(onAttempt, {
        attempt,
        maxAttempts: normalizedConfig.maxAnalysisAttempts,
        status: "failed",
        reasonCode
      }, observerState);
    }
  }

  return baselineResult({
    baselineReport,
    analysisAttempts,
    analysisFailures,
    observerFailures: observerState.failures,
    analysisStatus: "degraded",
    semanticCandidateCount: candidates.length
  });
}

export const orchestrateStructuredMorning = runStructuredMorning;
