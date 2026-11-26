# quant-platform 版本说明（2026-11-26：0.3.119）

> **依据**：`main` HEAD `aa4755ad9f00652b58a7bd77fc8c2d9ac79b9937`（PR #84 合并提交，与
> `origin/main` 一致）。本次有直接生产部署；生产主机为 `VM-0-3-ubuntu`（150.158.46.28），
> 入口 `/next/`。

## 1. 版本与发布身份

| 项目 | 值 |
|---|---|
| 版本 | `0.3.119-acceptance-after-daily-report-6e5e111d` |
| 源码基线 | `aa4755ad9f00652b58a7bd77fc8c2d9ac79b9937` |
| 内容 SHA256 | `6e5e111d5c3f6a2a70f3c839996e502b597477d78101217192d43b41e38453ea` |
| 内容文件数 | 482（`CONTENT.sha256.json` 自校验通过） |
| 激活时间 | 2026-11-26 10:12 CST |
| 激活结果 | `DB_CHECK_OK` → `ACTIVATE_OK` |
| 生产路径 | `/opt/qp/releases/0.3.119-acceptance-after-daily-report-6e5e111d`（`/opt/qp/current`） |
| 发布键 | `production/aa4755ad9f00652b58a7bd77fc8c2d9ac79b9937` |
| 数据库 | Alembic head `0017`，无迁移 |
| 部署前备份 | `/var/lib/qp/backups/qp-pre-0.3.119-canonical-20261126T1011.dump`（5,786,113 B；sha256 `5c61bc8d…e926`；0600 `qp:qp`） |
| 上一版 / 回滚目标 | `0.3.118-outcome-window-6f60f5a8` / `cf6e19d29d6a34293dee9772a55350baab59f158` |

`RELEASE_BASE` 记录为：
`aa4755ad9f00652b58a7bd77fc8c2d9ac79b9937 + acceptance after daily report content sha256 6e5e111d5c3f6a2a70f3c839996e502b597477d78101217192d43b41e38453ea`。

## 2. 发布内容

- **PR #84**：M4 acceptance 从 16:50 移到 17:50 CST，确保 A8 能看到 17:40 的
  `daily-combined` 日报；同步 Overview `signals` deadline、作业注册表
  `signals.acceptance.schedule_hint`、`CHAIN-SHADOW-RUNBOOK` 和 timer 回归测试。
- 前端复用上一版静态构建；本次无前端、依赖锁或 Alembic 迁移文件变更。
- `.118` 之后合入的 M9/QLIB 文档随 release 进入 `deploy/`，不改变运行时。
- 11-17 acceptance 已按日报实际送达证据人工重评为 PASS，并保留
  `/var/lib/qp/audit/acceptance-5656-before-20261126T001956.json` 与对应 after 证据；
  11-13 保持历史 shadow WARN，不做历史改写。

## 3. 构建与验证

- PR #84 CI run `45729424215`：backend pytest、security/lint、dependency audit、
  frontend build、frontend smoke 五项全部成功。
- 本地 LF 副本后端测试：564 passed / 44 skipped。
- 以精确提交生成 482 文件内容清单，自校验 0 差异；requirements lock 为
  `3a22e1b3…4434`，与生产基线一致。
- 构建初稿曾因 Windows `core.autocrlf=true` 产生 CRLF，已淘汰并移至
  `/var/lib/qp/audit/invalid-releases/`；最终 current 使用 canonical LF 产物。
- `/api/health` 与 `/next/api/health`：`status=ok`、`db=true`、版本一致；`qp-api` active。
- Smoke：`/next/`、`/next/login`、`/next/overview`、`/next/signals`、`/next/ops`、
  静态 JS/CSS、`/next/api/health` 均 200；`/next/api/auth/me`、`/next/api/overview`、
  `/next/api/signals/runs` 按预期 401。

## 4. 时间口径与 timer

- 当前运行版本 Overview `signals` deadline：`17:50`
- `signals.acceptance` schedule hint：`Mon-Fri 17:50 CST (after outcomes 16:10, index_close 16:30 and daily report 17:40)`
- Timer：enabled、active，`OnCalendar=Mon..Fri *-*-* 17:50:00 Asia/Shanghai`，
  下次 2026-11-26 17:50；未提前触发日报或重跑生产 acceptance。

## 5. 冻结与回滚

- `.119` 是冻结期内的 correctness 修复，不改变冻结起点（2026-11-25 11:03 CST）
  与最早 Phase 2 复核日（2026-12-09）。
- 本次未触发回滚。回滚目标 `.118` 的 release 目录与 venv 保留；回滚不降级数据库：

  ```bash
  sudo /opt/qp/current/deploy/bin/qp-release-activate \
    --release /opt/qp/releases/0.3.118-outcome-window-6f60f5a8 \
    --previous /opt/qp/releases/0.3.119-acceptance-after-daily-report-6e5e111d \
    --health-url http://127.0.0.1:18800/api/health
  ```

## 6. 证据位置

- 生产：`/opt/qp/current/{RELEASE_NAME,RELEASE_SHA,RELEASE_BASE,CONTENT.sha256.json}`
- 发布审计：`/var/lib/qp/audit/production-release-aa4755a-20261126.json`
- 备份：`/var/lib/qp/backups/qp-pre-0.3.119-canonical-20261126T1011.dump`
- 仓库：PR #84；评估/切换记录见 `QLIB-OVERLAP-ASSESSMENT-2026-11-26.md`、
  `QLIB-FULL-RELEASE-CUTOVER-2026-11-26.md`
