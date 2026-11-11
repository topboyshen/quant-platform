"""波段化趋势确认模块（收盘后批处理）。

把 Radar 从狙击个股的高频 daemon，降维成每日收盘后跑一次、鸟瞰大盘宽基/行业
ETF 的「趋势确认器」：

- ``store``         : SwingStore，持久化每日宏观情绪切片（sqlite）。
- ``macro_snapshot``: MacroSnapshotCollector，AkShare 优先采集成交额/两融增速/涨跌停比。
- ``trend_strategy``: TrendConfirmStrategy，日线/周线 MA + MACD 趋势信号。
- ``reporter``      : SwingReporter，过滤微观波动，仅在趋势确立/情绪极值时发一行 Fact。

入口脚本见仓库根目录 ``radar_swing_eod.py``。
"""

from radar.swing.store import MacroSlice, SwingStore
from radar.swing.trend_strategy import TrendConfirmStrategy, TrendSignal

__all__ = [
    "SwingStore",
    "MacroSlice",
    "TrendConfirmStrategy",
    "TrendSignal",
]
