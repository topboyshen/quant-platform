import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { buildV3Brief } from "../src/result-view-v3.mjs";
import { buildResultView } from "../src/result-view.mjs";
import { buildQuietCandidateReport } from "./helpers/quiet-candidate-context.mjs";

const MISSING_PATH = new URL("./fixtures/research-brief-missing-2026-08-21.md", import.meta.url);
const MISSING = readFileSync(MISSING_PATH, "utf8");
const DATE = "2026-08-21";
const MISSING_SHA = "5d5571e6bd80924d9d6155e3d98160b50acb797faa707a09eeac68ecf951a6c7";
const V1_SHA = "dddfba6154c8dccdd123d6d645ea8343d52103b2e62174c004974a8fc4028ae8";
const QUESTIONS = ["发生了什么", "为什么重要", "今天观察什么", "哪些还不能下结论"];
const QUIET_LABEL = "构造静日候选，非真实报告；缺少跨日比较证据，未判定静日";
const sha = (value) => createHash("sha256").update(value).digest("hex");
const brief = (report = MISSING, targetDate = DATE) => buildV3Brief(report, { targetDate });

function parts(output) {
  const marker = "\n## 详情\n";
  const at = output.indexOf(marker);
  assert.ok(at > 0, "a brief must preserve its explicit details boundary");
  assert.equal(output.indexOf(marker, at + marker.length), -1);
  return { body: output.slice(0, at), details: output.slice(at + marker.length) };
}

function section(body, name) {
  const marker = `## ${name}\n`;
  const at = body.indexOf(marker);
  assert.ok(at >= 0, `missing body section ${name}`);
  const remaining = body.slice(at + marker.length);
  const next = remaining.indexOf("\n## ");
  return (next < 0 ? remaining : remaining.slice(0, next)).trim();
}

function changed(from, to) {
  const report = MISSING.replace(from, to);
  assert.notEqual(report, MISSING, "negative test must change the real legacy fixture");
  return report;
}

test("real 8/21 legacy report renders deterministically without rewriting its archived bytes", () => {
  const before = readFileSync(MISSING_PATH);
  assert.equal(sha(before), MISSING_SHA);
  const output = brief();
  assert.equal(brief(), output);
  const { body, details } = parts(output);
  assert.deepEqual([...body.matchAll(/^## (.+)$/gm)].map((match) => match[1]), QUESTIONS);
  assert.ok(details.includes(MISSING.trimEnd()), "complete original report must remain in details");
  assert.deepEqual(readFileSync(MISSING_PATH), before);
  assert.equal(sha(MISSING), MISSING_SHA);
});

test("legacy valid index returns remain visible with their actual market date and report cutoff", () => {
  const { body } = parts(brief());
  const what = section(body, "发生了什么");
  for (const value of ["上证指数", "深证成指", "创业板指", "+0.24%", "+0.59%", "+0.64%", "2026-08-20"])
    assert.ok(what.includes(value), `verified market value lost: ${value}`);
  assert.match(body, /数据截止：2026-08-21 08:00/);
});

test("legacy combined turnover-breadth coverage failure suppresses 20815.93 in the brief only", () => {
  assert.ok(MISSING.includes("20815.93"), "fixture retains the contradictory raw turnover");
  const { body, details } = parts(brief());
  assert.doesNotMatch(body, /20815\.93/);
  assert.match(details, /沪深两市成交额：20815\.93 亿元/);
  const unknown = section(body, "哪些还不能下结论");
  assert.match(unknown, /成交额|量能/);
  assert.match(unknown, /市场宽度|广度/);
  assert.match(unknown, /不可核验|缺失|异常|无法/);
});

test("legacy lack of official announcements remains a decisive body limitation", () => {
  const { body } = parts(brief());
  assert.match(body, /公司官方公告源未提供/);
  assert.match(body, /无法核验是否有公告|未确认公司事项/);
  assert.doesNotMatch(body, /(?:今天|窗口内)(?:没有|无)新增披露|已确认无公告/);
});

test("historical conflicting module scores remain verbatim in details, not body priorities", () => {
  const { body, details } = parts(brief());
  assert.doesNotMatch(body, /65%|86%|数据桥模块评分|数据桥模块缺口|模块评分/);
  assert.ok(details.includes("| 行情 | 86% |"));
  assert.ok(details.includes("上一交易日行情评分为 65%，不代表字段齐全。"));
  assert.ok(details.includes(MISSING.trimEnd()), "do not reconcile the original scores by rewriting history");
});

test("legacy partial data is not promoted to recovery or inferred market strength", () => {
  const { body } = parts(brief());
  assert.doesNotMatch(body, /数据已恢复|已恢复正常|缺口已修复|全部数据可用|上涨有量能支撑|量能充足/);
  assert.doesNotMatch(body, /\[推断\]|方向相反|涨跌家数接近平衡/);
});

test("v3 legacy support does not weaken or edit the immutable v1 reader", () => {
  const path = new URL("../src/result-view.mjs", import.meta.url);
  assert.equal(sha(readFileSync(path)), V1_SHA);
  assert.throws(() => buildResultView(MISSING, { targetDate: DATE }), /unsupported_result_view/);
  brief();
  assert.throws(() => buildResultView(MISSING, { targetDate: DATE }), /unsupported_result_view/);
  assert.equal(sha(readFileSync(path)), V1_SHA);
});

test("legacy publication and provenance gates cannot be forged", () => {
  for (const report of [
    changed("发布级别：可发布", "发布级别：待核验"),
    changed("数据状态：部分可用", "数据状态：示例数据"),
    changed("生成方式：程序核验基础版", "生成方式：人工改写"),
    changed("> 数据截止：", "> 验证用途：事后资料重建\n> 数据截止："),
  ]) assert.throws(() => brief(report), /unsupported_result_view/);
});

test("legacy target dates, cutoff dates and market-session dates remain strict", () => {
  for (const targetDate of ["2026-08-22", "2026-02-30", "bad", null])
    assert.throws(() => brief(MISSING, targetDate), /unsupported_result_view/);
  for (const report of [
    changed("数据截止：2026-08-21", "数据截止：2026-08-20"),
    changed("A 股行情（2026-08-20）", "A 股行情（2026-08-21）"),
    changed("A 股行情（2026-08-20）", "A 股行情（2026-02-30）"),
  ]) assert.throws(() => brief(report), /unsupported_result_view/);
});

test("legacy source validation covers calendar, displayed facts and detail-only sector references", () => {
  for (const report of [
    changed(/^1\. .+\n/m, ""),
    changed(/^2\. .+\n/m, ""),
    changed(/^7\. .+\n/m, ""),
    changed("交易日 [来源 1]", "交易日 [来源 999]"),
    changed(/^(13\. .+)$/m, "$1\n$1"),
  ]) assert.throws(() => brief(report), /unsupported_result_view/);
});

test("legacy compatibility accepts only the known coverage table and report section layout", () => {
  for (const report of [
    changed("| 类别 | 数据桥模块评分（非准确性） |", "| 类别 | 未知模型保证 |"),
    changed("| 行情 | 86% |", "| 未知指标 | 86% |"),
    changed("| 财务 | 100% |", "| 财务 | 100% |\n| 新增神秘指标 | 100% |"),
    changed("## 来源", "## 附录\n\n未知附录内容\n\n## 来源"),
    changed("## 3. 隔夜数据与事件", "## 3. 隔夜数据与事件\n## 3. 隔夜数据与事件"),
  ]) assert.throws(() => brief(report), /unsupported_result_view/);
});

test("legacy malformed or undecodable text is rejected rather than silently repaired", () => {
  for (const report of [
    changed("上证指数", "上证\ufffd指数"),
    `${MISSING}\u0000`,
    MISSING.replaceAll("\n", "\r\n"),
    null,
  ]) assert.throws(() => brief(report), /unsupported_result_view/);
});

test("quiet candidate is explicitly synthetic and deterministically compiled, not a quiet-day verdict", () => {
  const candidate = buildQuietCandidateReport();
  assert.equal(candidate.synthetic, true);
  assert.equal(candidate.label, QUIET_LABEL);
  assert.equal(candidate.targetDate, "2026-08-10");
  assert.deepEqual(buildQuietCandidateReport(), candidate);
  assert.match(candidate.report, /生成方式：程序核验基础版/);
  assert.match(candidate.report, /https:\/\/example\.com\//);
  assert.doesNotThrow(() => buildResultView(candidate.report, { targetDate: candidate.targetDate }));
});

test("quiet candidate indices and watchlist are flat while balanced breadth retains source references", () => {
  const { report } = buildQuietCandidateReport();
  for (const name of ["上证指数", "深证成指", "创业板指"]) {
    const line = report.split("\n").find((value) => value.startsWith(`- [事实] ${name}：`));
    assert.ok(line, name);
    assert.match(line, /（\+?0%(?:\[陈旧\])?）/);
    assert.match(line, /\[来源 [1-9]\d*\]/);
  }
  const stockLine = report.split("\n").find((value) => value.startsWith("- [事实] 行情变化："));
  assert.ok(stockLine);
  assert.match(stockLine, /涨跌幅 \+?0%/);
  assert.match(report, /上涨 2500 家(?:\[陈旧\])?，下跌 2500 家(?:\[陈旧\])?，平盘 0 家(?:\[陈旧\])?。\[来源 [1-9]\d*\]/);
  assert.match(report, /沪深两市成交额：[^\n]+\[来源 [1-9]\d*(?:、[1-9]\d*)*\]/);
});

test("flat synthetic prices never erase announcement uncertainty or imply proven day-over-day quietness", () => {
  const { report, targetDate } = buildQuietCandidateReport();
  const { body, details } = parts(brief(report, targetDate));
  assert.doesNotMatch(body, /今日无重要变化|全市场无变化|已判定静日|静日已确认|今天没有新增披露/);
  assert.match(body, /公告/);
  assert.match(body, /未提供|未完成|无法核验|不判断/);
  assert.ok(details.includes(report.trimEnd()));
});

test("flat indices with 1805 advancers and 3275 decliners remain visible without an invented quiet threshold", () => {
  const { report, targetDate, label } = buildQuietCandidateReport({
    breadth: { advancers: 1805, decliners: 3275, unchanged: 0 },
  });
  assert.equal(label, QUIET_LABEL);
  const { body, details } = parts(brief(report, targetDate));
  const what = section(body, "发生了什么");
  assert.match(what, /1805/);
  assert.match(what, /3275/);
  assert.doesNotMatch(body, /今日无重要变化|已判定静日|涨跌家数接近平衡|触发静日阈值/);
  assert.ok(details.includes(report.trimEnd()));
});

test("synthetic breadth cannot introduce invalid counts and creating counterexamples leaves the default unchanged", () => {
  const baseline = buildQuietCandidateReport();
  for (const breadth of [{ advancers: -1 }, { decliners: 1.5 }, { unchanged: NaN }])
    assert.throws(() => buildQuietCandidateReport({ breadth }), /invalid_synthetic_breadth/);
  buildQuietCandidateReport({ breadth: { advancers: 1805, decliners: 3275 } });
  assert.deepEqual(buildQuietCandidateReport(), baseline);
});

test("legacy numeric index facts cannot borrow source references from neighboring market rows", () => {
  const line = MISSING.split("\n").find((value) => value.startsWith("- [事实] 上证指数："));
  assert.ok(line);
  assert.ok(line.includes("[来源 2]"));
  const unreferenced = line.replace("[来源 2]", "");
  const report = changed(line, unreferenced);
  assert.ok(report.includes("[来源 3]"), "neighboring index sources intentionally remain present");
  assert.throws(() => brief(report), /unsupported_result_view/);
});

test("synthetic breadth and turnover numeric rows each require their own complete source reference", () => {
  const { report, targetDate } = buildQuietCandidateReport();
  for (const label of ["市场宽度", "沪深两市成交额"]) {
    const line = report.split("\n").find((value) => value.startsWith(`- [事实] ${label}：`));
    assert.ok(line, `fixture must have ${label}`);
    const unreferenced = line.replace(/\[来源 [1-9]\d*(?:、[1-9]\d*)*\]/g, "");
    assert.notEqual(unreferenced, line, `must remove the whole ${label} reference`);
    assert.doesNotMatch(unreferenced, /\[来源/);
    assert.throws(() => brief(report.replace(line, unreferenced), targetDate), /unsupported_result_view/,
      `${label} must not inherit another row's provenance`);
  }
});

test("rendered synthetic breadth retains its own stale-data label rather than relying on index labels", () => {
  const { report, targetDate } = buildQuietCandidateReport();
  assert.match(report, /上涨 2500 家\[陈旧\]/, "fixture must actually contain stale breadth");
  const { body } = parts(brief(report, targetDate));
  const what = section(body, "发生了什么");
  const breadth = /(?:上涨|涨)\s*2500[^；。\n]*/.exec(what)?.[0];
  assert.ok(breadth, "breadth figures must remain visible");
  assert.match(breadth, /\[陈旧\]/, "an earlier index's stale marker does not label the breadth data");
});
