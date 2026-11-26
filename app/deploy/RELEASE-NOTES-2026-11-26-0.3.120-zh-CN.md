# quant-platform 版本说明（2026-11-26：0.3.120）

> **依据**：`main` HEAD `4f7047ca982e20af60586995f1ff5a9102c0de20`（PR #86 合并提交，
> 与 `origin/main` 一致）。本次有直接生产部署；生产主机为 `VM-0-3-ubuntu`
> （150.158.46.28），入口 `/next/`。

## 1. 版本与发布身份

| 项目 | 值 |
|---|---|
| 版本 | `0.3.120-market-generation-bce0f98e` |
| 源码基线 | `4f7047ca982e20af60586995f1ff5a9102c0de20` |
| 内容 SHA256 | `bce0f98e6e5e1a60de131f6ba500da86d8ad880baaac75283b99d0c58d8fdf58` |
| 内容文件数 | 483（`CONTENT.sha256.json` 自校验通过） |
| 激活时间 | 2026-11-26 19:17 CST |
| 激活结果 | `DB_CHECK_OK` → `ACTIVATE_OK` |
| 生产路径 | `/opt/qp/releases/0.3.120-market-generation-bce0f98e`（`/opt/qp/current`） |
| 发布键 | `production/4f7047ca982e20af60586995f1ff5a9102c0de20` |
| 数据库 | Alembic head `0017`，无迁移 |
| 部署前备份 | `/var/lib/qp/backups/qp-pre-0.3.120-20261126T1916.dump`（5,835,128 B；sha256 `d4adf800…4bc5`；0600 `qp:qp`） |
| 上一版 / 回滚目标 | `0.3.119-acceptance-after-daily-report-6e5e111d` / `aa4755ad9f00652b58a7bd77fc8c2d9ac79b9937` |

`RELEASE_BASE` 记录为：
`4f7047ca982e20af60586995f1ff5a9102c0de20 + market Qlib generation compare content sha256 bce0f98e6e5e1a60de131f6ba500da86d8ad880baaac75283b99d0c58d8fdf58`。

## 2. 发布内容

- **PR #86**：修复 Qlib 换树后 `snapshot.core_vs_qp` 的跨代次误报。
  - qp 的 qlib 摘要写入 `generation.tag`；
  - legacy 与 qp tag 不同且都存在时，coverage 只比较 `valid`，
    `eligible/invalid/ratio` 作为代次元数据保留；
  - 同代次仍要求 coverage 全字段严格相等；
  - detail 增加 `generation` / `coverage_scope`，并新增 3 个回归测试。
- 前端复用 0.3.119 的静态构建；无运行时前端、依赖锁或 Alembic 迁移变更。
- `.119` 之后的 Mac 迁移测试、文档与 `.119` 发布记录随 release 进入 `deploy/`。

## 3. 构建与验证

- main push CI run `45806406989`：backend pytest、security/lint、dependency audit、
  frontend build、frontend smoke 五项全部成功。
- 本地迁移分支完整后端测试：**569 passed / 44 skipped**。
- 483 文件内容清单自校验通过；requirements lock 为 `3a22e1b3…4434`，与现行一致。
- `/api/health` 与 `/next/api/health`：`status=ok`、`db=true`、版本一致；`qp-api` active。
- Smoke：`/next/`、`/next/login`、`/next/overview`、`/next/signals`、`/next/ops`、
  静态 JS、`/next/api/health` 均 200；`/next/api/auth/me`、`/next/api/overview`、
  `/next/api/signals/runs` 按预期 401。

## 4. 市场摘要与 compare

- 部署后只读重跑 `qp-market-read`（`ok=true`，36.67s）和
  `market.ingest_legacy`（`items=10, mismatch=[]`）。
- qp 摘要代次已对齐：legacy `2026-09-06`，qp `2026-11-25`。
- 只读执行 compare：`snapshot.core_vs_qp = match`，`coverage_scope=valid`，
  `coverage.valid` 与 breadth/top 三榜一致。
- `compare.latest` 由下一轮 **23:45** `market.compare` 写入后转为 match；本次未提前触发。

## 5. 时间口径与 timer

- 当前运行版本 Overview `signals` deadline：`17:50`
- `signals.acceptance` schedule hint：`Mon-Fri 17:50 CST (after outcomes 16:10, index_close 16:30 and daily report 17:40)`
- Timer：enabled、active，`OnCalendar=Mon..Fri *-*-* 17:50:00 Asia/Shanghai`，
  下次 2026-11-27 17:50。

## 6. 冻结与回滚

- `.120` 是冻结期内的 correctness 修复，不改变冻结起点（2026-11-25 11:03 CST）
  与最早 Phase 2 复核日（2026-12-09）。
- 本次未触发回滚。回滚目标 `.119` 的 release 目录与 venv 保留；回滚不降级数据库：

  ```bash
  sudo /opt/qp/current/deploy/bin/qp-release-activate \
    --release /opt/qp/releases/0.3.119-acceptance-after-daily-report-6e5e111d \
    --previous /opt/qp/releases/0.3.120-market-generation-bce0f98e \
    --health-url http://127.0.0.1:18800/api/health
  ```

## 7. 证据位置

- 生产：`/opt/qp/current/{RELEASE_NAME,RELEASE_SHA,RELEASE_BASE,CONTENT.sha256.json}`
- 发布审计：`/var/lib/qp/audit/production-release-4f7047c-20261126.json`
- 备份：`/var/lib/qp/backups/qp-pre-0.3.120-20261126T1916.dump`
- 上次发布：`RELEASE-NOTES-2026-11-26-zh-CN.md`（0.3.119）
- 相关评估/切换：`QLIB-OVERLAP-ASSESSMENT-2026-11-26.md`、
  `QLIB-FULL-RELEASE-CUTOVER-2026-11-26.md`
