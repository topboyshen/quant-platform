import path from "node:path";
import { RESULT_VIEW_VERSION } from "./result-view.mjs";
import { V3_VERSION } from "./result-view-v3.mjs";

const TERMINAL_CLOCK_SKEW_MS = 5_000;
const VALID_DATA_STATUSES = new Set(["live", "partial"]);
const RUN_PHASES = new Set(["collect", "generate", "review", "analysis", "validate", "publish"]);
const FAILURE_RETRYABILITY = Object.freeze({
  data_bridge_timeout: true,
  data_bridge_failed: true,
  trading_calendar_unknown: true,
  hermes_generation_failed: true,
  hermes_review_failed: true,
  pi_generation_failed: true,
  pi_review_failed: true,
  content_gate_exhausted: false,
  report_publish_failed: false,
  workflow_internal_error: false
});

function parseTime(value) {
  if (typeof value !== "string") return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function dateInTimeZone(timeZone, now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(now);
  const get = (type) => parts.find((part) => part.type === type)?.value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

function hasFreshTerminalState(lastRun, invocationStartedAt, invocationEndedAt) {
  const invocationStart = parseTime(invocationStartedAt);
  const invocationEnd = parseTime(invocationEndedAt);
  const runStart = parseTime(lastRun?.startedAt);
  const terminal = parseTime(lastRun?.completedAt ?? lastRun?.failedAt);
  if ([invocationStart, invocationEnd, runStart, terminal].some((value) => value === null)) {
    return false;
  }
  return (
    invocationStart <= invocationEnd &&
    runStart >= invocationStart - TERMINAL_CLOCK_SKEW_MS &&
    runStart <= invocationEnd + TERMINAL_CLOCK_SKEW_MS &&
    terminal >= runStart &&
    terminal <= invocationEnd + TERMINAL_CLOCK_SKEW_MS
  );
}

export function expectedDailyReportFile(targetDate) {
  return path.posix.join("reports", "daily", `${targetDate}.md`);
}

export function classifyRunOutcome({
  exitCode,
  invocationStartedAt,
  invocationEndedAt,
  expectedTargetDate,
  lastRun
}) {
  if (!Number.isInteger(exitCode) || exitCode < 0 || exitCode > 255) {
    throw new Error("exitCode 必须是 0—255 的整数");
  }

  // GNU timeout's 124 is authoritative even when SIGKILL prevented the child
  // from writing a terminal record. This is failure-only and never attaches a
  // report, so an old success cannot be reused as a delivery.
  if (exitCode === 124) {
    return {
      outcome: "failed",
      reasonCode: "workflow_timeout",
      workflowExitCode: exitCode
    };
  }

  if (!lastRun || typeof lastRun !== "object" || Array.isArray(lastRun)) {
    return { outcome: "failed", reasonCode: "fresh_run_state_missing", workflowExitCode: exitCode };
  }
  if (lastRun.targetDate !== expectedTargetDate) {
    return { outcome: "failed", reasonCode: "stale_target_date", workflowExitCode: exitCode };
  }
  if (!hasFreshTerminalState(lastRun, invocationStartedAt, invocationEndedAt)) {
    return {
      outcome: "failed",
      reasonCode: "stale_or_incomplete_run_state",
      workflowExitCode: exitCode
    };
  }

  if (exitCode !== 0) {
    if (
      lastRun.status === "failed" &&
      lastRun.publicationGate === "blocked" &&
      RUN_PHASES.has(lastRun.phase) &&
      Object.hasOwn(FAILURE_RETRYABILITY, lastRun.failureCode) &&
      lastRun.retryable === FAILURE_RETRYABILITY[lastRun.failureCode]
    ) {
      return {
        outcome: "failed",
        reasonCode: lastRun.failureCode,
        retryable: lastRun.retryable,
        workflowExitCode: exitCode
      };
    }
    if (
      exitCode === 2 &&
      lastRun.status === "blocked" &&
      lastRun.publicationGate === "blocked"
    ) {
      return {
        outcome: "failed",
        reasonCode: "publication_gate_blocked",
        workflowExitCode: exitCode
      };
    }
    return {
      outcome: "failed",
      reasonCode: "unexpected_terminal_state",
      workflowExitCode: exitCode
    };
  }

  if (lastRun.status === "skipped") {
    return { outcome: "skipped", reasonCode: "non_trading_day", workflowExitCode: 0 };
  }

  if (lastRun.status !== "success") {
    return { outcome: "failed", reasonCode: "unexpected_terminal_state", workflowExitCode: 0 };
  }
  if (lastRun.evidenceReview !== "passed") {
    return { outcome: "failed", reasonCode: "evidence_gate_not_passed", workflowExitCode: 0 };
  }
  if (lastRun.publicationGate !== "passed") {
    return { outcome: "failed", reasonCode: "publication_gate_not_passed", workflowExitCode: 0 };
  }
  if (!VALID_DATA_STATUSES.has(lastRun.dataStatus)) {
    return { outcome: "failed", reasonCode: "validated_data_missing", workflowExitCode: 0 };
  }
  const reportFile = expectedDailyReportFile(expectedTargetDate);
  const normalizedReportFile = String(lastRun.reportFile ?? "").replaceAll("\\", "/");
  if (normalizedReportFile !== reportFile) {
    return { outcome: "failed", reasonCode: "formal_report_path_invalid", workflowExitCode: 0 };
  }

  return {
    outcome: "success",
    reasonCode: "formal_report_ready",
    workflowExitCode: 0,
    reportFile,
    dataStatus: lastRun.dataStatus,
    completedAt: lastRun.completedAt
  };
}

export function notificationEventId(targetDate, outcome) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(targetDate)) throw new Error("targetDate 格式无效");
  if (!new Set(["success", "failed", "skipped"]).has(outcome)) {
    throw new Error("outcome 无效");
  }
  return `astock-${targetDate}-${outcome}-telegram-v1`;
}

export function correctionEventId(targetDate, revision) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(targetDate)) throw new Error("targetDate 格式无效");
  if (!/^v[1-9]\d{0,5}$/.test(revision)) throw new Error("revision 格式无效");
  return `astock-${targetDate}-correction-${revision}-telegram-v1`;
}

export function expectedCorrectionReportFile(targetDate, revision) {
  correctionEventId(targetDate, revision);
  return path.posix.join("reports", "corrections", `${targetDate}-${revision}.md`);
}

export function buildFixedCaption(event) {
  const footer = `事件：${event.eventId}`;
  if (event.eventType === "correction") {
    return [
      `AStock A股基础数据快报更正｜${event.targetDate}｜${event.revision}`,
      "状态：更正版，原稿保留供审计。",
      "更正报告正文见 Markdown 附件。",
      footer
    ].join("\n");
  }
  if (event.outcome === "success") {
    const dataLabel = event.dataStatus === "live" ? "实时完整" : "部分可用";
    if (event.report?.presentation === V3_VERSION) {
      return [
        `AStock 研究简报｜${event.targetDate}`,
        `数据：${event.dataStatus === "live" ? "实时数据包" : "部分可用"}；研究结论未经人工审校。`,
        "四问正文与完整来源见 Markdown 附件。"
      ].join("\n");
    }
    if (event.report?.presentation === RESULT_VIEW_VERSION) {
      return [
        `AStock 结果简报｜${event.targetDate}`,
        `数据：${event.dataStatus === "live" ? "实时数据包" : "部分可用"}；研究结论未经人工审校。`,
        "结论、股票池与风险见 Markdown 附件。"
      ].join("\n");
    }
    return [
      `AStock 数据快报（语义未审）｜${event.targetDate}`,
      "状态：快报附件已生成",
      "门禁：结构、来源与时效检查通过；不等于研究语义已审",
      `数据：${dataLabel}`,
      "研究线索：未作人工语义审校",
      "快报正文见 Markdown 附件。",
      footer
    ].join("\n");
  }
  if (event.outcome === "skipped") {
    return [
      `AStock A股基础数据快报｜${event.targetDate}`,
      "状态：交易日历确认为非交易日",
      "未调用模型，也未生成快报。",
      footer
    ].join("\n");
  }
  const failureLabels = {
    data_bridge_timeout: "A 股数据桥接超时",
    data_bridge_failed: "A 股数据桥接失败",
    trading_calendar_unknown: "交易日历状态无法确认",
    hermes_generation_failed: "Hermes 晨报生成失败",
    hermes_review_failed: "Hermes 晨报审校失败",
    pi_generation_failed: "Pi 晨报生成失败",
    pi_review_failed: "Pi 晨报审校失败",
    content_gate_exhausted: "内容质量门禁多次审校后仍未通过",
    report_publish_failed: "快报文件落盘失败",
    workflow_internal_error: "工作流内部错误",
    workflow_timeout: "工作流超时",
    workflow_nonzero_exit: "工作流异常退出",
    publication_gate_blocked: "核心数据发布门禁阻断",
    fresh_run_state_missing: "本次运行状态缺失",
    stale_target_date: "检测到陈旧运行状态",
    stale_or_incomplete_run_state: "本次运行状态不完整",
    unexpected_terminal_state: "终态异常",
    evidence_gate_not_passed: "证据审校未通过",
    publication_gate_not_passed: "核心数据发布门禁未通过",
    validated_data_missing: "有效数据包缺失",
    formal_report_path_invalid: "快报路径校验失败",
    report_snapshot_invalid: "快报快照校验失败"
  };
  return [
    `AStock 晨报失败｜${event.targetDate}`,
    `原因：${failureLabels[event.reasonCode] ?? "工作流状态异常"}`,
    "快报未推送，请运行 journalctl --user -u astock-xhs-source.service --since today --no-pager 检查日志。",
    footer
  ].join("\n");
}

export function buildHermesDelivery(event, payloadAbsolutePath = null) {
  const caption = buildFixedCaption(event);
  const hasDocument = event.outcome === "success" || event.eventType === "correction";
  if (!hasDocument) {
    return { target: "telegram", message: caption };
  }
  if (typeof payloadAbsolutePath !== "string" || !path.isAbsolute(payloadAbsolutePath)) {
    throw new Error("文档事件缺少固定绝对 payload 路径");
  }
  return {
    target: "telegram",
    message: `${caption}\n\n[[as_document]] MEDIA:${payloadAbsolutePath}`
  };
}
