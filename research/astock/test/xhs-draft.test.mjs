import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  stat,
  symlink,
  writeFile
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  buildXhsDraft,
  XhsDraftIntegrityError
} from "../src/xhs-draft.mjs";
import { parseXhsDraftArgs } from "../scripts/xhs-draft.mjs";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const TARGET_DATE = "2026-08-21";
const CUTOFF_LOCAL = "2026-08-21 08:00";
const DISCLAIMER = "本晨报用于信息整理与研究支持，不构成个性化投资建议；市场有风险，决策需独立核验。";
const NOW = new Date("2026-08-21T00:30:00.000Z");

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function graphemeLength(value) {
  return [...new Intl.Segmenter("zh-Hans", { granularity: "grapheme" }).segment(value)].length;
}

function report({ dataStatus = "partial", extra = "" } = {}) {
  const dataLabel = dataStatus === "live" ? "实时数据包" : "部分可用";
  return [
    `# AStock 数据快报（语义未审）｜${TARGET_DATE}`,
    "",
    `> 数据截止：${CUTOFF_LOCAL}（Asia/Shanghai）`,
    `> 数据状态：${dataLabel}`,
    "> 交易日状态：交易日 [来源 1]",
    "> 发布级别：可发布（结构、来源与时效门禁通过；研究语义未审校）",
    "> 生成方式：程序核验基础版",
    "",
    "## 一句话结论",
    "",
    "[事实] 程序已核验三大指数与股票池行情；本版只报告可复核字段，不生成因果判断。",
    "",
    "## 1. 数据覆盖与可用性",
    "",
    "> 数据桥模块评分只衡量模块新鲜度与可用性，不代表字段齐全或数值准确。",
    "> 正式发布必要项覆盖：4/5；X/X 不代表全部行情字段齐全；必要项缺口：市场宽度未能从固定数据包核验。",
    "",
    "| 类别 | 数据桥模块评分（非准确性） |",
    "|---|---:|",
    "| 行情 | 90% |",
    "| 资金 | 70% |",
    "| 财务 | 100% |",
    "",
    "## 2. 上一交易日 A 股行情（2026-08-20）",
    "",
    "- 上证指数收于 3724.88 点，较前收上涨 0.38%。[来源 1]",
    "- 深证成指收于 11873.25 点，较前收上涨 0.61%。[来源 1]",
    "- 创业板指收于 2578.91 点，较前收下跌 0.12%。[来源 1]",
    "- 沪深两市成交额为 18420 亿元，市场宽度字段缺失。[来源 1]",
    "",
    "## 3. 隔夜数据与事件",
    "",
    "程序核验基础版未生成跨市场因果解释。",
    "",
    "## 4. 板块与股票池",
    "",
    "### 板块排行事实",
    "",
    "- 通信设备板块涨幅靠前，日涨幅 2.14%。[来源 1]",
    "- 银行板块跌幅靠前，日跌幅 0.73%。[来源 1]",
    "",
    "### 股票池",
    "",
    "### 600519 **贵州茅台** https://example.com/should-not-leak",
    "- [事实] 收盘 1452.30 元，较前收上涨 0.45%。[来源 1]",
    "- 财务字段缺少可引用的报告期来源，保持为空。",
    "### 300750 宁德时代",
    "- [事实] 收盘 236.80 元，较前收下跌 0.86%。[来源 1]",
    "- 公告字段没有截止前的一手来源，不作补猜。",
    "### 000001 平安银行",
    "- [事实] 行情变化：收盘 12.34 元，较前收上涨 0.24%。[来源 1]",
    "- 财务与公告字段没有截止前的一手来源，不作补猜。",
    "",
    "## 5. 今日政策、公告与事件",
    "",
    "程序核验基础版未编入事件判断。",
    "",
    "## 6. 结构化分析覆盖层",
    "",
    "本次没有程序证明合格的语义候选。",
    "",
    "## 7. 风险提示与数据缺口",
    "",
    "- 正式发布必要项缺口：市场宽度未能从固定数据包核验。",
    "- 数据桥模块缺口：资金评分为 70%，不代表字段齐全。",
    "- 股票池财务与公告字段并不完整，缺失内容没有补猜。",
    "- 行情数字只对应最近已确认交易日，不代表盘中实时价格。",
    extra,
    "",
    "## 来源",
    "",
    "> 以下仅列出本报告实际引用的固定数据包来源。",
    "",
    "1. 上海证券交易所日行情 — market_data（上海证券交易所）；数据时间：2026-08-20 15:00（Asia/Shanghai）；https://example.com/sse-daily",
    "",
    "---",
    "",
    DISCLAIMER,
    ""
  ].join("\n");
}

function lastRun(overrides = {}) {
  return {
    schemaVersion: "1.0",
    targetDate: TARGET_DATE,
    startedAt: "2026-08-21T00:00:00.000Z",
    frozenCutoffInstant: "2026-08-21T00:00:00.000Z",
    cutoffLocal: CUTOFF_LOCAL,
    status: "success",
    phase: "publish",
    completedAt: "2026-08-21T00:20:00.000Z",
    dataStatus: "partial",
    publicationGate: "passed",
    publicationBlockers: [],
    evidenceReview: "passed",
    evidenceReviewScope: "structure_source_freshness_only",
    researchSemanticReview: "not_human_reviewed",
    contextSha256: "a".repeat(64),
    semanticCandidateCount: 0,
    reportFile: `reports/daily/${TARGET_DATE}.md`,
    ...overrides
  };
}

async function makeFixture(t, { state = lastRun(), reportBody = report(), configPatch = {} } = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "astock-xhs-test-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(path.join(root, "config"), { recursive: true });
  await mkdir(path.join(root, "reports", "daily"), { recursive: true });
  await mkdir(path.join(root, "work", "notifications"), { recursive: true });
  const config = {
    schemaVersion: "1.0",
    draftEnabled: true,
    autoPublishEnabled: false,
    humanApprovalRequired: true,
    outputRoot: "work/xhs",
    maxReportBytes: 262144,
    maxTitleGraphemes: 20,
    minBodyGraphemes: 200,
    maxBodyGraphemes: 900,
    defaultTags: ["A股", "财经", "市场复盘"],
    ...configPatch
  };
  await writeFile(path.join(root, "config", "xhs.json"), `${JSON.stringify(config, null, 2)}\n`);
  await writeFile(path.join(root, "config", "risk.json"), `${JSON.stringify({
    schemaVersion: "1.0",
    disclaimer: DISCLAIMER
  }, null, 2)}\n`);
  await writeFile(path.join(root, "work", "last-run.json"), `${JSON.stringify(state, null, 2)}\n`);
  await writeFile(path.join(root, "reports", "daily", `${TARGET_DATE}.md`), reportBody);
  const notificationMarker = path.join(root, "work", "notifications", "marker.json");
  await writeFile(notificationMarker, "{\"sent\":false}\n");
  return { root, notificationMarker };
}

async function readManifest(root, result) {
  return JSON.parse(await readFile(path.join(root, ...result.manifestFile.split("/")), "utf8"));
}

test("合格 partial 正式晨报只生成 facts_only 草稿且不触碰通知目录", async (t) => {
  const fixture = await makeFixture(t);
  const notificationBefore = sha256(await readFile(fixture.notificationMarker));
  const result = await buildXhsDraft({ projectRoot: fixture.root, now: NOW });
  assert.equal(result.status, "draft_ready");
  const manifest = await readManifest(fixture.root, result);
  assert.equal(manifest.state, "DRAFT_ONLY");
  assert.equal(manifest.contentMode, "facts_only");
  assert.equal(manifest.publishEligible, false);
  assert.equal(manifest.humanApprovalRequired, true);
  assert.deepEqual(manifest.publishBlockers, ["AUTO_PUBLISH_DISABLED", "HUMAN_APPROVAL_REQUIRED"]);
  assert.equal(manifest.source.dataStatus, "partial");
  assert.equal(manifest.source.semanticCandidateCount, 0);
  assert.match(manifest.content.body, /数据状态：部分可用/);
  assert.match(manifest.content.body, /【行情日期】以下行情均为 2026-08-20 收盘数据。/);
  assert.match(manifest.content.body, /数据缺口/);
  assert.match(manifest.content.body, /【数据缺口】正式发布必要项缺口：市场宽度未能从固定数据包核验。/);
  assert.match(manifest.content.body, /本草稿为基于固定数据包的程序化事实摘录/);
  assert.doesNotMatch(manifest.content.body, /【覆盖】/);
  assert.doesNotMatch(manifest.content.body, /X\/X/);
  assert.doesNotMatch(manifest.content.body, /(?:行情|资金|新闻|财务)[：:]\s*\d+(?:\.\d+)?%/);
  assert.doesNotMatch(manifest.content.body, /确定性事实摘录/);
  assert.doesNotMatch(manifest.content.body, /\|/);
  assert.doesNotMatch(manifest.content.body, /should-not-leak/);
  assert.ok(graphemeLength(manifest.content.body) <= 900);
  for (const [stock, firstFact] of [
    ["600519 贵州茅台", "收盘 1452.30 元"],
    ["300750 宁德时代", "收盘 236.80 元"],
    ["000001 平安银行", "行情变化：收盘 12.34 元"]
  ]) {
    assert.match(manifest.content.body, new RegExp(`【股票池】${stock}：${firstFact}`));
  }
  assert.doesNotMatch(manifest.content.body, /买入|卖出|仓位|目标价|止损/);
  assert.equal(sha256(await readFile(fixture.notificationMarker)), notificationBefore);
  assert.deepEqual(await readdir(path.join(fixture.root, "work", "notifications")), ["marker.json"]);
});

test("紧预算会为全部股票摘要预留空间而不是静默截断", async (t) => {
  const fixture = await makeFixture(t, {
    configPatch: { minBodyGraphemes: 200, maxBodyGraphemes: 600 }
  });
  const result = await buildXhsDraft({ projectRoot: fixture.root, now: NOW });
  const manifest = await readManifest(fixture.root, result);
  assert.ok(graphemeLength(manifest.content.body) <= 600);
  assert.match(manifest.content.body, /【行情日期】以下行情均为 2026-08-20 收盘数据。/);
  assert.match(manifest.content.body, /【数据缺口】/);
  for (const stock of ["600519 贵州茅台", "300750 宁德时代", "000001 平安银行"]) {
    assert.match(manifest.content.body, new RegExp(`【股票池】${stock}：`));
  }
});

test("长行情候选不能挤掉 partial 的必需具体缺口", async (t) => {
  const longMarketFact = `上证指数收于 3724.88 点，较前收上涨 0.38%。${"补充行情字段仅用于预算回归。".repeat(30)}[来源 1]`;
  const reportBody = report().replace(
    "上证指数收于 3724.88 点，较前收上涨 0.38%。[来源 1]",
    longMarketFact
  );
  const fixture = await makeFixture(t, {
    reportBody,
    configPatch: { minBodyGraphemes: 200, maxBodyGraphemes: 600 }
  });
  const result = await buildXhsDraft({ projectRoot: fixture.root, now: NOW });
  const manifest = await readManifest(fixture.root, result);
  assert.match(manifest.content.body, /【数据缺口】正式发布必要项缺口：市场宽度未能从固定数据包核验。/);
  assert.doesNotMatch(manifest.content.body, /补充行情字段仅用于预算回归/);
  assert.ok(graphemeLength(manifest.content.body) <= 600);
});

test("官方披露查询来源使用实际查询完成时间仍可解析", async (t) => {
  const extra = [
    "2. 巨潮公告查询摘要 — official_disclosure_query（巨潮资讯网）；实际查询完成时间：2026-08-21 07:50（Asia/Shanghai）；https://example.com/cninfo-query"
  ].join("\n");
  const reportBody = report().replace(
    "https://example.com/sse-daily",
    `https://example.com/sse-daily\n${extra}`
  );
  const fixture = await makeFixture(t, { reportBody });
  const result = await buildXhsDraft({ projectRoot: fixture.root, now: NOW });
  assert.equal(result.status, "draft_ready");
  const manifest = await readManifest(fixture.root, result);
  assert.equal(manifest.content.sources.length, 2);
  assert.equal(manifest.content.sources[1].url, "https://example.com/cninfo-query");
  assert.match(manifest.content.body, /原报告列出 2 个带时间的固定数据来源/);
});

test("官方公告来源使用披露日期与实际采集时间仍可解析", async (t) => {
  const extra = [
    "2. [陈旧] 2026年半年度报告摘要 — official_announcement（巨潮资讯网）；披露日期：2026-08-26（仅日期，发布时间未核实）；实际采集时间：2026-08-21 08:00（Asia/Shanghai）；https://static.cninfo.com.cn/finalpage/2026-08-26/1225501876.PDF",
    "3. 2026年半年度分红派息实施公告 — official_announcement（巨潮资讯网）；披露日期：2026-08-20（仅日期，发布时间未核实）；实际采集时间：2026-08-21 08:00（Asia/Shanghai）；https://static.cninfo.com.cn/finalpage/2026-08-20/1225566652.PDF"
  ].join("\n");
  const reportBody = report().replace(
    "https://example.com/sse-daily",
    `https://example.com/sse-daily\n${extra}`
  );
  const fixture = await makeFixture(t, { reportBody });
  const result = await buildXhsDraft({ projectRoot: fixture.root, now: NOW });
  assert.equal(result.status, "draft_ready");
  const manifest = await readManifest(fixture.root, result);
  assert.equal(manifest.content.sources.length, 3);
  assert.equal(manifest.content.sources[1].url, "https://static.cninfo.com.cn/finalpage/2026-08-26/1225501876.PDF");
  assert.match(manifest.content.body, /原报告列出 3 个带时间的固定数据来源/);
});

test("上一交易日日期必须有效且早于目标日期", async (t) => {
  for (const marketDate of ["2026-02-30", TARGET_DATE, "2026-08-22"]) {
    const reportBody = report().replace("2026-08-20）", `${marketDate}）`);
    const fixture = await makeFixture(t, { reportBody });
    await assert.rejects(
      buildXhsDraft({ projectRoot: fixture.root, now: NOW }),
      (error) => error instanceof XhsDraftIntegrityError &&
        error.code === "REPORT_CONTRACT_MISMATCH" &&
        /必须有效且早于目标日期/u.test(error.message)
    );
  }
});

test("正文上限容不下完整股票摘要时失败关闭", async (t) => {
  const budget = { minBodyGraphemes: 100, maxBodyGraphemes: 350 };
  const withoutStocks = report().replace(
    /### 股票池[\s\S]*?\n## 5\./u,
    "### 股票池\n\n暂无可核验股票。\n\n## 5."
  );
  const baseline = await makeFixture(t, {
    reportBody: withoutStocks,
    configPatch: budget
  });
  const baselineResult = await buildXhsDraft({ projectRoot: baseline.root, now: NOW });
  assert.equal(baselineResult.status, "draft_ready");

  const fixture = await makeFixture(t, {
    configPatch: budget
  });
  await assert.rejects(
    buildXhsDraft({ projectRoot: fixture.root, now: NOW }),
    (error) => error instanceof XhsDraftIntegrityError &&
      error.code === "CONTENT_CONTRACT_FAILED" &&
      /完整保留股票池摘要/u.test(error.message)
  );
});

test("后续 Markdown 标题会结束无事实股票 block", async (t) => {
  const reportBody = report().replace(
    "### 300750 宁德时代\n- [事实] 收盘 236.80 元，较前收下跌 0.86%。[来源 1]",
    "### 300750 宁德时代\n#### 观察\n- [事实] 这条内容不属于股票首行情事实。[来源 1]"
  );
  const fixture = await makeFixture(t, { reportBody });
  const result = await buildXhsDraft({ projectRoot: fixture.root, now: NOW });
  const manifest = await readManifest(fixture.root, result);
  assert.match(manifest.content.body, /【股票池】300750 宁德时代(?:\n|$)/u);
  assert.doesNotMatch(manifest.content.body, /这条内容不属于股票首行情事实/u);
});

test("live 正式晨报生成草稿，研究语义仍不自动升级", async (t) => {
  const fixture = await makeFixture(t, {
    state: lastRun({ dataStatus: "live", researchSemanticReview: "human_passed", semanticCandidateCount: 2 }),
    reportBody: report({ dataStatus: "live" })
  });
  const result = await buildXhsDraft({ projectRoot: fixture.root, now: NOW });
  const manifest = await readManifest(fixture.root, result);
  assert.equal(manifest.source.dataStatus, "live");
  assert.equal(manifest.source.researchSemanticReview, "human_passed");
  assert.equal(manifest.contentMode, "facts_only");
  assert.equal(manifest.publishEligible, false);
});

test("running、failed、blocked、skipped 与未通过门禁均 held 且没有草稿", async (t) => {
  const cases = [
    { status: "running", phase: "collect", completedAt: undefined },
    { status: "failed", phase: "validate", completedAt: undefined },
    { status: "blocked", publicationGate: "blocked" },
    { status: "skipped", phase: "collect", completedAt: undefined },
    { evidenceReview: "blocked" }
  ];
  for (const patch of cases) {
    const state = lastRun(patch);
    if (patch.completedAt === undefined) delete state.completedAt;
    const fixture = await makeFixture(t, { state });
    const result = await buildXhsDraft({ projectRoot: fixture.root, now: NOW });
    assert.equal(result.status, "held");
    await assert.rejects(lstat(path.join(fixture.root, "work", "xhs", "drafts")), { code: "ENOENT" });
  }
});

test("普通非正式报告路径 held，路径穿越与反斜杠按完整性错误关闭", async (t) => {
  const provisional = await makeFixture(t, {
    state: lastRun({ reportFile: `reports/provisional/${TARGET_DATE}.md` })
  });
  const held = await buildXhsDraft({ projectRoot: provisional.root, now: NOW });
  assert.equal(held.status, "held");
  assert.deepEqual(held.reasonCodes, ["FORMAL_REPORT_PATH_REQUIRED"]);

  for (const reportFile of ["../outside.md", `reports\\daily\\${TARGET_DATE}.md`]) {
    const attacked = await makeFixture(t, { state: lastRun({ reportFile }) });
    await assert.rejects(
      buildXhsDraft({ projectRoot: attacked.root, now: NOW }),
      (error) => error instanceof XhsDraftIntegrityError && error.code === "UNSAFE_REPORT_PATH"
    );
  }
});

test("正式报告符号链接、超限文件与无效 UTF-8 全部失败关闭", async (t) => {
  const linked = await makeFixture(t);
  const reportPath = path.join(linked.root, "reports", "daily", `${TARGET_DATE}.md`);
  const realPath = path.join(linked.root, "reports", "daily", "real.md");
  await writeFile(realPath, report());
  await rm(reportPath);
  try {
    await symlink(realPath, reportPath, "file");
    await assert.rejects(
      buildXhsDraft({ projectRoot: linked.root, now: NOW }),
      (error) => error instanceof XhsDraftIntegrityError && error.code === "UNSAFE_INPUT_FILE"
    );
  } catch (error) {
    if (error?.code === "EPERM") t.diagnostic("当前 Windows 环境不允许创建测试符号链接");
    else throw error;
  }

  const oversized = await makeFixture(t, {
    reportBody: `${report()}${"x".repeat(2048)}`,
    configPatch: { maxReportBytes: 1024 }
  });
  await assert.rejects(
    buildXhsDraft({ projectRoot: oversized.root, now: NOW }),
    (error) => error instanceof XhsDraftIntegrityError && error.code === "INPUT_SIZE_INVALID"
  );

  const invalidUtf8 = await makeFixture(t);
  await writeFile(
    path.join(invalidUtf8.root, "reports", "daily", `${TARGET_DATE}.md`),
    Buffer.from([0xc3, 0x28])
  );
  await assert.rejects(
    buildXhsDraft({ projectRoot: invalidUtf8.root, now: NOW }),
    (error) => error instanceof XhsDraftIntegrityError && error.code === "INVALID_UTF8"
  );
});

test("相同报告幂等 noop 且不可变草稿哈希与时间不变", async (t) => {
  const fixture = await makeFixture(t);
  const first = await buildXhsDraft({ projectRoot: fixture.root, now: NOW });
  const manifestPath = path.join(fixture.root, ...first.manifestFile.split("/"));
  const markdownPath = manifestPath.replace(/\.json$/u, ".md");
  const before = {
    manifestHash: sha256(await readFile(manifestPath)),
    markdownHash: sha256(await readFile(markdownPath)),
    manifestMtime: (await stat(manifestPath)).mtimeMs,
    markdownMtime: (await stat(markdownPath)).mtimeMs
  };
  const second = await buildXhsDraft({ projectRoot: fixture.root, now: new Date(NOW.getTime() + 1_000) });
  assert.equal(second.status, "noop");
  assert.equal(second.draftId, first.draftId);
  assert.equal(sha256(await readFile(manifestPath)), before.manifestHash);
  assert.equal(sha256(await readFile(markdownPath)), before.markdownHash);
  assert.equal((await stat(manifestPath)).mtimeMs, before.manifestMtime);
  assert.equal((await stat(markdownPath)).mtimeMs, before.markdownMtime);
});

test("同日正式报告内容变化生成新的哈希版本且不覆盖旧版", async (t) => {
  const fixture = await makeFixture(t);
  const first = await buildXhsDraft({ projectRoot: fixture.root, now: NOW });
  const firstBytes = await readFile(path.join(fixture.root, ...first.manifestFile.split("/")));
  await writeFile(
    path.join(fixture.root, "reports", "daily", `${TARGET_DATE}.md`),
    report({ extra: "- 补充缺口：成交额分市场口径未单独披露。" })
  );
  const second = await buildXhsDraft({ projectRoot: fixture.root, now: new Date(NOW.getTime() + 1_000) });
  assert.equal(second.status, "draft_ready");
  assert.notEqual(second.manifestFile, first.manifestFile);
  assert.deepEqual(await readFile(path.join(fixture.root, ...first.manifestFile.split("/"))), firstBytes);
});

test("构建期间 last-run 或报告变化时不产生任何草稿", async (t) => {
  for (const target of ["state", "report"]) {
    const fixture = await makeFixture(t);
    await assert.rejects(
      buildXhsDraft({
        projectRoot: fixture.root,
        now: NOW,
        beforeCommit: async () => {
          if (target === "state") {
            await writeFile(
              path.join(fixture.root, "work", "last-run.json"),
              `${JSON.stringify({ ...lastRun(), observedRace: true }, null, 2)}\n`
            );
          } else {
            await writeFile(
              path.join(fixture.root, "reports", "daily", `${TARGET_DATE}.md`),
              report({ extra: "- 读取期间内容变化。" })
            );
          }
        }
      }),
      (error) => error instanceof XhsDraftIntegrityError && error.code === "SOURCE_CHANGED_DURING_BUILD"
    );
    await assert.rejects(lstat(path.join(fixture.root, "work", "xhs", "drafts")), { code: "ENOENT" });
  }
});

test("当前日期、新鲜度和未来完成时间均按 held 处理", async (t) => {
  const old = await makeFixture(t);
  const result = await buildXhsDraft({
    projectRoot: old.root,
    now: new Date("2026-08-22T00:30:00.000Z"),
    requireCurrentDate: true,
    maxAgeMinutes: 120
  });
  assert.equal(result.status, "held");
  assert.ok(result.reasonCodes.includes("NOT_CURRENT_SHANGHAI_DATE"));
  assert.ok(result.reasonCodes.includes("REPORT_TOO_OLD"));

  const future = await makeFixture(t, {
    state: lastRun({ completedAt: "2026-08-21T01:00:00.000Z" })
  });
  const futureResult = await buildXhsDraft({ projectRoot: future.root, now: NOW });
  assert.equal(futureResult.status, "held");
  assert.ok(futureResult.reasonCodes.includes("COMPLETED_AT_IN_FUTURE"));
});

test("CLI 不接受任意输入、报告、输出或 URL 参数", () => {
  assert.deepEqual(parseXhsDraftArgs(["status"]), { command: "status" });
  assert.equal(parseXhsDraftArgs(["build", "--date", TARGET_DATE]).desiredDate, TARGET_DATE);
  for (const argument of ["--input", "--report", "--output", "--url"]) {
    assert.throws(() => parseXhsDraftArgs(["build", argument, "anything"]), /未知参数/);
  }
  assert.throws(
    () => parseXhsDraftArgs(["build", "--date", TARGET_DATE, "--date", TARGET_DATE]),
    /不得重复/
  );
});

test("systemd 自动链只从报告源触发密封，草稿保留按需入口", async () => {
  assert.deepEqual(
    (await readdir(path.join(repositoryRoot, "systemd"))).sort(),
    [
      "astock-cninfo-precollect.service",
      "astock-cninfo-precollect.timer",
      "astock-morning-notify.service",
      "astock-morning-notify.service.d",
      "astock-research-seal.service",
      "astock-research-seal.service.d",
      "astock-seal-failure-alert.service",
      "astock-xhs-draft.service",
      "astock-xhs-draft.service.d",
      "astock-xhs-source.service",
      "astock-xhs-source.service.d",
      "astock-xhs-source.timer"
    ]
  );
  const [sourceService, sourceTimer, draftService, wrapper, sealDropIn, sealAlert, sealService, sealNotify] = await Promise.all([
    readFile(path.join(repositoryRoot, "systemd", "astock-xhs-source.service"), "utf8"),
    readFile(path.join(repositoryRoot, "systemd", "astock-xhs-source.timer"), "utf8"),
    readFile(path.join(repositoryRoot, "systemd", "astock-xhs-draft.service"), "utf8"),
    readFile(path.join(repositoryRoot, "scripts", "run-morning-cron.sh"), "utf8"),
    readFile(path.join(repositoryRoot, "systemd", "astock-xhs-draft.service.d", "failure-alert.conf"), "utf8"),
    readFile(path.join(repositoryRoot, "systemd", "astock-seal-failure-alert.service"), "utf8"),
    readFile(path.join(repositoryRoot, "systemd", "astock-research-seal.service"), "utf8"),
    readFile(path.join(repositoryRoot, "systemd", "astock-research-seal.service.d", "notify.conf"), "utf8")
  ]);
  assert.match(sourceService, /^Environment=ASTOCK_FINAL_DELIVERY=disabled$/m);
  assert.doesNotMatch(sourceService, /^OnSuccess=astock-xhs-draft\.service$/m);
  assert.match(sourceService, /run-orchestrated-morning-cron\.sh/);
  assert.match(sourceTimer, /OnCalendar=Mon\.\.Fri \*-\*-\* 08:00:00 Asia\/Shanghai/);
  // 通知入口只经 drop-in 增加（主文件只保留 digest）；草稿不再触发通知。
  assert.match(sealService, /^OnSuccess=nng8-research-digest\.service$/m);
  assert.doesNotMatch(sealService, /astock-morning-notify\.service/);
  assert.match(sealNotify, /^OnSuccess=astock-morning-notify\.service$/m);
  assert.doesNotMatch(draftService, /astock-morning-notify\.service/);
  assert.match(draftService, /scripts\/xhs-draft\.mjs build --require-current-date --max-age-minutes 120/);
  assert.match(draftService, /^RestrictAddressFamilies=AF_UNIX$/m);
  assert.doesNotMatch(draftService, /hermes|telegram|publish/i);
  assert.equal((wrapper.match(/hermes-notify\.mjs deliver/g) ?? []).length, 0);
  assert.match(wrapper, /ASTOCK_FINAL_DELIVERY:-disabled/);
  assert.match(wrapper, /final_delivery" != "disabled/);
  assert.match(wrapper, /legacy_wrapper_delivery_disabled/);
  // The failure alert is additive: it must not touch the success chain, and the
  // alert unit may call the fixed seal-alert script but gains no delivery power.
  assert.match(sealDropIn, /^\[Unit\]$/m);
  assert.match(sealDropIn, /^OnFailure=research-chain-alert-post\.service$/m);
  assert.doesNotMatch(sealDropIn, /OnSuccess=/);
  assert.match(sealAlert, /morning_seal_alert\.sh draft_failure/);
  assert.doesNotMatch(sealAlert, /hermes-notify\.mjs deliver|reports\/daily|--manual-same-day/);
  // The source triggers sealing through its drop-in. The draft remains a
  // manual DRAFT_ONLY entry and is not part of the scheduled publish branch.
  const [sourceSealDropIn, researchSeal] = await Promise.all([
    readFile(path.join(repositoryRoot, "systemd", "astock-xhs-source.service.d", "research-seal.conf"), "utf8"),
    readFile(path.join(repositoryRoot, "systemd", "astock-research-seal.service"), "utf8"),
    readFile(path.join(repositoryRoot, "systemd", "astock-research-seal.service.d", "notify.conf"), "utf8")
  ]);
  assert.match(sourceSealDropIn, /^\[Unit\]$/m);
  assert.match(sourceSealDropIn, /^OnSuccess=astock-research-seal\.service$/m);
  assert.match(researchSeal, /scripts\/research-seal\.mjs --require-current-date --max-age-minutes 120/);
  assert.match(researchSeal, /^OnSuccess=nng8-research-digest\.service$/m);
  assert.match(researchSeal, /^OnFailure=research-chain-alert-post\.service$/m);
  assert.match(researchSeal, /^RestrictAddressFamilies=AF_UNIX$/m);
  assert.doesNotMatch(researchSeal, /hermes|telegram|publish|nng8-research-digest\.timer/i);
  // This repository's draft base unit names no relay. The signal-bus owned
  // draft drop-in is removed in the coordinated on-demand switch; the seal
  // remains the sole success-chain relay entry (apart from bounded timers).
  assert.doesNotMatch(draftService, /nng8/);
  // The pre-collector runs before the sealed source and stays out of delivery.
  const [precollectService, precollectTimer] = await Promise.all([
    readFile(path.join(repositoryRoot, "systemd", "astock-cninfo-precollect.service"), "utf8"),
    readFile(path.join(repositoryRoot, "systemd", "astock-cninfo-precollect.timer"), "utf8")
  ]);
  assert.match(precollectTimer, /^OnCalendar=Mon\.\.Fri \*-\*-\* 07:50:00 Asia\/Shanghai$/m);
  assert.match(precollectService, /mcp\/precollect_cninfo\.py/);
  assert.doesNotMatch(precollectService, /hermes-notify|deliver-morning-report|xhs-draft|ASTOCK_FINAL_DELIVERY=telegram/);
});
