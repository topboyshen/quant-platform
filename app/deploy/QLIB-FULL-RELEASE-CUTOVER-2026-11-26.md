# Qlib 完整 release 切换记录（2026-11-26）

> 状态：**Phase 1 已完成，生产树已切换到上游 2026-11-25 完整 release；Phase 2 的 qp
> 侧已验收；research-data core writer 仍需在 22:30 安全窗口验证。**
>
> 没有改写旧 `.day_bin`，没有放宽 Qlib 重叠校验。旧树、旧 manifest/tag 和失败状态均已保留。

## 1. 为什么不是增量追加

源端 2026-11-24 的 provider `adj_factor` 对账修正了 11-24 的复权收盘；Qlib 最后一次成功树
基于修正前的 `ce2n…`，所以增量追加会在 `2026-11-23:close` 失败。进一步 diff 发现旧树
不只是 11-24 一处问题：旧树有大量历史复权值已经过期/错误（见第 4 节），因此按评估结论
选择**完整 release 替换**，而不是原地改锚点。

## 2. 上游 release 证据

| 项 | 值 |
| --- | --- |
| release tag | `2026-11-25` |
| 目标交易日 | `2026-11-25` |
| Dolt source commit | `k3d6ue4uj53594c7p7d4q42oj7tii94b` |
| manifest target | `2026-11-25` |
| archive | `qlib_bin.tar.gz` |
| archive size | `572,936,642` bytes |
| archive SHA-256 | `5feb10587a8f805b3326363360c27d6faf5ce0cf954281fa53931bcfb020b6d3` |
| archive SHA 校验 | 通过（与上游 manifest 一致） |

## 3. 结构/覆盖验证

在 `/home/ubuntu/qlib/work/full-release-20261125/extract/qlib_bin` 和 staging 上执行：

- 日历：6517 行，最后一日 `2026-11-25`。
- `instruments`：6178 个标的；feature 目录 6178 个；两边无缺失。
- 全部 6178 个标的的 10 个 feature 文件均可读取，结构错误 **0**。
- `tree.validate_tree(..., target=2026-11-25)`：`eligible=5578, valid=5578, invalid=0,
  coverage=1.0`。
- `research-data` 的 `generation()` 识别为 release 模式，tag `2026-11-25`；
  `qlib_evidence()` 返回完整日期 `2026-11-25`。
- 上游 release 的 11-24 BJ920000/BJ920662 adjclose 分别为 `13.72`/`198.88`，
  与源提交 `k3d6…` 一致；raw close/volume/amount 未变。

## 4. 旧树 vs release 的历史 diff

对比范围：6178 个共同标的，`adjclose` 字段，旧树 6516 个交易日 vs release 6517 个交易日。

| 阈值 | 差异单元格 | 涉及标的 | BJ / SH / SZ |
| --- | ---: | ---: | --- |
| > 1e-6 | 30,001 | 2,190 | 21,994 / 2,452 / 5,555 |
| > 1e-4 | 28,620 | 1,864 | 21,453 / 1,807 / 5,360 |
| > 5% | 19,193 | 264 | 18,569 / 1 / 623 |

按年份：2019: 728；2020: 839；2021: 983；2022: 3,161；2023: 10,176；2024: 4,859；
2025: 4,129；2026: 5,126。

代表样本：

- `BJ920227` 2025-10-31：旧 adjclose `65706.32`，release `69.82`；
  源提交 `k3d6…` 同日 `69.82`。旧值明显是历史复权因子错误。
- `BJ838227` 2023-10-18：旧 `16156.73`，release `17.22`。
- `BJ920662` 2026-11-24：旧 `15.11`，release `198.88`（源修正）。
- `BJ920000` 2026-11-24：旧 `12.58`，release `13.72`（源修正）。

结论：旧 Qlib 树的历史复权数据存在多年、跨 2,190 个标的的错误，主要是 BJ；完整
release 是修复这些错误所必需的，不是“只补 11-24 一天”。

完整机器可读报告：`/home/ubuntu/qlib/work/full-release-20261125/DIFF-REPORT.json`。

## 5. 切换动作

- 备份当前树：`/home/ubuntu/qlib/backups/full-release-20261125/pre-cn_data`
- 备份旧 tag/manifest/失败状态：同目录 `pre.tag`、`pre-manifest.json`、
  `pre-update-status.json`
- 使用 `atomic.exchange` 原子交换：
  - 旧 active inode `1837078` → 备份路径 `old-live-cn_data`
  - 新 active inode `1115106`
- 新树写入 `.qlib-release=2026-11-25`；`data/.tag` 更新为 `2026-11-25`。
- 新树为 release 模式，不包含 `.qlib-dolt-incremental.json`；`data_health` 已确认
  release generation 可用。
- 旧失败状态已移出生产：
  `/home/ubuntu/qlib/backups/full-release-20261125/superseded/update-status-20261125-failed.json`
- 安装证据：
  `/home/ubuntu/qlib/logs/full-release-install-20261126.json`

## 6. 消费者验收

### qp

- `qp-market-read.service` 于 2026-11-26 07:02 成功重跑（`ok=true`，4.62s）。
- `qp-job@market.ingest_legacy.service` 成功：
  `{"ok": true, "items": 10, "mismatch": []}`。
- qp 生成的 qlib summary：
  - 2026-11-25：`coverage.valid=5578, eligible=5578, invalid=0, ratio=1.0`；
    `breadth up=586, down=4929, flat=63`。
  - 旧树 2026-11-24 的异常 BJ 跌幅（如 `bj920662 -92.22%`、`bj920021 -84.7%`）
    在新 summary 中消失；11-24 最大跌幅变为 `bj920067 -15.55%`、`sh688499 -13.81%`。

### research-data

- `generation()`：tag `2026-11-25`，release 模式。
- `qlib_evidence()`：完整日期 `2026-11-25`。
- **scratch core writer 验证通过**：使用独立的 `/tmp/rd-core-release-*` SQLite，`run_core`
  成功写入 `2026-11-25`，`coverage.valid=5578, eligible=5578`；未写生产 core DB。
- core writer 仍受 22:30–22:40 安全窗口约束；本次切换发生在窗口外，**没有**窗口外运行。
- 生产 core DB 仍停留在旧状态；需要在 2026-11-26 22:30 窗口验证新树写入。

## 7. 待验证/后续

1. **2026-11-26 22:00 Qlib nightly**：从完整 release 继续追加 11-26；应使用
   `legacy_index_absence` 处理已在 11-11 停止报价的指数成员（如 SH600185），不再依赖
   旧增量 manifest。
2. **2026-11-26 22:30 research-data core writer**：在安全窗口内验证能否写入 11-26，
   并运行 health 审计；届时才能确认 degraded 状态是否解除。
3. **Qlib 研究/回测**：本次 diff 涉及 2,190 个标的、30,001 个 adjclose 单元格；
   受影响研究输出需要标注“2026-11-26 full release 后重跑”，不得与旧结果直接拼接。
4. **M9 证据**：在 core writer/health 验证后更新 freeze/退出证据，记录本次数据修订
   范围、diff 报告 hash 和消费者验收结果。

## 8. 回滚

旧树完整保留，可回滚：

```bash
# 仅示例；执行前确认无 updater/writer 正在运行
python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, "/home/ubuntu/qlib/scripts/qlib_dolt_incremental")
import atomic
atomic.exchange(Path("/home/ubuntu/qlib/data/cn_data"),
                Path("/home/ubuntu/qlib/backups/full-release-20261125/old-live-cn_data"))
PY
printf '2026-09-06\n' > /home/ubuntu/qlib/data/.tag
# 如需恢复失败状态：
cp /home/ubuntu/qlib/backups/full-release-20261125/superseded/update-status-20261125-failed.json \
   /home/ubuntu/qlib/logs/update-status.json
```

回滚后需重跑 qp `market.read`/`ingest_legacy`，并确认 research-data core 不在窗口内运行。

## 9. 证据路径

- 上游 release manifest：`/home/ubuntu/qlib/work/full-release-20261125/qlib_bin.manifest.json`
- 上游 archive：`/home/ubuntu/qlib/work/full-release-20261125/qlib_bin.tar.gz`
- 解压树：`/home/ubuntu/qlib/work/full-release-20261125/extract/qlib_bin`
- diff 报告：`/home/ubuntu/qlib/work/full-release-20261125/DIFF-REPORT.json`
- 安装记录：`/home/ubuntu/qlib/logs/full-release-install-20261126.json`
- 旧树：`/home/ubuntu/qlib/backups/full-release-20261125/old-live-cn_data`
- 旧树副本/manifest/tag：`/home/ubuntu/qlib/backups/full-release-20261125/`
- 旧失败状态：`/home/ubuntu/qlib/backups/full-release-20261125/superseded/update-status-20261125-failed.json`
- 评估文档：`app/deploy/QLIB-OVERLAP-ASSESSMENT-2026-11-26.md`
