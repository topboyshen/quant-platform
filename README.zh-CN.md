# quant-platform 中文 README

> **依据**：`main` 分支 HEAD `6f8d2a288589ef564a73a21118459f7c8e81ade7`（与 `origin/main` 一致）；HEAD 提交时间 2026-11-24 20:43:59 +0800；读取时间 2026-11-24 22:42 +0800。
>
> **部署依据**：本地部署收据（证据目录，未随仓库发布）。当前生产为 `0.3.116-health-inventory-8375f5c8`，直接收据为 `qp-day-window-deploy-20261125/`；此前 `.108/.110/.111/.112` 均有直接收据。`0.3.109` 仅有间接记录。
>
> **关键文档**：`README.md`、`ARCHITECTURE.md`、`app/README.md`、`app/deploy/FREEZE-BASELINE-2026-11-24.md`、`app/deploy/KNOWN-LIMITATIONS.md`、`app/deploy/DECOMMISSION-ROADMAP.md`、`app/deploy/RELEASE-ACTIVATION.md`。
>
> **说明**：本文档是仓库中文说明，版本与部署依据见 §8；没有直接证据的内容标「待核实」，另见文末「不能确认」。英文入口见 [`README.md`](README.md)。

---

> **版本状态（截至 2026-11-25 08:45 CST）**
>
> - **当前生产：`0.3.116-health-inventory-8375f5c8`**，合并提交 `0a48377`（PR #66），2026-11-25 11:02 激活。
> - 冻结自 **2026-11-25 08:45 CST** 重新起算，最早 Phase 2 复核 **2026-12-09**；2026-11-24 是 `.105`–`.111` 的混合版本日，2026-11-25 盘前又连续发布 `.112`–`.116`，均不能算单版本干净日。
> - `#59`（`6f8d2a2`）已随 `0.3.112` 部署；`#61`（README）同版本；`#62`→`.113`；`#63`→`.114`；`#64`→`.115`；`#66`→`.116`。
> - 冻结期只允许事故修复、安全修复、证据纠正和文档；12-09 前不再为文案、列宽、概览措辞发版。

---

## 1. 这是什么

quant-platform 是 nnG8 量化研究与信号平台的统一仓库。它把原先分散的 trading-radar、QuantDinger、research 等栈作为**只读历史快照**导入，同时在 `app/` 下提供一个新内核：

- `app/backend`：FastAPI + SQLAlchemy 2 + Alembic，Python 包 `qp`，命令行 `qp`；
- `app/frontend`：Vue 3 + TypeScript + Vite + Naive UI，挂在 `/next/`；
- PostgreSQL 16：单库多 schema；
- `app/deploy`：systemd 单元、nginx 配置、发布/回滚脚本与运维 runbook。

它解决的问题不是「再做一个交易策略」，而是把**采集、研究、信号、报告、通知、运维**收进一个可审计、可回滚、边界清楚的内核。旧服务在 nnG8 上的运行路径不变；本仓库不向旧运行时写入。

一句话：**一个把量化研究与信号流程工程化的内核，研究结果只读，影子运行，不产生订单。**

## 2. 仓库结构

| 路径 | 角色 | 状态 |
|---|---|---|
| `app/backend` | FastAPI + SQLAlchemy 2 + Alembic，包 `qp`，CLI `qp` | 活跃内核 |
| `app/frontend` | Vue 3 + TypeScript + Naive UI，`/next/` | 活跃内核 |
| `app/deploy` | systemd、runbook、环境变量示例、发布脚本 | 活跃文档 |
| `market-data/research-data` | 研究/qlib 数据采集 | 活跃旧数据源 |
| `market-data/qlib-scripts` | 夜间 qlib 更新 | 活跃旧组件 |
| `research/astock` | AStock 早盘链 | M9 前的活跃旧组件 |
| `research/research-ledger` | 研究台账 | 冻结的只读导入源 |
| `research/research-system-phase2` | phase2 运行状态 | 冻结的只读导入源 |
| `signals/trading-radar` | QuantDinger/radar 旧栈 | 运行时已退役，数据保留 |
| `signals/signal-bus` | 旧信号总线 | M9 前的活跃旧组件 |
| `execution/quantdinger` | QuantDinger 供应商快照 | 运行时已退役 |
| `reports/daily-report-tracking` | 旧日报 | 冻结的只读源 |
| `ops/*`、`web/*` | 旧运维/面板/前端资产 | 混合状态，见退役路线图 |
| `archive/` | 只读归档区 | 暂空 |

来源与导入模式见根 `README.md` 的模块表；退役顺序与「停、观察、归档、删除」四阶段见 `app/deploy/DECOMMISSION-ROADMAP.md`。

## 3. 主要模块

### 3.1 后端与数据模型

单个 PostgreSQL 16 数据库，按 schema 划分：

| schema | 用途 |
|---|---|
| `core` | 用户、会话、服务令牌 |
| `market` | 标的、交易日历、指数收盘、数据源健康 |
| `signals` | EOD 运行、目标决策、记录链、影子 |
| `research` | 导入的研究数据、C13/ETF 评分影子 |
| `reports` | 报告快照 |
| `notify` | outbox 与投递状态 |
| `ops` | 作业定义/运行、备份、健康、告警、清单 |
| `execution` | 预留的执行镜像 |
| `archive` | 预留 |

迁移由 Alembic 管理（`qp db upgrade`）；仓库内当前迁移 head 为 `0017`（`app/backend/migrations/versions/0017_etf_score_shadow.py`）。鉴权为单管理员账户加服务令牌（`qpst_…`，仅存 sha256）；会话 Cookie 为 HttpOnly、Secure、SameSite=Lax、Path=`/next/`。

### 3.2 作业与调度

- 作业注册表 `qp.jobs.registry` 为每次调用写一条 `ops.job_run`。
- 每个作业用 PostgreSQL advisory lock 防止重叠运行；可选 dedup key 让重试幂等。
- `QP_JOB_GATES=1` 启用同日上游门禁：上游缺失、失败或产物日期不符时，下游作业记为 `blocked`。默认关闭。
- systemd 定时器位于 `app/deploy/systemd`；生产完整列表以主机上的 `systemctl list-timers --all` 为准。

### 3.3 信号链（record chain）

ETF 记录链是 `target_decision` 的受控后继：

- **双写**：`signals.eod` 先写 `target_decision`，再由 `signals.chain.register_run` 追加不可变的 `chain_*` 行。
- **实现**：`signals/chain.py` 是对 `chain_base`（身份/原语）、`chain_writer`（双写/结果）、`chain_snapshot`（快照组装）的兼容 facade；历史 `qp.signals.chain` 导入面不变。
- **身份**：确定性 `decision_uid`、输入证据摘要、universe 与批次版本。
- **快照**：`chain_snapshot` 是读取信封，`chain_snapshot_item` 承载漏斗/样本/进度/损失分桶。
- **证据链接**：`chain_evidence_link` 记录 `sidecar_kind=db_row`、期望/实际摘要，以及 `present/verified`、`present/failed`、`missing/pending`。
- **读取开关**：`QP_CHAIN_READS=1` 读快照；缺快照时回退 `target_decision` 并标 `source=legacy_fallback`、`fallback=true`；`QP_CHAIN_READS_FALLBACK=0` 恢复严格空响应。
- **只读归因**：`GET /api/signals/chain/attribution` 与 `qp chain attribution` 使用最新结果修订、当前资格/状态，按六个维度分组；合格样本不足 30 条时明确不给出绩效结论；`migrated_v1` 且无 `raw_signals` 的行记为 `unknown`，不做推断。

外部产物（模型文件、冻结输入、导出的评分载荷）不当作决策，使用单独的**产物证据清单**绑定主机、属主、权限、字节数、版本与 SHA-256；只有身份与期望一致才标 `verified`，没有期望时标 `unknown`。

### 3.4 研究影子

- C13 轮动影子（`signals.rotation_shadow`）：250 日 top-8，每日运行；`forward` 与 `replay` 账户隔离。
- 规则影子 v2（`signals.rule_shadow`）：每日放量突破，只做影子。
- ETF 评分影子（`research.etf_score_run` / `etf_score`）：离线冻结模型产出 `latest.json` 与 `latest-5d.json`，`qp` 严格校验产物与内容后导入；20 日与 5 日是两个独立的观察版本。
- 研究导入（台账、phase2、QuantDinger 历史）都是只读镜像。

以上都不会写订单、发通知或生成正式决策。

### 3.5 报告与通知

- 15:50 EOD 报告：市场背景、风控门、信号漏斗、研究资格。
- 17:40 ETF 研究观察：20 日/5 日评分名单、C13 账户与持仓、数据质量；明确为研究观察，与 EOD 运营报告分开。
- 投递走 `notify.outbox`；`QP_SIGNALS_NOTIFY_MODE=shadow|live` 控制是否真实发送；`QP_NOTIFY_KILL_SWITCH` 与禁用文件是硬停。
- 失败的 live 投递不会被静默改写：outbox 与 attempt 历史保留失败，恢复需显式人工操作。

### 3.6 前端

`/next/` 下的只读/运营页面按域分组：

- 总览：市场、信号、研究、报告、运维五个业务模块的截止时间、完成状态、缺口与证据；
- 行情：健康、标的、日历、指数收盘、覆盖、导入；
- 研究：回测、收益观察、AI、项目、ETF 评分；
- 信号：运行、ETF 池、趋势、结果、影子、规则影子、轮动影子、链对比、策略横向对比；
- 报告：报告中心、投递记录；
- 运维：任务、监控、告警、对比、服务、覆盖、令牌；
- 设置。

### 3.7 运维

`ops.job_run` 记录每次运行；健康检查、告警、备份（`pg_dump -Fc`，14 天）、异地副本、恢复演练、`qp decommission inventory` 清单工具都在 `app/deploy` 与 `ops` 相关模块中。恢复演练只恢复到 scratch 数据库，不写生产数据。

## 4. 数据与口径

### 4.1 数据来源

- 行情主源与备用源由 `market` 模块采集；名称解析以腾讯为主源，东方财富备用源在 nnG8 出口 IP 上不可达，属于已记录的已知限制（`app/deploy/KNOWN-LIMITATIONS.md` KL-001）。
- 影子结果评估在原行情源失败时可回退新浪 K 线（PR #42）。
- 研究台账、phase2、QuantDinger 历史均为只读导入，不反向写入。

### 4.2 时间口径

生产日时间表按中国标准时间（CST）编排，完整表见 `ARCHITECTURE.md` §4。主链路：

```
15:30 EOD → 15:55 链快照 → 16:12 5 日窗口
16:45/16:55 ETF 评分 → 17:40 合并研究报告
```

失败由下一个时间槽重试；失败记录保留在 `ops.job_run`，不会从观察分母中删除。

### 4.3 结果口径

- 持有期 `HORIZONS = (1, 3, 5, 10, 20)` 个交易日（`app/backend/qp/signals/chain_base.py`）。
- 资格策略 `eligibility.v1`；归因样本门槛 30 条合格样本（`SAMPLE_THRESHOLD = 30`）。
- 结果取最新 revision；未成熟、未成交或缺少净收益的记录不进入成熟样本。
- 历史 `migrated_v1` 行缺少 `raw_signals` 时，信号类型记 `unknown`，不推断。
- 归因是分组观察排序，不是加仓/停止建议；低于门槛只能作描述性参考。

### 4.4 证据口径

- **失败关闭**：严格导入契约、产物哈希、交易日历覆盖、缺快照处理，都不静默推进状态。
- **不可变历史**：链行与证据链接只追加；重跑增加 attempt/revision，不改写。
- **证据诚实**：文件存在但没有期望哈希时是 `unknown`，不是 `verified`；失败/跳过的运行仍留在观察分母。
- **影子优先**：新策略只读运行并给出明确指标；没有预注册评估不晋级。

## 5. 运行方式

> 以下命令摘自仓库文档（`app/README.md`、`app/deploy/*`），本文档未执行验证。生产细节以 nnG8 主机与部署收据为准。

### 5.1 前置条件

- Python 3.12；
- PostgreSQL 16（后端测试通过 `pgserver` 内嵌启动）；
- Node.js 与 npm（前端构建/测试）；
- 生产环境为 systemd + nginx，`qp-api` 监听 `127.0.0.1:18800`。

### 5.2 后端测试

```bash
cd app/backend
pytest                         # 单元套件；仓库资产测试默认跳过
pytest --run-assets -m assets  # 文档、锁文件、部署单元、前端源码相关测试
```

CI 还运行 ruff check/format、前端 Vitest/build、Playwright smoke、Gitleaks 全历史扫描与依赖审计；覆盖率门槛见 `.github/workflows/ci.yml`（当前为 85%）。

### 5.3 数据库与命令行

```bash
qp db upgrade     # 迁移到 head
qp db check       # 检查数据库是否在发布期望的 Alembic head
qp job list       # 列出注册作业
qp job run <name> # 手动运行一个作业
```

### 5.4 API 与前端

API 启动方式以 `app/deploy/systemd/qp-api.service` 为准：

```bash
uvicorn qp.api.main:app --host 127.0.0.1 --port 18800 --workers 1 --proxy-headers
```

前端：

```bash
cd app/frontend
npm ci
npm run dev      # 本地开发
npm run build    # vue-tsc + vite build
npm test         # Vitest
npm run smoke    # Playwright 冒烟（需要可登录的真实数据库）
```

### 5.5 发布

1. 先做数据库备份并执行 `qp db upgrade`；
2. 用 `app/deploy/bin/qp-release-venv` 按 `requirements-runtime.lock` 构建发布 venv；
3. 用 `app/deploy/bin/qp-release-activate` 切换 `current`：校验发布与数据库 head、原子切换、重启 `qp-api`、轮询健康、可选冒烟；任一步失败自动切回上一版代码与 venv；
4. 自动回滚**不降级数据库**；若已执行迁移，需按备份/恢复 runbook 单独处理。

## 6. 边界与免责

- 本项目是研究与信号工程平台，**不构成投资建议，也不承诺任何收益**。仓库与本文档不发布任何业绩、资金或实盘运营数据。
- ETF 评分、C13 轮动、规则影子、绩效归因都是**只读研究/观察**；不产生订单，不生成正式交易决策。
- **测试通过不等于策略有效**；CI 绿色只证明代码与测试，不证明自然样本、策略有效性或未来表现。
- **仓库有代码不等于已部署或已在实盘运行；合并提交也不等于部署。** 生产版本以部署收据为准；当前生产为 `0.3.115`（2026-11-25 08:44 收据），`#59` 已随 `.112` 部署。
- 旧模块是历史快照，按 `DECOMMISSION-ROADMAP.md` 冻结；第三方组件（QuantDinger、qlib 等）遵循其各自许可证与商标声明。仓库根目录未发现 LICENSE 文件，本仓库自身的授权方式需由维护者确认。
- 数据源可用性、交易日历与第三方接口都可能变化；已知限制见 `app/deploy/KNOWN-LIMITATIONS.md`。

## 7. 文档索引

- [Quant Platform 工程评分与优化方案](app/deploy/PLATFORM-SCORECARD-2026-11-25.md) (2026-11-25)
- `ARCHITECTURE.md` — 架构、数据流、生产日时间表、失败语义、恢复 RTO。
- `app/README.md` — 内核快速说明、模式与边界。
- `app/deploy/FREEZE-BASELINE-2026-11-24.md` — 冻结基线、生产发布记录、清单快照。
- `app/deploy/DECOMMISSION-ROADMAP.md` — 旧栈退役阶段与门禁。
- `app/deploy/KNOWN-LIMITATIONS.md` — 已接受的限制。
- `app/deploy/RELEASE-ACTIVATION.md` — 发布激活与回滚语义。
- `app/deploy/CHAIN-SHADOW-RUNBOOK.md` — 链快照、对比、归因命令。
- `app/deploy/M4-LIVE-RUNBOOK.md` — 通知 live/shadow 切换与停止。
- `app/deploy/M5-SHADOW-RUNBOOK.md` — 研究导入与 R1–R10 对比。
- `app/deploy/JOB-GATES.md` — 同日上游门禁与回退。
- `app/deploy/EVIDENCE-MANIFEST.md` — 外部模型/输入/导出的证据清单。
- `archive/README.md` — 归档政策。

## 8. 版本与依据

### 8.1 版本口径

- 仓库内没有 git tag；版本号来自本地部署收据与 `app/deploy/FREEZE-BASELINE-2026-11-24.md`。
- **当前生产基线：`0.3.116-health-inventory-8375f5c8`**，合并提交 `0a48377e5e5b677480484fa39f6289abec06da18`（PR #66，2026-11-25 10:44 +0800），11:02 激活。冻结窗口自 2026-11-25 11:03 CST 起，最早 Phase 2 复核 2026-12-09。[^freeze]
- `0.3.108` 是历史冻结基线；`.109` 仅有间接记录，`.110`–`.116` 均有直接部署收据。`.112` 包含 #59 C13 账户口径修正与 #61 README；`.113`–`.115` 为同日“曾失败”标记修正；`.116` 包含 #65 health/基准口径与 #66 容器告警修复。
- 更早的同日版本链与逐项改动见 [`app/deploy/RELEASE-NOTES-2026-11-24-zh-CN.md`](app/deploy/RELEASE-NOTES-2026-11-24-zh-CN.md)。

[^freeze]: `FREEZE-BASELINE-2026-11-24.md` 已更新为 `.116` 基线和 2026-11-25 11:03 重新起算的冻结窗口；2026-11-24 记录为 `.105`–`.111` 混合版本日。

### 8.2 #56–#66 逐项状态

> 「验收结果」只摘录对应部署收据原文；没有收据的项目标「待核实」，不自行推断。

| PR | 合并提交 | 部署版本 | 验收结果（收据摘录） | 依据收据 / 证据强度 |
|---|---|---|---|---|
| #56 | `b4754b3`[^sha56] | `0.3.109-entry-basis-dd8df677`（间接记录） | **待核实**（未找到 `0.3.109` 自身收据） | `0.3.110` 收据「上一版」；证据强度：间接 |
| #57 | `ed99f93`[^sha57] | `0.3.110-r10-attribution-27f18306` | `test_m5_compare.py` 8 passed；ruff 通过；`DB_CHECK_OK`→`ACTIVATE_OK`；health 200；`.110` active、firing 0；部署后手工重跑 research.compare：**PASS 11/11**；当日 EOD daily `live/sent`、ETF shadow `live/sent`、acceptance `PASS/natural`、chain window PASS 5/5 | `qp-r10-fix-deploy-20261124/DEPLOY-RECEIPT.md`；证据强度：直接 |
| #58 | `d103bc5`[^sha58] | `0.3.111-overview-chain-window-2c515008` | 后端 501 passed / 45 skipped；覆盖 88.29%；ruff 通过；`DB_CHECK_OK`→`ACTIVATE_OK`；health 200；生产只读 `_signals_module(2026-11-24)`：`status=ok`、`gaps=[]`；chain_window PASS 5/5；EOD success/blocked | `qp-overview-chain-window-deploy-20261124/DEPLOY-RECEIPT.md`；证据强度：直接 |
| #59 | `6f8d2a2` | `0.3.112-c13-readme-9c96e997` | C13 账户口径修正；后端 501 passed / 45 skipped；覆盖 88.29%；`DB_CHECK_OK`→`ACTIVATE_OK`；health 200；`_rotation_block` 返回 forward 账户 | `qp-c13-readme-deploy-20261125/DEPLOY-RECEIPT.md`；直接 |
| #60 | `3f585b5` | 未部署（纯文档） | 中文 README / 11-24 版本说明 | 仓库记录；无部署 |
| #61 | `4dc3de7` | `0.3.112-c13-readme-9c96e997` | README/CI 口径修正，随 #59 同版部署 | `qp-c13-readme-deploy-20261125/DEPLOY-RECEIPT.md`；直接 |
| #62 | `1db3128` | `0.3.113-same-day-failure-74e40fd0` | 同日曾失败标记；后端 503 passed / 45 skipped；覆盖 88.39%；CI 全绿 | `qp-day-window-deploy-20261125/DEPLOY-RECEIPT.md`；直接 |
| #63 | `e01936f` | `0.3.114-result-fail-e786abc1` | 识别 succeeded/result FAIL；CI 全绿 | `qp-day-window-deploy-20261125/DEPLOY-RECEIPT.md`；直接 |
| #64 | `ee310c9` | `0.3.115-day-window-e66f231f` | SQL 限定同日窗口；生产验证 11-24 research.compare run 13934 result FAIL → 13971 恢复；health 200 | `qp-day-window-deploy-20261125/DEPLOY-RECEIPT.md`；直接 |
| #65 | `ddba42c` | `0.3.116-health-inventory-8375f5c8` | health 返回 RELEASE_NAME；基准降级 warning；README/冻结基线更新 | `qp-health-benchmark-deploy-20261125/DEPLOY-RECEIPT.md`；直接 |
| #66 | `0a48377` | `0.3.116-health-inventory-8375f5c8` | 无 name 容器行不再告警；container:None resolved；health version=`0.3.116-health-inventory-8375f5c8`、package_version=0.1.0 | 同上；直接 |

- #56 的部署版本只能从 `0.3.110` 收据的「上一版」间接得到。`0.3.110` 发布内容清单包含 `backend/qp/signals/entry_basis.py`（#56 新增文件），可佐证 #56 代码已进入 `0.3.110`，但不能替代 `0.3.109` 自身的部署验收。[^56]
- #57–#64 的验收结果按对应收据摘录；#56 仍只有间接记录，#60 为纯文档未部署。
- 表中收据目录为本地部署证据，未随仓库发布；保留目录名以便追溯。

[^sha56]: 全 SHA `b4754b343ea01e8ea70f237ad4233d801207d317`，合并时间 2026-11-24 17:51:06 +0800。
[^sha57]: 全 SHA `ed99f93c30671fe245205d04b72d0431d7099c4a`，合并时间 2026-11-24 18:49:16 +0800。
[^sha58]: 全 SHA `d103bc55d5b0e555e2b2b9898b7ccbb795da3226`，合并时间 2026-11-24 19:12:17 +0800。
[^sha59]: 全 SHA `6f8d2a288589ef564a73a21118459f7c8e81ade7`，合并时间 2026-11-24 20:43:59 +0800。
[^56]: `0.3.110` 收据原文：「上一版：`0.3.109-entry-basis-dd8df677`」。未找到 `0.3.109` 的发布包、发布目录或自身收据；`0.3.110` 的回滚目标也写为 `.109`。`0.3.110` 的部署前备份为 `qp-pre-0.3.110-20261124T1851.dump`，据此可推断 `.109` 在 18:51 前已激活，但收据未记录其部署时间与验收结果。

## 9. 不能确认

1. **#56 的部署验收**：`0.3.109` 仍只有间接记录；其代码已进入 `.110`，但没有自身收据。
2. **#59 的部署状态**：已随 `0.3.112` 部署，收据见 `qp-c13-readme-deploy-20261125/`。
3. **更早 PR 的版本归属**：PR #20–#52 中，已核对到直接收据的有 #44（`0.3.103`）、#46（`0.3.104`）、#47（`0.3.105`）、#48（`0.3.106`）；其余未逐条核对到发布版本。
4. **当前生产环境**：当前为 `0.3.115`（2026-11-25 08:44 收据），health 200、firing 0；live 通知模式和定时器状态需上机核对。
5. **测试数量与覆盖率的最新实测值**：本文档未运行测试；收据中的数字属于各次发布时点；`ARCHITECTURE.md` §6 的测试数字是 2026-11-21 的记录，`app/README.md` 的覆盖率门槛 80% 与 CI 配置的 85% 不一致。
6. **仓库自身的开源/授权状态**：根目录没有 LICENSE 文件。
7. **远端仓库是否对外公开**：`origin` 为 `https://github.com/topboyshen/quant-platform.git`，可见性未核实。
