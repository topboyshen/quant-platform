# 研究指标适合性分析报告 — 600875 东方电气

> 生成时间：2026-08-04
> 仓库：QuantDinger（指标存储于 `qd_indicator_codes` id=1）+ trading-radar（K线数据/回测工具）
> 分析范围：自选股 6 只（东方电气/露笑科技/长信科技/长白山/同花顺/合锻智能），区间 2023-08-01 ~ 2026-07-30，日线前复权，10 万本金，单边佣金 1‰，仅做多

---

## 1. 指标概述

- **名称**：`CNStock-日线趋势动量-20260731`（`qd_indicator_codes.id=1`，user_id=1）
- **逻辑**：仅做多趋势动量
  - 入场：`收盘>MA20` ∧ `MA20>MA60` ∧ `MACD金叉` ∧ `RSI∈(45,70)` ∧ `量比>1.1`
  - 离场：`收盘<MA20` ∨ `MACD死叉` ∨ `收盘<risk_line`
- **参数**：ma_fast20 / ma_slow60 / rsi_len14 / macd 12/26/9 / atr_len14 / rsi_low45 / rsi_high70 / vol_ratio_min1.1 / atr_mult2.5

## 2. 对 600875 的回测（QuantDinger 库内结果）

| 指标 | 值 |
|------|-----|
| 总收益 | +46.0% (QuantDinger 原回测，起 2023-08-01) |
| 年化 | 17.4% |
| 最大回撤 | -21.5% |
| 夏普 | 0.79 |
| 胜率 | 55.6% |
| 盈亏比 PF | 3.95 |
| 交易次数 | 9 |

## 3. 基准对比（Buy&Hold 600875，同区间）

| 策略 | 总收益 | 最大回撤 | 夏普 | Calmar |
|------|------:|------:|----:|----:|
| **Buy&Hold 600875** | +98.14% | -44.29% | 0.81 | 2.22 |
| **研究指标(原版)** | +59.55% | -22.22% | 0.80 | 2.68 |

**结论**：该指标对 600875 **不产生超额收益**，本质是「降回撤工具」——以约 38pp 的收益换取回撤减半。夏普持平，Calmar 略优。

## 4. 参数稳健性扫描（OAT）

| 参数 | baseline | 表现 | 稳健性 |
|------|------|------|------|
| ma_fast(10-30) | 20 | 49%~80% | 🟡 中等 |
| ma_slow(40-80) | 60 | <50 塌陷，≥50 稳 | 🟢 ≥50 稳健 |
| rsi_low(35-55) | 45 | 35-50 不变 | 🟢 稳健 |
| **rsi_high(60-80)** | 70 | **60→25%, 65→26% 崩塌** | 🔴 悬崖参数 |
| vol_ratio_min(0.9-1.3) | 1.1 | 几乎不变 | 🟢 稳健(近乎失效) |
| **atr_mult(1.5-3.5)** | 2.5 | 5 档**完全相同** | ⚪ 失效 |

## 5. 关键 Bug 发现：ATR 止损为数学死代码

原指标代码：
```python
risk_line = close - atr_mult * atr
exit_cond = ... | (close < risk_line)
```
由于 `atr > 0`，`close < close - atr_mult*atr` **恒为假** → ATR 止损分支永不触发。这正是参数扫描中 `atr_mult` 5 档结果完全相同的根因。

## 6. 跨自选股泛化测试（6 只）

| 股票 | Buy&Hold | 原版收益 | 原PF | 跑输BH |
|------|------:|------:|----:|------:|
| 东方电气 | +98.1% | +59.6% | 3.28 | -29.4 |
| 露笑科技 | -6.9% | -15.7% | 0.51 | -14.2 |
| 长信科技 | -0.5% | -28.4% | 0.23 | -36.5 |
| 长白山 | +146.4% | +24.6% | 1.54 | -151.0 |
| 同花顺 | +120.2% | -0.5% | 0.98 | -114.0 |
| 合锻智能 | +126.0% | +44.8% | 1.66 | -81.2 |

**泛化结论**：6 只里仅东方电气 PF>2；6 只全部跑输 buy&hold。该指标**不适合作为通用选股/择时指标**，仅在「温和趋势+清晰回撤」的个股（如东方电气）有效。

## 7. 已实施的 P0 修复：ATR 止损 opt-in 化（向后兼容）

### 改动
将死代码修复为正确的 chandelier 跟踪止损，并以默认关闭的开关 `use_atr_trail` 保护旧行为：

```python
# 1) 修复死代码 → 滚动最高价 chandelier
risk_line = close.rolling(atr_len, min_periods=1).max() - atr_mult * atr
# 2) ATR 止损 opt-in（默认 0 → 与旧版比特级一致）
atr_trail_on = bool(params.get("use_atr_trail", 0))
atr_exit = (close < risk_line) if atr_trail_on else pd.Series(False, index=df.index)
exit_cond = (close < ma_fast_s) | macd_dead | atr_exit
# 3) 新增参数文档
# @param use_atr_trail int 0 是否启用ATR跟踪止损(0=关闭,与历史一致;1=启用chandelier)
```

### 验证结果（直接执行库内代码，600875）
| 版本 | 收益% | 回撤% | Sharpe | PF | 交易 |
|------|----:|----:|----:|----:|----:|
| 旧版(死ATR) | 59.55 | -22.22 | 0.80 | 3.28 | 12 |
| 新默认(trail=0) | 59.55 | -22.22 | 0.80 | 3.28 | 12 |
| 新opt-in(trail=1) | **68.74** | -22.22 | **0.88** | **3.62** | 12 |

- ✅ **旧 vs 新默认：long_entry/long_exit 比特级一致** → 默认行为零变化，向后兼容
- ✅ **opt-in(trail=1) 在 600875 改善**：+9.19pp 收益，Sharpe/PF 提升
- ⚠️ opt-in 不是普适改进（露笑科技 -5.35pp），故设为 opt-in 而非强制

### 回退方式
原代码已备份至 `~/indicator_id1_code_backup_before_atr_fix_20260804.py`，必要时可一键还原 `qd_indicator_codes.code`。

## 8. 后续建议（P1/P2）

| 优先级 | 行动 | 状态 |
|--------|------|------|
| P0 | 修复 ATR 死代码（opt-in chandelier） | ✅ 已完成并验证 |
| P1 | 回测引擎加 buy&hold 基准对照列 | ✅ 已完成并部署(2026-08-04) |
| P1 | RSI 上轨改为可选/默认关闭 | ❌ 已否决(隔离测试 6 只平均 -1.60pp, 净负面) |
| P1 | 指标加「大盘 regime 过滤」（指数在 MA60 上方才开仓） | 待办 |
| P2 | 仅对趋势评分高的票启用该指标 | 待办 |
| P2 | 参数稳健性网格入库，标注脆弱参数 | 待办 |

## 9. 最终适合性判断

- **对东方电气**：✅ **适合**。建议采用 opt-in 模式（`use_atr_trail=1`），收益/Sharpe/PF 均提升，回撤不变。但需对照 buy&hold 理解其为「降回撤工具」而非「超额收益工具」。
- **对自选股整体**：❌ **不适合作为通用指标**。需加大盘 regime 过滤 + 修复死代码后才有泛化价值。

## 10. 变更记录

- **2026-08-04 P0 已落地**：指标 id=1 的 ATR 死代码修复为 opt-in chandelier 止损（`use_atr_trail`，默认关闭，向后兼容已验证）。原代码备份 `~/indicator_id1_code_backup_before_atr_fix_20260804.py`。
- **2026-08-04 P1-1 已完成并部署**：`backend_api_python/app/services/backtest.py` 新增 `_buy_hold_benchmark` 方法，在 3 条回测路径注入 `result['benchmark']`。已用 4 文件 compose 集 rebuild 并重建容器 `radar-quantdinger-backend`，健康检查通过（`/health` healthy，gunicorn 干净启动，容器内代码含改动）。新镜像 id `fe5bc8898aed`，旧镜像保留为 `trading-radar-quantdinger-backend:rollback-benchmark-20260804`（id `a44fd5b13b81`）可一键回滚。原文件备份 `~/backtest.py.before_benchmark_patch_20260804`。
- **2026-08-04 P1-2 已否决**：RSI 上轨 opt-in 化经隔离测试（ATR 保持默认关闭）证明单独关闭上轨在 6 只自选股平均 -1.60pp（长信 -8.61、同花顺 -1.05 变差），非改进项，不应用。RSI 上轨在 ATR 失效时实际承担保护性过滤，正确组合应为「chandelier 止损开启」（即已落地的 `use_atr_trail=1`）。

## 11. 部署记录与隐患提示

- **部署方式**：用与运行态一致的 4 文件 compose 集（`docker-compose.yml` + `integration.override` + `quantdinger` + `runtime-cleanup`）build + `up -d quantdinger-backend`，同 tag `personal-2026.08.02-llmfix.1` 重建，仅 backend 容器受影响（postgres/redis/mihomo 未动）。
- **回滚命令**（如需）：`docker tag trading-radar-quantdinger-backend:rollback-benchmark-20260804 trading-radar-quantdinger-backend:personal-2026.08.02-llmfix.1 && cd ~/trading-radar && docker compose -f docker-compose.yml -f docker-compose.integration.override.yml -f docker-compose.quantdinger.yml -f docker-compose.runtime-cleanup.yml up -d quantdinger-backend`
- ℹ️ **compose 文件集说明（已核实，无需处置）**：`~/trading-radar/.env` 中 `COMPOSE_FILE=docker-compose.yml:docker-compose.integration.override.yml:docker-compose.quantdinger.yml:docker-compose.runtime-cleanup.yml`（4 文件集，不含 radarflow.13.candidate）。经核实默认 `docker compose config`（不带 -f）backend 镜像即为 `personal-2026.08.02-llmfix.1`，与运行容器一致；且无任何部署脚本引用 radarflow.13.candidate。故默认 `docker compose up -d`（不带 -f）是安全的。`docker-compose.quantdinger.radarflow.13.candidate.yml` 仅在显式 `-f` 指定时生效（候选迭代文件的正常用法），保持现状即可。（先前报告此条曾表述为隐患，经进一步核实已降级为说明。）
