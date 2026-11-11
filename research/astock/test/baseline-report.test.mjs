import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  attachPreviousFinancialPeriods,
  buildVerifiedBaselineReport,
  collectCurrentFinancialSnapshots,
  previousFinancialPeriodsFromArchive
} from "../src/baseline-report.mjs";
import { assessPublicationGate, normalizeReportOutput } from "../src/report.mjs";
import {
  buildStructuredAnalysisCandidates,
  parseStructuredAnalysisOutput
} from "../src/structured-analysis.mjs";
import { documentFixture } from "./helpers/document-fixture.mjs";

const TARGET_DATE = "2026-08-10";
const CUTOFF_LOCAL = "2026-08-10 08:00";
const CUTOFF = new Date("2026-08-10T00:00:00Z");
const PUBLICATION_LEVEL = "可发布（结构、来源与时效门禁通过；研究语义未审校）";
const DISCLAIMER = "本报告仅供研究与信息参考，不构成任何投资建议。";

function fixtureFinancialBaseline(context) {
  return previousFinancialPeriodsFromArchive({
    schemaVersion: "2.0", targetDate: "2026-08-07", reportSha256: "a".repeat(64),
    periods: collectCurrentFinancialSnapshots(context)
  }, TARGET_DATE);
}

function source(id, title, dataAsOf, extra = {}) {
  return {
    id,
    title,
    url: `https://example.com/${id}`,
    publisher: "测试数据源",
    sourceType: "market_data",
    dataAsOf,
    ...extra
  };
}

function indexRecord(code, name, close, previousClose) {
  const change = close - previousClose;
  return {
    code,
    name,
    close,
    previousClose,
    change,
    changePercent: change / previousClose * 100,
    units: {
      close: "index_point",
      previousClose: "index_point",
      change: "index_point",
      changePercent: "percentage_point"
    },
    dataAsOf: "2026-08-07T15:00:00+08:00",
    sourceIds: ["market"]
  };
}

function contextFixture({ missingWatchlistMarket = false } = {}) {
  const marketData = missingWatchlistMarket
    ? null
    : {
      code: "000001",
      name: "测试银行",
      last: 12.3,
      previousClose: 12,
      change: 0.3,
      changePercent: 2.5,
      units: {
        last: "CNY_per_share",
        previousClose: "CNY_per_share",
        change: "CNY_per_share",
        changePercent: "percentage_point"
      },
      dataAsOf: "2026-08-07T15:00:00+08:00",
      sourceIds: ["market"]
    };
  return {
    schemaVersion: "1.0",
    meta: {
      status: "partial",
      provider: "fixture",
      sourceType: "fixture",
      targetDate: TARGET_DATE,
      asOf: "2026-08-10T08:00:00+08:00",
      timezone: "Asia/Shanghai",
      isLive: false,
      dataCompleteness: {
        market: 96,
        capital: 50,
        news: 75,
        financial: 100,
        overall: 89
      }
    },
    data: {
      tradingCalendar: {
        targetDate: TARGET_DATE,
        status: "trading_day",
        previousTradingDate: "2026-08-07",
        dataAsOf: "2026-08-10T08:00:00+08:00",
        sourceIds: ["calendar"]
      },
      overnightGlobal: [],
      previousAShareSession: {
        sessionDate: "2026-08-07",
        indices: [
          indexRecord("sh000001", "上证指数", 3940.037, 3900.352),
          indexRecord("sz399001", "深证成指", 14311.008, 14110.121),
          indexRecord("sz399006", "创业板指", 3563.116, 3515.56)
        ],
        totalTurnover: 300010000000,
        turnoverByExchange: {
          SH: 120000000000,
          SZ: 180010000000,
          sourceIds: ["turnover-sh", "turnover-sz"]
        },
        marketBreadth: {
          advancers: 2670,
          decliners: 2399,
          unchanged: 133,
          sourceIds: ["breadth"]
        },
        units: { totalTurnover: "CNY" },
        dataAsOf: "2026-08-07T15:00:00+08:00",
        sourceIds: ["market", "turnover-sh", "turnover-sz", "breadth"]
      },
      policyAndCalendar: [],
      sectors: [
        {
          rank: 1,
          direction: "up",
          name: "医疗服务",
          changePercent: 8.34,
          units: { changePercent: "percentage_point" },
          dataAsOf: "2026-08-07T15:00:00+08:00",
          sourceIds: ["sector"]
        },
        {
          rank: 90,
          direction: "down",
          name: "软件开发",
          changePercent: -1.29,
          units: { changePercent: "percentage_point" },
          dataAsOf: "2026-08-07T15:00:00+08:00",
          sourceIds: ["sector"]
        }
      ],
      capitalFlows: [{ type: "northbound_realtime", status: "unavailable" }],
      watchlist: [{
        code: "000001",
        exchange: "SZ",
        symbol: "000001.SZ",
        name: "测试银行",
        status: missingWatchlistMarket ? "unavailable" : "available",
        marketData,
        financials: {
          reportDate: "2026-03-31",
          parentNetProfit: 100,
          dataAsOf: "2026-04-30T23:59:59+08:00",
          sourceIds: ["financial"]
        },
        monitor: ["量价及流动性异常", "盈利质量与现金流"],
        invalidationSignals: ["核心增长假设缺乏公告或财务数据支持"]
      }],
      announcementsAndNews: [],
      sectionCompleteness: {
        tradingCalendar: 100,
        overnightGlobal: 50,
        previousAShareSession: 96,
        policyAndCalendar: 50,
        sectors: 100,
        capitalFlows: 50,
        watchlist: 100,
        announcementsAndNews: 50
      }
    },
    sources: [
      source("calendar", "交易日历数据", "2026-08-10T08:00:00+08:00", {
        sourceType: "trading_calendar_data"
      }),
      source("market", "上一交易日行情数据", "2026-08-07T15:00:00+08:00"),
      source("turnover-sh", "上海市场成交额", "2026-08-07T15:00:00+08:00"),
      source("turnover-sz", "深圳市场成交额", "2026-08-07T15:00:00+08:00"),
      source("breadth", "市场宽度数据", "2026-08-07T15:00:00+08:00"),
      source("sector", "行业板块行情数据", "2026-08-07T15:00:00+08:00", {
        sourceType: "industry_market_data"
      }),
      source("financial", "季度财务数据", "2026-04-30T23:59:59+08:00", {
        sourceType: "financial_data_aggregation"
      }),
      source("unused", "未引用来源", "2026-08-07T15:00:00+08:00")
    ]
  };
}

function semanticCandidateContext() {
  const context = contextFixture();
  const [shanghai, shenzhen, chinext] = context.data.previousAShareSession.indices;
  shanghai.sourceIds = ["market-index-up"];
  shenzhen.close = 14000;
  shenzhen.previousClose = 14110;
  shenzhen.change = -110;
  shenzhen.changePercent = -110 / 14110 * 100;
  shenzhen.sourceIds = ["market-index-down"];
  chinext.sourceIds = ["market-index-up-2"];
  context.sources.push(
    source("market-index-up", "上证指数日线", "2026-08-07T15:00:00+08:00"),
    source("market-index-down", "深证成指日线", "2026-08-07T15:00:00+08:00"),
    source("market-index-up-2", "创业板指日线", "2026-08-07T15:00:00+08:00")
  );
  return context;
}

function opposingStockSectorContext(stockDirection = "up") {
  const context = contextFixture();
  const stock = context.data.watchlist[0];
  stock.sector = stockDirection === "up" ? "软件开发" : "医疗服务";
  if (stockDirection === "down") {
    Object.assign(stock.marketData, {
      last: 11.7,
      change: -0.3,
      changePercent: -2.5
    });
  }
  return context;
}

function buildOptions(context, publicationAudit, extra = {}) {
  return {
    context,
    targetDate: TARGET_DATE,
    cutoffLocal: CUTOFF_LOCAL,
    cutoffInstant: CUTOFF,
    publicationLevel: PUBLICATION_LEVEL,
    publicationAudit,
    disclaimer: DISCLAIMER,
    marketDataMaxAgeHours: 18,
    newsMaxAgeHours: 72,
    ...extra
  };
}

function normalizationOptions(context, publicationAudit) {
  return {
    targetDate: TARGET_DATE,
    disclaimer: DISCLAIMER,
    cutoffLocal: CUTOFF_LOCAL,
    cutoffInstant: CUTOFF,
    publicationLevel: PUBLICATION_LEVEL,
    requireStaleMarketLabel: true,
    context,
    strictContextSources: true,
    marketDataMaxAgeHours: 18,
    newsMaxAgeHours: 72,
    publicationAudit,
    enforcePublicationReady: true,
    requireDeterministicCompilerOutput: true
  };
}

for (const direction of ["up", "down"]) {
  test(`同日个股与板块方向相反（${direction}）时未选主题的基础稿仍通过全部候选编译白名单`, () => {
    const context = opposingStockSectorContext(direction);
    const publicationAudit = assessPublicationGate({
      context,
      cutoffInstant: CUTOFF,
      targetDate: TARGET_DATE
    });
    assert.equal(publicationAudit.passed, true);
    const candidate = buildStructuredAnalysisCandidates(context)
      .find((item) => item.proofKind === "watchlist_versus_named_sector");
    assert.ok(candidate, "合成同日反向输入必须实际生成触发故障的候选");
    assert.equal(candidate.sessionDate, context.data.previousAShareSession.sessionDate);
    assert.deepEqual(candidate.evidenceIds, ["market"]);
    assert.deepEqual(candidate.counterEvidenceIds, ["sector"]);

    const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
    assert.doesNotMatch(report, /程序核验基础版 \+ 结构化分析覆盖层/);
    assert.ok(!report.includes(candidate.text), "基础稿没有选用候选，但门禁仍必须安全编译完整白名单");
    assert.equal(normalizeReportOutput(report, normalizationOptions(context, publicationAudit)), report);
  });
}

test("反向板块单候选从候选 ID 展开、编译到最终报告门完整通过", () => {
  const context = opposingStockSectorContext();
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const candidates = buildStructuredAnalysisCandidates(context);
  const candidate = candidates.find((item) => item.proofKind === "watchlist_versus_named_sector");
  assert.ok(candidate);
  const overlay = parseStructuredAnalysisOutput(JSON.stringify({
    schemaVersion: "2.0",
    candidateIds: [candidate.id]
  }), { context });
  assert.equal(overlay.themes.length, 1);
  assert.equal(overlay.themes[0].text, candidate.text);
  assert.deepEqual(overlay.themes[0].evidenceIds, ["market"]);
  assert.deepEqual(overlay.themes[0].counterEvidenceIds, ["sector"]);
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit, {
    analysisOverlay: overlay
  }));
  assert.match(report, /^> 生成方式：程序核验基础版 \+ 结构化分析覆盖层$/m);
  assert.ok(report.includes(candidate.text));
  assert.equal(normalizeReportOutput(report, normalizationOptions(context, publicationAudit)), report);
  assert.equal(normalizeReportOutput(
    normalizeReportOutput(report, normalizationOptions(context, publicationAudit)),
    normalizationOptions(context, publicationAudit)
  ), report);

  for (const alternative of candidates) {
    const singleton = parseStructuredAnalysisOutput(JSON.stringify({
      schemaVersion: "2.0", candidateIds: [alternative.id]
    }), { context });
    const compiled = buildVerifiedBaselineReport(buildOptions(context, publicationAudit, {
      analysisOverlay: singleton
    }));
    assert.equal(normalizeReportOutput(compiled, normalizationOptions(context, publicationAudit)), compiled,
      `该上下文中每个单候选均须可编译并通过固定白名单：${alternative.proofKind}`);
  }
});

test("板块模板修复不放宽旧含一文本、外来数字或交易指令的编译与最终门禁", () => {
  const context = opposingStockSectorContext();
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const candidate = buildStructuredAnalysisCandidates(context)
    .find((item) => item.proofKind === "watchlist_versus_named_sector");
  assert.ok(candidate);
  const overlay = parseStructuredAnalysisOutput(JSON.stringify({
    schemaVersion: "2.0", candidateIds: [candidate.id]
  }), { context });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit, {
    analysisOverlay: overlay
  }));
  assert.equal(normalizeReportOutput(report, normalizationOptions(context, publicationAudit)), report,
    "先证明原始有效稿通过，避免无关候选故障掩盖拒绝测试");
  const maliciousTexts = [
    "个股与已对齐板块的方向可能不一致，仍需观察反证",
    "个股与已对齐板块的方向可能背离，预计涨幅 8%，仍需观察反证",
    "个股与已对齐板块的方向可能背离，预计上涨壹成，仍需观察反证",
    "个股与已对齐板块的方向可能背离，建议买入并加仓"
  ];
  for (const text of maliciousTexts) {
    const forgedOverlay = structuredClone(overlay);
    forgedOverlay.themes[0].text = text;
    assert.throws(() => buildVerifiedBaselineReport(buildOptions(context, publicationAudit, {
      analysisOverlay: forgedOverlay
    })), /数字|数词|交易|禁止|固定模板/, `编译器必须拒绝：${text}`);
    const forgedReport = report.replace(candidate.text, text);
    assert.notEqual(forgedReport, report);
    assert.throws(() => normalizeReportOutput(forgedReport, normalizationOptions(context, publicationAudit)),
      /数字|数词|交易|禁止|固定模板/, `最终报告门必须拒绝：${text}`);
  }

  assert.throws(() => parseStructuredAnalysisOutput(JSON.stringify({
    schemaVersion: "2.0", candidateIds: [candidate.id], text: maliciousTexts[0]
  }), { context }), /不允许的字段/);
  assert.throws(() => parseStructuredAnalysisOutput(JSON.stringify({
    schemaVersion: "2.0", candidateIds: ["outside-approved-candidates"]
  }), { context }), /程序候选之外/);
});

test("程序基础版相同输入稳定幂等并通过现有正式门禁", () => {
  const context = contextFixture();
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  assert.equal(publicationAudit.passed, true);
  assert.deepEqual(publicationAudit.marketCoverage, {
    obtained: 3,
    required: 3,
    score: 100,
    gaps: []
  });

  const first = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  const second = buildVerifiedBaselineReport(buildOptions(
    structuredClone(context),
    structuredClone(publicationAudit)
  ));
  assert.equal(first, second);
  assert.match(first, /^# AStock 数据快报（语义未审）｜2026-08-10$/m);
  assert.match(first, /股票池官方公告查询未完成或结果不适用于截止，不判断窗口内有无新增披露/);
  assert.doesNotMatch(first, /逻辑状态：仅列示已核验事实/);
  assert.match(first, /^> 生成方式：程序核验基础版$/m);
  assert.match(first, /门禁必要项：3\/3/);
  assert.match(first, /模块状态分数（非字段完整度）：行情 96% \/ 资金 50% \/ 新闻 75% \/ 财务 100%/);
  assert.match(first, /研究可用项：隔夜未写入；财务数字未写入；公告官方源无/);
  assert.doesNotMatch(first, /X\/X/);
  assert.doesNotMatch(first, /\| 类别 \| 数据桥模块评分/);
  assert.doesNotMatch(first, /数据桥模块缺口/);
  assert.doesNotMatch(first, /公司官方公告源未提供/);
  assert.match(
    first,
    /^> 行情口径：2026-08-07 已由交易日历确认为最近有效交易日；\[陈旧\] 仅表示距固定截止超过 18 小时、不是实时行情，不表示错用更早交易日。$/m
  );
  assert.match(first, /上证指数：收盘 3940\.037 点\[陈旧\].*前收 3900\.352 点\[陈旧\]/);
  assert.match(first, /涨跌 \+39\.685 点\[陈旧\]（\+1\.02%\[陈旧\]）/);
  assert.match(first, /沪深两市成交额：3000\.1 亿元\[陈旧\]/);
  const turnoverLine = first.split("\n").find((line) => line.includes("沪深两市成交额：3000.1"));
  const breadthLine = first.split("\n").find((line) => line.includes("市场宽度：上涨 2670"));
  assert.match(turnoverLine, /\[来源 3、4\]$/);
  assert.match(breadthLine, /\[来源 5\]$/);
  assert.match(first, /### 000001 测试银行/);
  assert.match(first, /量价：上一交易日行情已提供/);
  assert.doesNotMatch(first, /归母净利润：数据包已提供/);
  assert.match(first, /财务比较：缺少已核验的历史比较基准/);
  assert.doesNotMatch(first, /已观测/);
  assert.doesNotMatch(first, /盈利质量与现金流/);
  assert.match(first, /官方源缺失：未提供巨潮公告查询结果/);
  assert.match(first, /收盘 12\.3 元\[陈旧\].*涨跌幅 \+2\.5%\[陈旧\]/);
  assert.doesNotMatch(first, /https:\/\/example\.com\/unused/);

  const normalized = normalizeReportOutput(first, normalizationOptions(context, publicationAudit));
  assert.equal(normalized, first);
  assert.equal(normalizeReportOutput(normalized, normalizationOptions(context, publicationAudit)), first);

  const freeAnalysis = first.replace(
    "程序核验基础版不生成因果推断、价格目标、买卖或仓位建议。",
    "程序核验基础版不生成因果推断、价格目标、买卖或仓位建议。\n" +
      "- 企业经营线索可能与地缘局势变化有关，仍需观察反证。"
  );
  assert.throws(
    () => normalizeReportOutput(freeAnalysis, normalizationOptions(context, publicationAudit)),
    /生产发布只接受程序编译器逐字生成/
  );

  const duplicatePublicationLevel = first.replace(
    `> 发布级别：${PUBLICATION_LEVEL}`,
    `> 发布级别：${PUBLICATION_LEVEL}\n> 发布级别：正式（核心数据与证据门禁通过）`
  );
  assert.throws(
    () => normalizeReportOutput(
      duplicatePublicationLevel,
      normalizationOptions(context, publicationAudit)
    ),
    /恰好包含一条发布级别声明/
  );
});

test("基础版在核心门禁未通过时拒绝构建", () => {
  const context = contextFixture();
  assert.throws(
    () => buildVerifiedBaselineReport(buildOptions(context, {
      passed: false,
      blockers: ["核心指数缺失"],
      marketCoverage: { obtained: 0, required: 3, gaps: ["核心指数缺失"] }
    })),
    /核心发布门禁未通过/
  );
});

test("股票行情不可核验时只输出研究线索与明确缺口", () => {
  const context = contextFixture({ missingWatchlistMarket: true });
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  assert.equal(publicationAudit.passed, true);
  assert.equal(publicationAudit.marketCoverage.obtained, 2);

  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  assert.match(report, /行情变化：上一交易日行情无法核验/);
  assert.match(report, /逻辑状态：研究线索，待验证/);
  assert.doesNotMatch(report, /收盘 12\.3 元/);
  assert.doesNotThrow(
    () => normalizeReportOutput(report, normalizationOptions(context, publicationAudit))
  );
});

test("旧式自由拼接覆盖层即使格式正确也不能越过程序语义候选门", () => {
  const context = contextFixture();
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit, {
    analysisOverlay: {
      schemaVersion: "1.0",
      summary: {
        text: "市场线索可能分化，仍需观察反证",
        evidenceIds: ["sector"],
        counterEvidenceIds: ["market"]
      },
      themes: [{
        title: "待验证主题",
        text: "若行业线索延续，仍需观察反向风险",
        evidenceIds: ["sector"],
        counterEvidenceIds: ["market"]
      }],
      stocks: [{
        code: "000001",
        text: "公司线索可能变化，财务风险仍待核验",
        evidenceIds: ["financial"],
        counterEvidenceIds: ["market"]
      }],
      scenarios: [],
      risks: []
    }
  }));
  assert.match(report, /\[推断\] 摘要：市场线索可能分化，仍需观察反证/);
  assert.match(report, /^> 生成方式：程序核验基础版 \+ 结构化分析覆盖层$/m);
  assert.match(report, /\[推断\] 主题 待验证主题：若行业线索延续，仍需观察反向风险/);
  assert.match(report, /\[推断\] 股票 000001：公司线索可能变化，财务风险仍待核验/);
  assert.match(report, /支持证据：\[来源 \d+\]/);
  assert.match(report, /反证：\[来源 \d+\]/);
  assert.throws(
    () => normalizeReportOutput(report, normalizationOptions(context, publicationAudit)),
    /程序证明的语义候选白名单/
  );
  for (const replacement of [
    "> 生成方式：人工审校稿",
    "> 生成方式：程序核验基础版",
    ""
  ]) {
    const disguised = report.replace(
      "> 生成方式：程序核验基础版 + 结构化分析覆盖层",
      replacement
    );
    assert.throws(
      () => normalizeReportOutput(disguised, normalizationOptions(context, publicationAudit)),
      /含结构化推断或证据行的报告必须使用结构化覆盖层生成方式并通过语义白名单/,
      "手工、基础版或缺失生成方式声明都不能绕过结构化语义白名单"
    );
  }

  const boldManual = report
    .replace(
      "> 生成方式：程序核验基础版 + 结构化分析覆盖层",
      "> 生成方式：人工审校稿"
    )
    .replaceAll("- [推断]", "- **[推断]**")
    .replaceAll("  - 支持证据：", "  - **支持证据：**")
    .replaceAll("  - 反证：", "  - **反证：**");
  assert.throws(
    () => normalizeReportOutput(boldManual, normalizationOptions(context, publicationAudit)),
    /含结构化推断或证据行的报告必须使用结构化覆盖层生成方式并通过语义白名单/,
    "加粗保留标签不得绕过结构化语义白名单"
  );

  const inferenceOutsideSection = report.replace(
    "## 6. 结构化分析覆盖层",
    "- [推断] 摘要：市场线索可能分化，仍需观察反证\n## 6. 结构化分析覆盖层"
  );
  assert.throws(
    () => normalizeReportOutput(
      inferenceOutsideSection,
      normalizationOptions(context, publicationAudit)
    ),
    /只能出现在固定的第 6 节/
  );

  assert.throws(
    () => buildVerifiedBaselineReport(buildOptions(context, publicationAudit, {
      analysisOverlay: [{
        text: "上涨概率为百分之八十",
        evidenceIds: ["sector"],
        counterEvidenceIds: [],
        markdown: "## 注入"
      }]
    })),
    /未允许字段/
  );
  assert.throws(
    () => buildVerifiedBaselineReport(buildOptions(context, publicationAudit, {
      analysisOverlay: [{
        text: "明日上涨 80%",
        evidenceIds: ["sector"],
        counterEvidenceIds: []
      }]
    })),
    /含数字/
  );
  assert.throws(
    () => buildVerifiedBaselineReport(buildOptions(context, publicationAudit, {
      analysisOverlay: [{
        text: "若上证指数上涨壹成，仍需观察风险",
        evidenceIds: ["sector"],
        counterEvidenceIds: []
      }]
    })),
    /含数字/
  );
  assert.throws(
    () => buildVerifiedBaselineReport(buildOptions(context, publicationAudit, {
      analysisOverlay: [{
        text: "市场线索可能分化，仍需观察反证",
        evidenceIds: ["missing-source"],
        counterEvidenceIds: []
      }]
    })),
    /未知来源 id/
  );
});

test("最终报告门只接受与程序候选完全相同的对象、命题、周期和来源集合", () => {
  const context = semanticCandidateContext();
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const [candidate] = buildStructuredAnalysisCandidates(context);
  assert.ok(candidate);
  const overlay = parseStructuredAnalysisOutput(JSON.stringify({
    schemaVersion: "2.0",
    candidateIds: [candidate.id]
  }), { context });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit, {
    analysisOverlay: overlay
  }));
  assert.doesNotThrow(
    () => normalizeReportOutput(report, normalizationOptions(context, publicationAudit))
  );

  const sectorLine = report.slice(report.lastIndexOf("## 来源")).split("\n")
    .find((line) => line.includes("行业板块行情数据"));
  const sectorNumber = Number(sectorLine?.match(/^(\d+)\./)?.[1]);
  const tampered = report.replace(
    /支持证据：\[来源 \d+\]\[陈旧\]/,
    `支持证据：[来源 ${sectorNumber}][陈旧]`
  );
  assert.throws(
    () => normalizeReportOutput(tampered, normalizationOptions(context, publicationAudit)),
    /程序证明的语义候选白名单/
  );
});

test("旧结构化来源仍逐项复核时效，但最终必须属于程序语义候选", () => {
  const context = contextFixture();
  context.sources.push(
    source("old-media", "超过七十二小时的公司报道", "2026-08-06T07:00:00+08:00", {
      sourceType: "media_aggregation",
      publishedAt: "2026-08-06T07:00:00+08:00",
      aggregator: "测试聚合平台",
      originalPublisher: "测试原媒体"
    }),
    source("fresh-media", "截止前一小时的公司报道", "2026-08-10T07:00:00+08:00", {
      sourceType: "media_aggregation",
      publishedAt: "2026-08-10T07:00:00+08:00",
      aggregator: "测试聚合平台",
      originalPublisher: "测试原媒体"
    })
  );
  context.data.announcementsAndNews.push({
    code: "000001",
    name: "测试银行",
    title: "超过七十二小时的公司报道",
    publishedAt: "2026-08-06T07:00:00+08:00",
    sourceIds: ["old-media"]
  });
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit, {
    analysisOverlay: {
      schemaVersion: "1.0",
      summary: {
        text: "现有证据方向冲突，仍需继续核验",
        evidenceIds: ["old-media"],
        counterEvidenceIds: ["fresh-media"]
      },
      themes: [],
      stocks: [{
        code: "000001",
        text: "公司线索可能变化，财务风险仍待核验",
        evidenceIds: ["financial"],
        counterEvidenceIds: ["old-media"]
      }],
      scenarios: [],
      risks: [{
        text: "数据时效可能影响判断，仍需继续核验",
        evidenceIds: ["fresh-media"],
        counterEvidenceIds: []
      }]
    }
  }));
  const sourceLines = report.slice(report.lastIndexOf("## 来源")).split("\n");
  const oldMediaLine = sourceLines.find((line) => line.includes("超过七十二小时的公司报道"));
  const freshMediaLine = sourceLines.find((line) => line.includes("截止前一小时的公司报道"));
  const financialLine = sourceLines.find((line) => line.includes("季度财务数据"));
  const marketLine = sourceLines.find((line) => line.includes("上一交易日行情数据"));
  const oldMediaNumber = Number(oldMediaLine.match(/^(\d+)\./)?.[1]);
  const freshMediaNumber = Number(freshMediaLine.match(/^(\d+)\./)?.[1]);

  assert.match(report, /^> 陈旧证据：超过适用时效阈值的来源已在引用处及来源清单逐项标记 \[陈旧\]$/m);
  assert.match(report, new RegExp(`支持证据：\\[来源 ${oldMediaNumber}\\]\\[陈旧\\]`));
  assert.match(report, new RegExp(`反证：\\[来源 ${freshMediaNumber}\\](?!\\[陈旧\\])`));
  assert.match(oldMediaLine, /^\d+\. \[陈旧\] /);
  assert.doesNotMatch(freshMediaLine, /\[陈旧\]/);
  assert.doesNotMatch(financialLine, /\[陈旧\]/, "财务报告来源不套用行情或新闻阈值");
  assert.match(marketLine, /^\d+\. \[陈旧\] /, "周五行情在周一截止时按 18 小时阈值标记");
  assert.throws(
    () => normalizeReportOutput(report, normalizationOptions(context, publicationAudit)),
    /程序证明的语义候选白名单/
  );
  const normalized = report;
  assert.doesNotMatch(
    normalized,
    /\[陈旧\]\s+\[推断\]\s+股票 000001/,
    "推断父行不得因公司旧闻启发式命中而整体污染"
  );

  const missingEvidenceLabel = report.replace(
    `[来源 ${oldMediaNumber}][陈旧]`,
    `[来源 ${oldMediaNumber}]`
  );
  assert.throws(
    () => normalizeReportOutput(missingEvidenceLabel, normalizationOptions(context, publicationAudit)),
    /结构化分析的超时来源未紧邻标记陈旧/
  );

  const missingEvidenceSource = report.replace(
    `支持证据：[来源 ${oldMediaNumber}][陈旧]`,
    "支持证据：待核验"
  );
  assert.throws(
    () => normalizeReportOutput(missingEvidenceSource, normalizationOptions(context, publicationAudit)),
    /每条支持或反证必须使用固定单来源格式/
  );

  const duplicateEvidenceSource = report.replace(
    `支持证据：[来源 ${oldMediaNumber}][陈旧]`,
    `支持证据：[来源 ${oldMediaNumber}][陈旧]、[来源 ${freshMediaNumber}]`
  );
  assert.throws(
    () => normalizeReportOutput(duplicateEvidenceSource, normalizationOptions(context, publicationAudit)),
    /每条支持或反证必须使用固定单来源格式/
  );

  const separatedStaleLabel = report.replace(
    `[来源 ${freshMediaNumber}]`,
    `[来源 ${freshMediaNumber}] [陈旧]`
  );
  assert.throws(
    () => normalizeReportOutput(separatedStaleLabel, normalizationOptions(context, publicationAudit)),
    /固定单来源格式/
  );

  const missingAppendixLabel = report.replace(
    oldMediaLine,
    oldMediaLine.replace("[陈旧] ", "")
  );
  assert.throws(
    () => normalizeReportOutput(missingAppendixLabel, normalizationOptions(context, publicationAudit)),
    /来源清单项未标记陈旧/
  );

  const falseFreshLabel = report.replace(
    `[来源 ${freshMediaNumber}]`,
    `[来源 ${freshMediaNumber}][陈旧]`
  );
  assert.throws(
    () => normalizeReportOutput(falseFreshLabel, normalizationOptions(context, publicationAudit)),
    /结构化分析的新鲜来源不得标记陈旧/
  );

  const falseFinancialLabel = report.replace(
    financialLine,
    financialLine.replace(/^(\d+\.)\s+/, "$1 [陈旧] ")
  );
  assert.throws(
    () => normalizeReportOutput(falseFinancialLabel, normalizationOptions(context, publicationAudit)),
    /不适用时效阈值的来源清单项不得标记陈旧/
  );

  const staleLabelAtWrongPosition = report.replace(
    freshMediaLine,
    `${freshMediaLine} [陈旧]`
  );
  assert.throws(
    () => normalizeReportOutput(staleLabelAtWrongPosition, normalizationOptions(context, publicationAudit)),
    /未超过适用时效阈值的来源清单项不得标记陈旧/
  );

  const forgedSourceIdentity = report.replace(
    oldMediaLine,
    oldMediaLine.replace("media_aggregation", "official_announcement")
  );
  assert.throws(
    () => normalizeReportOutput(forgedSourceIdentity, normalizationOptions(context, publicationAudit)),
    /类型与发布方必须与固定数据包一致/
  );

  const twoUrlsInOneSource = report.replace(
    oldMediaLine,
    `${oldMediaLine} ${freshMediaLine.match(/https?:\/\/\S+$/)?.[0]}`
  );
  assert.throws(
    () => normalizeReportOutput(twoUrlsInOneSource, normalizationOptions(context, publicationAudit)),
    /每条来源只能包含一个直接 URL/
  );

  const pollutedParent = report.replace(
    "- [推断] 股票 000001：",
    "- [陈旧] [推断] 股票 000001："
  );
  assert.throws(
    () => normalizeReportOutput(pollutedParent, normalizationOptions(context, publicationAudit)),
    /推断父行不得包含来源或陈旧标记|推断父行未使用固定格式|包含不属于任何推断的证据行/
  );

  const forgedTradingAdvice = report.replace(
    "公司线索可能变化，财务风险仍待核验",
    "建议买入并加仓"
  );
  assert.throws(
    () => normalizeReportOutput(forgedTradingAdvice, normalizationOptions(context, publicationAudit)),
    /推断父行必须逐字使用程序固定模板/
  );

  const stockBlock = report.match(
    /- \[推断\] 股票 000001：[\s\S]*?(?=\n- \[推断\]|\n## 7\.)/
  )?.[0];
  const wrongStockEvidence = report.replace(
    stockBlock,
    stockBlock.replace(`[来源 ${oldMediaNumber}][陈旧]`, `[来源 ${freshMediaNumber}]`)
  );
  assert.throws(
    () => normalizeReportOutput(wrongStockEvidence, normalizationOptions(context, publicationAudit)),
    /引用了不属于该股票的来源/
  );

  const summaryBlock = report.match(
    /- \[推断\] 摘要：[\s\S]*?(?=\n- \[推断\]|\n## 7\.)/
  )?.[0];
  const overlappingEvidence = report.replace(
    summaryBlock,
    summaryBlock.replace(`[来源 ${freshMediaNumber}]`, `[来源 ${oldMediaNumber}][陈旧]`)
  );
  assert.throws(
    () => normalizeReportOutput(overlappingEvidence, normalizationOptions(context, publicationAudit)),
    /同一来源不得同时作为支持与反证/
  );

  const duplicatedSummary = report.replace(summaryBlock, `${summaryBlock}\n${summaryBlock}`);
  assert.throws(
    () => normalizeReportOutput(duplicatedSummary, normalizationOptions(context, publicationAudit)),
    /摘要推断超过程序固定上限/
  );

  const renamedStructuredSection = report.replace(
    "## 6. 结构化分析覆盖层",
    "## 6. 分析覆盖层"
  );
  assert.throws(
    () => normalizeReportOutput(renamedStructuredSection, normalizationOptions(context, publicationAudit)),
    /第 6、7 节必须各唯一且使用固定标题顺序/
  );

  const missingCounterLine = report.replace(
    `  - 反证：[来源 ${freshMediaNumber}]\n`,
    ""
  );
  assert.throws(
    () => normalizeReportOutput(missingCounterLine, normalizationOptions(context, publicationAudit)),
    /必须至少有一条反证/
  );

  const missingRiskPlaceholder = report.replace(
    "  - 反证：未提供结构化反证来源，不升级为事实结论。\n",
    ""
  );
  assert.throws(
    () => normalizeReportOutput(missingRiskPlaceholder, normalizationOptions(context, publicationAudit)),
    /风险推断必须有反证或固定无反证占位/
  );

  const duplicatedSection7 = report.replace(
    "## 7. 风险提示与数据缺口",
    "## 7. 提前截断\n\n## 7. 风险提示与数据缺口"
  );
  assert.throws(
    () => normalizeReportOutput(duplicatedSection7, normalizationOptions(context, publicationAudit)),
    /第 6、7 节必须各唯一且使用固定标题顺序/
  );

  const ambiguousContext = structuredClone(context);
  const originalOldMedia = ambiguousContext.sources.find((item) => item.id === "old-media");
  ambiguousContext.sources.push({ ...originalOldMedia, id: "old-media-duplicate" });
  assert.throws(
    () => normalizeReportOutput(
      report,
      normalizationOptions(ambiguousContext, publicationAudit)
    ),
    /无法唯一映射到固定数据包来源/
  );

  const sourceStart = report.lastIndexOf("## 来源");
  const structuredWithoutUrls = report.slice(0, sourceStart) + report
    .slice(sourceStart)
    .replace(/https?:\/\/\S+$/gm, "");
  assert.throws(
    () => normalizeReportOutput(structuredWithoutUrls, normalizationOptions(context, publicationAudit)),
    /每条编号来源都必须包含直接 URL/
  );

  const baselineOnly = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  const baselineSourceStart = baselineOnly.lastIndexOf("## 来源");
  const legacyWithoutUrls = baselineOnly.slice(0, baselineSourceStart) + baselineOnly
    .slice(baselineSourceStart)
    .replace(/https?:\/\/\S+$/gm, "");
  assert.throws(
    () => normalizeReportOutput(
      legacyWithoutUrls,
      normalizationOptions(context, publicationAudit)
    ),
    /每条编号来源都必须包含直接 URL/,
    "程序基础版也不得用全量数据包静默重排正文来源编号"
  );
});

test("聚合行情只使用字段级来源或旧 schema 的稳定用途 namespace", () => {
  const futureSchema = contextFixture();
  futureSchema.data.previousAShareSession.turnoverSourceIds = ["turnover-sh", "turnover-sz"];
  delete futureSchema.data.previousAShareSession.turnoverByExchange.sourceIds;
  const futureAudit = assessPublicationGate({
    context: futureSchema,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const futureReport = buildVerifiedBaselineReport(buildOptions(futureSchema, futureAudit));
  assert.match(
    futureReport.split("\n").find((line) => line.includes("沪深两市成交额：3000.1")),
    /\[来源 3、4\]$/
  );

  const context = contextFixture();
  delete context.data.previousAShareSession.turnoverByExchange.sourceIds;
  delete context.data.previousAShareSession.marketBreadth.sourceIds;
  const sse = "akshare:sse:daily-market-summary:test";
  const szse = "akshare:szse:daily-market-summary:test";
  const breadth = "akshare:legulegu:market-activity:test";
  context.data.previousAShareSession.sourceIds = [
    "market",
    sse,
    szse,
    breadth,
    "unused"
  ];
  context.sources.push(
    source(sse, "上海证券交易所成交额", "2026-08-07T15:00:00+08:00"),
    source(szse, "深圳证券交易所成交额", "2026-08-07T15:00:00+08:00"),
    source(breadth, "市场宽度收盘统计", "2026-08-07T15:00:00+08:00")
  );
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  const turnoverLine = report.split("\n").find((line) => line.includes("沪深两市成交额：3000.1"));
  const breadthLine = report.split("\n").find((line) => line.includes("市场宽度：上涨 2670"));
  assert.match(turnoverLine, /\[来源 3、4\]$/);
  assert.match(breadthLine, /\[来源 5\]$/);

  const unscoped = contextFixture();
  const previouslyPassedAudit = assessPublicationGate({
    context: unscoped,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  assert.equal(previouslyPassedAudit.passed, true);
  delete unscoped.data.previousAShareSession.turnoverByExchange.sourceIds;
  delete unscoped.data.previousAShareSession.marketBreadth.sourceIds;
  unscoped.data.previousAShareSession.sourceIds = ["market"];
  const unscopedAudit = assessPublicationGate({
    context: unscoped,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  assert.equal(unscopedAudit.passed, false);
  assert.match(unscopedAudit.blockers.join("\n"), /成交额.*行情来源|市场宽度.*行情来源/);
  const unscopedReport = buildVerifiedBaselineReport(buildOptions(unscoped, previouslyPassedAudit));
  assert.match(unscopedReport, /沪深两市成交额：单位或来源不可核验/);
  assert.match(unscopedReport, /市场宽度：家数或来源不可核验/);
  assert.doesNotMatch(unscopedReport, /沪深两市成交额：3000\.1/);
});

test("不可信内联字段和来源 URL 不能注入链接、HTML 或自动链接", () => {
  const cases = [
    ["股票名称 Markdown 链接", (context) => {
      context.data.watchlist[0].name = "[CLICK](//evil)";
      context.data.watchlist[0].marketData.name = "[CLICK](//evil)";
    }],
    ["板块名称 autolink", (context) => {
      context.data.sectors[0].name = "<https://evil>";
    }],
    ["来源标题 Markdown 链接", (context) => {
      context.sources[0].title = "[CLICK](//evil)";
    }],
    ["来源标题 Markdown 强调", (context) => {
      context.sources[0].title = "**伪造强调**";
    }],
    ["publisher autolink", (context) => {
      context.sources[0].publisher = "<https://evil>";
    }],
    ["sourceType Markdown 链接", (context) => {
      context.sources[0].sourceType = "[CLICK](//evil)";
    }],
    ["URL 拼接 Markdown 链接", (context) => {
      context.sources[0].url = "https://example.com/safe) [CLICK](//evil)";
    }],
    ["URL 用户信息", (context) => {
      context.sources[0].url = "https://user:password@example.com/calendar";
    }]
  ];

  for (const [label, mutate] of cases) {
    const context = contextFixture();
    const publicationAudit = assessPublicationGate({
      context,
      cutoffInstant: CUTOFF,
      targetDate: TARGET_DATE
    });
    assert.equal(publicationAudit.passed, true);
    mutate(context);
    assert.throws(
      () => buildVerifiedBaselineReport(buildOptions(context, publicationAudit)),
      /不得包含 Markdown|不安全的 URL 字符|不得包含 URL 用户信息|交易日历缺少截止前可核验来源/,
      label
    );
  }

  for (const [label, unsafeFields] of [
    ["aggregator Markdown 链接", {
      aggregator: "[CLICK](//evil)",
      originalPublisher: "安全原媒体"
    }],
    ["originalPublisher autolink", {
      aggregator: "安全聚合平台",
      originalPublisher: "<https://evil>"
    }]
  ]) {
    const context = contextFixture();
    const publicationAudit = assessPublicationGate({
      context,
      cutoffInstant: CUTOFF,
      targetDate: TARGET_DATE
    });
    context.sources.push({
      id: "media",
      title: "安全媒体标题",
      url: "https://example.com/media",
      sourceType: "media_aggregation",
      publishedAt: "2026-08-10T07:00:00+08:00",
      ...unsafeFields
    });
    const analysisOverlay = {
      schemaVersion: "1.0",
      summary: {
        text: "市场线索可能分化，仍需观察反证",
        evidenceIds: ["media"],
        counterEvidenceIds: ["market"]
      },
      themes: [],
      stocks: [],
      scenarios: [],
      risks: []
    };
    assert.throws(
      () => buildVerifiedBaselineReport(buildOptions(context, publicationAudit, {
        analysisOverlay
      })),
      /不得包含 Markdown|不得包含 Markdown 控制符/,
      label
    );
  }
});

test("股票池父级与 marketData 的代码或名称不一致时拒绝渲染", () => {
  for (const mutate of [
    (context) => { context.data.watchlist[0].marketData.code = "000002"; },
    (context) => { context.data.watchlist[0].marketData.name = "另一家银行"; }
  ]) {
    const context = contextFixture();
    const publicationAudit = assessPublicationGate({
      context,
      cutoffInstant: CUTOFF,
      targetDate: TARGET_DATE
    });
    assert.equal(publicationAudit.passed, true);
    mutate(context);
    assert.throws(
      () => buildVerifiedBaselineReport(buildOptions(context, publicationAudit)),
      /与 marketData 身份不一致/
    );
  }
});

test("基础版写入已核验的隔夜、行情细节、财务数字、板块资金、龙虎榜与媒体线索", () => {
  const context = contextFixture();
  context.data.overnightGlobal = [{
    symbol: ".DJI",
    name: "道琼斯工业指数",
    sessionDate: "2026-08-09",
    close: 10000,
    change: -100,
    changePercent: -1,
    units: {
      close: "index_point",
      change: "index_point",
      changePercent: "percentage_point"
    },
    dataAsOf: "2026-08-10T04:00:00+08:00",
    sourceIds: ["overnight"]
  }];
  context.data.watchlist[0].marketData.open = 12.1;
  context.data.watchlist[0].marketData.high = 12.5;
  context.data.watchlist[0].marketData.low = 12;
  context.data.watchlist[0].marketData.turnover = 591_000_000;
  Object.assign(context.data.watchlist[0].marketData.units, {
    open: "CNY_per_share",
    high: "CNY_per_share",
    low: "CNY_per_share",
    turnover: "CNY"
  });
  context.data.watchlist[0].financials = {
    reportDate: "2026-03-31",
    seasonLabel: "2026一季报",
    revenue: 1_950_000_000,
    revenueYoY: 11.32,
    parentNetProfit: 114_570_923.69,
    parentNetProfitYoY: -23.76,
    grossMargin: 20.49,
    roeWeighted: 1.78,
    eps: 0.0598,
    units: {
      revenue: "CNY",
      revenueYoY: "percentage_point",
      parentNetProfit: "CNY",
      parentNetProfitYoY: "percentage_point",
      grossMargin: "percentage_point",
      roeWeighted: "percentage_point",
      eps: "CNY_per_share"
    },
    dataAsOf: "2026-04-30T23:59:59+08:00",
    sourceIds: ["financial"]
  };
  context.data.sectors[0].netInflow = 29.61;
  context.data.sectors[0].leader = "测试龙头";
  context.data.sectors[0].leaderChangePercent = 11.69;
  context.data.sectors[0].units.netInflow = "CNY_100_million";
  context.data.sectors[0].units.leaderChangePercent = "percentage_point";
  context.data.capitalFlows.push({
    type: "dragon_tiger_list",
    status: "available",
    sessionDate: "2026-08-07",
    entries: [{
      code: "000001",
      name: "测试银行",
      changePercent: 2.5,
      netBuy: 12_000_000,
      units: {
        changePercent: "percentage_point",
        netBuy: "CNY"
      },
      dataAsOf: "2026-08-07T15:00:00+08:00",
      sourceIds: ["dragon"]
    }, {
      code: "000620",
      name: "测试龙虎",
      changePercent: 9.93,
      netBuy: 423_718_411.12,
      units: {
        changePercent: "percentage_point",
        netBuy: "CNY"
      },
      dataAsOf: "2026-08-07T15:00:00+08:00",
      sourceIds: ["dragon"]
    }, {
      code: "301689",
      name: "测试异常龙虎",
      changePercent: 237.5,
      netBuy: 305_000_000,
      units: {
        changePercent: "percentage_point",
        netBuy: "CNY"
      },
      dataAsOf: "2026-08-07T15:00:00+08:00",
      sourceIds: ["dragon"]
    }]
  });
  context.data.announcementsAndNews.push({
    type: "news",
    code: "000001",
    name: "测试银行",
    title: "测试银行出现机构持仓变化报道",
    sourceType: "media_aggregation",
    publishedAt: "2026-08-10T07:00:00+08:00",
    dataAsOf: "2026-08-10T07:00:00+08:00",
    sourceIds: ["fresh-media"]
  });
  context.sources.push(
    source("overnight", "隔夜美股指数", "2026-08-10T04:00:00+08:00"),
    source("dragon", "龙虎榜统计", "2026-08-07T15:00:00+08:00", {
      sourceType: "dragon_tiger_list_aggregation"
    }),
    source("fresh-media", "截止前一小时的公司报道", "2026-08-10T07:00:00+08:00", {
      sourceType: "media_aggregation",
      publishedAt: "2026-08-10T07:00:00+08:00",
      aggregator: "测试聚合平台",
      originalPublisher: "测试原媒体"
    })
  );
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  assert.match(report, /道琼斯工业指数（2026-08-09）：收盘 10000 点/);
  assert.match(report, /涨跌 -100 点（-1%）/);
  assert.match(report, /程序核验基础版未生成跨市场因果解释；以上仅列示可核验收盘事实。/);
  assert.match(report, /医疗服务：涨跌幅 \+8\.34%.*供应商净流入指标 \+29\.61 亿元.*供应商列首个股 测试龙头 \+11\.69%/);
  assert.match(report, /不等于机构账户流水，也不能证明资金在板块间迁移/);
  assert.match(report, /行情细节：开 12\.1 元\[陈旧\]，高 12\.5 元\[陈旧\]，低 12 元\[陈旧\]，成交额 5\.91 亿元\[陈旧\]/);
  assert.match(report, /财务（2026-03-31，2026一季报）：聚合营收 19\.5 亿元（供应商同比 \+11\.32%），归母净利润 1\.15 亿元（同比 -23\.76%），毛利率 20\.49%，加权 ROE 1\.78%，EPS 0\.0598 元/);
  assert.match(report, /财务口径：营收金额及同比为供应商口径，尚未与公告核对/);
  assert.doesNotMatch(report, /下一核验点：先对照同报告期公告/);
  assert.match(report, /财务比较：缺少已核验的历史比较基准/);
  assert.doesNotMatch(report, /财务较上日无新报告期/);
  assert.doesNotMatch(report, /观察条件：下一次数据采集后重新核验/);
  assert.match(report, /### 龙虎榜（席位统计，非公告）/);
  assert.match(report, /龙虎榜 000001 测试银行：涨跌幅 \+2\.5%\[陈旧\]，净买入额 1200 万元\[陈旧\]/);
  assert.doesNotMatch(report, /测试龙虎|000620/);
  assert.doesNotMatch(report, /测试异常龙虎|237\.5/);
  assert.match(report, /\[媒体线索，非公司公告\] 000001 测试银行：测试银行出现机构持仓变化报道/);
  assert.match(report, /程序核验基础版未把媒体聚合内容写成公司或交易所官方事实。/);
  assert.doesNotThrow(
    () => normalizeReportOutput(report, normalizationOptions(context, publicationAudit))
  );
});

test("周一密封时隔夜按最近完成美股交易日，正文与来源使用同一规则", () => {
  const context = contextFixture();
  context.data.overnightGlobal = [{
    symbol: ".INX",
    name: "标普500指数",
    sessionDate: "2026-08-07",
    close: 10100,
    change: 100,
    changePercent: 1,
    units: {
      close: "index_point",
      change: "index_point",
      changePercent: "percentage_point"
    },
    dataAsOf: "2026-08-08T04:00:00+08:00",
    sourceIds: ["overnight"]
  }];
  context.sources.push(source("overnight", "隔夜美股指数", "2026-08-08T04:00:00+08:00", {
    marketCalendar: "us_equity_regular_v1", marketSymbol: ".INX", sessionDate: "2026-08-07"
  }));
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  assert.match(report, /标普500指数（2026-08-07）：收盘 10100 点，涨跌 \+100 点（\+1%）。/);
  assert.doesNotMatch(report, /10100 点\[陈旧\]/);
  const normalized = normalizeReportOutput(report, normalizationOptions(context, publicationAudit));
  assert.doesNotMatch(normalized, /10100 点\[陈旧\]|\+100 点\[陈旧\]/);
  assert.match(normalized, /标普500指数（2026-08-07）：收盘 10100 点，涨跌 \+100 点（\+1%）。/);
});

test("隔夜与媒体在缺字段或过期时不编造，并保留原占位说明", () => {
  const context = contextFixture();
  context.data.overnightGlobal = [{
    symbol: ".DJI",
    name: "道琼斯工业指数",
    sessionDate: "2026-08-07",
    close: 10000,
    change: -50,
    changePercent: -1,
    units: {
      close: "index_point",
      change: "index_point",
      changePercent: "percentage_point"
    },
    dataAsOf: "2026-08-08T05:00:00+08:00",
    sourceIds: ["overnight"]
  }];
  context.data.announcementsAndNews.push({
    type: "news",
    title: "过期媒体标题",
    sourceType: "media_aggregation",
    publishedAt: "2026-08-06T07:00:00+08:00",
    dataAsOf: "2026-08-06T07:00:00+08:00",
    sourceIds: ["old-media"]
  });
  context.sources.push(
    source("overnight", "隔夜美股指数", "2026-08-08T05:00:00+08:00"),
    source("old-media", "超过七十二小时的公司报道", "2026-08-06T07:00:00+08:00", {
      sourceType: "media_aggregation",
      publishedAt: "2026-08-06T07:00:00+08:00",
      aggregator: "测试聚合平台",
      originalPublisher: "测试原媒体"
    })
  );
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  assert.match(report, /程序核验基础版未生成跨市场因果解释；相关数据留待分析层处理。/);
  assert.doesNotMatch(report, /道琼斯工业指数/);
  assert.doesNotMatch(report, /过期媒体标题/);
  assert.match(report, /程序核验基础版未把媒体聚合内容写成公司或交易所官方事实。/);
});

test("媒体只保留股票池代码且二十四小时内的标题", () => {
  const context = contextFixture();
  context.data.announcementsAndNews.push(
    {
      type: "news",
      code: "000001",
      name: "测试银行",
      title: "测试银行股票池时效内报道",
      sourceType: "media_aggregation",
      publishedAt: "2026-08-09T09:00:00+08:00",
      dataAsOf: "2026-08-09T09:00:00+08:00",
      sourceIds: ["fresh-media"]
    },
    {
      type: "news",
      code: "000001",
      name: "测试银行",
      title: "超过二十四小时的股票池报道",
      sourceType: "media_aggregation",
      publishedAt: "2026-08-08T08:00:00+08:00",
      dataAsOf: "2026-08-08T08:00:00+08:00",
      sourceIds: ["day-old-media"]
    },
    {
      type: "news",
      title: "无股票代码的行业资金流向日报",
      sourceType: "media_aggregation",
      publishedAt: "2026-08-10T07:00:00+08:00",
      dataAsOf: "2026-08-10T07:00:00+08:00",
      sourceIds: ["industry-media"]
    }
  );
  context.sources.push(
    source("fresh-media", "测试银行股票池时效内报道", "2026-08-09T09:00:00+08:00", {
      sourceType: "media_aggregation",
      publishedAt: "2026-08-09T09:00:00+08:00",
      aggregator: "测试聚合平台",
      originalPublisher: "测试原媒体"
    }),
    source("day-old-media", "超过二十四小时的股票池报道", "2026-08-08T08:00:00+08:00", {
      sourceType: "media_aggregation",
      publishedAt: "2026-08-08T08:00:00+08:00",
      aggregator: "测试聚合平台",
      originalPublisher: "测试原媒体"
    }),
    source("industry-media", "无股票代码的行业资金流向日报", "2026-08-10T07:00:00+08:00", {
      sourceType: "media_aggregation",
      publishedAt: "2026-08-10T07:00:00+08:00",
      aggregator: "测试聚合平台",
      originalPublisher: "测试原媒体"
    })
  );
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  assert.match(report, /测试银行股票池时效内报道/);
  assert.doesNotMatch(report, /超过二十四小时的股票池报道/);
  assert.doesNotMatch(report, /无股票代码的行业资金流向日报/);
  assert.doesNotThrow(
    () => normalizeReportOutput(report, normalizationOptions(context, publicationAudit))
  );
});

test("巨潮公告与媒体分栏，且不把媒体写成公告", () => {
  const context = contextFixture();
  context.data.watchlist[0].monitor = ["重大公告与监管信息", "量价及流动性异常"];
  context.data.watchlist[0].officialDisclosure = {
    status: "available",
    latestDisclosureDate: "2026-08-07",
    queriedAt: "2026-08-10T08:00:00+08:00",
    sourceIds: ["cninfo-query"],
    recentBackground: [{
      type: "official_announcement",
      sourceType: "official_announcement",
      code: "000001",
      name: "测试银行",
      title: "关于2025年年度报告的公告",
      disclosureDate: "2026-04-01",
      publishedAt: "2026-04-01T00:00:00+08:00",
      dataAsOf: "2026-08-10T08:00:00+08:00",
      sourceIds: ["cninfo-background"]
    }]
  };
  context.data.watchlist[0].sourceIds = ["market", "cninfo-query"];
  context.data.announcementsAndNews.push(
    {
      type: "official_announcement",
      sourceType: "official_announcement",
      code: "000001",
      name: "测试银行",
      title: "关于召开董事会的公告",
      disclosureDate: "2026-08-07",
      publishedAt: "2026-08-07T00:00:00+08:00",
      dataAsOf: "2026-08-10T08:00:00+08:00",
      sourceIds: ["cninfo-filing"]
    },
    {
      type: "news",
      code: "000001",
      name: "测试银行",
      title: "测试银行出现机构持仓变化报道",
      sourceType: "media_aggregation",
      publishedAt: "2026-08-10T07:00:00+08:00",
      dataAsOf: "2026-08-10T07:00:00+08:00",
      sourceIds: ["fresh-media"]
    },
    {
      type: "news",
      code: "000001",
      name: "测试银行",
      title: "行业资金流入榜不是公司公告",
      sourceType: "media_aggregation",
      publishedAt: "2026-08-10T07:10:00+08:00",
      dataAsOf: "2026-08-10T07:10:00+08:00",
      sourceIds: ["industry-media"]
    }
  );
  context.sources.push(
    source("cninfo-query", "巨潮资讯测试银行信息披露查询", "2026-08-10T08:00:00+08:00", {
      sourceType: "official_disclosure_query",
      publisher: "巨潮资讯网"
    }),
    source("cninfo-filing", "关于召开董事会的公告", "2026-08-10T08:00:00+08:00", {
      sourceType: "official_announcement",
      publisher: "巨潮资讯网",
      publishedAt: "2026-08-07T00:00:00+08:00"
    }),
    source("cninfo-background", "关于2025年年度报告的公告", "2026-08-10T08:00:00+08:00", {
      sourceType: "official_announcement",
      publisher: "巨潮资讯网",
      publishedAt: "2026-04-01T00:00:00+08:00"
    }),
    source("fresh-media", "测试银行出现机构持仓变化报道", "2026-08-10T07:00:00+08:00", {
      sourceType: "media_aggregation",
      publishedAt: "2026-08-10T07:00:00+08:00",
      aggregator: "测试聚合平台",
      originalPublisher: "测试原媒体"
    }),
    source("industry-media", "行业资金流入榜不是公司公告", "2026-08-10T07:10:00+08:00", {
      sourceType: "media_aggregation",
      publishedAt: "2026-08-10T07:10:00+08:00",
      aggregator: "测试聚合平台",
      originalPublisher: "测试原媒体"
    })
  );
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  assert.match(report, /新增披露 000001 测试银行（披露 2026-08-07）：关于召开董事会的公告/);
  assert.match(report, /巨潮公告 000001 测试银行（披露 2026-08-07）：关于召开董事会的公告/);
  assert.match(report, /近期背景（非今日新增） 000001 测试银行（披露 2026-04-01）：关于2025年年度报告的公告/);
  assert.match(report, /\[媒体线索，非公司公告\] 000001 测试银行：测试银行出现机构持仓变化报道/);
  assert.doesNotMatch(report, /行业资金流入榜/);
  assert.doesNotMatch(report, /未观测/);
  assert.doesNotMatch(report, /官方源缺失/);
  assert.match(report, /巨潮查询：完成，窗口内有披露条目/);
  assert.doesNotMatch(report, /重大公告与监管信息：已观测/);
  assert.doesNotMatch(report, /已观测/);
  assert.match(report, /研究可用项：.*公告官方源有/);
  assert.doesNotMatch(report, /公司官方公告源未提供/);
  assert.doesNotThrow(
    () => normalizeReportOutput(report, normalizationOptions(context, publicationAudit))
  );
});

test("巨潮已查询但窗口内无新公告时只写无新公告", () => {
  const context = contextFixture();
  context.meta.warnings = ["stock_news_em 提供媒体新闻；官方公告数据当前不可用。"];
  context.data.watchlist[0].monitor = ["重大公告与监管信息"];
  context.data.watchlist[0].officialDisclosure = {
    status: "available",
    latestDisclosureDate: "2026-07-01",
    queriedAt: "2026-08-10T08:00:00+08:00",
    sourceIds: ["cninfo-query"]
  };
  context.data.watchlist[0].sourceIds = ["market", "cninfo-query"];
  context.sources.push(source("cninfo-query", "巨潮资讯测试银行信息披露查询", "2026-08-10T08:00:00+08:00", {
    sourceType: "official_disclosure_query",
    publisher: "巨潮资讯网"
  }));
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  assert.match(report, /000001 测试银行：当前数据包中无可引用的窗口内公告条目，仅代表本次有限查询范围/);
  assert.match(report, /巨潮查询：完成，仅代表有限查询结果/);
  assert.doesNotMatch(report, /已观测/);
  assert.doesNotMatch(report, /公司官方公告源未提供/);
  assert.doesNotThrow(
    () => normalizeReportOutput(report, normalizationOptions(context, publicationAudit))
  );
});

test("报告期未变时仍展示关键财务数字并标明与封存快报一致", () => {
  const context = contextFixture();
  context.data.watchlist[0].financials = {
    ...context.data.watchlist[0].financials,
    reportDate: "2026-03-31",
    seasonLabel: "2026一季报",
    revenue: 1_950_000_000,
    parentNetProfit: 114_570_923.69,
    units: {
      ...(context.data.watchlist[0].financials.units ?? {}),
      revenue: "CNY",
      parentNetProfit: "CNY"
    }
  };
  const tagged = attachPreviousFinancialPeriods(context, fixtureFinancialBaseline(context));
  const publicationAudit = assessPublicationGate({
    context: tagged,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(tagged, publicationAudit));
  assert.match(report, /财务（2026-03-31，2026一季报）：聚合营收 19\.5 亿元，归母净利润 1\.15 亿元。与已封存快报（2026-08-07）一致/);
  assert.doesNotMatch(report, /财务比较：缺少已核验的历史比较基准/);
  assert.match(report, /研究可用项：.*财务数字已写入/);
  assert.doesNotThrow(
    () => normalizeReportOutput(report, normalizationOptions(tagged, publicationAudit))
  );
});

test("一句话结论写实际涨跌幅，不声明涨停，查询限制不占首屏", () => {
  const context = contextFixture();
  context.data.watchlist[0].marketData = {
    ...context.data.watchlist[0].marketData,
    last: 13.2,
    previousClose: 12,
    change: 1.2,
    changePercent: 10,
    high: 13.2,
    low: 12,
    units: {
      ...context.data.watchlist[0].marketData.units,
      high: "CNY_per_share",
      low: "CNY_per_share"
    }
  };
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  const digest = report.split("## 1. 数据覆盖与可用性")[0];
  assert.match(digest, /000001 测试银行：涨跌幅 \+10%\[陈旧\]（未核验为涨跌停）。/);
  assert.doesNotMatch(digest, /触及涨停|触及跌停/);
  assert.doesNotMatch(digest, /股票池官方公告查询未完成或结果不适用于截止/);
  assert.match(report, /官方公告查询未完成或不适用于截止，不判断窗口内有无新增披露/);
  assert.doesNotThrow(
    () => normalizeReportOutput(report, normalizationOptions(context, publicationAudit))
  );
});

test("查询在截止前完成时写明覆盖时刻，且不再声称窗口不可判", () => {
  const context = contextFixture();
  context.data.watchlist[0].officialDisclosure = {
    status: "available",
    latestDisclosureDate: "2026-07-01",
    queriedAt: "2026-08-10T07:50:00+08:00",
    sourceIds: ["cninfo-query"]
  };
  context.data.watchlist[0].sourceIds = ["market", "cninfo-query"];
  context.sources.push(source("cninfo-query", "巨潮资讯测试银行信息披露查询", "2026-08-10T07:50:00+08:00", {
    sourceType: "official_disclosure_query",
    publisher: "巨潮资讯网"
  }));
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  assert.match(report, /官方查询：完成于 2026-08-10 07:50（Asia\/Shanghai），仅有限查询，未证明窗口查全；数据截止 2026-08-10 08:00 不变。/);
  assert.doesNotMatch(report, /官方公告查询未完成或不适用于截止/);
  const normalized = normalizeReportOutput(report, normalizationOptions(context, publicationAudit));
  assert.match(normalized, /官方查询：完成于 2026-08-10 07:50/);
});

test("媒体标题必须含股票名称或代码且排除全市场稿", () => {
  const context = contextFixture();
  context.data.announcementsAndNews.push(
    {
      type: "news",
      code: "000001",
      name: "测试银行",
      title: "百元股数量达205只",
      sourceType: "media_aggregation",
      publishedAt: "2026-08-10T07:00:00+08:00",
      dataAsOf: "2026-08-10T07:00:00+08:00",
      sourceIds: ["wide-media"]
    },
    {
      type: "news",
      code: "000001",
      name: "测试银行",
      title: "无身份的盘后评论",
      sourceType: "media_aggregation",
      publishedAt: "2026-08-10T07:00:00+08:00",
      dataAsOf: "2026-08-10T07:00:00+08:00",
      sourceIds: ["anon-media"]
    }
  );
  context.sources.push(
    source("wide-media", "百元股数量达205只", "2026-08-10T07:00:00+08:00", {
      sourceType: "media_aggregation",
      publishedAt: "2026-08-10T07:00:00+08:00",
      aggregator: "测试聚合平台",
      originalPublisher: "测试原媒体"
    }),
    source("anon-media", "无身份的盘后评论", "2026-08-10T07:00:00+08:00", {
      sourceType: "media_aggregation",
      publishedAt: "2026-08-10T07:00:00+08:00",
      aggregator: "测试聚合平台",
      originalPublisher: "测试原媒体"
    })
  );
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  assert.doesNotMatch(report, /百元股/);
  assert.doesNotMatch(report, /无身份的盘后评论/);
  assert.doesNotThrow(
    () => normalizeReportOutput(report, normalizationOptions(context, publicationAudit))
  );
});

test("监控只写已提供字段，不把现金流或查询写成整项已观测", () => {
  const context = contextFixture();
  context.data.watchlist[0].monitor = [
    "市场成交活跃度",
    "毛利率和现金流",
    "盈利质量与现金流",
    "重大公告与监管信息"
  ];
  context.data.watchlist[0].financials.grossMargin = 20.49;
  context.data.watchlist[0].financials.units = {
    ...(context.data.watchlist[0].financials.units ?? {}),
    parentNetProfit: "CNY",
    grossMargin: "percentage_point"
  };
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  assert.match(report, /量价：上一交易日行情已提供/);
  assert.match(report, /毛利率：数据包已提供/);
  assert.match(report, /归母净利润：数据包已提供/);
  assert.match(report, /现金流：数据包未提供现金流字段，不作判断/);
  assert.match(report, /官方源缺失/);
  assert.doesNotMatch(report, /已观测/);
  assert.doesNotMatch(report, /毛利率和现金流/);
  assert.doesNotMatch(report, /盈利质量与现金流/);
  assert.doesNotMatch(report, /重大公告与监管信息/);
  assert.doesNotThrow(
    () => normalizeReportOutput(report, normalizationOptions(context, publicationAudit))
  );
});

test("财务比较只接受早于当日的真实留档", () => {
  assert.deepEqual(
    previousFinancialPeriodsFromArchive({
      schemaVersion: "1.0",
      targetDate: "2026-08-09",
      periods: { "000001": "2026-03-31", bad: "nope" }
    }, "2026-08-10"),
    {}
  );
  assert.deepEqual(
    previousFinancialPeriodsFromArchive({
      schemaVersion: "1.0",
      targetDate: "2026-08-10",
      periods: { "000001": "2026-03-31" }
    }, "2026-08-10"),
    {}
  );
  assert.deepEqual(
    previousFinancialPeriodsFromArchive({
      periods: { "000001": "2026-03-31" }
    }, "2026-08-10"),
    {}
  );
});

test("截止后的巨潮查询不得倒填成窗口内无披露", () => {
  const context = contextFixture();
  context.data.watchlist[0].officialDisclosure = {
    status: "available",
    latestDisclosureDate: "2026-07-01",
    queriedAt: "2026-08-10T17:08:00+08:00",
    sourceIds: ["cninfo-query"]
  };
  context.data.watchlist[0].sourceIds = ["market", "cninfo-query"];
  context.sources.push(source("cninfo-query", "巨潮资讯测试银行信息披露查询", "2026-08-10T17:08:00+08:00", {
    sourceType: "official_disclosure_query",
    publisher: "巨潮资讯网"
  }));
  const publicationAudit = assessPublicationGate({
    context,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  assert.match(report, /官方查询限制：结果在截止后取得/);
  assert.match(report, /股票池官方公告查询未完成或结果不适用于截止/);
  assert.doesNotMatch(report, /无程序可核验的股票池新增披露或财务更新/);
  assert.doesNotMatch(report, /没有可引用的巨潮披露条目/);
  assert.doesNotMatch(report, /巨潮查询：完成/);
  assert.doesNotMatch(report, /官方源缺失/);
  assert.doesNotMatch(report, /已观测/);
  assert.doesNotThrow(
    () => normalizeReportOutput(report, normalizationOptions(context, publicationAudit))
  );
});

test("仅有报告期的上日留档不能压缩财务数字", () => {
  const context = contextFixture();
  context.data.watchlist[0].financials = {
    ...context.data.watchlist[0].financials,
    reportDate: "2026-03-31",
    seasonLabel: "2026一季报",
    revenue: 1_950_000_000,
    parentNetProfit: 114_570_923.69,
    units: {
      ...(context.data.watchlist[0].financials.units ?? {}),
      revenue: "CNY",
      parentNetProfit: "CNY"
    }
  };
  const tagged = attachPreviousFinancialPeriods(context, { "000001": "2026-03-31" });
  const publicationAudit = assessPublicationGate({
    context: tagged,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(tagged, publicationAudit));
  assert.match(report, /财务比较：缺少已核验的历史比较基准/);
  assert.match(report, /聚合营收 19\.5 亿元/);
  assert.doesNotMatch(report, /财务较上日无新报告期/);
  assert.doesNotThrow(
    () => normalizeReportOutput(report, normalizationOptions(tagged, publicationAudit))
  );
});

test("同报告期字段变化时复读更新后的财务数字", () => {
  const context = contextFixture();
  context.data.watchlist[0].financials = {
    ...context.data.watchlist[0].financials,
    reportDate: "2026-03-31",
    seasonLabel: "2026一季报",
    revenue: 2_000_000_000,
    parentNetProfit: 114_570_923.69,
    units: {
      ...(context.data.watchlist[0].financials.units ?? {}),
      revenue: "CNY",
      parentNetProfit: "CNY"
    }
  };
  const previous = structuredClone(context);
  previous.data.watchlist[0].financials.revenue = 1_950_000_000;
  const tagged = attachPreviousFinancialPeriods(context, fixtureFinancialBaseline(previous));
  const publicationAudit = assessPublicationGate({
    context: tagged,
    cutoffInstant: CUTOFF,
    targetDate: TARGET_DATE
  });
  const report = buildVerifiedBaselineReport(buildOptions(tagged, publicationAudit));
  assert.match(report, /财务同报告期字段较已封存快报（2026-08-07）变化/);
  assert.match(report, /聚合营收 20 亿元/);
  assert.match(report, /同报告期财务字段较已封存基准变化/);
  assert.doesNotThrow(
    () => normalizeReportOutput(report, normalizationOptions(tagged, publicationAudit))
  );
});

test("供应商首列个股为负涨幅时不误称领涨", () => {
  const context = contextFixture();
  Object.assign(context.data.sectors[0], { leader: "测试列首", leaderChangePercent: -1.2, netInflow: -2 });
  Object.assign(context.data.sectors[0].units, { leaderChangePercent: "percentage_point", netInflow: "CNY_100_million" });
  const audit = assessPublicationGate({ context, targetDate: TARGET_DATE, cutoffInstant: CUTOFF });
  const report = buildVerifiedBaselineReport(buildOptions(context, audit));
  assert.match(report, /供应商列首个股 测试列首 -1\.2%/);
  assert.match(report, /供应商净流入指标 -2 亿元/);
  assert.doesNotMatch(report, /领涨 测试列首/);
  assert.equal(normalizeReportOutput(report, normalizationOptions(context, audit)), report);
});

test("聚合营收金额和同比均标识来源口径，保留封包数值并展示可用扣非利润", () => {
  const context = contextFixture();
  const fin = context.data.watchlist[0].financials;
  Object.assign(fin, {
    revenue: 38_624_000_000, revenueYoY: 1.24,
    deductedNetProfit: 90_000_000, deductedNetProfitYoY: -2.5,
    revenueBasis: { provider: "eastmoney", amountField: "TOTALOPERATEREVE", yoyField: "TOTALOPERATEREVETZ", verification: "unverified_against_filing" }
  });
  fin.units = { ...fin.units, revenue: "CNY", revenueYoY: "percentage_point", deductedNetProfit: "CNY", deductedNetProfitYoY: "percentage_point" };
  const audit = assessPublicationGate({ context, targetDate: TARGET_DATE, cutoffInstant: CUTOFF });
  const report = buildVerifiedBaselineReport(buildOptions(context, audit));
  assert.match(report, /聚合营收 386\.24 亿元（供应商同比 \+1\.24%）/);
  assert.match(report, /财务口径：营收金额及同比为供应商口径，尚未与公告核对，不等同于已核验的营业收入/);
  assert.doesNotMatch(report, /下一核验点：先对照同报告期公告/);
  assert.match(report, /扣非归母净利润 9000 万元（同比 -2\.5%）/);
  assert.match(report, /不据此推断经营杠杆/);
  assert.doesNotMatch(report, /营业收入 386\.24|381\.23|\+1\.33%/);
  assert.equal(normalizeReportOutput(report, normalizationOptions(context, audit)), report);
  delete fin.revenueBasis;
  const legacy = buildVerifiedBaselineReport(buildOptions(context, audit));
  assert.match(legacy, /财务口径：营收金额及同比为供应商口径，尚未与公告核对/);
  assert.doesNotMatch(legacy, /TOTALOPERATEREVE/);
});

test("日期精度披露保留实际采集时间，跨截止采集不伪造发布时间", () => {
  const context = contextFixture();
  const item = {
    type: "official_announcement", sourceType: "official_announcement", code: "000001", name: "测试银行",
    title: "关于召开董事会的公告", disclosureDate: "2026-08-07", timePrecision: "date", publishedAt: null,
    observedAt: "2026-08-10T17:08:00+08:00", dataAsOf: "2026-08-10T17:08:00+08:00", sourceIds: ["date-only-doc"]
  };
  context.data.announcementsAndNews.push(item);
  context.sources.push(source("date-only-doc", item.title, item.dataAsOf, {
    sourceType: item.sourceType, publishedAt: null, timePrecision: "date", disclosureDate: item.disclosureDate, observedAt: item.observedAt
  }));
  const audit = assessPublicationGate({ context, targetDate: TARGET_DATE, cutoffInstant: CUTOFF });
  const report = buildVerifiedBaselineReport(buildOptions(context, audit));
  assert.match(report, /披露日期：2026-08-07（仅日期，发布时间未核实）/);
  assert.match(report, /实际采集时间：2026-08-10 17:08/);
  assert.doesNotMatch(report, /发布时间：2026-08-07 00:00/);
  assert.equal(normalizeReportOutput(report, normalizationOptions(context, audit)), report);
  assert.throws(() => normalizeReportOutput(report.replace("实际采集时间：2026-08-10 17:08", "实际采集时间：2026-08-10 22:08"), normalizationOptions(context, audit)), /来源.*时间|时间.*来源/);
});

test("同一份官方 PDF 再次出现时仍展示现金流金额，不隐藏为不重复列数", () => {
  const context = contextFixture();
  const { doc, source } = documentFixture();
  // Align the document identity with the fixture watchlist stock.
  doc.code = "000001";
  doc.name = "测试银行";
  doc.pages[0].text = doc.pages[0].text.replace("002617", "000001").replace("露笑科技", "测试银行");
  doc.pages[0].textSha256 = createHash("sha256").update(doc.pages[0].text).digest("hex");
  // Adjust timing so the document is observable before this fixture's cutoff.
  const observed = "2026-08-09T09:00:00+08:00";
  doc.observedAt = doc.dataAsOf = source.observedAt = source.dataAsOf = observed;
  doc.disclosureDate = "2026-08-05";
  doc.url = "https://static.cninfo.com.cn/finalpage/2026-08-05/0.PDF";
  source.disclosureDate = doc.disclosureDate;
  source.url = doc.url;
  doc.queryRecord.date = doc.disclosureDate;
  doc.queryRecord.url = doc.url;
  doc.queryRecord.observedAt = "2026-08-09T08:59:00+08:00";
  context.data.watchlist[0].officialDisclosure = { financialSummary: doc };
  // 维度对齐（报告期与币种一致），只留金额差异：这正是东方电气那种情形
  context.data.watchlist[0].financials = { reportDate: doc.reportDate, revenue: 2100,
    revenueYoY: 1.24, units: { revenue: "CNY", revenueYoY: "percentage_point" } };
  context.data.watchlist[0].previousOfficialDocument = { url: doc.url, pdfSha256: doc.pdfSha256, targetDate: "2026-08-07" };
  context.sources.push(source);
  const publicationAudit = assessPublicationGate({ context, cutoffInstant: CUTOFF, targetDate: TARGET_DATE });
  const report = buildVerifiedBaselineReport(buildOptions(context, publicationAudit));
  assert.match(report, /官方现金流原文（PDF 第 2 页，合并排版空白）：.*1,000\.00 900\.00/);
  assert.doesNotMatch(report, /不重复列数/);
  assert.match(report, /与已封存证据（2026-08-07）一致/);
  // 逐字段核验：公告原文优先、供应商作对照、维度状态与差异都写出来
  assert.match(report, /官方列示（同份摘要原列）：本报告期 2,000\.00 元，上年同期 1,800\.00 元（同比 11\.11%）/);
  // 维度一致、金额相差：必须是“数值不一致，差异原因待核”，不能写成“口径不同”
  assert.match(report, /对照（供应商口径）：2100 元（同比 1\.24%）；逐字段核验：数值不一致，差异原因待核（报告期一致、合并范围尚未核验、币种一致、单位一致、是否重述尚未核验）/);
  assert.doesNotMatch(report, /逐字段核验：口径不同/, "维度无冲突时不得判成口径不同");
  assert.match(report, /未核验维度：合并范围：半年报摘要未声明合并范围/);
  assert.match(report, /差异：供应商较公告原列 100 元；不自动归并/);
  assert.doesNotMatch(report, /尚未与公告核对/, "有已核验原文时不再写“尚未与公告核对”");
  assert.doesNotThrow(
    () => normalizeReportOutput(report, normalizationOptions(context, publicationAudit))
  );
});
