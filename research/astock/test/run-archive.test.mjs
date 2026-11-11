import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { freezeRunInput, compileFrozenReport, sealRunResult, publishArchivedReport, replayRunArchive, loadArchivedCompilation, loadVerifiedFinancialBaseline, captureReviewedReport, selectFrozenCandidates } from "../src/run-archive.mjs";
import { attachPreviousFinancialPeriods } from "../src/financial-comparison.mjs";
import { documentFixture } from "./helpers/document-fixture.mjs";
import { sha256 } from "../src/snapshot-integrity.mjs";

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
async function setup(t, mode = "production") {
  const directory = await mkdtemp(path.join(os.tmpdir(), "astock-archive-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const options = { ...fixture(), archiveRoot: path.join(directory, "archives"), outputDirectory: path.join(directory, "reports"), mode };
  const frozen = await freezeRunInput(options);
  const report = compileFrozenReport(frozen.input);
  const compilation = { report, overlay: null, analysisStatus: "disabled", analysisAttempts: 0 };
  await sealRunResult(frozen, compilation);
  return { options, frozen, report, compilation };
}

test("完整输入、候选选择与输出可离线逐字重建，正式稿独占写入", async (t) => {
  const { options, frozen, report } = await setup(t);
  const output = path.join(options.outputDirectory, `${options.targetDate}.md`);
  await publishArchivedReport(frozen, output);
  const verified = await replayRunArchive(root, frozen.archiveDirectory);
  assert.equal(verified.matched, true);
  assert.match(verified.compilationInputSha256, /^[a-f0-9]{64}$/);
  assert.notEqual(verified.compilationInputSha256, verified.inputSha256);
  assert.equal(await readFile(output, "utf8"), report);
  assert.equal((await stat(path.join(frozen.archiveDirectory, "input.json"))).mode & 0o777, 0o600);
  assert.equal((await loadArchivedCompilation(frozen)).report, report);
  await publishArchivedReport(frozen, output);
  await writeFile(output, "already sealed different report");
  await assert.rejects(publishArchivedReport(frozen, output), /overwrite/);
  assert.equal(await readFile(output, "utf8"), "already sealed different report");
});

test("财务基准必须匹配前份封存稿，不能仅凭日期JSON", async (t) => {
  const { options, frozen } = await setup(t);
  assert.deepEqual(await loadVerifiedFinancialBaseline(options.archiveRoot, options.outputDirectory, "2026-08-11"), {});
  const output = path.join(options.outputDirectory, `${options.targetDate}.md`);
  await publishArchivedReport(frozen, output);
  const baseline = await loadVerifiedFinancialBaseline(options.archiveRoot, options.outputDirectory, "2026-08-11");
  const current = structuredClone(options.rawContext);
  current.meta.targetDate = "2026-08-11";
  const stock = attachPreviousFinancialPeriods(current, baseline).data.watchlist[0];
  assert.equal(stock.financialComparison.state, "same_period");
  assert.equal(stock.financialComparison.baselineTargetDate, "2026-08-10");
  await writeFile(output, "tampered");
  await assert.rejects(loadVerifiedFinancialBaseline(options.archiveRoot, options.outputDirectory, "2026-08-11"), /sealed report/);
});

test("重试固定原始包、比较基准与配置，拒绝更换输入", async (t) => {
  const { options, frozen } = await setup(t);
  assert.equal((await freezeRunInput(options)).inputSha256, frozen.inputSha256);
  const changed = structuredClone(options);
  changed.configuration.risk.newsMaxAgeHours = 24;
  await assert.rejects(freezeRunInput(changed), /inputs changed/);
  const changedRaw = structuredClone(options);
  changedRaw.rawContext.data.watchlist[0].financials.parentNetProfit = 999;
  await assert.rejects(freezeRunInput(changedRaw), /inputs changed/);
  await writeFile(path.join(frozen.archiveDirectory, "input.json"), "{}");
  await assert.rejects(replayRunArchive(root, frozen.archiveDirectory), /input hash mismatch/);
});

test("历史验证档不能变成正式发布凭据，结果篡改会被拒绝", async (t) => {
  const { options, frozen } = await setup(t, "historical_validation");
  await assert.rejects(publishArchivedReport(frozen, path.join(options.outputDirectory, "report.md")), /cannot publish/);
  assert.equal((await replayRunArchive(root, frozen.archiveDirectory)).mode, "historical_validation");
  await writeFile(path.join(frozen.archiveDirectory, "result", "report.md"), "tampered");
  await assert.rejects(replayRunArchive(root, frozen.archiveDirectory), /result hash mismatch/);
});

test("归档路径与文件不得通过符号链接替换", async (t) => {
  const { options, frozen } = await setup(t);
  const alias = path.join(options.archiveRoot, "linked");
  await symlink(frozen.archiveDirectory, alias, "dir");
  await assert.rejects(replayRunArchive(root, alias), /symlink/);
});

test("编译代码改变或未规范化稿件不能成为通过附件", async (t) => {
  const { options, frozen, report } = await setup(t);
  const changedRoot = path.join(options.archiveRoot, "changed-code");
  await mkdir(path.join(changedRoot, "src"), { recursive: true });
  await writeFile(path.join(changedRoot, "src/compiler.mjs"), "export const changed = true;\n");
  await assert.rejects(replayRunArchive(changedRoot, frozen.archiveDirectory), /code differs/);
  await assert.rejects(sealRunResult(frozen, { report: report + "unvalidated fallback", overlay: null }), /cannot be reproduced/);
  assert.equal((await loadArchivedCompilation(frozen)).report, report);
});

test("更改实际候选选择必须使完整编译输入身份失效", async (t) => {
  const { frozen } = await setup(t);
  const filename = path.join(frozen.archiveDirectory, "result/result.json");
  const result = JSON.parse(await readFile(filename, "utf8"));
  result.selection = { candidateIds: ["invented"] };
  await writeFile(filename, JSON.stringify(result));
  await assert.rejects(loadArchivedCompilation(frozen), /selection\/input hash mismatch/);
});

async function unsealedCandidateArchive(t) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "astock-candidate-archive-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const options = { ...fixture(), archiveRoot: path.join(directory, "archives"),
    outputDirectory: path.join(directory, "reports"), mode: "historical_validation" };
  const template = options.rawContext.data.watchlist[0];
  options.rawContext.data.watchlist = Array.from({ length: 9 }, (_, index) => {
    const stock = structuredClone(template);
    stock.code = String(index + 1).padStart(6, "0");
    stock.name = `合成公司${index + 1}`;
    stock.symbol = `${stock.code}.SZ`;
    stock.marketData.code = stock.code;
    stock.marketData.name = stock.name;
    stock.financials.parentNetProfitYoY = -10;
    stock.financials.units.parentNetProfitYoY = "percentage_point";
    return stock;
  });
  options.configuration.stocks.corePool = options.rawContext.data.watchlist.map(({ code, name, symbol, exchange }) =>
    ({ code, name, symbol, exchange }));
  return freezeRunInput(options);
}

for (const count of [0, 1, 2, 8]) {
  test(`九项候选归档精确恢复 ${count} 项选择，不枚举子集且封存后不能更改`, async (t) => {
    const frozen = await unsealedCandidateArchive(t);
    assert.equal(frozen.input.candidateIds.length, 9);
    assert.ok(frozen.input.code.files["agents/structured_analyst.md"]);
    const overlay = selectFrozenCandidates(frozen.input, frozen.input.candidateIds.slice(0, count).reverse());
    const report = compileFrozenReport(frozen.input, overlay);
    const result = await captureReviewedReport(frozen, report);
    assert.deepEqual(result.selection, overlay);
    assert.equal((await replayRunArchive(root, frozen.archiveDirectory)).matched, true);
    assert.deepEqual(await captureReviewedReport(frozen, report), result);
    await assert.rejects(captureReviewedReport(frozen, report + "tampered"), /differs from sealed/);
  });
}

test("恢复候选也不能接受正文改写或更换股票身份，失败不创建结果", async (t) => {
  const frozen = await unsealedCandidateArchive(t);
  const overlay = selectFrozenCandidates(frozen.input, frozen.input.candidateIds.slice(0, 2));
  const report = compileFrozenReport(frozen.input, overlay);
  const changedBody = report.replace("\n## 2.", "\n未核验的额外正文\n\n## 2.");
  assert.notEqual(changedBody, report);
  await assert.rejects(captureReviewedReport(frozen, changedBody), /not an archived deterministic/);
  // The third company has the same underlying source IDs but is not selected;
  // changing its identity must not be mistaken for the original selection.
  const changedIdentity = report.replace("[推断] 股票 000001", "[推断] 股票 999999");
  assert.notEqual(changedIdentity, report);
  await assert.rejects(captureReviewedReport(frozen, changedIdentity), /固定股票池/);
  assert.equal(await loadArchivedCompilation(frozen), null);
});

test("模拟连续工作日：原文压缩依赖封存回执和保留的PDF字节", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "astock-document-baseline-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const f = documentFixture();
  Object.assign(f.doc, { code: "000001", name: "测试银行", disclosureDate: "2026-08-07",
    url: "https://static.cninfo.com.cn/finalpage/2026-08-07/0.PDF",
    observedAt: "2026-08-09T17:00:00+08:00", dataAsOf: "2026-08-09T17:00:00+08:00" });
  f.doc.pages[0].text = f.doc.pages[0].text.replaceAll("002617", "000001").replaceAll("露笑科技", "测试银行");
  f.doc.pages[0].textSha256 = sha256(f.doc.pages[0].text);
  Object.assign(f.doc.queryRecord, { date: f.doc.disclosureDate, url: f.doc.url, observedAt: "2026-08-09T16:59:59+08:00" });
  Object.assign(f.source, { disclosureDate: f.doc.disclosureDate, url: f.doc.url, observedAt: f.doc.observedAt, dataAsOf: f.doc.observedAt });
  const documentStore = path.join(directory, "pdf-store");
  await mkdir(documentStore);
  await writeFile(path.join(documentStore, `${f.doc.pdfSha256}.pdf`), f.body);
  const options = { ...fixture(), documentStore, archiveRoot: path.join(directory, "archives"), outputDirectory: path.join(directory, "reports") };
  options.rawContext.data.watchlist[0].officialDisclosure = { financialSummary: f.doc };
  options.rawContext.sources.push(f.source);
  const first = await freezeRunInput(options);
  const report = compileFrozenReport(first.input);
  assert.match(report, /现金流原文.*1,000\.00 900\.00/);
  await sealRunResult(first, { report, overlay: null, analysisStatus: "disabled" });
  assert.deepEqual(await loadVerifiedFinancialBaseline(options.archiveRoot, options.outputDirectory, "2026-08-11"), {});
  await publishArchivedReport(first, path.join(options.outputDirectory, "2026-08-10.md"));
  const baseline = await loadVerifiedFinancialBaseline(options.archiveRoot, options.outputDirectory, "2026-08-11");
  assert.equal(baseline["000001"].officialDocument.pdfSha256, f.doc.pdfSha256);

  const next = structuredClone(options);
  Object.assign(next, { targetDate: "2026-08-11", cutoffInstant: "2026-08-11T00:00:00Z", cutoffLocal: "2026-08-11 08:00" });
  const ctx = next.rawContext;
  ctx.meta.targetDate = "2026-08-11";
  Object.assign(ctx.data.tradingCalendar, { targetDate: "2026-08-11", previousTradingDate: "2026-08-10", dataAsOf: "2026-08-11T08:00:00+08:00" });
  ctx.data.previousAShareSession.sessionDate = "2026-08-10";
  const marketTime = "2026-08-10T15:00:00+08:00";
  ctx.data.previousAShareSession.dataAsOf = marketTime;
  for (const index of ctx.data.previousAShareSession.indices) index.dataAsOf = marketTime;
  ctx.data.watchlist[0].marketData.dataAsOf = marketTime;
  ctx.sources.find((s) => s.id === "market").dataAsOf = marketTime;
  ctx.sources.find((s) => s.id === "calendar").dataAsOf = ctx.data.tradingCalendar.dataAsOf;
  const doc = ctx.data.watchlist[0].officialDisclosure.financialSummary;
  doc.observedAt = doc.dataAsOf = "2026-08-11T07:59:00+08:00";
  doc.queryRecord.observedAt = "2026-08-11T07:58:00+08:00";
  Object.assign(ctx.sources.find((s) => s.id === "synthetic-pdf"), { observedAt: doc.observedAt, dataAsOf: doc.dataAsOf });
  const second = await freezeRunInput(next);
  const repeated = compileFrozenReport(second.input);
  assert.match(repeated, /现金流原文（PDF 第 2 页.*1,000\.00 900\.00/);
  assert.match(repeated, /与已封存证据（2026-08-10）一致/);
  assert.doesNotMatch(repeated, /不重复列数/);
  await sealRunResult(second, { report: repeated, overlay: null, analysisStatus: "disabled" });
  assert.equal((await replayRunArchive(root, second.archiveDirectory)).matched, true);
  await writeFile(path.join(first.archiveDirectory, "evidence", `${f.doc.pdfSha256}.pdf`), Buffer.alloc(f.body.length));
  await assert.rejects(loadVerifiedFinancialBaseline(options.archiveRoot, options.outputDirectory, "2026-08-11"), /PDF byte hash mismatch/);
});
