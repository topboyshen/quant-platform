# 数据连续性检查：第一阶段

部署日期：2026-09-07。职责是**观察和留下证据**，不是补采、恢复、备份或发布。

## 边界

`data_health.py` 仅使用 Python 标准库，不导入采集器；SQLite 以 URI `mode=ro`
和 `query_only=ON` 打开，只执行完整性检查及 SELECT。没有网络请求、子进程、
锁文件、数据库写入、配置修改或 Telegram 外发。生产服务额外禁用网络，并将
文件系统设为只读；输出仅进入 systemd journal，不增加另一份数据库或备份。

新 timer 每日 **22:45 CST** 自然执行。22:00 Qlib、22:30 snapshot 的 cron 与
现有告警包装脚本不变，也不迁移它们至 systemd。Persistent=false，不在启动或
重新启用时补跑。22:45 是初始检查宽限，不是已达成的 SLA；若正常采集会超过
15 分钟，应依据运行时长单独评审阈值，不能将一次超时直接解释为数据丢失。

## 状态解释

- `ok`：本次可读本地证据未发现规则问题；不等于全市场完整或上游最新。
- `warning`：历史缺口、单个新增到期缺口、最后采集拒绝/延后、定时产物缺失、
  来源暂存不足或 Qlib 落后于已观察市场日等，需要查看 issues。
- `critical`：两个及以上新增已到期交易日缺口、历史基线行改变/消失、非 FROZEN
  或未来快照日期。这里只记录数据风险，不自动采取恢复动作。
- `unknown`：无法可靠读取/验证关键证据；不得解释为健康。

进程退出 0 表示检查完成，即使 JSON 为 warning/critical；退出 2 表示检查器未
取得可靠结果。**systemd success 不等于数据健康**，必须读取 JSON 的 status/issues。
本阶段不增加 Telegram 告警；现有快照包装脚本仍负责其原有失败通知。

## 日期覆盖与历史保护

审计起点为 2026-08-21，新增监控起点为 2026-09-07。之前的缺口或异常记录标为
legacy，持续保留，不因新日期成功而清除。每日台账从当前 Qlib 本地交易日历与
四只参考证券 close/high/low/amount 的共同完整日构建，而不是将 calendar 最后
一行直接当成完整数据日。读取前后检查 Qlib tag、内嵌 tag 和目录 inode 一致。

这不查询交易所日历或 release API，不扫描全市场，不证明所有日期均可恢复；
未观察到的新交易日不能凭日历推测。若 Qlib 与市场暂存同时停更，也可能缺少
独立时效证据，这是下一阶段需要补充的上游健康维度。

`config/data-health-row-baseline.json` 保护部署时已有五行的精确 payload 字节摘要。
它**不是备份**、不证明原数据语义正确、也不是防恶意篡改的独立签名。8 月 21、
24、25、26 日旧记录含非有限数，未通过现在的严格 JSON 检查；8 月 26 日还存在
日期不一致。保持原样、单独标 invalid；不修写、不删除、不覆盖。
新行会出现在每次报告的 rowHashes 中，但不自动改写固定基线。后续应将这些
报告纳入独立审计保存后，才能声称所有新增行也有跨日不可变性证明。

暂存检查包括私有权限、日期、严格 JSON、schema、payload/record 摘要及市场
字段；最后运行记录声称已封存或使用的暂存也须匹配。只有有效 AkShare 暂存时
仍仅标 `partial_evidence_only`：实时、滚动来源没有当时封存就不能用今天的数据
冒充历史快照。`automaticRecoveryAuthorized=false` 是固定边界。

## 运维查看与回滚

```sh
systemctl status research-data-health.timer --no-pager
journalctl -u research-data-health.service -n 10 --no-pager
```

手动执行检查器只读命令也不触发业务，但没有必要代替自然任务验收。离线测试：

```sh
python3 -B -W error::ResourceWarning -m unittest discover -s tests -p test_data_health.py
```

如需停止本项监控，禁用并停止 **research-data-health.timer** 即可；不要停止或
修改 snapshot/Qlib 调度，不要清空数据库和 journal。进一步卸载仅删除本项目
新增的两个 unit，再 daemon-reload；代码保留在 Git。回滚不需要恢复业务数据。

## 尚未实现

逐来源封存与补偿见 `SOURCE_EVIDENCE_CONTRACT.md`（设计草案）。异地副本需确认
COS/Mac 目标、加密密钥保管和保留期后再实施。当前没有新建备份、上传数据、
开通云资源或执行恢复演练；不能声称 RPO/RTO 已达标。
