import test from "node:test";
import assert from "node:assert/strict";
import { buildResultView, RESULT_VIEW_VERSION } from "../src/result-view.mjs";
import { buildVerifiedBaselineReport } from "../src/baseline-report.mjs";
import { assessPublicationGate, normalizeReportOutput } from "../src/report.mjs";
import { buildStructuredAnalysisCandidates, parseStructuredAnalysisOutput } from "../src/structured-analysis.mjs";
import { contextFixture, semanticCandidateContext, buildOptions, normalizationOptions, TARGET_DATE } from "./helpers/multi-candidate-context.mjs";

function fixture(context = contextFixture(), enhanced = false) {
  const audit = assessPublicationGate(buildOptions(context));
  const candidates = buildStructuredAnalysisCandidates(context);
  const overlay = enhanced ? parseStructuredAnalysisOutput(JSON.stringify({ schemaVersion: "2.0",
    candidateIds: candidates.map((item) => item.id) }), { context }) : null;
  const raw = buildVerifiedBaselineReport(buildOptions(context, audit, { analysisOverlay: overlay }));
  const report = normalizeReportOutput(raw, normalizationOptions(context, audit));
  return { report, context, audit, overlay };
}
const view = (report) => buildResultView(report, { targetDate: TARGET_DATE });

test("results-only view is deterministic, shorter and leaves the approved report unchanged", () => {
  const { report } = fixture();
  const before = report;
  const result = view(report);
  assert.equal(RESULT_VIEW_VERSION, "results_only_v1");
  assert.equal(view(report), result);
  assert.equal(report, before);
  assert.ok(Buffer.byteLength(result) < Buffer.byteLength(report));
  assert.doesNotMatch(result, /生成方式|门禁必要项|模块状态分数|数据桥模块评分|结构化分析覆盖层|支持证据：|反证：/);
  assert.match(result, /语义未审/);
  assert.match(result, /未经人工审校/);
  assert.match(result, /数据状态：部分可用/);
  assert.match(result, /数据截止：2026-08-10 08:00/);
  assert.match(result, /市场（2026-08-07）/);
  assert.match(result, /上证指数：收盘 3940\.037 点\[陈旧\]（\+1\.02%\[陈旧\]）/);
});

test("enhanced conclusions retain uncertainty and every supporting/opposing source without the process", () => {
  const { report } = fixture(semanticCandidateContext(), true);
  const result = view(report);
  const block = report.split("## 6. 结构化分析覆盖层\n")[1].split("## 7.")[0];
  assert.ok(block.includes("- [推断]"));
  const expectedRefs = [...block.matchAll(/\[来源 \d+\](?:\[陈旧\])?/g)].map((item) => item[0]);
  for (const ref of expectedRefs) assert.ok(result.includes(ref), ref);
  const expected = block.split("\n").filter((line) => line.startsWith("- [推断] "));
  assert.equal(result.split("\n").filter((line) => line.startsWith("- [推断] ")).length, expected.length);
  assert.match(result, /待确认/);
  assert.doesNotMatch(result, /支持证据：|反证：/);
});

test("all material missing-data and source-scope caveats remain visible", () => {
  const { report } = fixture(contextFixture({ missingWatchlistMarket: true }));
  const result = view(report);
  for (const text of ["上一交易日行情无法核验", "研究线索，待验证", "现金流字段，不作判断",
    "未提供巨潮公告查询结果", "不判断窗口内有无新增披露", "公开披露口径不再提供实时净流入",
    "三大指数各自成交额字段缺失", "不等于机构账户流水", "隔夜数据不可用", "不构成任何投资建议"]) {
    assert.ok(result.includes(text), text);
  }
  assert.doesNotMatch(result, /下一次数据采集后重新核验/);
});

test("negative financial figures, unverified revenue basis and comparison caveats are not hidden", () => {
  const context = contextFixture();
  const fin = context.data.watchlist[0].financials;
  Object.assign(fin, { revenue: 500000000, revenueYoY: -12, parentNetProfit: -20000000, parentNetProfitYoY: -40,
    revenueBasis: { provider: "eastmoney", amountField: "TOTALOPERATEREVE", yoyField: "TOTALOPERATEREVETZ", verification: "unverified_against_filing" },
    units: { revenue: "CNY", revenueYoY: "percentage_point", parentNetProfit: "CNY", parentNetProfitYoY: "percentage_point" } });
  const { report } = fixture(context);
  const result = view(report);
  assert.match(result, /供应商同比 -12%/);
  assert.match(result, /归母净利润 -2000 万元/);
  assert.match(result, /同比 -40%/);
  assert.match(result, /尚未与公告核对，不等同于已核验的营业收入/);
  assert.match(result, /不据此推断经营杠杆/);
  assert.doesNotMatch(result, /TOTALOPERATEREVE|下一核验点/);
});

test("only fixed monitoring status boilerplate is removed, not arbitrary indented content", () => {
  const { report } = fixture();
  const monitored = report.replace("## 5. 今日政策", "- 研究监控：\n  - 毛利率：数据包已提供\n  - 量价：上一交易日行情已提供\n\n## 5. 今日政策");
  assert.doesNotMatch(view(monitored), /研究监控|毛利率：数据包已提供/);
  assert.throws(() => view(monitored.replace("毛利率：数据包已提供", "现金流出现重大异常")), /unsupported_result_view/);
});

test("remaining core coverage gaps are carried over without module scoring", () => {
  const { report } = fixture();
  const changed = report.replace(/(> 门禁必要项：\d+\/\d+；缺口：)[^\n]+/, "$1合成测试：逐股资金缺失");
  const result = view(changed);
  assert.match(result, /数据缺口：合成测试：逐股资金缺失/);
  assert.doesNotMatch(result, /门禁必要项/);
});

test("official cash-flow quote column definitions, revision warnings and adverse values survive unchanged", () => {
  const { report } = fixture();
  const important = [
    '- [事实] 官方现金流原文（PDF 第 2 页，合并排版空白）：“经营活动现金流 -1,000.00 900.00”。[来源 7]',
    '- 对应列：本报告期 2026-03-31 累计 / 上年同期 2025-03-31 累计；原文单位：元；首次取数不等于首次披露。',
    '- 前档比较：同一文档链接内容不同，需核验修订。',
    '- 口径待核：聚合营收金额或供应商同比与官方摘要不同，不自动归并。'
  ];
  const result = view(report.replace("## 5. 今日政策", `${important.join("\n")}\n\n## 5. 今日政策`));
  for (const line of important) assert.ok(result.includes(line.replace("[事实] ", "")), line);
});

test("only unreferenced sources are omitted; referenced source identity, dates and stale labels remain exact", () => {
  const { report } = fixture();
  const result = view(report);
  const sources = new Map(report.split("\n").flatMap((line) => {
    const match = /^(\d+)\. /.exec(line); return match ? [[match[1], line]] : [];
  }));
  const used = new Set([...result.split("## 来源")[0].matchAll(/\[来源 (\d+(?:、\d+)*)\]/g)].flatMap((m) => m[1].split("、")));
  for (const [id, line] of sources) assert.equal(result.includes(line), used.has(id));
  assert.throws(() => view(report.replace(/^7\. .+\n/m, "")), /unsupported_result_view/);
});

test("unknown layouts, forged publication states, posthoc evidence, bad dates and extra sections fail closed", () => {
  const { report } = fixture();
  for (const invalid of [report.replace("## 6. 结构化分析覆盖层", "## 6. 新格式"),
    report.replace("## 来源", "## 未知段落\n\n## 来源"),
    report.replace("发布级别：可发布", "发布级别：待核验"),
    report.replace("数据状态：部分可用", "数据状态：示例数据"),
    report.replace("> 数据截止：", "> 验证用途：事后资料重建\n> 数据截止："),
    report.replace("## 3. 隔夜数据与事件", "## 3. 隔夜数据与事件\n## 3. 隔夜数据与事件")]) {
    assert.throws(() => view(invalid), /unsupported_result_view/);
  }
  for (const targetDate of ["2026-08-11", "2026-02-30", "bad", null]) {
    assert.throws(() => buildResultView(report, { targetDate }), /unsupported_result_view/);
  }
});

test("result view cannot pass as the original verified full report or recursively shorten itself", () => {
  const { report, context, audit } = fixture(semanticCandidateContext(), true);
  const result = view(report);
  assert.throws(() => normalizeReportOutput(result, normalizationOptions(context, audit)));
  assert.throws(() => view(result), /unsupported_result_view/);
});

test("结论区正文事实排在推断之前，不被模板推断盖过", () => {
  const { report } = fixture(semanticCandidateContext(), true);
  const result = view(report);
  const conclusionSection = result.split("## 结论\n")[1].split("\n## ")[0];
  const firstFact = conclusionSection.split("\n").find((line) => line.startsWith("- ") && !line.startsWith("- [推断]"));
  const firstInference = conclusionSection.split("\n").find((line) => line.startsWith("- [推断]"));
  assert.ok(firstFact, "结论区应有正文事实");
  assert.ok(firstInference, "结论区应有推断");
  const factIndex = conclusionSection.indexOf(firstFact);
  const inferenceIndex = conclusionSection.indexOf(firstInference);
  assert.ok(factIndex < inferenceIndex, `正文事实应排在推断之前（fact@${factIndex} >= inference@${inferenceIndex}）`);
});

test("malformed analysis evidence cannot disappear into a result view", () => {
  const { report } = fixture(semanticCandidateContext(), true);
  for (const bad of [report.replace(/  - 支持证据：[^\n]+/g, ""),
    report.replace(/  - 反证：[^\n]+/g, ""),
    report.replace(/  - 支持证据：/, "  - 未知角色："),
    report.replace(/  - 支持证据：\[来源 \d+\]/, "  - 支持证据：[来源 999]")]) {
    assert.throws(() => view(bad), /unsupported_result_view/);
  }
});
