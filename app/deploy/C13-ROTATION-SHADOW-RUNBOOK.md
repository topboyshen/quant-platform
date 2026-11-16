# C13 ETF 轮动影子（2026-11-15）

## 范围与冻结参数

- **纸面账户，不是交易系统**：只写 `signals.rotation_shadow_*`；不下单、不写 `target_decision`/正式记录链、不调用通知。
- `qp-rotation.v1`：250 个标的交易日价格动量，Top8，每只目标 12.5%；负动量也排序，无大盘过滤；20 个交易日调仓。
- 当前 40 只 ETF 池在注册时写入 `rotation_shadow_account.parameters.universe_codes`，与算法参数一同 SHA256 冻结。后续修改必须另建策略版本，不修改旧计划。
- 初始模拟资金 ¥1,000,000；真实未复权价/真实份额、100 份整手；双边佣金 0.03%（最低 5 元）、滑点 0.05%、无印花税。卖出所有减仓后再买入，现金不得为负。
- 20 日均成交额门槛 500 万元（腾讯量单位为手；新浪回退量单位股，入库前 ÷100）。
- 不计分红与现金利息，页面是价格收益，不是全收益或成交保证。

## 数据与前向期

1. `rotation_shadow_bar` 存原始未复权 OHLCV，历史缓存仅为指标预热；初始化部署导入 40×400 根，不导入历史净值/模拟交易。
2. `rotation_shadow_account` 注册两维 `(rule_version, phase)`，`forward` 和 `replay` 资金/持仓/计划/净值完全隔离。首次只初始化 `forward`，`replay` 为空属正常。
3. **资金基线 2026-11-13，前向起点 2026-11-16**。11-13 只记三条资金均为 100 万的基线和已锁定 Top8 计划，不算观察天数。
4. 11-16 收盘后的任务用当日开盘价回记模拟成交和收盘估值。该模拟不是 09:30 实时交易执行；不可作为实际成交回执。
5. 第 20 个前向交易日收盘生成下一计划，第 21 个开盘模拟执行；节假日按 `market.trading_day(origin='qp')`，不按自然日或 4 周算。即使没有下一日行情，最新收盘的计划也必须先持久化；日历未延伸时 `execute_date=null`，延伸后只能在下一个开市日执行。
6. 修复/补数不重写已计价历史：重复导入同数据幂等；价格修订或账本覆盖日前新增历史数据会拒绝。原始预热完整缓存的 SHA256 放在部署回执中。

## 相同口径的对照

- 510300：独立 100 万账户、首个开盘买入持有，不每日/每次调仓复位。
- EW-20d：同一池当日符合成交额的标的等权；与 C13 同一时钟、次日开盘模拟、同佣金/滑点/整手；信号日权重随 C13 计划一起冻结。
- 所有估值基于同一天 raw close，所有基准状态保存在每个 `rotation_shadow_day.detail.state`。
- 表中 `nav_after` 是**执行日收盘资产**，不是刚成交时点净值；`nav_before` 是当日开盘成交前资产。
- 观察不足一年不年化收益；不足 20 个日收益不展示夏普。历史回放不能累计到前向天数。

## 不可靠数据的处理

- 当日 40 只行情有缺失/发布滞后：暂停当天和后续净值，保留前次账本与 pending 计划，任务 `ok=false`；16:40 同任务幂等重试。
- 前向出现价格跌超 25% 或涨超 40%：可能份额折算/数据问题，**暂停、不自动补成零收益**。需独立确认份额与现金变更、修复方案后另行授权恢复。
- 排名历史的启发式连续化仅用于动量指标，不用于成交价、NAV 或持仓数量。上轮探索回测使用启发式公司行为处理与简化执行，不能宣称本影子账户逐元复现历史 CAGR；不把未经重建的历史净值放入页面。
- 日线一字涨跌停 ≥9.5% 或相关成交标的当日零成交量：保守判为无法验证成交，暂停纸面账本。
- 前向账本拒绝当日 15:05 前数据和未来日期；初始化不得在首次开盘后补登记。
- 251 根预热必须覆盖同一 251 个已知开市日，补数不能悄悄把动量锚点推到更老一天；推进区间每个自然日必须明确标记开闭市，且不能把任意已观测 ETF 行情日标成休市；缺口则暂停，恢复日历后再幂等续跑。
- 同源重试/备用新浪源只能追加未计价新数据，若最新重叠价格差 >0.0021，暂停。
- 任意直接推进/定时任务均用同一账户级 PostgreSQL transaction advisory lock；同一计划/NAV 有唯一键，逐日和整批运行需完全一致。

## CLI / API / 页面

```sh
# 只在新账户第一次部署时，先导原始预热（不会导历史 NAV）
qp signals rotation-shadow --seed-bars /var/lib/qp/bootstrap/c13/warmup-raw.json
# 必须在首次开盘前注册，不允许倒填 forward 起点
qp signals rotation-shadow --initialize 2026-11-16
# 注册后的幂等查询
qp signals rotation-shadow --summary
# 日常：获取当日 raw bars，推进到当日（自然运行由 timer）
qp signals rotation-shadow --step --date YYYY-MM-DD
# 无网络推进，仅用已入库 bars（非手动重跑实盘）
qp signals rotation-shadow --until YYYY-MM-DD
```

- 页面：`/next/signals/rotation-shadow` → 前向观察 / 历史回放（明确空态）；图表/数据表、持仓、pending、最近模拟调仓及成交明细。
- API：只读 `GET /api/signals/rotation-shadow?phase=forward|replay`；需要有效登录与 `signals:read` scope。
- 未登记的账户，定时任务不会自行创建或回填。

## 定时和自然验收

`qp-signals-rotation-shadow.timer`：工作日 **15:50 / 16:40 CST**，调用 `qp-job@signals.rotation_shadow.service`。16:40 只补发布延迟，不重复计仓位/费用/净值。

2026-11-16 首次验收：

- job succeeded 且 result.ok=true；基线一行 + 11-16 日一行，前向 `days=1`。
- 一个 pending 首笔计划变 executed，signal=11-13、execute=11-16；不可用 11-16 收盘重选目标。
- cash ≥0、持仓不超过 8；cash + raw-close 市值合计 = nav。
- 三条净值均从独立 100 万资金起，EW 与 510300 都有成交成本；replay 仍空。
- 再跑同一日期，day/run/费用/资产数值不变。
- `target_decision`、正式链、notify 没有来自本模块的数据。

## 部署 / 回滚

- migration `0016` 只新增 4 张 shadow 表，不更改正式表。
- 固定前一 release、tarball SHA、数据库版本 `0015`、health=200、磁盘容量；先留 schema/表计数与数据备份，再迁移。
- **`QP_APP_DIR` 必须指向新 release/backend**；迁移完成验证 0016 再原子切 `current`，重启 `qp-api`，health=200 后启用新 timer。
- 回滚：先停新 timer，`current` 切回上一个 release 后重启 API。0016 为纯新增表可保留，不删账本、不执行 downgrade；旧版本忽略这些表。
- 本任务不恢复任何退役 QuantDinger/trading-radar/报告生成服务。
