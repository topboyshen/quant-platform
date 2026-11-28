# quant-platform 版本说明（2026-11-28：0.3.122）

> **依据**：`main` 提交 `bf45bc2cccb02afce121ab1afc0b7cadb15d7528`（PR #94 合并提交）。
> 本次有直接生产部署；生产主机 `VM-0-3-ubuntu`，入口 `/next/`。

## 1. 版本与发布身份

| 项目 | 值 |
|---|---|
| 版本 | `0.3.122-backtest-search-recovery-ad12fb37` |
| 源码基线 | `bf45bc2cccb02afce121ab1afc0b7cadb15d7528` |
| 源码 tree | `bc1dad8694c3a5deca32f73e891464159e4174dc` |
| 内容 SHA256 | `ad12fb3779d23199fcefd8d2bcb1995fcc664386b256ca32f10265f43f6f7d76` |
| 内容文件数 | 486（`CONTENT.sha256.json` 自校验通过） |
| 激活时间 | 2026-11-28 16:30:42 CST |
| 激活结果 | `DB_CHECK_OK` → `ACTIVATE_OK` |
| 生产路径 | `/opt/qp/releases/0.3.122-backtest-search-recovery-ad12fb37`（`/opt/qp/current`） |
| 发布键 | `production/bf45bc2cccb02afce121ab1afc0b7cadb15d7528` |
| 数据库 | Alembic head `0017`，无迁移 |
| 部署前备份 | `/var/lib/qp/backups/qp-pre-0.3.122-20261128T1630.dump`（5,750,741 B；sha256 `10e1530bf4b4bafd50727a1085f328e0e9fa3ee4d56a3e52ec7df545bb5c125f`；0600 `qp:qp`） |
| 上一版 / 回滚目标 | `0.3.121-backtest-latest-7dfcb452` / `68902b4e6283f15be409ad5c5dbe70b80d4ebd6c` |

`RELEASE_BASE` 记录为：
`bf45bc2cccb02afce121ab1afc0b7cadb15d7528 + backtest search recovery content sha256 ad12fb3779d23199fcefd8d2bcb1995fcc664386b256ca32f10265f43f6f7d76`。

## 2. 发布内容

- **PR #94**：回测列表搜索在空结果或请求错误后仍可继续使用。
  - 搜索区移出 `AsyncState` 加载层，空态和错误态下搜索框、查询按钮保留；
  - 请求期间禁用输入与查询按钮（含 loading 状态），避免慢请求期间新查询被静默忽略；
  - 重试前清空旧错误，成功后旧错误消失；
  - 保持 #93 的 `?trades=latest` 最近成交与详情计数；
  - 新增前端回归测试：空结果恢复、错误重试、慢请求控件禁用、最近成交详情。
- 前端从精确源码 SHA 在非生产构建机（WSL）重新构建（`npm ci` + `build`）；
  无后端、依赖锁或 Alembic 迁移变更。
- 发布后 `main` 又合入 #95/#96/#97（均文档），不在 `.122` 发布内容内；
  生产仍为 `bf45bc2` 构建的版本。

## 3. 构建与验证

- PR #94 与合并 main 的 CI 各五项全部成功（main run `46120326392`）。
- 同树 WSL 回归：后端 7 通过、前端 73 通过，0 失败、0 跳过；lint、构建通过。
- 486 文件内容清单自校验通过（0 差异）；runtime lock 仍为
  `3a22e1b353b9f83bafd0632ac47f09be2c7a77e14d868690064c64b46c234434`。
- `/api/health` 与 `/next/api/health`：`status=ok`、`db=true`、版本一致；
  `qp-api` active。

## 4. Smoke

- 页面：`/next/`、`/next/login`、`/next/research/backtests`、`/next/api/health`
  均 200；`/next/api/auth/me`、`/next/api/research/backtests` 未登录 401。
- 浏览器实测（真实页面 + 真实线上 API）：
  - 登录后 `/next/research` 96 条；搜 `688002` → 1 条；搜 `zzzz_no_match` →
    0 条并显示「暂无回测」，搜索框与查询按钮仍在；清空 → 96 条；改搜
    `600875` → 13 条。
  - 点击 #96：详情显示 权益点 628 / 成交 30；「最近成交」#30→#1，共 30 行。
- 浏览器模拟（故障/慢请求路径；请求本身为线上真实接口）：
  - 单次拦截列表请求返回 500：页面显示错误、控件保留；原页再次查询重试 →
    恢复 13 条，旧错误消失。
  - 单次延迟 3 秒：请求期间输入框与查询按钮均为 disabled；完成后恢复可用。

## 5. 冻结与回滚

- `.122` 是冻结期内的 correctness 修复，不改变冻结起点（2026-11-25 11:03 CST）
  与最早 Phase 2 复核日（2026-12-09）。
- 本次未触发回滚。回滚目标 `.121` 的 release 目录、前端与 venv 保留；回滚不降级数据库：

  ```bash
  sudo /opt/qp/current/deploy/bin/qp-release-activate \
    --release /opt/qp/releases/0.3.121-backtest-latest-7dfcb452 \
    --previous /opt/qp/releases/0.3.122-backtest-search-recovery-ad12fb37 \
    --health-url http://127.0.0.1:18800/api/health
  ```

## 6. 证据位置

- 生产：`/opt/qp/current/{RELEASE_NAME,RELEASE_SHA,RELEASE_BASE,CONTENT.sha256.json}`
- 发布审计：`/var/lib/qp/audit/production-release-bf45bc2-20261128.json`
- 激活日志：`/var/lib/qp/audit/activate-0.3.122.log`
- 备份：`/var/lib/qp/backups/qp-pre-0.3.122-20261128T1630.dump`
- 发布回执：本空间私信 thread=172 #194
- 上次发布：`RELEASE-NOTES-2026-11-28-0.3.121-zh-CN.md`（0.3.121）
