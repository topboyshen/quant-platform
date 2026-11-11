#!/usr/bin/env node

// Compatibility CLI and public API. Importing this entry does not run a command.
import { correctionEventId } from "../src/run-notification.mjs";
import { isMainEntry } from "../src/main-entry.mjs";
import { validateExpectedTargetDate, isStrictUtcInstant, validateDeliveryPolicy, NOTIFICATION_ROOT } from "../src/notification-contract.mjs";
import { emitRunEvent, drainOutbox, deliverRunEvent, notificationStatus } from "../src/notification-queue.mjs";
import { authorizeSend, deliverCorrectionEvent, emitCorrectionEvent, drainCorrectionEvent } from "../src/notification-recovery.mjs";
import { sentConfirmation } from "../src/notification-proof.mjs";
import { publishSendReceipt, validateSendReceipt, readSendReceipt } from "../src/notification-receipts.mjs";
import { confirmsHermesDelivery, parseHermesAck, sendViaHermes } from "../src/notification-transport.mjs";

function parseEmitArguments(args) {
  const allowed = new Set(["--exit-code", "--started-at", "--ended-at", "--target-date"]);
  const parsed = {};
  for (let index = 0; index < args.length; index += 2) {
    const key = args[index];
    const value = args[index + 1];
    if (!allowed.has(key) || value === undefined || parsed[key] !== undefined) {
      throw new Error(`无效 emit 参数: ${key ?? ""}`);
    }
    parsed[key] = value;
  }
  if (
    parsed["--exit-code"] === undefined
    || parsed["--started-at"] === undefined
    || parsed["--target-date"] === undefined
  ) {
    throw new Error("emit 需要 --exit-code、--started-at 和 --target-date");
  }
  const exitCode = Number(parsed["--exit-code"]);
  const startedAt = parsed["--started-at"];
  const endedAt = parsed["--ended-at"] ?? new Date().toISOString();
  if (!Number.isInteger(exitCode) || exitCode < 0 || exitCode > 255) throw new Error("exit code 无效");
  if (
    !isStrictUtcInstant(startedAt)
    || !isStrictUtcInstant(endedAt)
    || Date.parse(startedAt) > Date.parse(endedAt)
  ) {
    throw new Error("started/ended 时间无效");
  }
  return {
    exitCode,
    invocationStartedAt: startedAt,
    invocationEndedAt: endedAt,
    expectedTargetDate: validateExpectedTargetDate(parsed["--target-date"])
  };
}



function parseCorrectionArguments(args) {
  const allowed = new Set(["--date", "--revision"]);
  const parsed = {};
  for (let index = 0; index < args.length; index += 2) {
    const key = args[index];
    const value = args[index + 1];
    if (!allowed.has(key) || value === undefined || parsed[key] !== undefined) {
      throw new Error(`无效 deliver-correction 参数: ${key ?? ""}`);
    }
    parsed[key] = value;
  }
  if (parsed["--date"] === undefined || parsed["--revision"] === undefined) {
    throw new Error("deliver-correction 需要 --date 和 --revision");
  }
  correctionEventId(parsed["--date"], parsed["--revision"]);
  return { targetDate: parsed["--date"], revision: parsed["--revision"] };
}



async function main() {
  const [command, ...args] = process.argv.slice(2);
  const deliveryMode = process.env.ASTOCK_FINAL_DELIVERY ?? "disabled";
  if (command === "emit") {
    const result = await emitRunEvent(parseEmitArguments(args));
    process.stdout.write(`${JSON.stringify(result)}\n`);
    if (!result.created && !["pending", "sent"].includes(result.state)) process.exitCode = 1;
    return;
  }
  if (command === "drain") {
    if (args.length > 0) throw new Error("drain 不接受参数");
    const result = await drainOutbox({ deliveryMode });
    process.stdout.write(`${JSON.stringify(result)}\n`);
    if (
      result.enabled !== true ||
      result.busy === true ||
      result.ambiguous > 0 ||
      result.dead > 0 ||
      result.deferred > 0 ||
      (result.unresolvedSending || 0) > 0
    ) process.exitCode = 1;
    return;
  }
  if (command === "deliver") {
    const result = await deliverRunEvent({
      ...parseEmitArguments(args),
      deliveryMode
    });
    process.stdout.write(`${JSON.stringify(result)}\n`);
    if (!result.confirmed) process.exitCode = 1;
    return;
  }
  if (command === "deliver-correction") {
    const result = await deliverCorrectionEvent({
      ...parseCorrectionArguments(args),
      deliveryMode
    });
    process.stdout.write(`${JSON.stringify(result)}\n`);
    if (!result.confirmed) process.exitCode = 1;
    return;
  }
  if (command === "authorize") {
    if (args.length !== 6) throw new Error("authorize 需要 --event-id、--target-date 和 --payload-sha256");
    const parsed = {};
    for (let index = 0; index < args.length; index += 2) {
      const key = args[index];
      const value = args[index + 1];
      if (!["--event-id", "--target-date", "--payload-sha256"].includes(key)
          || value === undefined || parsed[key] !== undefined) {
        throw new Error(`无效 authorize 参数: ${key ?? ""}`);
      }
      parsed[key] = value;
    }
    const result = await authorizeSend({
      eventId: parsed["--event-id"],
      targetDate: parsed["--target-date"],
      payloadSha256: parsed["--payload-sha256"]
    });
    process.stdout.write(`${JSON.stringify(result)}\n`);
    return;
  }
  if (command === "status") {
    if (args.length > 0) throw new Error("status 不接受参数");
    process.stdout.write(`${JSON.stringify(await notificationStatus())}\n`);
    return;
  }
  throw new Error("用法: hermes-notify.mjs emit|drain|deliver|deliver-correction|authorize|status");
}

if (isMainEntry(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`Hermes notification error: ${error.message}\n`);
    process.exitCode = 1;
  });
}

export { validateDeliveryPolicy, authorizeSend, emitRunEvent, emitCorrectionEvent, sentConfirmation, publishSendReceipt, validateSendReceipt, readSendReceipt, confirmsHermesDelivery, parseHermesAck, sendViaHermes, drainOutbox, drainCorrectionEvent, deliverCorrectionEvent, deliverRunEvent, notificationStatus, parseEmitArguments, NOTIFICATION_ROOT };
