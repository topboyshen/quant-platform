import test, { afterEach, beforeEach, mock } from "node:test";
import assert from "node:assert/strict";
import childProcess from "node:child_process";
import net from "node:net";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { syncBuiltinESMExports } from "node:module";
import { mkdtemp, mkdir, readFile, readdir, rm, stat, utimes, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { writeScheduledOccurrence } from "./helpers/scheduled-occurrence.mjs";

import { confirmsHermesDelivery, deliverRunEvent, drainOutbox, sendViaHermes } from "../scripts/hermes-notify.mjs";
import { notificationEventId } from "../src/run-notification.mjs";

const DAY = "2026-09-08";
const NOW = Date.parse(`${DAY}T01:00:00.000Z`);
const STATES = ["pending", "sending", "sent", "ambiguous", "dead"];
const ACK = { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };

// Test fixtures may perform local file I/O; no sender, process or network
// implementation may reach the host. Every delivery uses an explicit mock.
beforeEach(() => {
  const forbidden = () => { throw new Error("notification test forbids external effects"); };
  for (const name of ["spawn", "spawnSync", "exec", "execSync", "execFile", "execFileSync", "fork"]) {
    mock.method(childProcess, name, forbidden);
  }
  mock.method(net.Socket.prototype, "connect", forbidden);
  mock.method(globalThis, "fetch", forbidden);
  syncBuiltinESMExports();
});
afterEach(() => {
  mock.restoreAll();
  syncBuiltinESMExports();
});

async function fixture(t, deliveryEnabled = true) {
  const root = await mkdtemp(path.join(os.tmpdir(), "astock-targeted-notify-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  for (const directory of ["config", "work", "reports/daily"]) {
    await mkdir(path.join(root, directory), { recursive: true });
  }
  await writeFile(path.join(root, "config", "notifications.json"), JSON.stringify({
    schemaVersion: "1.0",
    deliveryEnabled,
    route: "telegram",
    payloadMode: "formal_report_markdown_document",
    ownerApproval: {
      status: "confirmed",
      confirmedAt: DAY,
      scope: "telegram_main_channel_formal_report_markdown_document"
    }
  }));
  await writeFile(path.join(root, "reports", "daily", `${DAY}.md`), "# Synthetic report\n");
  await writeFile(path.join(root, "work", "last-run.json"), JSON.stringify({
    schemaVersion: "1.0",
    targetDate: DAY,
    startedAt: `${DAY}T00:31:00.000Z`,
    completedAt: `${DAY}T00:33:00.000Z`,
    frozenCutoffInstant: `${DAY}T00:30:30.000Z`,
    contextSha256: "c".repeat(64),
    phase: "publish",
    publicationBlockers: [],
    dataStatus: "partial",
    status: "success",
    evidenceReview: "passed",
    publicationGate: "passed",
    reportFile: `reports/daily/${DAY}.md`
  }));
  await writeScheduledOccurrence(root, DAY);
  return root;
}

function event(targetDate = DAY, outcome = "failed") {
  return {
    schemaVersion: "1.0",
    eventId: notificationEventId(targetDate, outcome),
    source: "AStock_AI_System",
    route: "telegram",
    targetDate,
    outcome,
    reasonCode: outcome === "skipped" ? "non_trading_day" : "workflow_timeout",
    workflowExitCode: outcome === "skipped" ? 0 : 124,
    occurredAt: `${targetDate}T00:34:00.000Z`,
    dataStatus: null,
    deliveryAuthorization: "scheduled",
    report: null
  };
}

function eventPath(root, state, eventId) {
  assert.ok(STATES.includes(state));
  return path.join(root, "work", "notifications", state, `${eventId}.json`);
}

async function seed(root, state, value, filenameId = value.eventId) {
  const file = eventPath(root, state, filenameId);
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, `${JSON.stringify(value)}\n`);
  // 事件标记为定时路径时，当日 occurrence 也必须存在；否则门禁按人工恢复拦下。
  if (typeof value?.targetDate === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value.targetDate)) {
    await writeScheduledOccurrence(root, value.targetDate);
  }
  return file;
}

async function unchangedSnapshot(files) {
  return Promise.all(files.map(async (file) => {
    const info = await stat(file);
    return { file, body: await readFile(file, "utf8"), size: info.size,
      mtimeMs: info.mtimeMs, ctimeMs: info.ctimeMs, mode: info.mode };
  }));
}

function deliverOptions(projectRoot, sender, exitCode = 0) {
  return {
    projectRoot,
    sender,
    exitCode,
    expectedTargetDate: DAY,
    invocationStartedAt: `${DAY}T00:30:00.000Z`,
    invocationEndedAt: `${DAY}T00:34:00.000Z`,
    drainNow: NOW,
    deliveryMode: "telegram"
  };
}

test("targeted drain sends exactly one event and leaves other dates, outcomes and states untouched", async (t) => {
  const root = await fixture(t);
  const target = event();
  await seed(root, "pending", target);
  const untouched = [];
  for (let day = 1; day <= 25; day += 1) {
    untouched.push(await seed(root, "pending", event(`2026-08-${String(day).padStart(2, "0")}`)));
  }
  untouched.push(await seed(root, "pending", event(DAY, "skipped")));
  for (const state of ["sending", "sent", "ambiguous", "dead"]) {
    const file = await seed(root, state, event("2026-09-07"));
    if (state === "sending") await utimes(file, new Date(NOW - 20 * 60_000), new Date(NOW - 20 * 60_000));
    untouched.push(file);
  }
  const before = await unchangedSnapshot(untouched);
  const sender = mock.fn(async () => ({ ...ACK }));
  const result = await drainOutbox({ projectRoot: root, targetEventId: target.eventId,
    deliveryMode: "telegram", now: NOW, sender });
  assert.equal(result.sent, 1);
  assert.equal(result.ambiguous, 0);
  assert.equal(sender.mock.callCount(), 1);
  assert.equal(sender.mock.calls[0].arguments[1].event.eventId, target.eventId);
  assert.deepEqual(await unchangedSnapshot(untouched), before);
  assert.equal(JSON.parse(await readFile(eventPath(root, "sent", target.eventId))).eventId, target.eventId);
});

test("deliver pins the emitted event despite a backlog larger than the legacy batch limit", async (t) => {
  const root = await fixture(t);
  const untouched = [];
  for (let day = 1; day <= 25; day += 1) {
    untouched.push(await seed(root, "pending", event(`2026-08-${String(day).padStart(2, "0")}`)));
  }
  const before = await unchangedSnapshot(untouched);
  const sender = mock.fn(async () => ({ ...ACK }));
  const result = await deliverRunEvent(deliverOptions(root, sender));
  assert.equal(result.eventId, notificationEventId(DAY, "success"));
  assert.equal(result.confirmed, true);
  assert.equal(result.finalState, "sent");
  assert.equal(sender.mock.callCount(), 1);
  assert.equal(sender.mock.calls[0].arguments[1].event.eventId, result.eventId);
  assert.deepEqual(await unchangedSnapshot(untouched), before);
});

test("targeted recovery moves only its stale sending event and never sends it again", async (t) => {
  const root = await fixture(t);
  const target = event();
  const targetFile = await seed(root, "sending", target);
  const otherFile = await seed(root, "sending", event("2026-09-07"));
  for (const file of [targetFile, otherFile]) {
    await utimes(file, new Date(NOW - 20 * 60_000), new Date(NOW - 20 * 60_000));
  }
  const pending = await seed(root, "pending", event("2026-09-06"));
  const before = await unchangedSnapshot([otherFile, pending]);
  const sender = mock.fn(async () => ({ ...ACK }));
  const options = { projectRoot: root, targetEventId: target.eventId, deliveryMode: "telegram", now: NOW, sender };
  assert.equal((await drainOutbox(options)).ambiguous, 1);
  assert.equal((await drainOutbox(options)).sent, 0);
  assert.equal(sender.mock.callCount(), 0);
  assert.equal(JSON.parse(await readFile(eventPath(root, "ambiguous", target.eventId))).eventId, target.eventId);
  assert.deepEqual(await unchangedSnapshot([otherFile, pending]), before);
});

test("an active target sending event remains deferred without changing any event file", async (t) => {
  const root = await fixture(t);
  const target = event();
  const targetFile = await seed(root, "sending", target);
  await utimes(targetFile, new Date(NOW), new Date(NOW));
  const other = await seed(root, "pending", event("2026-09-07"));
  const before = await unchangedSnapshot([targetFile, other]);
  const sender = mock.fn(async () => ({ ...ACK }));
  const result = await drainOutbox({ projectRoot: root, targetEventId: target.eventId,
    deliveryMode: "telegram", now: NOW, sender });
  assert.equal(result.deferred, 1);
  assert.equal(sender.mock.callCount(), 0);
  assert.deepEqual(await unchangedSnapshot([targetFile, other]), before);
});

test("invalid target IDs are rejected before creating notification directories", async (t) => {
  const root = await fixture(t);
  const sender = mock.fn(async () => ({ ...ACK }));
  for (const targetEventId of [undefined, null, 1, [], {}, "", "../sent/event", " astock-2026-09-08-failed-telegram-v1",
    "astock-2026-09-08-failed-telegram-v1.json", "astock-2026-02-30-failed-telegram-v1",
    "astock-2026-09-08-correction-v1-telegram-v1", "astock-2026-09-08-success-email-v1",
    "astock-2026-09-08-failed-telegram-v1\n"]) {
    await assert.rejects(drainOutbox({ projectRoot: root, targetEventId, deliveryMode: "telegram", sender }), /targetEventId/);
  }
  await assert.rejects(drainOutbox(Object.assign(Object.create({ targetEventId: "../inherited" }), {
    projectRoot: root, deliveryMode: "telegram", sender
  })), /targetEventId/);
  assert.equal(sender.mock.callCount(), 0);
  await assert.rejects(stat(path.join(root, "work", "notifications")), { code: "ENOENT" });
});

test("a repeated sent delivery is confirmed without sending it or an unrelated pending event", async (t) => {
  const root = await fixture(t);
  const sender = mock.fn(async () => ({ ...ACK }));
  const options = deliverOptions(root, sender);
  assert.equal((await deliverRunEvent(options)).confirmed, true);
  const other = await seed(root, "pending", event("2026-09-07"));
  const before = await unchangedSnapshot([other]);
  const result = await deliverRunEvent(options);
  assert.equal(result.confirmed, true);
  assert.equal(result.created, false);
  assert.equal(result.drain.sent, 0);
  assert.equal(sender.mock.callCount(), 1);
  assert.deepEqual(await unchangedSnapshot([other]), before);
});

test("an ambiguous target is neither resent nor confirmed on repeated deliver", async (t) => {
  const root = await fixture(t);
  const sender = mock.fn(async () => ({ ok: false, started: true, exitCode: null, timedOut: true }));
  const options = deliverOptions(root, sender, 124);
  const first = await deliverRunEvent(options);
  assert.equal(first.finalState, "ambiguous");
  const other = await seed(root, "pending", event("2026-09-07"));
  const before = await unchangedSnapshot([other, eventPath(root, "ambiguous", first.eventId)]);
  const result = await deliverRunEvent(options);
  assert.equal(result.confirmed, false);
  assert.equal(result.finalState, "ambiguous");
  assert.equal(sender.mock.callCount(), 1);
  assert.deepEqual(await unchangedSnapshot([other, eventPath(root, "ambiguous", first.eventId)]), before);
});

test("conflicting copies of a target fail closed without sending, overwriting or claiming confirmation", async (t) => {
  for (const states of [["pending", "sent"], ["pending", "ambiguous"], ["sent", "ambiguous"], ["pending", "sending"]]) {
    const root = await fixture(t);
    const target = event();
    const files = await Promise.all(states.map((state) => seed(root, state, target)));
    const before = await unchangedSnapshot(files);
    const sender = mock.fn(async () => ({ ...ACK }));
    const result = await deliverRunEvent(deliverOptions(root, sender, 124));
    assert.equal(result.confirmed, false);
    assert.equal(result.drain.deferred, 1);
    assert.equal(sender.mock.callCount(), 0);
    assert.deepEqual(await unchangedSnapshot(files), before);
  }
});

test("a target filename containing another valid event cannot send the other event", async (t) => {
  const root = await fixture(t);
  const target = event();
  const other = event("2026-09-07");
  await seed(root, "pending", other, target.eventId);
  const otherFile = await seed(root, "pending", other);
  const before = await unchangedSnapshot([otherFile]);
  const sender = mock.fn(async () => ({ ...ACK }));
  const result = await drainOutbox({ projectRoot: root, targetEventId: target.eventId,
    deliveryMode: "telegram", now: NOW, sender });
  assert.equal(result.dead, 1);
  assert.equal(result.sent, 0);
  assert.equal(sender.mock.callCount(), 0);
  assert.deepEqual(await unchangedSnapshot([otherFile]), before);
});

test("a missing target cannot drain unrelated pending or recover unrelated sending", async (t) => {
  const root = await fixture(t);
  const pending = await seed(root, "pending", event("2026-09-07"));
  const sending = await seed(root, "sending", event("2026-09-06"));
  await utimes(sending, new Date(NOW - 20 * 60_000), new Date(NOW - 20 * 60_000));
  const before = await unchangedSnapshot([pending, sending]);
  const sender = mock.fn(async () => ({ ...ACK }));
  const result = await drainOutbox({ projectRoot: root, targetEventId: event().eventId,
    deliveryMode: "telegram", now: NOW, sender });
  assert.equal(result.sent, 0);
  assert.equal(result.ambiguous, 0);
  assert.equal(sender.mock.callCount(), 0);
  assert.deepEqual(await unchangedSnapshot([pending, sending]), before);
});

test("targeted drain retains both authorization gates and counts only its held target", async (t) => {
  for (const [deliveryEnabled, deliveryMode] of [[true, "disabled"], [false, "telegram"]]) {
    const root = await fixture(t, deliveryEnabled);
    const target = event();
    const targetFile = await seed(root, "pending", target);
    const other = await seed(root, "pending", event("2026-09-07"));
    const before = await unchangedSnapshot([targetFile, other]);
    const sender = mock.fn(async () => ({ ...ACK }));
    const result = await drainOutbox({ projectRoot: root, targetEventId: target.eventId, deliveryMode, sender });
    assert.equal(result.enabled, false);
    assert.equal(result.held, 1);
    assert.equal(sender.mock.callCount(), 0);
    assert.deepEqual(await unchangedSnapshot([targetFile, other]), before);
  }
});

test("explicit drain without a target preserves legacy twenty-event batches and stale recovery", async (t) => {
  const root = await fixture(t);
  for (let day = 1; day <= 25; day += 1) {
    await seed(root, "pending", event(`2026-08-${String(day).padStart(2, "0")}`));
  }
  const stale = event("2026-07-31");
  const file = await seed(root, "sending", stale);
  await utimes(file, new Date(NOW - 20 * 60_000), new Date(NOW - 20 * 60_000));
  const sender = mock.fn(async () => ({ ...ACK }));
  const result = await drainOutbox({ projectRoot: root, deliveryMode: "telegram", now: NOW, sender });
  // 这些事件属于别的日期，没有对应的当日认领运行，因此只留档不发送；
  // 陈旧 sending 仍按原规则恢复为 ambiguous。
  assert.equal(result.sent, 0);
  assert.equal(result.awaitingAuthorization, 20);
  assert.equal(result.ambiguous, 1);
  assert.equal(sender.mock.callCount(), 0);
  assert.equal((await readdir(path.join(root, "work", "notifications", "pending"))).length, 25);
});

const HERMES_RECEIPT = {
  success: true,
  platform: "telegram",
  chat_id: "SENSITIVE_SYNTHETIC_CHAT",
  message_id: "123456"
};

test("Hermes JSON confirmation accepts only the documented complete Telegram acknowledgement", () => {
  assert.equal(confirmsHermesDelivery(JSON.stringify(HERMES_RECEIPT), 0), true);
  assert.equal(confirmsHermesDelivery(Buffer.from(JSON.stringify({ ...HERMES_RECEIPT,
    warnings: [], skipped: false, note: "SENSITIVE_SYNTHETIC_NOTE", mirrored: true })), 0), true);
  for (const change of [
    { success: false }, { success: "true" }, { platform: "slack" }, { platform: null },
    { message_id: undefined }, { message_id: null }, { message_id: 123456 }, { message_id: "" },
    { message_id: " " }, { message_id: "0" }, { message_id: "-1" }, { message_id: "01" },
    { message_id: "1.0" }, { message_id: "123\n" }, { skipped: true }, { skipped: "false" },
    { error: "SENSITIVE_SYNTHETIC_ERROR" }, { error: null },
    { warnings: ["SENSITIVE_SYNTHETIC_ATTACHMENT_FAILURE"] }, { warnings: "" }, { warnings: null },
    { undocumented_result: true }
  ]) {
    assert.equal(confirmsHermesDelivery(JSON.stringify({ ...HERMES_RECEIPT, ...change }), 0), false);
  }
  for (const exitCode of [1, null, undefined, "0"]) {
    assert.equal(confirmsHermesDelivery(JSON.stringify(HERMES_RECEIPT), exitCode), false);
  }
});

test("Hermes confirmation rejects missing, mixed, malformed, oversized and invalid UTF-8 output", () => {
  const valid = JSON.stringify(HERMES_RECEIPT);
  for (const output of [undefined, null, 1, {}, "", "null", "[]", "true", "{}", "{broken",
    `startup log\n${valid}`, `${valid}\n${valid}`, JSON.stringify({ result: HERMES_RECEIPT }),
    `${valid}${" ".repeat(64 * 1024)}`,
    Buffer.concat([Buffer.from(valid.slice(0, -1) + ',"note":"'), Buffer.from([0xff]), Buffer.from('"}')])]) {
    assert.equal(confirmsHermesDelivery(output, 0), false);
  }
  assert.equal(confirmsHermesDelivery(valid.padEnd(64 * 1024, " "), 0), true);
});

function mockHermesProcess({ chunks = [Buffer.from(JSON.stringify(HERMES_RECEIPT))], exitCode = 0,
  spawnError = false, hangs = false } = {}) {
  const children = [];
  const launch = mock.method(childProcess, "spawn", (_command, _args, _options) => {
    const child = new EventEmitter();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.stdin = new PassThrough();
    child.input = "";
    child.stdin.on("data", (chunk) => { child.input += chunk.toString("utf8"); });
    child.kill = mock.fn(() => {
      queueMicrotask(() => child.emit("close", null));
      return true;
    });
    children.push(child);
    queueMicrotask(() => {
      if (spawnError) {
        child.emit("error", new Error("SENSITIVE_SYNTHETIC_SPAWN_ERROR"));
        return;
      }
      child.emit("spawn");
      child.stderr.write("SENSITIVE_SYNTHETIC_STDERR");
      for (const chunk of chunks) child.stdout.write(chunk);
      if (!hangs) child.emit("close", exitCode);
    });
    return child;
  });
  syncBuiltinESMExports();
  return { launch, children };
}

test("Hermes sender requests bounded JSON with bytecode disabled and returns no raw receipt", async () => {
  const wire = Buffer.from(JSON.stringify({ ...HERMES_RECEIPT, note: "SENSITIVE_SYNTHETIC_NOTE" }));
  const process = mockHermesProcess({ chunks: [wire.subarray(0, 17), wire.subarray(17)] });
  const result = await sendViaHermes({ target: "telegram", message: "synthetic caption" });
  assert.equal(result.ok, true);
  assert.equal(result.started, true);
  assert.equal(result.exitCode, 0);
  assert.equal(process.launch.mock.callCount(), 1);
  assert.deepEqual(process.launch.mock.calls[0].arguments[1],
    ["-B", "-m", "hermes_cli.main", "send", "--to", "telegram", "--file", "-", "--json"]);
  assert.deepEqual(process.launch.mock.calls[0].arguments[2].stdio, ["pipe", "pipe", "pipe"]);
  assert.equal(process.children[0].input, "synthetic caption");
  assert.doesNotMatch(JSON.stringify(result), /SENSITIVE_|chat_id|note|message_id|raw/);
});

test("an exit-zero attachment warning becomes ambiguous and is never automatically resent", async (t) => {
  const root = await fixture(t);
  const process = mockHermesProcess({ chunks: [Buffer.from(JSON.stringify({ ...HERMES_RECEIPT,
    warnings: ["SENSITIVE_SYNTHETIC_ATTACHMENT_FAILURE"] }))] });
  const options = deliverOptions(root, sendViaHermes);
  const first = await deliverRunEvent(options);
  assert.equal(first.confirmed, false);
  assert.equal(first.finalState, "ambiguous");
  assert.equal(first.drain.sent, 0);
  assert.equal(first.drain.ambiguous, 1);
  const second = await deliverRunEvent(options);
  assert.equal(second.confirmed, false);
  assert.equal(second.finalState, "ambiguous");
  assert.equal(process.launch.mock.callCount(), 1);
  const storedEvent = await readFile(eventPath(root, "ambiguous", first.eventId), "utf8");
  assert.doesNotMatch(storedEvent, /SENSITIVE_|chat_id|warnings|message_id/);
});

test("Hermes sender treats skipped and unparseable exit-zero responses as unconfirmed", async () => {
  for (const text of [JSON.stringify({ ...HERMES_RECEIPT, skipped: true }), "not JSON", ""]) {
    const process = mockHermesProcess({ chunks: [Buffer.from(text)] });
    const result = await sendViaHermes({ target: "telegram", message: "synthetic" });
    assert.equal(result.ok, false);
    assert.equal(result.started, true);
    assert.equal(result.exitCode, 0);
    assert.equal(process.launch.mock.callCount(), 1);
  }
});

test("Hermes sender stops oversized stdout without exposing or retaining it as an acknowledgement", async () => {
  const process = mockHermesProcess({ chunks: [Buffer.from(JSON.stringify(HERMES_RECEIPT)), Buffer.alloc(64 * 1024)] });
  const result = await sendViaHermes({ target: "telegram", message: "synthetic" });
  assert.equal(result.ok, false);
  assert.equal(result.started, true);
  assert.equal(result.exitCode, null);
  assert.equal(process.children[0].kill.mock.callCount(), 1);
  assert.ok(result.stdoutBytes > 64 * 1024);
  assert.doesNotMatch(JSON.stringify(result), /SENSITIVE_|chat_id|note|message_id/);
});

test("Hermes sender distinguishes failure to start from a started delivery timeout", async () => {
  mockHermesProcess({ spawnError: true });
  const absent = await sendViaHermes({ target: "telegram", message: "synthetic" });
  assert.equal(absent.ok, false);
  assert.equal(absent.started, false);
  const process = mockHermesProcess({ hangs: true, chunks: [] });
  const timeout = await sendViaHermes({ target: "telegram", message: "synthetic" }, { timeoutMs: 5 });
  assert.equal(timeout.ok, false);
  assert.equal(timeout.started, true);
  assert.equal(timeout.timedOut, true);
  assert.equal(process.children[0].kill.mock.callCount(), 1);
});
