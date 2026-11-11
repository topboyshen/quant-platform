import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { buildV3Brief, V3_VERSION } from "../src/result-view-v3.mjs";
import { buildResultView } from "../src/result-view.mjs";
import { buildVerifiedBaselineReport } from "../src/baseline-report.mjs";
import { assessPublicationGate, normalizeReportOutput } from "../src/report.mjs";
import {
  contextFixture, buildOptions, normalizationOptions, TARGET_DATE,
} from "./helpers/multi-candidate-context.mjs";

// Public, historical input: no network, production stores, notification or seal APIs.
const NORMAL_PATH = new URL("./fixtures/research-brief-normal-2026-09-18.md", import.meta.url);
const NORMAL = readFileSync(NORMAL_PATH, "utf8");
const NORMAL_DATE = "2026-09-18";
const NORMAL_SHA = "58e5658cb528a97c24f295c8136e05f2bdc8efe1ed0f68cdda80c9f0c617d1b2";
const V1_SOURCE_SHA = "dddfba6154c8dccdd123d6d645ea8343d52103b2e62174c004974a8fc4028ae8";
const V1_NORMAL_SHA = "e0baef8da4907c1c77ddeabf87d61cbe3c080c87029fcf286477003b7f7e1227";
const QUESTIONS = ["发生了什么", "为什么重要", "今天观察什么", "哪些还不能下结论"];
const sha = (value) => createHash("sha256").update(value).digest("hex");
const brief = (report = NORMAL, targetDate = NORMAL_DATE) => buildV3Brief(report, { targetDate });

function splitView(output) {
  const marker = "\n## 详情\n";
  const at = output.indexOf(marker);
  assert.ok(at > 0, "v3 must have an explicit body/details boundary");
  assert.equal(output.indexOf(marker, at + marker.length), -1);
  return { body: output.slice(0, at), details: output.slice(at + marker.length) };
}

function section(body, name) {
  const marker = `## ${name}\n`;
  const start = body.indexOf(marker);
  assert.ok(start >= 0, `missing question: ${name}`);
  const rest = body.slice(start + marker.length);
  const next = rest.indexOf("\n## ");
  return (next < 0 ? rest : rest.slice(0, next)).trim();
}

function syntheticReport(context = contextFixture()) {
  const audit = assessPublicationGate(buildOptions(context));
  const raw = buildVerifiedBaselineReport(buildOptions(context, audit));
  return normalizeReportOutput(raw, normalizationOptions(context, audit));
}

function changed(report, from, to) {
  const result = report.replace(from, to);
  assert.notEqual(result, report, "negative test must actually change its input");
  return result;
}

test("v3 has exactly four body questions, a details boundary and deterministic output", () => {
  const output = brief();
  const { body } = splitView(output);
  assert.equal(V3_VERSION, "research_brief_v3");
  assert.equal(brief(), output);
  assert.deepEqual([...body.matchAll(/^## (.+)$/gm)].map((m) => m[1]), QUESTIONS);
  assert.match(body, /2026-09-18/);
  assert.doesNotMatch(body, /读完自检|语义未审|未经人工审校|发布级别|模块状态分数|门禁必要项/);
});

test("real normal day keeps at most three fact priorities and separates market sessions", () => {
  const { body } = splitView(brief());
  const what = section(body, "发生了什么");
  const priorities = what.split("\n").filter((line) => /^- /.test(line));
  assert.equal(priorities.length, 3, "market, overnight and explicit revenue mismatch are three priorities");
  for (const fact of ["上证指数", "深证成指", "创业板指", "18249.71", "2503", "2558", "东方电气"])
    assert.ok(what.includes(fact), `normal day fact omitted: ${fact}`);
  assert.match(what, /2026-09-17/);
  assert.match(what, /北京|Asia\/Shanghai/);
  assert.match(what, /美东|America\/New_York/);
  assert.match(what, /纳斯达克|纳指/);
  assert.match(what, /1\.69%/);
});

test("why and watch do not invent cross-market, breadth-threshold or cross-period interpretations", () => {
  const { body } = splitView(brief());
  assert.match(section(body, "为什么重要"), /口径|归并|核对/);
  assert.match(section(body, "今天观察什么"), /口径|核验|公告查询/);
  assert.doesNotMatch(body, /\[推断\]|方向相反|同步分歧|指数与个股方向一致性有限|涨跌家数接近平衡/);
  assert.doesNotMatch(body, /利润同比与行情方向可能背离|价格与利润背离|是否继续扩大|半年报.*股价|单日.*半年/);
  assert.doesNotMatch(body, /89\.76%|42\.07%|23\.76%|-1,722,168,649\.61/);
});

test("decisive announcement cutoff restrictions remain in the body, longstanding gaps stay in details", () => {
  const { body, details } = splitView(brief());
  assert.match(body, /公告查询未完成|官方公告查询未完成|公告查询.*不适用于截止/);
  assert.match(body, /截止后取得/);
  assert.match(body, /不判断窗口内有无新增披露|不据此确认截止前有无披露/);
  assert.doesNotMatch(body, /北向资金实时净流入不可用|三大指数各自成交额字段缺失|行情 96%|资金 50%/);
  assert.match(details, /北向资金实时净流入不可用/);
  assert.match(details, /三大指数各自成交额字段缺失/);
});

test("details preserve the whole authoritative report including all 28 source lines", () => {
  const { details } = splitView(brief());
  assert.ok(details.includes(NORMAL.trimEnd()), "details must retain the complete original, not selected excerpts");
  const sources = NORMAL.split("\n").filter((line) => /^[1-9]\d*\. /.test(line));
  assert.equal(sources.length, 28);
  for (const line of sources) assert.ok(details.includes(line), `source was changed or dropped: ${line}`);
});

test("rendering never alters the historical input bytes or hash", () => {
  const bytes = readFileSync(NORMAL_PATH);
  assert.equal(sha(bytes), NORMAL_SHA);
  const before = sha(NORMAL);
  brief();
  assert.equal(sha(NORMAL), before);
  assert.deepEqual(readFileSync(NORMAL_PATH), bytes);
});

test("v1 source and real normal-day output remain byte-identical to the offline baseline", () => {
  const source = new URL("../src/result-view.mjs", import.meta.url);
  assert.equal(sha(readFileSync(source)), V1_SOURCE_SHA);
  const before = buildResultView(NORMAL, { targetDate: NORMAL_DATE });
  assert.equal(sha(before), V1_NORMAL_SHA);
  brief();
  const after = buildResultView(NORMAL, { targetDate: NORMAL_DATE });
  assert.equal(after, before);
  assert.equal(sha(readFileSync(source)), V1_SOURCE_SHA);
});

test("unverified publication, forged data state and posthoc reconstructions are rejected", () => {
  for (const bad of [
    changed(NORMAL, "发布级别：可发布", "发布级别：待核验"),
    changed(NORMAL, "数据状态：部分可用", "数据状态：示例数据"),
    changed(NORMAL, "生成方式：程序核验基础版 + 结构化分析覆盖层", "生成方式：人工改写"),
    changed(NORMAL, "> 数据截止：", "> 验证用途：事后资料重建\n> 数据截止："),
  ]) assert.throws(() => brief(bad), /unsupported_result_view/);
});

test("bad or mismatched target dates and malformed report inputs fail closed", () => {
  for (const targetDate of ["2026-09-19", "2026-02-30", "bad", "", null])
    assert.throws(() => brief(NORMAL, targetDate), /unsupported_result_view/);
  for (const bad of [null, 7, {}, NORMAL.replaceAll("\n", "\r\n"), `${NORMAL}\u0000`])
    assert.throws(() => brief(bad), /unsupported_result_view/);
  assert.throws(() => brief(changed(NORMAL, "数据截止：2026-09-18", "数据截止：2026-09-17")),
    /unsupported_result_view/);
});

test("every original reference is checked, including calendar references omitted by v1", () => {
  for (const bad of [
    changed(NORMAL, /^1\. .+\n/m, ""),
    changed(NORMAL, "交易日 [来源 1]", "交易日 [来源 999]"),
    changed(NORMAL, "[来源 18]", "[来源 999]"),
    changed(NORMAL, /^(2\. .+)$/m, "$1\n$1"),
    changed(NORMAL, /^(28\. .+https?:\/\/)\S+$/m, "$1"),
  ]) assert.throws(() => brief(bad), /unsupported_result_view/);
});

test("unknown, missing or repeated report sections and recursive v3 input are rejected", () => {
  for (const bad of [
    changed(NORMAL, "## 6. 结构化分析覆盖层", "## 6. 新格式"),
    changed(NORMAL, "## 来源", "## 未知段落\n\n## 来源"),
    changed(NORMAL, "## 3. 隔夜数据与事件", "## 3. 隔夜数据与事件\n## 3. 隔夜数据与事件"),
    changed(NORMAL, "## 7. 风险提示与数据缺口", "风险提示与数据缺口"),
    brief(),
  ]) assert.throws(() => brief(bad), /unsupported_result_view/);
});

test("missing support, missing counterevidence, unknown roles and dangling analysis sources are rejected", () => {
  for (const bad of [
    changed(NORMAL, /^  - 支持证据：.+\n/gm, ""),
    changed(NORMAL, /^  - 反证：.+\n/gm, ""),
    changed(NORMAL, "  - 支持证据：", "  - 未知角色："),
    changed(NORMAL, "  - 反证：[来源 16]", "  - 反证：[来源 999]"),
  ]) assert.throws(() => brief(bad), /unsupported_result_view/);
});

test("original adverse figures, inference roles, limitations and source timestamps survive in details", () => {
  const { body, details } = splitView(brief());
  const protectedLines = NORMAL.split("\n").filter((line) =>
    /支持证据：|反证：|\[推断\]|对应列：|原文单位：|口径待核：|官方查询限制：|实际采集时间：/.test(line));
  assert.ok(protectedLines.length > 20, "fixture must exercise material evidence boundaries");
  for (const line of protectedLines) assert.ok(details.includes(line), line);
  assert.match(details, /-1,722,168,649\.61 -556,466,428\.94/);
  assert.match(details, /9,839,766\.20 139,424,796\.07/);
  assert.match(details, /研究语义未审校/);
  assert.doesNotMatch(body, /支持证据：|反证：|利润同比与行情方向可能背离/);
});

test("synthetic flat indices alone never authorize a whole-market quiet-day claim", () => {
  const context = contextFixture();
  for (const item of context.data.previousAShareSession.indices) {
    item.close = item.previousClose;
    item.change = 0;
    item.changePercent = 0;
  }
  // Construction-only example: no official announcement check or historical quiet-day proof.
  const report = syntheticReport(context);
  const { body, details } = splitView(brief(report, TARGET_DATE));
  assert.doesNotMatch(body, /(?:今日|全市场)(?:无|没有)(?:重要|显著|新增)变化|今天没有新增披露/);
  assert.match(body, /公告|披露/);
  assert.ok(details.includes(report.trimEnd()));
  assert.doesNotMatch(section(body, "为什么重要"), /方向一致性|家数接近平衡|方向相反/);
});

test("synthetic critical-data gaps remain in the body without recycling unavailable figures", () => {
  let report = changed(NORMAL, "> 门禁必要项：5/5；缺口：无",
    "> 门禁必要项：4/5；缺口：成交额或市场宽度缺失/异常");
  report = changed(report, /^- \[事实\] 沪深两市成交额：.+$/m,
    "- 沪深两市成交额：不可核验，不采用该成交额。");
  report = changed(report, /^- \[事实\] 市场宽度：.+$/m,
    "- 市场宽度：家数或来源不可核验，不做量能与广度比较。");
  const { body, details } = splitView(brief(report));
  assert.match(body, /成交额或市场宽度缺失\/异常|不可核验/);
  assert.doesNotMatch(body, /18249\.71|上涨 2503|下跌 2558/);
  assert.doesNotMatch(body, /量能充足|上涨有支撑|家数接近平衡/);
  assert.ok(details.includes(report.trimEnd()));
});

test("rendered cutoff follows the source rather than hard-coding 08:00", () => {
  const report = changed(NORMAL, "> 数据截止：2026-09-18 08:00（Asia/Shanghai）",
    "> 数据截止：2026-09-18 09:17（Asia/Shanghai）");
  const { body, details } = splitView(brief(report));
  assert.match(body, /数据截止：2026-09-18 09:17/);
  assert.doesNotMatch(body, /数据截止：2026-09-18 08:00/);
  assert.ok(details.includes(report.trimEnd()));
});

test("overnight market dates bind to source time and cannot pass invalid dates or post-cutoff evidence", () => {
  for (const date of ["2026-09-19", "2026-09-31", "2026-09-16"]) {
    const report = changed(NORMAL, /指数（2026-09-17）/g, `指数（${date}）`);
    assert.throws(() => brief(report), /unsupported_result_view/,
      `US session ${date} must not be paired with source time 2026-09-18 04:00 Shanghai`);
  }
  const sourceClock = /^((?:18|19|20)\. .*数据时间：2026-09-18 )04:00/gm;
  const late = changed(NORMAL, sourceClock, (_line, prefix) => `${prefix}09:00`);
  assert.throws(() => brief(late), /unsupported_result_view/,
    "09:00 observations are later than the report's 08:00 cutoff");
  const earlier = changed(NORMAL, sourceClock, (_line, prefix) => `${prefix}03:00`);
  assert.doesNotThrow(() => brief(earlier),
    "03:00 Shanghai still maps to the stated New York date; do not hard-code 04:00");
});

test("material northbound date limitations are not hidden merely because their prefix is longstanding", () => {
  const limitation = "北向资金数据源日期与交易日不符；当日跨市场判断全部不可用。";
  const report = changed(NORMAL,
    "- 北向资金实时净流入不可用；本报告未填入估算值。", `- ${limitation}`);
  const { body, details } = splitView(brief(report));
  assert.ok(body.includes(limitation), "material source-date restrictions must remain visible in the brief");
  assert.ok(details.includes(report.trimEnd()), "source restrictions also remain unchanged in the original");
});

test("coverage gaps override stale numeric facts even when the original market lines remain present", () => {
  const examples = [
    { gap: "沪深两市成交额不可核验", sourceFact: "18249.71", forbidden: /18249\.71/ },
    { gap: "市场宽度不可用", sourceFact: "上涨 2503 家，下跌 2558 家，平盘 148 家",
      forbidden: /\b2503\b|\b2558\b|\b148\b/ },
  ];
  for (const { gap, sourceFact, forbidden } of examples) {
    const report = changed(NORMAL, "> 门禁必要项：5/5；缺口：无", `> 门禁必要项：4/5；缺口：${gap}`);
    assert.ok(report.includes(sourceFact), "this regression must retain contradictory raw numbers");
    const { body, details } = splitView(brief(report));
    assert.doesNotMatch(section(body, "发生了什么"), forbidden, gap);
    assert.ok(section(body, "哪些还不能下结论").includes(gap), "body must explain why those numbers are withheld");
    assert.ok(details.includes(report.trimEnd()), "do not repair or rewrite the archived original");
  }
});
