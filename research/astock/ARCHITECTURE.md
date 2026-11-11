# AStock 个人 AI 助手架构

## 职责边界

```text
工作日 08:00 既有 user timer / 经授权的 operator
              │
              ▼
固定 Node 编排层（白名单路由、单实例、业务日幂等、哈希审计）
              │
              ├─ 固定数据桥 / 受控 Research Context
              │
              ▼
程序核验基础版（事实、数字、时间、URL、陈旧标签均由代码生成）
              │
              ▼
程序编译器 + 证据门 + 发布门 → 研究密封 → 现有投递入口 → Hermes 无模型 send
                                        └─ 结构化摘要 → signal-bus relay

显式 --with-structured-analysis / 人工审校
   └─ 受限 Hermes 计算适配器（固定模型、最小环境、独立轮次/超时）
      无 Telegram/终端/文件写入能力，不进入每日默认链

Pi Agent
   ├─ 开发助手（人工启动）
   └─ 主动投研/诊断执行器（人工启动；不进入自动晨报）

受控研究工具层
   └─ Stock Research MCP
   ├─ get_stock_price()       AkShare 历史行情 + 技术指标
   ├─ get_financial_report()  财务摘要 + 机构/主要股东公开信息
   ├─ get_news()              个股媒体新闻与发布时间
   ├─ get_sector_rank()       行业/概念强弱
   ├─ get_capital_flow()      个股/板块资金流口径
   └─ run_backtest()          Qlib 最近验证回测与横截面排名（只读产物）
   │
   ├─ AkShare                 公共行情、财务、新闻、板块和资金数据
   ├─ Qlib                    因子、模型、回测、横截面评分与排名
   └─ Kronos                 OHLCV 时序状态推断（实验性、本地模型）
```

编排层只有两条可执行路由：`morning-report` 与 `research-context`。命令、路径、模型、工具名、URL 和环境变量都不能由请求选择；审计只保存输入/输出哈希、大小和终态，不保存问题正文、研究结果或凭据。部署、补发、调度变更继续要求操作员逐次批准。

Hermes 有两个互相隔离的角色：计算适配器只负责可丢弃的结构化分析增量，消息适配器只调用无模型 `send`。计算适配器的 bootstrap 只解析并校验固定 Kimi 凭据，随后通过匿名内存描述符把单一 Kimi key 与受控请求交给 `execve` 后的全新最小环境 worker；真实 Hermes Home 中的 Telegram 或其他 provider 凭据不会进入模型进程。worker 不加载插件、MCP、终端、浏览器、文件写入、会话记忆或自动 fallback；模型不能控制报告 Markdown、事实数字、时间、URL 或陈旧标签。消息适配器不读取 Agent Prompt，也不参与研究判断。不得把终端、文件、代码执行、cron、`pi-dev`、部署/补发或通用 Agent RPC 暴露给 Telegram。

按需分析配置选择 `hermes / kimi-coding / k3-256k`，`agentFallback` 为 `none`。默认事实编译不解析模型配置，固定 0 次调用；显式研究和人工审校才严格校验全部模型参数。Pi 的 `ollama-cloud / deepseek-v4-pro` 配置继续保留给开发和主动投研，但不会接管自动晨报写稿。这里描述的是配置和权限边界，不把配置本身当作云模型实跑证明；实时验证必须另行授权。

## 两条互不竞争的运行链

### 自动晨报

```text
工作日 08:00 astock-xhs-source.timer（user）
  → OS 单实例锁
  → 固定任务编排层（业务日幂等 + 哈希审计）
  → 既有 Node 晨报 runner
  → 固定 AkShare stdin bridge
  → 程序编译可发布基础版
  → 程序质量门禁
  → OnSuccess: 研究密封（休市 marker 阻断整个发布分支）
  → 既有 Telegram 投递与结构化 relay 摘要消费
```

这条默认生产链不调用模型、细粒度 Research MCP 或 Kronos，也不建立第二个晨报计划。模型配置损坏不能阻断事实编译；事实来源、固定数据桥、截止时间与正式门禁仍失败关闭。XHS 仅按需生成 DRAFT_ONLY 草稿，不是 Telegram 或 relay 的成功前提。

同一次 timer occurrence 在一个 OS 锁和共享总时限内运行。仅明确列入白名单的预发布故障最多内部重试两次；已经形成的合格 context 与固定截止时间在后续尝试中保持冻结。source runner 无发送能力；内容门禁、落盘失败、超时和信号中断均不自动重试。发送与回执幂等仍由既有投递入口单独守护。

### 主动诊断投研

```text
操作员明确启动 pi-research
  → 唯一工具 astock_research_context
  → 固定任务编排层（单实例 + 哈希审计）
  → 固定 Stock Research MCP 工具集合
  → Qlib 评分/回测证据
  → Kronos 实验性状态推断
  → Pi 生成投资备忘录
```

该入口保留用于人工投研和诊断，不由 cron、Hermes 或 Telegram 自动触发。所有上游文字（尤其新闻）都是不可信证据，不是给 Agent 的指令。链路不能下单、改账户、发稿、创建计划任务或修改系统。

## Qlib 与 Kronos 的固定位置

| 组件 | 规范入口 | 职责 |
|---|---|---|
| Qlib 研究树 | `/opt/ai/research/qlib` | 因子、训练、回测、组合评估 |
| Qlib 行情数据 | `/opt/ai/data/market/qlib` | 每晚更新的 CN 日频数据 |
| Qlib 结果 | `/opt/ai/reports/qlib` | 供 MCP/受限 Agent 只读消费的验证产物 |
| Qlib MCP 预留 | `/opt/ai/mcp/qlib-mcp` | 薄型只读适配，不放训练和任意路径执行 |
| Kronos 研究树 | `/opt/ai/research/kronos` | K 线时序模型与实验代码 |
| Kronos 模型 | `/opt/ai/models/kronos` | 固定 mini 与 tokenizer 权重 |
| Kronos 环境 | `/opt/ai/venvs/kronos` | 与 AStock/Qlib 隔离的 CPU 推理环境 |

保留这些规范入口；底层原目录继续通过符号链接兼容。不得把 Qlib 源码搬进 MCP，也不得把 Kronos 实验输出混进 Qlib 正式回测目录。

## 投资备忘录结构

1. 结论摘要与数据截止时间。
2. 行情与技术状态：价格、量能、趋势、波动和反证位。
3. 财务与机构信息：最新可得报告期、披露时间与缺口。
4. 行业趋势和资金口径。
5. 新闻催化：事实、推断、可能反证分别列出。
6. Qlib：模型样本截止日、原始 score、横截面 rank/percentile、回测收益与回撤。
7. Kronos：未来状态候选情景；明确是模型推断，不报告虚构置信度。
8. 人工规则：数据过期、财务恶化、流动性/合规风险等可以否决模型结论。
9. 风险和数据缺口；不输出保证收益、确定涨跌或无条件买卖指令。

最终判断由三类证据合成，但不把不可比指标硬凑成一个伪精确总分：

```text
AI 证据判断 + Qlib 横截面评分 + Kronos 时序情景
                         ↓
                  人工风险规则否决门
                         ↓
              观察 / 候选 / 回避（研究结论）
```

## 数据与执行原则

- 任何行情、财务、资金或新闻数字都要带来源和数据时间。
- Qlib 回测和排名是历史研究证据；`run_backtest()` 当前读取最近验证产物，不会从聊天中启动训练或任意回测进程。
- Kronos 只使用固定本地模型、固定代码路径和最多三组历史序列；失败时显式降级，不阻断其他证据。
- MCP 参数不接受 command、path、server、tool、URL 或模型路径。
- Hermes 生产计算、Hermes 无模型发稿、Pi 开发/显式回滚和受控研究工具保持四个独立权限域。
