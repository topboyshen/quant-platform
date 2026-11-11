# Docker Compose 生产迁移 Runbook

目标：把 Trading Radar 从当前宿主机进程 / systemd 运行态，安全切换到根目录 `docker-compose.yml` 的微服务矩阵。

本文只描述生产迁移步骤。不要在交易时段或无人值守时执行。

## 0. 关键原则

1. 不直接 `kill -9`。
   - Monitor / Dashboard 可能正在写 SQLite、日志或缓存。
   - Redis 可能正在 AOF 持久化。
   - 只允许优雅停止：`systemctl stop`、`docker compose down`、或 `SIGTERM` 后等待。
2. 新架构起不来，不在生产环境现场修 Bug。
   - 立即执行一分钟回滚，恢复旧服务。
   - 回滚成功后再离线排查 compose / 镜像 / 环境变量问题。
3. Docker 权限按组授权，不长期依赖 `sudo docker ...`。

## 1. Docker Daemon 权限规范

把当前 SSH 用户加入 `docker` 组：

```bash
sudo usermod -aG docker "$USER"
```

然后退出当前 SSH 会话，重新登录。

验证：

```bash
id -nG | tr ' ' '\n' | grep '^docker$'
docker ps
```

如果 `docker ps` 仍提示 socket permission denied，说明当前 shell 还没加载新 group，需要重新 SSH，而不是继续加 sudo 硬跑。

## 2. 迁移前检查

```bash
cd /home/ubuntu/trading-radar
scripts/compose_migration_preflight.sh
```

检查项包括：

- 当前用户是否属于 `docker` 组。
- Docker daemon 是否可访问。
- `docker compose config` 是否通过。
- Mihomo 配置文件是否存在（默认 `/home/ubuntu/mihomo/config.yaml`，可用 `MIHOMO_CONFIG` 覆盖）。
- 目标端口是否已被旧服务占用。
- `data/`、`logs/`、`models/`、`rl_models/` 是否存在。

端口被旧服务占用是正常现象，说明切换前必须先优雅停旧服务。

## 3. 优雅停止旧服务

优先 systemd：

```bash
sudo systemctl stop stock-radar.service || true
sudo systemctl stop radar-dashboard.service || true
sudo systemctl stop radar-ml-serve.service || true
```

如果不是 systemd，而是手工 Python 进程，使用 `TERM`，不要用 `kill -9`：

```bash
pkill -TERM -f '/home/ubuntu/trading-radar/radar_monitor_v3.py' || true
pkill -TERM -f '/home/ubuntu/trading-radar/dashboard/app.py' || true
pkill -TERM -f '/home/ubuntu/trading-radar/serve_ml.py' || true
sleep 5
```

确认端口释放：

```bash
ss -ltnp '( sport = :7890 or sport = :7891 or sport = :9092 or sport = :8080 or sport = :8081 or sport = :8090 or sport = :8091 )'
```

## 4. 启动 Compose 微服务矩阵

```bash
cd /home/ubuntu/trading-radar
# 默认读取 /home/ubuntu/mihomo/config.yaml；如需覆盖：
# export MIHOMO_CONFIG=/path/to/config.yaml
# export MIHOMO_HTTP_PORT=7890 MIHOMO_SOCKS_PORT=7891 MIHOMO_CONTROLLER_PORT=9092
docker compose up -d --build
```

验证：

```bash
docker compose ps
docker compose exec -T mihomo /mihomo -t -d /root/.config/mihomo
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8081/api/status
curl -fsS http://127.0.0.1:8090/health
curl -fsS http://127.0.0.1:9091/-/ready
curl -fsS http://127.0.0.1:3000/api/health
```

## 5. 一分钟回滚预案

如果 compose 启动后出现以下情况，立即回滚：

- `monitor` 容器反复退出。
- `dashboard` 无法访问。
- `/health` 连续失败。
- Telegram 告警异常刷屏。
- 行情扫描停止超过预期。

执行：

```bash
cd /home/ubuntu/trading-radar
scripts/compose_rollback.sh
```

回滚脚本会：

1. `docker compose down --remove-orphans` 停掉新架构。
2. 优先通过 systemd 拉起旧服务。
3. 如果当前环境没有 systemd，则回退到 `nohup venv/bin/python ...` 的旧进程方式。
4. 验证旧端点：monitor / dashboard / ml-serve。

## 6. 回滚后的处理原则

回滚成功后不要继续在生产环境热修。

保留日志：

```bash
docker compose logs --no-color > logs/compose-failed-$(date +%Y%m%d-%H%M%S).log 2>&1 || true
```

然后离线排查：

```bash
docker compose config
docker compose build --no-cache monitor ml-serve dashboard
```

## 7. 资产保护边界

这些目录是生产资产，不进镜像，只通过 volume 挂载：

```text
data/
logs/
models/
rl_models/
```

不要删除：

```text
*.db
*.db-wal
*.db-shm
models/*.pkl
models/manifest.json
```
