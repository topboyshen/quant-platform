# 研究链路告警接线

目的：核心晨报的生成、密封、Telegram 与 relay 使用同一个通知状态机。XHS 草稿按需运行，失败留在该命令的退出状态和结果文件中。

## 1. 来源映射（保留来源，不合并）

| 来源 | 触发 | 调用 | cause |
|---|---|---|---|
| 生成 | `astock-xhs-source.service` OnFailure | `research_chain_alert.py --phase generation` | `generation_failed` |
| 密封 | `astock-research-seal.service` OnFailure（drop-in **先清空再指向**） | `--phase post` | `seal_failed` |
| Telegram | 同日发送事件、平台回执与实际附件 | 核心链只读核验 | `telegram_missing` / `telegram_unconfirmed` / `telegram_mismatch` |
| relay | `run_research_relay_with_alert.sh` held 分支 | `--phase post` | `relay_blocked` |
| 截止前 | `research-chain-alert.timer` 工作日 08:12 | `--phase predeadline` | `predeadline_not_ready` |
| 窗口后复查 | 同一 `research-chain-alert.timer` 工作日 08:15 | 同一 `--phase predeadline` 入口，按实际时间分类 | 恢复或窗口结束后的实际故障 |

去重在**通知层**：同日核心链以稳定故障原子判定变化；相同故障不重发，新增、恶化和有正面证据的恢复各通知一次。XHS 状态不参与 collect/classify，也不成为核心成功或恢复证据。旧状态中已有的草稿故障原子保守保留，不由范围调整声明为恢复；本次不改旧证据。

08:15 复查保留 08:12 的同一服务、分类器与状态目录。虽然入口仍名为 `predeadline`，分类器以实际时钟和 08:14 截止点判断：08:12 未就绪、08:13 已成功时，08:15 依据完整回执通知一次恢复；08:12 正常而稍后出现故障时，08:15 通知一次新故障。重复运行不重复通知。它不调用 relay、不延长发送窗口、不补发日报；`Persistent=false` 保持停机后不追补此定时检查。

假日保留独立的 `nng8-research-digest.timer`（08:05/08:10）发送已签名 `research_skip`。`publish-branch.closed` 只关闭日报发布分支，不加到 relay 服务或其 timer 上。假日不生成日报、不发送 Telegram 日报；skip 仍须核验当日 occurrence、冻结上下文及接收端存储回执。确认后的 Telegram 状态为 `not_required`。这仍是现有 relay 发送器，不新增发送入口。

## 2. 目标配置

会话 ID 由 `RESEARCH_CHAIN_TELEGRAM_CHAT` 提供（`~/.config/systemd/user/research-chain-alert.env`，不入库）。

必须加载该文件的单元：

- `research-chain-alert-generation.service`
- `research-chain-alert-post.service`
- `research-chain-alert.service`（08:12 与 08:15 检查；**不得**再写 `Environment=RESEARCH_CHAIN_TELEGRAM_CHAT=%i`，否则会覆盖环境文件）
- `nng8-research-digest.service`（wrapper 继承）

`run_research_relay_with_alert.sh` 在 systemd 之外被直接调用时，也会 source 同一文件。未配置目标时发送器保持 `uncertain`，不自动重试。

## 3. 切换要点

1. 密封 drop-in 使用 `OnFailure=` 清空后再挂 `research-chain-alert-post.service`；草稿 drop-in 仅以空赋值清除自动告警目标，保留草稿命令失败返回值。只停用旧 `astock-seal-failure-alert.service` 不够：旧 `OnFailure` 仍会把它拉起来。
2. 旧告警服务在切换后 **mask**，防止残留依赖再次启动。
3. wrapper **保留 relay 原始退出码**（`exit "$rc"`）。告警调用失败不覆盖该码。
4. 用假发送器做集成验证；真实窗口只读验收，不人为制造故障、不补发。
5. 在空闲窗口先停止现有 `research-chain-alert.timer`，确认已 inactive、没有待执行 job、关联服务空闲，才安装更新文件。保持 timer 停止完成用户 systemd 重载、文件/版本校验和提交，最后只启动该 timer，确认 08:12 与 08:15 的下次触发均在未来，且服务 InvocationID/实际启动时间未变。不得在运行中的 timer 上直接重载新日历：即使 `Persistent=false`，旧的 last-trigger 也可能让新增的过去时刻立即触发。不得手动启动服务做发送验收。服务参数、环境文件、状态目录和状态格式均不变。
6. 提交前的安全回退同样保持 timer 停止，先恢复并核对旧文件，再重载旧规则，最后恢复原先的 timer 启用状态；保留所有告警状态。提交开始后或 timer 状态不明时停止自动文件回退，记录实际提交/待执行 job/启停结果，不盲目重启或修改回执。
