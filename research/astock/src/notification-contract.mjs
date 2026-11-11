// Pure notification constants, identity and policy contracts; no I/O or sends.
import path from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { isResultPresentation } from "./report-presentation.mjs";

const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const NOTIFICATION_ROOT = path.join(PROJECT_ROOT, "work", "notifications");
const MAX_LAST_RUN_BYTES = 64 * 1024;
const MAX_POLICY_BYTES = 16 * 1024;
const MAX_EVENT_BYTES = 128 * 1024;
const MAX_REPORT_BYTES = 1024 * 1024;
const MAX_GATE_BYTES = 128 * 1024;
const MAX_CONTEXT_BYTES = 8 * 1024 * 1024;
const MAX_RECEIPT_BYTES = 32 * 1024;
const DRAIN_LOCK_STALE_MS = 35 * 60 * 1000;
const SENDING_STALE_MS = 10 * 60 * 1000;
const EVENT_STATES = ["pending", "sending", "sent", "ambiguous", "dead"];
const RUN_EVENT_FILE_PATTERN = /^astock-\d{4}-\d{2}-\d{2}-(success|failed|skipped)-telegram-v1\.json$/;
const ORCHESTRATOR_INVOCATION_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const DELIVERY_AUTHORIZATION_SCHEDULED = "scheduled";
const DELIVERY_AUTHORIZATION_AWAITING = "awaiting_authorization";
const MAX_OCCURRENCE_BYTES = 64 * 1024;
const MAX_AUTHORIZATION_BYTES = 8 * 1024;
const MAX_SEAL_BYTES = 512 * 1024;

function validateExpectedTargetDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new Error("expectedTargetDate 格式无效");
  }
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) {
    throw new Error("expectedTargetDate 不是有效日期");
  }
  return value;
}



function validateTargetRunEventId(value) {
  if (typeof value !== "string") throw new Error("targetEventId 格式无效");
  const match = /^astock-(\d{4}-\d{2}-\d{2})-(success|failed|skipped)-telegram-v1$/.exec(value);
  if (!match || match[0] !== value) throw new Error("targetEventId 格式无效");
  try {
    validateExpectedTargetDate(match[1]);
  } catch {
    throw new Error("targetEventId 日期无效");
  }
  return value;
}



function isStrictUtcInstant(value) {
  return (
    typeof value === "string"
    && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(value)
    && Number.isFinite(Date.parse(value))
  );
}



function sha256(body) {
  return createHash("sha256").update(body).digest("hex");
}



function isIsoDateTime(value) {
  return typeof value === "string" && value.length > 0 && Number.isFinite(Date.parse(value));
}


function isZonedIsoInstant(value) {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/.test(value)
    && Number.isFinite(Date.parse(value));
}



function validateDeliveryPolicy(policy) {
  if (
    !policy ||
    typeof policy !== "object" ||
    Array.isArray(policy) ||
    policy.schemaVersion !== "1.0" ||
    typeof policy.deliveryEnabled !== "boolean" ||
    policy.route !== "telegram" ||
    policy.payloadMode !== "formal_report_markdown_document"
  ) {
    throw new Error("config/notifications.json 无效");
  }
  if (Object.hasOwn(policy, "reportView") &&
      policy.reportView !== "full" && !isResultPresentation(policy.reportView)) {
    throw new Error("config/notifications.json reportView 无效");
  }
  if (policy.deliveryEnabled) {
    const approval = policy.ownerApproval;
    if (
      !approval ||
      typeof approval !== "object" ||
      Array.isArray(approval) ||
      approval.status !== "confirmed" ||
      !/^\d{4}-\d{2}-\d{2}$/.test(approval.confirmedAt ?? "") ||
      approval.scope !== "telegram_main_channel_formal_report_markdown_document"
    ) {
      throw new Error("Telegram 外发已启用，但缺少所有者确认记录");
    }
  }
  return policy;
}



function validateDeliveryMode(value = "disabled") {
  if (value !== "disabled" && value !== "telegram") {
    throw new Error("ASTOCK_FINAL_DELIVERY 必须是 telegram 或 disabled");
  }
  return value;
}



function unauthorizedDrainResult({ correction = false } = {}) {
  return {
    enabled: false,
    held: 0,
    busy: false,
    sent: 0,
    ambiguous: 0,
    dead: 0,
    deferred: 0,
    awaitingAuthorization: 0,
    ...(correction ? { recovered: 0 } : {})
  };
}



function unauthorizedDeliverResult() {
  return {
    eventId: null,
    created: false,
    emittedState: null,
    drain: unauthorizedDrainResult(),
    finalState: null,
    confirmed: false
  };
}



/* 发送内容哈希：成功事件用已校验的 payload 身份（payload 文件字节），
   失败/跳过事件没有 payload 文件，就对实际发出的正文取哈希。 */
function deliveryIdentity(event, delivery) {
  if (event.outcome === "success" && event.report) {
    return { sha256: event.report.sha256, bytes: event.report.bytes };
  }
  const body = Buffer.from(delivery.message, "utf8");
  return { sha256: createHash("sha256").update(body).digest("hex"), bytes: body.length };
}

export { validateExpectedTargetDate, validateTargetRunEventId, isStrictUtcInstant, sha256, isIsoDateTime, isZonedIsoInstant, validateDeliveryPolicy, validateDeliveryMode, unauthorizedDrainResult, unauthorizedDeliverResult, deliveryIdentity, PROJECT_ROOT, NOTIFICATION_ROOT, MAX_LAST_RUN_BYTES, MAX_POLICY_BYTES, MAX_EVENT_BYTES, MAX_REPORT_BYTES, MAX_GATE_BYTES, MAX_CONTEXT_BYTES, MAX_RECEIPT_BYTES, DRAIN_LOCK_STALE_MS, SENDING_STALE_MS, EVENT_STATES, RUN_EVENT_FILE_PATTERN, ORCHESTRATOR_INVOCATION_PATTERN, DELIVERY_AUTHORIZATION_SCHEDULED, DELIVERY_AUTHORIZATION_AWAITING, MAX_OCCURRENCE_BYTES, MAX_AUTHORIZATION_BYTES, MAX_SEAL_BYTES };
