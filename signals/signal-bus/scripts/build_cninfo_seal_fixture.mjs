#!/usr/bin/env node
/**
 * Offline fixture builder for CNINFO seal+relay chain tests.
 * Writes last-run + report (with 披露日期/实际采集时间 sources) under --root,
 * then builds an xhs-draft seal. Prints JSON result only. No network/notify.
 */
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
const projectRoot = process.env.ASTOCK_TEST_ROOT || "/home/ubuntu/AStock_AI_System";
const { buildXhsDraft } = await import(pathToFileURL(path.join(projectRoot, "src/xhs-draft.mjs")));

function argValue(flag) {
  const i = process.argv.indexOf(flag);
  if (i < 0 || !process.argv[i + 1]) throw new Error(`missing ${flag}`);
  return process.argv[i + 1];
}

const root = path.resolve(argValue("--root"));
const day = argValue("--day");
const nowIso = argValue("--now");
const disclaimer =
  "本晨报用于信息整理与研究支持，不构成个性化投资建议；市场有风险，决策需独立核验。";

const cninfo = [
  `2. [陈旧] 2026年半年度报告摘要 — official_announcement（巨潮资讯网）；披露日期：2026-08-26（仅日期，发布时间未核实）；实际采集时间：2026-09-08 08:00（Asia/Shanghai）；https://static.cninfo.com.cn/finalpage/2026-08-26/1225501876.PDF`,
  `3. 2026年半年度分红派息实施公告 — official_announcement（巨潮资讯网）；披露日期：2026-08-20（仅日期，发布时间未核实）；实际采集时间：2026-09-08 08:00（Asia/Shanghai）；https://static.cninfo.com.cn/finalpage/2026-08-20/1225566652.PDF`
].join("\n");

const marketDate = (() => {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);
})();

const report = `# AStock 数据快报（语义未审）｜${day}

> 数据截止：${day} 08:00（Asia/Shanghai）
> 数据状态：部分可用
> 交易日状态：交易日 [来源 1]
> 发布级别：可发布（结构、来源与时效门禁通过；研究语义未审校）
> 生成方式：程序核验基础版

## 一句话结论

[事实] 程序已核验三大指数与股票池行情；本版只报告可复核字段，不生成因果判断。[来源 1]

## 1. 数据覆盖与可用性

> 数据桥模块评分只衡量模块新鲜度与可用性，不代表字段齐全或数值准确。
> 正式发布必要项覆盖：4/5；X/X 不代表全部行情字段齐全；必要项缺口：市场宽度未能从固定数据包核验。

| 类别 | 数据桥模块评分（非准确性） |
|---|---:|
| 行情 | 90% |
| 资金 | 70% |
| 财务 | 100% |

## 2. 上一交易日 A 股行情（${marketDate}）

- 上证指数收于 3724.88 点，较前收上涨 0.38%。[来源 1]
- 深证成指收于 11873.25 点，较前收上涨 0.61%。[来源 1]
- 创业板指收于 2578.91 点，较前收下跌 0.12%。[来源 1]
- 沪深两市成交额为 18420 亿元，市场宽度字段缺失。[来源 1]

## 3. 隔夜数据与事件

程序核验基础版未生成跨市场因果解释。

## 4. 板块与股票池

### 板块排行事实

- 通信设备板块涨幅靠前，日涨幅 2.14%。[来源 1]
- 银行板块跌幅靠前，日跌幅 0.73%。[来源 1]

### 股票池

### 600519 贵州茅台
- [事实] 收盘 1452.30 元，较前收上涨 0.45%。[来源 1]
- 财务字段缺少可引用的报告期来源，保持为空。
### 300750 宁德时代
- [事实] 收盘 236.80 元，较前收下跌 0.86%。[来源 1]
- 公告字段没有截止前的一手来源，不作补猜。
### 000001 平安银行
- [事实] 行情变化：收盘 12.34 元，较前收上涨 0.24%。[来源 1]
- 财务与公告字段没有截止前的一手来源，不作补猜。

## 5. 今日政策、公告与事件

程序核验基础版未编入事件判断。

## 6. 结构化分析覆盖层

本次没有程序证明合格的语义候选。

## 7. 风险提示与数据缺口

- 正式发布必要项缺口：市场宽度未能从固定数据包核验。
- 数据桥模块缺口：资金评分为 70%，不代表字段齐全。
- 股票池财务与公告字段并不完整，缺失内容没有补猜。
- 行情数字只对应最近已确认交易日，不代表盘中实时价格。

## 来源

> 以下仅列出本报告实际引用的固定数据包来源。

1. 上海证券交易所日行情 — market_data（上海证券交易所）；数据时间：${marketDate} 15:00（Asia/Shanghai）；https://example.com/sse-daily
${cninfo}

---

${disclaimer}
`;

const lastRun = {
  schemaVersion: "1.0",
  targetDate: day,
  startedAt: `${day}T00:00:04.910Z`,
  frozenCutoffInstant: `${day}T00:00:04.910Z`,
  cutoffLocal: `${day} 08:00`,
  status: "success",
  phase: "publish",
  completedAt: `${day}T00:00:24.000Z`,
  dataStatus: "partial",
  publicationGate: "passed",
  publicationBlockers: [],
  evidenceReview: "passed",
  evidenceReviewScope: "structure_source_freshness_only",
  researchSemanticReview: "not_human_reviewed",
  contextSha256: "a".repeat(64),
  semanticCandidateCount: 0,
  reportFile: `reports/daily/${day}.md`
};

await mkdir(path.join(root, "config"), { recursive: true });
await mkdir(path.join(root, "reports", "daily"), { recursive: true });
await mkdir(path.join(root, "work", "notifications"), { recursive: true });
await writeFile(path.join(root, "config", "xhs.json"), `${JSON.stringify({
  schemaVersion: "1.0",
  draftEnabled: true,
  autoPublishEnabled: false,
  humanApprovalRequired: true,
  outputRoot: "work/xhs",
  maxReportBytes: 262144,
  maxTitleGraphemes: 20,
  minBodyGraphemes: 200,
  maxBodyGraphemes: 900,
  defaultTags: ["A股", "财经", "市场复盘"]
}, null, 2)}\n`);
await writeFile(path.join(root, "config", "risk.json"), `${JSON.stringify({
  schemaVersion: "1.0",
  disclaimer
}, null, 2)}\n`);
await writeFile(path.join(root, "work", "last-run.json"), `${JSON.stringify(lastRun, null, 2)}\n`);
await writeFile(path.join(root, "reports", "daily", `${day}.md`), report);
await writeFile(path.join(root, "work", "notifications", "marker.json"), "{\"sent\":false}\n");

const result = await buildXhsDraft({
  projectRoot: root,
  desiredDate: day,
  now: new Date(nowIso)
});
if (result.status !== "draft_ready") {
  console.error(JSON.stringify(result));
  process.exit(2);
}
process.stdout.write(`${JSON.stringify(result)}\n`);
