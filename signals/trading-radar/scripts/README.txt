目录与入口说明（运行时 vs 训练/回测）
=====================================

运行时（生产监控，轻依赖）
  - 主入口: radar_monitor_v3.py
  - 核心包: radar/（MonitorEngine、行情、告警、schema 等）

训练 / 全功能 / 回测（重依赖 torch、sklearn 等）
  - 根目录历史入口仍保留，便于旧脚本与相对导入：
    radar_pro.py      — 全功能监控/训练编排
    backtest_pro.py   — 回测引擎（run_backtest_comparison.py、run_benchmark.py 等引用）
    rl_agent.py       — RL 训练与推理封装（多份 train_*.py、radar_pro 引用）
  - 工具脚本: tools/

说明: 将上述大文件整体搬迁到子包会破坏大量 `from rl_agent import ...` 路径。
若需统一目录，可再设 legacy/ 并逐步改为显式包导入。
