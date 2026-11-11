#!/usr/bin/env node
// Fixed historical/synthetic fixtures -> work/comparison-second only. No
// notification imports, credentials, network, seals or production state access.
import { createHash } from "node:crypto";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { buildResultView } from "../src/result-view.mjs";
import { buildV3Brief, V3_VERSION } from "../src/result-view-v3.mjs";
import { buildQuietCandidateReport } from "../test/helpers/quiet-candidate-context.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
if (process.argv.length !== 2) throw new Error("offline preview takes no production paths or dates");
const hash = (text) => createHash("sha256").update(text).digest("hex");
const historical = async (relativePath, expectedHash) => {
  const source = await readFile(path.join(root, relativePath), "utf8");
  if (hash(source) !== expectedHash) throw new Error(`fixture hash changed: ${relativePath}`);
  return source;
};
const scenarios = [
  {
    key: "normal", title: "正常日：9/18 历史材料", targetDate: "2026-09-18", synthetic: false,
    report: await historical("test/fixtures/research-brief-normal-2026-09-18.md",
      "58e5658cb528a97c24f295c8136e05f2bdc8efe1ed0f68cdda80c9f0c617d1b2"),
    note: "不同交易时段分别标注；财务口径差异是背景核验，不用单日股价对比半年利润。公告查询结果晚于截止，不能据此确认截止前有无新披露。",
    expectV1: true,
  },
  {
    key: "quiet-candidate", title: "构造静日候选：未判定静日", ...buildQuietCandidateReport(),
    note: "这是构造样例，不是真实晨报。指数与观察标的平盘、涨跌各 2500 家，但没有完整跨日比较证据，公告也未核实，因此仍展示事实与未知项，不写‘今日无重要变化’。原夹具的陈旧标记继续保留。",
    expectV1: true,
  },
  {
    key: "missing", title: "关键数据缺失日：8/21 历史材料", targetDate: "2026-08-21", synthetic: false,
    report: await historical("test/fixtures/research-brief-missing-2026-08-21.md",
      "5d5571e6bd80924d9d6155e3d98160b50acb797faa707a09eeac68ecf951a6c7"),
    note: "行情涨跌幅可读；成交额与市场宽度未通过当时门禁，原稿中的成交额不进入首屏。公司公告不可核验仍是正文重点。旧 v1 不支持这份历史格式，此处对照的是存档原稿，不冒称旧 v1 输出。存档不是对当日发送现场的还原。",
    expectV1: false,
  },
];
const out = path.join(root, "work/comparison-second");
const files = {};
const metrics = [];
const comparison = ["# v3 三场景阅读对照", "",
  "本机离线候选：未部署、未发送、未修改任何原稿或密封。正文精简，完整来源、推断的支持与反证及限制保留在详情。", "",
  "第二项是构造的静日候选，不是静日规则已实现；第三项的旧格式不受 v1 支持，不伪造旧版渲染结果。", ""];

for (const scenario of scenarios) {
  const { key, title, targetDate, synthetic, report, note, expectV1 } = scenario;
  const before = hash(report);
  const full = buildV3Brief(report, { targetDate });
  const body = full.split("\n## 详情\n")[0].trimEnd() + "\n";
  let v1 = null;
  let v1Error = null;
  try { v1 = buildResultView(report, { targetDate }); }
  catch (error) { v1Error = error.message; }
  if ((v1 !== null) !== expectV1) throw new Error(`unexpected v1 compatibility: ${key}`);
  if (before !== hash(report) || !full.endsWith(report)) throw new Error(`source not retained: ${key}`);
  const label = synthetic ? `> ${scenario.label}。所有下列行情与来源均为测试夹具，不能用于投资判断。\n\n`
    : "> 历史材料只读展示，不是重新生成、重新密封或补发。\n\n";
  const baselineName = `${key}-${v1 === null ? "original" : "v1"}.md`;
  files[baselineName] = label + (v1 ?? report);
  files[`${key}-v3-brief.md`] = label + body;
  files[`${key}-v3-full.md`] = label + full;
  files[`${key}-comparison.md`] = `# ${title}\n\n${label}${note}\n\n## 新版首屏\n\n${body}\n` +
    `## 对照基线：${v1 === null ? "历史原稿（v1 不支持）" : "旧 v1 原样输出"}\n\n${v1 ?? report}`;
  const embeddedBody = body.replace(/^## /gm, "#### ").replace(/^# /gm, "### ");
  comparison.push(`## ${title}`, "", label.trimEnd(), "", note, "", embeddedBody,
    `[对照基线](${baselineName}) · [新版完整详情](${key}-v3-full.md) · [新旧对照材料](${key}-comparison.md)`, "", "---", "");
  metrics.push({
    key, targetDate, synthetic, fixtureKind: synthetic ? "synthetic_not_a_quiet_verdict" : "historical_read_only",
    sourceSha256: before, v1Supported: v1 !== null, v1Error,
    v1Sha256: v1 === null ? null : hash(v1), v3Sha256: hash(full),
    sourceBytes: Buffer.byteLength(report), v1Bytes: v1 === null ? null : Buffer.byteLength(v1),
    briefCharactersIncludingHeadingsAndReferences: Array.from(body).length,
    fullCandidateBytesExcludingPreviewLabel: Buffer.byteLength(full), originalRetainedExactly: true,
  });
}
files["three-scenarios.md"] = comparison.join("\n");
files["metrics.json"] = JSON.stringify({
  baseCommit: "00d7d6050a6d0ddf708a78dfd9990e42083c0676", presentation: V3_VERSION,
  scope: "local_offline_only", productionModified: false, messagesSent: 0, scenarios: metrics,
}, null, 2) + "\n";
await mkdir(out, { recursive: true });
for (const [name, content] of Object.entries(files)) await writeFile(path.join(out, name), content);
console.log(JSON.stringify({ output: "work/comparison-second/three-scenarios.md", scenarios: metrics }));
