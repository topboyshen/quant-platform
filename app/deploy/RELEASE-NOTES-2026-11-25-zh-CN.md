# quant-platform 版本说明（2026-11-25：0.3.118）

> **依据**：`main` HEAD `cf6e19d29d6a34293dee9772a55350baab59f158`（PR #73 合并提交，与
> `origin/main` 一致）。本文只记录本次会话中有直接部署操作的版本；生产主机为
> `VM-0-3-ubuntu`（150.158.46.28）。

## 1. 版本与发布身份

| 项目 | 值 |
|---|---|
| 版本 | `0.3.118-outcome-window-6f60f5a8` |
| 源码基线 | `cf6e19d29d6a34293dee9772a55350baab59f158` |
| 内容 SHA256 | `6f60f5a80c71c8959c56b8b949a6dbd95efbb5b88946933cc2660edc0608b079` |
| 内容文件数 | 476（`CONTENT.sha256.json` 自校验通过） |
| 激活时间 | 2026-11-25 21:28 CST |
| 激活结果 | `DB_CHECK_OK` → `ACTIVATE_OK` |
| 生产路径 | `/opt/qp/releases/0.3.118-outcome-window-6f60f5a8`（`/opt/qp/current`） |
| 数据库 | Alembic head `0017`，无迁移 |
| 部署前备份 | `/var/lib/qp/backups/qp-pre-0.3.118-20261125T2127.dump`（5.3 MB） |
| 上一版 | `0.3.117-correctness-batch1-f531e552`（保留用于回滚） |

`RELEASE_BASE` 记录为：
`cf6e19d29d6a34293dee9772a55350baab59f158 + outcome window and OHLC validation content sha256 6f60f5a80c71c8959c56b8b949a6dbd95efbb5b88946933cc2660edc0608b079`。

## 2. 发布内容

- **PR #74**（`9a108cc`）：收益入场/期限日改由 `market.trading_day[origin=qp]` 交易日历确定；
  基准缺日或无效时记录 `data_gaps`，不再顺延成交；抓取与结果计算共用 OHLC 校验；
  新增 `qp signals outcomes --reconcile [--dry-run]`。
- **PR #73**（`cf6e19d`）：工程评分与优化方案文档，随 `deploy/` 进入 release，不改变运行时。
- 前端复用上一版静态构建；本次未改前端。

## 3. 部署后验证

- `/api/health`：`{"status":"ok","db":true,"version":"0.3.118-outcome-window-6f60f5a8"}`；
  `qp-api` 为 active。
- 生产 `qp signals outcomes --reconcile --dry-run --limit 20`：
  `checked=5, affected=0, would_revise=0, unchanged=25, waiting_data=0, failed=0`。
- 生产库 `decision_outcome` / `chain_outcome` 部署前后均为 `3700 / 3700`；dry-run 未写入。
- 结论：当前 5 个 allowed 决策的 25 条已存结果与日历窗口一致，无需修订；
  修复从后续 `signals.outcomes` 运行开始生效。

## 4. 冻结与回滚

- 冻结基线文档仍以 `0.3.116` 作为冻结起点参考；`.117` / `.118` 属于冻结期内的
  正确性修复，不改变冻结起点（2026-11-25 11:03 CST）与最早复核日（2026-12-09）。
- 回滚（不降级数据库）：

  ```bash
  sudo /opt/qp/current/deploy/bin/qp-release-activate \
    --release /opt/qp/releases/0.3.117-correctness-batch1-f531e552 \
    --previous /opt/qp/releases/0.3.118-outcome-window-6f60f5a8 \
    --health-url http://127.0.0.1:18800/api/health
  ```

- 上一版 `0.3.117` 的激活收据未在本次会话中直接记录；其 release 目录和
  `qp-pre-0.3.117-20261125T1518.dump` 存在，作为回滚目标保留。

## 5. 证据位置

- 生产：`/opt/qp/current/{RELEASE_NAME,RELEASE_SHA,RELEASE_BASE,CONTENT.sha256.json}`；
  备份路径见 §1。
- 仓库：PR #74 `9a108cc`、PR #73 `cf6e19d`；后端测试 607 passed，覆盖率 89.02%。
