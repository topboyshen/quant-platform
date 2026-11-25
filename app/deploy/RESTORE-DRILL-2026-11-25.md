# 隔离宿主恢复演练 — 2026-11-25

状态：**PASS（数据库恢复 + API + 登录页浏览器渲染）**。
范围仅覆盖“异地副本 → 数据库 → API → 登录页”；整机重建 RTO 未覆盖。

- 执行时间：2026-11-25 22:21–22:26 CST
- 隔离宿主：MS02u 的 WSL Ubuntu 24.04（PostgreSQL 16.2，独立 test 集群）
- 代码：`main` `a2d615a`（与生产 `.118` 同源；仅文档差异）
- 生产库：只读；scratch 库 `qp_restore_drill`，演练后删除

## 1. 演练口径

- **数据损失窗口：≤ 1 个自然日**。每日 03:50 CST 备份；备份之后产生的数据不在恢复点内，需要补采或重跑。
- **恢复目标：从异地副本开始复制到登录页可用 ≤ 15 分钟**。该目标包含 copy、restore、migration、API 冷启动、静态页与浏览器渲染；假设隔离宿主已有 PostgreSQL 和 release/venv 工具，不含整机 OS、TLS/nginx、secrets、timers 重建。
- **验收**：`qp db check` 通过；结构/触发器/孤儿检查通过；`qp signals acceptance` 不得 FAIL；页面 HTTP 200 且浏览器能渲染登录页。

## 2. 副本

| 项 | 值 |
|---|---|
| 来源 | 异地主机 `Quant-Server`（111.229.252.186）`/home/ubuntu/qp-offsite-backups/` |
| 文件 | `qp-20261125T035003.dump` |
| 大小 / 时间 | 5,434,600 bytes / 2026-11-25 03:50:04 CST |
| SHA256 | `cac6a1fde41e2bc892b51f536749292b2fbb2a2225029aa2dc98c00afba9a202` |
| 一致性 | 与生产 `/var/lib/qp/backups/qp-20261125T035003.dump` 的 SHA256 相同 |
| 最后完整交易日 | **2026-11-24**（max EOD 与 max chain date 均为 2026-11-24） |

## 3. 分段计时

| 步骤 | 实测 |
|---|---:|
| 异地副本复制到隔离宿主 | 14,543 ms |
| `pg_restore --no-owner --exit-on-error` | 1,947 ms |
| `qp db upgrade`（已到 head，无迁移） | 536 ms |
| `qp signals acceptance` | 554 ms |
| 临时 `qp-api` 冷启动到 `/api/health` 200 | 678 ms |
| 静态页服务就绪（`/next/` 200） | 110 ms |
| 浏览器（Playwright Chromium）渲染登录页 | 637 ms |
| **分段合计** | **19,005 ms（约 19.0 s）** |

说明：上表是各步骤单独测量的合计，不是一次连续 wall-clock；工具间隔未计入。
与 15 分钟目标相比，分段合计约占 **2.1%**，scoped drill 通过。

## 4. 恢复检查

| 检查 | 结果 |
|---|---|
| Alembic head | `0017`，与当前 release 一致 |
| `qp db check` | `{"ok": true, "current": "0017", "heads": ["0017"]}` |
| 关系数 / signals 触发器 | 104 / 20（当前 0017 schema；旧记录的 100/10 不可直接比较） |
| `decision_outcome` 孤儿 | 0 |
| `chain_outcome` 孤儿 | 0 |
| 恢复点计数 | 1,440 决策 / 3,700 结果 / 1,440 链决策 / 3,700 链结果 / 1 用户 |
| `qp signals acceptance` | **WARN**，10 项检查；仅 “signal notifications are shadow”（演练环境预期），其余 PASS |
| 调度检查 | `qp job list` 48 项；release 内 41 个 `.timer` 定义；关键 job（`signals.eod`、`signals.outcomes`、`market.calendar`、`ops.backup`）均在。隔离宿主未安装 qp timers（演练隔离，见 §5） |
| 页面 | `GET /next/` 200；主 JS `assets/index-*.js` 200；`/next/api/health` 200 |
| 浏览器 | 标题 `Quant Platform`，重定向到 `/next/login?redirect=/`，显示用户名/密码/登录表单；唯一非 2xx 是预期中的 `401 /next/api/auth/me`（未登录） |

## 5. 未覆盖 / 未完成

- **整机重建 RTO**：OS、release venv、`/etc/qp/qp.env`、TLS/nginx、systemd timers、外部 smoke 未包含；历史 venv 安装约 19 s，但不能替代整机计时。
- **调度器**：只验证了 job registry 与 release 内的 timer 定义；隔离宿主没有安装/启动 qp timers，生产定时器也未在演练中重启。
- **浏览器**：只渲染登录页；未登录后的业务页面、权限、通知投递未验证。
- **数据损失窗口**：演练恢复点为 2026-11-24；2026-11-25 03:50 之后的生产数据不在该副本内。
- **副本链**：本次只验证 Quant-Server 异地副本；Mac 拉取副本未在本次演练中重复验证。

## 6. 结论

- 在隔离宿主上从异地副本恢复到登录页可用的分段合计约 **19 s**，低于 15 分钟目标；数据库结构、孤儿、acceptance 与页面检查通过。
- 该结果证明**数据库/应用恢复路径可用**，不等于整机 RTO；整机重建仍需单独演练后才能对外声明。
