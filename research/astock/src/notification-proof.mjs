// Read-only notification evidence and authorization checks. Never queues, rewrites artifacts, or sends.
import path from "node:path";
import { createHash } from "node:crypto";
import { lstat, readFile, realpath } from "node:fs/promises";
import { notificationEventId } from "./run-notification.mjs";
import { dateInTimeZone } from "./config.mjs";
import { buildReportPresentation, isResultPresentation } from "./report-presentation.mjs";
import { validateMorningSealProof } from "./morning-seal-proof.mjs";
import { sha256, isZonedIsoInstant, deliveryIdentity, PROJECT_ROOT, MAX_LAST_RUN_BYTES, MAX_EVENT_BYTES, MAX_REPORT_BYTES, ORCHESTRATOR_INVOCATION_PATTERN, DELIVERY_AUTHORIZATION_SCHEDULED, DELIVERY_AUTHORIZATION_AWAITING, MAX_OCCURRENCE_BYTES, MAX_AUTHORIZATION_BYTES, MAX_SEAL_BYTES } from "./notification-contract.mjs";
import { layout, readBounded } from "./notification-store.mjs";
import { readSendReceipt } from "./notification-receipts.mjs";

const SEND_AUTHORIZATION_KEYS = new Set([
  "schemaVersion", "eventId", "targetDate", "route", "payloadSha256", "payloadBytes",
  "grantedAt", "consumedAt"
]);


async function readFormalReport(projectRoot, targetDate, reportFile) {
  const expectedRelative = `reports/daily/${targetDate}.md`;
  if (reportFile !== expectedRelative) throw new Error("正式报告相对路径不匹配");

  const reportPath = path.join(projectRoot, ...expectedRelative.split("/"));
  const reportInfo = await lstat(reportPath);
  if (!reportInfo.isFile() || reportInfo.isSymbolicLink()) throw new Error("正式报告不是普通文件");
  if (reportInfo.size <= 0 || reportInfo.size > MAX_REPORT_BYTES) {
    throw new Error("正式报告为空或超过 1 MiB");
  }
  const canonicalRoot = await realpath(path.join(projectRoot, "reports", "daily"));
  const canonicalReport = await realpath(reportPath);
  const relativeCanonical = path.relative(canonicalRoot, canonicalReport);
  if (relativeCanonical.startsWith("..") || path.isAbsolute(relativeCanonical)) {
    throw new Error("正式报告越出固定目录");
  }
  const body = await readFile(canonicalReport);
  if (body.length <= 0 || body.length > MAX_REPORT_BYTES) throw new Error("正式报告大小无效");
  return body;
}



function resultViewPayload(sourceBody, targetDate, presentation) {
  const sourceText = new TextDecoder("utf-8", { fatal: true }).decode(sourceBody);
  const body = Buffer.from(buildReportPresentation(sourceText, { targetDate, presentation }), "utf8");
  if (body.length <= 0 || body.length > MAX_REPORT_BYTES) throw new Error("结果视图大小无效");
  return body;
}



/* ---------------- 发送授权门禁 ---------------- */

/* 只有编排器认领过的当日晨报运行才算“定时”。claimMorningOccurrence 写入的
   running 记录与随后的 success 记录都带 UUID invocationId；人工恢复生成不会
   写出这种记录，因此恢复事件默认只留档、标记为“等待授权”。 */
async function runWasOrchestratorClaimed(projectRoot, targetDate) {
  const filePath = path.join(
    projectRoot,
    "work",
    "orchestrator",
    "states",
    `morning-report-${targetDate}.json`
  );
  let parsed;
  try {
    parsed = JSON.parse((await readBounded(filePath, MAX_OCCURRENCE_BYTES)).toString("utf8"));
  } catch {
    return false;
  }
  return Boolean(
    parsed
    && typeof parsed === "object"
    && !Array.isArray(parsed)
    && parsed.task === "morning-report"
    && parsed.targetDate === targetDate
    && (parsed.status === "running" || parsed.status === "success")
    && ORCHESTRATOR_INVOCATION_PATTERN.test(String(parsed.invocationId ?? ""))
  );
}



/* 密封件查找必须区分「不存在」与「存在但无效」：
   只有新密封确实不存在（ENOENT）才允许按既定兼容策略看旧草稿清单；
   新密封存在却损坏、不可读、超限或是符号链接时，一律拒绝自动发送，绝不回退——
   否则把新密封破坏掉就成了绕过门禁的手段。 */
async function readSealedReport(projectRoot, targetDate, reportSha) {
  for (const [kind, relative] of [
    ["research", `work/research-seal/${targetDate}/${reportSha}.json`],
    ["legacy", `work/xhs/drafts/${targetDate}/${reportSha}.json`]
  ]) {
    let raw;
    try {
      raw = await readBounded(path.join(projectRoot, ...relative.split("/")), MAX_SEAL_BYTES);
    } catch (error) {
      if (error?.code === "ENOENT" || error?.code === "ENOTDIR") {
        continue; // 该位置确实没有密封件：继续按顺序尝试下一个固定位置。
      }
      return { present: true, invalid: `seal_unreadable_${kind}` };
    }
    let parsed;
    try {
      parsed = JSON.parse(raw.toString("utf8"));
    } catch {
      return { present: true, invalid: `seal_not_json_${kind}` };
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { present: true, invalid: `seal_shape_${kind}` };
    }
    return { present: true, invalid: null, kind, seal: parsed };
  }
  return { present: false, invalid: null, seal: null };
}



/* 自动发送必须证明「这次运行产出了这份待发正文」。任一环缺失即不自动发送：
   认领记录只证明当天跑过一次编排，不能单独证明待发正文由它产生——当天先有一次
   正常认领运行、之后人工恢复生成另一份报告，就是必须拦下的反例。 */
async function scheduledSendBinding({ projectRoot, event }) {
  const targetDate = event?.targetDate;
  let occurrence;
  try {
    occurrence = JSON.parse((await readBounded(path.join(
      projectRoot, "work", "orchestrator", "states", `morning-report-${targetDate}.json`
    ), MAX_OCCURRENCE_BYTES)).toString("utf8"));
  } catch {
    return { ok: false, reason: "occurrence_missing" };
  }
  if (
    !occurrence || typeof occurrence !== "object" || Array.isArray(occurrence)
    || occurrence.task !== "morning-report"
    || occurrence.targetDate !== targetDate
    || !ORCHESTRATOR_INVOCATION_PATTERN.test(String(occurrence.invocationId ?? ""))
    || !isZonedIsoInstant(occurrence.startedAt)
  ) {
    return { ok: false, reason: "occurrence_not_claimed" };
  }
  // 成功附件必须来自成功终态；故障/跳过通知允许对应的已完成终态。
  // running 一律不能授权，失败记录使用 failedAt 而不是要求不存在的 completedAt。
  const terminalStates = event.outcome === "success" ? ["success"]
    : event.outcome === "skipped" ? ["skipped"]
      : ["success", "failed", "blocked", "unknown", "notification_pending"];
  if (!terminalStates.includes(occurrence.status) || !isZonedIsoInstant(occurrence.completedAt)) {
    return { ok: false, reason: "occurrence_not_completed" };
  }
  const occurrenceStart = Date.parse(occurrence.startedAt);
  const occurrenceEnd = Date.parse(occurrence.completedAt);
  if (!(occurrenceStart <= occurrenceEnd)) {
    return { ok: false, reason: "occurrence_window_invalid" };
  }

  let runRaw;
  try {
    runRaw = await readBounded(path.join(projectRoot, "work", "last-run.json"), MAX_LAST_RUN_BYTES);
  } catch {
    return { ok: false, reason: "run_state_missing" };
  }
  let run;
  try {
    run = JSON.parse(runRaw.toString("utf8"));
  } catch {
    return { ok: false, reason: "run_state_invalid" };
  }
  if (!run || typeof run !== "object" || Array.isArray(run) || run.targetDate !== targetDate) {
    return { ok: false, reason: "run_date_mismatch" };
  }
  // v2 separates the configured schedule cutoff from the actual start time.
  // Only v1 records without the new field retain their sealed legacy meaning;
  // a missing/invalid v2 cutoff must never fall back to startedAt.
  let claimedCutoff;
  if (occurrence.schemaVersion === "2.0") {
    claimedCutoff = occurrence.frozenCutoffInstant;
    // v2 is the fixed Asia/Shanghai 08:00 scheduled contract. Do not consult
    // today's workflow configuration to reinterpret an already claimed run.
    if (claimedCutoff !== `${targetDate}T00:00:00.000Z`
        || !isZonedIsoInstant(claimedCutoff)
        || Date.parse(claimedCutoff) > occurrenceStart
        || dateInTimeZone("Asia/Shanghai", new Date(claimedCutoff)) !== targetDate) {
      return { ok: false, reason: "occurrence_cutoff_invalid" };
    }
  } else if (occurrence.schemaVersion === "1.0" && !Object.hasOwn(occurrence, "frozenCutoffInstant")) {
    claimedCutoff = occurrence.startedAt;
  } else {
    return { ok: false, reason: "occurrence_version_invalid" };
  }
  if (run.frozenCutoffInstant !== claimedCutoff) {
    return { ok: false, reason: "run_not_from_claimed_occurrence" };
  }
  // 只有时间相等还不够：后来的人工恢复可以沿用同一个冻结截止时间再产出新正文。
  // 该运行必须开始于认领时刻之后、且完成于认领运行结束之前，才算出自这次运行。
  const runTerminal = run.status === "failed" ? run.failedAt : run.completedAt;
  if (!isZonedIsoInstant(run.startedAt) || !isZonedIsoInstant(runTerminal)) {
    return { ok: false, reason: "run_window_missing" };
  }
  const runStart = Date.parse(run.startedAt);
  const runEnd = Date.parse(runTerminal);
  if (!(runStart <= runEnd)) return { ok: false, reason: "run_window_invalid" };
  if (runStart < occurrenceStart || runEnd > occurrenceEnd) {
    return { ok: false, reason: "run_outside_claimed_window" };
  }

  if (event.outcome !== "success") {
    // 失败/跳过通知没有密封正文可绑定，因此只要求「目标日期 + 同一次运行 + 时间窗」。
    // 通知的成败由调用方的 exit code 分类，运行文件与它不一致不代表有人借用了认领记录。
    return { ok: true, reason: "scheduled_run_notice" };
  }

  return successfulSendProof({ projectRoot, event, run, runRaw });
}



// Explicit recovery authorization replaces the occurrence claim only. It must
// never replace proof that the successful attachment is still the sealed one.
async function successfulSendProof({ projectRoot, event, run, runRaw }) {
  const targetDate = event.targetDate;
  if (!runRaw) {
    try {
      runRaw = await readBounded(path.join(projectRoot, "work", "last-run.json"), MAX_LAST_RUN_BYTES);
      run = JSON.parse(runRaw.toString("utf8"));
    } catch {
      return { ok: false, reason: "run_state_invalid" };
    }
  }

  // 成功待发正文：该运行必须已完成密封与发布门禁。
  if (
    !run || run.targetDate !== targetDate || run.status !== "success" || run.phase !== "publish"
    || run.publicationGate !== "passed" || run.evidenceReview !== "passed"
    || !Array.isArray(run.publicationBlockers) || run.publicationBlockers.length > 0
    || (run.dataStatus !== "live" && run.dataStatus !== "partial")
  ) {
    return { ok: false, reason: "run_not_published" };
  }
  // 结果版事件的正文是投影：报告哈希在 sourceSha256，payload 哈希在 sha256。
  const reportSha = typeof event.report?.sourceSha256 === "string"
    ? event.report.sourceSha256
    : event.report?.sha256;
  if (typeof reportSha !== "string" || !/^[a-f0-9]{64}$/.test(reportSha)) {
    return { ok: false, reason: "report_hash_missing" };
  }
  const found = await readSealedReport(projectRoot, targetDate, reportSha);
  if (found.present && found.invalid) return { ok: false, reason: found.invalid };
  if (!found.present) return { ok: false, reason: "seal_missing" };
  try {
    const reportBody = await readFormalReport(projectRoot, targetDate, run.reportFile);
    if (sha256(reportBody) !== reportSha) throw new Error("report_changed");
    validateMorningSealProof({ seal: found.seal, proofKind: found.kind, targetDate,
      reportFile: run.reportFile, reportSha256: reportSha, lastRunSha256: sha256(runRaw), lastRun: run });
  } catch {
    return { ok: false, reason: "seal_does_not_bind_this_run" };
  }
  return { ok: true, reason: "scheduled_run_sealed" };
}



function sendAuthorizationPath(paths, eventId) {
  return path.join(paths.authorizations, `${eventId}.json`);
}



function validateSendAuthorization(record, expected = {}) {
  if (!record || typeof record !== "object" || Array.isArray(record)) return null;
  if (Object.keys(record).some((key) => !SEND_AUTHORIZATION_KEYS.has(key))) return null;
  if (record.schemaVersion !== "1.0") return null;
  if (typeof record.eventId !== "string" || record.eventId.trim() !== record.eventId) return null;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(record.targetDate ?? "")) return null;
  if (record.route !== "telegram") return null;
  if (typeof record.payloadSha256 !== "string" || !/^[a-f0-9]{64}$/.test(record.payloadSha256)) return null;
  if (!Number.isSafeInteger(record.payloadBytes) || record.payloadBytes <= 0) return null;
  if (!isZonedIsoInstant(record.grantedAt)) return null;
  if (record.consumedAt !== null && !isZonedIsoInstant(record.consumedAt)) return null;
  if (expected.eventId !== undefined && record.eventId !== expected.eventId) return null;
  if (expected.targetDate !== undefined && record.targetDate !== expected.targetDate) return null;
  if (expected.payloadSha256 !== undefined && record.payloadSha256 !== expected.payloadSha256) return null;
  return record;
}



/* 只读：缺失或无效一律按“没有授权”处理，不产生副作用。 */
async function readSendAuthorization(paths, eventId, expected = {}) {
  try {
    const raw = await readBounded(sendAuthorizationPath(paths, eventId), MAX_AUTHORIZATION_BYTES);
    return validateSendAuthorization(JSON.parse(raw.toString("utf8")), { eventId, ...expected });
  } catch {
    return null;
  }
}



/* 所有投递入口（emit+drain、drain、deliver-morning-report）都经这里判定。 */
async function sendIsAuthorized({ projectRoot, paths, event, delivery }) {
  if (event.deliveryAuthorization === DELIVERY_AUTHORIZATION_SCHEDULED) {
    // 标记可能来自被改写的队列文件，因此按「运行 → 密封 → 待发正文」重新推导。
    const binding = await scheduledSendBinding({ projectRoot, event });
    if (binding.ok) return { allowed: true, via: "scheduled", record: null };
    return { allowed: false, via: null, record: null, reason: binding.reason };
  }
  const identity = deliveryIdentity(event, delivery);
  const record = await readSendAuthorization(paths, event.eventId, {
    targetDate: event.targetDate,
    payloadSha256: identity.sha256
  });
  if (!record || record.consumedAt !== null) {
    // 附上绑定失败原因：运维需要区分「人工恢复本来就要授权」与「产物不匹配」。
    const binding = await scheduledSendBinding({ projectRoot, event });
    return {
      allowed: false, via: null, record: null,
      reason: binding.ok ? "authorization_required" : binding.reason
    };
  }
  if (event.outcome === "success") {
    const proof = await successfulSendProof({ projectRoot, event });
    if (!proof.ok) return { allowed: false, via: null, record: null, reason: proof.reason };
  }
  return { allowed: true, via: "authorization", record };
}



function validateEvent(event) {
  if (!event || typeof event !== "object" || Array.isArray(event)) throw new Error("事件不是对象");
  if (event.schemaVersion !== "1.0" || event.source !== "AStock_AI_System") {
    throw new Error("事件来源或版本无效");
  }
  if (event.route !== "telegram") throw new Error("事件路由不在白名单");
  if (
    Object.hasOwn(event, "deliveryAuthorization")
    && event.deliveryAuthorization !== DELIVERY_AUTHORIZATION_SCHEDULED
    && event.deliveryAuthorization !== DELIVERY_AUTHORIZATION_AWAITING
  ) {
    throw new Error("事件发送授权标记无效");
  }
  const expectedId = notificationEventId(event.targetDate, event.outcome);
  if (event.eventId !== expectedId) throw new Error("事件 ID 与内容不匹配");
  if (event.outcome === "success") {
    const report = event.report;
    if (!report || report.payloadFile !== `payloads/${event.eventId}.md`) {
      throw new Error("success 事件缺少固定 payload");
    }
    if (!/^[a-f0-9]{64}$/.test(report.sha256 ?? "")) throw new Error("payload hash 无效");
    if (!Number.isInteger(report.bytes) || report.bytes <= 0 || report.bytes > MAX_REPORT_BYTES) {
      throw new Error("payload 大小无效");
    }
    if (Object.hasOwn(report, "presentation")) {
      if (!isResultPresentation(report.presentation) ||
          report.reportFile !== `reports/daily/${event.targetDate}.md` ||
          !/^[a-f0-9]{64}$/.test(report.sourceSha256 ?? "") ||
          !Number.isInteger(report.sourceBytes) || report.sourceBytes <= 0 || report.sourceBytes > MAX_REPORT_BYTES) {
        throw new Error("结果视图来源契约无效");
      }
    } else if (Object.hasOwn(report, "sourceSha256") || Object.hasOwn(report, "sourceBytes")) {
      throw new Error("完整报告不得伪装结果视图来源");
    }
  } else if (event.report !== null) {
    throw new Error("非 success 事件不得附带报告");
  }
  return event;
}



async function validatePayload(projectRoot, paths, event) {
  if (event.outcome !== "success") return null;
  const expectedPath = path.join(paths.payloads, `${event.eventId}.md`);
  const canonicalRoot = await realpath(paths.payloads);
  const canonicalPayload = await realpath(expectedPath);
  const relative = path.relative(canonicalRoot, canonicalPayload);
  if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("payload 越出固定目录");
  const body = await readBounded(canonicalPayload, MAX_REPORT_BYTES);
  const sha256 = createHash("sha256").update(body).digest("hex");
  if (sha256 !== event.report.sha256 || body.length !== event.report.bytes) {
    throw new Error("payload 与事件 hash/size 不匹配");
  }
  if (isResultPresentation(event.report.presentation)) {
    const sourceBody = await readFormalReport(projectRoot, event.targetDate, event.report.reportFile);
    if (createHash("sha256").update(sourceBody).digest("hex") !== event.report.sourceSha256 ||
        sourceBody.length !== event.report.sourceBytes ||
        !body.equals(resultViewPayload(sourceBody, event.targetDate, event.report.presentation))) {
      throw new Error("结果视图与正式报告来源或确定性投影不匹配");
    }
  }
  return canonicalPayload;
}



/* 确认发送＝事件、附件、回执三者对上，而不是“文件在 sent 目录里”。
   历史记录没有回执时返回 confirmed:false（不重发、不补造），由调用方显示
   “已发送记录存在，确认依据不足”。 */
async function sentConfirmation(projectRoot = PROJECT_ROOT, eventId) {
  const paths = layout(projectRoot);
  let event;
  try {
    event = validateEvent(JSON.parse(await readBounded(path.join(paths.sent, `${eventId}.json`), MAX_EVENT_BYTES)));
  } catch {
    return { confirmed: false, reason: "sent_event_unreadable", receipt: null };
  }
  if (event.eventId !== eventId) {
    return { confirmed: false, reason: "sent_event_mismatch", receipt: null };
  }
  try {
    await validatePayload(projectRoot, paths, event);
  } catch {
    return { confirmed: false, reason: "sent_payload_mismatch", receipt: null };
  }
  const receipt = await readSendReceipt(paths, eventId);
  if (!receipt) return { confirmed: false, reason: "send_receipt_missing", receipt: null };
  if (receipt.targetDate !== event.targetDate) {
    return { confirmed: false, reason: "send_receipt_target_mismatch", receipt };
  }
  if (event.outcome === "success" && event.report) {
    if (receipt.payloadSha256 !== event.report.sha256
        || receipt.payloadBytes !== event.report.bytes) {
      return { confirmed: false, reason: "send_receipt_payload_mismatch", receipt };
    }
    if (typeof event.report.sourceSha256 === "string" && receipt.sourceSha256 !== event.report.sourceSha256) {
      return { confirmed: false, reason: "send_receipt_source_missing_or_mismatch", receipt };
    }
  }
  return { confirmed: true, reason: null, receipt };
}



async function runSentIsConfirmed(projectRoot, paths, eventId) {
  return (await sentConfirmation(projectRoot, eventId)).confirmed;
}

export { readFormalReport, resultViewPayload, scheduledSendBinding, sendAuthorizationPath, readSendAuthorization, sendIsAuthorized, validateEvent, validatePayload, sentConfirmation, runSentIsConfirmed };
