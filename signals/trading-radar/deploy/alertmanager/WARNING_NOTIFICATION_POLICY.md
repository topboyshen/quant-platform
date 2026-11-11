# Quant 普通告警长期免打扰策略

用户授权：2026-09-08，要求当前三类普通告警的免打扰长期有效。
追加授权：2026-09-15，将 `QuantDatasourceEODEvidenceUnknown` 纳入同一精确 warning 路由；严重故障通知保留。

## 固定范围

以下条件同时成立时，路由到没有通知集成的 `quant-warning-audit-only` 接收器：

- `severity="warning"`。
- alertname 为 `QuantDatasourceSilentFallbackRisk`、`QuantPrimaryDatasourceHighFailureRate`、`QuantTradingCalendarRefreshOverdue`、`QuantDatasourceEODEvidenceUnknown` 之一（完整名称匹配）。
- instance 为 `node-exporter:9100` 或 `runtime-observer:9116`。

匹配分支置于根路由的首个子分支，`continue=false`。没有期限、不分时段，不会在09:00自动恢复。通知规则变更、服务升级和配置同步时应保留此正式策略，除非用户明确修改。

## 保留的行为

- 这些 warning 仍由 Prometheus 判定、在看板展示、保留现有日志与审计；不是禁用监控或把故障改为成功。
- 不向 Telegram 推送这些告警的 firing、repeat 或 resolved 消息。
- 同名告警若变为 critical，不匹配此分支；严重故障及其他告警仍使用原 Telegram 接收器。
- 原默认接收器、group_by/group_wait/group_interval/repeat_interval、Telegram凭据/模板、critical抑制warning规则均保持不变。
- 不修改业务、数据源、代理、数据库、日历timer、备份任务或 Prometheus 告警条件。

## 发布与验收

Alertmanager 配置为单文件 bind mount；发布时必须保持 inode，并在原文件 SHA 校验后写入、fsync，确认容器读取相同 SHA 后热加载，不重建容器。真实已加载路由（不使用候选文件参数）应验证四种 warning→audit-only，同名 critical/info、其他告警、不同或缺失 instance→原 Telegram 接收器。验证不应实际注入告警或发送测试消息。

2026-09-08 的临时 silence `777e3780-41e6-447f-974c-caba0d4a4a7d` 已在长期路由生效后结束；2026-09-15 不新建或续期任何 silence。长期效果依赖已保存配置，而不是一个不断续期的临时静音。

此策略不能撤回已发出或在途的历史消息。用户如希望恢复这些通知，应单独修改此精确分支，不应全局打开/关闭所有告警。
