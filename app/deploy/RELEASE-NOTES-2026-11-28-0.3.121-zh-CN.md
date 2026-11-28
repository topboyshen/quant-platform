# quant-platform 版本说明（2026-11-28：0.3.121）

> **依据**：`main` 提交 `68902b4e6283f15be409ad5c5dbe70b80d4ebd6c`（PR #93 合并提交）。
> 本次有直接生产部署；生产主机 `VM-0-3-ubuntu`，入口 `/next/`。

## 1. 版本与发布身份

| 项目 | 值 |
|---|---|
| 版本 | `0.3.121-backtest-latest-7dfcb452` |
| 源码基线 | `68902b4e6283f15be409ad5c5dbe70b80d4ebd6c` |
| 源码 tree | `83267dc0085505aa9aec86e9a785c0c2e3e8f51d` |
| 内容 SHA256 | `7dfcb4526e66cb70736de5e77a021e2970e82ab5d3f48fe7d6effdec3908c053` |
| 内容文件数 | 486（`CONTENT.sha256.json` 自校验通过） |
| 激活时间 | 2026-11-28 14:22:32 CST |
| 激活结果 | `DB_CHECK_OK` → `ACTIVATE_OK` |
| 生产路径 | `/opt/qp/releases/0.3.121-backtest-latest-7dfcb452`（`/opt/qp/current`） |
| 发布键 | `production/68902b4e6283f15be409ad5c5dbe70b80d4ebd6c` |
| 数据库 | Alembic head `0017`，无迁移 |
| 部署前备份 | `/var/lib/qp/backups/qp-pre-0.3.121-20261128T1422.dump`（5,984,094 B；sha256 `cb28ce41c6d19fd3ea035e12ff9a4d2e22bb0d211f0f6174c9e9051112170c2e`；0600 `qp:qp`） |
| 上一版 / 回滚目标 | `0.3.120-market-generation-bce0f98e` / `4f7047ca982e20af60586995f1ff5a9102c0de20` |

`RELEASE_BASE` 记录为：
`68902b4e6283f15be409ad5c5dbe70b80d4ebd6c + backtest latest trades content sha256 7dfcb4526e66cb70736de5e77a021e2970e82ab5d3f48fe7d6effdec3908c053`。

## 2. 发布内容

- **PR #93**：回测明细支持查看最近成交。
  - 后端 `GET /api/research/backtests/{run_id}` 新增 `trades=earliest|latest`，
    默认仍为 `earliest`；`latest` 按 `trade_index` 倒序取最近 500 条。
  - 响应字段不变（成交序号仍为 `index`），`counts.trades` 仍为总成交数。
  - 前端回测明细请求显式带 `?trades=latest`，「最近成交」按新→旧展示。
  - 新增回归测试：后端覆盖默认/最新两种排序及 501 条边界；前端新增
    `backtest-latest-501.json` fixture 与明细断言。
- 前端从精确源码 SHA 重新构建（`npm ci` + `build`）；无依赖锁变更、无 Alembic 迁移。
- 发布后 `main` 又合入 PR #94（`bf45bc2`），不在 `.121` 发布内容内；生产仍为
  `68902b4` 构建的版本。

## 3. 构建与验证

- main push CI run `46115006298`：backend pytest、security/lint、dependency audit、
  frontend build、frontend smoke 五项全部成功。
- 同树 WSL 回归：**77 passed / 0 failed / 0 skipped**（后端 7、前端 70），
  逐字段诊断 1 通过。
- 486 文件内容清单自校验通过（0 差异）；requirements lock 未变更。
- `/api/health` 与 `/next/api/health`：`status=ok`、`db=true`、版本一致；
  `qp-api` active。

## 4. Smoke

- 页面：`/next/`、`/next/login`、`/next/research/backtests`、`/next/api/health`
  均 200；`/next/api/auth/me`、`/next/api/research/backtests` 按预期 401。
- 回测明细（线上 run 96）：总成交 30，`equity_points` 628；
  - 默认接口 `/api/research/backtests/96` 返回 `index` 1→30（默认行为兼容）；
  - `?trades=latest` 返回 30→1；
  - 页面「最近成交」显示 #30→#1，共 30 行。

## 5. 冻结与回滚

- `.121` 是冻结期内的 correctness 修复，不改变冻结起点（2026-11-25 11:03 CST）
  与最早 Phase 2 复核日（2026-12-09）。
- 本次未触发回滚。回滚目标 `.120` 的 release 目录与 venv 保留；回滚不降级数据库：

  ```bash
  sudo /opt/qp/current/deploy/bin/qp-release-activate \
    --release /opt/qp/releases/0.3.120-market-generation-bce0f98e \
    --previous /opt/qp/releases/0.3.121-backtest-latest-7dfcb452 \
    --health-url http://127.0.0.1:18800/api/health
  ```

## 6. 证据位置

- 生产：`/opt/qp/current/{RELEASE_NAME,RELEASE_SHA,RELEASE_BASE,CONTENT.sha256.json}`
- 发布审计：`/var/lib/qp/audit/production-release-68902b4-20261128.json`
- 备份：`/var/lib/qp/backups/qp-pre-0.3.121-20261128T1422.dump`
- 发布回执：本空间私信 thread=113 #147
- 上次发布：`RELEASE-NOTES-2026-11-26-0.3.120-zh-CN.md`（0.3.120）
