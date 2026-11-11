# Quant-Server Hermes Telegram 恢复报告（2026-08-28）

## 结论

Quant-Server 上真正的 Hermes 为用户级 `hermes-gateway.service`，并非 dedirock 的 Mini Hermes。本次持续性全断故障已经恢复，根因是 `radar-mihomo` 的旧代理节点池整体失效；Hermes 进程、Telegram token 和轮询唯一性均不是根因。

恢复期间没有发送测试消息、没有修改 token、没有补跑 Swing、没有重启 Hermes，也没有重启其他业务容器。唯一重建对象是 `radar-mihomo`。

## 故障证据

- `hermes-gateway.service` 一直 active/running，但恢复前两小时有 139 条 Telegram 连接失败，成功连接为 0。
- Telegram 直连不可用；经 HTTP `127.0.0.1:7890` 和 SOCKS `127.0.0.1:7891` 访问 Telegram、gstatic 均在 TLS 阶段超时。
- Mihomo 容器显示 healthy，但健康检查仅验证配置语法。
- 生产配置停留在 2026-07-09，旧快照的 42 个叶子节点健康延迟全部为 0。
- Mihomo 日志显示 Telegram、Google、Yahoo 等不同目标均在上游节点发生 `i/o timeout`，证明不是 Telegram 单点故障。
- 2026-08-28 15:30 Swing EOD 因代理相关数据源失败落为 `macro_data_partial`；15:50 watchdog 又因 Telegram 通知失败退出。

## 恢复过程

1. 通过 TLS 校验的订阅地址取得 40 个新节点。
2. 修复合并器：同步更新 14 个策略组，避免只替换节点后留下失效引用；对当前 VLESS TCP + Reality 输入采用 fail-closed 校验。
3. 使用当前已部署 Mihomo 精确镜像 ID，在 `--network none` 的隔离容器中验证配置语法。
4. 在隔离代理实例中验证 Telegram HTTP 302、gstatic HTTP 204。
5. 保存生产配置、合并器和三个 selector 的回滚快照。
6. 原子替换配置，只受控重建 `radar-mihomo`，并固定使用已有镜像，不拉取新镜像。
7. 将 Telegram、Google、漏网之鱼三个策略组切到已验证节点。

生产配置：

```text
SHA256  3fbfcd26c32f128343bd7f71fe9b27a4d130670e05916cedca68453087ba8a81
```

回滚文件：

```text
/home/ubuntu/mihomo/config.yaml.bak.20260828-195124-hermes-telegram
/home/ubuntu/mihomo/merge_subscription.py.bak.20260828-195124-hermes-telegram
```

运行镜像：

```text
sha256:e6acd921addecfd59a8e2d38203f88356d635b54de6c0673db0e015139989312
```

## 验收结果

| 检查 | 结果 |
|---|---|
| HTTP 7890 → Telegram | 连续 3/3 返回 302 |
| SOCKS 7891 → Telegram | 连续 3/3 返回 302 |
| HTTP 7890 → gstatic | 连续 3/3 返回 204 |
| SOCKS 7891 → gstatic | 连续 3/3 返回 204 |
| Hermes 进程 | 原 PID 保持，NRestarts=0 |
| Hermes polling | 2 条到 7890 的 ESTABLISHED 连接 |
| 恢复后错误 | ConnectError=0，409/鉴权错误=0 |
| Mihomo | healthy，RestartCount=0 |
| Quant 容器 | 9/9 healthy |
| Quant 前端 | HTTP 200 |
| Grafana | HTTP 200，database=ok |
| Prometheus | healthy/ready 200，契约内 target 1/1 up |
| Swing 成交额只读验证 | 腾讯源 21017.15 亿元，交易日 2026-08-28，fresh |

观察窗口超过 10 分钟，无新的 Hermes 连接错误。今天的 Swing 正式任务没有补跑；历史失败已保留在 journal，随后只清除了 systemd failed 标记。

## 过夜复核（2026-08-28 20:00–2026-08-29 10:35 CST）

- 匿名出口 watchdog 共执行 165 轮、660 个探针：
  - 158 轮四路全部通过；
  - 7 轮各仅一条腿超时；
  - 没有连续两轮失败。
- 单腿超时分布：
  - Telegram HTTP：3；
  - Telegram SOCKS：0；
  - gstatic HTTP：3；
  - gstatic SOCKS：1。
- 七次异常均在下一轮恢复。

Hermes 在窗口内发生 5 次瞬时网络事件，时间为 20:05、20:25、22:40、03:11、09:51。每次事件同时记录 heartbeat degraded 和 network error，因此不能按 `ConnectError` 文本行数误算成 10 次。五次均止于 `attempt 1/10`，`attempt 2–10=0`，服务 `NRestarts=0`。

Durable outbox 截至窗口结束为：

- `pending=0`
- `sending=0`
- `sent=5`
- `failed=6`

六条 failed 均为观察窗口前的历史记录；最后一条创建于 2026-08-28 15:40 CST，最后一次尝试为 15:50 CST。窗口内没有新增、重试或新增失败，历史消息继续禁止盲目重放。

## 防复发加固

- Compose 中 Mihomo 从 `latest` 固定到当前 digest。
- 镜像身份复核：
  - Compose 期望状态已固定到 `docker.1ms.run/metacubex/mihomo@sha256:e6acd921addecfd59a8e2d38203f88356d635b54de6c0673db0e015139989312`；
  - 运行容器的 `.Image`、本机镜像 `Id` 和 `RepoDigest` 均对应上述 digest；
  - `docker diff radar-mihomo` 为空，运行镜像层无漂移；
  - `.Config.Image` 仍显示创建容器时使用的 `docker.1ms.run/metacubex/mihomo:latest`。

`.Config.Image` 是创建时的引用元数据，不代表当前容器仍在运行未知 `latest` 字节。不要仅为改变该字段重建生产容器；下次受控重建后它会自然显示固定 digest。

- 新增匿名真实出口 watchdog：
  - HTTP/SOCKS × Telegram/gstatic，共四项；
  - 每五分钟执行；
  - `Persistent=false`，不会补跑；
  - 不读取 bot token、不发送消息、不切节点、不自动重启 Docker/Mihomo。
- 首次自动执行时间：2026-08-28 20:01:17 CST。
- 首次结果：四项全部通过，service `Result=success`。
- 代码提交：`9a1f62e fix: harden mihomo egress monitoring`。

## 后续事项

1. 2026-08-31 15:30 验收下一次 Swing EOD，确认不再出现 `macro_data_partial`。
2. `/home/ubuntu/mihomo/update.sh` 没有 timer、cron 或 systemd 调用，本次未使用；在完成同等级事务/回滚改造前，不应直接运行旧脚本。
3. 当前 watchdog 只写 journal/failed unit。若需要跨主机告警，应接入一个不依赖本机 Telegram 出口的备用通道。
4. 只有连续两轮 watchdog 失败、Hermes 出现 `attempt >= 2`，或 outbox 新增 failed 时才升级处理；孤立单腿超时只记录并观察下一轮。
