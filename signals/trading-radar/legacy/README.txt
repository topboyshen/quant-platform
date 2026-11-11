legacy/ — 已停用入口与历史包装层
================================================================================

⚠ 这里的 *.py 不再是生产入口。生产监控仅运行 radar_monitor_v3.py
  （位于仓库根目录），由 systemd 单元 stock-radar.service 拉起。

文件清单
--------------------------------------------------------------------------------
radar_monitor.py       V1 主入口（已废弃，2026-05 归档）
                       - 直接驱动 radar/core/monitor_engine.py 的 MonitorEngine
                       - 不含告警去重 / 自适应扫描 / 数据源管理器
                       - 不再被任何代码或服务引用，仅作历史参考

radar_monitor_v2.py    V2 主入口（已废弃，2026-05 归档）
                       - 引入 ConcurrentMarketDataFetcher V2 + HealthServerMixin
                       - 已被 V3 重写覆盖（V3 用 DataSourceManager + AsyncStrategyEngine
                         + AlertManager + ScanScheduler）

为何 radar/core/monitor_engine.py、radar/data/market_data.py
等"看似 V1"的模块还在 radar/ 下而不是 legacy/？
--------------------------------------------------------------------------------
这些模块的类（MonitorEngine / MarketDataFetcher / AlertCooldownManager 等）
仍被以下 active 路径作为 **公共 API** 引用：

  - tests/test_*.py（8+ 测试文件，依赖 MonitorEngine / MonitorConfig / TargetStock）
  - train_*.py、retrain_base_models.py、run_benchmark.py（训练/回测 pipeline）
  - radar/ml/training_compat.py、radar_trainer.py、radar_pro/* 也会拉这些类
  - radar/core/battlefield_state.py 用 MonitorEngine 做 typing 注解

把它们物理移动会拖出 ~30 个 import 错误，不属于"半天瘦身"工作量。
该债务记录在 trading_radar_diagnosis_report.md，需配合下一步重构集中清理。

如何启动 V3
--------------------------------------------------------------------------------
  # systemd（推荐）
  sudo systemctl start stock-radar.service        # alias: radar-monitor.service

  # 手动（开发/调试）
  python radar_monitor_v3.py --config config.json

历史保留脚本（与本目录无关，但同样不应再用作生产入口）
--------------------------------------------------------------------------------
  radar.py              经典 RadarEngine 包装器（已发出 deprecation warning）
  radar_trainer.py      独立训练 Job
  monitor.py            小时级 watchdog（独立功能，仍可用作系统巡检）
