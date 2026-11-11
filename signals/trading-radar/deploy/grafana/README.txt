Grafana 面板（Trading Radar）
============================

仪表盘 JSON
-----------
- dashboards/trading-radar-prometheus.json （uid: trading-radar-prom）— 扫描 / PushAdapter / ML Serve QPS
- dashboards/trading-radar-latency.json — 延迟专项
- dashboards/trading-radar-battlefield.json （uid: trading-radar-battlefield）— 战局统筹
- dashboards/trading-radar-ml-advisor.json （uid: trading-radar-ml-advisor）— ML-Serve 参谋层
- dashboards/trading-radar-three-realms.json （uid: trading-radar-three-realms）— 三层视界 · L0 上帝视角
- dashboards/trading-radar-l1-warzone.json （uid: trading-radar-l1-warzone）— L1 主战区 · Alpha 曲线 vs 沪深300、策略仓位 Bar gauge
- dashboards/trading-radar-l2-engine-room.json （uid: trading-radar-l2-engine-room）— L2 引擎室 · TTT 热力图 / 尾延迟 + ML-Serve 特征缓存 Gauge

导入步骤
--------
1. Grafana → Dashboards → Import → Upload JSON file，选择上述文件。
2. 数据源 UID 已固定为 **prometheus**（与 `deploy/grafana/provisioning/datasources/datasource.yml` 一致）；手工导入时可在面板里改选你的 Prometheus。
3. Save。

Docker Compose
--------------
`docker compose -f deploy/docker-compose.yml up -d` 会挂载本目录下的 `dashboards/`，并自动 provision 数据源与仪表盘。

指标来源
--------
- radar_monitor 健康端口 `/metrics`（见 radar/ops/prometheus-scrape-example.yml）。
- 需安装 **prometheus-client** 后才有 `radar_pushadapter_*` 与事件模式相关曲线。
- `radar_ml_serve_*` 来自独立推理服务 `radar/ml_serve` 的 `/metrics`，需在 Prometheus 中 **增加单独 scrape job** 指向 ML 服务端口（默认 8090）。
- **L0 红绿灯**（`radar_l0_dependency_up`）：默认无系列时可由环境变量注入：`RADAR_L0_QMT_ADAPTER_UP`、`RADAR_L0_DATABASE_UP`（或 `RADAR_L0_AWS_DATABASE_UP`），或对适配器调用 `observe_l0_dependency_up("qmt", True)`。

无数据排查
----------
- 查询 `radar_pushadapter_*` 无数据：当前未开事件模式 PushAdapter，或未装 prometheus-client — 属正常。
- `time() - radar_last_scan_timestamp` 过大：监控未成功扫描或 HealthMonitor 未 record_scan。
