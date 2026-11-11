# QuantDinger 个人 A 股研究雷达 P0 治理

日期：2026-08-15（Asia/Shanghai）

## 范围

本次只实施 P0 正确性与资格控制：

- 使用本地 A 股交易日历计算最近已收盘交易日；
- 在唯一活跃消费者（Hermes Telegram）前执行中央 fail-closed 判定；
- 输出固定资格状态、原因码和受限审计记录；
- 移除 AI 摘要中提取的方向性行动文案；
- 不执行 AI、指标或回测重算，不调参，不自动交易。

QuantDinger API 与 PersonalHome 当前没有个人股票资格契约。本次没有虚报三端一致；API/UI 复用中央契约列入 P1。

## 资格状态

- `BLOCKED`：任一硬门禁失败，只能进入证据修复队列。
- `RESEARCH_ONLY`：P0/P1 证据完整，但 P2 或人工复核尚未完成。
- `VALIDATED_CANDIDATE`：仅在预注册 P2 验证和人工复核均完成后允许出现；仍不代表自动交易。

硬门禁固定顺序：

1. `GATE_CALENDAR`
2. `GATE_MKT_FRESH`
3. `GATE_DATA_LINEAGE`
4. `GATE_STRATEGY_VERSION`
5. `GATE_INDICATOR_ID`
6. `GATE_BACKTEST_BASELINE`
7. `GATE_BACKTEST_REPRO`
8. `GATE_LOOKAHEAD`

`GATE_P2_VALIDATION`、`GATE_MANUAL_REVIEW` 与 `AI_STALE` 作为升级限制或研究提示保存；RPS 不得覆盖硬门禁。

## 交易日语义

运行时只读取 `/home/ubuntu/trading-radar/data/trading_calendar.json`，不联网猜测交易日，不新增依赖。日历 schema、来源、生成时间、数量、范围、排序和覆盖任一项无法验证时，资格默认阻断。

- 交易日 15:05 前：最近收盘日为上一交易日；
- 交易日 15:05 后：最近收盘日为当日；
- 周末和休市日：回溯至最近交易日；
- 腾讯行情市场时间必须与该日期一致，且时间不早于 14:55。

## 2026-08-15 冻结回放

- 最近已收盘交易日：2026-08-14；
- 观察标的：7；
- `BLOCKED`：7；
- `RESEARCH_ONLY`：0；
- `VALIDATED_CANDIDATE`：0；
- 周五正式收盘行情识别：7/7；
- Telegram 方向性行动词：0；
- 报告长度：3178 字符（上限 3800）。

40 个交易时段、周末、节假日、坏缓存和行情集成场景全部通过；硬门禁漏拦截为 0。

## 运行与回滚

生产 release：

`/home/ubuntu/releases/quantdinger-research-radar/20260815-p0-governance`

受限 append-only 审计目录：

`/home/ubuntu/trading-radar/runtime/research-radar-audit/`

回滚脚本：

`/home/ubuntu/archive/quantdinger-research-radar-p0-pre-20260815T2015+0800/rollback.sh`

旧 release 保留，没有重建或重启任何生产容器。

