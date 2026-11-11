# Grafana 部署指南

## 文件结构

```
deploy/grafana/
├── dashboards/
│   ├── trading-radar-latency.json      # 延迟与健康面板（8个可视化）
│   └── trading-radar-prometheus.json   # 基础监控面板
├── provisioning/
│   ├── dashboards/
│   │   └── dashboard.yml               # 自动加载配置
│   └── datasources/
│       └── prometheus.yml              # Prometheus 数据源
└── README.md                           # 本文件
```

## 部署方式

### 方式一：Docker Compose（推荐）

```bash
cd /home/ubuntu/trading_radar
docker-compose up -d grafana
```

访问 http://localhost:3000，默认账号 admin/admin。

### 方式二：裸机部署

1. 安装 Grafana：
```bash
# Ubuntu/Debian
sudo apt-get install -y apt-transport-https software-properties-common wget
wget -q -O /usr/share/keyrings/grafana.key https://apt.grafana.com/gpg.key
echo "deb [signed-by=/usr/share/keyrings/grafana.key] https://apt.grafana.com stable main" | sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt-get update
sudo apt-get install grafana

# 启动
sudo systemctl enable grafana-server
sudo systemctl start grafana-server
```

2. 复制面板文件：
```bash
sudo mkdir -p /var/lib/grafana/dashboards
sudo cp deploy/grafana/dashboards/*.json /var/lib/grafana/dashboards/
sudo cp -r deploy/grafana/provisioning /etc/grafana/
```

3. 重启 Grafana：
```bash
sudo systemctl restart grafana-server
```

### 方式三：Kubernetes

```bash
kubectl create configmap grafana-dashboards \
  --from-file=deploy/grafana/dashboards/ \
  -n monitoring

kubectl apply -f deploy/k8s/grafana-deployment.yaml
```

## 面板说明

### Trading Radar - Latency & Health

| 面板 | 类型 | 指标 | 告警阈值 |
|------|------|------|----------|
| TTT P95 | Timeseries | `histogram_quantile(0.95, ...)` | >50ms 红色 |
| TTT P99 | Timeseries | `histogram_quantile(0.99, ...)` | >80ms 红色 |
| TTT Heatmap | Heatmap | `..._bucket` | 直观分布 |
| EventBus 队列 | Timeseries | `radar_eventbus_queue_size` | >50 红色 |
| Redis Lag | Timeseries | `radar_eventbus_redis_lag_seconds` | >0.5s 红色 |
| HTTP 429 | Timeseries | `rate(..._429_total[5m])` | >1/s 红色 |
| 断路器熔断 | Timeseries | `rate(..._circuit_open_total[5m])` | >0.01/s 红色 |
| HTTP 5xx | Timeseries | `rate(..._5xx_total[5m])` | >1/s 红色 |

## 数据源配置

默认已配置 Prometheus 数据源指向 `http://localhost:9090`。

如需修改，编辑 `deploy/grafana/provisioning/datasources/prometheus.yml`。

## 告警规则（Prometheus Alertmanager）

```yaml
groups:
  - name: trading-radar
    rules:
      - alert: TickToTradeLatencyHigh
        expr: histogram_quantile(0.95, sum(rate(radar_tick_to_trade_latency_milliseconds_bucket[5m])) by (le)) > 50
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "Tick-to-Trade P95 延迟超过 50ms"
          
      - alert: EventBusQueueBacklog
        expr: radar_eventbus_queue_size > 50
        for: 30s
        labels:
          severity: critical
        annotations:
          summary: "EventBus 队列堆积超过 50"
          
      - alert: APIRateLimited
        expr: rate(radar_api_rate_limit_429_total[5m]) > 1
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "HTTP 429 限频频繁"
```
