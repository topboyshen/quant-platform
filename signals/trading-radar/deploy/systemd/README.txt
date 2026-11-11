systemd 单元（监控与 ML 拆分）
==============================

文件
----
- stock-radar.service   仅 radar_monitor_v3.py（行情扫描、告警、/metrics）
- radar-ml-serve.service  serve_ml.py（POST /v1/predict、热重载、可选 Redis 缓存）

安装示例
--------
  sudo cp deploy/systemd/*.service /etc/systemd/system/
  sudo systemctl daemon-reload

监控依赖独立推理时，在 stock-radar 环境中设置：
  RADAR_ML_SERVE_URL=http://127.0.0.1:8090
（见 .env 或 Environment= 行）

建议先启 ml-serve 再启 monitor：
  sudo systemctl enable --now radar-ml-serve
  sudo systemctl enable --now stock-radar

ML 缓存（可选）写在 radar-ml-serve 的 Environment 或 .env：
  ML_SERVE_CACHE_BACKEND=redis
  ML_SERVE_REDIS_URL=redis://127.0.0.1:6379/0
