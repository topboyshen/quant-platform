// Deterministic presentation only. The sealed full report remains authoritative;
// callers must retain its approval and hash checks before creating this view.
export const RESULT_VIEW_VERSION = "results_only_v1";
const MAX_BYTES = 1024 * 1024;
const H2 = [
  "## 一句话结论", "## 1. 数据覆盖与可用性", null,
  "## 3. 隔夜数据与事件", "## 4. 板块与股票池",
  "## 5. 今日政策、公告与事件", "## 6. 结构化分析覆盖层",
  "## 7. 风险提示与数据缺口", "## 附录", "## 来源"
];
const NO_ANALYSIS = new Set([
  "程序核验基础版不生成因果推断、价格目标、买卖或仓位建议。",
  "结构化分析覆盖层为空；本次仅发布程序核验基础版。"
]);
const MONITOR = new Set([
  "量价：上一交易日行情已提供", "毛利率：数据包已提供",
  "归母净利润：数据包已提供", "现金流：数据包已提供",
  "巨潮查询：完成，窗口内有披露条目", "巨潮查询：完成，仅代表有限查询结果"
]);
const REVENUE_BASIS = "- 财务口径：营收金额取自供应商 TOTALOPERATEREVE，同比取自 TOTALOPERATEREVETZ；二者均未与公告口径核验，不等同于已核验的营业收入。";
const NEXT_CHECK = "- 下一核验点：先对照同报告期公告核验营收金额、同比定义和扣非利润，再补现金流证据；当前不据此推断经营杠杆。";

function fail() { throw new Error("unsupported_result_view_report"); }
function nonempty(lines) { return lines.filter((line) => line.trim() !== ""); }
function fact(line) { return line.replace(/^(- (?:\[陈旧\] )?)\[事实\] /, "$1"); }
function compactMarket(line) {
  return fact(line.replace(/^(.*：收盘 [^，]+)，前收 [^，]+，涨跌 [^（]+（([^）]+)）。(\[来源 .+)$/, "$1（$2）。$3"));
}
function compactStock(line) {
  return fact(line.replace(/^(- \[事实\] 行情变化：收盘 [^，]+)，前收 [^，]+，涨跌额 [^，]+，涨跌幅 ([^。]+)。(\[来源 .+)$/, "$1（$2）。$3"));
}

function analysisLines(lines) {
  const content = nonempty(lines);
  if (content.length === 1 && NO_ANALYSIS.has(content[0])) return [];
  const result = [];
  let block = null;
  const flush = () => {
    if (!block) return;
    if (!block.support || !block.counter) fail();
    result.push(`${block.text}${block.period ? `（${block.period}）` : ""}${block.uncertain ? "（信息不充分，不作事实结论）" : ""} ${[...block.refs].join("")}`);
  };
  for (const line of content) {
    if (line.startsWith("- [推断] ")) {
      flush();
      block = { text: line.replace(/^- \[推断\] 主题 (?:观察主题：)?/, "- [推断] ")
          .replace(/，仍需(?:观察反证|继续核验)$/, "（待确认）"),
        refs: new Set(), support: false, counter: false, uncertain: false };
      continue;
    }
    if (line === "  - 推断限制：未提供结构化反证来源；此限制只约束本条推断，不否定已核验事实。") {
      block.counter = true;
      continue;
    }
    const period = /^  - 期间：(.+)。$/.exec(line);
    if (period) {
      block.period = period[1];
      continue;
    }
    const match = /^  - (支持证据|反证)(?:（[^）]+）)?：(\[来源 [1-9]\d*\](?:\[陈旧\])?|未提供结构化反证来源，不升级为事实结论。)$/.exec(line);
    if (!block || !match) fail();
    if (match[1] === "支持证据") {
      if (!match[2].startsWith("[来源 ")) fail();
      block.support = true;
    } else block.counter = true;
    if (match[2].startsWith("[来源 ")) block.refs.add(match[2]);
    else block.uncertain = true;
  }
  flush();
  if (!result.length) fail();
  return result;
}

function stockLines(lines) {
  const result = [];
  let monitor = false;
  for (const line of lines) {
    if (line === "### 股票池") continue;
    if (line === "- 研究监控：") { monitor = true; continue; }
    if (monitor && line.startsWith("  - ")) {
      if (!MONITOR.has(line.slice(4))) fail();
      continue;
    }
    monitor = false;
    if (line.startsWith("  - ")) fail();
    if (line === "- 观察条件：下一次数据采集后重新核验行情与证据字段。") continue;
    if (line === NEXT_CHECK) {
      result.push("- 不据未核对的财务字段判断经营杠杆。");
      continue;
    }
    if (line === REVENUE_BASIS) {
      result.push("- 营收金额及同比为供应商口径，尚未与公告核对，不等同于已核验的营业收入。");
      continue;
    }
    // All financial comparisons, quotations, column definitions and adverse
    // data/availability caveats pass through. Never keyword-filter limitations.
    result.push(compactStock(line));
  }
  return result;
}

export function buildResultView(verifiedReport, { targetDate } = {}) {
  if (typeof verifiedReport !== "string" || Buffer.byteLength(verifiedReport) > MAX_BYTES ||
      typeof targetDate !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(targetDate) ||
      !Number.isFinite(Date.parse(`${targetDate}T00:00:00Z`)) ||
      new Date(`${targetDate}T00:00:00Z`).toISOString().slice(0, 10) !== targetDate ||
      /[\u0000\r]/.test(verifiedReport)) fail();
  const lines = verifiedReport.split("\n");
  if (lines[0] !== `# AStock 数据快报（语义未审）｜${targetDate}`) fail();
  const starts = lines.flatMap((line, i) => line.startsWith("## ") ? [i] : []);
  if (starts.length !== H2.length) fail();
  for (let i = 0; i < H2.length; i++) {
    if (H2[i] ? lines[starts[i]] !== H2[i]
      : !/^## 2\. 上一交易日 A 股行情（\d{4}-\d{2}-\d{2}）$/.test(lines[starts[i]])) fail();
  }
  const sections = starts.map((at, i) => lines.slice(at + 1, starts[i + 1] ?? lines.length));
  const header = nonempty(lines.slice(1, starts[0]));
  const singleton = (prefix) => {
    const values = header.filter((line) => line.startsWith(prefix));
    if (values.length !== 1) fail();
    return values[0];
  };
  const cutoff = singleton("> 数据截止：");
  if (!cutoff.startsWith(`> 数据截止：${targetDate} `)) fail();
  const dataStatus = singleton("> 数据状态：");
  if (!new Set(["> 数据状态：实时数据包", "> 数据状态：部分可用"]).has(dataStatus)) fail();
  if (singleton("> 发布级别：") !== "> 发布级别：可发布（结构、来源与时效门禁通过；研究语义未审校）" ||
      !/^> 生成方式：程序核验基础版(?: \+ 结构化分析覆盖层)?$/.test(singleton("> 生成方式："))) fail();
  const headerPrefixes = ["> 数据截止：", "> 数据状态：", "> 交易日状态：", "> 发布级别：", "> 生成方式：",
    "> 行情口径：", "> 陈旧数据：", "> 陈旧证据："];
  if (header.some((line) => !headerPrefixes.some((prefix) => line.startsWith(prefix)))) fail();
  const retainedHeader = header.filter((line) => /^(?:> 行情口径：|> 陈旧数据：|> 陈旧证据：)/.test(line));
  const coverage = nonempty(sections[1]);
  const coverageExtra = coverage.slice(4);
  if (coverage.length < 4 || coverage.length > 5 || !coverage[0].startsWith("> 数据桥模块评分") ||
      !/^> 门禁必要项：\d+\/\d+；缺口：.+$/.test(coverage[1]) ||
      !coverage[2].startsWith("> 模块状态分数（非字段完整度）：") ||
      !coverage[3].startsWith("> 研究可用项：") ||
      coverageExtra.some((line) => !line.startsWith("> 北向资金："))) fail();
  const coverageGap = coverage[1].split("；缺口：")[1];
  const risks = nonempty(sections[7]).filter((line) => line !== "- 其余缺口已在第 1 节覆盖率三行中列示。");
  if (coverageGap !== "无") risks.unshift(`- 数据缺口：${coverageGap}`);
  // 数据说明里的北向口径限制属于“材料性限制”，必须随结果版一起保留。
  for (const line of coverageExtra) risks.unshift(`- ${line.slice(2)}`);
  if (!risks.length) risks.push("- 数据限制见各项结果；未经人工审校，不作为交易指令。");
  const overnight = nonempty(sections[3]).filter((line) => line !==
    "程序核验基础版未生成跨市场因果解释；以上仅列示可核验收盘事实。");
  if (overnight.length === 1 && overnight[0] === "程序核验基础版未生成跨市场因果解释；相关数据留待分析层处理。") {
    overnight[0] = "- 隔夜数据不可用。";
  }
  const events = sections[5].filter((line) => line !==
    "程序核验基础版未把媒体聚合内容写成公司或交易所官方事实。");
  const footerAt = sections[9].indexOf("---");
  if (footerAt < 0 || sections[9].lastIndexOf("---") !== footerAt) fail();
  const disclaimer = nonempty(sections[9].slice(footerAt + 1));
  if (disclaimer.length !== 1 || !/不构成.*(?:建议|交易)/.test(disclaimer[0])) fail();
  const sources = new Map();
  for (const line of nonempty(sections[9].slice(0, footerAt))) {
    if (line === "> 以下仅列出本报告实际引用的固定数据包来源。") continue;
    const match = /^([1-9]\d*)\. .+https?:\/\/\S+$/.exec(line);
    if (!match || sources.has(match[1])) fail();
    sources.set(match[1], line);
  }
  const conclusions = analysisLines(sections[6]);
  const body = [
    `# AStock 早报（语义未审）｜${targetDate}`, "", cutoff, dataStatus,
    "> 研究判断未经人工审校，仅供研究参考。", ...retainedHeader, "",
    "## 结论", "", ...nonempty(sections[0]).map(fact), ...conclusions, "",
    lines[starts[2]].replace("## 2. 上一交易日 A 股行情", "## 市场"), "", ...nonempty(sections[2]).map(compactMarket), "",
    "## 隔夜市场", "", ...overnight.map(compactMarket), "",
    "## 股票池", "", ...stockLines(sections[4]), "",
    "## 公告与事件", "", ...events.map(fact), "",
    "## 风险与限制", "", ...risks, "",
    "## 板块表现", "", ...sections[8].filter((line) => line !== "### 板块排行事实").map(fact)
  ].join("\n").replace(/\n{3,}/g, "\n\n").trimEnd();
  const referenced = new Set();
  for (const match of body.matchAll(/\[来源 ([1-9]\d*(?:、[1-9]\d*)*)\]/g)) {
    for (const id of match[1].split("、")) {
      if (!sources.has(id)) fail();
      referenced.add(id);
    }
  }
  if (!referenced.size) fail();
  const output = `${body}\n\n## 来源\n\n${[...sources].filter(([id]) => referenced.has(id)).map(([, line]) => line).join("\n")}\n\n---\n\n${disclaimer[0]}\n`;
  if (Buffer.byteLength(output) > MAX_BYTES) fail();
  return output;
}
