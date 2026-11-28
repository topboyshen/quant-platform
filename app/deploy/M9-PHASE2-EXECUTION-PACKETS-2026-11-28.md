# M9 Phase 2 execution packets — 2026-11-28 (draft)

> **草案，未授权执行。** 每个 packet 只覆盖一个 unit（或必须同批处理的监控 pair），
> 执行前必须满足观察窗口、单独授权、per-unit checklist、rollback 与 owner 审批。
> 本文件没有停止、禁用或删除任何服务或数据。

## 0. 共同前置门

- 当前 release：`0.3.122-backtest-search-recovery-ad12fb37`，源 `bf45bc2`，DB `0017`。
- 观察窗口：**2026-11-18 → 2026-12-01**；截至 2026-11-27 为 8/10，11-30、12-01 未到。
- Phase 2 最早复核：**2026-12-09**，且必须有单独的用户授权。
- qp health、compare、10 个业务日 acceptance/日报均为绿色；当前无未恢复告警。
- 每个 packet 执行前重新跑 inventory 和 unit/config archive，核对 sha256。
- 数据完整性（2026-11-28 readiness-2）：ledger 9/9 表、phase2 6/6 表行数与源
  完全一致；calendar/index 对账见 prep §10.1。
- D/E 与 `market.compare` 的耦合见 prep §11；Packet E 当前 **blocked**。
- **禁止前缀批量 stop**；禁止 `docker rm`、数据删除、配置删除。
- 每个 packet 只停一个 unit；停后观察至少一个业务日，再考虑下一个。

## 1. Packet A — research-ledger（含 source healthcheck pair）

| Field | Value |
|---|---|
| Unit | `research-ledger.service`（127.0.0.1:18769，`researchledger`，enabled/active） |
| Current responsibility | Research Ledger local-only API；`runtime-health.mode=FROZEN`，备份状态停留在 2026-09-10 |
| Replacement | qp `research.import_ledger`（09:40 / 16:40，最近 2026-11-27 成功） |
| Consumer switched | qp `research.import_ledger` 已成功运行；qp research 模块读取导入结果 |
| Independent supervision | qp job timer + qp health/ops inventory；legacy 侧 `research-ledger-healthcheck.timer` 每 5 分钟检查并以 Telegram alert 报错。**qp 只有连续两次失败告警，无“定时器不再运行”告警**，执行日需人工核对 `ops.job_run` 最近一次 run |
| Paired monitor | `research-ledger-healthcheck.timer` / `.service`；它监控该 legacy source，**没有 qp equivalent**，停服前必须先停 |
| Stop order | 先停 `research-ledger-healthcheck.timer`，确认 inactive；再停 `research-ledger.service` |
| Rollback | `sudo systemctl enable --now research-ledger-healthcheck.timer research-ledger.service` |
| Post-stop check | qp `research.import_ledger` 下一轮成功；qp health 无新增 warning；无 ledger 消费者报错 |
| Current status | **ready for authorization after observation**, actual stop/start not tested |

Packet A 必须把 healthcheck 作为同一 source pair 记录；不能只停 service 留下 timer 持续告警。

## 2. Packet B — research-system-phase2

| Field | Value |
|---|---|
| Unit | `research-system-phase2.service`（127.0.0.1:18768，`researchphase2`，enabled/active） |
| Current responsibility | Research System Phase 2 loopback dashboard；`runtime-health.mode=FROZEN` |
| Replacement | qp `research.import_phase2`（Mon-Fri 16:45，14 天 10/10，最近 2026-11-27 16:45 成功；job hint 写 every 10 min，以实际 timer 为准） |
| Consumer switched | qp import job 成功；无独立 legacy consumer 连接 |
| Independent supervision | qp job timer + qp health/ops inventory；legacy healthcheck 每 5 分钟检查并以 Telegram alert 报错。**qp 只有连续两次失败告警，无“定时器不再运行”告警**，执行日需人工核对 `ops.job_run` 最近一次 run |
| Rollback | `sudo systemctl enable --now research-system-phase2.service` |
| Post-stop check | qp `research.import_phase2` 成功；qp research health 不变；报告/页面无依赖 |
| Current status | **ready for authorization after observation**, actual stop/start not tested |

## 3. Packet C — quant-approved-signals（hold）

| Field | Value |
|---|---|
| Unit | `quant-approved-signals.service`（127.0.0.1:8094，enabled/active） |
| Consumer path | nginx `quantdinger-8443` 的 approved-signals location → 8094；路径存在 |
| Observed traffic | 全部保留日志（2026-11-11 → 11-28，含 `.gz`）该路径命中 0；采样无 established 连接 |
| Replacement | qp `signals.approved` + `/next/` |
| Open gap | 未观测到近期实际请求；不能仅凭“无连接”证明消费者已切换 |
| Decision | **hold**：补请求/consumer 证据或由 owner 明确接受停服影响后再发 packet |

## 4. Packet D — research-trading-calendar-refresh.timer

| Field | Value |
|---|---|
| Unit | `research-trading-calendar-refresh.timer`（enabled/active；06:20） |
| Current responsibility | legacy A-share calendar cache refresh |
| Replacement | qp `market.calendar`（06:30，14 天 14/14，最近 2026-11-28 成功） |
| Independent supervision | qp `market.health` 的 `calendar.qp`（最近 ok）会写 `market:health:*` alert |
| Rollback | `sudo systemctl enable --now research-trading-calendar-refresh.timer` |
| Post-stop check | qp calendar 下一交易日成功；qp market health/compare 不变；出现 `calendar.*` mismatch 即恢复 timer |
| Current status | **ready for authorization after observation（带条件）**：legacy 日历文件将冻结，不再跟随修订；停后必须监控 compare |

## 5. Packet E — research-index-close-cache.timer

| Field | Value |
|---|---|
| Unit | `research-index-close-cache.timer`（enabled/active；15:55） |
| Current responsibility | legacy four-index close cache |
| Replacement | qp `market.index_close`（16:30/16:40，14 天 20/20，最近 2026-11-27 成功） |
| Independent supervision | qp `market.health` 的 `index_close.qp_current`（最近 ok）会写 `market:health:*` alert |
| Rollback | `sudo systemctl enable --now research-index-close-cache.timer` |
| Post-stop check | qp index close 下一交易日成功；qp market compare/health 不变 |
| Current status | **blocked**：qp compare 的 `index_close.legacy_vs_qp` 依赖 legacy 最近 20 天数据；停后约 20 天该项变 `skipped`、`compare.latest` 转 `unknown`。前置方案见 [`M9-PHASE2-INDEX-COMPARE-PREREQ-2026-11-28.md`](M9-PHASE2-INDEX-COMPARE-PREREQ-2026-11-28.md) |

## 6. Hold / retain（不在本批）

| 对象 | 原因 |
|---|---|
| `nng8-operations.service` | 无 qp replacement；监听 10.0.0.3:18771，无 established 客户端，仍需 owner 决策 |
| `radar-quantdinger-postgres` / `redis` | 无 qp replacement；数据保留/消费者未闭环，禁止停止或删除 |
| `research-data` producer | core 已到 11-27，但 snapshot/health 仍 stale/partial；无 qp-owned replacement |
| qlib cron | source 仍 active；qp 只是 read-only mirror |
| `research-ledger-healthcheck.timer` | 与 Packet A 成对；不单独停 |
| `quant-notify-maintenance-monitor.timer` | qp 覆盖不完整，未逐项确认 |
| `nng8-dashboard-cert-renew.timer` | 无 qp replacement |
| user timers (`hermes-telegram-watchdog`, `research-data-bundle`, `launchpadlib-cache-clean`) | 无 qp replacement，需 user scope/owner |
| `quant-research-daily-summary.service` | inactive，无 timer；无运行可停 |
| `research-index-close-cache.timer` | **blocked**：qp compare 依赖 legacy 最近 20 天数据，需先发版退役/放宽比较项 |

## 7. 执行日 preflight（逐 packet）

- [ ] 观察窗口已完成，日期 ≥2026-12-01，且已过 2026-12-09 复核。
- [ ] 用户给出该 unit 的单独 stop 授权（范围、时间、owner）。
- [ ] `qp decommission inventory` fresh inventory 与 unit/config archive 已重跑并核对 sha256；确认 `.d/*.conf` 已包含或补充归档已核对。
- [ ] qp health、compare、acceptance、日报、告警均 green；无回滚历史。
- [ ] unit 当前状态、enabled、消费者路径和 rollback 命令再次核对。
- [ ] mock rollback drill 记录在案；实际 stop/start 仍按授权执行。
- [ ] 记录 before/after：unit 状态、qp rows、报告、告警、consumer 日志。
- [ ] 失败或任一空白 → 停止并回滚，不继续下一个 packet。

## 8. 结论

可执行包：research-ledger（pair）、research-system-phase2、legacy calendar timer
（带 compare 监控条件）；前提是观察窗口完成、2026-12-09 复核和单独授权。

阻塞：legacy index timer（`index_close.legacy_vs_qp` 依赖 legacy 最近 20 天数据，
需先发版退役/放宽该比较项）。暂缓：approved-signals（流量证据不足）。
保留：nng8、QD PG/Redis、research-data/qlib、ledger healthcheck（除 packet A 成对处理）、
user timers、cert-renew、notify maintenance。

## 9. 证据

- `M9-PHASE2-PREP-2026-11-28.md`
- `M9-EXECUTION-CHECKLIST.md`
- `M9-DECOMMISSION-RUNBOOK.md`
- inventory：`/var/lib/qp/decommission-2026-11-28.json`
- unit archive：`/var/lib/qp/decommission-2026-11-28-units.tgz`
- mock drill：`/var/lib/qp/decommission/2026-11-28/rollback-drill.log`
- PM refresh（2026-11-28 16:05 CST）：`/var/lib/qp/decommission/2026-11-28/pm-refresh/`
  （`SHA256SUMS` 覆盖 8 个文件）
- readiness-2（2026-11-28 16:20 CST）：`/var/lib/qp/decommission/2026-11-28/readiness-2/`
  （数据完整性、监督矩阵、归档缺口、回滚单元校验）
- drop-in 补充归档：`/var/lib/qp/decommission-2026-11-28-units-dropins.tgz`
  （14 成员，sha256 `644227a7…9372b`）
- Packet E 前置方案：`M9-PHASE2-INDEX-COMPARE-PREREQ-2026-11-28.md`
