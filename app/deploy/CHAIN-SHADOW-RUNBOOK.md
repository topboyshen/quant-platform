# 记录链影子窗口与切换 Runbook

状态（2026-11-15）：P1–P5 已上线；**P6 影子窗口 2026-11-16 ~ 11-20**；P7 受控切换未开始。
设计冻结与数据字典见 mirasim 文档 `f7744dd34a`。

## 1. 每日时间表（Mon–Fri，CST）

| 时间 | 事件 | 归属 |
|---|---|---|
| 15:30 | EOD 双写（`signals.eod`，QP_RECORD_CHAIN=1） | qp timer |
| 15:36 | 影子样本记录（`signals.shadow_record`） | qp timer |
| 15:40 / 15:50 | watchdog 终态审计 | qp timer |
| 15:48 | EOD 重试（仅 data_unavailable 且零信号） | qp timer |
| 15:55 | 记录链快照（`signals.chain_snapshot`，含 compare） | qp timer |
| 16:05 | 对账自动化（`qp-chain-shadow-check-20261116`） | mirasim 自动化 |
| 16:12 | 5 日窗口表（`signals.chain_window`） | qp timer |
| 16:15 | 影子结果（`signals.shadow_outcomes`） | qp timer |
| 16:30 | 指数收盘 | qp timer |
| 16:50 | M4 不变量验收（A1–A10） | qp timer |

## 2. 每日核对命令

```bash
# 窗口表（FAIL 时退出码 1）
sudo -u qp bash -c 'set -a; . /etc/qp/qp.env; set +a; \
  /opt/qp/current/venv/bin/qp chain window --markdown'

# 当日快照与对账
sudo -u qp bash -c 'set -a; . /etc/qp/qp.env; set +a; \
  /opt/qp/current/venv/bin/qp chain compare --date YYYY-MM-DD'
```

PASS 条件：当天有终态 EOD run、有快照、`compare` 无差异、`loss` 三项为 0；
5 个交易日全部 PASS 后进入 P7。`engineeringLoss` 非零即 FAIL，先定位再补证。

## 3. 验收矩阵

| 分支 | 隔离证据（CI，Linux） | 自然运行状态 |
|---|---|---|
| 身份唯一 / 重试不增决策 | `test_register_run_materialises_only_allowed`、`test_retry_appends_attempts_not_decisions`、`test_register_run_is_idempotent_across_sessions` | 待 11-16 起观察 |
| 多持有期同身份 | `test_register_run_materialises_only_allowed`（5 个 horizon 同一 uid） | 待观察 |
| 部分失败批次 | 历史 4 条 `run_incomplete`；`test_history_import...` 的 partial 日 | 历史已覆盖，自然待 |
| 风险拦截 | 历史 10 条 `risk_blocked` + 影子样本页 | 历史已覆盖，自然待 |
| 未成交 / 到期未采集 | `test_expired_reconciliation_appends_revision`、`test_due_queue_skips...` | 自然未覆盖 |
| 归属未知（UNKNOWN） | `test_snapshot_flags_unknown_attribution` | 自然未覆盖（当前全部 confirmed） |
| 规则 / 标的池切换 | `test_snapshot_ignores_other_policy_versions`、`test_universe_version_follows_trade_date` | 自然未覆盖 |
| 快照重放 / digest 稳定 | `test_snapshot_build_buckets_and_replay` | 待观察 |
| 历史导入幂等 / 原业务日期 | `test_history_import_dry_run_then_apply_is_idempotent` + migration `0013` | 已完成（4,860 行回填） |
| 并发写入 | 以跨 session 重复写入覆盖；无真实多线程压测 | 自然未覆盖 |

自然期间未出现的分支，在切换申请里按“隔离回归已覆盖、自然运行未覆盖”标注，不得写成已验证。

## 4. P7 受控切换清单

1. 窗口 5/5 PASS（`qp chain window` 退出码 0，`days_pass == days_total`）。
2. 冻结精确镜像：release、`RELEASE_SHA`、`QP_RECORD_CHAIN`、资格/统计策略版本、标的池版本。
3. ETF 研究池页签改为读快照（`/api/signals/chain/*`），旧 `target_decision` 读取降级为兼容视图；保留 `signal_compare` 历史口径对照。
4. 写集只增不改：不回填、不重算历史收益、不恢复旧生产者。
5. 回退：切回旧读取入口（页面配置），新增记录保留供调查；不做数据回滚。
6. 切换后首个交易日核对：页面/API/窗口表同一快照计数一致。

## 5. 边界与说明（截至 2026-11-15）

- 历史 1,160 条决策的尝试已按 `target_decision.run_id → eod_run` 回填（`engine_version=legacy.v0`，`source_decision_id` 指向源行）；旧系统没有更细的执行时间，`started_at/completed_at` 取对应 run。
- `input_evidence_sha256` 为决策输入的稳定摘要（run|日期|代码|数据状态|触发数|信号数|收盘），native 与历史导入写入，migration `0014` 回填；可用同一表达式在 SQL 重算核对。
- `chain_evidence_link` 表为外部副档预留；当前所有证据都是数据库行引用（ref + sha256），没有外部副档，因此未接线是设计选择，不是遗漏。
- 日报链路已按需求删除，方案中的“日报读同一快照”不再适用；窗口表与页面承担展示。
- P6 影子窗口（11-16 ~ 11-20）与 P7 切读仍未完成，切换条件见 §3/§4。
