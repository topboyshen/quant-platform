/**
 * Synthetic project fixture for research-seal tests.
 *
 * The report body shape is the same synthetic contract the XHS draft tests use;
 * only the source block is parameterised so the 2026-09-17 regression (a
 * numbered source whose time label the parser did not recognise) can be
 * reproduced without touching any production file.
 */
import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { compileFrozenReport, freezeRunInput } from "../../src/run-archive.mjs";
import { jsonHash } from "../../src/snapshot-integrity.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

export const DISCLAIMER =
  "本晨报用于信息整理与研究支持，不构成个性化投资建议；市场有风险，决策需独立核验。";

export function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

export function reportText({
  targetDate,
  dataStatus = "partial",
  marketDate,
  sourceTwo
}) {
  const dataLabel = dataStatus === "live" ? "实时数据包" : "部分可用";
  return [
    `# AStock 数据快报（语义未审）｜${targetDate}`,
    "",
    `> 数据截止：${targetDate} 08:00（Asia/Shanghai）`,
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
    `## 2. 上一交易日 A 股行情（${marketDate}）`,
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
    "### 600519 贵州茅台",
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
    "",
    "## 来源",
    "",
    "> 以下仅列出本报告实际引用的固定数据包来源。",
    "",
    `1. 上海证券交易所日行情 — market_data（上海证券交易所）；数据时间：${marketDate} 15:00（Asia/Shanghai）；https://example.com/sse-daily`,
    sourceTwo,
    "",
    "---",
    "",
    DISCLAIMER,
    ""
  ].join("\n");
}

/** The CNINFO shape that failed on 2026-09-17, fixed by 2cc9cec. */
export const CNINFO_SOURCE_FIXED =
  "2. [陈旧] 2026年半年度报告摘要 — official_announcement（巨潮资讯网）；披露日期：2026-08-26（仅日期，发布时间未核实）；实际采集时间：2026-09-08 08:00（Asia/Shanghai）；https://static.cninfo.com.cn/finalpage/2026-08-26/1225501876.PDF";

/** A numbered source with a time label the parser does not recognise. */
export const CNINFO_SOURCE_UNPARSABLE =
  "2. [陈旧] 2026年半年度报告摘要 — official_announcement（巨潮资讯网）；核验时间：2026-09-08 08:00（Asia/Shanghai）；https://static.cninfo.com.cn/finalpage/2026-08-26/1225501876.PDF";


/**
 * Write the frozen compilation archive the sealing step binds its digest to.
 * The archive's own compiled report is not used here: the seal validates the
 * synthetic report above, and the digest binds to this report's hash plus the
 * frozen input, exactly like production binds to the compiled report.
 */
export async function writeFrozenArchive(root, { targetDate, marketDate, report = null, dataStatus = "partial" }) {
  const time = `${marketDate}T15:00:00+08:00`;
  const stock = { code: "000001", name: "测试银行", symbol: "000001.SZ", exchange: "SZ" };
  const index = (code, name) => ({ code, name, close: 101, previousClose: 100, change: 1, changePercent: 1,
    units: { close: "index_point", previousClose: "index_point", change: "index_point", changePercent: "percentage_point" },
    dataAsOf: time, sourceIds: ["market"] });
  const rawContext = {
    schemaVersion: "1.0",
    meta: { targetDate, timezone: "Asia/Shanghai", status: dataStatus, provider: "test-fixture", sourceType: "fixture", isLive: false },
    data: {
      tradingCalendar: { status: "trading_day", targetDate, previousTradingDate: marketDate,
        dataAsOf: `${targetDate}T08:00:00+08:00`, sourceIds: ["calendar"] },
      previousAShareSession: { sessionDate: marketDate, dataAsOf: time, sourceIds: ["market"],
        indices: [index("sh000001", "上证指数"), index("sz399001", "深证成指"), index("sz399006", "创业板指")] },
      watchlist: [{ ...stock, status: "available",
        marketData: { code: stock.code, name: stock.name, last: 12.3, previousClose: 12, change: 0.3, changePercent: 2.5,
          dataAsOf: time, sourceIds: ["market"],
          units: { last: "CNY_per_share", previousClose: "CNY_per_share", change: "CNY_per_share", changePercent: "percentage_point" } },
        financials: { reportDate: "2026-06-30", parentNetProfit: 100, units: { parentNetProfit: "CNY" },
          dataAsOf: "2026-07-30T18:00:00+08:00", sourceIds: ["financial"] } }],
      capitalFlows: [], sectors: [], announcementsAndNews: []
    },
    sources: [
      { id: "calendar", title: "交易日历", url: "https://example.com/calendar", sourceType: "trading_calendar_data", dataAsOf: `${targetDate}T08:00:00+08:00` },
      { id: "market", title: "行情", url: "https://example.com/market", sourceType: "market_data", dataAsOf: time },
      { id: "financial", title: "财务指标", url: "https://example.com/financial", sourceType: "financial_data_aggregation", dataAsOf: "2026-07-30T18:00:00+08:00" }
    ]
  };
  const frozen = await freezeRunInput({
    projectRoot: REPO_ROOT,
    archiveRoot: path.join(root, "work", "report-archives"),
    outputDirectory: path.join(root, "reports"),
    rawContext,
    targetDate,
    cutoffInstant: `${targetDate}T00:00:00Z`,
    cutoffLocal: `${targetDate} 08:00`,
    configuration: {
      stocks: { corePool: [stock] },
      risk: { disclaimer: DISCLAIMER, marketDataMaxAgeHours: 18, newsMaxAgeHours: 72 },
      workflow: { reportMode: "structured_compiler" }
    }
  });
  // R05: the digest binds the compilation's own body. Fixtures therefore seal the
  // compiled report by default; a synthetic body is only used by report-contract
  // tests, which are expected to stop before (or at) the digest step.
  const compiled = compileFrozenReport(frozen.input, null);
  const reportBody = report ?? compiled;
  const reportSha = sha256(reportBody);
  const result = {
    schemaVersion: "1.0", status: "validated", inputSha256: frozen.inputSha256,
    reportSha256: reportSha, selection: null, selectionSha256: jsonHash(null),
    compilationInputSha256: jsonHash({ inputSha256: frozen.inputSha256, selection: null }),
    analysisStatus: "disabled", analysisFailures: [], mode: "verified_baseline",
    analysisAttempts: 0, semanticCandidateCount: frozen.input.candidateIds.length,
    financialSnapshots: {}
  };
  const resultDirectory = path.join(root, "work", "report-archives", targetDate, "result");
  await mkdir(resultDirectory, { recursive: true });
  await writeFile(path.join(resultDirectory, "report.md"), reportBody);
  await writeFile(path.join(resultDirectory, "result.json"), `${JSON.stringify(result, null, 2)}\n`);
  // R05: the archive publication identity the seal step now reuses.
  await writeFile(path.join(root, "work", "report-archives", targetDate, "published.json"),
    `${JSON.stringify({ status: "sealed", inputSha256: frozen.inputSha256, reportSha256: reportSha,
                        resultSha256: jsonHash(result) }, null, 2)}\n`);
  return { frozen, compiled, report: reportBody };
}

export async function writeFixture(root, {
  targetDate,
  marketDate,
  sourceTwo = CNINFO_SOURCE_FIXED,
  dataStatus = "partial",
  lastRunPatch = {},
  reportMode = "compiled"
}) {
  await mkdir(path.join(root, "config"), { recursive: true });
  await mkdir(path.join(root, "work"), { recursive: true });
  await mkdir(path.join(root, "reports", "daily"), { recursive: true });
  await writeFile(path.join(root, "config", "risk.json"),
    `${JSON.stringify({ schemaVersion: "1.0", disclaimer: DISCLAIMER }, null, 2)}\n`);
  const synthetic = reportText({ targetDate, dataStatus, marketDate, sourceTwo });
  const { report } = await writeFrozenArchive(root, {
    targetDate, marketDate, dataStatus, report: reportMode === "synthetic" ? synthetic : null
  });
  await writeFile(path.join(root, "reports", "daily", `${targetDate}.md`), report);
  const lastRun = {
    schemaVersion: "1.0",
    targetDate,
    startedAt: `${targetDate}T00:00:04.910Z`,
    frozenCutoffInstant: `${targetDate}T00:00:04.910Z`,
    cutoffLocal: `${targetDate} 08:00`,
    status: "success",
    phase: "publish",
    publicationGate: "passed",
    evidenceReview: "passed",
    publicationBlockers: [],
    dataStatus,
    researchSemanticReview: "not_human_reviewed",
    contextSha256: sha256(`context:${targetDate}`),
    reportFile: `reports/daily/${targetDate}.md`,
    completedAt: `${targetDate}T00:01:15.883Z`,
    ...lastRunPatch
  };
  await writeFile(path.join(root, "work", "last-run.json"), `${JSON.stringify(lastRun, null, 2)}\n`);
  return { report, lastRun };
}
