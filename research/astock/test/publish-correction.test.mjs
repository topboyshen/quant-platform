import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  cp,
  mkdir,
  mkdtemp,
  readFile,
  rm,
  symlink,
  writeFile
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { parseArgs, publishCorrection } from "../scripts/publish-correction.mjs";
import { buildVerifiedBaselineReport } from "../src/baseline-report.mjs";
import {
  assessPublicationGate,
  DATA_BRIEF_PUBLICATION_LEVEL,
  LEGACY_FORMAL_PUBLICATION_LEVEL
} from "../src/report.mjs";

const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const targetDate = "2026-08-02";
const cutoffInstant = new Date("2026-08-02T00:30:00.000Z");
const disclaimer =
  "本晨报用于信息整理与研究支持，不构成个性化投资建议；市场有风险，决策需独立核验。";

function contextFixture() {
  const completeness = {
    market: 50,
    capital: 50,
    news: 50,
    financial: 50,
    overall: 50
  };
  return {
    schemaVersion: "1.0",
    meta: {
      status: "partial",
      provider: "test-provider",
      sourceType: "fixture",
      targetDate,
      asOf: cutoffInstant.toISOString(),
      timezone: "Asia/Shanghai",
      isLive: false,
      dataCompleteness: completeness,
      warnings: []
    },
    data: {
      tradingCalendar: {
        targetDate,
        status: "trading_day",
        previousTradingDate: "2026-08-01",
        dataAsOf: "2026-08-01T23:00:00.000Z",
        sourceIds: ["calendar-source"]
      },
      overnightGlobal: [],
      previousAShareSession: {
        sessionDate: "2026-08-01",
        indices: [
          { code: "sh000001", name: "上证指数", close: 100, previousClose: 99, change: 1, changePercent: 1.010101, units: { close: "index_point", previousClose: "index_point", change: "index_point", changePercent: "percentage_point" }, dataAsOf: "2026-08-01T07:00:00.000Z", sourceIds: ["source-1"] },
          { code: "sz399001", name: "深证成指", close: 200, previousClose: 202, change: -2, changePercent: -0.990099, units: { close: "index_point", previousClose: "index_point", change: "index_point", changePercent: "percentage_point" }, dataAsOf: "2026-08-01T07:00:00.000Z", sourceIds: ["source-1"] },
          { code: "sz399006", name: "创业板指", close: 300, previousClose: 300, change: 0, changePercent: 0, units: { close: "index_point", previousClose: "index_point", change: "index_point", changePercent: "percentage_point" }, dataAsOf: "2026-08-01T07:00:00.000Z", sourceIds: ["source-1"] }
        ],
        totalTurnover: 0,
        marketBreadth: { advancers: 0, decliners: 0, unchanged: 0 },
        dataAsOf: "2026-08-01T07:00:00.000Z",
        sourceIds: ["source-1"]
      },
      policyAndCalendar: [],
      sectors: [],
      capitalFlows: [],
      watchlist: [],
      announcementsAndNews: [],
      sectionCompleteness: {
        tradingCalendar: 50,
        overnightGlobal: 50,
        previousAShareSession: 50,
        policyAndCalendar: 50,
        sectors: 50,
        capitalFlows: 50,
        watchlist: 50,
        announcementsAndNews: 50
      }
    },
    sources: [
      {
        id: "source-1",
        title: "测试数据源",
        url: "https://example.com/market",
        publisher: "测试发布方",
        sourceType: "market_data",
        publishedAt: null,
        dataAsOf: "2026-08-01T07:00:00.000Z"
      },
      {
        id: "calendar-source",
        title: "交易日历来源",
        url: "https://example.com/calendar",
        publisher: "测试发布方",
        sourceType: "trading_calendar_data",
        publishedAt: null,
        dataAsOf: "2026-08-01T23:00:00.000Z"
      }
    ]
  };
}

function reportFixture(publicationLevel = LEGACY_FORMAL_PUBLICATION_LEVEL) {
  const context = contextFixture();
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant,
    targetDate
  });
  assert.equal(publicationAudit.passed, true);
  return buildVerifiedBaselineReport({
    context,
    targetDate,
    cutoffLocal: "2026-08-02 08:30",
    cutoffInstant,
    publicationLevel,
    publicationAudit,
    disclaimer,
    marketDataMaxAgeHours: 18,
    newsMaxAgeHours: 72
  });
}

async function makeProject(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "astock-correction-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await cp(path.join(sourceRoot, "config"), path.join(root, "config"), { recursive: true });
  const runtimeRoot = path.join(root, "runtime");
  await mkdir(runtimeRoot, { recursive: true });
  const inputPath = path.join(runtimeRoot, "correction.md");
  const contextPath = path.join(runtimeRoot, "context.json");
  const contextBody = Buffer.from(`${JSON.stringify(contextFixture(), null, 2)}\n`);
  await Promise.all([
    writeFile(inputPath, reportFixture()),
    writeFile(contextPath, contextBody)
  ]);
  return { root, runtimeRoot, inputPath, contextPath, contextBody };
}

function options(fixture, revision = "v1") {
  return {
    projectRoot: fixture.root,
    runtimeRoot: fixture.runtimeRoot,
    targetDate,
    revision,
    input: fixture.inputPath,
    context: fixture.contextPath,
    cutoffInstant,
    now: new Date("2026-08-02T01:00:00.000Z")
  };
}

test("固定 CLI 拒绝缺参、重复参数和非正整数版本", () => {
  assert.throws(() => parseArgs([]), /用法/);
  assert.throws(() => parseArgs([
    "--date", targetDate,
    "--revision", "v1",
    "--input", "a",
    "--input", "b",
    "--cutoff", cutoffInstant.toISOString()
  ]), /不得重复/);
  assert.throws(() => parseArgs([
    "--date", targetDate,
    "--revision", "v0",
    "--input", "a",
    "--context", "b",
    "--cutoff", cutoffInstant.toISOString()
  ]), /vN/);
});

test("通过门禁后独占发布报告、证据和回执且不修改 daily/last-run", async (t) => {
  const fixture = await makeProject(t);
  const dailyPath = path.join(fixture.root, "reports", "daily", `${targetDate}.md`);
  const lastRunPath = path.join(fixture.root, "work", "last-run.json");
  await mkdir(path.dirname(dailyPath), { recursive: true });
  await mkdir(path.dirname(lastRunPath), { recursive: true });
  const dailyBefore = Buffer.from("legacy daily bytes\n");
  const lastRunBefore = Buffer.from('{"legacy":true}\n');
  await Promise.all([writeFile(dailyPath, dailyBefore), writeFile(lastRunPath, lastRunBefore)]);

  const result = await publishCorrection(options(fixture));
  assert.equal(result.created, true);
  const [report, evidence, gate, dailyAfter, lastRunAfter] = await Promise.all([
    readFile(result.reportPath),
    readFile(result.evidencePath),
    readFile(result.gatePath, "utf8").then(JSON.parse),
    readFile(dailyPath),
    readFile(lastRunPath)
  ]);
  assert.deepEqual(evidence, fixture.contextBody);
  assert.deepEqual(dailyAfter, dailyBefore);
  assert.deepEqual(lastRunAfter, lastRunBefore);
  assert.equal(gate.schemaVersion, "1.0");
  assert.equal(gate.eventType, "correction_gate");
  assert.equal(gate.contextFile, `work/corrections/evidence/${targetDate}-v1.json`);
  assert.equal(gate.reportFile, `reports/corrections/${targetDate}-v1.md`);
  assert.equal(gate.contextSha256, createHash("sha256").update(evidence).digest("hex"));
  assert.equal(gate.reportSha256, createHash("sha256").update(report).digest("hex"));
  assert.equal(gate.contextBytes, evidence.length);
  assert.equal(gate.reportBytes, report.length);
  assert.equal(gate.evidenceCutoffIso, cutoffInstant.toISOString());
  assert.equal(gate.contextMetaAsOf, cutoffInstant.toISOString());
  assert.equal(gate.publicationAudit.passed, true);
  assert.equal(gate.passed, true);
});

test("更正版同时接受新基础快报级别并保持旧稿兼容", async (t) => {
  const fixture = await makeProject(t);
  const legacyFreeAnalysis = reportFixture().replace(
    "程序核验基础版不生成因果推断、价格目标、买卖或仓位建议。",
    "程序核验基础版不生成因果推断、价格目标、买卖或仓位建议。\n" +
      "- 企业经营线索可能与地缘局势变化有关，仍需观察反证。"
  );
  await writeFile(fixture.inputPath, legacyFreeAnalysis);
  await assert.rejects(
    publishCorrection(options(fixture)),
    /生产发布只接受程序编译器逐字生成/,
    "旧发布级别只能兼容程序编译稿，不能恢复自由研究文本"
  );
  const newLabelReport = reportFixture(DATA_BRIEF_PUBLICATION_LEVEL);
  await writeFile(fixture.inputPath, newLabelReport);
  const result = await publishCorrection(options(fixture));
  const normalized = await readFile(result.reportPath, "utf8");
  assert.match(normalized, /AStock 数据快报/);
  assert.match(normalized, /发布级别：可发布（结构、来源与时效门禁通过；研究语义未审校）/);
});

test("完全重复幂等成功，同版本内容冲突失败且不改既有产物", async (t) => {
  const fixture = await makeProject(t);
  const first = await publishCorrection(options(fixture));
  const before = await Promise.all([
    readFile(first.reportPath),
    readFile(first.evidencePath),
    readFile(first.gatePath)
  ]);
  const repeated = await publishCorrection({
    ...options(fixture),
    now: new Date("2026-08-02T02:00:00.000Z")
  });
  assert.equal(repeated.idempotent, true);

  await writeFile(fixture.inputPath, reportFixture(DATA_BRIEF_PUBLICATION_LEVEL));
  await assert.rejects(publishCorrection(options(fixture)), /内容冲突/);
  await writeFile(fixture.inputPath, reportFixture());
  const changedContext = contextFixture();
  changedContext.meta.provider = "different-provider";
  await writeFile(fixture.contextPath, `${JSON.stringify(changedContext, null, 2)}\n`);
  await assert.rejects(publishCorrection(options(fixture)), /内容冲突/);
  const after = await Promise.all([
    readFile(first.reportPath),
    readFile(first.evidencePath),
    readFile(first.gatePath)
  ]);
  assert.deepEqual(after, before);
});

test("publication gate 阻断时不产生任何更正版文件", async (t) => {
  const fixture = await makeProject(t);
  const blocked = contextFixture();
  blocked.data.previousAShareSession.indices[0].close = 0;
  await writeFile(fixture.contextPath, `${JSON.stringify(blocked, null, 2)}\n`);
  await assert.rejects(publishCorrection(options(fixture, "v2")), /发布门禁未通过/);
  await assert.rejects(readFile(path.join(fixture.root, "reports", "corrections", `${targetDate}-v2.md`)), /ENOENT/);
  await assert.rejects(readFile(path.join(fixture.root, "work", "corrections", "gates", `${targetDate}-v2.json`)), /ENOENT/);
});

test("拒绝越界、符号链接和超过 1 MiB 的输入", async (t) => {
  const fixture = await makeProject(t);
  const outsideRoot = await mkdtemp(path.join(os.tmpdir(), "astock-correction-outside-"));
  t.after(() => rm(outsideRoot, { recursive: true, force: true }));
  const outside = path.join(outsideRoot, "outside.md");
  await writeFile(outside, reportFixture());
  await assert.rejects(publishCorrection({ ...options(fixture, "v3"), input: outside }), /必须位于项目根或运行工作目录内/);

  const oversized = path.join(fixture.runtimeRoot, "oversized.md");
  await writeFile(oversized, Buffer.alloc(1024 * 1024 + 1, 0x61));
  await assert.rejects(publishCorrection({ ...options(fixture, "v3"), input: oversized }), /超过 1024 KiB/);

  let linked = path.join(fixture.runtimeRoot, "linked.md");
  try {
    await symlink(fixture.inputPath, linked, "file");
  } catch (error) {
    if (["EPERM", "EACCES", "ENOSYS"].includes(error.code)) {
      const realDirectory = path.join(fixture.runtimeRoot, "real-directory");
      const linkedDirectory = path.join(fixture.runtimeRoot, "linked-directory");
      await mkdir(realDirectory);
      await writeFile(path.join(realDirectory, "report.md"), reportFixture());
      try {
        await symlink(realDirectory, linkedDirectory, "junction");
      } catch (junctionError) {
        if (["EPERM", "EACCES", "ENOSYS"].includes(junctionError.code)) {
          t.diagnostic(`当前平台不能创建测试符号链接或 junction: ${junctionError.code}`);
          return;
        }
        throw junctionError;
      }
      linked = path.join(linkedDirectory, "report.md");
    } else {
      throw error;
    }
  }
  await assert.rejects(publishCorrection({ ...options(fixture, "v3"), input: linked }), /符号链接/);
});
