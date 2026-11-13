# M8 早报链影子部署与验收 Runbook

状态：**M8.1–M8.5 开发完成，未部署**。M8 交付的是**影子快照 + 对比**；把生成/relay/告警
真正迁到 qp 并停旧 timer，需要单独的切换授权（本文 §8 给出切换前置条件）。

影子期 qp 只读 AStock / signal-bus 的产物，只写自己的 `reports` schema；不触发旧任务、
不发 Telegram、不改旧 timer、不写旧目录。

## 1. 部署物

| 类别 | 路径 |
|---|---|
| 读取器 | `qp/morning/artifacts.py`（precollect/generation/seal/relay/alert，告警脱敏） |
| 分类 | `qp/morning/classify.py`（expected atoms vs 旧告警 atoms） |
| 快照 | `qp/morning/chain.py` → `reports.report(kind=morning_chain)` |
| 任务 | `qp/jobs/morning.py`：`reports.import_morning_chain` |
| 定时器 | `app/deploy/systemd/qp-reports-morning.timer`（工作日 08:25，`Persistent=false`） |
| 对比 | `qp reports compare` 自动包含 A1–A6（存在 `morning_chain` 报告时） |
| 配置样例 | `app/deploy/qp.env.example`（M8 段） |

无需新 migration：复用 `reports.report`（`0010`）。

## 2. 源访问前置（只读，需单独授权）

```bash
# AStock 产物
sudo setfacl -m u:qp:x /home/ubuntu
sudo setfacl -m u:qp:x /home/ubuntu/AStock_AI_System \
  /home/ubuntu/AStock_AI_System/work /home/ubuntu/AStock_AI_System/data
sudo setfacl -R -m u:qp:rX \
  /home/ubuntu/AStock_AI_System/work/orchestrator/states \
  /home/ubuntu/AStock_AI_System/work/cninfo-staging \
  /home/ubuntu/AStock_AI_System/work/research-seal \
  /home/ubuntu/AStock_AI_System/data/inbox

# signal-bus 产物
sudo setfacl -m u:qp:x /home/ubuntu/signal-bus /home/ubuntu/signal-bus/out
sudo setfacl -R -m u:qp:rX /home/ubuntu/signal-bus/out

# 告警状态
sudo setfacl -m u:qp:x /home/ubuntu/.local /home/ubuntu/.local/state
sudo setfacl -m u:qp:rx /home/ubuntu/.local/state/signal-bus
```

回滚：对应路径 `setfacl -x u:qp ...`。旧系统数据全程只读。

## 3. 环境变量

`/etc/qp/qp.env`（root:qp 0640）追加：

```ini
QP_MORNING_ASTOCK_ROOT=/home/ubuntu/AStock_AI_System
QP_SIGNAL_BUS_ROOT=/home/ubuntu/signal-bus
QP_SIGNAL_BUS_STATE=/home/ubuntu/.local/state/signal-bus
```

## 4. systemd

```bash
sudo install -m 0644 app/deploy/systemd/qp-reports-morning.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qp-reports-morning.timer
```

## 5. 上线自检

```bash
sudo -u qp bash -c 'set -a; . /etc/qp/qp.env; set +a; \
  /opt/qp/current/venv/bin/qp job run reports.import_morning_chain'
sudo -u qp psql -d qp -At -F '|' -c \
  "select kind,business_date,version,content_sha256 from reports.report where kind='morning_chain' order by id desc limit 3;"

sudo -u qp bash -c 'set -a; . /etc/qp/qp.env; set +a; \
  /opt/qp/current/venv/bin/qp reports compare --date "$(date +%F)" \
  --out /var/lib/qp/report-reports'
```

期望：A1–A6 PASS/WARN；失败日（如 11-13 generation_failed）应 A4 PASS（分类与旧告警一致）。

## 6. 每日对账（A1–A6）

- A1 generation 的 `targetDate`/status/exit 与业务日一致
- A2 封存 `SEALED`、`reportSha256` 与文件名一致、digest 存在
- A3 relay `target_date`/`produced_at` 一致，attempt/delivery 有记录
- A4 qp 分类的 atoms 与旧告警 `business_atoms` 完全一致
- A5 影子只读：morning_chain 投递全 shadow、`delivery_attempt=0`
- A6 脱敏：快照内不得出现 chat_id/message_id/evidence/attempts 等键

判定：全 PASS 记通过；WARN 只允许告警文件缺失/relay attempt 缺失；FAIL 需人工裁决。

## 7. 验收窗口

- 至少 **5 个交易日**（设计稿）；窗口从部署后第一个工作日 08:25 起算。
- 窗口内代码冻结在 `RELEASE_SHA`；任何修复结束当前窗口并重新冻结。
- 输出与旧链路一致（A1–A6 每日 PASS/WARN），才具备切换条件。

## 8. 切换前置（单独授权，不在 M8 自动执行）

1. 生成器/relay/告警的 qp 实现（M8 目前只做影子快照；切换需要把生成与 relay 逻辑迁入 qp）。
2. 周末切换：停旧 user timers（07:50/08:00/08:05/08:10/08:12/08:15），启 qp 链；保留旧链路 2 周可回退。
3. 切换前基线：git tag + 旧 unit 文件备份 + 当日产物快照。

## 9. 安全

- 不写 AStock/signal-bus/告警状态；全部只读打开。
- 不触发旧 timer、不发 Telegram、不调用 relay。
- 快照与日志不出现 chat_id、message_id、token、密钥。

## 10. 回滚

1. `sudo systemctl disable --now qp-reports-morning.timer`。
2. `/opt/qp/current` 切回上一版本，重启 `qp-api`。
3. 保留 `reports` 数据与快照；如已授权 ACL，按 §2 撤销。旧链路未被修改，无需恢复。
