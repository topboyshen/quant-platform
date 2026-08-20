# 统一研究系统架构

**合并后**: 旧一代(晨报/forecast链) + 新研究验证台 → 一个统一系统。

## 五层架构

```
┌─ 治理层 policy/ ──────────────────────────────┐
│  EDGE_POLICY.md + edge-policy.json            │
│  规则: 无先验edge → 主动资本0 → 不制造交易冲动  │
└──────────────────────────────────────────────┘
┌─ 数据层 daily_snapshot.py ───────────────────┐
│  11源(qlib/sina/THS/legu/交易所/mootdx/腾讯/   │
│  baostock/巨潮/财联社) → snapshot.db(SQLite)   │
│  cron 22:30 每日自动                          │
└──────────────────────────────────────────────┘
┌─ 研究层 ─────────────────────────────────────┐
│  review.py      复盘(实时/历史)               │
│  screen.py      选股(涨停×资金流×估值)         │
│  chip.py        筹码分布(本地计算)            │
│  strategies.py  6策略(移植Sequoia-X)          │
└──────────────────────────────────────────────┘
┌─ 验证层 backtest*.py ────────────────────────┐
│  backtest.py          向量化+A股规则(T+1/涨跌停/手续费) │
│  backtest_sweep.py    参数扫描                │
│  backtest_benchmark.py 基准对比(alpha vs beta)│
│  backtest_oos.py      样本外验证              │
│  RESEARCH_FINDINGS.md 结论(公开策略无稳定edge)│
└──────────────────────────────────────────────┘
┌─ 结算层 /opt/research-ledger ────────────────┐
│  PostgreSQL research_ledger (盈亏真相层)      │
│  待命: 将来真edge出现时, 封存+前瞻结算         │
└──────────────────────────────────────────────┘
```

## 数据流

```
22:00 qlib更新 → 22:30 daily_snapshot → snapshot.db
  → review.py(复盘) / screen.py(选股) / chip.py(筹码)
  → strategies.py(6策略) → backtest(回测) → benchmark(基准) → oos(样本外)
  → RESEARCH_FINDINGS(结论) → 治理层裁决(无edge→不交易)
```

## 关键决策

1. **治理层并入**: EDGE_POLICY 从 candidate-engine 并入 research-data/policy/，研究系统受同一政策约束。
2. **配置自包含**: watchlist(config/stocks.json) 从 AStock 移入，解除对旧系统的依赖。
3. **结算层独立**: ledger 保留在 /opt，作为"盈亏真相层"待命——将来真 edge 出现时，用它做封存+前瞻结算。
4. **旧一代冻结**: AStock 晨报(停)、candidate-engine(冻结)、旧 forecast 链(冻结) 不再扩建。

## 系统定位

**一台"研究验证台"**：数据 → 研究 → 回测 → 验证 → 治理裁决，完整闭环。核心结论已固化：**公开策略无稳定 edge，系统回到 EDGE_DISCOVERY 待命，等真正的 edge 来源。**
