# Qlib 重叠失败修复评估（2026-11-26）

> 状态：**源端复权因子更正已确认合法；仅完成评估，未执行修复**。没有改写 Qlib
> 历史数据，没有放宽重叠校验，没有绕过 research-data 的 22:30–22:40 安全窗口。
> 生产 Qlib 状态仍为 2026-11-25 的失败记录。

## 1. 结论摘要

- 2026-11-25 22:13，Qlib nightly 增量更新在 `price_conversion` 阶段失败：
  `overlap_mismatch:2026-11-23:close`（源提交 `ahm32sacqodth7459al2e67bdne3qk9o`）。
- 2026-11-26 05:02 的只读诊断使用新源提交
  `k3d6ue4uj53594c7p7d4q42oj7tii94b`，同一日期仍然失败；说明这不是一次性的源抖动。
- 诊断确认：**至少 2049 个标的**在 2026-11-23 的 `close` 与新源不一致。差异集中在北交所：
  - 171 个标的差异 > 5%，全部为 BJ；
  - 83 个标的差异 > 50%，全部为 BJ；
  - 非 BJ 最大差异仅 0.885%（SH601005）；
  - 202 个 BJ 标的全部在差异列表中。
- 这不是“改一个锚点”的问题。Qlib 的转换契约明确写着：历史复权修订
  **不能通过改写旧 feature 字节来修复**；而且 2049 个标的的旧字节改写会破坏
  historical-prefix/manifest 证据。
- **2026-11-26 已确认修订合法**：这是源端 provider `adj_factor` 对账后的复权因子更正，
  不是源错误。源端 `ts_provider_reconciliation` 事件记录了 4721 个变更单元格
  （BJ 220、SH 1980、SZ 2521），并发布为 release tag `2026-11-24`。
- 表面错误日期是 `2026-11-23`，实际修正的是 **2026-11-24 的复权收盘（adjclose）**：
  Qlib 用旧 `11-24` 锚点重建 scale，导致 `11-23` 的归一化值对不上。
- 旧 Qlib 树基于 2026-11-24 10:08 的源提交 `ce2n…`，其中 11-24 的复权值尚未修正；
  源在 2026-11-25 10:14 的 `ahm32…` 已修正。旧树是过期/错误的一方。
- 建议：
  1. **不需要等上游回退**，也不要求恢复旧值；
  2. 构建**新的完整 Qlib release**（新树、新 manifest/tag），staging 验证后原子切换，
     保留旧树回滚；不做原地锚点修补；
  3. 在完整重建完成前保持 research-data degraded，不用窗口外运行或放宽校验来“绕过”。

## 2. 时间线与现状

| 时间（CST） | 事件 |
| --- | --- |
| 2026-11-24 10:08 | 源提交 `ce2n…`：11-24 复权值尚未修正（旧 Qlib 树基于此） |
| 2026-11-25 10:14 | 源提交 `ahm32…`：修正 11-24 复权值；provider 对账事件 `ahm32…-2026-11-25`，发布 `upj4o5…`（tag `2026-11-24`） |
| 2026-11-25 16:50 | 源提交 `k3d6…`，保留修正后的 11-24 复权值 |
| 2026-11-25 22:00 | Qlib nightly 固定源提交 `ahm32…`，目标 2026-11-25 |
| 2026-11-25 22:13 | `price_conversion` 失败：`overlap_mismatch:2026-11-23:close` |
| 2026-11-25 22:13 | release fallback 触发，状态 `pending_retry`（`release_not_published`） |
| 2026-11-25 23:49 | research-data 手工补跑被 `start_after_safe_window` 拒绝，无写入 |
| 2026-11-26 05:02 | 只读诊断使用新源提交 `k3d6…`，同日期再次失败 |
| 2026-11-26 05:09 | 首个详细错误：`BJ920000:2026-11-23:close` |
| 2026-11-26 05:20 | collect-all 诊断得到 2049 条重叠差异；随后另报 `source_absence_before_listing_or_out_of_bounds:SH603595:2026-11-25` |
| 2026-11-26 06:30 | release fallback 的下一次重试（本评估未等待其结果） |
| 2026-11-26 | DoltHub 只读核验确认：修订来自 provider `adj_factor` 对账，合法；旧 Qlib 11-24 锚点过期 |

当前生产 Qlib 仍停留在最后一次成功增量：`baseDate=2026-11-23`、
`targetDate=2026-11-24`、源提交 `ce2nqentchb6rbqci7vgifcv419glbbn`。
`/home/ubuntu/qlib/logs/update-status.json` 仍是 11-25 的 `ahm32…` 失败记录。

## 3. 失败机制

Qlib 的增量追加不是“覆盖旧值”，而是：

1. 取已有 Qlib 树中最后两个有效交易日作为锚点（本次为 2026-11-23、2026-11-24）；
2. 取新源中对应日期的原始行，以及第一个锚点之前最近的有效收盘；
3. 用旧锚点重建 scale，对新源锚点做归一化；
4. 十个字段逐一比较；非原始字段要求 float32 ULP ≤ 8 且相对误差 ≤ 2e-6；
5. 任一字段不一致即 `overlap_mismatch`，整次更新失败；
6. 只有全部标的转换通过后，才会写 staged tree。

关键约束来自 `qlib_dolt_incremental/conversion.py`：

> “a raw-history adjustment/revision is not repaired by rewriting the old feature bytes.”

也就是说，当前失败不是实现遗漏，而是**源历史发生了修订**，而增量路径只允许追加。
`run.py` 的 transaction/manifest 还会校验 historical prefixes、文件大小/身份和旧区间不变；
即使手工改了旧 `.day.bin`，也会破坏这些证据，不能作为受支持的修复。

## 4. 诊断方法与证据边界

### 4.1 方法

使用 **/tmp 下的 Qlib 脚本副本**，只改诊断错误信息和 per-symbol 收集逻辑，然后执行：

```bash
/usr/bin/python3 -B /tmp/qlib-diag/qlib_dolt_incremental/run.py \
  --data-dir /home/ubuntu/qlib/data \
  --dry-run --through 2026-11-25
```

- `--dry-run` 不 promotion；生产 `cn_data` 没有被写入。
- 第一个诊断在遇到 `BJ920000` 时失败，错误中增加了标的、旧值、新值和 scale。
- 第二个诊断在临时副本中捕获每个 `overlap_mismatch` 并继续，得到
  `/tmp/qlib-diag/mismatches.log`（2049 行）。
- 诊断期间没有产生残留 staging 目录；生产进程锁已释放。
- 诊断只写了 dry-run 状态文件 `/home/ubuntu/qlib/data/update-status.json`
  （`mode=dry-run`）；生产状态文件 `/home/ubuntu/qlib/logs/update-status.json` 未被修改。

### 4.2 边界

- 2049 条是**已有标的**的重叠检查结果；new/short-history 标的没有全部覆盖。
- collect-all 之后仍有一个独立错误：
  `source_absence_before_listing_or_out_of_bounds:SH603595:2026-11-25`。
  这说明即使解决重叠，源缺失/上市区间还有第二个门禁需要处理。
- 诊断源提交 `k3d6…` 与 11-25 失败的 `ahm32…` 不同，但 11-24 的复权修正仍在；
  后续核验已确认这是源端永久更正，等待不会自动恢复。

### 4.3 源端核验结果（2026-11-26）

通过 DoltHub API 只读查询，确认了修订的来源和方向：

1. **源端对账事件**：`ts_provider_reconciliation` 中
   `event_id=ahm32…-2026-11-25`，`source_base=ahm32…`，发布提交
   `upj4o5r10qfddsfd4h04m291ep5l77g0`，release tag `2026-11-24`，
   manifest `sha256:d60ef9f…`。
2. **输入**：`adj_factor`（`20261124`、`20261125` 各 5586 行）与 `daily`
   （`20261125`，5573 行）；报告记录 **4721 个变更单元格**：
   BJ 220、SH 1980、SZ 2521；其中 169 个 BJ 变更 >5%。
3. **修正值**：
   - `BJ920000` 11-24：旧 adjclose `12.58` → 新 `13.72`，raw close 仍为 `9.68`；
   - `BJ920662` 11-24：旧 adjclose `15.11` → 新 `198.88`，raw close 仍为 `14.2`。
4. **因子连续性**：
   - `BJ920000`：11-23 因子 `13.68/9.65 = 1.4176`；新 11-24 `13.72/9.68 = 1.4174`；
     旧 11-24 `12.58/9.68 = 1.2996`，明显断裂。
   - `BJ920662`：11-23 因子 `194.11/13.86 = 14.005`；新 11-24 `198.88/14.2 = 14.006`；
     旧 11-24 `15.11/14.2 = 1.064`，明显错误。
5. **scale 对得上**：`ts_provider_factor_scale` 对 `920000.BJ` 记录
   `scale=1.0909020936`，正好对应 Qlib 的 `0.4553928/0.4175541`；
   `920662.BJ` 记录 `scale=13.1628759398`，对应 `0.44560498/0.03385504`。
6. **时间线**：`ce2n…`（11-24 10:08）是旧值；`ahm32…`（11-25 10:14）是修正值；
   Qlib 最后一棵成功树基于旧值，因此下一次追加必然失败。

结论：新源是**合法的复权因子更正**，旧 Qlib 的 11-24 锚点是过期且错误的数据；
不是需要上游撤回的源错误。

## 5. 影响范围

### 5.1 差异统计

| 维度 | 结果 |
| --- | --- |
| 差异标的数 | 2049（5590 个库存标的的 36.7%） |
| 按日期 | 2046 条 2026-11-23；11-24、11-20、11-09 各 1 条 |
| 按字段 | 2048 条 `close`；1 条 `adjclose` |
| 按交易所 | SZ 988；SH 859；BJ 202 |
| > 50% | 83，全部 BJ |
| > 10% | 136，全部 BJ |
| > 5% | 171，全部 BJ |
| > 1% | 195，全部 BJ |
| > 0.1% | 255（197 BJ、38 SH、20 SZ） |
| > 0.01% | 1721 |
| 非 BJ 最大差异 | 0.885%（SH601005） |
| BJ 最大差异 | BJ920662，旧值约为新源的 13.2 倍（相对差 1216%） |
| 源端对账变更 | 4721 个单元格（BJ 220、SH 1980、SZ 2521；169 个 BJ >5%） |

口径说明：2049 是 Qlib 对**已有标的锚点**的重叠检查结果；源端 4721 是 2026-11-25
provider 对账报告中的变更单元格数。两者不是同一口径，但都指向 2026-11-24 的复权修正。

代表值：

- `BJ920000`：旧 `0.4553928375`，新源归一化 `0.4175540739`，scale `0.0305229586`；
  旧值比新源高约 9.1%。对应 11-24 adjclose 从 `12.58` 修正为 `13.72`。
- `BJ920662`：旧 `0.44560498`，新源 `0.03385504`；对应 11-24 adjclose 从 `15.11`
  修正为 `198.88`，是 13.16 倍的复权基准修正，不是价格行情改写。
- `SZ301611`：旧 `1.61265361`，新源 `1.61291383`，相对差约 0.016%；
  这类 SH/SZ 小差异大量存在，但多数低于 0.1%。

### 5.2 消费者

| 消费者 | 读取方式 | 影响 |
| --- | --- | --- |
| `research-data` core writer | `qlib_io` 读取 Qlib 树 | 当前被 Qlib 失败阻塞；日历修复已在 `5b4f207` 部署，但仍无法发布 11-25 |
| `research-data` health | 读取 core DB/Qlib 状态 | 仍 critical/FROZEN；Qlib 修复后需重新审计 |
| qp `market.read` | 读取 Qlib 最近 3 个交易日摘要 | 11-23/24 的 breadth/top 列表可能变化；需重新 ingest/对账 |
| qp `market.ingest_legacy` | 导入 market 摘要并比对 | 可能出现一次 checksum/mismatch，需要按现有对账流程处理 |
| Qlib 研究脚本 | `rolling_backtest_v2.py`、`rolling_train*.py`、`lgb_search.py`、`kronos_demo.py` 等 | 使用 Qlib 历史价的研究/回测会受 2026-11-24 复权修正影响（表现为 2049 个 11-23 重叠失败） |
| `csiall` 指数成员 | BJ920000 等 BJ 标的在 `instruments/csiall.txt` | BJ 修订会影响 csiall 广度/成员价格校验；CSI300/500/800/1000 不含 BJ |
| AStock 早报链 | 已退役 | 不再作为活跃消费者；不应为它恢复旧路径 |

当前 qp 的实时信号/下单路径不直接读 Qlib 价格（以 qp 的 ETF/行情路径为准）；本次修复
主要影响研究、research-data 和市场摘要，不应被描述为实时交易故障。

### 5.3 冻结与观察窗口

M9 的 legacy freeze 观察窗口是 2026-11-18 至 2026-12-01。本次 2026-11-24 复权修正
已发生在窗口内：

- 必须作为**数据更正事件**记录到 M9 证据，不得静默改变历史；
- 依赖 Qlib 的研究输出应标注“11-24 复权修正后重跑”，不能与修正前结果直接拼接；
- 若选择完整重建，应先做 diff 报告，再决定是否需要重跑/重基线；
- 不因为修复 Qlib 而停止/删除 legacy 服务，也不改变 freeze 的停止边界。

## 6. 根因判断（已确认）

2026-11-26 的 DoltHub 只读核验已完成，根因确认如下：

1. **源端 provider `adj_factor` 对账修正了 2026-11-24 的复权收盘**。
   `ts_provider_reconciliation` 事件 `ahm32…-2026-11-25` 记录了 4721 个变更单元格
   （BJ 220、SH 1980、SZ 2521；169 个 BJ >5%），并发布为 `upj4o5…`（tag `2026-11-24`）。
2. **修正的是 adjclose，不是行情价格**。以 BJ920000/BJ920662 为例，raw close、volume、
   amount 均未变化；修正后的 adjclose 与 11-23 的复权因子连续，旧值则明显断裂。
3. **SH/SZ 的小差异同属这次 provider 对账**。2049 个 Qlib 重叠失败中大部分是非 BJ 的
   小差异，最大 0.885%；它们同样会触发 Qlib 的严格重叠校验，但不是行情价格改写。
4. **`SH603595` 的 source absence/上市边界错误**是独立的第二道门禁，不能用
   “忽略重叠”解决；需要和源端/重建路径一起验证。
5. 不是日历问题；research-data 的日历门已单独修复并通过测试。

结论：修订**合法**，旧 Qlib 的 2026-11-24 锚点是过期且错误的数据；继续等待或要求上游
回退都不会恢复。完整重建时应以修正后的源为准，而不是把旧值固化。

## 7. 修复选项

### A. 等待/重试（不作为修复方案）

- 内容：等待 06:30 release fallback 和下一次 nightly。
- 成本：零写入。
- 风险：已确认源端是永久更正，不会回退；fallback 是 GitHub release 路径，不是本地完整重建，
  对“源历史修订”没有修复能力。
- 结论：只作为临时状态，不能作为验收路径。

### B. 上游/源端更正（不适用）

- 内容：要求 Dolt/数据提供方恢复 2026-11-24 的旧复权值。
- 结论：**不适用**。核验已确认新值才是正确值；要求回退会把错误固化。

### C. 构建新的完整 Qlib release（推荐）

- 内容：以修正后的源构建一棵新的 `cn_data` 树（完整历史一致），生成新 manifest/tag，
  在 staging 验证后原子切换；旧树完整保留用于回滚。
- 成本：高。当前树 849 MB、61,781 个文件；staging 需要约 2 倍空间，生产盘当前约 21 GB 可用；
  现有工具是增量 append，不是完整重建，需要补齐构建/验证步骤。
- 风险：2026-11-24 的复权值会变，源端对账涉及 4721 个单元格，Qlib 侧表现为 2049 个标的
  重叠失败（BJ 171 个 >5% 为重大变化）；历史回测/研究输出会变化；若源还修正了更早日期，
  diff 范围会更大。
- 结论：**这是唯一符合 append-only 契约、且不会把旧错误固化下来的修复路径**。

### D. 原地改写旧 `.day.bin` 锚点（拒绝）

- 内容：把 2049 个标的的旧锚点（11-23/11-24 复权值）改成新源值，再让增量通过。
- 成本：看似低，实际是 2049 个文件的历史写入。
- 风险：直接违反 `conversion.py` 的“不得改写旧 feature 字节”契约；破坏
  historical-prefix/manifest/tag 证据；无法区分哪些历史被改；消费者无法审计。
- 结论：**不采用**。

### E. 只重建 BJ + 放宽 SH/SZ 容差（拒绝作为生产修复）

- 内容：重建 202 个 BJ 标的；对 58 个非 BJ 的 >0.1% 差异放宽容差。
- 风险：容差放宽会掩盖真实修订；58 个非 BJ 差异和 1721 个 >0.01% 差异没有解释；
  不能作为通用契约。
- 结论：不采用；最多只能作为隔离研究环境的临时实验，且必须显式标注。

### F. 退役 research-data / 改走 qp 摘要（战略选项）

- 内容：承认 legacy research-data 的 core producer 不再维护，qp 侧使用自己的
  `market.read` 摘要；Qlib 研究仍需要数据修复。
- 成本：属于 M9 退役范围，周期长。
- 风险：不能解决 Qlib 历史不一致；只是减少一个消费者。
- 结论：可与 M9 路线并行讨论，但不能替代本次修复评估。

## 8. 建议执行计划（待授权）

### Phase 0：确认（已完成，2026-11-26）

1. 已核对 `ts_provider_reconciliation`：provider `adj_factor` 对账，4721 个变更单元格，
   发布提交 `upj4o5…`，release tag `2026-11-24`。
2. 已核对 BJ920000/BJ920662 的 11-24 adjclose 修正和 11-23 因子连续性；
   raw close/volume/amount 未变。
3. 已核对 `ts_provider_factor_scale` 的 scale 与 Qlib observed/expected 完全对应。
4. 尚需在 Phase 1/2 处理：`SH603595` 的 source absence/上市边界错误。

### Phase 1：隔离构建（已具备启动条件，待授权）

1. 备份当前 `cn_data`（或至少备份 manifest/tag + 受影响文件清单），记录树哈希。
2. 在独立 staging 构建新树；不覆盖 `/home/ubuntu/qlib/data/cn_data`。
3. 生成 diff 报告：
   - 标的/日期/字段；
   - 相对差分布（BJ vs 非 BJ）；
   - `csiall` 成员与指数影响；
   - 对研究/回测的样本影响。
4. 运行 Qlib 自带测试与 `run.py --dry-run`，要求重叠和 source absence 全通过。

### Phase 2：消费者验收

1. research-data：在下一个 22:30 窗口验证 core writer 能插入 11-25；health 不再 critical。
2. qp：`market.read`、`market.ingest_legacy` 重跑并核对摘要/对账；确认无实时信号影响。
3. 研究脚本：对受影响的 BJ/csiall 回测标注并重跑抽样。
4. 旧树保留，记录回滚命令和回滚演练结果。

### Phase 3：切换/记录

1. 原子切换新树 + 新 tag/manifest；旧树保留为 rollback。
2. 更新 M9 证据和 freeze 记录：数据修订时间、范围、diff 哈希、消费者验收。
3. 在 2026-12-09 复核前确认没有未解释的历史变化。

## 9. 验收标准

- [x] 2026-11-24 复权因子更正性质有源端对账证据，不是猜测（2026-11-26 已确认）。
- [ ] 新树在 staging 通过 Qlib 全部相关测试和 `--dry-run`。
- [ ] diff 报告覆盖 2049 个标的，BJ/非 BJ 分开统计；无未解释差异。
- [ ] `SH603595` 的 source absence/上市边界问题已修复或有明确上游结论。
- [ ] research-data 下一个安全窗口成功写入 11-25；health 审计通过。
- [ ] qp `market.read`/`market.ingest_legacy` 对账完成，无未处理 mismatch。
- [ ] 旧树、manifest/tag、回滚命令和回滚演练记录齐全。
- [ ] M9 证据记录本次数据修订，未静默改写历史.

## 10. 未执行事项

- 未修改 `/home/ubuntu/qlib/data/cn_data` 或任何 `.day.bin`。
- 未放宽 `conversion.py` 的重叠容差，未跳过 source absence/coverage 校验。
- 未修改生产 Qlib 状态文件 `/home/ubuntu/qlib/logs/update-status.json`。
- 未在窗口外运行 research-data core writer。
- 未停止、禁用或删除任何 legacy 服务。
- 诊断脚本只存在于 `/tmp/qlib-diag`，生产代码未被改动。
- 源端核验仅通过 DoltHub API 只读查询，没有写入源仓库。

## 11. 证据路径

- 生产失败状态：`/home/ubuntu/qlib/logs/update-status.json`
- 生产失败日志：`/home/ubuntu/qlib/logs/update_data.log`（run `9b7d5e83…`）
- 诊断日志：`/tmp/qlib-diag/run.log`、`/tmp/qlib-diag/run-collect.log`
- 诊断差异明细：`/tmp/qlib-diag/mismatches.log`（2049 行，临时文件）
- 当前增量 manifest：`/home/ubuntu/qlib/data/cn_data/.qlib-dolt-incremental.json`
- 源端对账：DoltHub `chenditc/investment_data` 的 `ts_provider_reconciliation`，
  event `ahm32…-2026-11-25`，发布 `upj4o5…`
- 源端 scale：`ts_provider_factor_scale`（effective `2026-11-23`，5586 个标的）
- research-data 日历修复：`/home/ubuntu/research-data` 提交 `5b4f207`
- research-data 失败日志：`/home/ubuntu/research-data/snapshot.log`

---

**决策请求**：源端复权因子更正已确认合法（2026-11-26）。是否授权按 Phase 1–3 启动
完整 Qlib release 重建（不改旧字节、保留旧树、先 staging 验证后原子切换）？
