# 备份与恢复 Runbook

状态：**已执行恢复演练（2026-11-14 22:40 CST，PASS）**；异地副本已启用（Mac 每日拉取）。

## 1. 备份

| 项 | 值 |
|---|---|
| 任务 | `qp-job@ops.backup.service`（`qp-backup.timer`，每天 03:50 CST） |
| 位置 | `/var/lib/qp/backups/qp-YYYYMMDDTHHMMSS.dump`（`qp:qp 0600`） |
| 格式 | `pg_dump -Fc --no-owner`（custom format） |
| 保留 | `QP_BACKUP_KEEP_DAYS`，默认 14 天 |
| 记录 | job 结果含 `file/bytes/sha256`，可在任务中心查看 |

手工补一份（当天 dedup 已存在时）：

```bash
sudo -u qp bash -c 'set -a; . /etc/qp/qp.env; set +a; \
  /opt/qp/current/venv/bin/qp job run ops.backup --dedup-key manual-'"$(date +%Y%m%dT%H%M)"''
```

## 2. 恢复演练

脚本：`app/deploy/bin/qp-restore-drill`（生产安装于 `/usr/local/sbin/qp-restore-drill`）。

```bash
sudo /usr/local/sbin/qp-restore-drill \
  --out /home/ubuntu/qp-restore-drill-$(date +%F)/report.txt
# 选项：--dump <file>  --db <scratch>  --live qp  --release /opt/qp/current
#       --skip-acceptance  --keep
```

脚本做六件事，全程不写生产库：

1. `createdb qp_restore_drill owner qp`，以 qp 身份 `pg_restore --no-owner --exit-on-error`；
2. 在恢复副本上重放 `qp db upgrade`（用当前 release 的迁移，验证真实数据可升级）；
3. 比对 alembic head、schema/表集合（90 张）、signals 触发器数（10 个）；
4. 完整性：`decision_outcome`/`chain_decision` 孤儿 = 0；40 只 run 必须有 40 条决策；
5. 打印恢复点（最后 EOD 日、最后链日期、决策/结果/链/报告/用户数）；
6. 用当前代码对恢复副本跑 `qp signals acceptance`，FAIL 即演练失败。

**不要**用行数与生产对比：备份是时点数据，生产一直在动。

### 2026-11-14 演练记录

| 时间 | dump | 结果 | 说明 |
|---|---|---|---|
| 22:37 | `qp-20261114T035002.dump`（03:50 夜间） | 结构 PASS / 发现差异 | 该 dump 在 `0011` 迁移之前：`db upgrade` 可正常补到 0011；链数据为空（当日导入发生在其后） |
| 22:40 | `qp-20261114T223756.dump`（22:37 手工） | **PASS** | 0011 + 1,160 决策 / 3,700 结果 / 1,160 链；恢复 4s、升级 1s；acceptance 11-13 = WARN（仅 A8 历史 shadow） |

报告：`/home/ubuntu/qp-restore-drill-20261114/report-20261114T2240.txt`。

结论：夜间 dump 在迁移之后需要先 `qp db upgrade` 再切流量；这是恢复流程的固定一步，已写入 §4。

## 3. 异地副本

- 位置：Mac `~/Library/Application Support/qp-offsite-backups/`（不放在 `~/Documents`：launchd 进程受 macOS TCC 限制）
- 调度：LaunchAgent `com.mirasim.qp-offsite-backup`，每天 09:20 拉取最新 dump，保留 14 份
- 脚本：`~/.local/bin/qp-offsite-backup.sh`；拉取后本地/远端 sha256 必须一致，否则失败退出
- 日志：`~/Library/Logs/qp-offsite-backup.log`
- 手工验证：

```bash
launchctl kickstart -k gui/$(id -u)/com.mirasim.qp-offsite-backup
tail -5 ~/Library/Logs/qp-offsite-backup.log
```

2026-11-14 22:42 实测：`up to date: qp-20261114T223756.dump`，exit 0，本地文件 4.0 MB。

## 4. 灾难恢复（新主机或原主机重建）

```bash
# 1) 安装 release（与生产同版本）并准备 venv/依赖，见部署脚本
# 2) 建库与角色
sudo -u postgres psql -c "create role qp login"          # 若不存在
sudo -u postgres psql -c "create database qp owner qp"
# 3) 恢复
sudo -u qp pg_restore --no-owner --exit-on-error -d qp /path/qp-YYYYMMDDTHHMMSS.dump
# 4) 迁移到当前 head（备份可能早于最新 migration）
cd /opt/qp/current/backend && sudo -u qp env QP_DATABASE_URL=postgresql+psycopg:///qp \
  /opt/qp/current/venv/bin/qp db upgrade
# 5) 写入 /etc/qp/qp.env，启动 qp-api 与 timers
# 6) 验收
sudo -u qp bash -c 'set -a; . /etc/qp/qp.env; set +a; \
  /opt/qp/current/venv/bin/qp signals acceptance --date <最后交易日>'
```

验收后再开 timer；恢复副本上的 `qp signals acceptance` 不允许 FAIL。

## 5. 边界

- 演练只读生产、只写 scratch 库，结束即 `drop database`（`--keep` 可保留排查）。
- dump 含用户密码哈希与通知正文，按机密对待：不入 git、不发聊天、不放到 `~/Documents`。
- 异地副本只有 Mac 一份；如 Mac 长期离线，应在另一台常在线主机再加一份拉取。
