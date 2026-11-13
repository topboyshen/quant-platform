# M9 下线 Runbook（只停不删）

状态：**M9 工具与文档完成，未执行任何停止/删除**。执行下线需要在前置条件全部满足后单独授权。

## 1. 前置条件（缺一不可）

1. M3–M8 均已切换并通过验收；qp 承担全部在用职责。
2. 切换后观察 **≥2 周**，无回退旧链路的故障。
3. 用户对“停旧服务/容器”给出**单独授权**；**数据删除需再一次单独授权**。
4. `NO_BACKUP` 政策不变；qp 03:50 备份继续运行。

## 2. 只读盘点（任何阶段都可先跑）

```bash
sudo -u qp /opt/qp/current/venv/bin/qp decommission inventory \
  --out /var/lib/qp/decommission-$(date +%F).json
```

- 输出：legacy system/user timers、`radar-*` 容器、关键路径大小、`sha256`。
- 工具只调用 `systemctl list-timers`、`docker ps`、`du -sb`，**不执行 stop/disable/rm**。
- root 属主路径（`/opt/research-ledger` 等）若 du 失败，需以 root 重跑对应路径。

## 3. 停服顺序（只停不删）

```bash
# 3.1 早报链（user units）
systemctl --user disable --now \
  astock-cninfo-precollect.timer astock-xhs-source.timer nng8-research-digest.timer \
  research-chain-alert.timer research-chain-alert-post.timer

# 3.2 研究/数据/监控 timers（system units）
sudo systemctl disable --now \
  quant-research-daily-summary.timer research-digest.timer research-ledger-pipeline-health.timer \
  research-data-health.timer research-data-health-alert.timer research-index-close-cache.timer \
  quant-execution-digest.timer

# 3.3 其余 legacy timers（按盘点清单逐个）
sudo systemctl disable --now <unit> ...

# 3.4 容器：先依赖方，最后 QD 前端/PG（只 stop，不 rm）
sudo docker stop radar-integration-api radar-macro-risk
sudo docker stop radar-quantdinger-backend radar-quantdinger-frontend
sudo docker stop radar-quantdinger-postgres radar-quantdinger-redis
sudo docker stop radar-prometheus radar-alertmanager radar-grafana radar-node-exporter radar-runtime-observer
```

旧站点外壳与 QD 前端保留 **30 天只读回看**；根路径已由 qp 接管。

## 4. 只读归档

- 只归档配置与 unit 文件（不含业务数据）：

```bash
sudo tar czf /var/lib/qp/decommission-$(date +%F)-units.tgz \
  /etc/systemd/system/{trading-radar,astock,quant-*,research-*,signal-bus,qd-*,radar-*,nng8-*}* \
  /home/ubuntu/.config/systemd/user/{astock,research,signal-bus,nng8}* 2>/dev/null || true
sha256sum /var/lib/qp/decommission-$(date +%F)-units.tgz
```

- 数据只记录清单与 sha256（`qp decommission inventory`），**不迁移、不打包、不删除**。
- 文档：本 runbook + 盘点 JSON + 停服记录（时间、执行人、回执）。

## 5. 观察与回滚

- 停服后观察 2 周；期间 qp 告警与日报为唯一来源。
- 回滚：
  ```bash
  sudo systemctl enable --now <unit> ...
  systemctl --user enable --now <unit> ...
  sudo docker start radar-...   # 镜像与容器未删
  ```
- 回滚不改 qp 数据；旧系统数据全程未被修改。

## 6. 数据清理（不在 M9 自动执行）

删除旧库、qlib/parquet、证据、Hermes ledger、AStock work 等，需要**再一次单独授权**，
并逐项列出路径、大小、保留理由与回滚可能性。M9 不提供删除命令。

## 7. 安全

- 不删除业务数据、证据、日志；不 `docker rm`、不 `rm -rf`。
- 报告/日志不出现 token、chat_id、DSN 密码。
- 停服与 qp 发布解耦：同一时段只做一件事，并记录基线（git tag + unit 备份）。
