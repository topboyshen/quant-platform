# M4 Live 切换 Runbook（Swing 信号通知）

状态：2026-11-13 起执行；2026-11-25 22:44 CST 核对通过（live 通知与 timer）。范围仅限 **M4 信号链的真实 Telegram 通知**。

## 1. 范围与前置事实

- 旧 swing EOD/retry/watchdog/repair/outcomes timer 已在 2026-11-13 停用；旧日报/研究摘要/早报链已删除。qp 的 `signals.*` 是唯一信号生产者。
- M4 的 EOD、watchdog、repair、outcomes 仍按原时间运行；本次只改变**通知出口**，不改变计算逻辑。
- 不包含：dedirock 摘要 POST（`qp-signals-digest.timer` 已删除）、报告自动生成、approved-signals 出站。
- 迁移版本不变：`0010`。本发布只替换 backend + deploy 单元。
- 旧 outbox 行是 `mode=shadow`，切换后**不补发**；只有新产生的 `mode=live` 行会投递。

## 2. 通知路径

```
signals.watchdog 15:40/15:50
  -> notify.outbox (mode 由 QP_SIGNALS_NOTIFY_MODE 决定)
  -> qp-notify.timer (每 2 分钟) -> qp-job@notify.send.service
  -> Telegram sendMessage -> notify.delivery_attempt(message_id) + outbox.status=sent
```

- `QP_NOTIFY_MODE=shadow` 保持全局默认；运维健康告警仍只进 shadow。
- `QP_SIGNALS_NOTIFY_MODE=live` 让信号链通知变为 live：watchdog 的 failure/daily、
  17:40 ETF 研究观察（`etf-shadow`；combined 模式为 `daily-combined`）和每周绩效归因
  （`attribution-weekly`）。由 `QP_NOTIFY_MODE` 管控的运维/健康通知仍保持 shadow。
- 同一 `dedup_key`（`watchdog:<date>:daily|failure`）只入队一次；同日重跑不会重复发送。

## 3. 密钥与配置

```bash
# root:qp 0640，值不写进 qp.env、不进日志
/etc/qp/telegram_bot_token
/etc/qp/telegram_chat_id
```

`/etc/qp/qp.env` 追加/确认：

```ini
QP_NOTIFY_MODE=shadow
QP_SIGNALS_NOTIFY_MODE=live
QP_NOTIFY_KILL_SWITCH=0
QP_NOTIFY_DISABLED_FILE=/etc/qp/notify.disabled
QP_TELEGRAM_BOT_TOKEN_FILE=/etc/qp/telegram_bot_token
QP_TELEGRAM_CHAT_ID_FILE=/etc/qp/telegram_chat_id
QP_TELEGRAM_API_BASE=https://api.telegram.org
# nnG8: api.telegram.org is not directly reachable; use the local mihomo proxy.
QP_TELEGRAM_PROXY=http://127.0.0.1:7890
QP_TELEGRAM_TIMEOUT_SEC=10
QP_NOTIFY_MAX_ATTEMPTS=5
```

上线前用 `getMe` 验证 token（只输出 HTTP 码/用户名，不回显 token）：

```bash
tok=$(sudo cat /etc/qp/telegram_bot_token)
curl -sS -o /tmp/getme.json -w '%{http_code}\n' "https://api.telegram.org/bot${tok}/getMe"
sudo python3 -c 'import json;d=json.load(open("/tmp/getme.json"));print(d.get("ok"),d.get("result",{}).get("username"))'
rm -f /tmp/getme.json
```

## 4. 切换步骤

1. 部署 release（backend + deploy），重启 `qp-api`，确认 `/api/health` 200。
2. 安装并启用 `qp-notify.timer`：

   ```bash
   sudo install -m 0644 app/deploy/systemd/qp-notify.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now qp-notify.timer
   systemctl list-timers qp-notify.timer
   ```

3. 写入 `/etc/qp/telegram_bot_token`、`/etc/qp/telegram_chat_id`（`root:qp 0640`）。
4. 在 `/etc/qp/qp.env` 设置 `QP_SIGNALS_NOTIFY_MODE=live`，`QP_NOTIFY_KILL_SWITCH=0`。
5. 手工 smoke：插入一条 `mode=live` 的测试 outbox 行，`systemctl start qp-job@notify.send.service`，确认 Telegram 收到、`delivery_attempt.ok=true`、`outbox.status=sent`。
6. 次日 15:50 自然运行后核对 daily 报告是否送达；15:40 若出现 failure，应即时送达。

## 5. 验收（每个交易日）

```sql
select mode,status,count(*) from notify.outbox group by 1,2 order by 1,2;
select id,outbox_id,at,mode,ok,detail from notify.delivery_attempt order by id desc limit 20;
```

- 15:50 后 `watchdog:<date>:daily` 行为 `sent`，`delivery_attempt` 有 `message_id`；
- 失败行最多重试 `QP_NOTIFY_MAX_ATTEMPTS` 次后转 `failed`，不会无限重发；
- 旧 shadow 行保持 `shadowed`，不得出现 `mode=live` 的历史补发；
- `ops.job_run` 中 `notify.send` 为 `succeeded`（网络抖动时 `retry>0` 但仍成功）。

**2026-11-25 22:44 CST 核对**：

- env：`QP_NOTIFY_MODE=shadow`、`QP_SIGNALS_NOTIFY_MODE=live`、`QP_NOTIFY_KILL_SWITCH=0`，
  `/etc/qp/notify.disabled` 不存在。
- outbox：18 条 `live/sent`、219 条 `shadow/shadowed`，无 pending/failed；最近 live 为
  `watchdog:2026-11-25:daily`（15:52 发送）、`etf-shadow:2026-11-25`（17:42 发送）、
  `attribution-weekly:2026-W48`（11-24 发送）。
- `delivery_attempt`：outbox 237 `live`、`ok=true`、latency 930 ms、`message_id=4744`。
- timers：42/42 个 qp timer active，关键 timer 均 enabled；`qp-notify.timer` 每 2 分钟运行，
  最近 `notify.send` 均为 `succeeded`；`systemctl --failed` 为 0。

## 6. 紧急停止

```bash
sudo systemctl stop qp-notify.timer     # 立即停止投递
sudo touch /etc/qp/notify.disabled      # 或文件级 kill switch
# 或在 /etc/qp/qp.env 设 QP_NOTIFY_KILL_SWITCH=1
```

停止后 pending 行保留，恢复时把开关置 0/删除文件并 `systemctl start qp-notify.timer`；已 `sent` 的行不会重发。

## 7. 回滚

1. `QP_SIGNALS_NOTIFY_MODE=shadow`（新行回到 shadow，不发送）。
2. `systemctl disable --now qp-notify.timer`。
3. 如需回退代码：`/home/ubuntu/qp-m4live-deploy-*/rollback-*.sh` 切回 `0.3.11-history-02e488d` 并重启 API。
4. 数据无需回滚：`signals.*`、`notify.outbox`、`delivery_attempt` 均为增量；已发送的消息无法撤回。

## 8. 安全不变量

- token 只存在于 `/etc/qp/telegram_bot_token`，不在 qp.env、数据库、日志、job result 中出现。
- `recipient` 只存逻辑名（`signals`），chat id 从文件读取。
- `notify.send` 在缺密钥时 fail-closed：job `failed`，行保持 pending，不发送、不标记。
- 4xx（除 429）视为永久失败；429/5xx/网络错误才重试。
- 关闭报告链后，`notify.outbox` 不应再出现 `report:` 或 `morning:` 新行。

## 9. Acceptance 的 verdict 与 run_quality

`qp signals acceptance` 的 `verdict` 只回答“这次复评有没有 FAIL/WARN”；
`run_quality` 另外保留自然运行质量，二者不能互相替代：

| 字段 | 含义 |
|---|---|
| `natural` | verdict PASS，定时运行的首次投递即成功 |
| `retry_recovered` | verdict PASS，自动重试后成功 |
| `manual_recovered` | verdict PASS，但 outbox 来自人工 job run，或来源未知且超过自动尝试预算 |
| `warn` / `failed` | verdict WARN / FAIL |
| `natural_pass` | 仅 `natural` 为 true |

A8 仍会把“已补发成功”判为 PASS，但 `run_quality=manual_recovered` 和
`natural_pass=false` 会保留事实。`delivery.execution_source` 来自
`ops.job_run.trigger`（`scheduled` / `scheduled_retry` / `manual` / `replay`），
`recovery_basis` 标明结论来自执行来源还是仅来自尝试次数启发式；不得把定时任务
的延迟自动写成“人工恢复”。

历史验收记录只追加、不覆盖。需要复核旧运行时使用：

```bash
qp job review --run-id <id> --reviewer <name> --reason <text> --field key=value
```

该命令写入新的 `ops.job_run_review` 行并在 result 中关联原 run id；原
`ops.job_run.result` 保持不变。页面同时保留原始结论与后续复核，不为了纠正
口径而回滚或改写历史行。验收统计同时看 verdict 和 run_quality，不能把手工
恢复计入自然通过率。

## 10. 影子 allowed 样本（验证用，M12）

当风控长期拦截全部触发信号时，`decision_outcome` 没有样本可验证趋势规则。影子通道把「趋势触发但被风控拦截」的决策复制到独立表并计算前瞻收益，**只用于验证**。

| 项 | 值 |
|---|---|
| 表 | `signals.shadow_signal` / `signals.shadow_outcome`（`0012`） |
| 记录 | `signals.shadow_record`，Mon-Fri 15:36 CST（EOD 之后） |
| 结果 | `signals.shadow_outcomes`，Mon-Fri 16:15 CST（outcomes 之后） |
| 视图 | 页面 信号 → 影子样本；API `GET /api/signals/shadow` |
| 验收摘要 | `qp signals acceptance` 的 `strategy.shadow`（samples/outcomes/last_sample_date） |

回填与手工执行：

```bash
sudo -u qp bash -c 'set -a; . /etc/qp/qp.env; set +a; \
  /opt/qp/current/venv/bin/qp signals shadow-record --since 2026-08-01'
sudo -u qp bash -c 'set -a; . /etc/qp/qp.env; set +a; \
  /opt/qp/current/venv/bin/qp signals shadow-outcomes'
```

不变量：

- 只记录 `decision_state=blocked`、`raw_trigger_count>0`、`data_status=ok`，取当日最后一次 success/partial run；
- 不进 `notify.outbox`、不进记录链、不改 `target_decision`/`decision_outcome` 的任何统计；
- 未到期显示“待到期”，限价无法成交记 `unfilled`，从不按 0 收益处理；
- 记录幂等：`(decision_date, code)` 与 `(shadow_id, horizon)` 唯一。
