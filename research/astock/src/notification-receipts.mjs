// Platform receipt contract and immutable persistence. Missing historical proof remains missing.
import path from "node:path";
import { mkdir } from "node:fs/promises";
import { isZonedIsoInstant, MAX_RECEIPT_BYTES } from "./notification-contract.mjs";
import { readBounded, publishOnce } from "./notification-store.mjs";

const SEND_RECEIPT_KEYS = new Set([
  "schemaVersion", "eventId", "targetDate", "route", "payloadSha256", "payloadBytes",
  "sourceSha256", "platformMessageId", "ackKind", "confirmedAt"
]);
const SEND_RECEIPT_ACK_KIND = "hermes_cli_json_message_id";


function sendReceiptPath(paths, eventId) {
  return path.join(paths.sendReceipts, `${eventId}.json`);
}



/* 只保留可核对的字段：平台消息 ID、事件、发送内容哈希、确认时间。
   Token、chat_id、原始响应一律不落盘。 */
function buildSendReceipt(event, ack, confirmedAt, payload) {
  const receipt = {
    schemaVersion: "1.0",
    eventId: event.eventId,
    targetDate: event.targetDate,
    route: "telegram",
    payloadSha256: payload.sha256,
    payloadBytes: payload.bytes,
    platformMessageId: ack.messageId,
    ackKind: SEND_RECEIPT_ACK_KIND,
    confirmedAt
  };
  // 结果版事件同时绑定正式报告来源哈希，便于与密封件直接比对。
  if (typeof event.report?.sourceSha256 === "string") receipt.sourceSha256 = event.report.sourceSha256;
  return receipt;
}



/* 回执先落盘，事件才允许进入 sent；落盘失败则进 ambiguous（待核实），不自动重发。 */
async function publishSendReceipt(paths, event, ack, confirmedAt, payload) {
  if (!ack || typeof ack.messageId !== "string" || !/^[1-9][0-9]*$/.test(ack.messageId)) {
    throw new Error("发送回执缺少平台消息 ID");
  }
  if (!payload || !/^[0-9a-f]{64}$/.test(payload.sha256) || !Number.isSafeInteger(payload.bytes)) {
    throw new Error("发送回执缺少内容哈希");
  }
  if (!isZonedIsoInstant(confirmedAt)) throw new Error("发送回执确认时间无效");
  await mkdir(paths.sendReceipts, { recursive: true, mode: 0o700 });
  const receipt = buildSendReceipt(event, ack, confirmedAt, payload);
  if (!await publishOnce(sendReceiptPath(paths, event.eventId), JSON.stringify(receipt, null, 2) + "\n")) {
    throw new Error("发送回执已存在，不允许覆盖");
  }
  return sendReceiptPath(paths, event.eventId);
}



/* 只读校验：历史记录没有回执就是“回执缺失”，不补造。 */
function validateSendReceipt(receipt, expected = {}) {
  if (!receipt || typeof receipt !== "object" || Array.isArray(receipt)) return null;
  if (Object.keys(receipt).some((key) => !SEND_RECEIPT_KEYS.has(key))) return null;
  if (receipt.schemaVersion !== "1.0") return null;
  if (typeof receipt.eventId !== "string" || receipt.eventId.trim() !== receipt.eventId) return null;
  if (expected.eventId !== undefined && receipt.eventId !== expected.eventId) return null;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(receipt.targetDate ?? "")) return null;
  if (expected.targetDate !== undefined && receipt.targetDate !== expected.targetDate) return null;
  if (receipt.route !== "telegram" || receipt.ackKind !== SEND_RECEIPT_ACK_KIND) return null;
  if (typeof receipt.payloadSha256 !== "string" || !/^[0-9a-f]{64}$/.test(receipt.payloadSha256)) return null;
  if (expected.payloadSha256 !== undefined && receipt.payloadSha256 !== expected.payloadSha256) return null;
  if (!Number.isSafeInteger(receipt.payloadBytes) || receipt.payloadBytes < 0) return null;
  if (expected.payloadBytes !== undefined && receipt.payloadBytes !== expected.payloadBytes) return null;
  if (expected.route !== undefined && receipt.route !== expected.route) return null;
  if (expected.ackKind !== undefined && receipt.ackKind !== expected.ackKind) return null;
  if (Object.hasOwn(receipt, "sourceSha256")
      && (typeof receipt.sourceSha256 !== "string" || !/^[0-9a-f]{64}$/.test(receipt.sourceSha256))) {
    return null;
  }
  // null means the sending event has no source hash, so a receipt must not invent one.
  if (expected.sourceSha256 === null) {
    if (Object.hasOwn(receipt, "sourceSha256")) return null;
  } else if (expected.sourceSha256 !== undefined && receipt.sourceSha256 !== expected.sourceSha256) {
    return null;
  }
  if (typeof receipt.platformMessageId !== "string" || !/^[1-9][0-9]*$/.test(receipt.platformMessageId)) return null;
  if (!isZonedIsoInstant(receipt.confirmedAt)) return null;
  return receipt;
}



async function readSendReceipt(paths, eventId, expected = {}) {
  let raw;
  try {
    raw = await readBounded(sendReceiptPath(paths, eventId), MAX_RECEIPT_BYTES);
  } catch {
    return null;
  }
  let parsed;
  try {
    parsed = JSON.parse(raw.toString("utf8"));
  } catch {
    return null;
  }
  return validateSendReceipt(parsed, { eventId, ...expected });
}

export { sendReceiptPath, publishSendReceipt, validateSendReceipt, readSendReceipt };
