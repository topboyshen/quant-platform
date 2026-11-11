// Deterministic presentation. This function neither verifies a remote seal
// nor sends/persists anything: callers must supply the already verified report.
// Keep results_only_v1 immutable: historical receipts recheck its exact bytes.
import { buildResultView } from "./result-view.mjs";

export const V3_VERSION = "research_brief_v3";
const MAX_BYTES = 1024 * 1024;
const REF = /\[来源 ([1-9]\d*(?:、[1-9]\d*)*)\]/g;
const unique = (items) => [...new Set(items)];
const nonempty = (lines) => lines.filter((line) => line.trim());
const plain = (line) => line.replace(/^- (?:\[事实\] )?/, "");
const ids = (text) => unique([...text.matchAll(REF)].flatMap((match) => match[1].split("、")));
const references = (items) => items.length ? `[来源 ${unique(items).join("、")}]` : "";
const fail = (reason) => { throw new Error(`unsupported_result_view_report:${reason}`); };
const LEGACY_TITLE = "# A 股基础数据快报（含待核验线索）｜";
const LONGSTANDING = new Set([
  "- 三大指数各自成交额字段缺失；本报告未据此作指数成交额比较。",
  "- 北向资金实时净流入不可用；本报告未填入估算值。",
  "- 财务口径：营收金额及同比为供应商口径，尚未与公告核对，不等同于已核验的营业收入；不据此推断经营杠杆。",
]);

function validDate(value) {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    Number.isFinite(Date.parse(`${value}T00:00:00Z`)) &&
    new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value;
}

function validateLegacyLayout(lines, starts, sections, targetDate) {
  // Explicit, bounded historical format support, never a fallback for a broken
  // modern report. No title/date rewrite, fresh seal, or delivery is performed.
  const headings = ["## 一句话结论", "## 1. 数据覆盖与可用性", null,
    "## 3. 隔夜数据与事件", "## 4. 板块与股票池", "## 5. 今日政策、公告与事件",
    "## 6. 结构化分析覆盖层", "## 7. 风险提示与数据缺口", "## 来源"];
  if (starts.length !== headings.length) fail("legacy_layout");
  for (let index = 0; index < headings.length; index++) {
    if (headings[index] ? lines[starts[index]] !== headings[index]
      : !/^## 2\. 上一交易日 A 股行情（\d{4}-\d{2}-\d{2}）$/.test(lines[starts[index]])) fail("legacy_layout");
  }
  const header = nonempty(lines.slice(1, starts[0]));
  if (lines[0] !== `${LEGACY_TITLE}${targetDate}` || header.length !== 5 ||
      !header[0].startsWith(`> 数据截止：${targetDate} `) ||
      !["> 数据状态：部分可用", "> 数据状态：实时数据包"].includes(header[1]) ||
      !/^> 交易日状态：交易日 \[来源 [1-9]\d*\]$/.test(header[2]) ||
      header[3] !== "> 发布级别：可发布（结构、来源与时效门禁通过；研究语义未审校）" ||
      header[4] !== "> 生成方式：程序核验基础版") fail("legacy_header");
  const coverage = nonempty(sections[1]);
  const count = /^> 正式发布必要项覆盖：(\d+)\/(\d+)；计算定义：三大指数组 1 项、沪深成交额与宽度 1 项、每只股票行情各 1 项；X\/X 不代表全部行情字段齐全；必要项缺口：(.+)$/.exec(coverage[1]);
  if (coverage.length !== 8 ||
      coverage[0] !== "> 数据桥模块评分只衡量模块新鲜度与可用性，不代表字段齐全或数值准确。" ||
      !count || Number(count[2]) <= 0 || Number(count[1]) > Number(count[2]) ||
      (Number(count[1]) === Number(count[2])) !== (count[3] === "无") ||
      coverage[2] !== "| 类别 | 数据桥模块评分（非准确性） |" || coverage[3] !== "|---|---:|") fail("legacy_coverage");
  for (const [index, name] of ["行情", "资金", "新闻", "财务"].entries()) {
    const value = new RegExp(`^\\| ${name} \\| (\\d+(?:\\.\\d+)?)% \\|$`).exec(coverage[index + 4]);
    if (!value || Number(value[1]) > 100) fail("legacy_coverage");
  }
  const analysis = nonempty(sections[6]);
  if (analysis.length !== 1 || analysis[0] !== "程序核验基础版不生成因果推断、价格目标、买卖或仓位建议。") {
    fail("legacy_analysis");
  }
}

function parseReport(report, targetDate) {
  // Share the existing bounded input/layout/publication checks, not its lossy
  // output. The original sections below retain evidence roles and caveats.
  if (typeof report !== "string" || Buffer.byteLength(report) > MAX_BYTES ||
      !validDate(targetDate) || /[\u0000\r\ufffd]/.test(report)) fail("input_contract");
  const lines = report.split("\n");
  const starts = lines.flatMap((line, index) => line.startsWith("## ") ? [index] : []);
  const rawSections = starts.map((at, index) => lines.slice(at + 1, starts[index + 1] ?? lines.length));
  const legacy = lines[0] === `${LEGACY_TITLE}${targetDate}`;
  if (legacy) validateLegacyLayout(lines, starts, rawSections, targetDate);
  else {
    try { buildResultView(report, { targetDate }); }
    catch { fail("input_contract"); }
  }
  // The old layout has no appendix. Normalize the in-memory section index,
  // never the original text or its bytes, timestamps, title, or evidence.
  const sections = legacy ? [...rawSections.slice(0, 8), [], rawSections[8]] : rawSections;
  const header = lines.slice(1, starts[0]);
  const cutoff = header.find((line) => line.startsWith("> 数据截止："));
  const clock = /^> 数据截止：(\d{4}-\d{2}-\d{2}) ([0-2]\d):([0-5]\d)（Asia\/Shanghai）$/.exec(cutoff);
  if (!clock || Number(clock[2]) > 23) fail("cutoff");
  const marketDate = /^## 2\. 上一交易日 A 股行情（(.+)）$/.exec(lines[starts[2]])?.[1];
  if (!marketDate || !Number.isFinite(Date.parse(`${marketDate}T00:00:00Z`)) ||
      new Date(`${marketDate}T00:00:00Z`).toISOString().slice(0, 10) !== marketDate || marketDate >= targetDate) {
    fail("market_date");
  }
  const footerAt = sections[9].indexOf("---");
  const disclaimer = nonempty(sections[9].slice(footerAt + 1));
  if (footerAt < 0 || sections[9].lastIndexOf("---") !== footerAt || disclaimer.length !== 1 ||
      !/不构成.*(?:建议|交易)/.test(disclaimer[0])) fail("source_footer");
  const sourceLines = nonempty(sections[9].slice(0, footerAt));
  const sources = new Map();
  for (const line of sourceLines) {
    if (line === "> 以下仅列出本报告实际引用的固定数据包来源。") continue;
    const match = /^([1-9]\d*)\. \S.*https?:\/\/\S+$/.exec(line);
    if (!match || sources.has(match[1])) fail("source_definition");
    sources.set(match[1], line);
  }
  // v1 checks references in its own projection. Also check every original
  // reference, including header/details that v1 may omit. Never accept orphans.
  const text = lines.slice(0, starts.at(-1)).join("\n");
  if (text.replace(REF, "").includes("[来源") || !ids(text).length ||
      ids(text).some((id) => !sources.has(id))) fail("source_reference");
  validateAnalysis(sections[6]);
  const coverage = sections[1].find((line) => line.startsWith(legacy ? "> 正式发布必要项覆盖：" : "> 门禁必要项："));
  return { sections, header, cutoff, marketDate, sources, legacy,
    gap: coverage.split(legacy ? "；必要项缺口：" : "；缺口：")[1] };
}

function validateAnalysis(lines) {
  let block = null;
  const finish = () => {
    if (block && (!block.support || !block.counter)) fail("analysis_roles");
  };
  for (const line of nonempty(lines)) {
    if (line.startsWith("- [推断] ")) {
      finish(); block = { support: false, counter: false }; continue;
    }
    if (line.startsWith("  - ")) {
      if (!block) fail("orphan_analysis_evidence");
      if (/^  - 支持证据(?:（[^）]+）)?：/.test(line)) {
        if (!ids(line).length) fail("analysis_support");
        block.support = true;
      } else if (/^  - 反证(?:（[^）]+）)?：/.test(line) ||
          line === "  - 推断限制：未提供结构化反证来源；此限制只约束本条推断，不否定已核验事实。") {
        block.counter = true;
      }
    }
  }
  finish();
}

function marketPoint(parsed) {
  const facts = [];
  const used = [];
  for (const line of nonempty(parsed.sections[2])) {
    const index = /^- \[事实\] (.+?)：收盘 .+（([^（）]+%)((?:\[陈旧\])?)）\。/.exec(line);
    if (index) {
      if (!ids(line).length) fail("market_fact_source");
      facts.push(`${index[1]} ${index[2]}${index[3]}`); used.push(...ids(line)); continue;
    }
    const turnover = /^- \[事实\] 沪深两市成交额：([^。]+)。/.exec(line);
    if (turnover && !/成交额|量能/.test(parsed.gap)) {
      if (!ids(line).length) fail("market_fact_source");
      facts.push(`成交 ${turnover[1]}`); used.push(...ids(line)); continue;
    }
    const breadth = /^- \[事实\] 市场宽度：上涨 (\d+) 家(\[陈旧\])?，下跌 (\d+) 家(\[陈旧\])?，平盘 (\d+) 家(\[陈旧\])?。/.exec(line);
    if (breadth && !/宽度|家数|广度/.test(parsed.gap)) {
      if (!ids(line).length) fail("market_fact_source");
      facts.push(`涨 ${breadth[1]}${breadth[2] ?? ""} / 跌 ${breadth[3]}${breadth[4] ?? ""} / 平 ${breadth[5]}${breadth[6] ?? ""}`);
      used.push(...ids(line));
    }
  }
  return facts.length ? `A 股 ${parsed.marketDate} 收盘（北京时间）：${facts.join("；")}。${references(used)}` : null;
}

function overnightPoint(parsed) {
  // This narrow identity map only labels the session of known US indices. No
  // direction comparison, threshold, cross-market causality or new calculation.
  const names = new Map([["道琼斯工业指数", "道指"], ["标普500指数", "标普500"], ["纳斯达克综合指数", "纳指"]]);
  const rows = [];
  for (const line of nonempty(parsed.sections[3])) {
    const match = /^- \[事实\] (.+?)（(\d{4}-\d{2}-\d{2})）：收盘 .+（([+-]?[\d.]+%)）\。/.exec(line);
    if (match && names.has(match[1]) && ids(line).length) {
      rows.push({ name: names.get(match[1]), date: match[2], percent: match[3], refs: ids(line) });
    }
  }
  if (!rows.length) return null;
  const cutoff = Date.parse(parsed.cutoff.slice("> 数据截止：".length).replace("（Asia/Shanghai）", ":00+08:00").replace(" ", "T"));
  const nyDate = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
  });
  for (const row of rows) {
    const session = Date.parse(`${row.date}T00:00:00Z`);
    if (!Number.isFinite(session) || new Date(session).toISOString().slice(0, 10) !== row.date) fail("overnight_date");
    for (const id of row.refs) {
      const stamp = /数据时间：(\d{4}-\d{2}-\d{2} \d{2}:\d{2})（Asia\/Shanghai）/.exec(parsed.sources.get(id))?.[1];
      if (!stamp) return null;
      const observed = Date.parse(stamp.replace(" ", "T") + ":00+08:00");
      const dateParts = nyDate.formatToParts(new Date(Number.isFinite(observed) ? observed : 0));
      const part = (type) => dateParts.find((entry) => entry.type === type)?.value;
      const sourceSession = `${part("year")}-${part("month")}-${part("day")}`;
      const sourceLocal = Number.isFinite(observed)
        ? new Date(observed + 8 * 3600_000).toISOString().slice(0, 16).replace("T", " ") : null;
      if (sourceLocal !== stamp || observed > cutoff || sourceSession !== row.date) fail("overnight_time");
    }
  }
  if (new Set(rows.map((row) => row.date)).size !== 1) return null;
  const times = unique(rows.flatMap((row) => row.refs.map((id) => {
    return /数据时间：(\d{4}-\d{2}-\d{2} \d{2}:\d{2})（Asia\/Shanghai）/.exec(parsed.sources.get(id))?.[1];
  })));
  // Unknown source clock is not silently filled with a customary closing time.
  if (times.length !== 1 || !times[0]) return null;
  return `美股 ${rows[0].date} 收盘（美东交易日；来源时间 ${times[0]} 北京时间）：` +
    `${rows.map((row) => `${row.name} ${row.percent}`).join("；")}。${references(rows.flatMap((row) => row.refs))}`;
}

function stocks(lines) {
  const result = [];
  let current = null;
  for (const line of lines) {
    const heading = /^### (\d{6}) (.+)$/.exec(line);
    if (heading) { current = { code: heading[1], name: heading[2], lines: [] }; result.push(current); }
    else if (current) current.lines.push(line);
  }
  return result;
}

function financialFocus(parsed) {
  // A narrowly defined source-backed mismatch, NOT daily-price/half-year-profit
  // divergence. Keep the financial period and explicitly mark it as background.
  for (const stock of stocks(parsed.sections[4])) {
    if (!stock.lines.some((line) => line.startsWith("- 口径待核："))) continue;
    const financial = stock.lines.find((line) => /^- \[事实\] 财务（/.test(line));
    const official = stock.lines.find((line) => /^- \[事实\] 官方营业收入原文/.test(line));
    const period = /财务（(\d{4}-\d{2}-\d{2})/.exec(financial ?? "")?.[1];
    if (!period || !official || !ids(financial).length || !ids(official).length) continue;
    const refs = references([...ids(financial), ...ids(official)]);
    return {
      what: `${stock.name}：聚合营收与官方摘要口径待核（财报期 ${period}，背景资料，非当日变化）。${refs}`,
      watch: `${stock.name}：核对聚合营收金额、同比与官方原列口径；未对齐前不自动归并。${refs}`,
      why: parsed.sections[7].find((line) => line.startsWith("- 财务口径：") && line.includes("不据此推断"))
        ?.replace(/^- 财务口径：/, ""),
    };
  }
  return null;
}

function unknowns(parsed) {
  const result = [];
  if (parsed.gap !== "无") result.push(`当日数据缺口：${parsed.gap}。`);
  for (const line of parsed.header) {
    if (/^> (?:行情口径|陈旧数据|陈旧证据)：/.test(line)) result.push(line.slice(2));
  }
  for (const line of parsed.sections[2]) {
    if (/缺失|不可用|不可核验|无法核验|不能|不作/.test(line)) result.push(plain(line));
  }
  for (const line of [...parsed.sections[5], ...parsed.sections[4]]) {
    if (/^(?:官方公告查询|未提供.*公告)/.test(line) && /未完成|不适用|未提供|不判断/.test(line)) result.push(line);
    if (line.startsWith("- 官方查询限制：")) result.push(plain(line));
  }
  for (const line of parsed.sections[7]) {
    if (!line.trim() || line === "- 其余缺口已在第 1 节覆盖率三行中列示。") continue;
    // These explicit standing limitations remain verbatim in the full details.
    // All unrecognised risk lines are conservatively kept in the brief as well.
    if (LONGSTANDING.has(line)) continue;
    // Only the exact legacy scoring boilerplate goes to details. A new warning
    // that happens to mention a score must not disappear with it.
    if (/^- 数据桥模块缺口：(上一交易日行情|资金|公告与新闻)评分为 \d+(?:\.\d+)?%，不代表字段齐全。$/.test(line)) continue;
    if (line === `- 正式发布必要项缺口：${parsed.gap}。`) continue;
    result.push(plain(line));
  }
  return unique(result);
}

export function buildV3Brief(verifiedReport, { targetDate } = {}) {
  const parsed = parseReport(verifiedReport, targetDate);
  const focus = financialFocus(parsed);
  const what = [marketPoint(parsed), overnightPoint(parsed), focus?.what].filter(Boolean);
  const why = focus?.why ? [focus.why] : [];
  const watch = focus ? [focus.watch] : [];
  const unknown = unknowns(parsed);
  const missing = [];
  if (/成交额|量能/.test(parsed.gap)) missing.push("成交额");
  if (/宽度|家数|广度/.test(parsed.gap)) missing.push("市场宽度（涨跌家数）");
  if (missing.length) {
    why.push(`${missing.join("、")}尚未通过核验；指数涨跌不能替代量能或广度证据，本版不据此判断上涨是否有支撑。`);
    watch.push(`先核对${missing.join("与")}的缺口；恢复前不做相应的量能或广度比较，不用估算补齐。`);
  }
  // Do not generate observations from arbitrary inferences or risk paragraphs.
  // All original analysis blocks (with support/counter labels) stay in details.
  const lines = [`# AStock 研究简报｜${targetDate}`, "", parsed.cutoff, ""];
  if (parsed.legacy) lines.push("> 历史格式只读展示：保留原报告日期与缺口，不代表重新生成、重新密封或补发。", "");
  const question = (name, values, fallback) => {
    lines.push(`## ${name}`, "", ...(values.length ? values : [fallback]).map((value) => `- ${value}`), "");
  };
  question("发生了什么", what, "当前格式未提取出可核验的正文要点；不能据此判断没有变化，完整材料见详情。");
  question("为什么重要", why, "原稿未提供适合直接摘录的重要性解释；本版不额外推断。");
  question("今天观察什么", watch, "原稿未提供本版可直接提取的明确核验事项；不自动把推断改写为建议。");
  question("哪些还不能下结论", unknown, "本版未提取出当日判断阻断项，不等于数据完整或研究结论成立；限制见详情。");
  lines.push("## 详情", "",
    "以下保留原始研究全文与来源；半年度财务及历史材料属于背景，不是当日变化。原稿推断不自动成为本版结论或观察建议。",
    "原始研究语义未经人工审核。此展示不改变原始报告、密封证明或投递记录。", "");
  // Exact source suffix is intentional for this offline candidate: no roles,
  // warnings, stale markers or source definitions can vanish during shortening.
  const output = `${lines.join("\n")}\n${verifiedReport}`;
  if (Buffer.byteLength(output) > MAX_BYTES) fail("output_size");
  return output;
}
