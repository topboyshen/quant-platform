# AStock 晨报唯一调度入口

以下是本仓候选模板的职责边界，不是已执行的服务器操作；部署后仍须核对实际生效的 user 单元。

## 自动拓扑

工作日 08:00（Asia/Shanghai）仅由既有 `astock-xhs-source.timer` 启动 source-only 工作流。runner 的兼容文件名仍含 `cron`，但没有安装 cron 的能力，也不发送消息。

```text
user timer → 唯一固定编排入口 → 数据桥 → 事实编译与发布门禁
    ├─ 正常休市 → 写入同次 marker，source→seal 日报发布分支结束
    └─ 交易日成功 → research-seal → Telegram / 结构化 relay
```

XHS 草稿与模型研究只按需运行，不是每日投递前提。已有归档和事件遵循自身封存身份，不重渲染历史以适配当前设置。timer 的 `Persistent=false`：错过窗口不能自动追补。

休市不生成日报，也不发送 Telegram 日报。独立 relay 的既有定时任务仍可提交签名的 `research_skip` 状态回执；它不属于 source→seal 日报发布分支，marker 不关闭这条回执合同。

定时 occurrence 的截止时间由 `config/workflow.json` 的 `timezone`、`scheduledTime` 推导，继续执行项目既有的 Asia/Shanghai 08:00 固定约束，而 `startedAt` 单独记录实际启动时刻。08:00:30 启动或同次 08:10 重试，仍使用该日 08:00 截止。新 occurrence 使用 `schemaVersion=2.0` 并保存 `frozenCutoffInstant`；v2 读取器按版本核验目标日精确 `00:00:00.000Z`，不读取当前配置重解释历史。删除或篡改该字段不会按旧记录回退，即便关联文件自洽，错误的08:30截止仍被拒绝。显式按需观察时间保持原语义。将来调整计划时间需明确升级合同版本。

旧 `schemaVersion=1.0` occurrence 继续按其原始 `startedAt` 截止身份验证，不修改历史归档或密封件。回退边界：尚未产生新 occurrence 时可整组回退；产生 v2 状态后，旧发送读取器会拒绝延迟启动记录，须保留兼容读取器或前向修复，不得改写 occurrence／密封件来绕过旧门禁。

## 已退役的安装/修复入口

`install-morning-schedule.sh` 和 `repair-morning-schedule.sh` 保留名称以给旧调用者明确说明，但均立即退出 78，不安装 cron/watchdog，不拉取代码，不采集数据，不补跑。它们不是健康检查或恢复命令。

任何实际安装、变更单元、重载或手工恢复均须单独审查和授权；不能把代码更新、实时采集、自动补跑与调度修复混做一键操作。

## 只读状态核对

```bash
systemctl --user show astock-xhs-source.timer -p ActiveState -p NextElapseUSecRealtime
systemctl --user show astock-xhs-source.service -p Result -p ActiveState -p OnSuccess
systemctl --user show astock-research-seal.service -p Result -p OnSuccess -p OnFailure
npm run task:status
npm run notify:status
```

服务退出成功不是业务成功证明。交易日逐层核对本次生成、密封、Telegram 平台回执与 relay 摘要接收证据；休市核对入口结束、marker 同次绑定、日报分支未误触发，并单独核对独立 relay 的签名 `research_skip` 回执。缺本次证据保持证据不足，不以旧成功替代，不为验收主动重跑。
