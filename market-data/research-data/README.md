# research-data

`research-data` 是一套本地研究数据快照与离线验证工具。它把 Qlib 量价数据和经校验的市场数据固化到 SQLite，供复盘、筛选和回测使用；它不负责自动交易、自动发布，也不负责向 Research Ledger 写入业务记录。

项目的研究结论与治理边界分别见 [RESEARCH_FINDINGS.md](RESEARCH_FINDINGS.md) 和 [policy/EDGE_POLICY.md](policy/EDGE_POLICY.md)。当前公开策略没有形成可投入资本的稳定 edge，相关业务写入调度保持冻结。

## 数据链路

生产链路按 `Asia/Shanghai` 时间由用户 crontab 调度：

```text
22:00  独立 Qlib 更新任务
  ↓
22:30  daily_snapshot.py
  ↓
snapshot.db（SQLite，本地 append-only 快照）
```

- 22:00 的 Qlib 更新脚本位于 Qlib 部署目录，不属于本仓库。它检查上游 release、验证下载包，并以目录替换方式安装新一代数据。
- 22:30 的任务从本地 Qlib 选择最新完整交易日；随后只在目标日期尚无快照时采集其余来源并尝试插入一行。
- 调度的权威配置是生产用户的 crontab。文档只描述依赖顺序，不复制完整 crontab 命令；部署检查时应使用 `crontab -l` 核对。

这是 cron 驱动的批处理项目，没有常驻的 `research-data.service`。缺少常驻业务服务不是故障；应通过 cron、结构化运行记录和 SQLite 状态判断健康度。

## 快照安全模型

`daily_snapshot.py` 不接受历史日期参数，只处理本地 Qlib 当前最新的完整交易日。

写入前会执行以下门禁：

1. 四只参考证券的 `close`、`high`、`low`、`amount` 必须指向同一最新交易日。
2. Qlib release tag 与数据目录 inode 在扫描前后必须保持一致，防止读取更新中的混合代数据。
3. 市场覆盖率、关键来源完整性、有限数值和严格 JSON 必须通过校验。
4. Qlib 交易日必须与 AkShare 市场活跃度的统计日期一致。
5. SQLite 在事务前后执行 `PRAGMA quick_check`。

`snapshot` 表以交易日为主键，写入策略为只追加：

- 新目标日期通过全部门禁后使用普通 `INSERT` 新增一行。
- 已存在且内部日期、来源状态和 JSON 均有效的目标日期返回 `skipped_existing`；该快速路径在外部来源采集前结束，不替换旧行。
- 底层存储层只允许完全相同 payload 的幂等重试；同一日期的不同 payload 会被拒绝为冲突。
- 旧的异常历史行不会被自动改写，也不会阻止新的干净日期追加。

因此，失败不能通过覆盖已有数据“自愈”。如果日期、来源、Qlib generation、完整性或 SQLite 状态不满足要求，本次运行会失败关闭，已有快照保持不变。

## 本地状态与运行结果

默认运行文件：

| 内容 | 默认位置 |
|---|---|
| SQLite 快照 | `~/research-data/snapshot.db` |
| 最新结构化状态 | `~/.local/state/research-data/last-run.json` |
| cron 追加日志 | 仓库内 `snapshot.log` |
| Qlib 数据 | `~/qlib/data/cn_data` |

测试或隔离运行可使用以下环境变量改变写入位置：

- `RESEARCH_DATA_SNAPSHOT_DB`
- `RESEARCH_DATA_STATE_DIR`

每次运行会把同一结构化结果写到标准输出、syslog 和 `last-run.json`。常见状态包括：

- `inserted`：新日期已追加。
- `skipped_existing`：目标日期已有经过验证的不可变快照。
- `rejected`：数据或完整性门禁未通过。
- `temporary_failure`：进程锁或 SQLite busy 等临时条件阻止运行。
- `usage_error`：调用方式不受支持，例如传入历史日期。
- `failed`：未分类异常。

排障时应先核对三处记录的 `runId`、`status`、`target`、Qlib tag、payload SHA-256 和时间字段是否一致，不要先手工重跑生产任务。

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
- `skipped_existing` 是正常幂等结果，不需要重跑。
- `rejected` 时应定位日期分歧、覆盖率、来源错误或 generation 变化；不要修改旧行来绕过门禁。
- `temporary_failure` 时应确认是否仍有合法进程或数据库锁；不要并发启动第二个生产任务。
- 新日期采集失败时不会插入半成品。修复外部条件后，应优先等待下一次自然调度；任何人工生产运行都需要单独批准。
- 本仓库没有数据库恢复自动化，也不把 `snapshot.db` 纳入 Git。恢复介质、保留周期和恢复演练属于主机运维范围，不能从 Qlib 数据或 Git 仓库推定已有可用备份。
- 恢复时不得覆盖一个已存在但 payload 不同的日期。应先以只读方式验证数据库、目标日期和 payload 哈希，再制定单独的恢复方案。

Research Ledger、Phase2 API 及其业务 timers 是独立系统。`research-data` 测试不得连接其生产 PostgreSQL；本项目的定时快照也不应被解释为解除 `FROZEN` 或允许自动交易。
