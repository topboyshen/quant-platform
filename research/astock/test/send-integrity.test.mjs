import test, { mock } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import { createHash } from "node:crypto";
import { copyFile, mkdir, mkdtemp, readFile, realpath, rename, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { authorizeSend, drainOutbox, emitRunEvent, notificationStatus, publishSendReceipt }
  from "../scripts/hermes-notify.mjs";
import { deliverSealedMorning } from "../scripts/deliver-morning-report.mjs";

// Every send-capable call below receives an in-memory fake. Only synthetic
// files under the individual temporary root are read or modified.
const DAY = "2026-09-28";
const START = `${DAY}T00:00:00.000Z`;
const RUN_START = `${DAY}T00:00:01.000Z`;
const RUN_END = `${DAY}T00:00:30.000Z`;
const END = `${DAY}T00:00:31.000Z`;
const NOW = new Date(`${DAY}T00:01:00.000Z`);
const EVENT = `astock-${DAY}-success-telegram-v1`;
const hash = (value) => createHash("sha256").update(value).digest("hex");
const encode = (value) => JSON.stringify(value, null, 2) + "\n";
async function put(root, relative, value) {
  const file = path.join(root, relative);
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, typeof value === "string" ? value : encode(value));
}
async function fixture(t) {
  const root = await realpath(await mkdtemp(path.join(os.tmpdir(), "send-integrity-")));
  t.after(() => rm(root, { recursive: true, force: true }));
  const report = "# Synthetic report\n";
  const reportSha = hash(report);
  const occurrence = { schemaVersion: "1.0", task: "morning-report", targetDate: DAY,
    status: "success", invocationId: "6f1c2f0e-8b1a-4d5e-9c77-2a4b6d8e0f13",
    startedAt: START, completedAt: END, childExitCode: 0, childSignal: null,
    timedOut: false, interrupted: false };
  const run = { schemaVersion: "1.0", targetDate: DAY, status: "success", phase: "publish",
    startedAt: RUN_START, completedAt: RUN_END, frozenCutoffInstant: START,
    dataStatus: "partial", publicationGate: "passed", evidenceReview: "passed",
    publicationBlockers: [], reportFile: `reports/daily/${DAY}.md`, contextSha256: hash("synthetic context") };
  const seal = { schemaVersion: "astock.research-seal.v1", kind: "research_seal", targetDate: DAY,
    state: "SEALED", generatedAt: END, source: { lastRunFile: "work/last-run.json",
      lastRunSha256: hash(encode(run)), reportFile: run.reportFile, reportSha256: reportSha,
      publicationGate: "passed", evidenceReview: "passed", frozenCutoffInstant: START,
      contextSha256: run.contextSha256 } };
  await put(root, "config/notifications.json", { schemaVersion: "1.0", deliveryEnabled: true,
    route: "telegram", payloadMode: "formal_report_markdown_document", ownerApproval: {
      status: "confirmed", confirmedAt: DAY, scope: "telegram_main_channel_formal_report_markdown_document" } });
  await put(root, run.reportFile, report);
  await put(root, "work/last-run.json", run);
  await put(root, `work/orchestrator/states/morning-report-${DAY}.json`, occurrence);
  const sealPath = `work/research-seal/${DAY}/${reportSha}.json`;
  await put(root, sealPath, seal);
  let sends = 0;
  const sender = async () => ({ ok: true, started: true, exitCode: 0, ack: { messageId: String(++sends) } });
  return { root, report, reportSha, run, occurrence, seal, sealPath, sender, count: () => sends,
    pending: path.join(root, `work/notifications/pending/${EVENT}.json`),
    sent: path.join(root, `work/notifications/sent/${EVENT}.json`),
    receipt: path.join(root, `work/notifications/receipts/send/${EVENT}.json`) };
}
const emit = (f, exitCode = 0) => emitRunEvent({ projectRoot: f.root, exitCode,
  invocationStartedAt: START, invocationEndedAt: END, expectedTargetDate: DAY });
const drain = (f, extra = {}) => drainOutbox({ projectRoot: f.root, deliveryMode: "telegram", sender: f.sender, ...extra });

test("an emitter paused at publication excludes another emitter and a drain, then stays terminal", async (t) => {
  const f = await fixture(t);
  let entered, release;
  const atPublication = new Promise((resolve) => { entered = resolve; });
  const continuePublication = new Promise((resolve) => { release = resolve; });
  const originalLink = fs.promises.link;
  let publications = 0;
  const replacement = mock.method(fs.promises, "link", async (from, to) => {
    if (to === f.pending && ++publications === 1) {
      entered();
      await continuePublication;
    }
    return originalLink(from, to);
  });
  syncBuiltinESMExports();
  const first = emit(f);
  try {
    await atPublication;
    assert.equal((await emit(f)).busy, true, "second publisher must share the consumer lock");
    assert.equal((await drain(f)).busy, true);
    release();
    assert.equal((await first).created, true);
    assert.equal((await drain(f)).sent, 1);
    const originalReceipt = await readFile(f.receipt);
    assert.equal((await emit(f)).state, "sent");
    assert.equal((await drain(f)).sent, 0);
    assert.equal(f.count(), 1);
    assert.equal(publications, 1);
    assert.deepEqual(await readFile(f.receipt), originalReceipt);
  } finally {
    release();
    await first.catch(() => {});
    replacement.mock.restore();
    syncBuiltinESMExports();
  }
});

test("bulk drain preserves terminal duplicates and the first platform receipt", async (t) => {
  const f = await fixture(t);
  await emit(f);
  assert.equal((await drain(f)).sent, 1);
  const receipt = await readFile(f.receipt);
  const terminal = await readFile(f.sent);
  await copyFile(f.sent, f.pending); // Model a pre-upgrade late publisher's two-state residue.
  const result = await drain(f);
  assert.equal(result.sent, 0);
  assert.equal(result.deferred, 1);
  assert.equal(f.count(), 1);
  assert.deepEqual(await readFile(f.receipt), receipt);
  assert.deepEqual(await readFile(f.sent), terminal);
  assert.deepEqual(await readFile(f.pending), terminal);
});

test("a durable send receipt alone prevents replay after a state-file rollback", async (t) => {
  const f = await fixture(t);
  await emit(f);
  await drain(f);
  const receipt = await readFile(f.receipt);
  await rename(f.sent, f.pending);
  assert.equal((await drain(f)).deferred, 1);
  assert.equal(f.count(), 1);
  assert.deepEqual(await readFile(f.receipt), receipt);
});

test("publishSendReceipt cannot overwrite an earlier platform acknowledgement", async (t) => {
  const f = await fixture(t);
  await emit(f);
  await drain(f);
  const before = await readFile(f.receipt);
  const event = JSON.parse(await readFile(f.sent, "utf8"));
  await assert.rejects(publishSendReceipt({ sendReceipts: path.dirname(f.receipt) }, event,
    { messageId: "999" }, NOW.toISOString(), { sha256: event.report.sha256, bytes: event.report.bytes }), /不允许覆盖/);
  assert.deepEqual(await readFile(f.receipt), before);
});

test("bulk drain refuses a filename containing a different valid event", async (t) => {
  const f = await fixture(t);
  await emit(f);
  const failed = path.join(path.dirname(f.pending), `astock-${DAY}-failed-telegram-v1.json`);
  await rename(f.pending, failed);
  const result = await drain(f);
  assert.equal(result.dead, 1);
  assert.equal(f.count(), 0);
});

test("receipt persistence before a failed sent transition cannot cause automatic resend", async (t) => {
  const f = await fixture(t);
  await emit(f);
  await assert.rejects(drain(f, { sender: async (...args) => {
    const result = await f.sender(...args);
    await mkdir(f.sent); // Failure after remote acceptance, before the sent transition.
    return result;
  } }));
  const receipt = await readFile(f.receipt);
  await rm(f.sent, { recursive: true });
  const result = await drain(f, { now: Date.now() + 11 * 60_000 });
  assert.equal(result.sent, 0);
  assert.equal(f.count(), 1);
  assert.deepEqual(await readFile(f.receipt), receipt);
  assert.equal((await notificationStatus(f.root)).ambiguous, 1);
});

test("every automatic entry applies the same research seal proof", async (t) => {
  const mutations = {
    "wrong context": (s) => { s.source.contextSha256 = hash("another context"); },
    "missing context": (s) => { delete s.source.contextSha256; },
    "wrong cutoff": (s) => { s.source.frozenCutoffInstant = `${DAY}T00:00:02.000Z`; },
    "missing cutoff": (s) => { delete s.source.frozenCutoffInstant; },
    "wrong kind": (s) => { s.kind = "xhs_draft"; },
    "wrong schema": (s) => { s.schemaVersion = "astock.xhs-draft.v1"; s.state = "DRAFT_ONLY"; }
  };
  for (const [name, mutate] of Object.entries(mutations)) {
    await t.test(name, async (subtest) => {
      const f = await fixture(subtest);
      mutate(f.seal);
      await put(f.root, f.sealPath, f.seal);
      const checked = await deliverSealedMorning({ projectRoot: f.root, now: NOW,
        deliveryMode: "telegram", checkOnly: true, sender: f.sender });
      assert.equal(checked.reason, "sealed_report_proof_invalid");
      await emit(f);
      const result = await drain(f);
      assert.equal(result.sent, 0);
      assert.equal(result.awaitingAuthorization, 1);
      assert.equal(f.count(), 0);
    });
  }
});

test("proof changed after enqueue is rechecked at generic drain", async (t) => {
  const f = await fixture(t);
  await emit(f);
  f.seal.source.contextSha256 = hash("changed after queue");
  await put(f.root, f.sealPath, f.seal);
  assert.equal((await drain(f)).awaitingAuthorization, 1);
  assert.equal(f.count(), 0);
});

test("explicit recovery authorization cannot bypass a changed seal proof", async (t) => {
  const f = await fixture(t);
  f.occurrence.invocationId = "operator-recovery";
  await put(f.root, `work/orchestrator/states/morning-report-${DAY}.json`, f.occurrence);
  await emit(f);
  await authorizeSend({ projectRoot: f.root, eventId: EVENT, targetDate: DAY, payloadSha256: f.reportSha });
  f.seal.source.contextSha256 = hash("changed after authorization");
  await put(f.root, f.sealPath, f.seal);
  const result = await drain(f);
  assert.equal(result.sent, 0);
  assert.equal(result.awaitingAuthorization, 1);
  assert.equal(f.count(), 0);
  const authorization = JSON.parse(await readFile(path.join(f.root,
    `work/notifications/authorizations/${EVENT}.json`), "utf8"));
  assert.equal(authorization.consumedAt, null);
});

test("stale recovery does not overwrite an existing ambiguous copy", async (t) => {
  const f = await fixture(t);
  await emit(f);
  await drain(f, { sender: async () => ({ ok: false, started: true, exitCode: null }) });
  const ambiguous = path.join(f.root, `work/notifications/ambiguous/${EVENT}.json`);
  const before = await readFile(ambiguous);
  const sending = path.join(f.root, `work/notifications/sending/${EVENT}.json`);
  await put(f.root, `work/notifications/sending/${EVENT}.json`, "conflicting stale copy");
  await drain(f, { now: Date.now() + 11 * 60_000 });
  assert.deepEqual(await readFile(ambiguous), before);
  assert.equal(await readFile(sending, "utf8"), "conflicting stale copy");
  assert.equal(f.count(), 0);
});

test("real failed, blocked and skipped terminal records can deliver only their bound notice", async (t) => {
  for (const status of ["failed", "blocked", "skipped"]) {
    await t.test(status, async (subtest) => {
      const f = await fixture(subtest);
      f.occurrence.status = f.run.status = status;
      f.occurrence.childExitCode = status === "failed" ? 1 : status === "blocked" ? 2 : 0;
      if (status === "failed") {
        delete f.run.completedAt;
        Object.assign(f.run, { failedAt: RUN_END, phase: "validate", publicationGate: "blocked",
          failureCode: "content_gate_exhausted", retryable: false });
      }
      if (status === "blocked") f.run.publicationGate = "blocked";
      await put(f.root, "work/last-run.json", f.run);
      await put(f.root, `work/orchestrator/states/morning-report-${DAY}.json`, f.occurrence);
      const event = await emit(f, f.occurrence.childExitCode);
      assert.match(event.eventId, status === "skipped" ? /-skipped-/ : /-failed-/);
      const result = await drain(f);
      assert.equal(result.sent, 1);
      assert.equal(f.count(), 1);
    });
  }
});

test("failedAt outside the claimed run and recovery identities still require authorization", async (t) => {
  for (const mismatch of ["time", "identity"]) {
    await t.test(mismatch, async (subtest) => {
      const f = await fixture(subtest);
      Object.assign(f.run, { status: "failed", phase: "validate", publicationGate: "blocked",
        failedAt: mismatch === "time" ? `${DAY}T00:05:00.000Z` : RUN_END,
        failureCode: "content_gate_exhausted", retryable: false });
      delete f.run.completedAt;
      f.occurrence.status = "failed";
      f.occurrence.childExitCode = 1;
      if (mismatch === "identity") f.occurrence.invocationId = "operator-recovery";
      await put(f.root, "work/last-run.json", f.run);
      await put(f.root, `work/orchestrator/states/morning-report-${DAY}.json`, f.occurrence);
      await emit(f, 1);
      const result = await drain(f);
      assert.equal(result.sent, 0);
      assert.equal(result.awaitingAuthorization, 1);
      assert.equal(f.count(), 0);
    });
  }
});
