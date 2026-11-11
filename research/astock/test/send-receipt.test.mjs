// Telegram 回执留存的回归测试。
//
// 规则：发送时本来就校验了平台 message_id，现在必须把它连同事件、发送内容
// 哈希与确认时间落盘；回执落盘成功后才允许标记“已确认发送”。已发出但回执
// 落盘失败＝“待核实”，进 ambiguous，绝不自动重发；历史记录没有回执就是
// “回执缺失”，不补造。
import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { writeScheduledOccurrence } from "./helpers/scheduled-occurrence.mjs";

import {
  deliverRunEvent,
  drainOutbox,
  parseHermesAck,
  publishSendReceipt,
  readSendReceipt,
  sentConfirmation,
  validateSendReceipt
} from "../scripts/hermes-notify.mjs";
import { buildResultView, RESULT_VIEW_VERSION } from "../src/result-view.mjs";
import { buildVerifiedBaselineReport } from "../src/baseline-report.mjs";
import { assessPublicationGate, normalizeReportOutput } from "../src/report.mjs";
import { contextFixture, buildOptions } from "./helpers/multi-candidate-context.mjs";
import { notificationEventId } from "../src/run-notification.mjs";

const DAY = "2026-09-08";
const NOW = Date.parse(`${DAY}T01:15:00Z`);
const EVENT = notificationEventId(DAY, "success");
const digest = (value) => createHash("sha256").update(value).digest("hex");
const ACK = { ok: true, started: true, exitCode: 0, ack: { messageId: "4242" } };

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "astock-send-receipt-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  for (const directory of ["config", "work", "reports/daily"]) {
    await mkdir(path.join(root, directory), { recursive: true });
  }
  await writeFile(path.join(root, "config", "notifications.json"), JSON.stringify({
    schemaVersion: "1.0",
    deliveryEnabled: true,
    route: "telegram",
    payloadMode: "formal_report_markdown_document",
    ownerApproval: {
      status: "confirmed",
      confirmedAt: DAY,
      scope: "telegram_main_channel_formal_report_markdown_document"
    }
  }));
  await writeFile(path.join(root, "reports", "daily", `${DAY}.md`), "# Synthetic report\n");
  // 同一份 last-run 让重复触发派生出同一个事件，才能验证“不再发送”。
  await writeFile(path.join(root, "work", "last-run.json"), JSON.stringify({
    schemaVersion: "1.0", targetDate: DAY, startedAt: `${DAY}T00:31:00.000Z`,
    completedAt: `${DAY}T00:33:00.000Z`, frozenCutoffInstant: `${DAY}T00:30:30.000Z`,
    contextSha256: "c".repeat(64),
    phase: "publish", publicationBlockers: [],
    dataStatus: "partial", status: "success",
    evidenceReview: "passed", publicationGate: "passed", reportFile: `reports/daily/${DAY}.md`
  }));
  await writeScheduledOccurrence(root, DAY);
  return root;
}

function event(outcome = "success") {
  const body = Buffer.from("# Synthetic report\n");
  const value = {
    schemaVersion: "1.0",
    eventId: notificationEventId(DAY, outcome),
    source: "AStock_AI_System",
    route: "telegram",
    targetDate: DAY,
    outcome,
    reasonCode: outcome === "success" ? "formal_report_ready" : "content_gate_exhausted",
    workflowExitCode: outcome === "success" ? 0 : 1,
    occurredAt: `${DAY}T01:00:00.000Z`,
    dataStatus: "partial",
    deliveryAuthorization: "scheduled"
  };
  value.report = null;   // 非 success 事件必须显式不带报告
  if (outcome === "success") {
    value.report = {
      reportFile: `reports/daily/${DAY}.md`,
      payloadFile: `payloads/${value.eventId}.md`,
      sha256: digest(body),
      bytes: body.length
    };
  }
  return value;
}

async function seed(root, state, value) {
  const file = path.join(root, "work", "notifications", state, `${value.eventId}.json`);
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, `${JSON.stringify(value)}\n`);
  return file;
}

async function seedPayload(root, value) {
  const body = await readFile(path.join(root, "reports", "daily", `${DAY}.md`));
  const file = path.join(root, "work", "notifications", "payloads", `${value.eventId}.md`);
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, body);
  return file;
}

test("Hermes acknowledgement is parsed into the platform message id, or refused", () => {
  assert.deepEqual(parseHermesAck('{"success":true,"platform":"telegram","message_id":"77"}', 0),
    { messageId: "77" });
  assert.deepEqual(parseHermesAck(Buffer.from('{"success":true,"platform":"telegram","message_id":"1","note":"ok"}'), 0),
    { messageId: "1" });
  const rejected = [
    ['{"success":true,"platform":"telegram"}', 0, "缺少 message_id"],
    ['{"success":true,"platform":"telegram","message_id":77}', 0, "message_id 不是字符串"],
    ['{"success":true,"platform":"telegram","message_id":"0"}', 0, "非正数 message_id"],
    ['{"success":true,"platform":"telegram","message_id":" 7"}', 0, "带空白的 message_id"],
    ['{"success":false,"platform":"telegram","message_id":"7"}', 0, "success=false"],
    ['{"success":true,"platform":"email","message_id":"7"}', 0, "平台不是 telegram"],
    ['{"success":true,"platform":"telegram","message_id":"7","skipped":true}', 0, "skipped"],
    ['{"success":true,"platform":"telegram","message_id":"7","warnings":["x"]}', 0, "warnings"],
    ['{"success":true,"platform":"telegram","message_id":"7","token":"secret"}', 0, "未允许字段"],
    ['{"success":true,"platform":"telegram","message_id":"7"}', 1, "非零退出码"],
    ["not json", 0, "非 JSON"]
  ];
  for (const [stdout, code, why] of rejected) {
    assert.equal(parseHermesAck(stdout, code), null, why);
  }
});

test("a successful send records acknowledgement time before the event is marked sent", async (t) => {
  const root = await fixture(t);
  const value = event();
  await seed(root, "pending", value);
  await seedPayload(root, value);
  const calls = [];
  let clock = NOW;
  let clockReads = 0;
  const sender = async (delivery, meta) => {
    calls.push(meta.event.eventId);
    // 模拟平台发送耗时；无需真实等待，也不发送任何消息。
    clock += 5_000;
    return { ...ACK };
  };
  const receiptClock = () => {
    assert.equal(calls.length, 1, "确认时钟必须在 sender 返回成功之后才读取");
    clockReads += 1;
    return clock;
  };
  const summary = await drainOutbox({ projectRoot: root, deliveryMode: "telegram", now: NOW,
    receiptClock, sender });
  assert.equal(summary.sent, 1);
  assert.equal(summary.ambiguous, 0);
  assert.equal(summary.receiptFailed, 0);
  assert.equal(calls.length, 1);

  const receiptFile = path.join(root, "work", "notifications", "receipts", "send", `${EVENT}.json`);
  const receipt = JSON.parse(await readFile(receiptFile, "utf8"));
  assert.deepEqual(Object.keys(receipt).sort(), [
    "ackKind", "confirmedAt", "eventId", "payloadBytes", "payloadSha256", "platformMessageId",
    "route", "schemaVersion", "targetDate"
  ]);
  assert.equal(receipt.platformMessageId, "4242");
  assert.equal(receipt.eventId, EVENT);
  assert.equal(receipt.targetDate, DAY);
  assert.equal(receipt.payloadSha256, value.report.sha256);
  assert.equal(receipt.payloadBytes, value.report.bytes);
  assert.equal(receipt.ackKind, "hermes_cli_json_message_id");
  assert.equal(clockReads, 1);
  assert.equal(receipt.confirmedAt, new Date(NOW + 5_000).toISOString());
  assert.notEqual(receipt.confirmedAt, new Date(NOW).toISOString(), "不能记录成发送开始时刻");
  // 不得出现凭据或原始响应字段。
  const raw = JSON.stringify(receipt);
  for (const forbidden of ["token", "chat_id", "Authorization", "bot"]) {
    assert.ok(!raw.includes(forbidden), `回执不得包含 ${forbidden}`);
  }
  // 事件确实进了 sent，且回执可被只读校验读回。
  const sent = JSON.parse(await readFile(path.join(root, "work", "notifications", "sent", `${EVENT}.json`), "utf8"));
  assert.equal(sent.eventId, EVENT);
  const readBack = await readSendReceipt({ sendReceipts: path.dirname(receiptFile) }, EVENT,
    { targetDate: DAY, payloadSha256: value.report.sha256 });
  assert.equal(readBack.platformMessageId, "4242");
});

test("default receipt clock uses real acknowledgement time, not injected drain start", async (t) => {
  const root = await fixture(t);
  const value = event();
  await seed(root, "pending", value);
  await seedPayload(root, value);
  let acknowledgedAt;
  const sender = async () => {
    acknowledgedAt = Date.now();
    return { ...ACK };
  };
  const summary = await drainOutbox({ projectRoot: root, deliveryMode: "telegram", now: NOW, sender });
  const finishedAt = Date.now();
  assert.equal(summary.sent, 1);
  const receipt = JSON.parse(await readFile(path.join(root, "work", "notifications",
    "receipts", "send", `${EVENT}.json`), "utf8"));
  const confirmedAt = Date.parse(receipt.confirmedAt);
  assert.ok(confirmedAt >= acknowledgedAt, "确认不能早于 sender 成功返回");
  assert.ok(confirmedAt <= finishedAt, "确认不能晚于此次调用结束");
  assert.notEqual(confirmedAt, NOW, "旧 drain now 不得成为新的确认时间");
});

test("invalid receipt clock option is refused before queue mutation or sending", async (t) => {
  const root = await fixture(t);
  let calls = 0;
  const sender = async () => { calls += 1; return { ...ACK }; };
  for (const receiptClock of [null, false, 0, "clock", {}, []]) {
    await assert.rejects(drainOutbox({ projectRoot: root, deliveryMode: "telegram", now: NOW,
      receiptClock, sender }), /receiptClock 必须是函数/);
  }
  assert.equal(calls, 0);
  await assert.rejects(readdir(path.join(root, "work", "notifications")), { code: "ENOENT" });
});

test("invalid acknowledgement clock becomes ambiguous and never resends", async (t) => {
  const cases = [
    ["NaN", () => NaN], ["infinite", () => Infinity], ["negative", () => -1],
    ["fractional", () => NOW + 0.5], ["string", () => String(NOW)],
    ["null", () => null], ["out-of-range date", () => Number.MAX_SAFE_INTEGER],
    ["throwing", () => { throw new Error("clock unavailable"); }]
  ];
  for (const [name, receiptClock] of cases) {
    await t.test(name, async (subtest) => {
      const root = await fixture(subtest);
      const value = event();
      await seed(root, "pending", value);
      await seedPayload(root, value);
      let calls = 0;
      const sender = async () => { calls += 1; return { ...ACK }; };
      const first = await drainOutbox({ projectRoot: root, deliveryMode: "telegram", now: NOW,
        receiptClock, sender });
      assert.equal(first.sent, 0);
      assert.equal(first.ambiguous, 1);
      assert.equal(first.receiptFailed, 1);
      assert.equal(calls, 1);
      assert.equal((await sentConfirmation(root, EVENT)).confirmed, false);
      const sendDir = path.join(root, "work", "notifications", "receipts", "send");
      assert.deepEqual(await readdir(sendDir), [], "错误时钟不得产生回执");
      const ambiguous = JSON.parse(await readFile(path.join(root, "work", "notifications",
        "ambiguous", `${EVENT}.json`), "utf8"));
      assert.equal(ambiguous.eventId, EVENT);
      const again = await drainOutbox({ projectRoot: root, deliveryMode: "telegram", now: NOW + 60_000,
        receiptClock: () => NOW + 60_000, sender });
      assert.equal(again.sent, 0);
      assert.equal(calls, 1, "已有 ambiguous 事件绝不自动重发");
    });
  }
});

test("a send whose receipt cannot be stored becomes 待核实 and is never resent", async (t) => {
  const root = await fixture(t);
  const value = event();
  await seed(root, "pending", value);
  await seedPayload(root, value);
  // 先让布局正常建好（disabled 模式只建目录、不发送），发送后再阻断回执路径。
  await drainOutbox({ projectRoot: root, deliveryMode: "disabled", now: NOW, sender: async () => ({}) });
  // 真正开始发送后才让落盘失败；已有回执路径现在会在发送前拒绝。
  const sendDir = path.join(root, "work", "notifications", "receipts", "send");

  let calls = 0;
  const sender = async () => {
    calls += 1;
    await mkdir(path.join(sendDir, `${EVENT}.json`), { recursive: true });
    return { ...ACK };
  };
  const first = await drainOutbox({ projectRoot: root, deliveryMode: "telegram", now: NOW, sender });
  assert.equal(first.sent, 0, "回执没落盘就不能算已确认发送");
  assert.equal(first.ambiguous, 1);
  assert.equal(first.receiptFailed, 1);
  assert.equal(calls, 1, "消息已经发出，只发这一次");

  const second = await drainOutbox({ projectRoot: root, deliveryMode: "telegram", now: NOW + 60_000, sender });
  assert.equal(second.sent, 0);
  assert.equal(second.ambiguous, 0, "事件不在 pending 里，不会再次进入发送");
  assert.equal(calls, 1, "绝不自动重发");
  const ambiguous = JSON.parse(await readFile(
    path.join(root, "work", "notifications", "ambiguous", `${EVENT}.json`), "utf8"));
  assert.equal(ambiguous.eventId, EVENT);
  // 失败路径不留半成品回执或临时文件。
  const leftovers = (await readdir(sendDir)).filter((name) => name.startsWith("."));
  assert.deepEqual(leftovers, [], `临时文件应被清理：${leftovers}`);
});

test("history keeps 发送记录存在、回执缺失 and forged receipts are refused", async (t) => {
  const root = await fixture(t);
  const value = event();
  // 历史形态：只有 sent 记录，没有回执。
  await seed(root, "sent", value);
  const paths = { sendReceipts: path.join(root, "work", "notifications", "receipts", "send") };
  assert.equal(await readSendReceipt(paths, EVENT), null, "没有回执就是回执缺失");

  const good = {
    schemaVersion: "1.0", eventId: EVENT, targetDate: DAY, route: "telegram",
    payloadSha256: value.report.sha256, payloadBytes: value.report.bytes,
    platformMessageId: "99", ackKind: "hermes_cli_json_message_id",
    confirmedAt: `${DAY}T01:15:00.000Z`
  };
  assert.ok(validateSendReceipt(good));
  // [候选, 说明, 形状本身是否非法]
  const forged = [
    [{ ...good, eventId: notificationEventId("2026-09-09", "success") }, "事件不符", false],
    [{ ...good, targetDate: "2026-09-09" }, "日期不符", false],
    [{ ...good, payloadSha256: "b".repeat(64) }, "哈希不符", false],
    [{ ...good, payloadBytes: -1 }, "字节数非法", true],
    [{ ...good, ackKind: "hermes_cli_exit_0" }, "确认类型不符", true],
    [{ ...good, confirmedAt: "2026-09-08 01:15" }, "确认时间非 ISO", true],
    [{ ...good, message_id: "99" }, "未允许字段", true],
    [{ ...good, platformMessageId: "0" }, "消息 ID 非正数", true],
    [{ ...good, payloadSha256: undefined }, "缺哈希", true]
  ];
  for (const [candidate, why, shapeInvalid] of forged) {
    // 与期望事件比对时必须拒绝（形状合法但绑定不符的也要拦住）。
    assert.equal(validateSendReceipt(candidate, { eventId: EVENT, targetDate: DAY,
      payloadSha256: value.report.sha256 }), null, why);
    if (shapeInvalid) assert.equal(validateSendReceipt(candidate), null, `${why}（形状即非法）`);
    else assert.ok(validateSendReceipt(candidate), `${why}：形状合法，仅绑定不符`);
  }
  assert.equal(validateSendReceipt(good, { payloadSha256: "c".repeat(64) }), null, "与密封件不一致时不通过");
});

test("failed notifications record the delivered text hash instead of a report hash", async (t) => {
  const root = await fixture(t);
  const value = event("failed");
  await seed(root, "pending", value);
  const summary = await drainOutbox({
    projectRoot: root, deliveryMode: "telegram", now: NOW,
    sender: async () => ({ ...ACK })
  });
  assert.equal(summary.sent, 1);
  const receipt = JSON.parse(await readFile(
    path.join(root, "work", "notifications", "receipts", "send", `${value.eventId}.json`), "utf8"));
  assert.equal(receipt.eventId, value.eventId);
  assert.match(receipt.payloadSha256, /^[0-9a-f]{64}$/);
  assert.ok(receipt.payloadBytes > 0);
  assert.ok(!Object.hasOwn(receipt, "sourceSha256"), "没有来源哈希就不写这个字段");
});

test("publish refuses an acknowledgement without a platform message id", async (t) => {
  const root = await fixture(t);
  const value = event();
  const paths = { sendReceipts: path.join(root, "work", "notifications", "receipts", "send") };
  const payload = { sha256: value.report.sha256, bytes: value.report.bytes };
  await assert.rejects(publishSendReceipt(paths, value, null, `${DAY}T01:15:00.000Z`, payload),
    /平台消息 ID/);
  await assert.rejects(publishSendReceipt(paths, value, { messageId: "0" }, `${DAY}T01:15:00.000Z`, payload),
    /平台消息 ID/);
  await assert.rejects(publishSendReceipt(paths, value, { messageId: "5" }, `${DAY}T01:15:00.000Z`,
    { sha256: "nope", bytes: 3 }), /内容哈希/);
  await assert.rejects(publishSendReceipt(paths, value, { messageId: "5" }, "not-a-time", payload),
    /确认时间|Invalid/);
});

/* ---------------------------------------- 发送侧确认：必须核对回执 */

async function confirmedEvent(root, value) {
  // 走真实发送路径：回执由 drain 写盘，事件随后进入 sent。
  const summary = await drainOutbox({ projectRoot: root, deliveryMode: "telegram", now: NOW,
    sender: async () => ({ ...ACK }) });
  assert.equal(summary.sent, 1, "前置条件：事件已发送并写入回执");
  return value;
}

test("confirmation requires the receipt, not just a file in sent", async (t) => {
  const root = await fixture(t);
  const value = event();
  await seed(root, "pending", value);
  await seedPayload(root, value);
  await confirmedEvent(root, value);

  const ok = await sentConfirmation(root, EVENT);
  assert.equal(ok.confirmed, true);
  assert.equal(ok.reason, null);
  assert.equal(ok.receipt.platformMessageId, "4242");

  // 回执被删除 → 不再确认，但事件仍留在 sent（不重发）
  await rm(path.join(root, "work", "notifications", "receipts", "send", `${EVENT}.json`));
  const missing = await sentConfirmation(root, EVENT);
  assert.equal(missing.confirmed, false);
  assert.equal(missing.reason, "send_receipt_missing");
  assert.ok(await readFile(path.join(root, "work", "notifications", "sent", `${EVENT}.json`), "utf8"));

  const again = await deliverRunEvent({ projectRoot: root, exitCode: 0,
    invocationStartedAt: `${DAY}T00:31:00.000Z`, invocationEndedAt: `${DAY}T00:33:00.000Z`,
    expectedTargetDate: DAY, deliveryMode: "telegram", sender: async () => ({ ...ACK }) });
  assert.equal(again.drain.sent, 0, "不会重发");
  assert.equal(again.confirmed, false, "没有回执就不能声称已确认");
});

test("confirmation rejects a receipt whose attachment binding is wrong", async (t) => {
  const cases = [
    ["payload hash", { payloadSha256: "b".repeat(64) }, "send_receipt_payload_mismatch"],
    ["payload bytes", { payloadBytes: 1 }, "send_receipt_payload_mismatch"],
    ["target date", { targetDate: "2026-09-09" }, "send_receipt_target_mismatch"]
  ];
  for (const [label, changes, expected] of cases) {
    const root = await fixture(t);
    const value = event();
    await seed(root, "pending", value);
    await seedPayload(root, value);
    await confirmedEvent(root, value);
    const receiptPath = path.join(root, "work", "notifications", "receipts", "send", `${EVENT}.json`);
    const receipt = JSON.parse(await readFile(receiptPath, "utf8"));
    await writeFile(receiptPath, JSON.stringify({ ...receipt, ...changes }));
    const result = await sentConfirmation(root, EVENT);
    assert.equal(result.confirmed, false, label);
    assert.equal(result.reason, expected, label);
  }
});

test("a corrupt receipt never confirms", async (t) => {
  const root = await fixture(t);
  const value = event();
  await seed(root, "pending", value);
  await seedPayload(root, value);
  await confirmedEvent(root, value);
  const receiptPath = path.join(root, "work", "notifications", "receipts", "send", `${EVENT}.json`);
  for (const body of ["{not json", JSON.stringify({ junk: true }), JSON.stringify({
    ...JSON.parse(await readFile(receiptPath, "utf8")), token: "secret" })]) {
    await writeFile(receiptPath, body);
    const result = await sentConfirmation(root, EVENT);
    assert.equal(result.confirmed, false, body.slice(0, 20));
    assert.equal(result.reason, "send_receipt_missing", body.slice(0, 20));
  }
});

/* 结果版附件是投影：需要一份结构合法的正式报告才能生成。 */
function completeReport() {
  const context = contextFixture();
  context.meta.targetDate = DAY;
  context.data.tradingCalendar.targetDate = DAY;
  const cutoffInstant = new Date(`${DAY}T00:00:00Z`);
  const publicationAudit = assessPublicationGate({ context, cutoffInstant, targetDate: DAY });
  const options = buildOptions(context, publicationAudit,
    { targetDate: DAY, cutoffLocal: `${DAY} 08:00`, cutoffInstant });
  return normalizeReportOutput(buildVerifiedBaselineReport(options), { ...options,
    strictContextSources: true, enforcePublicationReady: true,
    requireDeterministicCompilerOutput: true });
}

test("a result-view send confirms only when both attachments bind", async (t) => {
  const root = await fixture(t);
  await writeFile(path.join(root, "reports", "daily", `${DAY}.md`), completeReport());
  // 密封件绑定的是报告字节：报告换了就必须按新字节重新出密封，否则正文与密封失配。
  await writeScheduledOccurrence(root, DAY);
  const source = await readFile(path.join(root, "reports", "daily", `${DAY}.md`));
  const projection = Buffer.from(buildResultView(source.toString("utf8"), { targetDate: DAY }), "utf8");
  const value = {
    ...event(),
    report: {
      reportFile: `reports/daily/${DAY}.md`,
      payloadFile: `payloads/${EVENT}.md`,
      sha256: digest(projection), bytes: projection.length,
      presentation: RESULT_VIEW_VERSION,
      sourceSha256: digest(source), sourceBytes: source.length
    }
  };
  const payloadPath = path.join(root, "work", "notifications", "payloads", `${EVENT}.md`);
  await mkdir(path.dirname(payloadPath), { recursive: true });
  await writeFile(payloadPath, projection);
  await seed(root, "pending", value);
  await confirmedEvent(root, value);

  const ok = await sentConfirmation(root, EVENT);
  assert.equal(ok.confirmed, true, "附件与来源都绑定时才确认");
  assert.equal(ok.receipt.payloadSha256, digest(projection));
  assert.equal(ok.receipt.sourceSha256, digest(source));

  // 回执缺来源哈希：附件对也不确认
  const receiptPath = path.join(root, "work", "notifications", "receipts", "send", `${EVENT}.json`);
  const receipt = JSON.parse(await readFile(receiptPath, "utf8"));
  delete receipt.sourceSha256;
  await writeFile(receiptPath, JSON.stringify(receipt));
  const missingSource = await sentConfirmation(root, EVENT);
  assert.equal(missingSource.confirmed, false);
  assert.equal(missingSource.reason, "send_receipt_source_missing_or_mismatch");

  // 来源哈希错：同样不确认
  await writeFile(receiptPath, JSON.stringify({ ...receipt, sourceSha256: "f".repeat(64) }));
  const wrongSource = await sentConfirmation(root, EVENT);
  assert.equal(wrongSource.confirmed, false);
  assert.equal(wrongSource.reason, "send_receipt_source_missing_or_mismatch");
});
