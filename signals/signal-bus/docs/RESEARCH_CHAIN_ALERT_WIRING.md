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

去重在**通知层**：同日核心链以稳定故障原子判定变化；相同故障不重发，新增、恶化和有正面证据的恢复各通知一次。XHS 状态不参与 collect/classify，也不成为核心成功或恢复证据。旧状态中已有的草稿故障原子保守保留，不由范围调整声明为恢复；本次不改旧证据。

## 2. 目标配置

会话 ID 由 `RESEARCH_CHAIN_TELEGRAM_CHAT` 提供（`~/.config/systemd/user/research-chain-alert.env`，不入库）。

必须加载该文件的单元：

- `research-chain-alert-generation.service`
- `research-chain-alert-post.service`
- `research-chain-alert.service`（08:12 检查；**不得**再写 `Environment=RESEARCH_CHAIN_TELEGRAM_CHAT=%i`，否则会覆盖环境文件）
- `nng8-research-digest.service`（wrapper 继承）

`run_research_relay_with_alert.sh` 在 systemd 之外被直接调用时，也会 source 同一文件。未配置目标时发送器保持 `uncertain`，不自动重试。

## 3. 切换要点

1. 密封 drop-in 使用 `OnFailure=` 清空后再挂 `research-chain-alert-post.service`；草稿 drop-in 仅以空赋值清除自动告警目标，保留草稿命令失败返回值。只停用旧 `astock-seal-failure-alert.service` 不够：旧 `OnFailure` 仍会把它拉起来。
2. 旧告警服务在切换后 **mask**，防止残留依赖再次启动。
3. wrapper **保留 relay 原始退出码**（`exit "$rc"`）。告警调用失败不覆盖该码。
4. 用假发送器做集成验证；真实窗口只读验收，不人为制造故障、不补发。
