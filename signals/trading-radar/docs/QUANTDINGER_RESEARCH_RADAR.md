# QuantDinger 个人 A 股研究雷达

`scripts/quantdinger_research_radar.py` 将 Trading Radar 的宏观风险状态与
QuantDinger 的个人研究记录组合为一份适合 Telegram 阅读的纯文本报告。

## 数据边界

报告只读取以下已有数据，不触发新的 AI 分析、回测或交易：

- `qd_watchlist`：指定用户的 `CNStock` 个人观察名单，是股票范围的唯一来源；
- `qd_analysis_memory`：每只观察股最近一次已完成的 AI 资产分析和技术快照；报告按交易日标注新鲜度；
- `qd_indicator_codes`：个人指标名称；只有 `qd_backtest_runs.indicator_id` 匹配时才视为已用于回测；
- `qd_strategies_trading`：个人 A 股策略、当前运行状态及当前代码哈希；
- `qd_backtest_runs`：优先读取当前策略 ID 与代码哈希匹配的成功回测，否则回退展示最近历史结果并标注来源；
- `data/radar_risk_state.json`：Trading Radar 正式宏观风险状态；
- `data/swing_macro.db`：最近一个正式市场宽度快照；非当日数据标为“最近正式收盘宽度”。
- 腾讯行情：生产任务经用户授权，只发送观察名单股票代码，并读取带时间戳的当日行情。

观察名单为空时，报告明确显示为空，不使用示例股票或默认股票。数据库不可用时，
报告失败关闭并显示错误摘要，不回退到另一份名单。

## 隐私与安全默认值

- `--user-id` 必须显式提供，避免把其他用户的记录混入报告；
- 命令行默认不把个人观察名单发送给外部行情服务；
- 只有显式使用 `--live-quotes` 才会向腾讯行情接口查询观察股代码，生产包装器已按用户授权显式启用；
- 腾讯行情缺失、时间无效或不是报告当日时失败关闭，不使用 AI 历史分析价冒充当前价；
- 报告分别显示腾讯当前/收盘价及时间、AI 分析价及北京时间，两者不混用；
- 生成器只执行数据库查询和本地文件读取；
- 输出不包含令牌、邮箱、数据库密码或策略源码；
- 报告自动限制在 Telegram 单条消息的安全长度以内。

## 手动预览

在 QuantDinger PostgreSQL 容器和 Trading Radar 数据目录都可用的主机上运行：

```bash
python3 -B scripts/quantdinger_research_radar.py --user-id 1
```

命令只把报告写到标准输出。不带 `--live-quotes` 的预览不会访问外部行情；验证生产行情时
可显式添加该参数，但不要把预览自动投递到 Telegram。

## Hermes 单次投递

生产调度应使用 Hermes 的脚本直出模式，避免 LLM 改写以及“脚本发送一次、任务总结再
发送一次”的重复通知：

```bash
hermes cron create '35 15 * * 1-5' \
  --name quantdinger-personal-a-share-research-radar \
  --script quantdinger_research_radar.sh \
  --no-agent \
  --deliver origin
```

将 `deploy/hermes/quantdinger_research_radar.sh` 安装到
`~/.hermes/scripts/quantdinger_research_radar.sh`。包装器只调用
`/home/ubuntu/releases/quantdinger-research-radar/current` 指向的不可变发布目录，并显式
传入用户 ID。创建任务前先运行脚本预览；创建后先保持旧任务暂停，确认新任务记录中
`no_agent=true`、投递目标仍是原 Telegram 会话，且只存在一个启用的同类任务。

生产包装器经用户授权显式添加 `--live-quotes`。任务安排在工作日 15:35，在 A 股收盘及
Trading Radar 市场宽度更新后生成；腾讯请求只包含观察名单中的股票代码。撤销授权时，
从包装器移除该参数即可恢复为纯本地只读报告。

## 回测解释

报告不在每日调度中现场重跑。对每只股票，先寻找与当前策略 ID 和代码哈希都匹配的成功回测；
没有匹配项时可以展示最近历史结果，但必须标成“历史代码版本”“未关联策略”或“其他策略”，且不能计入
“当前代码回测”。回测有 `benchmark` 时分别展示策略和 buy&hold 的收益、回撤与 Sharpe，并显示超额收益；
旧回测没有该字段时明确显示“基准待重跑”。

AI 的 `decision/confidence` 显示为“AI方向”，摘要中的操作性语句单列为“行动结论”；二者含义不同。
RSI、MACD、ATR 等来自 `qd_analysis_memory.indicators_snapshot`，因此统一标为“AI技术快照”，不再冒充
`qd_indicator_codes` 中的个人指标。页尾“数据质量”同时报告 AI 过期、非当前代码回测和个人指标未关联数量。

## 回滚

1. 暂停新版 Hermes 任务；
2. 保留最后一次报告和 Hermes 运行记录用于审计；
3. 将脚本入口切回上一份不可变发布目录，或恢复调度前的 `jobs.json` 备份；
4. 不要恢复旧的硬编码股票任务。
