# research-data

`research-data` 是一套本地研究数据快照与离线验证工具。它把 Qlib 量价数据和经校验的市场数据固化到 SQLite，供复盘、筛选和回测使用；它不负责自动交易、自动发布，也不负责向 Research Ledger 写入业务记录。

项目的研究结论与治理边界分别见 [RESEARCH_FINDINGS.md](RESEARCH_FINDINGS.md) 和 [policy/EDGE_POLICY.md](policy/EDGE_POLICY.md)。当前公开策略没有形成可投入资本的稳定 edge，相关业务写入调度保持冻结。

## 数据链路

生产链路按 `Asia/Shanghai` 时间由用户 crontab 调度：

```text
22:00  独立 Qlib 更新任务
  ↓
22:30  读取 Qlib 目标日与已有快照/封存
  ↓
       尝试封存当日 AkShare 市场截面
  ↓
       用目标日的同日封存数据组装 daily_snapshot.py
  ↓
snapshot.db（SQLite，本地 append-only 快照）
```

- 22:00 的 Qlib 更新脚本位于 Qlib 部署目录，不属于本仓库。它检查上游 release、验证下载包，并以目录替换方式安装新一代数据。
- 22:30 的任务先从本地 Qlib 确定最新完整交易日并检查已有快照/同日封存，再把 AkShare 市场活跃度和行业摘要按其自身统计日期封存为不可变暂存文件。
- 目标日期尚无快照时，只能加载同交易日的 AkShare 封存文件，再采集其余带有明确 `observed_at` 语义的来源并尝试插入一行。Qlib 尚未追上当日数据时，暂存文件会留给后续自然运行使用。
- 目标日期已有快照时仍会尝试封存当天 AkShare 数据，然后返回 `skipped_existing`。当天封存失败会记录在 `akshareStage` 和 `warnings`，但不会阻断已验证旧行的幂等结果。
- 调度的权威配置是生产用户的 crontab。文档只描述依赖顺序，不复制完整 crontab 命令；部署检查时应使用 `crontab -l` 核对。

这是 cron 驱动的批处理项目，没有常驻的 `research-data.service`。缺少常驻业务服务不是故障；应通过 cron、结构化运行记录和 SQLite 状态判断健康度。

## 快照安全模型

`daily_snapshot.py` 不接受历史日期参数，只处理本地 Qlib 当前最新的完整交易日。

写入前会执行以下门禁：

1. 四只参考证券的 `close`、`high`、`low`、`amount` 必须指向同一最新交易日。
2. Qlib release tag 与数据目录 inode 在扫描前后必须保持一致，防止读取更新中的混合代数据。
3. 市场覆盖率、关键来源完整性、有限数值和严格 JSON 必须通过校验；AkShare 封存至少需要 50 个结构完整的行业板块，以及有效的涨跌、平盘和涨跌停核心计数。
4. Qlib 交易日必须与一个已封存、哈希和内部日期均通过校验的 AkShare 市场截面一致；绝不使用较新交易日的实时页面代替。
5. SQLite 在事务前后执行 `PRAGMA quick_check`。

AkShare 暂存文件默认位于 `~/.local/state/research-data/akshare-market/YYYY-MM-DD.json`。目录必须由运行用户拥有且权限为 `0700`，文件必须为 `0600`；文件包含严格 JSON、采集时间窗、payload SHA-256 和整条记录 SHA-256。同日相同 payload 是幂等操作，同日不同 payload、符号链接、权限异常、哈希错误或字段篡改都会失败关闭，既有文件不会被覆盖。

`snapshot` 表以交易日为主键，写入策略为只追加：

- 新目标日期通过全部门禁后使用普通 `INSERT` 新增一行。
- 已存在且内部日期、来源状态和 JSON 均有效的目标日期返回 `skipped_existing`；该快速路径会先尝试当天 AkShare 封存，但在其余外部来源采集前结束，并且不替换旧行。
- 若目标日封存已经有效，当天新封存暂时失败不会阻断目标日的合法追加；最终状态会保留 `warnings`，供验收发现下一日证据缺口。
- 底层存储层只允许完全相同 payload 的幂等重试；同一日期的不同 payload 会被拒绝为冲突。
- 旧的异常历史行不会被自动改写，也不会阻止新的干净日期追加。

因此，失败不能通过覆盖已有数据“自愈”。如果日期、来源、Qlib generation、完整性或 SQLite 状态不满足要求，本次运行会失败关闭，已有快照保持不变。

## 本地状态与运行结果

默认运行文件：

| 内容 | 默认位置 |
|---|---|
| SQLite 快照 | `~/research-data/snapshot.db` |
| 最新结构化状态 | `~/.local/state/research-data/last-run.json` |
| AkShare 不可变暂存 | `~/.local/state/research-data/akshare-market/` |
| cron 追加日志 | 仓库内 `snapshot.log` |
| Qlib 数据 | `~/qlib/data/cn_data` |

测试或隔离运行可使用以下环境变量改变写入位置：

- `RESEARCH_DATA_SNAPSHOT_DB`
- `RESEARCH_DATA_STATE_DIR`
- `RESEARCH_DATA_AKSHARE_STAGE_DIR`

每次运行会把同一结构化结果写到标准输出、syslog 和 `last-run.json`。常见状态包括：

- `inserted`：新日期已追加。
- `skipped_existing`：目标日期已有经过验证的不可变快照。
- `deferred`：本次已尽可能封存当前 AkShare 数据，但 Qlib 目标日对应的不可变暂存尚不存在；退出码为 69，数据库不写入，应等待后续自然调度。
- `rejected`：数据或完整性门禁未通过。
- `temporary_failure`：进程锁或 SQLite busy 等临时条件阻止运行。
- `usage_error`：调用方式不受支持，例如传入历史日期。
- `failed`：未分类异常。

`sourceAsOf.akshareActivity.kind=trade_date` 与
`liveAndRollingSources.sources` 中的 `akshareSectors` 为兼容既有读取方而保留；新增的
`provenance=immutable_stage` 和 `sourceAsOf.akshareStage` 是封存来源的权威元数据。
只要结果含有 `warnings` 或 `akshareStage.status=unavailable`，即使主状态是
`inserted` 或 `skipped_existing`，也应作为暂存链路告警调查。

排障时应先核对三处记录的 `runId`、`status`、`target`、Qlib tag、payload SHA-256 和时间字段是否一致，不要先手工重跑生产任务。

### 公告采集与诊断

巨潮公告使用同一直接 HTTPS Session 获取官方证券映射和公告列表，不切换代理、
不猜测缺失的 orgId、不改日期或市场筛选范围。每个失败请求最多尝试 3 次，
连接/读取超时为 5/15 秒，退避 1/2 秒；仅超时、连接错误和 HTTP 429/5xx 重试。
403、重定向、非法 JSON、证券错配、缺失字段及不完整页面均失败关闭。
逐证券重试不会反复抓取已经成功的证券，但任一证券最终失败仍拒绝整个快照。
只有明确的 totalAnnouncement=0 才允许空公告列表，接口故障不等于没有公告。

公告失败状态包含 `sourceErrors`，只保留来源、请求阶段、证券代码、错误类型、
尝试次数、HTTP 状态及有限枚举 `validationReason`；不会记录原始异常、URL 查询串、响应正文或请求头。
来源完整性拒绝同时附带 `target` 和 `qlibTag`，便于区分日期与网络问题。
故障注入测试只使用模拟请求，不运行生产快照；修复后仍等待自然调度验收。

2026-09-08 起，公告 `date` 明确是源披露日期标签，不是首次观察时间。每条记录
新增 `dateKind`、实际 `observedAt` 和 `nextDayDisclosure`。仅下一自然日的午夜
标签且证券/机构/附件路径日期与 ID 一致时兼容；放行依据存入 `disclosureEvidence`。
不推断假期后的下一交易日，不允许更远未来，不改写源日期或观察时间。
这只验证 API 元数据相互一致，不表示已下载或鉴真 PDF。
详见 [公告日期兼容与证据说明](CNINFO_FIX_NOTES.md)。

### 概念数据采集与诊断

概念数据继续使用原百度 HTTPS 端点、原证券集合与市场身份，改为正常 Session
请求及规范参数编码；不切换来源或代理，不关闭证书校验。逐证券有限重试，仅
超时、连接错误及 429/5xx 可重试；403、重定向、证书错误和 schema 错误直接失败。

严格检查证券、成功码、类别和名称，`概念` 类别必须明确存在；显式空列表可以
保留，类别缺失不能当作没有概念。响应大小和条数有上限，超限拒绝而非截断。
一只证券失败会保留其它成功结果，但整个快照仍被完整性门禁拒绝；失败原因以
白名单形式并入 `sourceErrors`，不反复重取已经成功的证券。
详见 [概念源修复与证据说明](CONCEPT_FIX_NOTES.md)。

## 开发环境

生产使用 Python 3.12 和仓库内 `.venv/`。直接依赖固定在 [requirements.txt](requirements.txt)；虚拟环境、运行日志和生产 SQLite 均不纳入 Git。

关键文件：

- `daily_snapshot.py`：采集、校验、锁、状态记录与 append-only 持久化。
- `qlib_io.py`：严格读取并对齐 Qlib 二进制特征。
- `config/stocks.json`：研究观察池及其可证伪假设。
- `review.py`、`screen.py`、`chip.py`：复盘、筛选与本地研究工具。
- `strategies.py`、`backtest*.py`：历史验证工具，不构成生产交易信号。
- `ARCHITECTURE.md`：完整架构与数据流说明。

## 安全离线测试

单元测试使用临时目录、模拟数据源和临时 SQLite，不需要生产数据库，也不应调用外部行情或模型。为进一步隔离默认路径，可在仓库根目录执行：

```bash
test_root="$(mktemp -d)"
RESEARCH_DATA_SNAPSHOT_DB="$test_root/snapshot.db" \
RESEARCH_DATA_STATE_DIR="$test_root/state" \
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py' -v
rm -rf "$test_root"
```

该命令只用于测试。不要把 `daily_snapshot.py` 本身当作测试命令：直接运行它会读取本地 Qlib，在新目标日期上连接外部数据源，并可能写入生产 SQLite。

## 运维与恢复边界

- 先检查 Qlib 更新日志、`last-run.json`、`snapshot.log` 和 syslog，再决定是否需要维护。
- `skipped_existing` 是正常幂等结果，不需要重跑；仍应检查其 `akshareStage` 字段，确认当天暂存成功。
- `deferred` 通常表示 Qlib 与实时页面存在固有发布时差。先确认当前交易日暂存文件已安全生成，再等待 Qlib 追上；不要把较新日期 payload 改名成目标日期，也不要手工补跑生产任务。
- `rejected` 时应定位日期分歧、覆盖率、来源错误或 generation 变化；不要修改旧行来绕过门禁。
- `temporary_failure` 时应确认是否仍有合法进程或数据库锁；不要并发启动第二个生产任务。
- 新日期采集失败时不会插入半成品。修复外部条件后，应优先等待下一次自然调度；任何人工生产运行都需要单独批准。
- 本仓库没有数据库恢复自动化，也不把 `snapshot.db` 纳入 Git。恢复介质、保留周期和恢复演练属于主机运维范围，不能从 Qlib 数据或 Git 仓库推定已有可用备份。
- 恢复时不得覆盖一个已存在但 payload 不同的日期。应先以只读方式验证数据库、目标日期和 payload 哈希，再制定单独的恢复方案。
- 部署暂存机制以前没有封存的日期保持为显式空缺。尤其不能用后一天抓取的 payload 伪造历史暂存；历史缺口只能在存在权威、可验证的同日来源时通过单独方案处理。

Research Ledger、Phase2 API 及其业务 timers 是独立系统。`research-data` 测试不得连接其生产 PostgreSQL；本项目的定时快照也不应被解释为解除 `FROZEN` 或允许自动交易。
