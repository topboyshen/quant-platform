import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { freezeRunInput, sealRunResult, compileFrozenReport } from "../src/run-archive.mjs";
import { buildDigestView } from "../src/digest-view.mjs";
import { loadFrozenNotificationPresentation } from "../src/frozen-notification-presentation.mjs";
import { runTask } from "../src/task-orchestrator.mjs";
import { jsonHash, sha256 } from "../src/snapshot-integrity.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function fixture() {
  const time = "2026-08-07T15:00:00+08:00";
  const stock = { code: "000001", name: "测试银行", symbol: "000001.SZ", exchange: "SZ" };
  const index = (code, name) => ({ code, name, close: 101, previousClose: 100, change: 1, changePercent: 1,
    units: { close: "index_point", previousClose: "index_point", change: "index_point", changePercent: "percentage_point" }, dataAsOf: time, sourceIds: ["market"] });
  const rawContext = {
    schemaVersion: "1.0",
    meta: { targetDate: "2026-08-10", timezone: "Asia/Shanghai", status: "partial", provider: "test-fixture", sourceType: "fixture", isLive: false },
    data: {
      tradingCalendar: { status: "trading_day", targetDate: "2026-08-10", previousTradingDate: "2026-08-07", dataAsOf: "2026-08-10T08:00:00+08:00", sourceIds: ["calendar"] },
      previousAShareSession: { sessionDate: "2026-08-07", dataAsOf: time, sourceIds: ["market"],
        indices: [index("sh000001", "上证指数"), index("sz399001", "深证成指"), index("sz399006", "创业板指")] },
      watchlist: [{ ...stock, status: "available", marketData: { code: stock.code, name: stock.name,
        last: 12.3, previousClose: 12, change: 0.3, changePercent: 2.5, dataAsOf: time, sourceIds: ["market"],
        units: { last: "CNY_per_share", previousClose: "CNY_per_share", change: "CNY_per_share", changePercent: "percentage_point" } },
        financials: { reportDate: "2026-06-30", parentNetProfit: 100, units: { parentNetProfit: "CNY" }, dataAsOf: "2026-07-30T18:00:00+08:00", sourceIds: ["financial"] } }],
      capitalFlows: [], sectors: [], announcementsAndNews: []
    },
    sources: [
      { id: "calendar", title: "交易日历", url: "https://example.com/calendar", sourceType: "trading_calendar_data", dataAsOf: "2026-08-10T08:00:00+08:00" },
      { id: "market", title: "行情", url: "https://example.com/market", sourceType: "market_data", dataAsOf: time },
      { id: "financial", title: "财务指标", url: "https://example.com/financial", sourceType: "financial_data_aggregation", dataAsOf: "2026-07-30T18:00:00+08:00" }
    ]
  };
  return { projectRoot: root, rawContext, targetDate: "2026-08-10", cutoffInstant: "2026-08-10T00:00:00Z", cutoffLocal: "2026-08-10 08:00",
    configuration: { stocks: { corePool: [stock] }, risk: { disclaimer: "本报告仅供研究与信息参考，不构成任何投资建议。", marketDataMaxAgeHours: 18, newsMaxAgeHours: 72 }, workflow: { reportMode: "structured_compiler" } } };
}

async function baselineArchive(t, notificationPresentation = null) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "astock-digest-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const options = { ...fixture(), archiveRoot: path.join(directory, "archives"), outputDirectory: path.join(directory, "reports") };
  if (notificationPresentation) options.configuration.notificationPresentation = notificationPresentation;
  const frozen = await freezeRunInput(options);
  const report = compileFrozenReport(frozen.input, null);
  await sealRunResult(frozen, { report, overlay: null, analysisStatus: "disabled", analysisAttempts: 0 });
  return { frozen, report, options };
}

test("基础版零语义候选生成有效摘要，不伪造推断条目", async (t) => {
  const { frozen, report } = await baselineArchive(t);
  const view = buildDigestView({
    input: frozen.input, selection: null, inputSha256: frozen.inputSha256,
    selectionSha256: jsonHash(null), reportSha256: sha256(report), generatedAt: "2026-08-10T00:00:30.000Z"
  });
  assert.equal(view.digest.schemaVersion, "astock.research-digest.v1");
  assert.equal(view.digest.reportDate, "2026-08-10");
  assert.equal(view.digest.dataDate, "2026-08-07");
  assert.equal(view.digest.reportSha256, sha256(report));
  assert.equal(view.digest.inputSha256, frozen.inputSha256);
  assert.equal(view.digest.reportView, null, "old frozen input does not prove a presentation choice");
  assert.equal(JSON.parse(view.body).reportView, null);
  assert.deepEqual(view.digest.items, []);
  assert.ok(view.digest.limitations.some((value) => value.includes("基础版")));
  assert.ok(view.digest.conclusionLines.length > 0);
  assert.ok(view.digest.facts.length > 0, "基础版仍应带可引用的行情事实");
  assert.match(view.digestSha256, /^[0-9a-f]{64}$/);
});

test("new digest binds the minimal frozen notification view and ignores later policy edits", async (t) => {
  const policyRoot = await mkdtemp(path.join(os.tmpdir(), "astock-digest-policy-"));
  t.after(() => rm(policyRoot, { recursive: true, force: true }));
  await mkdir(path.join(policyRoot, "config"));
  const policyFile = path.join(policyRoot, "config", "notifications.json");
  const policy = { schemaVersion: "1.0", deliveryEnabled: true, route: "telegram",
    payloadMode: "formal_report_markdown_document", reportView: "research_brief_v3",
    ownerApproval: { status: "confirmed", confirmedAt: "2026-08-03" } };
  await writeFile(policyFile, JSON.stringify(policy));
  const snapshot = await loadFrozenNotificationPresentation(policyRoot);
  assert.deepEqual(snapshot, { reportView: "research_brief_v3" }, "no approval metadata is archived");
  const { frozen, report, options } = await baselineArchive(t, snapshot);
  assert.deepEqual(frozen.input.configuration.notificationPresentation, snapshot);
  await writeFile(policyFile, JSON.stringify({ ...policy, reportView: "full" }));
  assert.deepEqual(await loadFrozenNotificationPresentation(policyRoot), { reportView: "full" });
  const view = buildDigestView({
    input: frozen.input, selection: null, inputSha256: frozen.inputSha256,
    selectionSha256: jsonHash(null), reportSha256: sha256(report), generatedAt: "2026-08-10T00:00:30.000Z"
  });
  assert.equal(view.digest.reportView, "research_brief_v3");
  assert.equal(JSON.parse(view.body).reportView, "research_brief_v3");
  const changed = structuredClone(options);
  changed.configuration.notificationPresentation.reportView = "full";
  await assert.rejects(freezeRunInput(changed), /immutable run inputs changed/);
});

test("delayed v2 occurrence freezes schedule cutoff and presentation in the same immutable input", async (t) => {
  for (const reportView of ["full", "results_only_v1", "research_brief_v3"]) {
    const taskRoot = await mkdtemp(path.join(os.tmpdir(), "astock-cutoff-presentation-"));
    t.after(() => rm(taskRoot, { recursive: true, force: true }));
    await mkdir(path.join(taskRoot, "config"));
    await writeFile(path.join(taskRoot, "config", "workflow.json"), JSON.stringify({
      timezone: "Asia/Shanghai", scheduledTime: "08:00"
    }));
    const policyFile = path.join(taskRoot, "config", "notifications.json");
    const policy = { schemaVersion: "1.0", deliveryEnabled: true, route: "telegram",
      payloadMode: "formal_report_markdown_document", reportView };
    await writeFile(policyFile, JSON.stringify(policy));
    const startedAt = "2026-08-10T00:00:30.000Z";
    let frozen, options, report;
    const outcome = await runTask({ task: "morning-report", origin: "cron" }, {
      projectRoot: taskRoot, now: () => new Date(startedAt),
      processRunner: async (_definition, _input, { occurrenceIdentity }) => {
        options = { ...fixture(), archiveRoot: path.join(taskRoot, "work", "report-archives"),
          outputDirectory: path.join(taskRoot, "reports"),
          targetDate: occurrenceIdentity.targetDate, cutoffInstant: occurrenceIdentity.frozenCutoffInstant };
        options.configuration.notificationPresentation = await loadFrozenNotificationPresentation(taskRoot);
        frozen = await freezeRunInput(options);
        report = compileFrozenReport(frozen.input, null);
        await sealRunResult(frozen, { report, overlay: null, analysisStatus: "disabled", analysisAttempts: 0 });
        await writeFile(path.join(taskRoot, "work", "last-run.json"), JSON.stringify({
          targetDate: occurrenceIdentity.targetDate, status: "success",
          frozenCutoffInstant: occurrenceIdentity.frozenCutoffInstant
        }));
        return { exitCode: 0, signal: null, stdout: Buffer.alloc(0), timedOut: false };
      }
    });
    assert.equal(outcome.outcome, "success", reportView);
    const occurrence = JSON.parse(await readFile(path.join(taskRoot,
      "work/orchestrator/states/morning-report-2026-08-10.json"), "utf8"));
    assert.equal(occurrence.schemaVersion, "2.0");
    assert.equal(occurrence.startedAt, startedAt);
    assert.equal(frozen.input.cutoffInstant, "2026-08-10T00:00:00.000Z");
    assert.equal(frozen.input.cutoffInstant, occurrence.frozenCutoffInstant);
    const inputFile = path.join(frozen.archiveDirectory, "input.json"), before = await readFile(inputFile);
    await writeFile(policyFile, JSON.stringify({ ...policy, reportView: reportView === "full" ? "research_brief_v3" : "full" }));
    const view = buildDigestView({ input: frozen.input, selection: null,
      inputSha256: frozen.inputSha256, selectionSha256: jsonHash(null), reportSha256: sha256(report) });
    assert.equal(view.digest.reportView, reportView);
    assert.equal((await freezeRunInput(options)).inputSha256, frozen.inputSha256);
    await assert.rejects(freezeRunInput({ ...options, cutoffInstant: startedAt }), /immutable run inputs changed/);
    await assert.rejects(freezeRunInput({ ...options, configuration: { ...options.configuration,
      notificationPresentation: await loadFrozenNotificationPresentation(taskRoot) } }), /immutable run inputs changed/);
    assert.deepEqual(await readFile(inputFile), before);
  }
});

test("invalid frozen or live presentation is rejected instead of inferred", async (t) => {
  const { frozen, report } = await baselineArchive(t);
  const args = { input: { ...frozen.input, configuration: {
    ...frozen.input.configuration, notificationPresentation: { reportView: "unverified_view" }
  } }, selection: null, inputSha256: frozen.inputSha256,
  selectionSha256: jsonHash(null), reportSha256: sha256(report) };
  assert.throws(() => buildDigestView(args), /invalid frozen notification presentation/);

  const policyRoot = await mkdtemp(path.join(os.tmpdir(), "astock-digest-invalid-policy-"));
  t.after(() => rm(policyRoot, { recursive: true, force: true }));
  await mkdir(path.join(policyRoot, "config"));
  await writeFile(path.join(policyRoot, "config", "notifications.json"), JSON.stringify({
    schemaVersion: "1.0", deliveryEnabled: true, route: "telegram",
    payloadMode: "formal_report_markdown_document", reportView: "unverified_view"
  }));
  await assert.rejects(loadFrozenNotificationPresentation(policyRoot), /reportView 无效/);
});

test("摘要确定性与来源身份一致（编号 -> 同次注册表来源）", async (t) => {
  const { frozen, report } = await baselineArchive(t);
  const args = {
    input: frozen.input, selection: null, inputSha256: frozen.inputSha256,
    selectionSha256: jsonHash(null), reportSha256: sha256(report), generatedAt: "2026-08-10T00:00:30.000Z"
  };
  const first = buildDigestView(args);
  const second = buildDigestView(args);
  assert.equal(first.digestSha256, second.digestSha256, "同一 generatedAt 必须得到同一摘要哈希");
  const byNumber = new Map(first.digest.sourceIndex.map((entry) => [entry.number, entry.sourceId]));
  for (const item of first.digest.items) {
    for (const entry of [...item.evidence, ...item.counterEvidence]) {
      const number = Number(/^\[来源 (\d+)\]/.exec(entry.reference)?.[1]);
      assert.equal(byNumber.get(number), entry.sourceId, "编号必须绑定到同一来源 id");
    }
  }
  assert.ok(first.digest.sourceIndex.every((entry) => entry.title !== null));
});
