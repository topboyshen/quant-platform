// Explicit operator authorization and correction workflows; not part of the scheduled generation path.
import path from "node:path";
import { randomBytes } from "node:crypto";
import { link, open, realpath, stat, unlink } from "node:fs/promises";
import { buildHermesDelivery, correctionEventId, expectedCorrectionReportFile, notificationEventId } from "./run-notification.mjs";
import { validateExpectedTargetDate, validateTargetRunEventId, sha256, isIsoDateTime, validateDeliveryMode, unauthorizedDrainResult, unauthorizedDeliverResult, deliveryIdentity, PROJECT_ROOT, MAX_EVENT_BYTES, MAX_REPORT_BYTES, MAX_GATE_BYTES, MAX_CONTEXT_BYTES, MAX_RECEIPT_BYTES, SENDING_STALE_MS, DELIVERY_AUTHORIZATION_AWAITING } from "./notification-contract.mjs";
import { ensureLayout, fileExists, readBounded, loadDeliveryPolicy, deliveryIsAuthorized, publishOnce, eventStates, readFixedFile, moveEvent, acquireDrainLock } from "./notification-store.mjs";
import { sendAuthorizationPath, readSendAuthorization, validateEvent, validatePayload } from "./notification-proof.mjs";
import { sendViaHermes } from "./notification-transport.mjs";
import { settleSenderCall } from "./notification-attempt.mjs";

async function loadCorrectionGate(projectRoot, targetDate, revision) {
  const stem = `${targetDate}-${revision}`;
  const gateFile = `work/corrections/gates/${stem}.json`;
  const expectedContextFile = `work/corrections/evidence/${stem}.json`;
  const expectedReportFile = expectedCorrectionReportFile(targetDate, revision);
  const gateArtifact = await readFixedFile(
    projectRoot,
    gateFile,
    "work/corrections/gates",
    MAX_GATE_BYTES
  );
  let gate;
  try {
    gate = JSON.parse(gateArtifact.body.toString("utf8"));
  } catch {
    throw new Error("correction gate receipt 不是有效 JSON");
  }
  if (
    !gate ||
    typeof gate !== "object" ||
    Array.isArray(gate) ||
    gate.schemaVersion !== "1.0" ||
    gate.eventType !== "correction_gate" ||
    gate.passed !== true ||
    gate.targetDate !== targetDate ||
    gate.revision !== revision ||
    gate.contextFile !== expectedContextFile ||
    gate.reportFile !== expectedReportFile ||
    !isIsoDateTime(gate.evidenceCutoffIso) ||
    !isIsoDateTime(gate.contextMetaAsOf) ||
    !gate.publicationAudit ||
    typeof gate.publicationAudit !== "object" ||
    Array.isArray(gate.publicationAudit) ||
    gate.publicationAudit.passed !== true ||
    !isIsoDateTime(gate.createdAt) ||
    !/^[a-f0-9]{64}$/.test(gate.contextSha256 ?? "") ||
    !/^[a-f0-9]{64}$/.test(gate.reportSha256 ?? "") ||
    !Number.isInteger(gate.contextBytes) ||
    gate.contextBytes <= 0 ||
    gate.contextBytes > MAX_CONTEXT_BYTES ||
    !Number.isInteger(gate.reportBytes) ||
    gate.reportBytes <= 0 ||
    gate.reportBytes > MAX_REPORT_BYTES
  ) {
    throw new Error("correction gate receipt 校验失败");
  }

  const contextArtifact = await readFixedFile(
    projectRoot,
    expectedContextFile,
    "work/corrections/evidence",
    MAX_CONTEXT_BYTES
  );
  if (
    contextArtifact.body.length !== gate.contextBytes ||
    sha256(contextArtifact.body) !== gate.contextSha256
  ) {
    throw new Error("correction evidence snapshot 与 gate hash/size 不匹配");
  }
  const reportArtifact = await readFixedFile(
    projectRoot,
    expectedReportFile,
    "reports/corrections",
    MAX_REPORT_BYTES
  );
  if (
    reportArtifact.body.length !== gate.reportBytes ||
    sha256(reportArtifact.body) !== gate.reportSha256
  ) {
    throw new Error("correction report 与 gate hash/size 不匹配");
  }
  return {
    gate,
    gateFile,
    gateReceiptSha256: sha256(gateArtifact.body),
    contextBody: contextArtifact.body,
    reportBody: reportArtifact.body,
    reportPath: reportArtifact.path
  };
}



async function validateOriginalSuccess(projectRoot, paths, targetDate) {
  const eventId = notificationEventId(targetDate, "success");
  const eventPath = path.join(paths.sent, `${eventId}.json`);
  const raw = await readBounded(eventPath, MAX_EVENT_BYTES);
  let event;
  try {
    event = validateEvent(JSON.parse(raw.toString("utf8")));
  } catch {
    throw new Error("原 success 事件无效");
  }
  if (
    event.eventId !== eventId ||
    event.outcome !== "success" ||
    event.report.reportFile !== `reports/daily/${targetDate}.md`
  ) {
    throw new Error("原 success 事件与更正日期不匹配");
  }
  await validatePayload(projectRoot, paths, event);
  return { event, originalPayloadSha256: event.report.sha256 };
}



async function snapshotCorrectionReport(paths, eventId, gateResult) {
  const payloadPath = path.join(paths.payloads, `${eventId}.md`);
  await publishOnce(payloadPath, gateResult.reportBody);
  const payload = await readBounded(payloadPath, MAX_REPORT_BYTES);
  if (
    payload.length !== gateResult.gate.reportBytes ||
    sha256(payload) !== gateResult.gate.reportSha256
  ) {
    throw new Error("correction 不可变报告快照校验失败");
  }
  return {
    reportFile: gateResult.gate.reportFile,
    payloadFile: `payloads/${eventId}.md`,
    sha256: gateResult.gate.reportSha256,
    bytes: gateResult.gate.reportBytes
  };
}



async function publishAuthorizationOnce(paths, record) {
  const finalPath = sendAuthorizationPath(paths, record.eventId);
  const temporary = path.join(
    paths.authorizations,
    `.${record.eventId}.${process.pid}.${randomBytes(6).toString("hex")}.tmp`
  );
  const handle = await open(temporary, "wx", 0o600);
  try {
    await handle.writeFile(`${JSON.stringify(record, null, 2)}\n`);
    await handle.sync();
  } finally {
    await handle.close();
  }
  try {
    await link(temporary, finalPath);
    return true;
  } catch (error) {
    if (error.code === "EEXIST") return false;
    throw error;
  } finally {
    await unlink(temporary).catch(() => {});
  }
}



/* 二次授权：绑定事件 ID（含报告日期）、实际待发正文哈希与目标渠道，一次有效。
   没有通用 --force；重复授权、日期不符、正文不符都在写盘前拒绝。 */
async function authorizeSend({
  projectRoot = PROJECT_ROOT,
  eventId,
  targetDate,
  payloadSha256
}) {
  validateTargetRunEventId(eventId);
  const date = validateExpectedTargetDate(targetDate);
  const outcome = eventId.split("-").slice(-3)[0];
  if (notificationEventId(date, outcome) !== eventId) throw new Error("授权日期与事件 ID 不一致");
  if (typeof payloadSha256 !== "string" || !/^[a-f0-9]{64}$/.test(payloadSha256)) {
    throw new Error("授权正文哈希无效");
  }
  const paths = await ensureLayout(projectRoot);
  const raw = await readBounded(path.join(paths.pending, `${eventId}.json`), MAX_EVENT_BYTES);
  const event = validateEvent(JSON.parse(raw.toString("utf8")));
  if (event.targetDate !== date) throw new Error("授权日期与事件不匹配");
  if (event.deliveryAuthorization !== DELIVERY_AUTHORIZATION_AWAITING) {
    throw new Error("只对等待授权的恢复事件做二次授权");
  }
  if (await readSendAuthorization(paths, eventId)) throw new Error("该事件已有发送授权，重复授权被拒绝");
  const payloadPath = await validatePayload(projectRoot, paths, event);
  const delivery = buildHermesDelivery(event, payloadPath);
  const identity = deliveryIdentity(event, delivery);
  if (identity.sha256 !== payloadSha256) throw new Error("授权正文哈希与实际待发正文不一致");
  const record = {
    schemaVersion: "1.0",
    eventId,
    targetDate: date,
    route: "telegram",
    payloadSha256: identity.sha256,
    payloadBytes: identity.bytes,
    grantedAt: new Date().toISOString(),
    consumedAt: null
  };
  if (!await publishAuthorizationOnce(paths, record)) {
    throw new Error("该事件已有发送授权，重复授权被拒绝");
  }
  return { eventId, targetDate: date, route: "telegram",
           payloadSha256: identity.sha256, granted: true };
}



async function emitCorrectionEvent(options = {}) {
  const projectRoot = options.projectRoot ?? PROJECT_ROOT;
  const eventId = correctionEventId(options.targetDate, options.revision);
  const paths = await ensureLayout(projectRoot);
  const release = await acquireDrainLock(paths);
  if (!release) {
    const states = await eventStates(paths, eventId);
    return { created: false, eventId, state: states[0] ?? null, states, busy: true };
  }
  try {
    return await emitCorrectionEventUnlocked(options);
  } finally {
    await release();
  }
}



async function emitCorrectionEventUnlocked({
  projectRoot = PROJECT_ROOT,
  targetDate,
  revision,
  occurredAt = new Date().toISOString()
}) {
  const eventId = correctionEventId(targetDate, revision);
  if (!isIsoDateTime(occurredAt)) throw new Error("correction occurredAt 无效");
  const paths = await ensureLayout(projectRoot);
  const existingStates = await eventStates(paths, eventId);
  if (existingStates.length > 0) {
    return { created: false, eventId, state: existingStates[0], states: existingStates };
  }

  const original = await validateOriginalSuccess(projectRoot, paths, targetDate);
  const gateResult = await loadCorrectionGate(projectRoot, targetDate, revision);
  const report = await snapshotCorrectionReport(paths, eventId, gateResult);
  const event = {
    schemaVersion: "1.0",
    eventId,
    eventType: "correction",
    source: "AStock_AI_System",
    route: "telegram",
    targetDate,
    revision,
    occurredAt,
    supersedesEventId: original.event.eventId,
    originalPayloadSha256: original.originalPayloadSha256,
    gateReceiptFile: gateResult.gateFile,
    gateReceiptSha256: gateResult.gateReceiptSha256,
    contextFile: gateResult.gate.contextFile,
    contextSha256: gateResult.gate.contextSha256,
    contextBytes: gateResult.gate.contextBytes,
    report
  };
  const eventPath = path.join(paths.pending, `${eventId}.json`);
  const created = await publishOnce(eventPath, `${JSON.stringify(event, null, 2)}\n`);
  if (!created) {
    const states = await eventStates(paths, eventId);
    return { created: false, eventId, state: states[0] ?? null, states };
  }
  return { created: true, eventId, state: "pending", states: ["pending"] };
}



function validateCorrectionEvent(event) {
  if (!event || typeof event !== "object" || Array.isArray(event)) {
    throw new Error("correction 事件不是对象");
  }
  const expectedId = correctionEventId(event.targetDate, event.revision);
  const expectedReport = expectedCorrectionReportFile(event.targetDate, event.revision);
  const expectedOriginal = notificationEventId(event.targetDate, "success");
  const expectedGate = `work/corrections/gates/${event.targetDate}-${event.revision}.json`;
  const expectedContext = `work/corrections/evidence/${event.targetDate}-${event.revision}.json`;
  if (
    event.schemaVersion !== "1.0" ||
    event.eventType !== "correction" ||
    event.source !== "AStock_AI_System" ||
    event.route !== "telegram" ||
    event.eventId !== expectedId ||
    event.supersedesEventId !== expectedOriginal ||
    event.gateReceiptFile !== expectedGate ||
    event.contextFile !== expectedContext ||
    !isIsoDateTime(event.occurredAt) ||
    !/^[a-f0-9]{64}$/.test(event.originalPayloadSha256 ?? "") ||
    !/^[a-f0-9]{64}$/.test(event.gateReceiptSha256 ?? "") ||
    !/^[a-f0-9]{64}$/.test(event.contextSha256 ?? "") ||
    !Number.isInteger(event.contextBytes) ||
    event.contextBytes <= 0 ||
    event.contextBytes > MAX_CONTEXT_BYTES ||
    !event.report ||
    event.report.reportFile !== expectedReport ||
    event.report.payloadFile !== `payloads/${expectedId}.md` ||
    !/^[a-f0-9]{64}$/.test(event.report.sha256 ?? "") ||
    !Number.isInteger(event.report.bytes) ||
    event.report.bytes <= 0 ||
    event.report.bytes > MAX_REPORT_BYTES
  ) {
    throw new Error("correction 事件校验失败");
  }
  return event;
}



async function readValidatedCorrection(paths, eventPath) {
  const raw = await readBounded(eventPath, MAX_EVENT_BYTES);
  let event;
  try {
    event = validateCorrectionEvent(JSON.parse(raw.toString("utf8")));
  } catch {
    throw new Error("correction 事件 JSON 无效");
  }
  const expectedPath = path.join(paths.payloads, `${event.eventId}.md`);
  const canonicalRoot = await realpath(paths.payloads);
  const canonicalPayload = await realpath(expectedPath);
  const relative = path.relative(canonicalRoot, canonicalPayload);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error("correction payload 越出固定目录");
  }
  const payload = await readBounded(canonicalPayload, MAX_REPORT_BYTES);
  if (payload.length !== event.report.bytes || sha256(payload) !== event.report.sha256) {
    throw new Error("correction payload 与事件 hash/size 不匹配");
  }
  return {
    event,
    raw,
    eventSha256: sha256(raw),
    payloadPath: canonicalPayload,
    payload
  };
}



async function validateCorrectionPrerequisites(projectRoot, paths, validated) {
  const { event } = validated;
  const original = await validateOriginalSuccess(projectRoot, paths, event.targetDate);
  const gateResult = await loadCorrectionGate(projectRoot, event.targetDate, event.revision);
  if (
    original.event.eventId !== event.supersedesEventId ||
    original.originalPayloadSha256 !== event.originalPayloadSha256 ||
    gateResult.gateReceiptSha256 !== event.gateReceiptSha256 ||
    gateResult.gate.contextFile !== event.contextFile ||
    gateResult.gate.contextSha256 !== event.contextSha256 ||
    gateResult.gate.contextBytes !== event.contextBytes ||
    gateResult.gate.reportFile !== event.report.reportFile ||
    gateResult.gate.reportSha256 !== event.report.sha256 ||
    gateResult.gate.reportBytes !== event.report.bytes
  ) {
    throw new Error("correction 事件与原事件或 gate receipt 不匹配");
  }
  return gateResult;
}



function receiptPath(paths, eventId) {
  return path.join(paths.receipts, `${eventId}.json`);
}



async function validateCorrectionReceipt(paths, validated) {
  const raw = await readBounded(receiptPath(paths, validated.event.eventId), MAX_RECEIPT_BYTES);
  let receipt;
  try {
    receipt = JSON.parse(raw.toString("utf8"));
  } catch {
    throw new Error("correction receipt 不是有效 JSON");
  }
  const allowedKeys = new Set([
    "schemaVersion",
    "eventId",
    "eventSha256",
    "payloadSha256",
    "payloadBytes",
    "hermesExitCode",
    "confirmedAt",
    "ackKind"
  ]);
  if (
    !receipt ||
    typeof receipt !== "object" ||
    Array.isArray(receipt) ||
    Object.keys(receipt).some((key) => !allowedKeys.has(key)) ||
    receipt.schemaVersion !== "1.0" ||
    receipt.eventId !== validated.event.eventId ||
    receipt.eventSha256 !== validated.eventSha256 ||
    receipt.payloadSha256 !== validated.event.report.sha256 ||
    receipt.payloadBytes !== validated.event.report.bytes ||
    receipt.hermesExitCode !== 0 ||
    receipt.ackKind !== "hermes_cli_exit_0" ||
    !isIsoDateTime(receipt.confirmedAt)
  ) {
    throw new Error("correction receipt 校验失败");
  }
  return receipt;
}



async function publishCorrectionReceipt(paths, validated, confirmedAt) {
  const receipt = {
    schemaVersion: "1.0",
    eventId: validated.event.eventId,
    eventSha256: validated.eventSha256,
    payloadSha256: validated.event.report.sha256,
    payloadBytes: validated.event.report.bytes,
    hermesExitCode: 0,
    confirmedAt,
    ackKind: "hermes_cli_exit_0"
  };
  await publishOnce(
    receiptPath(paths, validated.event.eventId),
    `${JSON.stringify(receipt, null, 2)}\n`
  );
  return validateCorrectionReceipt(paths, validated);
}



async function drainCorrectionEvent(options = {}) {
  if (!options || typeof options !== "object" || Array.isArray(options)) {
    throw new Error("correction drain options 无效");
  }
  const allowedOptions = new Set([
    "projectRoot",
    "targetDate",
    "revision",
    "sender",
    "now",
    "deliveryMode"
  ]);
  const unexpected = Object.keys(options).filter((key) => !allowedOptions.has(key));
  if (unexpected.length > 0) throw new Error("correction drain options 包含未允许字段");
  const {
    projectRoot = PROJECT_ROOT,
    targetDate,
    revision,
    sender = sendViaHermes,
    now = Date.now(),
    deliveryMode = "disabled"
  } = options;
  const mode = validateDeliveryMode(deliveryMode);
  const eventId = correctionEventId(targetDate, revision);
  const currentTime = now instanceof Date ? now.getTime() : now;
  if (!Number.isFinite(currentTime)) throw new Error("correction drain now 无效");
  const policy = await loadDeliveryPolicy(projectRoot);
  const paths = await ensureLayout(projectRoot);
  if (mode !== "telegram" || !policy.deliveryEnabled) {
    return {
      enabled: false,
      held: (await eventStates(paths, eventId)).includes("pending") ? 1 : 0,
      busy: false,
      sent: 0,
      ambiguous: 0,
      dead: 0,
      deferred: 0,
      recovered: 0
    };
  }
  const release = await acquireDrainLock(paths, currentTime);
  if (!release) {
    return {
      enabled: true,
      busy: true,
      sent: 0,
      ambiguous: 0,
      dead: 0,
      deferred: 0,
      recovered: 0
    };
  }

  const summary = {
    enabled: true,
    busy: false,
    sent: 0,
    ambiguous: 0,
    dead: 0,
    deferred: 0,
    recovered: 0
  };
  try {
    const states = await eventStates(paths, eventId);
    if (states.length !== 1) {
      if (states.length > 1) summary.deferred += 1;
      return summary;
    }
    const state = states[0];
    if (state === "sent" || state === "ambiguous" || state === "dead") return summary;
    if (state === "sending") {
      const sendingPath = path.join(paths.sending, `${eventId}.json`);
      const info = await stat(sendingPath).catch(() => null);
      if (!info || currentTime - info.mtimeMs <= SENDING_STALE_MS) {
        summary.deferred += 1;
        return summary;
      }
      let validated;
      try {
        validated = await readValidatedCorrection(paths, sendingPath);
        await validateCorrectionReceipt(paths, validated);
      } catch {
        await moveEvent(sendingPath, paths.ambiguous).catch(() => {});
        summary.ambiguous += 1;
        return summary;
      }
      try {
        await moveEvent(sendingPath, paths.sent);
        summary.sent += 1;
        summary.recovered += 1;
      } catch {
        summary.deferred += 1;
      }
      return summary;
    }

    const pendingPath = path.join(paths.pending, `${eventId}.json`);
    // A correction retains its own gate/receipt format, but an existing durable
    // receipt is just as terminal as an ordinary-send receipt after rollback.
    if (await fileExists(receiptPath(paths, eventId))) {
      summary.deferred += 1;
      return summary;
    }
    let sendingPath;
    try {
      sendingPath = await moveEvent(pendingPath, paths.sending);
    } catch {
      summary.deferred += 1;
      return summary;
    }
    let validated;
    try {
      validated = await readValidatedCorrection(paths, sendingPath);
      await validateCorrectionPrerequisites(projectRoot, paths, validated);
    } catch {
      await moveEvent(sendingPath, paths.dead).catch(() => {});
      summary.dead += 1;
      return summary;
    }

    const delivery = buildHermesDelivery(validated.event, validated.payloadPath);
    let entered = false;
    let thrown = false;
    let result;
    try {
      entered = true;
      result = await sender(delivery, { event: validated.event });
    } catch {
      thrown = true;
    }
    const settlement = settleSenderCall({ entered, thrown, result });
    if (settlement.disposition === "acked") {
      try {
        await publishCorrectionReceipt(paths, validated, new Date(currentTime).toISOString());
      } catch {
        await moveEvent(sendingPath, paths.ambiguous).catch(() => {});
        summary.ambiguous += 1;
        return summary;
      }
      try {
        await moveEvent(sendingPath, paths.sent);
        summary.sent += 1;
      } catch {
        // The durable acknowledgement is authoritative. Leave the event in
        // sending so stale recovery can validate the receipt and finish the
        // transition without ever invoking Hermes again.
        summary.deferred += 1;
      }
    } else if (settlement.disposition === "pending") {
      await moveEvent(sendingPath, paths.pending).catch(() => {});
      summary.deferred += 1;
    } else {
      await moveEvent(sendingPath, paths.ambiguous).catch(() => {});
      summary.ambiguous += 1;
    }
  } finally {
    await release();
  }
  return summary;
}



async function correctionSentIsConfirmed(projectRoot, paths, eventId) {
  try {
    const states = await eventStates(paths, eventId);
    if (states.length !== 1 || states[0] !== "sent") return false;
    const validated = await readValidatedCorrection(paths, path.join(paths.sent, `${eventId}.json`));
    await validateCorrectionReceipt(paths, validated);
    return true;
  } catch {
    return false;
  }
}



async function deliverCorrectionEvent(options = {}) {
  if (!options || typeof options !== "object" || Array.isArray(options)) {
    throw new Error("deliver correction options 无效");
  }
  const allowedOptions = new Set([
    "projectRoot",
    "targetDate",
    "revision",
    "sender",
    "occurredAt",
    "drainNow",
    "deliveryMode"
  ]);
  const unexpected = Object.keys(options).filter((key) => !allowedOptions.has(key));
  if (unexpected.length > 0) throw new Error("deliver correction options 包含未允许字段");
  const {
    projectRoot = PROJECT_ROOT,
    targetDate,
    revision,
    sender = sendViaHermes,
    occurredAt = new Date().toISOString(),
    drainNow = Date.now(),
    deliveryMode = "disabled"
  } = options;
  const mode = validateDeliveryMode(deliveryMode);
  if (!await deliveryIsAuthorized(projectRoot, mode)) {
    return {
      ...unauthorizedDeliverResult(),
      drain: unauthorizedDrainResult({ correction: true })
    };
  }
  const paths = await ensureLayout(projectRoot);
  const eventId = correctionEventId(targetDate, revision);
  const before = await eventStates(paths, eventId);
  const emitted = before.length > 0
    ? { created: false, eventId, state: before[0], states: before }
    : await emitCorrectionEvent({ projectRoot, targetDate, revision, occurredAt });
  const drain = await drainCorrectionEvent({
    projectRoot,
    targetDate,
    revision,
    sender,
    now: drainNow,
    deliveryMode: mode
  });
  const finalStates = await eventStates(paths, eventId);
  const finalState = finalStates.length === 1 ? finalStates[0] : finalStates[0] ?? null;
  return {
    eventId,
    created: emitted.created,
    emittedState: emitted.state,
    drain,
    finalState,
    confirmed: await correctionSentIsConfirmed(projectRoot, paths, eventId)
  };
}

export { authorizeSend, emitCorrectionEvent, drainCorrectionEvent, deliverCorrectionEvent };
