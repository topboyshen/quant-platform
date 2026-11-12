# M4 影子部署与验收 Runbook

状态：**2026-11-13 用户授权按方案 C 部署**。M3 影子验收 PASS（缩短为“1 个完整交易日（11-12）+ 部署 smoke”），M4 于 11-13 收盘前部署。
M3 冻结版本为 `0.3.7-m3-sourcerole-4c3f1692`（`be49796`）；M4 代码在其之上开发，部署时另行冻结
M4 commit/tag，窗口内不得再改代码。

本文只描述影子运行：qp 只写自己的 `signals` schema 与 `/var/lib/qp*`，不写旧库、不发 Telegram、
不向 dedirock POST、不启停旧 timer。任何生产部署/定时器变更都需要用户单独授权。

## 1. 部署物

| 类别 | 路径 |
|---|---|
| migration | `app/backend/migrations/versions/0004_m4_signals_schema.py`（head `0004`） |
| 只读导出器 | `app/backend/qp/collect/signals.py`（`python -m qp.collect.signals`） |
| 影子任务 | `qp/signals/{eod,watchdog,repair,outcomes,approved}.py`，注册于 `qp/jobs/signals.py` |
| 对比工具 | `qp shadow compare`（`qp/shadow/compare.py`） |
| 任务定时器 | `app/deploy/systemd/qp-signals-*.timer`（调用 `qp-job@<job>.service`） |
| 导出定时器 | `app/deploy/systemd/qp-signals-export*.timer` + `qp-signals-export@.service` |
| 配置样例 | `app/deploy/qp.env.example`（M4 段） |

任务清单：

| job | 时点（CST） | 行为 |
|---|---|---|
| `signals.eod` | 周一至周五 15:30 | 40 ETF 趋势扫描 + 风险门，写 `signals.*` |
| `signals.eod_retry` | 周一至周五 15:48 | 仅当同日最新 attempt 为 `failed/data_unavailable`，或同日 `partial/data_unavailable` 且三个信号计数均为 0 时重跑；绝不发起首次运行 |
| `signals.watchdog` | 周一至周五 15:40、15:50 | 终态审计，只写 `notify.outbox`（shadow） |
| `signals.total_amount_repair` | 周一至周五 15:47、15:57 | 补同日空 `total_amount` |
| `signals.outcomes` | 周一至周五 16:10 | 1/3/5/10/20 日结果 |
| `signals.execution_digest` | 每日 08:35 | 生成前一日摘要，写文件 + shadow 行，不 POST；每日运行才能覆盖周五（周六生成） |

导出：15:55、16:20 导出当日（`@today`）；次日 08:45 导出前一日（`@yesterday`，周二至周六）。
15:55 早于 15:48 重试结束，只作中途快照；16:20 与次日 08:45 才是对账输入。

## 2. 安装（需授权后执行）

### 2.1 目录与权限

```bash
# 父目录 /var/lib/qp-inbox 已是 ubuntu:qp 2750；子目录保持 setgid，qp 通过组读取
sudo install -d -o ubuntu -g qp -m 2750 /var/lib/qp-inbox/signals-legacy
sudo install -d -o qp -g qp -m 0750 /var/lib/qp/signals/digests
sudo install -d -o qp -g qp -m 0750 /var/lib/qp/shadow-reports
```

导出器以 `ubuntu` 运行（`ProtectHome=read-only` 只读 `/home/ubuntu/trading-radar`），只写
`/var/lib/qp-inbox`；不要用放宽 `/home/ubuntu` 权限的方式解决访问问题。

### 2.2 密钥

影子期使用独立测试密钥，**不要**安装 dedirock/Telegram 生产密钥：

```bash
sudo sh -c 'umask 027; openssl rand -hex 32 > /etc/qp/approved_signals_hmac'
sudo sh -c 'umask 027; openssl rand -hex 32 > /etc/qp/dedirock_digest_hmac'
sudo chown root:qp /etc/qp/approved_signals_hmac /etc/qp/dedirock_digest_hmac
sudo chmod 0640 /etc/qp/approved_signals_hmac /etc/qp/dedirock_digest_hmac
```

密钥值不得出现在命令输出、工单、聊天或日志中。`qp.env` 只写路径：

```ini
QP_APPROVED_SIGNALS_HMAC_FILE=/etc/qp/approved_signals_hmac
QP_APPROVED_SIGNALS_MAX_SKEW_SEC=300
QP_EXECUTION_DIGEST_HMAC_FILE=/etc/qp/dedirock_digest_hmac
QP_EXECUTION_DIGEST_DIR=/var/lib/qp/signals/digests
QP_RISK_OVERRIDE_FILE=/var/lib/qp/radar_risk_override.json
QP_NOTIFY_MODE=shadow
```

### 2.3 代码与 migration

部署版本必须是冻结 commit 的不可变构建；`qp db upgrade` 只新增 `signals` 表，不改旧库：

```bash
sudo -u qp env QP_APP_DIR=/opt/qp/current/backend /opt/qp/current/venv/bin/qp db upgrade
sudo -u qp /opt/qp/current/venv/bin/qp job list | grep '^signals\.'
```

### 2.4 systemd

```bash
sudo install -m 0644 app/deploy/systemd/qp-signals-*.service \
  app/deploy/systemd/qp-signals-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qp-signals-export.timer qp-signals-export-previous.timer \
  qp-signals-eod.timer qp-signals-eod-retry.timer qp-signals-watchdog.timer \
  qp-signals-repair.timer qp-signals-outcomes.timer qp-signals-digest.timer
```

所有 timer 均为 `Persistent=false`：停机不补跑，不产生补发或补算。

## 3. 上线自检

```bash
# 1) 只读导出（不写旧库；manifest 含每个文件的 sha256/bytes）
sudo systemctl start qp-signals-export@today.service
sudo journalctl -u 'qp-signals-export@today.service' -n 20 --no-pager

# 2) 任务已注册且可被 timer 调用
sudo -u qp /opt/qp/current/venv/bin/qp job list | grep '^signals\.'

# 3) 对比工具可运行（首日 15:30 前没有 qp run 时返回 BLOCKED 且 rc=2，属预期）
sudo -u qp bash -c 'set -a; . /etc/qp/qp.env; set +a; \
  /opt/qp/current/venv/bin/qp shadow compare --date "$(date +%F)" \
  --legacy /var/lib/qp-inbox/signals-legacy \
  --hmac-secret-file /etc/qp/dedirock_digest_hmac \
  --out /var/lib/qp/shadow-reports'
```

不要为自检手工触发 `signals.eod`：会多出一个 attempt，改变“最新 attempt”语义并污染当日对账。

## 4. 每日对账

对账日 D 的完整输入在 **D+1 08:45** 之后才齐（08:35 摘要 + 08:45 导出）。每天 08:50 执行
（周二至周六跑前一交易日；周一跑上周五）：

```bash
DAY=$(date -d yesterday +%F)
[ "$(date +%u)" = 1 ] && DAY=$(date -d 'last friday' +%F)
sudo -u qp bash -c 'set -a; . /etc/qp/qp.env; set +a; \
  /opt/qp/current/venv/bin/qp shadow compare --date '"$DAY"' \
  --legacy /var/lib/qp-inbox/signals-legacy \
  --hmac-secret-file /etc/qp/dedirock_digest_hmac \
  --out /var/lib/qp/shadow-reports'
```

同日 16:30 可先跑一次早期检查（C1–C8；C9 通常 WARN/BLOCKED），只用于提前发现漂移，
不作为最终结论。退出码：`0` PASS/WARN，`1` FAIL，`2` BLOCKED。

判定：

- `PASS`：全部适用项通过；`WARN` 只允许来源差异、文本差异、未提供密钥等非硬字段。
- `FAIL`：C1–C6 硬字段不一致，或 C10 违反。
- `BLOCKED`：导出缺失/损坏或上游当天不可评估。最多 2 天不计入窗口；连续 2 天 BLOCKED
  视为未通过，需人工裁决，不得自行重开窗口。
- 任一代码变更使 5 日窗口作废。

窗口：2026-11-13 ~ 2026-11-19（5 个交易日：11-13、11-16、11-17、11-18、11-19）；最后一天
11-19 的 C9 依赖 11-20 08:35/08:45，故全窗口结论最早在 2026-11-20 08:50。窗口内代码冻结在
部署时写入的 `RELEASE_SHA`（以 `/opt/qp/current/RELEASE_SHA` 为准）。

## 5. 安全不变量（C10）

每天确认：

```bash
# qp 侧 outbox 全 shadow、从未投递
sudo -u postgres psql -d qp -Atc \
  "select count(*) from notify.delivery_attempt;"
# 应为 0（影子期）
```

- qp 不写 `swing_macro.db`、`intake.sqlite3`、digests、`radar_risk_state.json`、
  `radar_risk_override.json`；导出器只读连接。
- 不启动/停止旧 systemd 单元，不写旧 `notification_outbox`。
- 无 Telegram 出站、无 dedirock POST；`QP_NOTIFY_MODE=shadow`。
- 日志、对比报告、工单中不得出现 token、chat_id、密钥值；导出器已对 payload 做脱敏。

## 6. 回滚

1. `sudo systemctl disable --now qp-signals-*.timer`。
2. 保留 `signals` schema 与 `/var/lib/qp*` 证据，不删表、不删导出。
3. 旧系统在影子期全程未被修改，无需恢复旧 timer；如曾为验收改动旧单元，按变更单单独恢复。
4. 记录回滚原因、最后一个 PASS/BLOCKED 日期与报告路径。

## 7. 变更控制

- M4 部署、migration、timer、`qp.env` 变更均需用户明确授权。
- 避开 22:30–23:10 与 07:30–08:20；EOD 相关操作放在 16:30 之后。
- 窗口内代码冻结；任何修复都要结束当前窗口，重新冻结版本并从第 1 个交易日重开。
- 禁止手工重跑、补算、补发自然日任务；`Persistent=false` 已确保不补跑。
