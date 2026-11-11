import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  mkdir,
  mkdtemp,
  readFile,
  rename,
  rm,
  symlink,
  unlink,
  writeFile
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { writeScheduledOccurrence } from "./helpers/scheduled-occurrence.mjs";

import { correctionEventId } from "../src/run-notification.mjs";
import {
  deliverCorrectionEvent,
  drainCorrectionEvent,
  drainOutbox,
  emitCorrectionEvent,
  emitRunEvent,
  notificationStatus
} from "../scripts/hermes-notify.mjs";

const targetDate = "2026-08-03";
const revision = "v1";
const invocationStartedAt = "2026-08-03T00:30:00.000Z";
const invocationEndedAt = "2026-08-03T00:34:00.000Z";

function digest(body) {
  return createHash("sha256").update(body).digest("hex");
}

async function writeGate(root, overrides = {}) {
  const reportFile = `reports/corrections/${targetDate}-${revision}.md`;
  const contextFile = `work/corrections/evidence/${targetDate}-${revision}.json`;
  const report = await readFile(path.join(root, ...reportFile.split("/")));
  const context = await readFile(path.join(root, ...contextFile.split("/")));
  const gate = {
    schemaVersion: "1.0",
    eventType: "correction_gate",
    targetDate,
    revision,
    evidenceCutoffIso: "2026-08-03T00:25:00.000Z",
    contextMetaAsOf: "2026-08-03T00:20:00.000Z",
    contextFile,
    contextSha256: digest(context),
    contextBytes: context.length,
    reportFile,
    reportSha256: digest(report),
    reportBytes: report.length,
    publicationAudit: { passed: true, reviewer: "test", result: "passed" },
    passed: true,
    createdAt: "2026-08-03T00:29:00.000Z",
    ...overrides
  };
  await writeFile(
    path.join(root, "work", "corrections", "gates", `${targetDate}-${revision}.json`),
    `${JSON.stringify(gate, null, 2)}\n`,
    "utf8"
  );
  return gate;
}

async function createFixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), "astock-correction-notify-"));
  for (const relative of [
    "config",
    "reports/daily",
    "reports/corrections",
    "work/corrections/evidence",
    "work/corrections/gates"
  ]) {
    await mkdir(path.join(root, ...relative.split("/")), { recursive: true });
  }
  await writeFile(path.join(root, "config", "notifications.json"), `${JSON.stringify({
    schemaVersion: "1.0",
    deliveryEnabled: true,
    route: "telegram",
    payloadMode: "formal_report_markdown_document",
    ownerApproval: {
      status: "confirmed",
      confirmedAt: "2026-08-03",
      scope: "telegram_main_channel_formal_report_markdown_document"
    }
  }, null, 2)}\n`, "utf8");
  await writeFile(path.join(root, "reports", "daily", `${targetDate}.md`), "# 原始晨报\n", "utf8");
  await writeFile(path.join(root, "work", "last-run.json"), `${JSON.stringify({
    schemaVersion: "1.0",
    targetDate,
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
    reportFile: `reports/daily/${targetDate}.md`
  }, null, 2)}\n`, "utf8");
  await writeScheduledOccurrence(root, targetDate);
  await emitRunEvent({
    projectRoot: root,
    exitCode: 0,
    invocationStartedAt,
    invocationEndedAt,
    expectedTargetDate: targetDate
  });
  await drainOutbox({
    projectRoot: root,
    deliveryMode: "telegram",
    sender: async () => ({ ok: true, started: true, exitCode: 0, ack: { messageId: "1" } })
  });
  await writeFile(
    path.join(root, "work", "corrections", "evidence", `${targetDate}-${revision}.json`),
    "{\"evidence\":\"fixed snapshot\"}\n",
    "utf8"
  );
  await writeFile(
    path.join(root, "reports", "corrections", `${targetDate}-${revision}.md`),
    "# 更正版\n\n修正后的正文。\n",
    "utf8"
  );
  await writeGate(root);
  return root;
}

test("unauthorized and invalid delivery modes cannot emit a correction", async () => {
  const root = await createFixture();
  try {
    let calls = 0;
    const sender = async () => {
      calls += 1;
      return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
    };
    const before = await notificationStatus(root);
    const unauthorized = await deliverCorrectionEvent({
      projectRoot: root,
      targetDate,
      revision,
      sender
    });
    assert.equal(unauthorized.confirmed, false);
    assert.equal(calls, 0);
    assert.deepEqual(await notificationStatus(root), before);

    await assert.rejects(
      deliverCorrectionEvent({
        projectRoot: root,
        deliveryMode: "email",
        targetDate,
        revision,
        sender
      }),
      /ASTOCK_FINAL_DELIVERY|deliveryMode|delivery mode|发送模式|无效/
    );
    assert.equal(calls, 0);
    assert.deepEqual(await notificationStatus(root), before);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("correction drain requires both runtime mode and persisted policy", async () => {
  const root = await createFixture();
  try {
    await emitCorrectionEvent({
      projectRoot: root,
      targetDate,
      revision,
      occurredAt: "2026-08-03T01:10:00.000Z"
    });
    let calls = 0;
    const sender = async () => {
      calls += 1;
      return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
    };
    const before = await notificationStatus(root);
    const runtimeClosed = await drainCorrectionEvent({
      projectRoot: root,
      targetDate,
      revision,
      sender
    });
    assert.equal(runtimeClosed.enabled, false);
    assert.equal(runtimeClosed.held, 1);
    assert.equal(calls, 0);
    assert.deepEqual(await notificationStatus(root), before);

    await assert.rejects(
      drainCorrectionEvent({
        projectRoot: root,
        deliveryMode: "email",
        targetDate,
        revision,
        sender
      }),
      /ASTOCK_FINAL_DELIVERY|deliveryMode|delivery mode|发送模式|无效/
    );
    assert.equal(calls, 0);
    assert.deepEqual(await notificationStatus(root), before);

    await writeFile(path.join(root, "config", "notifications.json"), `${JSON.stringify({
      schemaVersion: "1.0",
      deliveryEnabled: false,
      route: "telegram",
      payloadMode: "formal_report_markdown_document"
    }, null, 2)}\n`, "utf8");
    const policyClosed = await drainCorrectionEvent({
      projectRoot: root,
      deliveryMode: "telegram",
      targetDate,
      revision,
      sender
    });
    assert.equal(policyClosed.enabled, false);
    assert.equal(policyClosed.held, 1);
    assert.equal(calls, 0);
    assert.deepEqual(await notificationStatus(root), before);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("persisted policy false blocks correction sending even with Telegram mode", async () => {
  const root = await createFixture();
  try {
    await writeFile(path.join(root, "config", "notifications.json"), `${JSON.stringify({
      schemaVersion: "1.0",
      deliveryEnabled: false,
      route: "telegram",
      payloadMode: "formal_report_markdown_document"
    }, null, 2)}\n`, "utf8");
    let calls = 0;
    const before = await notificationStatus(root);
    const result = await deliverCorrectionEvent({
      projectRoot: root,
      deliveryMode: "telegram",
      targetDate,
      revision,
      sender: async () => {
        calls += 1;
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    assert.equal(result.confirmed, false);
    assert.equal(result.eventId, null);
    assert.equal(result.created, false);
    assert.equal(result.finalState, null);
    assert.equal(result.drain.enabled, false);
    assert.equal(calls, 0);
    assert.deepEqual(await notificationStatus(root), before);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("correction delivery snapshots, receipts, and deduplicates the exact event", async () => {
  const root = await createFixture();
  try {
    const deliveries = [];
    const first = await deliverCorrectionEvent({
      projectRoot: root,
      deliveryMode: "telegram",
      targetDate,
      revision,
      occurredAt: "2026-08-03T01:10:00.000Z",
      drainNow: Date.parse("2026-08-03T01:10:01.000Z"),
      sender: async (delivery) => {
        deliveries.push(delivery);
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    assert.equal(first.confirmed, true);
    assert.equal(first.finalState, "sent");
    assert.equal(deliveries.length, 1);
    assert.match(deliveries[0].message, /更正版，原稿保留供审计/);
    assert.equal((deliveries[0].message.match(/MEDIA:/g) ?? []).length, 1);

    const eventId = correctionEventId(targetDate, revision);
    const event = JSON.parse(await readFile(
      path.join(root, "work", "notifications", "sent", `${eventId}.json`),
      "utf8"
    ));
    assert.equal(event.eventType, "correction");
    assert.equal(event.supersedesEventId, `astock-${targetDate}-success-telegram-v1`);
    assert.match(event.originalPayloadSha256, /^[a-f0-9]{64}$/);
    assert.match(event.gateReceiptSha256, /^[a-f0-9]{64}$/);

    const receipt = JSON.parse(await readFile(
      path.join(root, "work", "notifications", "receipts", `${eventId}.json`),
      "utf8"
    ));
    assert.equal(receipt.ackKind, "hermes_cli_exit_0");
    assert.equal(receipt.hermesExitCode, 0);
    assert.equal(receipt.payloadBytes, event.report.bytes);
    assert.equal("message_id" in receipt, false);

    const duplicate = await deliverCorrectionEvent({
      projectRoot: root,
      deliveryMode: "telegram",
      targetDate,
      revision,
      sender: async () => { throw new Error("sent correction must not be resent"); }
    });
    assert.equal(duplicate.confirmed, true);
    assert.equal(deliveries.length, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("results-only policy never changes correction documents or their receipt contract", async () => {
  const root = await createFixture();
  try {
    const policyPath = path.join(root, "config", "notifications.json");
    const policy = JSON.parse(await readFile(policyPath, "utf8"));
    await writeFile(policyPath, JSON.stringify({ ...policy, reportView: "results_only_v1" }));
    const delivered = await deliverCorrectionEvent({
      projectRoot: root, deliveryMode: "telegram", targetDate, revision,
      occurredAt: "2026-08-03T01:10:00.000Z",
      drainNow: Date.parse("2026-08-03T01:10:01.000Z"),
      sender: async () => ({ ok: true, started: true, exitCode: 0, ack: { messageId: "1" } })
    });
    assert.equal(delivered.confirmed, true);
    const eventId = correctionEventId(targetDate, revision);
    const event = JSON.parse(await readFile(path.join(root, "work", "notifications", "sent", `${eventId}.json`), "utf8"));
    const original = await readFile(path.join(root, "reports", "corrections", `${targetDate}-${revision}.md`));
    const payload = await readFile(path.join(root, "work", "notifications", "payloads", `${eventId}.md`));
    assert.deepEqual(payload, original);
    assert.equal(event.report.sha256, digest(original));
    assert.equal(event.report.presentation, undefined);
    assert.equal(event.report.sourceSha256, undefined);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("correction refuses a missing original and a corrupted original payload", async (t) => {
  await t.test("missing original", async () => {
    const root = await createFixture();
    try {
      await unlink(path.join(
        root,
        "work",
        "notifications",
        "sent",
        `astock-${targetDate}-success-telegram-v1.json`
      ));
      await assert.rejects(
        deliverCorrectionEvent({
          projectRoot: root,
          deliveryMode: "telegram",
          targetDate,
          revision,
          sender: async () => assert.fail()
        })
      );
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
  await t.test("bad original payload hash", async () => {
    const root = await createFixture();
    try {
      await writeFile(path.join(
        root,
        "work",
        "notifications",
        "payloads",
        `astock-${targetDate}-success-telegram-v1.md`
      ), "tampered\n", "utf8");
      await assert.rejects(
        deliverCorrectionEvent({
          projectRoot: root,
          deliveryMode: "telegram",
          targetDate,
          revision,
          sender: async () => assert.fail()
        }),
        /hash\/size/
      );
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
});

test("correction refuses a gate whose context hash no longer matches", async () => {
  const root = await createFixture();
  try {
    await writeFile(
      path.join(root, "work", "corrections", "evidence", `${targetDate}-${revision}.json`),
      "tampered context\n",
      "utf8"
    );
    await assert.rejects(
      deliverCorrectionEvent({
        projectRoot: root,
        deliveryMode: "telegram",
        targetDate,
        revision,
        sender: async () => assert.fail()
      }),
      /evidence snapshot/
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("correction rejects a fixed report symlink even when its bytes match", {
  skip: process.platform === "win32"
}, async () => {
  const root = await createFixture();
  try {
    const directory = path.join(root, "reports", "corrections");
    const fixedPath = path.join(directory, `${targetDate}-${revision}.md`);
    const body = await readFile(fixedPath);
    await writeFile(path.join(directory, "same-bytes.md"), body);
    await unlink(fixedPath);
    await symlink("same-bytes.md", fixedPath);
    await assert.rejects(
      deliverCorrectionEvent({
        projectRoot: root,
        deliveryMode: "telegram",
        targetDate,
        revision,
        sender: async () => assert.fail()
      }),
      /固定产物不是普通文件/
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a started correction failure becomes ambiguous and is never retried", async () => {
  const root = await createFixture();
  try {
    let calls = 0;
    const first = await deliverCorrectionEvent({
      projectRoot: root,
      deliveryMode: "telegram",
      targetDate,
      revision,
      sender: async () => {
        calls += 1;
        return { ok: false, started: true, exitCode: 1, timedOut: false };
      }
    });
    assert.equal(first.finalState, "ambiguous");
    const second = await deliverCorrectionEvent({
      projectRoot: root,
      deliveryMode: "telegram",
      targetDate,
      revision,
      sender: async () => {
        calls += 1;
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    assert.equal(second.finalState, "ambiguous");
    assert.equal(second.confirmed, false);
    assert.equal(calls, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("an explicitly unstarted sender may safely retry the same pending correction", async () => {
  const root = await createFixture();
  try {
    let calls = 0;
    const first = await deliverCorrectionEvent({
      projectRoot: root,
      deliveryMode: "telegram",
      targetDate,
      revision,
      sender: async () => {
        calls += 1;
        return { ok: false, started: false, exitCode: null, timedOut: false };
      }
    });
    assert.equal(first.finalState, "pending");
    const second = await deliverCorrectionEvent({
      projectRoot: root,
      deliveryMode: "telegram",
      targetDate,
      revision,
      sender: async () => {
        calls += 1;
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    assert.equal(second.confirmed, true);
    assert.equal(calls, 2);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a valid receipt recovers stale correction sending without another send", async () => {
  const root = await createFixture();
  try {
    const eventId = correctionEventId(targetDate, revision);
    const initialNow = Date.now();
    const first = await deliverCorrectionEvent({
      projectRoot: root,
      deliveryMode: "telegram",
      targetDate,
      revision,
      drainNow: initialNow,
      sender: async () => ({ ok: true, started: true, exitCode: 0, ack: { messageId: "1" } })
    });
    assert.equal(first.confirmed, true);
    await rename(
      path.join(root, "work", "notifications", "sent", `${eventId}.json`),
      path.join(root, "work", "notifications", "sending", `${eventId}.json`)
    );
    const recovered = await deliverCorrectionEvent({
      projectRoot: root,
      deliveryMode: "telegram",
      targetDate,
      revision,
      drainNow: initialNow + 11 * 60 * 1000,
      sender: async () => { throw new Error("receipt recovery must not send"); }
    });
    assert.equal(recovered.confirmed, true);
    assert.equal(recovered.drain.recovered, 1);
    assert.equal(recovered.finalState, "sent");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a sent acknowledgement survives a final state-move failure for receipt recovery", async () => {
  const root = await createFixture();
  try {
    const eventId = correctionEventId(targetDate, revision);
    const sentDirectory = path.join(root, "work", "notifications", "sent");
    const initialNow = Date.now();
    const first = await deliverCorrectionEvent({
      projectRoot: root,
      deliveryMode: "telegram",
      targetDate,
      revision,
      drainNow: initialNow,
      sender: async () => {
        await rm(sentDirectory, { recursive: true, force: true });
        await writeFile(sentDirectory, "blocks the final rename\n", "utf8");
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    assert.equal(first.confirmed, false);
    assert.equal(first.finalState, "sending");
    assert.equal(first.drain.deferred, 1);
    assert.equal(JSON.parse(await readFile(
      path.join(root, "work", "notifications", "receipts", `${eventId}.json`),
      "utf8"
    )).ackKind, "hermes_cli_exit_0");

    await unlink(sentDirectory);
    await mkdir(sentDirectory);
    const recovered = await deliverCorrectionEvent({
      projectRoot: root,
      deliveryMode: "telegram",
      targetDate,
      revision,
      drainNow: initialNow + 11 * 60 * 1000,
      sender: async () => { throw new Error("durably acknowledged event must not be resent"); }
    });
    assert.equal(recovered.confirmed, true);
    assert.equal(recovered.drain.recovered, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a correction receipt prevents resend when its sent event is rolled back to pending", async () => {
  const root = await createFixture();
  try {
    const eventId = correctionEventId(targetDate, revision);
    let calls = 0;
    const sender = async () => ({ ok: true, started: true, exitCode: 0,
      ack: { messageId: String(++calls) } });
    const first = await deliverCorrectionEvent({ projectRoot: root, deliveryMode: "telegram",
      targetDate, revision, sender });
    assert.equal(first.confirmed, true);
    const receiptPath = path.join(root, "work", "notifications", "receipts", `${eventId}.json`);
    const before = await readFile(receiptPath);
    await rename(path.join(root, "work", "notifications", "sent", `${eventId}.json`),
      path.join(root, "work", "notifications", "pending", `${eventId}.json`));
    const next = await drainCorrectionEvent({ projectRoot: root, deliveryMode: "telegram",
      targetDate, revision, sender });
    assert.equal(next.deferred, 1);
    assert.equal(next.sent, 0);
    assert.equal(calls, 1);
    assert.deepEqual(await readFile(receiptPath), before);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("ordinary drain never selects a correction event", async () => {
  const root = await createFixture();
  try {
    const emitted = await emitCorrectionEvent({ projectRoot: root, targetDate, revision });
    assert.equal(emitted.state, "pending");
    let calls = 0;
    const drained = await drainOutbox({
      projectRoot: root,
      deliveryMode: "telegram",
      sender: async () => {
        calls += 1;
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    assert.equal(drained.sent, 0);
    assert.equal(calls, 0);
    const eventId = correctionEventId(targetDate, revision);
    assert.equal(
      JSON.parse(await readFile(
        path.join(root, "work", "notifications", "pending", `${eventId}.json`),
        "utf8"
      )).eventType,
      "correction"
    );
    await rename(
      path.join(root, "work", "notifications", "pending", `${eventId}.json`),
      path.join(root, "work", "notifications", "sending", `${eventId}.json`)
    );
    const staleDrain = await drainOutbox({
      projectRoot: root,
      deliveryMode: "telegram",
      now: Date.now() + 11 * 60 * 1000,
      sender: async () => {
        calls += 1;
        return { ok: true, started: true, exitCode: 0, ack: { messageId: "1" } };
      }
    });
    assert.equal(staleDrain.ambiguous, 0);
    assert.equal(calls, 0);
    assert.equal(JSON.parse(await readFile(
      path.join(root, "work", "notifications", "sending", `${eventId}.json`),
      "utf8"
    )).eventType, "correction");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
