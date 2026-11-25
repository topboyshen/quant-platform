# Qlib 重叠失败修复评估（2026-11-26）

> 状态：**仅评估，未执行修复**。没有改写 Qlib 历史数据，没有放宽重叠校验，没有绕过
> research-data 的 22:30–22:40 安全窗口。生产 Qlib 状态仍为 2026-11-25 的失败记录。

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
- 建议顺序：
  1. **先确认 2026-11-23 的 BJ 修正是官方复权/数据更正，还是源错误**；
  2. 若源正确：构建**新的完整 Qlib release**（新树、新 manifest/tag），原子切换并保留旧树，
     不做原地锚点修补；
  3. 若源错误：要求上游恢复，再重试，不在本地写历史；
  4. 在确认前保持 research-data degraded，不用窗口外运行或放宽校验来“绕过”。

## 2. 时间线与现状

| 时间（CST） | 事件 |
| --- | --- |
| 2026-11-25 22:00 | Qlib nightly 固定源提交 `ahm32…`，目标 2026-11-25 |
| 2026-11-25 22:13 | `price_conversion` 失败：`overlap_mismatch:2026-11-23:close` |
| 2026-11-25 22:13 | release fallback 触发，状态 `pending_retry`（`release_not_published`） |
| 2026-11-25 23:49 | research-data 手工补跑被 `start_after_safe_window` 拒绝，无写入 |
| 2026-11-26 05:02 | 只读诊断使用新源提交 `k3d6…`，同日期再次失败 |
| 2026-11-26 05:09 | 首个详细错误：`BJ920000:2026-11-23:close` |
| 2026-11-26 05:20 | collect-all 诊断得到 2049 条重叠差异；随后另报 `source_absence_before_listing_or_out_of_bounds:SH603595:2026-11-25` |
| 2026-11-26 06:30 | release fallback 的下一次重试（本评估未等待其结果） |

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
- 诊断源提交 `k3d6…` 与 11-25 失败的 `ahm32…` 不同，但 11-23 的修订仍在；
  不能假设“等下一个源提交”会自动恢复。

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

代表值：

- `BJ920000`：旧 `0.4553928375`，新源归一化 `0.4175540739`，scale `0.0305229586`；
  旧值比新源高约 9.1%。
- `BJ920662`：旧 `0.44560498`，新源 `0.03385504`；这是数量级差异，
  不像单个交易日的普通价格修正。
- `SZ301611`：旧 `1.61265361`，新源 `1.61291383`，相对差约 0.016%；
  这类 SH/SZ 小差异大量存在，但多数低于 0.1%。

### 5.2 消费者

| 消费者 | 读取方式 | 影响 |
| --- | --- | --- |
| `research-data` core writer | `qlib_io` 读取 Qlib 树 | 当前被 Qlib 失败阻塞；日历修复已在 `5b4f207` 部署，但仍无法发布 11-25 |
| `research-data` health | 读取 core DB/Qlib 状态 | 仍 critical/FROZEN；Qlib 修复后需重新审计 |
| qp `market.read` | 读取 Qlib 最近 3 个交易日摘要 | 11-23/24 的 breadth/top 列表可能变化；需重新 ingest/对账 |
| qp `market.ingest_legacy` | 导入 market 摘要并比对 | 可能出现一次 checksum/mismatch，需要按现有对账流程处理 |
| Qlib 研究脚本 | `rolling_backtest_v2.py`、`rolling_train*.py`、`lgb_search.py`、`kronos_demo.py` 等 | 使用 Qlib 历史价的研究/回测会受 2049 个标的 11-23 修订影响 |
| `csiall` 指数成员 | BJ920000 等 BJ 标的在 `instruments/csiall.txt` | BJ 修订会影响 csiall 广度/成员价格校验；CSI300/500/800/1000 不含 BJ |
| AStock 早报链 | 已退役 | 不再作为活跃消费者；不应为它恢复旧路径 |

当前 qp 的实时信号/下单路径不直接读 Qlib 价格（以 qp 的 ETF/行情路径为准）；本次修复
主要影响研究、research-data 和市场摘要，不应被描述为实时交易故障。

### 5.3 冻结与观察窗口

M9 的 legacy freeze 观察窗口是 2026-11-18 至 2026-12-01。Qlib 历史修订如果发生在本窗口内：

- 必须作为**数据更正事件**记录到 M9 证据，不得静默改变历史；
- 依赖 Qlib 的研究输出应标注“11-23 数据修订后重跑”，不能与修订前结果直接拼接；
- 若选择完整重建，应先做 diff 报告，再决定是否需要重跑/重基线；
- 不因为修复 Qlib 而停止/删除 legacy 服务，也不改变 freeze 的停止边界。

## 6. 根因判断

目前证据支持以下判断，但**尚未完成官方数据核验**：

1. **BJ 复权/价格基准被整体修订**。202 个 BJ 全部在差异列表中，171 个 >5%，
   83 个 >50%；这不像单只股票的除权，更像北交所价格/复权基准的一次系统性修正。
2. **SH/SZ 存在大量轻微修订**。2049 个标的中大部分是非 BJ 的小差异，最大 0.885%；
   可能是源端重新计算/舍入，也可能是数据更正。它们同样会触发 Qlib 的严格重叠校验。
3. **`SH603595` 的 source absence/上市边界错误**是独立的第二道门禁，不能用
   “忽略重叠”解决；需要和源端/重建路径一起验证。
4. 不是日历问题；research-data 的日历门已单独修复并通过测试。

在确认第 1、2 条的性质前，不应选择修复方案：如果源是错的，完整重建会把错误固化；
如果源是对的，继续等待只会无限期阻塞。

## 7. 修复选项

### A. 等待/重试（不作为修复方案）

- 内容：等待 06:30 release fallback 和下一次 nightly；源提交回退则自动恢复。
- 成本：零写入。
- 风险：源修订已跨两个提交仍存在；fallback 是 GitHub release 路径，不是本地完整重建，
  对“源历史修订”没有修复能力。
- 结论：只作为临时状态，不能作为验收路径。

### B. 上游/源端更正（若修订是错误）

- 内容：让 Dolt/数据提供方恢复 2026-11-23 的 BJ/SH/SZ 旧值，或明确发布更正说明；
  本地不写历史，等新提交后重试。
- 成本：取决于上游。
- 风险：不可控；若上游认为新值才是正确的，会长期阻塞。
- 结论：**先核验，若是源错误则优先**。

### C. 构建新的完整 Qlib release（推荐，若修订合法）

- 内容：以当前源提交构建一棵新的 `cn_data` 树（完整历史一致），生成新 manifest/tag，
  在 staging 验证后原子切换；旧树完整保留用于回滚。
- 成本：高。当前树 849 MB、61,781 个文件；staging 需要约 2 倍空间，生产盘当前约 21 GB 可用；
  现有工具是增量 append，不是完整重建，需要补齐构建/验证步骤。
- 风险：2026-11-23 的 2049 个标的旧值会变（BJ 171 个 >5% 为重大变化）；历史回测/研究输出
  会变化；若源还修订了更早日期，diff 范围会更大。
- 结论：**在确认源修订合法后，这是唯一符合 append-only 契约的修复路径**。

### D. 原地改写旧 `.day.bin` 锚点（拒绝）

- 内容：把 2049 个标的的 11-23 旧值改成新源值，再让增量通过。
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

### Phase 0：确认（必须先做）

1. 核对 2026-11-23 的 BJ920000、BJ920662 等样本：
   - 交易所/公司公告是否有除权、拆股、复权基准调整；
   - Dolt 源提交 `ahm32…` / `k3d6…` 的修订说明；
   - 旧 Qlib 值（11-23）与新源值哪个与官方收盘/复权价一致。
2. 确认 SH/SZ 的 2049 条小差异是舍入/重算还是实质修订。
3. 单独确认 `SH603595:2026-11-25` 的 source absence/上市边界错误。

### Phase 1：隔离构建（仅确认源合法后）

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

- [ ] 2026-11-23 修订性质有官方/源端证据，不是猜测。
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

## 11. 证据路径

- 生产失败状态：`/home/ubuntu/qlib/logs/update-status.json`
- 生产失败日志：`/home/ubuntu/qlib/logs/update_data.log`（run `9b7d5e83…`）
- 诊断日志：`/tmp/qlib-diag/run.log`、`/tmp/qlib-diag/run-collect.log`
- 诊断差异明细：`/tmp/qlib-diag/mismatches.log`（2049 行，临时文件）
- 当前增量 manifest：`/home/ubuntu/qlib/data/cn_data/.qlib-dolt-incremental.json`
- research-data 日历修复：`/home/ubuntu/research-data` 提交 `5b4f207`
- research-data 失败日志：`/home/ubuntu/research-data/snapshot.log`

---

**决策请求**：请先确认 2026-11-23 的 BJ/SH/SZ 修订是官方更正还是源错误。若确认合法，
是否授权按 Phase 1–3 做完整 Qlib release 重建（不改旧字节、保留旧树、先 staging 后切换）？
