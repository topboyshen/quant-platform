# AStock_AI_System

本机每日 A 股基础数据快报系统：系统计划任务和 Node 负责总编排、状态与发稿门禁；AkShare 固定数据桥收集可追溯数据；程序把已核验事实编译成完整基础版。未显式使用研究开关时，模型调用固定为 0 次，不解析或验证可选模型配置；事实、截止时间、固定数据桥及风险配置仍严格校验。显式使用 `--with-structured-analysis --date YYYY-MM-DD --input FILE` 或人工审校入口时，才严格校验全部模型参数。Hermes 只能从程序预审出的候选白名单中选择，不能自行写分析、拼接来源或控制报告 Markdown。结构、来源与时效门禁通过不代表研究语义经过人工审校；Pi 不在每日生产主链路中，仅保留为开发助手和人工诊断入口。

显式模型开关仅适用于尚未封存的当次输入；已有归档会被重用，不得用按需研究改写或重新发送旧日报。需要事后研究时应另作有来源、观察时间的附录，不冒充原截止已知。

轻量生产版自动链路（本仓模板；以部署后实际生效单元为准）：

```text
工作日 08:00 astock-xhs-source.timer
  -> astock-xhs-source.service
  -> 固定数据桥、确定性编译与正式报告门禁
  -> ASTOCK_FINAL_DELIVERY=disabled（不写通知 outbox，不外发）
  -> 休市由同一入口写正常跳过 marker，source→seal 日报发布分支结束
  -> 交易日 OnSuccess 仅启动 astock-research-seal.service
  -> 密封成功后由既有单元触发 Hermes Telegram 与 signal-bus relay

astock-xhs-draft.service 不在每日自动链；需要时手工构建本地不可变
DRAFT_ONLY 草稿，不具发布权。原有 relay 的定时有界重试仍独立存在；
休市可提交签名 research_skip 状态回执，不生成日报或发送 Telegram 日报。
```

XHS 草稿链不调用 Telegram、Hermes send 或任何发布端点。正式报告的 `publicationGate=passed` 只说明结构、来源与时效门禁通过，不等于 XHS 可以发布；XHS manifest 固定为 `DRAFT_ONLY`、`publishEligible=false`，且要求人工批准。

分析与消息投递的权限完全分离：计算进程不能发消息，发送进程不读取 Agent Prompt，也不参与研究判断。定时基础版仍须独立通过原有证据门和发布门；显式模型分析失败只会舍弃增量。配置不自动回退到 Pi。

## 现在能做什么

- 获取 A 股主要指数、市场概况、主题/行业线索和三只核心股票的数据。
- 采集可用的资金、公告、新闻及财务字段，并保存来源与数据时间。
- 输出“数据完整度、市场状态、今日主线、股票池、今日风险”结构化晨报。
- 跟踪 `002617 露笑科技`、`600875 东方电气`、`300033 同花顺`。
- 通过确定性事实编译器和程序门禁后才落盘；AI 只提供受限、可丢弃的分析增量。
- 提供细粒度 Stock Research MCP：行情/技术、财务/机构、新闻、行业、资金流和 Qlib 回测/排名。
- 交互式 `pi-research` 聚合六类工具、Qlib 横截面评分和本地 Kronos 实验性状态推断，生成中文投资备忘录。
- 通过固定任务编排层为晨报和主动投研提供白名单路由、单实例控制、业务日幂等与隐私哈希审计。

这是一套研究辅助工具，不构成个性化投资建议，也不会自动交易。

## 一键安装数据环境

开发机需要 Windows PowerShell、Node.js 22 或更高版本；生产服务器还需要项目固定的 Python 数据环境、Hermes 0.20.0，以及已在 Hermes 中配置好的 `kimi-coding / k3-256k` 认证。Pi 及其 Ollama Cloud 配置只在开发或显式回滚时需要。

进入项目根目录后执行：

```powershell
npm ci
npm run data:setup
```

第一条命令会按锁文件安装 Node 依赖；项目级 `.npmrc` 固定使用 legacy peer 语义，因为 `pi-turn-limit` 仅为机器上全局 Pi 的显式回滚路径提供轮次保险丝，不在项目内安装第二套 Pi。第二条会创建项目专用的 `.venv`，并安装 `requirements.txt` 中固定的 `akshare 1.18.81`、`mcp 2.0.0` 与 `pandas 3.0.5`。这些依赖都保留在项目文件夹中，系统 Python、Hermes 凭据和 Pi 凭据不会被修改。

随后检查整个项目：

```powershell
npm run validate
npm test
npm run doctor
```

需要额外验证当前所选 Agent 的云端模型时再运行 `npm run doctor:live`；这一步会产生少量云端用量，普通 `npm run doctor` 不应发起模型生成。

## 先做数据 dry-run

```powershell
npm run data:test
```

该命令通过固定 AkShare 桥获取并校验真实数据，只准备 `work/morning-context.json` 等运行材料；不会调用模型，也不会生成正式晨报。首次完整采集通常需要约 1—3 分钟；若上游接口暂时不可用，它会以失败退出，不会用演示数据冒充真实行情。

## 正式生成晨报

```powershell
npm run morning
```

今天是 2026-08-02（周日），不是 A 股交易日。要生成首份面向下一交易日的报告，先预演数据，再把目标日期指定为 2026-08-03；交易日状态仍以数据桥返回的交易日历为准：

```powershell
npm run data:test
.\scripts\run-morning-report.ps1 -Date 2026-08-03
```

指定日期：

```powershell
.\scripts\run-morning-report.ps1 -Date 2026-08-03
```

固定数据桥自动使用 `.venv\Scripts\python.exe mcp\akshare_bridge.py`，无需设置环境变量。数据包为 `live` 或 `partial` 且全部门禁通过时，程序基础版写入 `reports/daily/YYYY-MM-DD.md`；有效的结构化分析增量会由程序安全合并。核心数据门禁不通过时不生成正式报告，也不会调用模型来掩盖数据缺口。

## Hermes 与 Pi 的职责

Hermes 是显式按需研究的分析执行器，不在定时日报默认路径；调用时只通过项目的受限适配器接收固定 Prompt、结构化 schema 和经过边界检查的证据附件。适配器先校验固定 Kimi 凭据，再以全新最小环境启动隔离 worker，只传入该 Kimi key 与本次受控请求；Telegram 和其他 provider 凭据不会进入模型进程。worker 使用固定 provider/model、较短的独立轮次与超时，并关闭插件、MCP、终端、浏览器、文件写入、会话记忆与自动 fallback。模型不能写报告 Markdown、行情数字、时间、URL 或陈旧标签；最终正文只由 Node 编译器生成并经过证据和发布门禁。

Pi 不负责每日晨报调度、生成、审校或发稿。服务器保留以下人工入口，供开发和主动投研诊断：

```bash
pi-dev
pi-research
```

`pi-dev` 是开发助手：读代码、修改、测试；只有目录已经是 Git 仓库且用户明确要求时才提交。它不运行计划任务、完整晨报或发稿。

`pi-research` 是人工启动的只读投研入口：不加载 Bash、文件读写等内置工具，只能调用固定的 `astock_research_context`。它不会被 cron 或 Telegram 自动调用。保留的研究链路为：

```text
Pi
  -> 固定任务编排层（单实例、审计，不接受任意命令/路径）
  -> Stock Research MCP（stdio，六个固定工具）
  -> AkShare 行情 / 技术 / 财务 / 机构 / 新闻 / 行业 / 资金流
  -> Qlib 最近验证回测 + 历史横截面评分/排名（只读）
  -> Kronos-mini 本地时序状态推断（实验性、失败可降级）
  -> Pi 中文投资备忘录
```

六个 MCP 工具为：`get_stock_price`、`get_financial_report`、`get_news`、`get_sector_rank`、`get_capital_flow`、`run_backtest`。其中 `run_backtest` 是兼容名称：当前只读取固定位置的最近验证 Qlib 历史产物，不从聊天启动训练、动态回测或文件写入。

投资备忘录固定包含结论摘要、行情与技术、财务与机构、行业与资金、新闻催化、Qlib 评分与回测、Kronos 状态、人工规则/反证、风险与数据缺口。完整架构见 `ARCHITECTURE.md`。

交互式 Pi 中的旧 `/morning-report` 只说明职责边界，不执行工作流。人工正式发稿由操作员在 Pi 之外直接运行 `npm run morning` 或既有日期 runner；自动发稿仍由系统计划任务调用 Node 编排器。`agentFallback` 固定为 `none`；即使操作员临时停用 Hermes，晨报也只能使用程序核验基础版，不会让 Pi 接管自动写稿。

Windows 的 `scripts/start-pi.ps1` 仍可用于开发会话，但不再默认传入全局 `--approve`。

默认生产计算配置为：

```text
agentExecutor: hermes
provider: kimi-coding
model: k3-256k
reasoning: high
agentFallback: none
```

保留的 Pi 开发/主动投研配置仍固定为 `ollama-cloud / deepseek-v4-pro`。Hermes 和 Pi 的认证信息都只保存在各自用户配置中，本项目不会复制、记录或显示密钥。

## Hermes Telegram 通知

`astock-xhs-source.service` 固定设置 `ASTOCK_FINAL_DELIVERY=disabled`，source 本身不外发；成功密封后的 `astock-morning-notify.service` 经 seal 的 drop-in 触发，继续使用既有 Hermes 发送与平台回执。XHS 草稿单元不是 Telegram 的触发器。任何手工恢复发送仍需逐次授权。

正式通知只由密封成功链进入现有 `deliver-morning-report.mjs`，再由 Hermes 的无模型 `send` 接口投递到已配置的 Telegram 主频道；source-only runner 不写发送 outbox，也不调用通知器：

```text
AStock Node -> 程序基础版 -> 证据门禁 -> 研究密封 -> 现有投递入口 -> Hermes send -> Telegram
```

- 正式成功必须同时满足：本次进程退出 0、`last-run` 属于本次运行、证据审校通过、报告位于 `reports/daily/`。
- 正式报告会先复制为不可变 Markdown 快照，再作为 Telegram 文档发送；报告正文不会被 Hermes 当成 `MEDIA:` 等控制指令执行。
- 休市在唯一入口结束发布分支，不发送日报；失败由独立统一告警判定，不发送堆栈、凭据或旧报告。
- 同一日期的成功、失败和跳过事件分别幂等；同日重跑不会重复发送同类通知。
- 网络调用结果不确定时事件进入 `ambiguous`，不会自动重发，避免 Telegram 重复消息。

查看状态；只有在另行获得外发授权后，才可显式投递尚未开始发送的事件：

```bash
npm run notify:status
ASTOCK_FINAL_DELIVERY=telegram npm run notify:drain
```

仓库不包含 notify systemd unit，VPS 也不再安装它们。未来若重新引入后台 drain，必须先纳入 canonical、测试和审查；添加或启用它属于发布策略变更，不是日常维护动作。

`config/notifications.json` 中保留的 2026-08-03 owner approval 仅适用于显式人工通知路径。发送端同时执行两道门：持久策略必须有效，且本次进程必须精确设置 `ASTOCK_FINAL_DELIVERY=telegram`；缺失或 `disabled` 均不发送。`scripts/run-morning-cron.sh` 只接受未设置或 `disabled`，其它值在运行业务前以 64 拒绝，不能再作为第二发送路径。显式 drain、correction 或 manual-same-day 仍属于真实外发操作，须逐次授权，不能作为健康检查。

## 自动调度

### Windows 计划任务

部署本机计划任务：

```powershell
.\scripts\install-schedule.ps1
```

任务名为 `AStock_AI_Morning_Report`，工作日 08:00 在当前用户已登录时运行；电脑错过时间后会在可用时补跑。脚本会先用交易日历确认，节假日不调用模型，也不生成空报告。数据桥失败时任务会失败退出，不会把演示数据发布成正式晨报。

当前 Windows 电脑已经部署该任务；只有需要修复或变更运行时间时才需再次执行安装命令。

### Ubuntu VPS user timers

Ubuntu VPS 不再通过 crontab 调度 AStock；AStock 的 canonical unit 位于 `systemd/`，部署副本位于 `~/.config/systemd/user/`：

- `astock-xhs-source.timer`：工作日 08:00 启动 source-only 主链；交易日成功后只启动研究密封。
- `astock-cninfo-precollect.timer`：工作日 07:50 预采集，保持与发送隔离。
- source timer 为 `Persistent=false`；主机关机错过时间不会追补，需先只读核验后再决定人工恢复。
- `astock-xhs-draft.timer` 与 `astock-morning-notify.timer` 已从可部署模板目录移出；无触发时间的 timer 会成为无效单元，不能作为“默认 inactive”模板安装。只读历史模板见 `docs/retired-systemd/`。已有部署的退役须另行授权：保留相应 service 与密封成功链，归档并 mask 旧 timer，不启停研究或发送任务。

草稿只由明确的人工按需操作构建；晨报通知由研究密封成功链触发，不另设通知 timer。模块边界与旧入口兼容规则见 [核心模块](docs/CORE_MODULES.md)。

`astock-xhs-draft.service` 的 `held` 是失败关闭的业务状态，通常仍以进程退出 0 结束；仅在有人显式请求草稿时检查 `work/xhs/last-run.json` 的 `draft_ready` / `noop` / `held`，不能把未运行的按需草稿报成每日故障。定时报告与通知分别以密封件、发送事件和平台回执验收，不以 systemd 的 `success` 替代业务证据。

source runner 只负责 OS 进程锁、固定编排、证据门禁和有界内部重试，不负责发送。只能由带有明确环境边界的 unit 或经授权的人工操作调用；不得通过手工触发来做健康检查。兼容名称 `install-morning-schedule.sh` 与 `repair-morning-schedule.sh` 均已退役为退出 78 的说明入口，不能安装 cron/watchdog、拉取代码、采集或补跑。实际单元变更须单独审查并核对已生效 user 单元。

如需移除 Windows 计划任务：

```powershell
.\scripts\install-schedule.ps1 -Remove
```

## 数据链路与 MCP 说明

```text
晨报工作流
  -> 固定 stdin 桥（项目 .venv）
  -> AkShare 数据核心
  -> 标准晨报数据包 + 来源
  -> 程序核验基础版
  -> 可选的受限 Hermes 结构化分析增量（定时默认关闭）
  -> 程序确定性编译
  -> 程序质量门禁
  -> 正式报告或明确阻断
```

自动晨报继续使用固定 stdin 数据桥；Hermes 只消费编排器准备好的证据，不直接获得任意 MCP、command、path、server、tool、URL 或模型路径。显式回滚模式下的 `pi-research` 通过项目内固定客户端连接 `mcp/stock_research_server.py`，精确校验六个工具名和受控核心股票池。Qlib 只读适配器从固定结果/MLflow 根读取最近验证产物；Kronos 通过独立固定 Python 环境和本地 mini 权重推断最多三组 OHLCV 序列。详细协议见 `mcp/README.md`、`mcp/contract.schema.json` 和 `ARCHITECTURE.md`。

## 晨报质量规则

- 报告完整保留编号 1—7，包含行情/资金/新闻/财务完整度、市场状态、AI/半导体/新能源/机器人主线、三只股票及今日风险。
- 具体数字必须关联来源和时间；来源晚于固定截止时间时不能使用。
- `[事实]` 只表示来源直接支持；影响方向、主题受益、涨跌原因和逻辑状态一律属于 `[推断]`，并提供反证。
- 北向实时数据不可获得时必须明确说明，不能拿历史值或其他资金口径代替。
- 数据完整度百分比必须逐字来自数据桥模块评分，或有可复算的分子/分母；不能凭主观填 100%。
- 不能编造主力资金、龙虎榜、技术位、目标价或止损价。

## 常用命令

| 命令 | 用途 |
|---|---|
| `npm run data:setup` | 创建 `.venv` 并安装固定数据依赖 |
| `npm run data:test` | 真实数据 dry-run，不调用模型 |
| `npm run morning` | 正式运行每日晨报工作流 |
| `npm run morning:dry` | 普通上下文预演；桥失败时允许安全降级 |
| `npm run research:dry` | 列出六个受控研究工具与备忘录契约，不联网、不加载 Kronos |
| `npm run task:catalog` | 查看固定任务路由与禁止自动化的敏感能力 |
| `npm run task:status` | 查看不含问题正文/结果正文的任务审计状态 |
| `npm run notify:status` | 查看 Hermes 通知 outbox 状态 |
| `npm run notify:drain` | 投递尚未开始发送的通知事件 |
| `.\scripts\install-schedule.ps1` | 部署工作日 08:00 的本机计划任务 |
| `npm run doctor` | 离线检查所选执行器、固定数据桥和包版本 |
| `npm run doctor:live` | 额外验证所选云端模型，会产生少量用量 |
| `npm run validate` | 检查配置、Prompt 和契约 |
| `npm test` | 运行程序测试 |

## 目录

```text
AStock_AI_System/
├── .pi/                    # Pi 开发/显式回滚规则与受控研究扩展
├── .venv/                  # 项目 Python 环境；安装后生成
├── agents/                 # 研究与证据审校 Prompt
├── config/                 # 股票池、策略、风险、数据与工作流配置
├── data/                   # 示例和可选人工输入
├── mcp/                    # 晨报桥、Stock Research MCP、Qlib/Kronos 受控适配器
├── reports/daily/          # 通过门禁的正式晨报
├── reports/provisional/    # 数据不足的待核验稿
├── scripts/                # 安装、运行、诊断和计划任务脚本
├── src/                    # Node 工作流核心
├── test/                   # 自动测试
└── work/                   # 每次运行材料、通知 outbox 与隔离的编排审计
```

## 风险和维护

- AkShare 的上游接口可能变化、限流或暂时不可用。先用 `npm run data:test` 判断数据层是否正常。
- “主力资金”等数据受供应方口径限制，不代表真实交易意图。
- 机构持股接口缺少公开披露时间时，只能把日期解释为持股期末，不能证明历史时点已经可得。
- Qlib score 是历史截面的相对排名；Kronos 是实验性模型情景；两者都不是确定涨跌或交易信号。
- Hermes、回滚模型、云服务和 Python 包会变化；定期运行 `npm run doctor`，升级依赖前先跑完整测试。
- 不要把 `auth.json`、API key、Cookie、Token、`.env` 或其他凭据提交到项目。
