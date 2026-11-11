import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {access, lstat, mkdir, mkdtemp, readFile, readdir, realpath, rm, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {SCHEDULED_INVOCATION} from './helpers/scheduled-occurrence.mjs';
import {deliverSealedMorning, prepareSealedMorning} from '../scripts/deliver-morning-report.mjs';
import {deliverRunEvent, drainOutbox, emitRunEvent, sentConfirmation} from '../scripts/hermes-notify.mjs';
import {buildResultView, RESULT_VIEW_VERSION} from '../src/result-view.mjs';
import {buildV3Brief, V3_VERSION} from '../src/result-view-v3.mjs';

const DAY = '2026-09-18';
const NOW = new Date(`${DAY}T01:15:00Z`);
// Presentation must never change the established daily delivery identity.
const EVENT = `astock-${DAY}-success-telegram-v1`;
const SOURCE = await readFile(new URL('./fixtures/research-brief-normal-2026-09-18.md', import.meta.url), 'utf8');
const hash = value => createHash('sha256').update(value).digest('hex');
const json = value => `${JSON.stringify(value, null, 2)}\n`;
const deferredSender = async () => ({ok: false, started: false});
const eventFile = (f, state) => path.join(f.root, `work/notifications/${state}/${EVENT}.json`);
const payloadFile = f => path.join(f.root, `work/notifications/payloads/${EVENT}.md`);
const receiptFile = f => path.join(f.root, `work/notifications/receipts/send/${EVENT}.json`);
const readJson = async file => JSON.parse(await readFile(file, 'utf8'));
const missing = async file => assert.rejects(access(file), {code: 'ENOENT'});

async function treeSnapshot(root, relative = '') {
  const entries = [];
  for (const name of (await readdir(path.join(root, relative))).sort()) {
    const rel = path.join(relative, name), full = path.join(root, rel), info = await lstat(full);
    entries.push({path: rel, mode: info.mode, mtimeMs: info.mtimeMs,
      ...(info.isDirectory() ? {directory: true} : {sha256: hash(await readFile(full))})});
    if (info.isDirectory()) entries.push(...await treeSnapshot(root, rel));
  }
  return entries;
}

// Public historical report bytes are real; all runs, proofs, outbox state and
// platform receipts below are explicitly synthetic and confined to a temp tree.
async function fixture(t, {view = V3_VERSION, report = SOURCE} = {}) {
  const root = await realpath(await mkdtemp(path.join(os.tmpdir(), 'v3-delivery-test-')));
  t.after(() => rm(root, {recursive: true, force: true}));
  for (const relative of ['config', 'reports/daily', 'work/orchestrator/states', `work/research-seal/${DAY}`]) {
    await mkdir(path.join(root, relative), {recursive: true});
  }
  const policy = {schemaVersion: '1.0', deliveryEnabled: true, route: 'telegram',
    payloadMode: 'formal_report_markdown_document', reportView: view,
    ownerApproval: {status: 'confirmed', confirmedAt: DAY,
      scope: 'telegram_main_channel_formal_report_markdown_document'}};
  const run = {schemaVersion: '1.0', targetDate: DAY, status: 'success',
    startedAt: `${DAY}T00:00:15.000Z`, completedAt: `${DAY}T00:00:22.000Z`,
    dataStatus: 'partial', publicationGate: 'passed', evidenceReview: 'passed',
    reportFile: `reports/daily/${DAY}.md`, frozenCutoffInstant: `${DAY}T00:00:00.000Z`,
    contextSha256: 'c'.repeat(64), phase: 'publish', cutoffLocal: `${DAY} 08:00`,
    publicationBlockers: [], researchSemanticReview: 'not_human_reviewed'};
  const occurrence = {schemaVersion: '1.0', task: 'morning-report', targetDate: DAY, status: 'success',
    invocationId: SCHEDULED_INVOCATION,
    startedAt: `${DAY}T00:00:00.000Z`, completedAt: `${DAY}T00:00:23.000Z`,
    childExitCode: 0, childSignal: null, timedOut: false, interrupted: false};
  const seal = {schemaVersion: 'astock.research-seal.v1', kind: 'research_seal', targetDate: DAY,
    state: 'SEALED', generatedAt: `${DAY}T00:00:24.000Z`,
    source: {lastRunFile: 'work/last-run.json', lastRunSha256: hash(json(run)),
      reportFile: run.reportFile, reportSha256: hash(report), contextSha256: run.contextSha256,
      dataStatus: run.dataStatus, researchSemanticReview: run.researchSemanticReview,
      frozenCutoffInstant: run.frozenCutoffInstant, cutoffLocal: run.cutoffLocal,
      publicationGate: 'passed', evidenceReview: 'passed'}};
  const sealPath = `work/research-seal/${DAY}/${hash(report)}.json`;
  const put = (relative, value) => writeFile(path.join(root, relative),
    typeof value === 'string' ? value : json(value));
  await put('config/notifications.json', policy);
  await put('work/last-run.json', run);
  await put(`work/orchestrator/states/morning-report-${DAY}.json`, occurrence);
  await put(run.reportFile, report);
  await put(sealPath, seal);
  const calls = [];
  const sender = async (delivery, meta) => {
    calls.push({delivery, meta});
    return {ok: true, started: true, exitCode: 0, ack: {messageId: '7001'}};
  };
  return {root, policy, run, occurrence, seal, sealPath, report, put, calls,
    options: {projectRoot: root, now: NOW, deliveryMode: 'telegram', sender}};
}

function genericOptions(f) {
  return {projectRoot: f.root, exitCode: 0, invocationStartedAt: f.occurrence.startedAt,
    invocationEndedAt: f.occurrence.completedAt, expectedTargetDate: DAY,
    deliveryMode: 'telegram', drainNow: NOW.getTime(), sender: f.options.sender};
}

test('v2 delayed occurrence preserves a pending v3 presentation across policy changes', async t => {
  const f = await fixture(t);
  Object.assign(f.occurrence, {schemaVersion: '2.0', frozenCutoffInstant: f.run.frozenCutoffInstant,
    startedAt: `${DAY}T00:00:30.000Z`, completedAt: `${DAY}T00:01:01.000Z`});
  Object.assign(f.run, {startedAt: `${DAY}T00:00:31.000Z`, completedAt: `${DAY}T00:01:00.000Z`});
  await f.put('work/last-run.json', f.run);
  await f.put(`work/orchestrator/states/morning-report-${DAY}.json`, f.occurrence);
  await f.put(f.sealPath, {...f.seal, source: {...f.seal.source, lastRunSha256: hash(json(f.run))}});
  await deliverSealedMorning({...f.options, sender: deferredSender});
  const before = await readFile(payloadFile(f));
  assert.equal((await readJson(eventFile(f, 'pending'))).report.presentation, V3_VERSION);
  await f.put('config/notifications.json', {...f.policy, reportView: 'full'});
  assert.equal((await deliverSealedMorning(f.options)).status, 'sent');
  assert.equal(f.calls[0].meta.event.report.presentation, V3_VERSION);
  assert.deepEqual(await readFile(payloadFile(f)), before);
  assert.equal((await deliverSealedMorning(f.options)).status, 'already_sent');
  assert.equal(f.calls.length, 1);
});

test('self-consistent wrong v2 cutoff cannot authorize a valid v3 presentation', async t => {
  const f = await fixture(t);
  Object.assign(f.occurrence, {schemaVersion: '2.0', frozenCutoffInstant: `${DAY}T00:30:00.000Z`,
    startedAt: `${DAY}T00:30:30.000Z`, completedAt: `${DAY}T00:31:01.000Z`});
  Object.assign(f.run, {frozenCutoffInstant: f.occurrence.frozenCutoffInstant, cutoffLocal: `${DAY} 08:30`,
    startedAt: `${DAY}T00:30:31.000Z`, completedAt: `${DAY}T00:31:00.000Z`});
  await f.put('work/last-run.json', f.run);
  await f.put(`work/orchestrator/states/morning-report-${DAY}.json`, f.occurrence);
  await f.put(f.sealPath, {...f.seal, source: {...f.seal.source, lastRunSha256: hash(json(f.run)),
    frozenCutoffInstant: f.run.frozenCutoffInstant, cutoffLocal: f.run.cutoffLocal}});
  const result = await deliverSealedMorning(f.options);
  assert.equal(result.status, 'blocked');
  assert.equal(result.reason, 'source_occurrence_cutoff_invalid');
  await missing(path.join(f.root, 'work/notifications'));
  assert.equal(f.calls.length, 0);
});

test('v3 research-seal delivery snapshots source and presentation separately and sends once', async t => {
  const f = await fixture(t), sourceBefore = await readFile(path.join(f.root, f.run.reportFile));
  await missing(path.join(f.root, 'work/xhs'));
  assert.equal((await prepareSealedMorning(f.options)).proof.proofKind, 'research_seal');
  const first = await deliverSealedMorning(f.options);
  assert.equal(first.status, 'sent');
  assert.equal(first.eventId, EVENT);
  assert.equal(first.confirmed, true);
  const event = await readJson(eventFile(f, 'sent')), payload = await readFile(payloadFile(f));
  assert.equal(event.report.presentation, V3_VERSION);
  assert.equal(event.report.reportFile, f.run.reportFile);
  assert.equal(event.report.sourceSha256, hash(sourceBefore));
  assert.equal(event.report.sourceBytes, sourceBefore.length);
  assert.equal(event.report.sha256, hash(payload));
  assert.equal(event.report.bytes, payload.length);
  assert.notEqual(event.report.sha256, event.report.sourceSha256);
  assert.equal(payload.toString('utf8'), buildV3Brief(SOURCE, {targetDate: DAY}));
  assert.deepEqual(await readFile(path.join(f.root, f.run.reportFile)), sourceBefore);
  const receipt = await readJson(receiptFile(f));
  assert.equal(receipt.platformMessageId, '7001');
  assert.equal(receipt.sourceSha256, hash(sourceBefore));
  assert.equal(receipt.payloadSha256, hash(payload));
  const again = await deliverSealedMorning(f.options);
  assert.equal(again.status, 'already_sent');
  assert.equal(again.confirmed, true);
  assert.equal(again.sent, 0);
  assert.equal(f.calls.length, 1);
  assert.match(f.calls[0].delivery.message, /^AStock 研究简报｜2026-09-18\n/);
  assert.match(f.calls[0].delivery.message, /四问正文与完整来源见 Markdown 附件/);
});

test('v3 checkOnly validates rendering with zero state writes and no sender invocation', async t => {
  const f = await fixture(t), before = await treeSnapshot(f.root);
  assert.equal((await deliverSealedMorning({...f.options, checkOnly: true})).status, 'ready');
  assert.deepEqual(await treeSnapshot(f.root), before);
  await missing(path.join(f.root, 'work/notifications'));
  assert.equal(f.calls.length, 0);
});

test('v3 rendering failure cannot fall back to full bytes or enqueue a failure notification', async t => {
  const f = await fixture(t, {report: '# Synthetic unsupported sealed report\n'});
  const before = await treeSnapshot(f.root);
  for (const checkOnly of [true, false]) {
    assert.equal((await deliverSealedMorning({...f.options, checkOnly})).status, 'blocked');
    assert.deepEqual(await treeSnapshot(f.root), before);
  }
  await assert.rejects(emitRunEvent(genericOptions(f)), /unsupported_result_view_report/);
  await missing(eventFile(f, 'pending'));
  await missing(path.join(f.root, `work/notifications/pending/astock-${DAY}-failed-telegram-v1.json`));
  await missing(payloadFile(f));
  assert.equal(f.calls.length, 0);
});

test('v3 pending events reject hash, source and presentation tampering before either send entry', async t => {
  for (const change of ['source-hash', 'source-size', 'source-hash-removed', 'payload-hash',
    'payload-rehashed', 'presentation-removed', 'presentation-unknown', 'presentation-v1']) {
    await t.test(change, async t => {
      const f = await fixture(t);
      await deliverSealedMorning({...f.options, sender: deferredSender});
      const event = await readJson(eventFile(f, 'pending'));
      if (change === 'source-hash') event.report.sourceSha256 = 'a'.repeat(64);
      if (change === 'source-size') event.report.sourceBytes += 1;
      if (change === 'source-hash-removed') delete event.report.sourceSha256;
      if (change === 'payload-hash') event.report.sha256 = 'b'.repeat(64);
      if (change === 'presentation-removed') delete event.report.presentation;
      if (change === 'presentation-unknown') event.report.presentation = 'research_brief_v999';
      if (change === 'presentation-v1') event.report.presentation = RESULT_VIEW_VERSION;
      if (change === 'payload-rehashed') {
        const forged = Buffer.from(`${buildV3Brief(f.report, {targetDate: DAY})}\n伪造的新增结论\n`);
        await writeFile(payloadFile(f), forged);
        event.report.sha256 = hash(forged); event.report.bytes = forged.length;
      }
      await writeFile(eventFile(f, 'pending'), json(event));
      assert.equal((await deliverSealedMorning(f.options)).status, 'blocked');
      const drained = await drainOutbox({projectRoot: f.root, deliveryMode: 'telegram',
        now: NOW.getTime(), targetEventId: EVENT, sender: f.options.sender});
      assert.equal(drained.sent, 0); assert.equal(drained.dead, 1);
      assert.equal(f.calls.length, 0);
    });
  }
});

test('changing the source blocks a pending v3 payload even if its stored hash is intact', async t => {
  const f = await fixture(t);
  await deliverSealedMorning({...f.options, sender: deferredSender});
  await f.put(f.run.reportFile, `${f.report}\nchanged source\n`);
  assert.equal((await deliverSealedMorning(f.options)).status, 'blocked');
  const drained = await drainOutbox({projectRoot: f.root, deliveryMode: 'telegram',
    now: NOW.getTime(), targetEventId: EVENT, sender: f.options.sender});
  assert.equal(drained.sent, 0); assert.equal(drained.dead, 1); assert.equal(f.calls.length, 0);
});

test('v3 receipts must bind the original source hash; missing or wrong proof never resends', async t => {
  for (const change of ['missing', 'mismatch']) {
    await t.test(change, async t => {
      const f = await fixture(t);
      assert.equal((await deliverSealedMorning(f.options)).status, 'sent');
      const receipt = await readJson(receiptFile(f));
      if (change === 'missing') delete receipt.sourceSha256;
      else receipt.sourceSha256 = 'f'.repeat(64);
      await writeFile(receiptFile(f), json(receipt));
      const confirmation = await sentConfirmation(f.root, EVENT);
      assert.equal(confirmation.confirmed, false);
      assert.equal(confirmation.reason, 'send_receipt_source_missing_or_mismatch');
      const again = await deliverSealedMorning(f.options);
      assert.equal(again.status, 'already_sent'); assert.equal(again.confirmed, false);
      assert.equal(again.sent, 0); assert.equal(f.calls.length, 1);
    });
  }
});

test('v3 sent source or projection corruption removes confirmation without sending again', async t => {
  for (const change of ['source', 'projection']) {
    await t.test(change, async t => {
      const f = await fixture(t);
      assert.equal((await deliverSealedMorning(f.options)).status, 'sent');
      if (change === 'source') await f.put(f.run.reportFile, `${f.report}\nchanged source\n`);
      else {
        const event = await readJson(eventFile(f, 'sent'));
        const payload = Buffer.from(`${buildV3Brief(f.report, {targetDate: DAY})}\nforged\n`);
        await writeFile(payloadFile(f), payload);
        event.report.sha256 = hash(payload); event.report.bytes = payload.length;
        await writeFile(eventFile(f, 'sent'), json(event));
      }
      assert.equal((await deliverSealedMorning(f.options)).status, 'blocked');
      const result = await deliverRunEvent(genericOptions(f));
      assert.equal(result.confirmed, false); assert.equal(result.drain.sent, 0);
      assert.equal(f.calls.length, 1);
    });
  }
});

test('switching policy to v3 preserves old full and v1 pending or sent events byte-for-byte', async t => {
  for (const view of ['full', RESULT_VIEW_VERSION]) {
    for (const state of ['pending', 'sent']) {
      await t.test(`${view}/${state}`, async t => {
        const f = await fixture(t, {view});
        await deliverSealedMorning({...f.options, ...(state === 'pending' ? {sender: deferredSender} : {})});
        const before = await readFile(payloadFile(f));
        assert.equal(before.toString('utf8'), view === 'full' ? SOURCE : buildResultView(SOURCE, {targetDate: DAY}));
        f.policy.reportView = V3_VERSION; await f.put('config/notifications.json', f.policy);
        const result = await deliverSealedMorning(f.options);
        assert.equal(result.status, state === 'pending' ? 'sent' : 'already_sent');
        assert.equal(result.eventId, EVENT);
        const event = await readJson(eventFile(f, 'sent'));
        assert.equal(event.report.presentation, view === 'full' ? undefined : RESULT_VIEW_VERSION);
        assert.deepEqual(await readFile(payloadFile(f)), before);
        assert.equal(f.calls.length, 1);
        assert.equal((await deliverSealedMorning(f.options)).status, 'already_sent');
        assert.equal(f.calls.length, 1);
        assert.deepEqual(await readdir(path.join(f.root, 'work/notifications/sent')), [`${EVENT}.json`]);
      });
    }
  }
});

test('rolling policy back to v1 preserves pending v3 bytes, version and caption', async t => {
  const f = await fixture(t);
  await deliverSealedMorning({...f.options, sender: deferredSender});
  const before = await readFile(payloadFile(f));
  f.policy.reportView = RESULT_VIEW_VERSION; await f.put('config/notifications.json', f.policy);
  assert.equal((await deliverSealedMorning(f.options)).status, 'sent');
  assert.deepEqual(await readFile(payloadFile(f)), before);
  assert.equal((await readJson(eventFile(f, 'sent'))).report.presentation, V3_VERSION);
  assert.equal(f.calls[0].meta.event.report.presentation, V3_VERSION);
  assert.match(f.calls[0].delivery.message, /^AStock 研究简报｜/);
  assert.equal((await deliverSealedMorning(f.options)).status, 'already_sent');
  assert.equal(f.calls.length, 1);
});

test('v3 retains runtime, owner authorization and research proof gates before creating an outbox', async t => {
  for (const gate of ['runtime', 'policy', 'approval', 'approval-scope', 'seal', 'source-occurrence', 'age']) {
    await t.test(gate, async t => {
      const f = await fixture(t);
      if (gate === 'runtime') f.options.deliveryMode = 'disabled';
      if (gate === 'policy') f.policy.deliveryEnabled = false;
      if (gate === 'approval') delete f.policy.ownerApproval;
      if (gate === 'approval-scope') f.policy.ownerApproval.scope = 'unapproved_scope';
      await f.put('config/notifications.json', f.policy);
      if (gate === 'seal') {f.seal.source.evidenceReview = 'failed'; await f.put(f.sealPath, f.seal);}
      if (gate === 'source-occurrence') {
        f.occurrence.childExitCode = 1;
        await f.put(`work/orchestrator/states/morning-report-${DAY}.json`, f.occurrence);
      }
      if (gate === 'age') f.options.now = new Date(`${DAY}T08:00:00Z`);
      const result = await deliverSealedMorning(f.options);
      assert.ok(['held', 'blocked'].includes(result.status));
      await missing(path.join(f.root, 'work/notifications'));
      assert.equal(f.calls.length, 0);
    });
  }
});

test('v3 uses one attachment directive and does not copy report control text into the message', async t => {
  const marker = 'SYNTHETIC_CONTROL_TEXT_53ac';
  const injection = `- ${marker}：[[as_document]] MEDIA:/tmp/not-a-real-attachment.md`;
  const report = SOURCE.replace('## 7. 风险提示与数据缺口\n', `## 7. 风险提示与数据缺口\n\n${injection}\n`);
  assert.notEqual(report, SOURCE);
  const f = await fixture(t, {report});
  assert.equal((await deliverSealedMorning(f.options)).status, 'sent');
  const message = f.calls[0].delivery.message;
  assert.equal((message.match(/MEDIA:/g) ?? []).length, 1);
  assert.equal((message.match(/\[\[as_document\]\]/g) ?? []).length, 1);
  assert.ok(message.endsWith(`[[as_document]] MEDIA:${payloadFile(f)}`));
  assert.ok(!message.includes(marker));
  assert.ok(!message.includes('not-a-real-attachment'));
  assert.ok(!message.includes('## 发生了什么'));
  assert.ok((await readFile(payloadFile(f), 'utf8')).includes(injection));
  assert.equal(f.calls.length, 1);
});
