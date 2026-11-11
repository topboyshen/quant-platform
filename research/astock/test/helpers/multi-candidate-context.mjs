// Synthetic fixtures copied from baseline-report tests; no test registration or I/O.
const TARGET_DATE = "2026-08-10";
const CUTOFF_LOCAL = "2026-08-10 08:00";
const CUTOFF = new Date("2026-08-10T00:00:00Z");
const PUBLICATION_LEVEL = "可发布（结构、来源与时效门禁通过；研究语义未审校）";
const DISCLAIMER = "本报告仅供研究与信息参考，不构成任何投资建议。";

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


export { source, contextFixture, semanticCandidateContext, opposingStockSectorContext,
  buildOptions, normalizationOptions, TARGET_DATE, CUTOFF_LOCAL, CUTOFF,
  PUBLICATION_LEVEL, DISCLAIMER };
