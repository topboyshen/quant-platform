# M6 报告模块影子部署与验收 Runbook

状态：**开发中（M6.1–M6.6），未部署**。部署需单独授权；真实 Telegram 发送不在 M6 开启。

影子期 qp 只读旧报告文件与 Hermes ledger，只写自己的 `reports`/`notify` schema；不写
`trading-radar` 状态、不改 Hermes cron、不发消息、不触发旧任务。

## 1. 部署物

| 类别 | 路径 |
|---|---|
| migration | `app/backend/migrations/versions/0010_m6_reports_schema.py`（head `0010`） |
| 归档 | `qp/reports/archive.py`（冻结、不可覆盖、投递/回执分离） |
| 日报 | `qp/reports/legacy.py` + `qp/reports/daily.py`（复刻旧 `report_binding` 校验） |
| 摘要 | `qp/reports/digests.py`（研究摘要按收盘日；执行摘要引用 M4） |
| Hermes | `qp/reports/hermes.py`（envelope + `.raw` 正文校验） |
| 投递/回执 | `qp/reports/delivery.py`、`qp/reports/receipts.py` |
| 对比 | `qp/reports/compare.py` + `qp reports compare`（S1–S8） |
| 任务 | `qp/jobs/reports.py`：`reports.import_daily|research_digest|execution_digest|profit_summary|health_summary|shadow_deliver|import_receipts|compare` |
| 定时器 | `app/deploy/systemd/qp-reports-*.timer`（8 个，全部 `Persistent=false`） |
| 配置样例 | `app/deploy/qp.env.example`（M6 段） |

## 2. 源访问前置（只读，需单独授权）

以下 ACL 只加读权限，不改属主/模式：

```bash
# 日报状态目录（0700 ubuntu）与研究摘要 JSON
sudo setfacl -m u:qp:x /home/ubuntu/trading-radar/data
sudo setfacl -m u:qp:rx /home/ubuntu/trading-radar/data/daily-research-summary
sudo setfacl -R -m u:qp:rX /home/ubuntu/trading-radar/data/daily-research-summary/1
sudo setfacl -m u:qp:r /home/ubuntu/trading-radar/data/research_digest.json

# Hermes ledger（0600 ubuntu）
sudo setfacl -m u:qp:x /home/ubuntu/.hermes /home/ubuntu/.hermes/cron
sudo setfacl -m u:qp:r /home/ubuntu/.hermes/cron/deliveries.db \
  /home/ubuntu/.hermes/cron/executions.db
```

回滚：对应目录/文件执行 `setfacl -x u:qp ...`。旧系统数据全程只读。

## 3. 目录与环境变量

```bash
sudo install -d -o qp -g qp -m 0750 /var/lib/qp/report-reports
```

`/etc/qp/qp.env`（root:qp 0640）追加：

```ini
QP_LEGACY_DAILY_DIR=/home/ubuntu/trading-radar/data/daily-research-summary/1
QP_RESEARCH_DIGEST_JSON=/home/ubuntu/trading-radar/data/research_digest.json
QP_HERMES_ORIGINALS_DIR=/home/ubuntu/trading-radar/data/daily-research-summary/1/originals
QP_HERMES_DELIVERIES_DB=/home/ubuntu/.hermes/cron/deliveries.db
QP_HERMES_EXECUTIONS_DB=/home/ubuntu/.hermes/cron/executions.db
QP_REPORTS_REPORT_DIR=/var/lib/qp/report-reports
```

## 4. systemd

```bash
sudo install -m 0644 app/deploy/systemd/qp-reports-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  qp-reports-daily.timer qp-reports-research-digest.timer qp-reports-execution-digest.timer \
  qp-reports-profit.timer qp-reports-health.timer qp-reports-shadow-deliver.timer \
  qp-reports-receipts.timer qp-reports-compare.timer
```

| timer | 时点（CST） | job |
|---|---|---|
| `qp-reports-profit.timer` | 工作日 15:20 | `reports.import_profit_summary` |
| `qp-reports-health.timer` | 工作日 15:45 | `reports.import_health_summary` |
| `qp-reports-daily.timer` | 工作日 16:40 | `reports.import_daily` |
| `qp-reports-research-digest.timer` | 每日 16:50 | `reports.import_research_digest` |
| `qp-reports-execution-digest.timer` | 每日 09:00 | `reports.import_execution_digest` |
| `qp-reports-shadow-deliver.timer` | 每日 18:30 | `reports.shadow_deliver` |
| `qp-reports-receipts.timer` | 每日 18:40 | `reports.import_receipts` |
| `qp-reports-compare.timer` | 每日 18:50 | `reports.compare` |

全部 `Persistent=false`：停机不补跑、不补发。

## 5. 上线自检

```bash
sudo -u qp env QP_APP_DIR=/opt/qp/current/backend /opt/qp/current/venv/bin/qp db upgrade
sudo -u qp /opt/qp/current/venv/bin/qp job list | grep '^reports\.'

for j in reports.import_daily reports.import_research_digest reports.import_execution_digest \
         reports.import_profit_summary reports.import_health_summary \
         reports.shadow_deliver reports.import_receipts; do
  sudo -u qp bash -c 'set -a; . /etc/qp/qp.env; set +a; \
    /opt/qp/current/venv/bin/qp job run '"$j"
done

sudo -u qp bash -c 'set -a; . /etc/qp/qp.env; set +a; \
  /opt/qp/current/venv/bin/qp reports compare --date "$(date +%F)" \
  --out /var/lib/qp/report-reports'
```

期望：`S1–S5` PASS/WARN，`S6/S7` 全 shadow，`S8` 源文件未变；任何 FAIL 先查报告 `diffs`。

## 6. 每日对账（S1–S8）

- S1 日报与旧 `<date>-v1.json` 逐字段一致（payload 摘要相同）
- S2 研究摘要的行情/宏观/策略段落一致（允许 `generated_at` 差异）
- S3 执行摘要引用 M4 `signals.execution_digest` 的 sha256/body
- S4 profit/health 与旧 envelope 正文一致（旧端重试未归档记 WARN）
- S5 冻结报告 `content_sha256` 自洽
- S6 投递全 shadow、`delivery_attempt=0`
- S7 回执与 `send_result` 分离；未投递的 delivery 不得有 `received` 回执
- S8 旧源文件未被改动（sha256 与归档时一致）

判定：全 PASS 记通过；WARN 只允许时间戳/重试差异；FAIL 需人工裁决；BLOCKED（源缺失）
最多 2 天，连续 2 天视为未通过。

## 7. 安全不变量

- 不写 `daily-research-summary/1`、`research_digest.json`、Hermes DB；全部只读打开。
- 不启动/停止旧 timer，不改 Hermes cron jobs。
- 不发 Telegram、不 POST dedirock；`report_delivery.mode` 只能 `shadow`。
- 日志、报告、工单不出现 token、chat_id、密钥值。

## 8. 回滚

1. `sudo systemctl disable --now qp-reports-*.timer`。
2. `/opt/qp/current` 切回上一版本，重启 `qp-api`。
3. 保留 `reports` schema 与报告文件；不删表、不删证据。
4. 如已授权 ACL：按 §2 撤销；旧系统未被修改。

## 9. 变更控制

- M6 部署、migration、timer、`qp.env` 与 ACL 均需用户明确批准。
- 避开 07:45–08:25、15:00–16:25、21:40–23:15；部署放在 16:30 之后。
- 真实 Telegram 投递不在 M6 自动开启；需单独授权并先处理 token 轮换。
- 窗口内代码冻结在 `RELEASE_SHA`；任何修复结束当前窗口并重新冻结。
