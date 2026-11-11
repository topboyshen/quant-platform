// Daily event publication and targeted queue lifecycle; all sends repeat proof checks under the shared lock.
import path from "node:path";
import { createHash } from "node:crypto";
import { readdir, stat } from "node:fs/promises";
import { buildHermesDelivery, classifyRunOutcome, notificationEventId } from "./run-notification.mjs";
import { isResultPresentation } from "./report-presentation.mjs";
import { validateExpectedTargetDate, validateTargetRunEventId, sha256, validateDeliveryMode, unauthorizedDeliverResult, deliveryIdentity, PROJECT_ROOT, MAX_EVENT_BYTES, MAX_REPORT_BYTES, SENDING_STALE_MS, EVENT_STATES, RUN_EVENT_FILE_PATTERN, DELIVERY_AUTHORIZATION_SCHEDULED, DELIVERY_AUTHORIZATION_AWAITING } from "./notification-contract.mjs";
import { ensureLayout, fileExists, readBounded, readLastRun, loadDeliveryPolicy, deliveryIsAuthorized, publishOnce, eventAlreadyExists, eventStates, writeAtomicJson, moveEvent, acquireDrainLock } from "./notification-store.mjs";
import { readFormalReport, resultViewPayload, scheduledSendBinding, sendAuthorizationPath, sendIsAuthorized, validateEvent, validatePayload, runSentIsConfirmed } from "./notification-proof.mjs";
import { sendReceiptPath, publishSendReceipt, readSendReceipt } from "./notification-receipts.mjs";
import { sendViaHermes, transportDiagnostic } from "./notification-transport.mjs";
import { settleSenderCall } from "./notification-attempt.mjs";

async function snapshotFormalReport(projectRoot, targetDate, reportFile, paths, eventId, reportView = "full") {
  const sourceBody = await readFormalReport(projectRoot, targetDate, reportFile);
  const body = isResultPresentation(reportView) ? resultViewPayload(sourceBody, targetDate, reportView) : sourceBody;
  const digest = sha256(body);
  const payloadPath = path.join(paths.payloads, `${eventId}.md`);
  await publishOnce(payloadPath, body);
  const payload = await readBounded(payloadPath, MAX_REPORT_BYTES);
  const payloadSha256 = createHash("sha256").update(payload).digest("hex");
  if (payloadSha256 !== digest || payload.length !== body.length) {
    throw new Error("不可变报告快照校验失败");
  }
  return {
    reportFile,
    payloadFile: `payloads/${eventId}.md`,
    sha256: digest,
    bytes: body.length,
    ...(isResultPresentation(reportView) ? {
      presentation: reportView,
      sourceSha256: sha256(sourceBody),
      sourceBytes: sourceBody.length
    } : {})
  };
}



// Queue publication and consumption share the same lock. Checking all states
// outside this lock allowed a late emitter to recreate pending after sent.
async function emitRunEvent(options = {}) {
  const projectRoot = options.projectRoot ?? PROJECT_ROOT;
  const targetDate = validateExpectedTargetDate(options.expectedTargetDate);
  const paths = await ensureLayout(projectRoot);
  const release = await acquireDrainLock(paths);
  if (!release) {
    const outcome = classifyRunOutcome({ ...options, expectedTargetDate: targetDate,
      invocationEndedAt: options.invocationEndedAt ?? new Date().toISOString(),
      lastRun: await readLastRun(projectRoot) });
    const eventId = notificationEventId(targetDate, outcome.outcome);
    return { created: false, eventId, state: await eventAlreadyExists(paths, eventId), busy: true };
  }
  try {
    return await emitRunEventUnlocked(options);
  } finally {
    await release();
  }
}



async function emitRunEventUnlocked({
  projectRoot = PROJECT_ROOT,
  exitCode,
  invocationStartedAt,
  invocationEndedAt = new Date().toISOString(),
  expectedTargetDate
}) {
  const targetDate = validateExpectedTargetDate(expectedTargetDate);
  const paths = await ensureLayout(projectRoot);
  const lastRun = await readLastRun(projectRoot);
  let classification = classifyRunOutcome({
    exitCode,
    invocationStartedAt,
    invocationEndedAt,
    expectedTargetDate: targetDate,
    lastRun
  });
  let eventId = notificationEventId(targetDate, classification.outcome);
  let existingState = await eventAlreadyExists(paths, eventId);
  if (existingState) return { created: false, eventId, state: existingState };

  let report = null;
  if (classification.outcome === "success") {
    const reportView = (await loadDeliveryPolicy(projectRoot)).reportView ?? "full";
    try {
      report = await snapshotFormalReport(
        projectRoot,
        targetDate,
        classification.reportFile,
        paths,
        eventId,
        reportView
      );
    } catch (error) {
      // A result-view failure must not fall back to a full document or turn
      // into a sendable failure event. Keep the source and event untouched.
      if (isResultPresentation(reportView)) throw error;
      classification = {
        outcome: "failed",
        reasonCode: "report_snapshot_invalid",
        workflowExitCode: exitCode
      };
      eventId = notificationEventId(targetDate, "failed");
      existingState = await eventAlreadyExists(paths, eventId);
      if (existingState) return { created: false, eventId, state: existingState };
    }
  }

  const event = {
    schemaVersion: "1.0",
    eventId,
    source: "AStock_AI_System",
    route: "telegram",
    targetDate,
    outcome: classification.outcome,
    reasonCode: classification.reasonCode,
    workflowExitCode: classification.workflowExitCode,
    occurredAt: invocationEndedAt,
    dataStatus: classification.dataStatus ?? null,
    report
  };
  // 标记反映真实绑定结果：只有能证明「这次运行产出了这份待发正文」才可自动发送，
  // 否则只留档等待一次性二次授权。
  const binding = await scheduledSendBinding({ projectRoot, event });
  event.deliveryAuthorization = binding.ok
    ? DELIVERY_AUTHORIZATION_SCHEDULED
    : DELIVERY_AUTHORIZATION_AWAITING;
  const eventPath = path.join(paths.pending, `${eventId}.json`);
  const created = await publishOnce(eventPath, `${JSON.stringify(event, null, 2)}\n`);
  return { created, eventId, state: created ? "pending" : "pending" };
}




async function recordAmbiguous(paths, sendingPath, eventId, fields, { failSidecar = false } = {}) {
  await moveEvent(sendingPath, paths.ambiguous);
  const diagnosticPath = path.join(paths.ambiguous, `${eventId}.transport.json`);
  if (await fileExists(diagnosticPath)) return { diagnosticDegraded: true, preserved: true };
  try {
    if (failSidecar) throw new Error("injected_sidecar_crash");
    await writeAtomicJson(diagnosticPath, transportDiagnostic(eventId, fields));
    return { diagnosticDegraded: false, preserved: false };
  } catch {
    return { diagnosticDegraded: true, preserved: false };
  }
}

function diagnosticFields(result, receiptRejectReason) {
  return {
    started: result?.started === true,
    exitCode: Number.isInteger(result?.exitCode) ? result.exitCode : null,
    timedOut: result?.timedOut === true,
    stdoutBytes: Number.isSafeInteger(result?.stdoutBytes) && result.stdoutBytes >= 0 ? result.stdoutBytes : 0,
    stderrBytes: Number.isSafeInteger(result?.stderrBytes) && result.stderrBytes >= 0 ? result.stderrBytes : 0,
    receiptRejectReason
  };
}

async function recoverStaleSending(paths, now = Date.now(), targetEventId) {
  const names = targetEventId === undefined
    ? await readdir(paths.sending)
    : [`${targetEventId}.json`];
  let recovered = 0;
  for (const name of names.filter((value) => RUN_EVENT_FILE_PATTERN.test(value))) {
    const filePath = path.join(paths.sending, name);
    // Never overwrite a terminal duplicate while repairing a stale transition.
    if ((await eventStates(paths, name.slice(0, -5))).length !== 1) continue;
    const info = await stat(filePath).catch(() => null);
    if (info && now - info.mtimeMs > SENDING_STALE_MS) {
      try {
        await recordAmbiguous(paths, filePath, name.slice(0, -5), diagnosticFields(null, "stale_sending_no_spawn_confirmation"));
        recovered += 1;
      } catch {
        // Another recovery/drain process won the race.
      }
    }
  }
  return recovered;
}

function noteUnresolved(count) {
  const unresolved = Number.isSafeInteger(count) && count > 0 ? count : 0;
  return { sent: 0, ambiguous: 0, deferred: unresolved, unresolvedSending: unresolved };
}

async function receiptBindingForSending(projectRoot, paths, eventId) {
  let event;
  try {
    const raw = await readBounded(path.join(paths.sending, `${eventId}.json`), MAX_EVENT_BYTES);
    event = validateEvent(JSON.parse(raw.toString("utf8")));
    if (event.eventId !== eventId || event.route !== "telegram") return null;
  } catch {
    return null;
  }
  try {
    const payloadPath = event.outcome === "success"
      ? await validatePayload(projectRoot, paths, event)
      : null;
    const identity = deliveryIdentity(event, buildHermesDelivery(event, payloadPath));
    if (!/^[0-9a-f]{64}$/.test(identity?.sha256 ?? "") || !Number.isSafeInteger(identity.bytes) || identity.bytes < 0) {
      return null;
    }
    const sourceSha256 = typeof event.report?.sourceSha256 === "string" ? event.report.sourceSha256 : null;
    return {
      eventId,
      targetDate: event.targetDate,
      route: event.route,
      payloadSha256: identity.sha256,
      payloadBytes: identity.bytes,
      sourceSha256,
      ackKind: "hermes_cli_json_message_id"
    };
  } catch {
    return null;
  }
}

async function settleTargetedSending({ projectRoot, paths, eventId, now }) {
  const binding = await receiptBindingForSending(projectRoot, paths, eventId);
  const verifiedReceipt = binding ? await readSendReceipt(paths, eventId, binding) : null;
  if (verifiedReceipt) {
    try {
      await moveEvent(path.join(paths.sending, `${eventId}.json`), paths.sent);
      return { sent: 1, ambiguous: 0, deferred: 0, unresolvedSending: 0 };
    } catch {
      return noteUnresolved(1);
    }
  }
  if (await fileExists(sendReceiptPath(paths, eventId))) return noteUnresolved(1);
  const recovered = await recoverStaleSending(paths, now, eventId);
  if (recovered > 0) return { sent: 0, ambiguous: recovered, deferred: 0, unresolvedSending: 0 };
  return noteUnresolved(1);
}

async function drainOutbox(options = {}) {
  if (!options || typeof options !== "object" || Array.isArray(options)) {
    throw new Error("drain options 无效");
  }
  const allowedOptions = new Set(["projectRoot", "sender", "now", "receiptClock", "deliveryMode", "targetEventId", "faultInjection"]);
  const unexpected = Object.keys(options).filter((key) => !allowedOptions.has(key));
  if (unexpected.length > 0) throw new Error("drain options 包含未允许字段");
  const {
    projectRoot = PROJECT_ROOT,
    sender = sendViaHermes,
    now = Date.now(),
    receiptClock = Date.now,
    deliveryMode = "disabled",
    targetEventId,
    faultInjection = null
  } = options;
  // Validate before any policy read, directory creation or queue transition.
  if (typeof receiptClock !== "function") throw new Error("receiptClock 必须是函数");
  if ("targetEventId" in options) validateTargetRunEventId(targetEventId);
  const mode = validateDeliveryMode(deliveryMode);
  const policy = await loadDeliveryPolicy(projectRoot);
  const paths = await ensureLayout(projectRoot);
  // Delivery requires both the validated persistent owner policy and an
  // explicit per-invocation Telegram mode. Programmatic callers cannot
  // override either gate.
  const enabled = mode === "telegram" && policy.deliveryEnabled === true;
  if (!enabled) {
    const held = targetEventId === undefined
      ? (await readdir(paths.pending)).filter((name) => name.endsWith(".json")).length
      : Number(await fileExists(path.join(paths.pending, `${targetEventId}.json`)));
    return {
      enabled: false,
      held,
      busy: false,
      sent: 0,
      ambiguous: 0,
      dead: 0,
      deferred: 0,
      awaitingAuthorization: 0
    };
  }
  const release = await acquireDrainLock(paths, now);
  if (!release) {
    return { enabled: true, busy: true, sent: 0, ambiguous: 0, dead: 0, deferred: 0,
             awaitingAuthorization: 0 };
  }

  const summary = { enabled: true, busy: false, sent: 0, ambiguous: 0, dead: 0, deferred: 0,
                    receiptFailed: 0, awaitingAuthorization: 0, awaitingReasons: [] };
  try {
    if (targetEventId !== undefined) {
      const states = await eventStates(paths, targetEventId);
      // A terminal state or multiple copies must never be replayed from pending.
      if (states.length !== 1) {
        if (states.length > 1) summary.deferred += 1;
        return summary;
      }
      if (["sent", "ambiguous", "dead"].includes(states[0])) return summary;
      if (states[0] === "sending") {
        const settled = await settleTargetedSending({
          projectRoot, paths, eventId: targetEventId, now
        });
        summary.sent += settled.sent;
        summary.ambiguous += settled.ambiguous;
        summary.deferred += settled.deferred;
        summary.unresolvedSending = settled.unresolvedSending;
        return summary;
      }
    }
    summary.ambiguous += await recoverStaleSending(paths, now, targetEventId);
    if (targetEventId === undefined) {
      const unresolved = (await readdir(paths.sending)).filter((name) => RUN_EVENT_FILE_PATTERN.test(name)).length;
      summary.deferred += unresolved;
      summary.unresolvedSending = unresolved;
    }
    const names = (targetEventId === undefined
      ? await readdir(paths.pending)
      : [`${targetEventId}.json`])
      .filter((name) => RUN_EVENT_FILE_PATTERN.test(name))
      .sort()
      .slice(0, 20);
    for (const name of names) {
      const pendingPath = path.join(paths.pending, name);
      const eventId = name.slice(0, -5);
      const states = await eventStates(paths, eventId);
      // This guard applies to bulk and targeted drain alike. A durable receipt
      // also prevents replay if an operator moved a sent file back to pending.
      if (states.length !== 1 || states[0] !== "pending"
          || await fileExists(sendReceiptPath(paths, eventId))) {
        summary.deferred += 1;
        continue;
      }
      let sendingPath;
      try {
        sendingPath = await moveEvent(pendingPath, paths.sending);
      } catch {
        continue;
      }

      let event;
      let payloadPath;
      try {
        const raw = await readBounded(sendingPath, MAX_EVENT_BYTES);
        event = validateEvent(JSON.parse(raw.toString("utf8")));
        if (event.eventId !== eventId) throw new Error("事件文件名与内容不匹配");
        if (targetEventId !== undefined && event.eventId !== targetEventId) {
          throw new Error("事件内容与 targetEventId 不匹配");
        }
        payloadPath = await validatePayload(projectRoot, paths, event);
      } catch {
        await moveEvent(sendingPath, paths.dead).catch(() => {});
        summary.dead += 1;
        continue;
      }

      const delivery = buildHermesDelivery(event, payloadPath);
      const authorization = await sendIsAuthorized({ projectRoot, paths, event, delivery });
      if (!authorization.allowed) {
        // 恢复生成默认只留档：事件退回 pending 等待二次授权，不发送、不计为故障。
        await moveEvent(sendingPath, paths.pending).catch(() => {});
        summary.awaitingAuthorization += 1;
        if (summary.awaitingReasons.length < 8) {
          summary.awaitingReasons.push(authorization.reason ?? "not_authorized");
        }
        continue;
      }
      let entered = false;
      let thrown = false;
      let result;
      try {
        if (faultInjection === "before_sender") throw new Error("injected_crash_before_sender");
        entered = true;
        result = await sender(delivery, { event });
      } catch (error) {
        if (!entered) throw error;
        thrown = true;
      }
      const settlement = settleSenderCall({ entered, thrown, result });
      const processExitUnproven = settlement.disposition === "ambiguous"
        && result?.processExited !== true;
      if (settlement.disposition === "acked") {
        // 回执先落盘，事件才允许进入 sent。落盘失败＝已发出但无法核实，
        // 进 ambiguous 等人工确认，绝不自动重发。
        try {
          // 确认时间取自收到平台成功回执之后，不能复用队列处理开始时刻。
          // 可注入时钟仅用于程序调用/离线测试；CLI 不提供这个选项。
          const confirmedAtMs = receiptClock();
          if (!Number.isSafeInteger(confirmedAtMs) || confirmedAtMs < 0) {
            throw new Error("发送回执确认时钟无效");
          }
          if (faultInjection === "receipt_write") throw new Error("injected_receipt_crash");
          await publishSendReceipt(paths, event, result.ack, new Date(confirmedAtMs).toISOString(),
            deliveryIdentity(event, delivery));
        } catch {
          const recorded = await recordAmbiguous(
            paths, sendingPath, event.eventId, diagnosticFields(result, "receipt_persist_failed")
          ).catch(() => ({ diagnosticDegraded: true }));
          if (recorded?.diagnosticDegraded) summary.diagnosticDegraded = (summary.diagnosticDegraded || 0) + 1;
          summary.ambiguous += 1;
          summary.receiptFailed += 1;
          continue;
        }
        if (faultInjection === "terminal_move") throw new Error("injected_terminal_move");
        await moveEvent(sendingPath, paths.sent);
        summary.sent += 1;
        if (authorization.record) {
          try {
            await writeAtomicJson(sendAuthorizationPath(paths, event.eventId),
              { ...authorization.record, consumedAt: new Date(receiptClock()).toISOString() });
          } catch {
            // 事件已进入 sent，一次有效性由终态保证；消费标记失败不回退发送结果。
          }
        }
      } else if (settlement.disposition === "pending") {
        await moveEvent(sendingPath, paths.pending).catch(() => {});
        summary.deferred += 1;
      } else {
        const recorded = await recordAmbiguous(
          paths,
          sendingPath,
          event.eventId,
          diagnosticFields(result, settlement.reason),
          { failSidecar: faultInjection === "sidecar_write" }
        ).catch(() => ({ diagnosticDegraded: true }));
        if (recorded?.diagnosticDegraded) summary.diagnosticDegraded = (summary.diagnosticDegraded || 0) + 1;
        summary.ambiguous += 1;
        if (processExitUnproven) {
          summary.haltedUnknownProcess = true;
          break;
        }
      }
    }
  } finally {
    await release();
  }
  return summary;
}



async function deliverRunEvent(options = {}) {
  if (!options || typeof options !== "object" || Array.isArray(options)) {
    throw new Error("deliver options 无效");
  }
  const allowedOptions = new Set([
    "projectRoot",
    "exitCode",
    "invocationStartedAt",
    "invocationEndedAt",
    "expectedTargetDate",
    "sender",
    "drainNow",
    "deliveryMode"
  ]);
  const unexpected = Object.keys(options).filter((key) => !allowedOptions.has(key));
  if (unexpected.length > 0) throw new Error("deliver options 包含未允许字段");
  const {
    projectRoot = PROJECT_ROOT,
    exitCode,
    invocationStartedAt,
    invocationEndedAt = new Date().toISOString(),
    expectedTargetDate,
    sender = sendViaHermes,
    drainNow = Date.now(),
    deliveryMode = "disabled"
  } = options;
  const mode = validateDeliveryMode(deliveryMode);
  if (!await deliveryIsAuthorized(projectRoot, mode)) return unauthorizedDeliverResult();
  const emitted = await emitRunEvent({
    projectRoot,
    exitCode,
    invocationStartedAt,
    invocationEndedAt,
    expectedTargetDate
  });
  const drain = await drainOutbox({
    projectRoot,
    sender,
    now: drainNow,
    deliveryMode: mode,
    targetEventId: emitted.eventId
  });
  const paths = await ensureLayout(projectRoot);
  const finalStates = await eventStates(paths, emitted.eventId);
  const finalState = finalStates[0] ?? null;
  return {
    eventId: emitted.eventId,
    created: emitted.created,
    emittedState: emitted.state,
    drain,
    finalState,
    confirmed: finalStates.length === 1 && finalState === "sent" &&
      await runSentIsConfirmed(projectRoot, paths, emitted.eventId)
  };
}



async function notificationStatus(projectRoot = PROJECT_ROOT) {
  const paths = await ensureLayout(projectRoot);
  const result = {};
  for (const state of EVENT_STATES) {
    result[state] = (await readdir(paths[state])).filter((name) => RUN_EVENT_FILE_PATTERN.test(name)).length;
  }
  let awaiting = 0;
  for (const name of (await readdir(paths.pending)).filter((value) => RUN_EVENT_FILE_PATTERN.test(value))) {
    try {
      const event = JSON.parse(
        (await readBounded(path.join(paths.pending, name), MAX_EVENT_BYTES)).toString("utf8"));
      if (event?.deliveryAuthorization === DELIVERY_AUTHORIZATION_AWAITING) awaiting += 1;
    } catch {
      // 损坏事件不计入等待授权；drain 会把它归入 dead。
    }
  }
  result.awaitingAuthorization = awaiting;
  return result;
}

export { emitRunEvent, drainOutbox, deliverRunEvent, notificationStatus };
