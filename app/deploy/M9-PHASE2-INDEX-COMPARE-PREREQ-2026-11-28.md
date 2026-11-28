# Packet E 前置：`index_close.legacy_vs_qp` 退役方案（2026-11-28）

> **草案，未授权改代码或停服。** Packet E（`research-index-close-cache.timer`）
> 当前为 **blocked**：qp `market.compare` 的必需项 `index_close.legacy_vs_qp`
> 依赖 legacy 最近 20 天数据；停掉 legacy timer 约 20 天后该项变 `skipped`，
> `compare.latest` 从 `ok` 转 `unknown`。本文档只整理可选方案、影响和顺序，
> 供 2026-12-09 复核后决策。

## 1. 约束与现状

- `REQUIRED_COMPARE_ITEMS`（`app/backend/qp/jobs/market_compare.py`）硬编码 5 项，
  其中 `index_close.legacy_vs_qp` 取 legacy `market.index_close` 最近 20 天的行。
- `compare_latest` 只把 `match`/`mismatch` 计入 `compared`；`skipped` 计入
  `missing`，因此必需项一旦 skipped，`compare.latest` 不再是 `ok`。
- 该项 `mismatch` 会创建 `market:compare:index_close.legacy_vs_qp` 告警；
  `skipped` 只是 `keep_open`，不会新建告警，但健康卡会降级。
- 当前：compare 五项全 `match`；legacy 48 行（2026-11-11→11-27），
  qp 88 行（10-29→11-27）；`market.ingest_legacy` 仍读取 legacy JSON。
- 相关测试：`app/backend/tests/test_m3_verdict.py`（必需项/缺失/skipped）、
  `app/backend/tests/test_m3.py`（per-item alert）。

## 2. 可选方案

### 方案 A — 直接退役该项

- 从 `REQUIRED_COMPARE_ITEMS` 移除 `index_close.legacy_vs_qp`；`compare()` 仍生成
  该行，但 `compare_latest` 不再要求它，detail 里作为 `extra` 展示。
- 优点：改动最小；停 E 后 compare 保持 `ok`。
- 缺点：release 一上线就失去 legacy/qp 指数对账覆盖，即使 legacy 还在跑；
  若 legacy 在过渡期出现真实差异，不再阻断。

### 方案 B — 按 legacy 新鲜度动态要求（推荐）

- `compare_latest` 先看该项是否为 `skipped` 且 legacy 最大交易日已超过
  N 个交易日（建议 N=5）：是则把该项从本次 required 集合移除，detail 标记
  `retired: ["index_close.legacy_vs_qp"]`、`reason=legacy_producer_stale`；
  否则维持现状（fresh legacy 时 skipped 仍算 missing）。
- 优点：legacy 还在跑时保留完整对账；停 E 后经过 5 个交易日自动退出必需集，
  不需要精确卡发版时间；健康卡能看到显式 `retired` 原因。
- 缺点：`compare_latest` 逻辑略复杂，需要新增测试：
  1. legacy 新鲜 + skipped → 仍 `unknown`（保持现有测试）；
  2. legacy 过期 + skipped → `ok` 且 detail 有 `retired`；
  3. legacy 过期 + mismatch → 仍 `warning`（mismatch 永远优先）。
- 实现边界：只影响 `index_close.legacy_vs_qp`，不改其他 4 个必需项；
  不改 `compare()` 的采集逻辑；不动 `market.ingest_legacy`。

### 方案 C — 保留 legacy producer

- 不停 `research-index-close-cache.timer`，把它移出 Phase 2 停止清单。
- 优点：零代码、零监控变化。
- 缺点：保留一个已可由 qp 替代的 legacy timer；与 Phase 2 目标相悖，
  但可作为“先不动”的保守选项。

## 3. 推荐与顺序

推荐 **方案 B**，顺序如下（全部需要单独授权）：

1. 2026-12-09 复核通过后，用户批准 Phase 2 及该项代码变更；
2. 走正常 release 流程（预计 `.123`）：仅后端 `market_compare.py` + 测试，
   无迁移、无依赖锁变更；
3. 上线后先验证 legacy 仍新鲜时 compare 五项 `ok`；
4. 再停 Packet E，观察 5 个交易日，确认 `compare.latest` 仍 `ok` 且 detail
   出现 `retired`；
5. 若 compare 异常或健康卡未按预期，恢复 legacy timer 并回滚 release。

## 4. 回滚

- 仅回滚 release：恢复旧的必需项逻辑；如果此时 E 已停且 legacy 已过期，
  compare 会回到 `unknown`，因此回滚 release 必须同时
  `sudo systemctl enable --now research-index-close-cache.timer`。
- 仅恢复 legacy timer：不改变 release，compare 在下一轮恢复 `match`。
- 两者都不降级数据库；无数据删除。

## 5. 验收

- 单测覆盖方案 B 的三种情况；`test_m3_verdict.py` 全通过。
- release 后 `market.compare` 五项 `ok`（legacy 新鲜时）。
- 停 E 后 5 个交易日：`compare.latest` 仍 `ok`，detail `retired` 含该项，
  无 `market:compare:*` 新告警。
- qp `market.index_close` 与 `market.health` 的 `index_close.qp_current`
  持续 `ok`。

## 6. 证据

- `M9-PHASE2-PREP-2026-11-28.md` §11
- `M9-PHASE2-EXECUTION-PACKETS-2026-11-28.md` Packet E
- 代码：`app/backend/qp/jobs/market_compare.py`、`app/backend/tests/test_m3_verdict.py`
