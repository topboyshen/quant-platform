# 研究链路告警接线（**未安装**）

目的：把四条故障来源保留各自语义，统一到同一个通知状态机，避免新旧各发一条。
状态：本文件与 `systemd/` 下的单元为**待接线提案**，尚未安装、未启用。

## 1. 来源映射（保留来源，不合并）

| 来源 | 触发 | 调用 | cause |
|---|---|---|---|
| 生成 | `astock-xhs-source.service` OnFailure（新增） | `research_chain_alert.py --phase generation` | `generation_failed` |
| 密封 | `astock-research-seal.service` OnFailure（改指向） | `--phase post`（读密封绑定与报告哈希） | `seal_failed` |
| 草稿 | `astock-xhs-draft.service` OnFailure（改指向） | `--phase post` | `draft_failed` |
| relay | `run_research_relay_with_alert.sh` held 分支（改指向） | `--phase post`（journal 读真实 reason） | `relay_blocked` |
| 截止前 | `research-chain-alert.timer` 工作日 08:12 | `--phase predeadline` | `predeadline_not_ready` |

去重在**通知层**：一天一个事故（`<日期>:research_chain`），各来源作为并列 cause；当天已通知后的新来源只更新记录并写 `same_day_suppressed` 日志。

## 2. 待应用的改动（安装时才生效）

1. 新增两个单元：`research-chain-alert.{service,timer}`（08:12 只读检查）。
   - 会话 ID 由 `RESEARCH_CHAIN_TELEGRAM_CHAT` 提供（沿用已确认的 `TELEGRAM_HOME_CHANNEL` 解析结果，不写入仓库）。
2. `astock-xhs-source.service`：`[Unit]` 增加 `OnFailure=research-chain-alert@generation.service`（或同效的 oneshot 包装）。
3. `astock-xhs-draft.service.d/failure-alert.conf`：`OnFailure` 由 `astock-seal-failure-alert.service` 改为本研究链路告警服务，语义由 `draft_failed` 承载。
4. `astock-research-seal.service.d/failure-alert.conf`（新增）：`OnFailure` → 本研究链路告警服务，语义由 `seal_failed` 承载。
5. `run_research_relay_with_alert.sh`：`held` 分支由直接调用 `morning_seal_alert.sh relay_held` 改为调用新模块 `--phase post`；旧脚本保留 `--dry-run-notify` 仅供对照。
6. 旧 `astock-seal-failure-alert.service` 在切换后停用（避免并行直发）。

## 3. 切换顺序（一次完成）

1. 配置 `RESEARCH_CHAIN_TELEGRAM_CHAT`；
2. 演练通过（新发送器 + 独立状态目录 + 「演练」标记，核对回执与实际收到）；
3. 安装 timer 与 OnFailure，改 wrapper，停用旧告警服务；
4. 同一天同一事故校验只发出 1 条（其余 `same_day_suppressed`）；
5. 下一次真实窗口只读验收，不人为制造故障。
