# Quant EOD 告警口径与日历维护契约（2026-09-08）

## 范围

仅修改 host textfile datasource 统计、对应 Prometheus 告警规则与测试，并新增日历维护 service/timer。无需重建镜像或重启业务。不得补跑 EOD/repair/outcomes、手工更新日历、清除 failed、改生产代理或修改业务数据库。既有未提交改动和备份策略冲突不属于本次发布。

## EOD 指标与告警

- 从最多14天、5000行、8 MiB、12秒预算内的结构化 journal 中按系统 Invocation 分组；选择有真实开始及终态的最近完成事件。事件时间取终态，而不是 textfile 抓取时间。
- K线比例仅为可识别腾讯 K线失败日志 / 可识别成功及失败日志；不是实际HTTP请求率，不包含无法对称识别成功的 total_amount。后者失败独立计数。宏观与K线 fallback 分开，同一 journal cursor 去重，不宣称是请求级别去重。
- `_total` 旧名称仅保留为 deprecated gauge 兼容；不得套用 counter 的 rate/increase。无样本比例为 NaN，另输出 complete/has_samples/parsed，不把没有日志当成功。
- 最新正常运行事件不会抹掉上一完整事件；无开始、已终态但缺记录、或超出45分钟+5分钟退出预算仍无终态的更新事件，显式标记证据不完整。14天窗口不是交易日历新鲜度证明。
- 保留两个原 alertname 以便来源可追踪，但主失败/有fallback分支互斥。共同条件为 K线日志失败比例>0.5 或 total_amount 存在失败日志；后者是独立的成交额异常，不再用不对称分母稀释。
- 文案包含历史事件日期/时刻、Invocation和各计数。告警只说明当轮异常，不断言当前持续失效或所有业务可用。后续 repair/outcomes 成功单独验收，不混入 EOD 分母制造恢复。
- 证据缺失、部分schema缺失、采集过旧和新事件截断有单独 Unknown 告警。历史降级与采集过旧可同时存在，这是两个不同问题；同一历史降级不再同时触发两个主告警。
- 既有宏观风险、磁盘、业务端点及新鲜度阈值不变。日历告警只更新说明，14天阈值不变。

## 日历维护

新增 `research-trading-calendar-refresh.timer`：每日 **06:20 Asia/Shanghai**，`Persistent=false`，无 OnBoot/OnActive 触发，零随机延迟。只启用 timer；不手工启动 service，不追回错过的时点。

service 使用已存在的 `runtime/venvs/swing-20260802/bin/python`，不安装包、不加载业务环境文件。沿用现有 updater，不修改其源码；参数 `--max-age-days 7 --min-ahead-days 30`，新鲜时自然 no-op，超期才联网。150秒启动超时、30秒停止宽限、无自动重启；写权限限于现有 data 父目录以支持锁和原子替换，保留 DNS/HTTPS/V8 所需能力。

首次自然运行需验收 Invocation、Result/退出码、generated_at、count、min/max、最近日期集合及内容散列。exit0可能是锁竞争或新鲜no-op，不足以证明刷新成功。上游/校验失败保留失败审计，等待后续自然时点，不手工补跑、不放宽验证。

## 发布顺序与回退

1. 对旧源码、规则、日历及更新器做CAS校验；只安装精确文件，不全量 add/stash/reset。
2. 独立离线解析测试、promtool规则测试、Linux systemd-analyze verify通过后安装host exporter及日历unit。
3. 启用日历timer，核实NEXT、Persistent=no、service无Invocation和日历字节未变。
4. 等现有5分钟采集器自然产生新schema并被Prometheus抓取后，再发布规则。
5. alerts.yml是单文件bind mount，保留inode原地完整写入+fsync，确认容器SHA相同后POST本地reload；不重建容器。
6. 核实实际加载表达式/注释、rule health、互斥条件与Unknown条件，再仅提交本次精确文件，不push。

部署目录中的原文件只用于本次配置回退，不是恢复任何备份任务。规则加载失败时以保inode方式恢复旧规则并reload；其他阶段失败则保留证据停止，不能通过重启/补跑业务“修复”。
