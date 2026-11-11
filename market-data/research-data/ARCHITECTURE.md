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
│  先定 Qlib 目标日，再封存当日 AkShare；按同日组装│
│  cron 22:30 每日自动                          │
└──────────────────────────────────────────────┘
┌─ 研究层 ─────────────────────────────────────┐
│  review.py      复盘(实时/历史)               │
│  screen.py      选股(涨停×资金流×估值)         │
│  chip.py        筹码分布(本地计算)            │
│  strategies.py  6策略(移植Sequoia-X)          │
└──────────────────────────────────────────────┘
┌─ 诊断层 backtest*.py ────────────────────────┐
│  signal_rules.py     五规则共享信号实现      │
│  backtest.py         逐笔价格情景，非组合收益 │
│  backtest_sweep.py    自动参数扫描已阻断      │
│  backtest_benchmark.py 旧基准诊断已阻断       │
│  backtest_oos.py      事后切分诊断已阻断      │
│  RESEARCH_FINDINGS.md 结论(公开策略无稳定edge)│
└──────────────────────────────────────────────┘
┌─ 结算层 /opt/research-ledger ────────────────┐
│  PostgreSQL research_ledger (盈亏真相层)      │
│  待命: 将来真edge出现时, 封存+前瞻结算         │
└──────────────────────────────────────────────┘
```

## 数据流

```
22:00 qlib更新
  → 22:30 读取 Qlib 最新完整交易日和既有状态
  → 封存 AkShare 当日市场截面（不可变、按交易日）
  → 仅用同交易日封存截面组装 daily_snapshot
  → snapshot.db（append-only）
  → review.py(复盘) / screen.py(选股) / chip.py(筹码)
  → strategies.py(研究扫描) → backtest(逐笔价格情景)
  → 研究证据审查 → 治理层裁决(未证明edge→不交易)
```

扫描与逐笔诊断的五规则共用 `signal_rules.py`，均量使用信号日前完整二十个观测。
开盘决策不读取入场日最终高低收盘；未到退出日或无有效退出价格的观测保留并单独计数。
输出不包含组合/累计收益或独立同分布置信区间，也不证明成交、策略盈利或实盘准入。
`benchmark`、`oos` 与自动参数扫描入口保留名称但主动报错，已退出上述流程；
可信组合、匹配时点的基准与预先登记的样本外引擎尚未实现。历史研究记录保留，
离线代码回归不构成对这些历史结论的重新认证。

## 运行环境

- Python 3.12，运行环境位于 `.venv/`，不纳入 Git。
- `requirements.txt` 固定本项目直接导入的第三方包版本；安装后必须运行 `python -m pip check`。
- qlib 数据集由独立的 22:00 更新任务维护。22:30 快照任务先确定本地 Qlib
  目标和既有状态，再封存 AkShare 当前市场截面并消费完全同日的封存数据；如果 Qlib
  对应日期尚无封存记录，则返回 `deferred`，不写 SQLite。

## 日期一致性与只追加边界

AkShare 的市场活跃度接口只提供当前页面，没有可靠的历史日期参数；Qlib release
通常晚一个交易日。因此不能在同一次夜间运行中把两者的“最新值”直接拼接。

系统采用一日缓冲的不可变封存层：

1. 每次自然运行先确定 Qlib 目标和已有状态，再按 AkShare 自己声明的
   `统计日期` 封存市场活跃度与行业摘要。
2. Qlib 目标日只能读取文件名、内部日期、payload SHA-256 和整条记录 SHA-256
   都一致的同日封存文件。
3. 同日相同 payload 允许幂等重试；同日不同 payload、权限异常、符号链接、
   字段或哈希异常全部失败关闭，既有封存文件不会被覆盖。
4. 目标封存尚未形成时，退出码为 69、状态为 `deferred`；等待后续自然运行，
   不把后一天数据改名回填，也不修改既有 SQLite 行。
5. `snapshot` 表继续以交易日为主键只追加；旧日期的不同 payload 永远不能覆盖。

为兼容既有分析器，`sourceAsOf.akshareActivity.kind=trade_date` 和原有来源列表保持
不变；`provenance=immutable_stage` 与新增的 `sourceAsOf.akshareStage` 承载封存
哈希、采集窗口和来源列表。当天新封存失败只记录 warning，不会阻断已经验证的
旧快照或已具备同日封存的目标；但这类 warning 必须在夜间验收中处理，因为它可能
造成下一交易日缺少可用封存。

默认封存位置为
`~/.local/state/research-data/akshare-market/YYYY-MM-DD.json`，目录权限必须为
`0700`、文件权限必须为 `0600`。部署这项机制以前缺失的日期保持为空缺，除非
以后取得可验证的权威同日来源并制定独立恢复方案。

## 关键决策

1. **治理层并入**: EDGE_POLICY 从 candidate-engine 并入 research-data/policy/，研究系统受同一政策约束。
2. **配置自包含**: watchlist(config/stocks.json) 从 AStock 移入，解除对旧系统的依赖。
3. **结算层独立**: ledger 保留在 /opt，作为"盈亏真相层"待命——将来真 edge 出现时，用它做封存+前瞻结算。
4. **受限运行边界**: AStock 工作日晨报与离线小红书草稿定时任务继续运行，但通知、自动发布与自动交易均禁用并要求人工批准；candidate-engine 与旧 forecast 链保持冻结。

## 系统定位

**一台"研究验证台"**：数据 → 研究 → 逐笔诊断 → 治理裁决。当前工具尚未建立资金组合、可信基准和样本外验证，不能由代码测试推导策略盈利。现有治理结论仍是缺少可投入资本的稳定 edge 证据，系统保持 EDGE_DISCOVERY 待命。
