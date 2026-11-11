import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { compilerIdentity, freezeRunInput, compileFrozenReport, sealRunResult, publishArchivedReport, replayRunArchive, loadArchivedCompilation, loadVerifiedFinancialBaseline, readFrozenInput } from "../src/run-archive.mjs";
import { jsonHash, sha256 } from "../src/snapshot-integrity.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function otherEra(code) {
  const files = { ...code.files, "src/run-archive.mjs": "a".repeat(64) };
  return { ...code, files, filesSha256: jsonHash(files) };
}

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

async function frozenArchive(t) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "astock-era-compat-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const options = { ...fixture(), archiveRoot: path.join(directory, "archives"), outputDirectory: path.join(directory, "reports") };
  const frozen = await freezeRunInput(options);
  return { directory, options, frozen };
}

// 同代加载（编译指纹一致）：候选目录强校验必须原样保留。
test("同代重读必须逐字重现候选目录，篡改候选清单仍被拒绝", async (t) => {
  const { frozen } = await frozenArchive(t);
  const code = await compilerIdentity(root);
  const intact = await readFrozenInput(frozen.archiveDirectory, { compilerCode: code });
  assert.equal(intact.candidateCatalogueEraVerified, true);

  const tampered = JSON.parse(await readFile(path.join(frozen.archiveDirectory, "input.json"), "utf8"));
  tampered.candidateIds = ["theme:invented:2026-08-10:deadbeef"];
  const body = `${JSON.stringify(tampered)}\n`;
  await writeFile(path.join(frozen.archiveDirectory, "input.json"), body);
  await writeFile(path.join(frozen.archiveDirectory, "input.sha256"), `${sha256(body)}\n`);
  await assert.rejects(readFrozenInput(frozen.archiveDirectory, { compilerCode: code }), /candidate catalogue mismatch/);
  await assert.rejects(replayRunArchive(root, frozen.archiveDirectory), /candidate catalogue mismatch/);
});

test("省略编译指纹仍保持候选目录强校验，篡改不能借省略跳过", async (t) => {
  const { frozen } = await frozenArchive(t);
  const intact = await readFrozenInput(frozen.archiveDirectory);
  assert.equal(intact.candidateCatalogueEraVerified, false);
  assert.deepEqual(intact.input.candidateIds, frozen.input.candidateIds);

  const tampered = JSON.parse(await readFile(path.join(frozen.archiveDirectory, "input.json"), "utf8"));
  tampered.candidateIds = ["theme:invented:2026-08-10:deadbeef"];
  const body = `${JSON.stringify(tampered)}\n`;
  await writeFile(path.join(frozen.archiveDirectory, "input.json"), body);
  await writeFile(path.join(frozen.archiveDirectory, "input.sha256"), `${sha256(body)}\n`);
  await assert.rejects(readFrozenInput(frozen.archiveDirectory), /candidate catalogue mismatch/);
  await assert.rejects(readFrozenInput(frozen.archiveDirectory, { allowCrossEraCatalogue: true }), /candidate catalogue mismatch/);
});

// 跨代旧档不按旧规则重算候选目录；仅封存基线读取跳过重算，并仍要哈希链与结果绑定。
test("跨代旧档仅封存基线读取可跳过候选重算，普通读取仍被时代锁拒绝", async (t) => {
  const { options, frozen } = await frozenArchive(t);
  const mutated = JSON.parse(await readFile(path.join(frozen.archiveDirectory, "input.json"), "utf8"));
  mutated.code = otherEra(mutated.code);
  const body = `${JSON.stringify(mutated)}\n`;
  await writeFile(path.join(frozen.archiveDirectory, "input.json"), body);
  await writeFile(path.join(frozen.archiveDirectory, "input.sha256"), `${sha256(body)}\n`);
  const code = await compilerIdentity(root);
  await assert.rejects(readFrozenInput(frozen.archiveDirectory, { compilerCode: code }), /code differs/);
  await assert.rejects(readFrozenInput(frozen.archiveDirectory, { compilerCode: code, allowCrossEraCatalogue: true }), /code differs/);

  const legacy = { input: mutated, inputSha256: sha256(body), archiveDirectory: frozen.archiveDirectory };
  const report = compileFrozenReport(legacy.input);
  await sealRunResult(legacy, { report, overlay: null, analysisStatus: "disabled", analysisAttempts: 0 });
  const output = path.join(options.outputDirectory, `${options.targetDate}.md`);
  await publishArchivedReport(legacy, output);
  const baseline = await loadVerifiedFinancialBaseline(options.archiveRoot, options.outputDirectory, "2026-08-11", code);
  assert.ok(baseline["000001"]);
  assert.equal(baseline["000001"].baselineTargetDate, "2026-08-10");
  await assert.rejects(replayRunArchive(root, frozen.archiveDirectory), /code differs/);
});

test("跨代封存档篡改候选目录仍被结果绑定拒绝；未封存旧档不被基线读取消费", async (t) => {
  const { options, frozen } = await frozenArchive(t);
  const report = compileFrozenReport(frozen.input);
  await sealRunResult(frozen, { report, overlay: null, analysisStatus: "disabled", analysisAttempts: 0 });
  const output = path.join(options.outputDirectory, `${options.targetDate}.md`);
  await publishArchivedReport(frozen, output);
  // 先封存并发布，再把档改成旧代指纹并篡改候选目录：结果绑定必须拒绝。
  const mutated = JSON.parse(await readFile(path.join(frozen.archiveDirectory, "input.json"), "utf8"));
  mutated.code = otherEra(mutated.code);
  mutated.candidateIds = ["theme:invented:2026-08-10:deadbeef"];
  const body = `${JSON.stringify(mutated)}\n`;
  await writeFile(path.join(frozen.archiveDirectory, "input.json"), body);
  await writeFile(path.join(frozen.archiveDirectory, "input.sha256"), `${sha256(body)}\n`);
  await assert.rejects(
    loadVerifiedFinancialBaseline(options.archiveRoot, options.outputDirectory, "2026-08-11", await compilerIdentity(root)),
    /result hash mismatch/);

  const directory2 = await mkdtemp(path.join(os.tmpdir(), "astock-era-unsealed-test-"));
  t.after(() => rm(directory2, { recursive: true, force: true }));
  const options2 = { ...fixture(), archiveRoot: path.join(directory2, "archives"), outputDirectory: path.join(directory2, "reports") };
  const unsealed = await freezeRunInput(options2);
  const unsealedMutated = JSON.parse(await readFile(path.join(unsealed.archiveDirectory, "input.json"), "utf8"));
  unsealedMutated.code = otherEra(unsealedMutated.code);
  unsealedMutated.candidateIds = ["theme:invented:2026-08-10:deadbeef"];
  const unsealedBody = `${JSON.stringify(unsealedMutated)}\n`;
  await writeFile(path.join(unsealed.archiveDirectory, "input.json"), unsealedBody);
  await writeFile(path.join(unsealed.archiveDirectory, "input.sha256"), `${sha256(unsealedBody)}\n`);
  assert.deepEqual(await loadVerifiedFinancialBaseline(options2.archiveRoot, options2.outputDirectory, "2026-08-11", await compilerIdentity(root)), {});
});

test("旧档内容不一致门禁逐项保持：输入哈希、上下文链、推导性", async (t) => {
  const { frozen } = await frozenArchive(t);
  const file = path.join(frozen.archiveDirectory, "input.json");
  const sidecar = path.join(frozen.archiveDirectory, "input.sha256");
  const original = await readFile(file, "utf8");
  const code = await compilerIdentity(root);
  const restore = async () => { await writeFile(file, original); await writeFile(sidecar, `${sha256(original)}\n`); };

  // 1) 篡改正文未换 sidecar。
  const parsed1 = JSON.parse(original);
  parsed1.rawContext.meta.provider = "forged";
  await writeFile(file, `${JSON.stringify(parsed1)}\n`);
  await assert.rejects(readFrozenInput(frozen.archiveDirectory), /frozen input hash mismatch/);

  // 2) 伪造 sidecar 但 rawContextText 被改。
  const parsed2 = JSON.parse(original);
  parsed2.rawContextText = parsed2.rawContextText.replace("测试银行", "伪名");
  const body2 = `${JSON.stringify(parsed2)}\n`;
  await writeFile(file, body2);
  await writeFile(sidecar, `${sha256(body2)}\n`);
  await assert.rejects(readFrozenInput(frozen.archiveDirectory, { compilerCode: code }), /frozen context hash mismatch/);

  // 3) 连 compilerContextSha256 一起伪造，推导性检查必须拒绝。
  const parsed3 = JSON.parse(original);
  parsed3.compilerContext.data.watchlist[0].marketData.last = 99;
  parsed3.compilerContextSha256 = jsonHash(parsed3.compilerContext);
  const body3 = `${JSON.stringify(parsed3)}\n`;
  await writeFile(file, body3);
  await writeFile(sidecar, `${sha256(body3)}\n`);
  await assert.rejects(readFrozenInput(frozen.archiveDirectory, { compilerCode: code }), /not derivable from archived inputs/);

  await restore();
  const intact = await readFrozenInput(frozen.archiveDirectory, { compilerCode: code });
  assert.equal(intact.candidateCatalogueEraVerified, true);
});

// ---------------------------------------------------------------- 2026-09-23 回归
// 实机故障形状：08:00 生成需要读取上一交易日（2026-09-22）的密封财务基线，
// 该档案由旧代码代（fingerprint 2ad6c53d4091）写出，当前代码指纹为 235f07458b12。
// 修复前 loadVerifiedFinancialBaseline 不带编译指纹读取档案，于是按当前代码重建
// 候选目录并与旧代档案逐字比较，抛出 frozen candidate catalogue mismatch。
// 下面两个用例用“档案候选目录与当前代码重建结果确实不同”的材料复现该形状：
//   * 跨代密封档 -> 封存基线读取必须通过（修复前失败）
//   * 同代候选目录被篡改 -> 仍然必须拒绝（跨代放宽不得下沉到同代校验）
import { buildStructuredAnalysisCandidates } from "../src/structured-analysis.mjs";

test("跨代密封档候选目录与当前代码不一致时，仅封存基线读取跳过重算（2026-09-23 回归）", async (t) => {
  const { options, frozen } = await frozenArchive(t);
  const archiveInput = path.join(frozen.archiveDirectory, "input.json");
  const archiveSidecar = path.join(frozen.archiveDirectory, "input.sha256");
  const archiveNow = JSON.parse(await readFile(archiveInput, "utf8"));

  // 旧代码代写出的候选目录：与当前代码重建结果确实不同，并换成不同代编译指纹。
  const rebuiltNow = buildStructuredAnalysisCandidates(archiveNow.compilerContext).map((item) => item.id);
  const crossEraCandidateIds = rebuiltNow.length > 1
    ? rebuiltNow.slice(0, -1)
    : [...rebuiltNow, "theme:legacy-era:2026-08-10:0000000000000000"];
  assert.notDeepEqual(crossEraCandidateIds, rebuiltNow,
    "回归材料必须让档案候选目录与当前代码重建结果确实不同，否则测不到跳过重算这条路径");

  const crossEra = { ...archiveNow, code: otherEra(archiveNow.code), candidateIds: crossEraCandidateIds };
  const crossBody = `${JSON.stringify(crossEra)}\n`;
  await writeFile(archiveInput, crossBody);
  await writeFile(archiveSidecar, `${sha256(crossBody)}\n`);

  const code = await compilerIdentity(root);
  const legacy = { input: crossEra, inputSha256: sha256(crossBody), archiveDirectory: frozen.archiveDirectory };
  const report = compileFrozenReport(legacy.input);
  await sealRunResult(legacy, { report, overlay: null, analysisStatus: "disabled", analysisAttempts: 0 });
  await publishArchivedReport(legacy, path.join(options.outputDirectory, `${options.targetDate}.md`));

  // 修复后：封存基线读取按跨代处理，跳过候选重算并返回基线；修复前这里抛
  // frozen candidate catalogue mismatch，正是今早 08:00 的失败。
  const baseline = await loadVerifiedFinancialBaseline(options.archiveRoot, options.outputDirectory, "2026-08-11", code);
  assert.ok(baseline["000001"], "cross-era sealed baseline must load");
  assert.equal(baseline["000001"].baselineTargetDate, "2026-08-10");

  // 普通读取与回放保持时代锁，不因基线读取放宽而放行。
  await assert.rejects(readFrozenInput(frozen.archiveDirectory, { compilerCode: code }), /code differs/);
  await assert.rejects(replayRunArchive(root, frozen.archiveDirectory), /code differs/);
});

test("同代候选目录被篡改仍然拒绝（跨代跳过不得放宽同代校验）", async (t) => {
  const { frozen } = await frozenArchive(t);
  const archiveInput = path.join(frozen.archiveDirectory, "input.json");
  const archiveSidecar = path.join(frozen.archiveDirectory, "input.sha256");
  const archiveNow = JSON.parse(await readFile(archiveInput, "utf8"));
  // 只改候选目录，编译指纹保持当前代：必须走重算并逐字比较。
  const tampered = { ...archiveNow, candidateIds: [...archiveNow.candidateIds, "theme:invented:2026-08-10:deadbeef"] };
  const body = `${JSON.stringify(tampered)}\n`;
  await writeFile(archiveInput, body);
  await writeFile(archiveSidecar, `${sha256(body)}\n`);

  const code = await compilerIdentity(root);
  await assert.rejects(readFrozenInput(frozen.archiveDirectory, { compilerCode: code }), /candidate catalogue mismatch/);
  await assert.rejects(readFrozenInput(frozen.archiveDirectory), /candidate catalogue mismatch/);
  await assert.rejects(replayRunArchive(root, frozen.archiveDirectory), /candidate catalogue mismatch/);
});

for (const [name, mutate] of [
  ["missing", (code) => { delete code.filesSha256; }],
  ["empty", (code) => { code.filesSha256 = ""; }],
  ["malformed", (code) => { code.filesSha256 = "z".repeat(64); }],
  ["hash mismatch", (code) => { code.filesSha256 = "0".repeat(64); }],
  ["file manifest mismatch", (code) => { code.files["src/run-archive.mjs"] = "f".repeat(64); }],
  ["empty file manifest", (code) => { code.files = {}; code.filesSha256 = jsonHash(code.files); }],
  ["unsafe file identity", (code) => { code.files["../injected"] = "f".repeat(64); code.filesSha256 = jsonHash(code.files); }]
]) {
  test(`sealed baseline rejects ${name} code identity even with self-consistent outer hashes`, async (t) => {
    const { options, frozen } = await frozenArchive(t);
    const report = compileFrozenReport(frozen.input);
    await sealRunResult(frozen, { report, overlay: null, analysisStatus: "disabled", analysisAttempts: 0 });
    await publishArchivedReport(frozen, path.join(options.outputDirectory, `${options.targetDate}.md`));
    const input = structuredClone(frozen.input);
    mutate(input.code);
    input.candidateIds = ["unknown-legacy-candidate"];
    const body = `${JSON.stringify(input)}\n`;
    const inputSha256 = sha256(body);
    await writeFile(path.join(frozen.archiveDirectory, "input.json"), body);
    await writeFile(path.join(frozen.archiveDirectory, "input.sha256"), `${inputSha256}\n`);
    const resultPath = path.join(frozen.archiveDirectory, "result/result.json");
    const result = JSON.parse(await readFile(resultPath, "utf8"));
    result.inputSha256 = inputSha256;
    result.compilationInputSha256 = jsonHash({ inputSha256, selection: result.selection });
    await writeFile(resultPath, `${JSON.stringify(result)}\n`);
    const publishedPath = path.join(frozen.archiveDirectory, "published.json");
    const published = JSON.parse(await readFile(publishedPath, "utf8"));
    Object.assign(published, { inputSha256, resultSha256: jsonHash(result) });
    await writeFile(publishedPath, `${JSON.stringify(published)}\n`);
    await assert.rejects(loadVerifiedFinancialBaseline(options.archiveRoot, options.outputDirectory,
      "2026-08-11", await compilerIdentity(root)), /invalid compiler code identity/);
    await assert.rejects(readFrozenInput(frozen.archiveDirectory), /invalid compiler code identity/);
  });
}
