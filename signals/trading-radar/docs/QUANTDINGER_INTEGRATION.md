# Trading Radar + QuantDinger 统一部署

## 目标与边界

本仓库以单仓库方式管理两套互补能力：

- **Trading Radar**：行情、Swing EOD 决策、风险过滤、审计、告警与只读集成接口。
- **QuantDinger**：策略源码、指标研究、回测、模拟盘和交易基础设施。
- **Integration API**：仅在 Docker 内部网络向 QuantDinger 提供 Radar 市场上下文。

两套数据面保持隔离：Radar 使用自己的 SQLite/Redis，QuantDinger 使用独立 PostgreSQL/Redis。集成不共享数据库，也不允许 QuantDinger 直接写入 Radar 状态。

## 单仓库布局

QuantDinger 源码位于仓库内：

```text
/home/ubuntu/trading-radar/QuantDinger
```

`docker-compose.quantdinger.yml` 默认使用 `./QuantDinger`。迁移期间若仍保留旧的相邻仓库，可在 `.env` 中临时设置：

```dotenv
QUANTDINGER_ROOT=../QuantDinger
```

完成密钥与配置迁移后删除该覆盖项，即回到单仓库默认路径。

## Compose 组成

生产 `.env` 的 `COMPOSE_FILE` 应包含：

```dotenv
COMPOSE_FILE=docker-compose.yml:docker-compose.integration.override.yml:docker-compose.quantdinger.yml:docker-compose.runtime-cleanup.yml
```

对应职责：

1. `docker-compose.yml`：Radar 基础服务。
2. `docker-compose.integration.override.yml`：只读 Integration API。
3. `docker-compose.quantdinger.yml`：QuantDinger PostgreSQL、Redis、后端与前端。
4. `docker-compose.runtime-cleanup.yml`：运行时清理和兼容覆盖。

## 密钥与配置

真实配置不得提交。单仓库默认路径为：

```text
/home/ubuntu/trading-radar/QuantDinger/config/backend.env
/home/ubuntu/trading-radar/secrets/radar_integration_token
```

要求：

- `backend.env` 和服务令牌权限设为 `600`。
- `QuantDinger/config/`、根 `.env` 与 `secrets/` 已被 Git 忽略。
- 不要把密钥写入 Compose、Dockerfile、日志、Issue 或 PR。
- 曾进入 Git 历史的密钥必须轮换；删除历史字符串不是轮换的替代方案。

## 访问边界

- 统一页面：经服务器现有 TLS/Nginx 入口访问。
- QuantDinger UI：默认 `127.0.0.1:8888`。
- QuantDinger API：默认 `127.0.0.1:5000`。
- PostgreSQL、QuantDinger Redis、Radar Redis 与 Integration API：不发布公网端口。

需要本地隧道时：

```bash
ssh -L 8888:127.0.0.1:8888 Quant-Server
```

然后打开 `http://127.0.0.1:8888`。不要把 5000、8888 或数据库端口直接开放到公网。

## 日常操作

```bash
cd /home/ubuntu/trading-radar
docker compose config --quiet
docker compose up -d
docker compose ps
python scripts/smoke_unified.py
```

QuantDinger 容器：

- `radar-quantdinger-postgres`
- `radar-quantdinger-redis`
- `radar-quantdinger-backend`
- `radar-quantdinger-frontend`

不要执行 `docker compose down -v`；`-v` 会删除 PostgreSQL 与 Redis 命名卷。

## 安全默认值

- `AGENT_LIVE_TRADING_ENABLED=false`
- `ENABLE_REGISTRATION=false`
- `ALLOW_LOCAL_DESKTOP_BROKERS=false`
- `DISABLE_RESTORE_RUNNING_STRATEGIES=true`
- PostgreSQL 与 QuantDinger Redis 不发布宿主机端口。
- Radar 服务令牌通过 Docker secret 挂载。
- 内部主机列入 `NO_PROXY`，避免服务间请求错误经过外部代理。

任何实盘开关、券商连接或权限范围变更都必须单独评审，并重新运行统一冒烟测试。

## 备份与恢复

每日 PostgreSQL 逻辑备份：

```bash
/home/ubuntu/trading-radar/scripts/backup_quantdinger_postgres.sh
```

默认备份目录：

```text
/home/ubuntu/backups/quantdinger-postgres
```

备份文件权限为 `600`，并生成 SHA-256 校验文件。恢复前先校验：

```bash
sha256sum -c /home/ubuntu/backups/quantdinger-postgres/<backup>.sql.gz.sha256
gzip -t /home/ubuntu/backups/quantdinger-postgres/<backup>.sql.gz
```

恢复必须在维护窗口进行：停止 QuantDinger 写入端，导入新建的空数据库，依次恢复 PostgreSQL、Redis、后端和前端，最后执行 `scripts/smoke_unified.py`。不要覆盖仍有活动连接的生产数据库。

## 回滚

只停止 QuantDinger，不影响 Radar：

```bash
cd /home/ubuntu/trading-radar
docker compose stop quantdinger-frontend quantdinger-backend quantdinger-redis quantdinger-postgres
```

配置回滚时保留 QuantDinger 命名卷。部署前配置备份位于
`/home/ubuntu/trading-radar-deploy-backups/unified-merge-pre-20260729-1`。
确认 Radar 核心容器全部健康后再结束维护。

## 上游升级

仓库内 `QuantDinger/` 是现网验证过的固定定制版，不自动追随上游。升级时必须独立完成：

1. 数据库迁移与回滚审查。
2. 认证、策略、回测和 OpenAPI 回归。
3. Docker 镜像构建和 Compose 校验。
4. 维护窗口内的灰度切换与回滚演练。

上游来源和许可证见根目录 [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)。
