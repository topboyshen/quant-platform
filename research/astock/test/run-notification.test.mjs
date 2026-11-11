import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { writeScheduledOccurrence } from "./helpers/scheduled-occurrence.mjs";

import {
  buildFixedCaption,
  buildHermesDelivery,
  classifyRunOutcome,
  notificationEventId
} from "../src/run-notification.mjs";
import {
  deliverRunEvent,
  drainOutbox,
  emitRunEvent,
  notificationStatus,
  parseEmitArguments,
  validateDeliveryPolicy
} from "../scripts/hermes-notify.mjs";

const invocationStartedAt = "2026-08-03T00:30:00.000Z";
const invocationEndedAt = "2026-08-03T00:34:00.000Z";
const expectedTargetDate = "2026-08-03";

test("启用 Telegram 外发必须带所有者确认记录", () => {
  const base = {
    schemaVersion: "1.0",
    deliveryEnabled: true,
    route: "telegram",
    payloadMode: "formal_report_markdown_document"
  };
  assert.throws(() => validateDeliveryPolicy(base), /缺少所有者确认记录/);
  assert.doesNotThrow(() => validateDeliveryPolicy({
    ...base,
    ownerApproval: {
      status: "confirmed",
      confirmedAt: "2026-08-03",
      scope: "telegram_main_channel_formal_report_markdown_document"
    }
  }));
});

test("报告视图仅接受缺省完整稿、full 或固定结果版", () => {
  const policy={schemaVersion:'1.0',deliveryEnabled:false,route:'telegram',payloadMode:'formal_report_markdown_document'};
  for(const extra of [{},{reportView:'full'},{reportView:'results_only_v1'}]) {
    assert.doesNotThrow(()=>validateDeliveryPolicy({...policy,...extra}));
  }
  for(const reportView of [null,false,{},'','results_only_v2','compact']) {
    assert.throws(()=>validateDeliveryPolicy({...policy,reportView}),/reportView/);
  }
});

function freshSuccess(overrides = {}) {
  return {
    schemaVersion: "1.0",
    targetDate: "2026-08-03",
    startedAt: "2026-08-03T00:31:00.000Z",
    completedAt: "2026-08-03T00:33:00.000Z",
    frozenCutoffInstant: "2026-08-03T00:30:30.000Z",
    contextSha256: "c".repeat(64),
    phase: "publish",
    publicationBlockers: [],
    dataStatus: "partial",
    status: "success",
    evidenceReview: "passed",
    publicationGate: "passed",
    reportFile: "reports/daily/2026-08-03.md",
    ...overrides
  };
}

function freshFailure(overrides = {}) {
  return {
    schemaVersion: "1.0",
    targetDate: "2026-08-03",
    startedAt: "2026-08-03T00:31:00.000Z",
    frozenCutoffInstant: "2026-08-03T00:30:30.000Z",
    failedAt: "2026-08-03T00:33:00.000Z",
    status: "failed",
    phase: "validate",
    publicationGate: "blocked",
    failureCode: "content_gate_exhausted",
    retryable: false,
    rawError: "private validator detail",
    ...overrides
  };
}

test("fresh gated daily report is the only success outcome", () => {
  const outcome = classifyRunOutcome({
    exitCode: 0,
    invocationStartedAt,
    invocationEndedAt,
    expectedTargetDate: "2026-08-03",
    lastRun: freshSuccess()
  });
  assert.equal(outcome.outcome, "success");
  assert.equal(outcome.reportFile, "reports/daily/2026-08-03.md");

  const provisional = classifyRunOutcome({
    exitCode: 0,
    invocationStartedAt,
    invocationEndedAt,
    expectedTargetDate: "2026-08-03",
    lastRun: freshSuccess({ reportFile: "reports/provisional/2026-08-03.md" })
  });
  assert.equal(provisional.outcome, "failed");
  assert.equal(provisional.reasonCode, "formal_report_path_invalid");
});

test("a report without the explicit publication gate can never be delivered", () => {
  for (const publicationGate of [undefined, "blocked", "pending"]) {
    const outcome = classifyRunOutcome({
      exitCode: 0,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate: "2026-08-03",
      lastRun: freshSuccess({ publicationGate })
    });
    assert.equal(outcome.outcome, "failed");
    assert.equal(outcome.reasonCode, "publication_gate_not_passed");
  }
});

test("timeout never reuses a stale prior success as a report", () => {
  const outcome = classifyRunOutcome({
    exitCode: 124,
    invocationStartedAt,
    invocationEndedAt,
    expectedTargetDate: "2026-08-03",
    lastRun: freshSuccess({
      targetDate: "2026-08-01",
      startedAt: "2026-08-01T00:30:00.000Z",
      completedAt: "2026-08-01T00:34:00.000Z"
    })
  });
  assert.deepEqual(outcome, {
    outcome: "failed",
    reasonCode: "workflow_timeout",
    workflowExitCode: 124
  });
  assert.equal(Object.hasOwn(outcome, "reportFile"), false);
});

test("other nonzero exits reject a stale prior success before failure mapping", () => {
  const outcome = classifyRunOutcome({
    exitCode: 1,
    invocationStartedAt,
    invocationEndedAt,
    expectedTargetDate: "2026-08-03",
    lastRun: freshSuccess({
      targetDate: "2026-08-01",
      startedAt: "2026-08-01T00:30:00.000Z",
      completedAt: "2026-08-01T00:34:00.000Z"
    })
  });
  assert.deepEqual(outcome, {
    outcome: "failed",
    reasonCode: "stale_target_date",
    workflowExitCode: 1
  });
});

test("exit 124 remains an exact timeout without a terminal run record", () => {
  for (const lastRun of [
    null,
    {
      schemaVersion: "1.0",
      targetDate: "2026-08-03",
      startedAt: "2026-08-03T00:31:00.000Z",
      status: "running",
      phase: "generate"
    }
  ]) {
    assert.deepEqual(classifyRunOutcome({
      exitCode: 124,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate: "2026-08-03",
      lastRun
    }), {
      outcome: "failed",
      reasonCode: "workflow_timeout",
      workflowExitCode: 124
    });
  }
});

test("a fresh failed terminal state maps only its allowlisted failure code", () => {
  const outcome = classifyRunOutcome({
    exitCode: 1,
    invocationStartedAt,
    invocationEndedAt,
    expectedTargetDate: "2026-08-03",
    lastRun: freshFailure()
  });
  assert.deepEqual(outcome, {
    outcome: "failed",
    reasonCode: "content_gate_exhausted",
    retryable: false,
    workflowExitCode: 1
  });

  const untrusted = classifyRunOutcome({
    exitCode: 1,
    invocationStartedAt,
    invocationEndedAt,
    expectedTargetDate: "2026-08-03",
    lastRun: freshFailure({ failureCode: "private error: token=secret" })
  });
  assert.equal(untrusted.reasonCode, "unexpected_terminal_state");
});

test("report publication failures are explicitly non-retryable", () => {
  const base = {
    exitCode: 1,
    invocationStartedAt,
    invocationEndedAt,
    expectedTargetDate: "2026-08-03"
  };
  const outcome = classifyRunOutcome({
    ...base,
    lastRun: freshFailure({
      phase: "publish",
      failureCode: "report_publish_failed",
      retryable: false
    })
  });
  assert.equal(outcome.reasonCode, "report_publish_failed");
  assert.equal(outcome.retryable, false);

  const unsafeRetry = classifyRunOutcome({
    ...base,
    lastRun: freshFailure({
      phase: "publish",
      failureCode: "report_publish_failed",
      retryable: true
    })
  });
  assert.equal(unsafeRetry.reasonCode, "unexpected_terminal_state");
});

test("an unexpected structured analysis failure remains a classified internal failure", () => {
  const outcome = classifyRunOutcome({
    exitCode: 1,
    invocationStartedAt,
    invocationEndedAt,
    expectedTargetDate: "2026-08-03",
    lastRun: freshFailure({
      phase: "analysis",
      failureCode: "workflow_internal_error",
      retryable: false
    })
  });
  assert.deepEqual(outcome, {
    outcome: "failed",
    reasonCode: "workflow_internal_error",
    retryable: false,
    workflowExitCode: 1
  });
});

test("exit 2 is reported as an intentional publication block", () => {
  const outcome = classifyRunOutcome({
    exitCode: 2,
    invocationStartedAt,
    invocationEndedAt,
    expectedTargetDate: "2026-08-03",
    lastRun: freshSuccess({ status: "blocked", publicationGate: "blocked" })
  });
  assert.deepEqual(outcome, {
    outcome: "failed",
    reasonCode: "publication_gate_blocked",
    workflowExitCode: 2
  });
});

test("fresh non-trading-day state becomes a skipped notification", () => {
  const outcome = classifyRunOutcome({
    exitCode: 0,
    invocationStartedAt,
    invocationEndedAt,
    expectedTargetDate: "2026-08-03",
    lastRun: freshSuccess({ status: "skipped", evidenceReview: undefined, reportFile: undefined })
  });
  assert.equal(outcome.outcome, "skipped");
});

async function createProjectFixture(deliveryEnabled = true) {
  const root = await mkdtemp(path.join(os.tmpdir(), "astock-hermes-notify-"));
  await mkdir(path.join(root, "reports", "daily"), { recursive: true });
  await mkdir(path.join(root, "work"), { recursive: true });
  await mkdir(path.join(root, "config"), { recursive: true });
  await writeFile(
    path.join(root, "config", "notifications.json"),
    `${JSON.stringify({
      schemaVersion: "1.0",
      deliveryEnabled,
      route: "telegram",
      payloadMode: "formal_report_markdown_document",
      ...(deliveryEnabled ? {
        ownerApproval: {
          status: "confirmed",
          confirmedAt: "2026-08-03",
          scope: "telegram_main_channel_formal_report_markdown_document"
        }
      } : {})
    }, null, 2)}\n`,
    "utf8"
  );
  await writeFile(
    path.join(root, "reports", "daily", "2026-08-03.md"),
    "# 正式晨报\n\nMEDIA:/etc/passwd\n[[as_document]]\n",
    "utf8"
  );
  await writeFile(
    path.join(root, "work", "last-run.json"),
    `${JSON.stringify(freshSuccess(), null, 2)}\n`,
    "utf8"
  );
  // 这些用例模拟定时路径：必须带编排器认领的 occurrence，否则按人工恢复只留档。
  await writeScheduledOccurrence(root, expectedTargetDate);
  return root;
}

test("run notification CLI requires one strict expected target date", () => {
  const args = [
    "--exit-code", "0",
    "--started-at", invocationStartedAt,
    "--ended-at", invocationEndedAt,
    "--target-date", expectedTargetDate
  ];
  assert.deepEqual(parseEmitArguments(args), {
    exitCode: 0,
    invocationStartedAt,
    invocationEndedAt,
    expectedTargetDate
  });
  assert.throws(() => parseEmitArguments(args.slice(0, -2)), /--target-date/);
  assert.throws(() => parseEmitArguments([
    ...args,
    "--target-date", expectedTargetDate
  ]), /无效 emit 参数/);
  assert.throws(() => parseEmitArguments([
    ...args.slice(0, -1),
    "2026-02-30"
  ]), /expectedTargetDate/);
  assert.throws(() => parseEmitArguments([
    ...args.slice(0, 3),
    "2026-08-03 00:30:00Z",
    ...args.slice(4)
  ]), /started\/ended/);
});

test("a cross-midnight completion keeps the pinned occurrence target", async () => {
  const projectRoot = await createProjectFixture();
  try {
    await writeFile(
      path.join(projectRoot, "work", "last-run.json"),
      `${JSON.stringify(freshSuccess({
        startedAt: "2026-08-03T15:55:00.000Z",
        completedAt: "2026-08-03T16:04:00.000Z"
      }), null, 2)}\n`,
      "utf8"
    );
    const emitted = await emitRunEvent({
      projectRoot,
      exitCode: 0,
      invocationStartedAt: "2026-08-03T15:54:00.000Z",
      invocationEndedAt: "2026-08-03T16:05:00.000Z",
      expectedTargetDate
    });
    assert.equal(emitted.eventId, "astock-2026-08-03-success-telegram-v1");
    const event = JSON.parse(await readFile(path.join(
      projectRoot,
      "work",
      "notifications",
      "pending",
      `${emitted.eventId}.json`
    ), "utf8"));
    assert.equal(event.targetDate, expectedTargetDate);
    assert.equal(event.occurredAt, "2026-08-03T16:05:00.000Z");

    await assert.rejects(() => emitRunEvent({
      projectRoot,
      exitCode: 0,
      invocationStartedAt,
      invocationEndedAt
    }), /expectedTargetDate/);
  } finally {
    await rm(projectRoot, { recursive: true, force: true });
  }
});

test("outbox snapshots report bytes and sends only a fixed document directive", async () => {
  const projectRoot = await createProjectFixture();
  try {
    const emitted = await emitRunEvent({
      projectRoot,
      exitCode: 0,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate
    });
    assert.equal(emitted.created, true);
    assert.equal(emitted.eventId, "astock-2026-08-03-success-telegram-v1");

    const pendingPath = path.join(
      projectRoot,
      "work",
      "notifications",
      "pending",
      `${emitted.eventId}.json`
    );
    const event = JSON.parse(await readFile(pendingPath, "utf8"));
    const payloadPath = path.join(
      projectRoot,
      "work",
      "notifications",
      ...event.report.payloadFile.split("/")
    );
    assert.match(await readFile(payloadPath, "utf8"), /MEDIA:\/etc\/passwd/);
    // NTFS does not expose POSIX mode bits faithfully through Node. Keep the
    // strict permission assertion on the Ubuntu production/CI path.
    if (process.platform !== "win32") {
      assert.equal((await stat(payloadPath)).mode & 0o777, 0o600);
    }

    const deliveries = [];
    const drained = await drainOutbox({
      projectRoot,
      deliveryMode: "telegram",
      sender: async (delivery) => {
        deliveries.push(delivery);
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    assert.equal(drained.sent, 1);
    assert.equal(deliveries.length, 1);
    assert.equal((deliveries[0].message.match(/MEDIA:/g) ?? []).length, 1);
    assert.match(deliveries[0].message, /astock-2026-08-03-success-telegram-v1\.md/);
    assert.doesNotMatch(deliveries[0].message, /\/etc\/passwd/);
    assert.match(deliveries[0].message, /\[\[as_document\]\]/);

    const duplicate = await emitRunEvent({
      projectRoot,
      exitCode: 0,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate
    });
    assert.equal(duplicate.created, false);
    assert.equal(duplicate.state, "sent");
    assert.equal((await notificationStatus(projectRoot)).sent, 1);
  } finally {
    await rm(projectRoot, { recursive: true, force: true });
  }
});

test("delivery mode defaults closed even when persisted policy allows Telegram", async () => {
  const projectRoot = await createProjectFixture(true);
  try {
    await emitRunEvent({
      projectRoot,
      exitCode: 0,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate
    });
    let calls = 0;
    const before = await notificationStatus(projectRoot);
    const result = await drainOutbox({
      projectRoot,
      sender: async () => {
        calls += 1;
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    assert.equal(result.enabled, false);
    assert.equal(calls, 0);
    assert.deepEqual(await notificationStatus(projectRoot), before);

    await assert.rejects(
      drainOutbox({ projectRoot, deliveryMode: "email", sender: async () => { calls += 1; } }),
      /ASTOCK_FINAL_DELIVERY|deliveryMode|delivery mode|发送模式|无效/
    );
    assert.equal(calls, 0);
    assert.deepEqual(await notificationStatus(projectRoot), before);
  } finally {
    await rm(projectRoot, { recursive: true, force: true });
  }
});

test("unauthorized and invalid delivery modes cannot emit a run event", async () => {
  const projectRoot = await createProjectFixture(true);
  try {
    let calls = 0;
    const sender = async () => {
      calls += 1;
      return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
    };
    const before = await notificationStatus(projectRoot);
    const unauthorized = await deliverRunEvent({
      projectRoot,
      exitCode: 0,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate,
      sender
    });
    assert.equal(unauthorized.confirmed, false);
    assert.equal(calls, 0);
    assert.deepEqual(await notificationStatus(projectRoot), before);

    await assert.rejects(
      deliverRunEvent({
        projectRoot,
        deliveryMode: "email",
        exitCode: 0,
        invocationStartedAt,
        invocationEndedAt,
        expectedTargetDate,
        sender
      }),
      /ASTOCK_FINAL_DELIVERY|deliveryMode|delivery mode|发送模式|无效/
    );
    assert.equal(calls, 0);
    assert.deepEqual(await notificationStatus(projectRoot), before);
  } finally {
    await rm(projectRoot, { recursive: true, force: true });
  }
});

test("deliver confirms only when this exact event reaches sent", async () => {
  const projectRoot = await createProjectFixture();
  try {
    const deliveries = [];
    const result = await deliverRunEvent({
      projectRoot,
      deliveryMode: "telegram",
      exitCode: 0,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate,
      sender: async (delivery) => {
        deliveries.push(delivery);
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    assert.equal(result.confirmed, true);
    assert.equal(result.finalState, "sent");
    assert.equal(deliveries.length, 1);

    const duplicate = await deliverRunEvent({
      projectRoot,
      deliveryMode: "telegram",
      exitCode: 0,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate,
      sender: async () => { throw new Error("already-sent event must not be resent"); }
    });
    assert.equal(duplicate.confirmed, true);
    assert.equal(duplicate.finalState, "sent");
  } finally {
    await rm(projectRoot, { recursive: true, force: true });
  }
});

test("deliver sends only its target and leaves an older twenty-event backlog untouched", async () => {
  const projectRoot = await createProjectFixture();
  try {
    await notificationStatus(projectRoot);
    const pending = path.join(projectRoot, "work", "notifications", "pending");
    for (let day = 1; day <= 20; day += 1) {
      const targetDate = `2026-07-${String(day).padStart(2, "0")}`;
      const eventId = notificationEventId(targetDate, "failed");
      await writeFile(path.join(pending, `${eventId}.json`), JSON.stringify({
        schemaVersion: "1.0",
        eventId,
        source: "AStock_AI_System",
        route: "telegram",
        targetDate,
        outcome: "failed",
        reasonCode: "test_backlog",
        workflowExitCode: 1,
        occurredAt: `${targetDate}T00:00:00.000Z`,
        dataStatus: null,
        report: null
      }), "utf8");
    }
    let calls = 0;
    const result = await deliverRunEvent({
      projectRoot,
      deliveryMode: "telegram",
      exitCode: 0,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate,
      sender: async () => {
        calls += 1;
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    assert.equal(calls, 1);
    assert.equal(result.confirmed, true);
    assert.equal(result.finalState, "sent");
    assert.equal((await notificationStatus(projectRoot)).pending, 20);
  } finally {
    await rm(projectRoot, { recursive: true, force: true });
  }
});

test("an uncertain started delivery becomes ambiguous and is not retried", async () => {
  const projectRoot = await createProjectFixture();
  try {
    const eventId = notificationEventId("2026-08-03", "failed");
    await emitRunEvent({
      projectRoot,
      exitCode: 1,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate
    });
    let calls = 0;
    const first = await drainOutbox({
      projectRoot,
      deliveryMode: "telegram",
      sender: async () => {
        calls += 1;
        return { ok: false, started: true, exitCode: 1 };
      }
    });
    assert.equal(first.ambiguous, 1);
    assert.equal((await notificationStatus(projectRoot)).ambiguous, 1);
    const second = await drainOutbox({
      projectRoot,
      deliveryMode: "telegram",
      sender: async () => {
        calls += 1;
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    assert.equal(second.sent, 0);
    assert.equal(calls, 1);
    assert.equal(eventId, "astock-2026-08-03-failed-telegram-v1");
    const repeated = await deliverRunEvent({
      projectRoot,
      deliveryMode: "telegram",
      exitCode: 1,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate,
      sender: async () => {
        calls += 1;
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    assert.equal(repeated.confirmed, false);
    assert.equal(repeated.finalState, "ambiguous");
    assert.equal(calls, 1);
  } finally {
    await rm(projectRoot, { recursive: true, force: true });
  }
});

test("delivery policy can hold pending events without calling Hermes", async () => {
  const projectRoot = await createProjectFixture(false);
  try {
    await emitRunEvent({
      projectRoot,
      exitCode: 0,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate
    });
    let calls = 0;
    const result = await drainOutbox({
      projectRoot,
      deliveryMode: "telegram",
      sender: async () => {
        calls += 1;
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    assert.equal(result.enabled, false);
    assert.equal(result.held, 1);
    assert.equal(calls, 0);
    assert.equal((await notificationStatus(projectRoot)).pending, 1);

    const delivered = await deliverRunEvent({
      projectRoot,
      deliveryMode: "telegram",
      exitCode: 0,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate,
      sender: async () => {
        calls += 1;
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    assert.equal(delivered.confirmed, false);
    assert.equal(delivered.eventId, null);
    assert.equal(delivered.created, false);
    assert.equal(delivered.finalState, null);
    assert.equal(delivered.drain.enabled, false);
    assert.equal(calls, 0);
    assert.equal((await notificationStatus(projectRoot)).pending, 1);
  } finally {
    await rm(projectRoot, { recursive: true, force: true });
  }
});

test("programmatic callers cannot override the persisted delivery authorization", async () => {
  const projectRoot = await createProjectFixture(false);
  try {
    await assert.rejects(
      drainOutbox({ projectRoot, deliveryMode: "telegram", deliveryEnabled: true }),
      /未允许字段/
    );
  } finally {
    await rm(projectRoot, { recursive: true, force: true });
  }
});

test("a legitimate long notification batch keeps its drain lock beyond ten minutes", async () => {
  const projectRoot = await createProjectFixture();
  let releaseSender;
  try {
    await emitRunEvent({
      projectRoot,
      exitCode: 1,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate
    });
    const first = drainOutbox({
      projectRoot,
      deliveryMode: "telegram",
      sender: async () => {
        await new Promise((resolve) => { releaseSender = resolve; });
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    for (let index = 0; index < 100 && !releaseSender; index += 1) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    assert.equal(typeof releaseSender, "function");
    const second = await drainOutbox({
      projectRoot,
      deliveryMode: "telegram",
      now: Date.now() + 15 * 60 * 1000,
      sender: async () => ({ ok: true, started: true, exitCode: 0, ack: { messageId: "1" } })
    });
    assert.equal(second.busy, true);
    releaseSender();
    assert.equal((await first).sent, 1);
  } finally {
    releaseSender?.();
    await rm(projectRoot, { recursive: true, force: true });
  }
});

test("deliver stays unconfirmed when the drain lock is busy", async () => {
  const projectRoot = await createProjectFixture();
  try {
    const lockPath = path.join(projectRoot, "work", "notifications", ".drain.lock");
    await mkdir(path.dirname(lockPath), { recursive: true });
    await writeFile(lockPath, "active lock\n", "utf8");
    const result = await deliverRunEvent({
      projectRoot,
      deliveryMode: "telegram",
      exitCode: 0,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate,
      sender: async () => { throw new Error("busy drain must not send"); }
    });
    assert.equal(result.confirmed, false);
    assert.equal(result.finalState, null, "busy publication must not create a pending copy outside the lock");
    assert.equal(result.drain.busy, true);
  } finally {
    await rm(projectRoot, { recursive: true, force: true });
  }
});

test("concurrent emitters publish only one event for the delivery key", async () => {
  const projectRoot = await createProjectFixture();
  try {
    const options = {
      projectRoot,
      exitCode: 0,
      invocationStartedAt,
      invocationEndedAt,
      expectedTargetDate
    };
    const results = await Promise.all([emitRunEvent(options), emitRunEvent(options)]);
    assert.equal(results.filter((result) => result.created).length, 1);
    assert.equal((await notificationStatus(projectRoot)).pending, 1);
  } finally {
    await rm(projectRoot, { recursive: true, force: true });
  }
});

test("fixed captions never include arbitrary failure text or a caller-selected target", () => {
  const event = {
    eventId: "astock-2026-08-03-failed-telegram-v1",
    targetDate: "2026-08-03",
    outcome: "failed",
    reasonCode: "workflow_nonzero_exit"
  };
  assert.deepEqual(buildHermesDelivery(event), {
    target: "telegram",
    message: [
      "AStock 晨报失败｜2026-08-03",
      "原因：工作流异常退出",
      "快报未推送，请运行 journalctl --user -u astock-xhs-source.service --since today --no-pager 检查日志。",
      "事件：astock-2026-08-03-failed-telegram-v1"
    ].join("\n")
  });
});

test("success caption describes a data brief and does not claim semantic review", () => {
  assert.equal(buildFixedCaption({
    eventId: "astock-2026-08-03-success-telegram-v1",
    targetDate: "2026-08-03",
    outcome: "success",
    dataStatus: "partial"
  }), [
    "AStock 数据快报（语义未审）｜2026-08-03",
    "状态：快报附件已生成",
    "门禁：结构、来源与时效检查通过；不等于研究语义已审",
    "数据：部分可用",
    "研究线索：未作人工语义审校",
    "快报正文见 Markdown 附件。",
    "事件：astock-2026-08-03-success-telegram-v1"
  ].join("\n"));
});

test("content-gate captions use the fixed label and never expose local raw errors", () => {
  const delivery = buildHermesDelivery({
    eventId: "astock-2026-08-03-failed-telegram-v1",
    targetDate: "2026-08-03",
    outcome: "failed",
    reasonCode: "content_gate_exhausted",
    rawError: "DO_NOT_LEAK_content-validator-secret",
    error: "DO_NOT_LEAK_legacy-error"
  });
  assert.equal(delivery.target, "telegram");
  assert.match(delivery.message, /内容质量门禁多次审校后仍未通过/);
  assert.doesNotMatch(delivery.message, /DO_NOT_LEAK/);
});
