/**
 * 发送授权门禁回归（含产物绑定）。
 *
 * 规则：事件只有在能证明「这次认领运行产出了这份待发正文」时才允许自动发送：
 *   occurrence(编排器认领、已完成 success + completedAt)
 *     -> last-run 截止与 occurrence 的版本化截止身份一致          (同一次运行)
 *     -> 运行开始/完成落在认领时间窗内                            (出自这次运行)
 *     -> 该运行已完成密封与发布门禁
 *     -> 密封件把 报告哈希 与 last-run 字节绑在一起
 *     -> event.report.sha256 == 待发正文字节
 * 任一环缺失即标记 awaiting_authorization 并只留档，等待一次性二次授权。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { deliverRunEvent, drainOutbox, emitRunEvent, notificationStatus, authorizeSend }
  from "../scripts/hermes-notify.mjs";

const DAY = "2026-08-03";
const REPORT = "# 正式晨报\n\n正文\n";
const OCC_START = `${DAY}T00:30:30.000Z`;
const OCC_END = `${DAY}T00:33:01.000Z`;
const RUN_START = `${DAY}T00:31:00.000Z`;
const RUN_END = `${DAY}T00:33:00.000Z`;
const CUTOFF = `${DAY}T00:30:30.000Z`;
const RECOVERY_START = `${DAY}T02:48:55.920Z`;
const RECOVERY_END = `${DAY}T02:49:34.770Z`;
const SCHEDULED_INVOCATION = "6f1c2f0e-8b1a-4d5e-9c77-2a4b6d8e0f13";
const EVENT = `astock-${DAY}-success-telegram-v1`;

const sha256 = (buffer) => createHash("sha256").update(buffer).digest("hex");
const REPORT_SHA = sha256(Buffer.from(REPORT, "utf8"));

const P = (root) => ({
  pending: path.join(root, "work", "notifications", "pending", `${EVENT}.json`),
  sent: path.join(root, "work", "notifications", "sent", `${EVENT}.json`),
  payload: path.join(root, "work", "notifications", "payloads", `${EVENT}.md`),
  receipt: path.join(root, "work", "notifications", "receipts", "send", `${EVENT}.json`),
  authorization: path.join(root, "work", "notifications", "authorizations", `${EVENT}.json`),
  run: path.join(root, "work", "last-run.json"),
  occurrence: path.join(root, "work", "orchestrator", "states", `morning-report-${DAY}.json`),
  seal: path.join(root, "work", "research-seal", DAY, `${REPORT_SHA}.json`),
  legacySeal: path.join(root, "work", "xhs", "drafts", DAY, `${REPORT_SHA}.json`),
  report: path.join(root, "reports", "daily", `${DAY}.md`)
});

function baseRun(overrides = {}) {
  return {
    schemaVersion: "1.0",
    targetDate: DAY,
    status: "success",
    phase: "publish",
    startedAt: RUN_START,
    completedAt: RUN_END,
    frozenCutoffInstant: CUTOFF,
    contextSha256: "c".repeat(64),
    cutoffLocal: `${DAY} 08:30`,
    dataStatus: "partial",
    publicationGate: "passed",
    evidenceReview: "passed",
    publicationBlockers: [],
    reportFile: `reports/daily/${DAY}.md`,
    ...overrides
  };
}

function baseOccurrence(overrides = {}) {
  return {
    schemaVersion: "1.0",
    task: "morning-report",
    targetDate: DAY,
    status: "success",
    invocationId: SCHEDULED_INVOCATION,
    startedAt: OCC_START,
    completedAt: OCC_END,
    childExitCode: 0,
    childSignal: null,
    timedOut: false,
    interrupted: false,
    ...overrides
  };
}

function sealFor(runRaw, overrides = {}, sourceOverrides = {}) {
  return {
    schemaVersion: "astock.research-seal.v1",
    kind: "research_seal",
    targetDate: DAY,
    state: "SEALED",
    generatedAt: RUN_END,
    source: {
      lastRunFile: "work/last-run.json",
      lastRunSha256: sha256(runRaw),
      reportFile: `reports/daily/${DAY}.md`,
      reportSha256: REPORT_SHA,
      publicationGate: "passed",
      evidenceReview: "passed",
      frozenCutoffInstant: CUTOFF,
      contextSha256: "c".repeat(64),
      ...sourceOverrides
    },
    ...overrides
  };
}

const json = (value) => `${JSON.stringify(value, null, 2)}\n`;

async function write(root, file, value) {
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, typeof value === "string" ? value : json(value), "utf8");
}

/* 一个完整一致的「定时运行」现场：认领已完成、运行在窗内、密封绑定报告与 last-run。 */
async function makeRoot() {
  const root = await mkdtemp(path.join(os.tmpdir(), "astock-send-binding-"));
  await mkdir(path.join(root, "reports", "daily"), { recursive: true });
  await mkdir(path.join(root, "work"), { recursive: true });
  await mkdir(path.join(root, "config"), { recursive: true });
  await write(root, path.join(root, "config", "notifications.json"), {
    schemaVersion: "1.0",
    deliveryEnabled: true,
    route: "telegram",
    payloadMode: "formal_report_markdown_document",
    ownerApproval: {
      status: "confirmed",
      confirmedAt: DAY,
      scope: "telegram_main_channel_formal_report_markdown_document"
    }
  });
  await write(root, P(root).report, REPORT);
  const run = baseRun();
  await write(root, P(root).run, run);
  await write(root, P(root).occurrence, baseOccurrence());
  const runRaw = await readFile(P(root).run);
  await write(root, P(root).seal, sealFor(runRaw));
  return root;
}

async function emit(root, times = {}) {
  return emitRunEvent({
    projectRoot: root,
    exitCode: 0,
    invocationStartedAt: times.startedAt ?? RUN_START,
    invocationEndedAt: times.endedAt ?? RUN_END,
    expectedTargetDate: DAY
  });
}

async function markerOf(root) {
  const event = JSON.parse(await readFile(P(root).pending, "utf8"));
  return event.deliveryAuthorization;
}

function recordingSender() {
  const calls = [];
  return {
    calls,
    sender: async (delivery, meta) => {
      calls.push({ delivery, meta });
      return { ok: true, started: true, exitCode: 0, ack: { messageId: String(2000 + calls.length) } };
    }
  };
}

async function exists(file) {
  try { await stat(file); return true; } catch { return false; }
}

const drainOpts = (root, sender) => ({ projectRoot: root, deliveryMode: "telegram", sender });

test("正常定时运行：认领已完成 + 运行在窗内 + 密封绑定 → 自动发送", async () => {
  const root = await makeRoot();
  try {
    await emit(root);
    assert.equal(await markerOf(root), "scheduled", "完整绑定应标记为可自动发送");
    const { calls, sender } = recordingSender();
    const drained = await drainOutbox(drainOpts(root, sender));
    assert.equal(drained.sent, 1);
    assert.equal(drained.awaitingAuthorization, 0);
    assert.equal(calls.length, 1);
    assert.equal(await exists(P(root).receipt), true, "应产生平台回执");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("v2 延迟启动保留固定计划截止，并通过同次密封和实际运行窗核验", async () => {
  const root = await makeRoot();
  try {
    const cutoff = `${DAY}T00:00:00.000Z`;
    const occurrence = baseOccurrence({ schemaVersion: "2.0", frozenCutoffInstant: cutoff,
      startedAt: `${DAY}T00:00:30.000Z` });
    const run = baseRun({ frozenCutoffInstant: cutoff, cutoffLocal: `${DAY} 08:00` });
    await write(root, P(root).occurrence, occurrence);
    await write(root, P(root).run, run);
    await write(root, P(root).seal, sealFor(await readFile(P(root).run), {}, { frozenCutoffInstant: cutoff }));
    await emit(root);
    assert.equal(await markerOf(root), "scheduled");
    const { calls, sender } = recordingSender();
    const drained = await drainOutbox(drainOpts(root, sender));
    assert.equal(drained.sent, 1);
    assert.equal(calls.length, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("新版本 cutoff 删除或篡改不能回退旧身份，未知版本也不能自动发送", async () => {
  for (const [label, overrides, reason] of [
    ["missing", { schemaVersion: "2.0" }, "occurrence_cutoff_invalid"],
    ["null", { schemaVersion: "2.0", frozenCutoffInstant: null }, "occurrence_cutoff_invalid"],
    ["self-consistent wrong schedule", { schemaVersion: "2.0", frozenCutoffInstant: CUTOFF }, "occurrence_cutoff_invalid"],
    ["changed", { schemaVersion: "2.0", frozenCutoffInstant: `${DAY}T00:00:00.000Z` }, "run_not_from_claimed_occurrence"],
    ["downgraded", { schemaVersion: "1.0", frozenCutoffInstant: CUTOFF }, "occurrence_version_invalid"],
    ["unknown", { schemaVersion: "3.0" }, "occurrence_version_invalid"]
  ]) {
    const root = await makeRoot();
    try {
      // The run cutoff equals startedAt here: a field-missing fallback would
      // wrongly accept the new record, so this covers the downgrade hazard.
      await write(root, P(root).occurrence, baseOccurrence(overrides));
      await emit(root);
      assert.equal(await markerOf(root), "awaiting_authorization", label);
      const { calls, sender } = recordingSender();
      const drained = await drainOutbox(drainOpts(root, sender));
      assert.equal(drained.sent, 0, label);
      assert.deepEqual(drained.awaitingReasons, [reason], label);
      assert.equal(calls.length, 0, label);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  }
});

test("反例：旧认领仍在、恢复沿用同一冻结截止时间但完成于窗后 → 必须等待授权", async () => {
  const root = await makeRoot();
  try {
    // 恢复运行：冻结截止时间与旧认领相同（时间相等），但实际发生在数小时之后，
    // 并重新密封了新正文。
    const recovery = baseRun({ startedAt: RECOVERY_START, completedAt: RECOVERY_END });
    await write(root, P(root).run, recovery);
    const runRaw = await readFile(P(root).run);
    await write(root, P(root).seal, sealFor(runRaw));
    await emit(root, { startedAt: RECOVERY_START, endedAt: RECOVERY_END });

    assert.equal(await markerOf(root), "awaiting_authorization",
      "时间相等不足以证明归属：运行不在认领窗内就必须等待授权");
    const { calls, sender } = recordingSender();
    const drained = await drainOutbox(drainOpts(root, sender));
    assert.equal(drained.sent, 0, "不得借用旧认领记录自动发送新正文");
    assert.equal(drained.awaitingAuthorization, 1);
    assert.deepEqual(drained.awaitingReasons, ["run_outside_claimed_window"]);
    assert.equal(calls.length, 0);
    assert.equal(await exists(P(root).receipt), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("反例：认领记录仍是 running（未完成）→ 必须等待授权", async () => {
  const root = await makeRoot();
  try {
    const running = baseOccurrence({ status: "running" });
    delete running.completedAt;
    await write(root, P(root).occurrence, running);
    await emit(root);
    assert.equal(await markerOf(root), "awaiting_authorization", "running 不能单独放行");
    const { calls, sender } = recordingSender();
    const drained = await drainOutbox(drainOpts(root, sender));
    assert.equal(drained.sent, 0);
    assert.deepEqual(drained.awaitingReasons, ["occurrence_not_completed"]);
    assert.equal(calls.length, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("新密封存在但损坏时拒绝自动发送，不得回退旧草稿", async () => {
  const root = await makeRoot();
  try {
    const legacy = sealFor(await readFile(P(root).run));
    await write(root, P(root).legacySeal, legacy);
    // 新密封存在但内容损坏：不得因此改用旧草稿。
    await writeFile(P(root).seal, "{ not json", "utf8");
    await emit(root);
    assert.equal(await markerOf(root), "awaiting_authorization");
    const { calls, sender } = recordingSender();
    const drained = await drainOutbox(drainOpts(root, sender));
    assert.equal(drained.sent, 0, "损坏的新密封不能成为回退旧草稿的理由");
    assert.deepEqual(drained.awaitingReasons, ["seal_not_json_research"]);
    assert.equal(calls.length, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("新密封确实不存在时才按兼容策略使用旧草稿清单", async () => {
  const root = await makeRoot();
  try {
    await rm(P(root).seal);
    const legacy = { ...sealFor(await readFile(P(root).run)), schemaVersion: "astock.xhs-draft.v1",
      kind: "xhs_draft", state: "DRAFT_ONLY", publishEligible: false, humanApprovalRequired: true };
    await write(root, P(root).legacySeal, legacy);
    await emit(root);
    assert.equal(await markerOf(root), "scheduled", "缺少新密封时旧草稿是既定兼容路径");
    const { calls, sender } = recordingSender();
    assert.equal((await drainOutbox(drainOpts(root, sender))).sent, 1);
    assert.equal(calls.length, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("密封存在但门禁证据不符 / 状态不符 → 等待授权", async () => {
  for (const [why, mutate] of [
    ["evidenceReview", (seal) => { seal.source.evidenceReview = "unknown"; }],
    ["publicationGate", (seal) => { seal.source.publicationGate = "blocked"; }],
    ["lastRunSha256", (seal) => { seal.source.lastRunSha256 = "a".repeat(64); }],
    ["reportSha256", (seal) => { seal.source.reportSha256 = "b".repeat(64); }],
    ["state", (seal) => { seal.state = "DRAFT_ONLY"; }]
  ]) {
    const root = await makeRoot();
    try {
      const seal = sealFor(await readFile(P(root).run));
      mutate(seal);
      await write(root, P(root).seal, seal);
      await emit(root);
      const { calls, sender } = recordingSender();
      const drained = await drainOutbox(drainOpts(root, sender));
      assert.equal(drained.sent, 0, why);
      assert.equal(calls.length, 0, why);
      assert.equal(drained.awaitingAuthorization, 1, why);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  }
});

test("运行尚未完成发布阶段 / 仍有未决阻塞项 → 成功正文等待授权", async () => {
  for (const overrides of [{ phase: "validate" }, { publicationBlockers: ["x"] }]) {
    const root = await makeRoot();
    try {
      await write(root, P(root).run, baseRun(overrides));
      await write(root, P(root).seal, sealFor(await readFile(P(root).run)));
      await emit(root);
      const { calls, sender } = recordingSender();
      const drained = await drainOutbox(drainOpts(root, sender));
      assert.equal(drained.sent, 0, JSON.stringify(overrides));
      assert.equal(calls.length, 0, JSON.stringify(overrides));
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  }
});

test("人工恢复生成（无认领记录 / 非 UUID 认领）→ 默认只留档", async () => {
  for (const mutate of [
    async (root) => { await rm(P(root).occurrence); },
    async (root) => { await write(root, P(root).occurrence,
      baseOccurrence({ invocationId: `operator-recovery-${DAY}T024855Z`, recoveryOf: "scheduled-failed" })); }
  ]) {
    const root = await makeRoot();
    try {
      await mutate(root);
      await emit(root);
      assert.equal(await markerOf(root), "awaiting_authorization");
      const { calls, sender } = recordingSender();
      const drained = await drainOutbox(drainOpts(root, sender));
      assert.equal(drained.sent, 0);
      assert.equal(calls.length, 0);
      assert.equal(await exists(P(root).pending), true, "事件应留档在 pending 等待授权");
      assert.equal((await notificationStatus(root)).awaitingAuthorization, 1);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  }
});

test("二次授权：绑定日期与正文哈希、一次有效、重复与错误一律拒绝", async () => {
  const root = await makeRoot();
  try {
    await write(root, P(root).occurrence,
      baseOccurrence({ invocationId: `operator-recovery-${DAY}T024855Z` }));
    await emit(root);
    const payloadSha = sha256(await readFile(P(root).payload));

    await assert.rejects(() => authorizeSend({
      projectRoot: root, eventId: EVENT, targetDate: DAY, payloadSha256: "c".repeat(64)
    }), /正文哈希与实际待发正文不一致/);
    await assert.rejects(() => authorizeSend({
      projectRoot: root, eventId: EVENT, targetDate: "2026-08-04", payloadSha256: payloadSha
    }), /授权日期与事件 ID 不一致/);

    const granted = await authorizeSend({
      projectRoot: root, eventId: EVENT, targetDate: DAY, payloadSha256: payloadSha
    });
    assert.equal(granted.granted, true);
    assert.equal(granted.route, "telegram");
    await assert.rejects(() => authorizeSend({
      projectRoot: root, eventId: EVENT, targetDate: DAY, payloadSha256: payloadSha
    }), /已有发送授权/);

    const { calls, sender } = recordingSender();
    assert.equal((await drainOutbox(drainOpts(root, sender))).sent, 1);
    assert.equal(calls.length, 1);
    const record = JSON.parse(await readFile(P(root).authorization, "utf8"));
    assert.notEqual(record.consumedAt, null, "发送后授权应标记已消费");

    const again = recordingSender();
    assert.equal((await drainOutbox(drainOpts(root, again.sender))).sent, 0);
    assert.equal(again.calls.length, 0, "授权一次有效，不得二次发送");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("已发送事件不会重复发送，也不接受二次授权", async () => {
  const root = await makeRoot();
  try {
    await emit(root);
    const first = recordingSender();
    assert.equal((await drainOutbox(drainOpts(root, first.sender))).sent, 1);
    const receipt = await readFile(P(root).receipt, "utf8");
    const second = recordingSender();
    const drained = await drainOutbox(drainOpts(root, second.sender));
    assert.equal(drained.sent, 0);
    assert.equal(second.calls.length, 0);
    assert.equal(await readFile(P(root).receipt, "utf8"), receipt, "历史回执不得被改写");
    await assert.rejects(() => authorizeSend({
      projectRoot: root, eventId: EVENT, targetDate: DAY, payloadSha256: REPORT_SHA
    }), /ENOENT|no such file/i);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("改写队列里的授权标记不能绕过绑定", async () => {
  const root = await makeRoot();
  try {
    const recovery = baseRun({ startedAt: RECOVERY_START, completedAt: RECOVERY_END });
    await write(root, P(root).run, recovery);
    await write(root, P(root).seal, sealFor(await readFile(P(root).run)));
    await emit(root, { startedAt: RECOVERY_START, endedAt: RECOVERY_END });
    const event = JSON.parse(await readFile(P(root).pending, "utf8"));
    event.deliveryAuthorization = "scheduled";
    await write(root, P(root).pending, event);

    const { calls, sender } = recordingSender();
    const drained = await drainOutbox(drainOpts(root, sender));
    assert.equal(drained.sent, 0, "标记被改写也要按现场重新推导");
    assert.deepEqual(drained.awaitingReasons, ["run_outside_claimed_window"]);
    assert.equal(calls.length, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("正文被改动后即使有授权也不会发送", async () => {
  const root = await makeRoot();
  try {
    await write(root, P(root).occurrence,
      baseOccurrence({ invocationId: `operator-recovery-${DAY}T024855Z` }));
    await emit(root);
    await authorizeSend({ projectRoot: root, eventId: EVENT, targetDate: DAY,
      payloadSha256: sha256(await readFile(P(root).payload)) });
    await writeFile(P(root).payload, "# 被改动的正文\n", "utf8");
    const { calls, sender } = recordingSender();
    const drained = await drainOutbox(drainOpts(root, sender));
    assert.equal(drained.sent, 0);
    assert.equal(calls.length, 0);
    assert.equal(await exists(P(root).receipt), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("投递入口 deliverRunEvent 同样受绑定约束", async () => {
  const root = await makeRoot();
  try {
    const recovery = baseRun({ startedAt: RECOVERY_START, completedAt: RECOVERY_END });
    await write(root, P(root).run, recovery);
    await write(root, P(root).seal, sealFor(await readFile(P(root).run)));
    const { calls, sender } = recordingSender();
    const result = await deliverRunEvent({
      projectRoot: root,
      exitCode: 0,
      invocationStartedAt: RECOVERY_START,
      invocationEndedAt: RECOVERY_END,
      expectedTargetDate: DAY,
      deliveryMode: "telegram",
      sender
    });
    assert.equal(result.confirmed, false);
    assert.equal(result.finalState, "pending");
    assert.equal(result.drain.awaitingAuthorization, 1);
    assert.equal(calls.length, 0, "换投递入口不得绕过绑定");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("定时日的故障通知仍然自动发送（门禁不得吞掉故障告警）", async () => {
  const root = await makeRoot();
  try {
    const failedRun = baseRun({ status: "failed", phase: "validate",
      publicationGate: "blocked", failedAt: RUN_END,
      failureCode: "content_gate_exhausted", retryable: false });
    delete failedRun.completedAt;
    await write(root, P(root).run, failedRun);
    await write(root, P(root).occurrence, baseOccurrence({ status: "failed", childExitCode: 1 }));
    const failedEvent = `astock-${DAY}-failed-telegram-v1`;
    await emitRunEvent({
      projectRoot: root,
      exitCode: 1,
      invocationStartedAt: RUN_START,
      invocationEndedAt: RUN_END,
      expectedTargetDate: DAY
    });
    const pending = path.join(root, "work", "notifications", "pending", `${failedEvent}.json`);
    const event = JSON.parse(await readFile(pending, "utf8"));
    assert.equal(event.outcome, "failed");
    assert.equal(event.deliveryAuthorization, "scheduled",
      "出自本次认领运行的故障通知应可自动发送");

    const { calls, sender } = recordingSender();
    const drained = await drainOutbox({ projectRoot: root, deliveryMode: "telegram",
      sender, targetEventId: failedEvent });
    assert.equal(drained.sent, 1);
    assert.equal(calls.length, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("发布门禁受阻的运行产出的是故障通知，仍应自动发送", async () => {
  const root = await makeRoot();
  try {
    // exitCode 0 + publicationGate blocked 会被分类为故障通知（不是可发布正文）。
    await write(root, P(root).run, baseRun({ publicationGate: "blocked" }));
    await emitRunEvent({
      projectRoot: root,
      exitCode: 0,
      invocationStartedAt: RUN_START,
      invocationEndedAt: RUN_END,
      expectedTargetDate: DAY
    });
    const failedEvent = `astock-${DAY}-failed-telegram-v1`;
    const event = JSON.parse(await readFile(
      path.join(root, "work", "notifications", "pending", `${failedEvent}.json`), "utf8"));
    assert.equal(event.outcome, "failed");
    const { calls, sender } = recordingSender();
    const drained = await drainOutbox({ projectRoot: root, deliveryMode: "telegram",
      sender, targetEventId: failedEvent });
    assert.equal(drained.sent, 1, "故障通知不应被正文门禁吞掉");
    assert.equal(calls.length, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
