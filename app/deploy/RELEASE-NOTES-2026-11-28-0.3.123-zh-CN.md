# quant-platform 版本说明（2026-11-28：0.3.123）

> **依据**：`main` 提交 `f0c51f12de9368a6cd6f93041af07752c8faa081`（PR #100 合并提交）。
> 本次有直接生产部署；生产主机 `VM-0-3-ubuntu`，入口 `/next/`。

## 1. 版本与发布身份

| 项目 | 值 |
|---|---|
| 版本 | `0.3.123-index-close-union-5f698f20` |
| 源码基线 | `f0c51f12de9368a6cd6f93041af07752c8faa081` |
| 源码 tree | `e97e4e33b5928020bde85fe939c20f470f940b85` |
| 内容 SHA256 | `5f698f203e08d16ecd962c01993b5ef65a94abcd273aad5d4214dd10ff413339` |
| 内容文件数 | 488（`CONTENT.sha256.json` 自校验通过） |
| 激活时间 | 2026-11-28 18:31:14 CST |
| 激活结果 | `DB_CHECK_OK` → `ACTIVATE_OK` |
| 生产路径 | `/opt/qp/releases/0.3.123-index-close-union-5f698f20`（`/opt/qp/current`） |
| 发布键 | `production/f0c51f12de9368a6cd6f93041af07752c8faa081` |
| 数据库 | Alembic head `0017`，无迁移 |
| 部署前备份 | `/var/lib/qp/backups/qp-pre-0.3.123-20261128T1831.dump`（5,756,296 B；sha256 `656d6d8673749ed57e4d3d75a841caadb22b9fad3beb2f994b7fe80e8234a6ef`；0600 `qp:qp`） |
| 上一版 / 回滚目标 | `0.3.122-backtest-search-recovery-ad12fb37` / `1df7e4375b81cae411f6521d803d44b7f7dfb51e` |

`RELEASE_BASE` 记录为：
`f0c51f12de9368a6cd6f93041af07752c8faa081 + index close union content sha256 5f698f203e08d16ecd962c01993b5ef65a94abcd273aad5d4214dd10ff413339`。

## 2. 发布内容

- **PR #100**：指数收盘明细按（指数，交易日）取新平台与旧缓存的并集。
  - 每个键只渲染一行；单侧有值时保留真实收盘值，缺失侧显示 `-`，
    对比列也显示 `-`，不产生数字差值；
  - 双侧按 `|新 - 旧| ≤ 0.011` 判定「一致」，否则显示 `差 <新减旧>`（4 位小数）；
  - 排序为日期降序、同日指数升序；
  - 新增前端回归测试覆盖单侧/双侧/排序。
- 前端从精确源码 SHA 在非生产构建机（WSL）重新构建；无后端、依赖锁或迁移变更。
- 发布后 `main` 又合入 #101（文档），不在 `.123` 发布内容内。

## 3. 构建与验证

- PR #100 CI run `46134650479` 与合并 main CI run `46135148051` 各五项全部成功。
- WSL 同树：前端 75 通过、后端全量（含资产）614 通过，0 失败、0 跳过；lint、构建通过。
- 488 文件内容清单自校验通过（0 差异）；runtime lock 仍为
  `3a22e1b353b9f83bafd0632ac47f09be2c7a77e14d868690064c64b46c234434`。
- `/api/health` 与 `/next/api/health`：`status=ok`、`db=true`、版本一致；
  `qp-api` active。

## 4. 功能实测（登录后，真实线上只读）

- 窗口 60/120/365 天均返回 200；实际日期 `2026-10-29 → 2026-11-27`
  （数据只覆盖该区间，三个窗口结果相同）。
- 原始行数 136（qp 88 + legacy 48）；去重键 88；仅新 40、仅旧 0、共同 48；
  越界差值 0；最大 `|差|` 0.005。
- 明细 5 页：20/20/20/20/8，共 88 行；日期降序、同日指数升序。
- 单侧行显示 新平台值 / 来源 / 旧缓存 `-` / 对比 `-`。例：
  `sz399006 2026-11-10 = 2494.4 / tencent / - / -`；
  `sh000001 2026-11-16 = 3602.57 / tencent / - / -`。
- 双侧行显示「一致」。例：`sh000001 2026-11-11` 新 3680.64 / 旧 3680.643 /
  差 `-0.0030`；`sz399006 2026-11-27` 新 2443.96 / 旧 2443.964 / 差 `-0.0040`。
- 现场「仅旧」计数为 0，按真实计数记录；该场景由 WSL Chromium 夹具 7/7 行
  （仅新 2、仅旧 2、共同 3）单独作为夹具证据。
- 未修改生产行情数据。

## 5. 冻结与回滚

- `.123` 是冻结期内的 correctness 修复，不改变冻结起点（2026-11-25 11:03 CST）
  与最早 Phase 2 复核日（2026-12-09）。
- 本次未触发回滚。回滚目标 `.122` 的 release 目录、前端与 venv 保留；回滚不降级数据库：

  ```bash
  sudo /opt/qp/current/deploy/bin/qp-release-activate \
    --release /opt/qp/releases/0.3.122-backtest-search-recovery-ad12fb37 \
    --previous /opt/qp/releases/0.3.123-index-close-union-5f698f20 \
    --health-url http://127.0.0.1:18800/api/health
  ```

## 6. 证据位置

- 生产：`/opt/qp/current/{RELEASE_NAME,RELEASE_SHA,RELEASE_BASE,CONTENT.sha256.json}`
- 发布审计：`/var/lib/qp/audit/production-release-f0c51f12-20261128.json`
- 激活日志：`/var/lib/qp/audit/activate-0.3.123.log`
- 备份：`/var/lib/qp/backups/qp-pre-0.3.123-20261128T1831.dump`
- 发布回执：本空间私信 thread=201 #219
- 上次发布：`RELEASE-NOTES-2026-11-28-0.3.122-zh-CN.md`（0.3.122）
