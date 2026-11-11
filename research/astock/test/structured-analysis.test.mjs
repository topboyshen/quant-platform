import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import {
  STRUCTURED_ANALYSIS_MAX_OUTPUT_BYTES,
  STRUCTURED_ANALYSIS_SELECTION_LIMITS,
  STRUCTURED_ANALYSIS_TEXT_CHOICES,
  buildStructuredAnalysisCandidates,
  buildStructuredAnalysisInstruction,
  containsNumericSemantics,
  parseStructuredAnalysisOutput
} from "../src/structured-analysis.mjs";

const SESSION_DATE = "2026-08-07";

test("静态 Agent、动态指令及解析器共用八项总上限和现有角色上限", async () => {
  const prompt = await readFile(new URL("../agents/structured_analyst.md", import.meta.url), "utf8");
  const instruction = buildStructuredAnalysisInstruction({
    targetDate: "2026-08-10", cutoffLocal: "2026-08-10 08:00", context: opposingIndexContext()
  });
  assert.equal(STRUCTURED_ANALYSIS_SELECTION_LIMITS.total, 8);
  for (const content of [prompt, instruction]) {
    assert.match(content, new RegExp(`总数最多 ${STRUCTURED_ANALYSIS_SELECTION_LIMITS.total} 项`));
    for (const role of ["summary", "theme", "scenario", "risk"]) {
      assert.match(content, new RegExp(`${role} 最多 ${STRUCTURED_ANALYSIS_SELECTION_LIMITS[role]} 项`));
    }
    assert.match(content, new RegExp(`每只股票最多 ${STRUCTURED_ANALYSIS_SELECTION_LIMITS.stockPerCode} 项 stock`));
    assert.doesNotMatch(content, /最多选择一个候选/);
    assert.match(content, /不必选满|不得重复或为凑数量选满/);
  }
});

test("所有固定模板必须通过生产数字语义门禁，包括中文数词", () => {
  for (const [role, choices] of Object.entries(STRUCTURED_ANALYSIS_TEXT_CHOICES)) {
    for (const text of choices) {
      assert.equal(containsNumericSemantics(text), false, `${role}: ${text}`);
    }
  }
});

function indexRecord(id, changePercent) {
  const names = {
    sh000001: "上证指数",
    sz399001: "深证成指",
    sz399006: "创业板指"
  };
  return {
    code: id,
    name: names[id],
    changePercent,
    dataAsOf: `${SESSION_DATE}T15:00:00+08:00`,
    sourceIds: [`market:${id}`]
  };
}

function opposingIndexContext() {
  return {
    data: {
      previousAShareSession: {
        sessionDate: SESSION_DATE,
        dataAsOf: `${SESSION_DATE}T15:00:00+08:00`,
        indices: [
          indexRecord("sh000001", 0.6),
          indexRecord("sz399001", -0.4)
        ]
      },
      watchlist: [],
      announcementsAndNews: []
    },
    sources: [
      { id: "market:sh000001", sourceType: "market_data", dataAsOf: `${SESSION_DATE}T15:00:00+08:00` },
      { id: "market:sz399001", sourceType: "market_data", dataAsOf: `${SESSION_DATE}T15:00:00+08:00` },
      { id: "news:unrelated", sourceType: "media_aggregation", dataAsOf: "2026-08-10T07:30:00+08:00" }
    ]
  };
}

function aug17ContextFixture() {
  const sessionDate = "2026-08-14";
  const marketSources = [
    "akshare:sina:a-share-index-daily:7b140e2b7e91",
    "akshare:sina:a-share-index-daily:019090563ecb",
    "akshare:sina:a-share-index-daily:9256e5760fb7"
  ];
  const media = [
    ["akshare:eastmoney:global-news:fe996e6877e4", "Anthropic营收暴增并实现盈利"],
    ["akshare:eastmoney:global-news:63d73d3010bb", "俄称空袭乌港口 乌称打击俄物流枢纽"],
    ["akshare:eastmoney:global-news:5b1c9b673cc1", "监管趋严基金仍频走偏 风格漂移为何屡禁不止"],
    ["akshare:eastmoney:stock-news:dcf7de679b4e", "露笑科技收盘跌幅偏离值累计超限"],
    ["akshare:eastmoney:stock-news:d8b97770ac1d", "退休后东方电气原董事涉嫌严重违纪违法被查"],
    ["akshare:eastmoney:stock-news:2a3a28e54b6c", "计算机行业资金流出榜"]
  ];
  return {
    data: {
      previousAShareSession: {
        sessionDate,
        dataAsOf: `${sessionDate}T15:00:00+08:00`,
        indices: [
          { code: "sh000001", name: "上证指数", changePercent: 0.0053731062, dataAsOf: `${sessionDate}T15:00:00+08:00`, sourceIds: [marketSources[0]] },
          { code: "sz399001", name: "深证成指", changePercent: 0.4539295516, dataAsOf: `${sessionDate}T15:00:00+08:00`, sourceIds: [marketSources[1]] },
          { code: "sz399006", name: "创业板指", changePercent: 1.1224907231, dataAsOf: `${sessionDate}T15:00:00+08:00`, sourceIds: [marketSources[2]] }
        ],
        marketBreadth: {
          advancers: 2322,
          decliners: 2720,
          sourceIds: ["akshare:legulegu:market-activity:1e96549e3956"]
        }
      },
      watchlist: [
        { code: "002617", sourceIds: ["akshare:eastmoney:financial:792b50e5cac1"] },
        { code: "600875", sourceIds: ["akshare:eastmoney:financial:06b01b493493"] },
        { code: "300033", sourceIds: ["akshare:eastmoney:financial:d43665657cb1"] }
      ],
      announcementsAndNews: media.slice(3).map(([id], index) => ({
        code: ["002617", "600875", "300033"][index],
        sourceIds: [id]
      }))
    },
    sources: [
      ...marketSources.map((id) => ({ id, sourceType: "market_data", dataAsOf: `${sessionDate}T15:00:00+08:00` })),
      { id: "akshare:legulegu:market-activity:1e96549e3956", sourceType: "market_data", dataAsOf: `${sessionDate}T15:00:00+08:00` },
      { id: "akshare:ths:industry-summary", sourceType: "industry_market_data", dataAsOf: `${sessionDate}T15:00:00+08:00` },
      ...media.map(([id, title]) => ({ id, title, sourceType: "media_aggregation", dataAsOf: "2026-08-17T07:45:43+08:00" })),
      ...[
        "akshare:eastmoney:financial:792b50e5cac1",
        "akshare:eastmoney:financial:06b01b493493",
        "akshare:eastmoney:financial:d43665657cb1"
      ].map((id) => ({ id, sourceType: "financial_data_aggregation", dataAsOf: "2026-04-30T23:59:59+08:00" }))
    ]
  };
}

function candidateSelection(candidateId) {
  return JSON.stringify({ schemaVersion: "2.0", candidateIds: [candidateId] });
}

function escaped(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

test("同一交易日相反 A 股指数涨跌生成唯一可证明 summary 候选", () => {
  const context = opposingIndexContext();
  const candidates = buildStructuredAnalysisCandidates(context);
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0].kind, "summary");
  assert.equal(candidates[0].proofKind, "same_session_opposite_ashare_index_returns");
  assert.deepEqual(candidates[0].evidenceIds, ["market:sh000001"]);
  assert.deepEqual(candidates[0].counterEvidenceIds, ["market:sz399001"]);

  const parsed = parseStructuredAnalysisOutput(candidateSelection(candidates[0].id), { context });
  assert.deepEqual(parsed, {
    schemaVersion: "1.0",
    summary: {
      text: "股指涨跌方向冲突，仍需观察反证",
      evidenceIds: ["market:sh000001"],
      counterEvidenceIds: ["market:sz399001"]
    },
    themes: [], stocks: [], scenarios: [], risks: []
  });
});

test("候选 ID 与输出顺序不受 context 数组顺序影响", () => {
  const first = opposingIndexContext();
  const second = structuredClone(first);
  second.data.previousAShareSession.indices.reverse();
  second.sources.reverse();
  assert.deepEqual(buildStructuredAnalysisCandidates(second), buildStructuredAnalysisCandidates(first));
});

test("v2 允许空选择，未知候选、旧 overlay 和额外字段一律拒绝", () => {
  const context = opposingIndexContext();
  assert.equal(parseStructuredAnalysisOutput(JSON.stringify({ schemaVersion: "2.0", candidateIds: [] }), { context }), null);
  assert.throws(() => parseStructuredAnalysisOutput(candidateSelection("news:unrelated"), { context }), /程序候选之外/);
  assert.throws(() => parseStructuredAnalysisOutput(JSON.stringify({
    schemaVersion: "2.0", candidateIds: [], evidenceIds: ["market:sh000001"]
  }), { context }), /不允许的字段/);
  assert.throws(() => parseStructuredAnalysisOutput(JSON.stringify({
    schemaVersion: "1.0", summary: null, themes: [], stocks: [], scenarios: [], risks: []
  }), { context }));
});

test("媒体来源即使挂到指数记录也永不成为候选", () => {
  const context = opposingIndexContext();
  context.data.previousAShareSession.indices[1].sourceIds = ["news:unrelated"];
  assert.deepEqual(buildStructuredAnalysisCandidates(context), []);
});

test("未知指数或核心指数名称不匹配不能构造候选", () => {
  const unknown = opposingIndexContext();
  unknown.data.previousAShareSession.indices[1].code = "sz399999";
  unknown.data.previousAShareSession.indices[1].name = "未知指数";
  assert.deepEqual(buildStructuredAnalysisCandidates(unknown), []);

  const renamed = opposingIndexContext();
  renamed.data.previousAShareSession.indices[1].name = "深证指数别名";
  assert.deepEqual(buildStructuredAnalysisCandidates(renamed), []);
});

test("8月17日真实错误组合无候选：Anthropic 对俄乌、基金漂移和弱公司新闻全部降级", () => {
  const context = aug17ContextFixture();
  assert.deepEqual(buildStructuredAnalysisCandidates(context), []);
  const rejectedIds = [
    "akshare:eastmoney:global-news:fe996e6877e4",
    "akshare:eastmoney:global-news:63d73d3010bb",
    "akshare:eastmoney:global-news:5b1c9b673cc1",
    "akshare:eastmoney:stock-news:dcf7de679b4e",
    "akshare:eastmoney:stock-news:d8b97770ac1d",
    "akshare:eastmoney:stock-news:2a3a28e54b6c",
    "akshare:eastmoney:financial:792b50e5cac1"
  ];
  for (const id of rejectedIds) {
    assert.throws(() => parseStructuredAnalysisOutput(candidateSelection(id), { context }), /程序候选之外/, id);
  }

  const actualBadOverlay = {
    schemaVersion: "1.0",
    summary: {
      text: "市场线索可能分化，仍需观察反证",
      evidenceIds: ["akshare:ths:industry-summary"],
      counterEvidenceIds: ["akshare:eastmoney:global-news:5b1c9b673cc1"]
    },
    themes: [{
      title: "待验证主题",
      text: "若行业线索延续，仍需观察反向风险",
      evidenceIds: ["akshare:eastmoney:global-news:fe996e6877e4"],
      counterEvidenceIds: ["akshare:eastmoney:global-news:63d73d3010bb"]
    }],
    stocks: [{
      code: "600875",
      text: "公司证据可能分化，仍需观察反证",
      evidenceIds: ["akshare:eastmoney:financial:06b01b493493"],
      counterEvidenceIds: ["akshare:eastmoney:stock-news:d8b97770ac1d"]
    }],
    scenarios: [], risks: []
  };
  assert.throws(() => parseStructuredAnalysisOutput(JSON.stringify(actualBadOverlay), { context }));

  const instruction = buildStructuredAnalysisInstruction({
    targetDate: "2026-08-17", cutoffLocal: "2026-08-17 08:00", context
  });
  for (const id of rejectedIds) assert.doesNotMatch(instruction, new RegExp(escaped(id)));
  assert.match(instruction, /程序证明合格的候选（唯一白名单）：\n\[\]/);
});

test("指令只公开程序候选并明确允许空选择", () => {
  const context = opposingIndexContext();
  const [candidate] = buildStructuredAnalysisCandidates(context);
  const instruction = buildStructuredAnalysisInstruction({
    targetDate: "2026-08-10", cutoffLocal: "2026-08-10 08:00", context
  });
  assert.match(instruction, /schemaVersion": "2\.0"/);
  assert.match(instruction, /candidateIds/);
  assert.match(instruction, /允许跳过所有候选/);
  assert.match(instruction, new RegExp(escaped(candidate.id)));
  assert.doesNotMatch(instruction, /news:unrelated/);
  assert.doesNotMatch(instruction, /全部来源 ID 白名单|股票来源 ID 白名单/);
});

test("结构化 JSON 按 UTF-8 字节严格限制为 64 KiB", () => {
  const context = opposingIndexContext();
  assert.equal(STRUCTURED_ANALYSIS_MAX_OUTPUT_BYTES, 64 * 1024);
  assert.throws(() => parseStructuredAnalysisOutput(" ".repeat(STRUCTURED_ANALYSIS_MAX_OUTPUT_BYTES), { context }), /不是有效 JSON/);
  for (const output of [
    " ".repeat(STRUCTURED_ANALYSIS_MAX_OUTPUT_BYTES + 1),
    "中".repeat(Math.floor(STRUCTURED_ANALYSIS_MAX_OUTPUT_BYTES / 3) + 1)
  ]) {
    assert.throws(() => parseStructuredAnalysisOutput(output, { context }), /1—64 KiB/);
  }
});

test("利润同比与行情方向相反时才生成股票候选，无数字模板", () => {
  const context = opposingIndexContext();
  context.data.watchlist = [{
    code: "002617",
    marketData: {
      changePercent: 2.28,
      units: { changePercent: "percentage_point" },
      dataAsOf: `${SESSION_DATE}T15:00:00+08:00`,
      sourceIds: ["market:stock"]
    },
    financials: {
      parentNetProfitYoY: -23.76,
      units: { parentNetProfitYoY: "percentage_point" },
      sourceIds: ["fin:stock"]
    }
  }];
  context.sources.push(
    { id: "market:stock", sourceType: "market_data", dataAsOf: `${SESSION_DATE}T15:00:00+08:00` },
    { id: "fin:stock", sourceType: "financial_data_aggregation", dataAsOf: "2026-06-30T23:59:59+08:00" }
  );
  const candidates = buildStructuredAnalysisCandidates(context);
  assert.equal(candidates.some((item) => item.proofKind === "watchlist_profit_yoy_versus_price"), true);
  const stock = candidates.find((item) => item.proofKind === "watchlist_profit_yoy_versus_price");
  assert.equal(stock.text, "单日行情与报告期利润同比方向不同，期间不同，不足以认定同期背离，仍需继续核验");
  assert.deepEqual(stock.evidenceIds, ["fin:stock", "market:stock"]);
  assert.deepEqual(stock.counterEvidenceIds, []);
  assert.doesNotMatch(stock.text, /可能背离|反证/);
  assert.doesNotMatch(stock.text, /\d/);
  const parsed = parseStructuredAnalysisOutput(JSON.stringify({
    schemaVersion: "2.0",
    candidateIds: candidates.map((item) => item.id)
  }), { context });
  assert.equal(parsed.summary.text, "股指涨跌方向冲突，仍需观察反证");
  assert.equal(parsed.stocks[0].code, "002617");
});

test("未对齐板块名称时不生成个股相对板块候选", () => {
  const context = opposingIndexContext();
  context.data.watchlist = [{
    code: "002617",
    researchTags: ["第三代半导体"],
    marketData: {
      changePercent: 2.28,
      units: { changePercent: "percentage_point" },
      dataAsOf: `${SESSION_DATE}T15:00:00+08:00`,
      sourceIds: ["market:sh000001"]
    }
  }];
  context.data.sectors = [{
    name: "半导体",
    changePercent: -1.28,
    units: { changePercent: "percentage_point" },
    sourceIds: ["sector"]
  }];
  context.sources.push({ id: "sector", sourceType: "industry_market_data", dataAsOf: `${SESSION_DATE}T15:00:00+08:00` });
  const candidates = buildStructuredAnalysisCandidates(context);
  assert.equal(candidates.some((item) => item.proofKind === "watchlist_versus_named_sector"), false);
});

test("sector 与当日板块名完全一致且方向相反时才生成相对板块候选", () => {
  const context = opposingIndexContext();
  context.data.watchlist = [{
    code: "002617",
    sector: "半导体",
    marketData: {
      changePercent: 2.28,
      units: { changePercent: "percentage_point" },
      dataAsOf: `${SESSION_DATE}T15:00:00+08:00`,
      sourceIds: ["market:stock"]
    }
  }];
  context.data.sectors = [{
    name: "半导体",
    changePercent: -1.28,
    units: { changePercent: "percentage_point" },
    sourceIds: ["sector"]
  }];
  context.sources.push(
    { id: "market:stock", sourceType: "market_data", dataAsOf: `${SESSION_DATE}T15:00:00+08:00` },
    { id: "sector", sourceType: "industry_market_data", dataAsOf: `${SESSION_DATE}T15:00:00+08:00` }
  );
  const candidates = buildStructuredAnalysisCandidates(context);
  const theme = candidates.find((item) => item.proofKind === "watchlist_versus_named_sector");
  assert.ok(theme);
  assert.equal(theme.kind, "theme");
  assert.doesNotMatch(theme.text, /\d/);
  assert.equal(containsNumericSemantics(theme.text), false);
  const overlay = parseStructuredAnalysisOutput(JSON.stringify({
    schemaVersion: "2.0", candidateIds: [theme.id]
  }), { context });
  assert.equal(overlay.themes[0].text, theme.text);
});

test("上涨占优的成交额来源是支持证据，不是反证", () => {
  const context = opposingIndexContext();
  context.data.previousAShareSession.marketBreadth = {
    advancers: 4216, decliners: 914, unchanged: 79, sourceIds: ["breadth"]
  };
  context.data.previousAShareSession.totalTurnover = 2_033_759_165_319.21;
  context.data.previousAShareSession.units = { totalTurnover: "CNY" };
  context.data.previousAShareSession.turnoverSourceIds = ["sse", "szse"];
  context.sources.push(
    { id: "breadth", sourceType: "market_data", dataAsOf: `${SESSION_DATE}T15:00:00+08:00` },
    { id: "sse", sourceType: "market_data", dataAsOf: `${SESSION_DATE}T15:00:00+08:00` },
    { id: "szse", sourceType: "market_data", dataAsOf: `${SESSION_DATE}T15:00:00+08:00` }
  );
  const theme = buildStructuredAnalysisCandidates(context)
    .find((item) => item.proofKind === "previous_session_breadth_and_turnover");
  assert.equal(theme.text, "上涨家数占优且成交额可核验，仍需继续核验");
  assert.deepEqual(theme.evidenceIds, ["breadth", "sse", "szse"]);
  assert.deepEqual(theme.counterEvidenceIds, []);
  assert.equal(theme.evidenceIds.includes("sse") && theme.counterEvidenceIds.includes("sse"), false);
});
