"""EventModeMixin 单元测试：next_open 计算 + battlefield 节流。

无需构造完整 MonitorEngine —— 用最小 stub 继承 mixin 直接测纯逻辑。
"""

from __future__ import annotations

import datetime
import logging
import time
from unittest.mock import MagicMock

import pytz

from radar.core.event_mode_mixin import EventModeMixin


class _StubTimeChecker:
    """复刻 MarketTimeChecker 中 _calculate_next_open_time 用到的接口。"""

    def __init__(self) -> None:
        self.beijing_tz = pytz.timezone("Asia/Shanghai")
        self.morning_start = datetime.time(9, 30)
        self.afternoon_start = datetime.time(13, 0)


class _StubEngine(EventModeMixin):
    """最小宿主类：仅暴露 EventModeMixin 需要的属性。"""

    def __init__(self) -> None:
        self.logger = logging.getLogger("event_mode_unit_test")
        self.logger.addHandler(logging.NullHandler())
        self.time_checker = _StubTimeChecker()
        self.targets: list = []
        self._portfolio = MagicMock()
        self._alert_batcher = MagicMock()
        self._battlefield = MagicMock()
        self._flush_alert_batch = MagicMock()
        self.process_target = MagicMock(return_value=[])  # type: ignore[assignment]


def _bj(year: int, month: int, day: int, hour: int, minute: int) -> datetime.datetime:
    """构造一个北京时区的 aware datetime（_calculate_next_open_time 的输入约定）。"""
    return pytz.timezone("Asia/Shanghai").localize(
        datetime.datetime(year, month, day, hour, minute)
    )


# ---------- next_open 计算 ----------

class TestCalculateNextOpenTime:
    """覆盖工作日开盘前/午休/收盘后 + 周末，重点防回归"周五收盘后误返今天 09:30"。"""

    def setup_method(self) -> None:
        self.eng = _StubEngine()

    def test_weekday_before_morning(self) -> None:
        # 周一 08:00 -> 今天 09:30
        now = _bj(2026, 5, 18, 8, 0)  # 周一
        nxt = self.eng._calculate_next_open_time(now)
        assert nxt == _bj(2026, 5, 18, 9, 30)

    def test_weekday_midday_break(self) -> None:
        # 周二 11:45（午休）-> 今天 13:00
        now = _bj(2026, 5, 19, 11, 45)
        nxt = self.eng._calculate_next_open_time(now)
        assert nxt == _bj(2026, 5, 19, 13, 0)

    def test_weekday_after_close_goes_to_next_day(self) -> None:
        # 周三 15:30（收盘后）-> 周四 09:30
        now = _bj(2026, 5, 20, 15, 30)
        nxt = self.eng._calculate_next_open_time(now)
        assert nxt == _bj(2026, 5, 21, 9, 30)

    def test_friday_after_close_skips_weekend(self) -> None:
        """回归测试：旧实现会返回今天 09:30（过去时间），导致主循环
        wait_seconds < 0 立即 break 进入 tick loop。"""
        now = _bj(2026, 5, 22, 15, 30)  # 周五收盘后
        nxt = self.eng._calculate_next_open_time(now)
        assert nxt == _bj(2026, 5, 25, 9, 30)  # 周一
        assert nxt > now, "next_open 不能早于当前时间"

    def test_friday_before_open_still_today(self) -> None:
        # 周五 08:00（盘前）-> 今天 09:30，不要错跳到下周
        now = _bj(2026, 5, 22, 8, 0)
        nxt = self.eng._calculate_next_open_time(now)
        assert nxt == _bj(2026, 5, 22, 9, 30)

    def test_saturday_goes_to_monday(self) -> None:
        now = _bj(2026, 5, 23, 10, 0)  # 周六
        nxt = self.eng._calculate_next_open_time(now)
        assert nxt == _bj(2026, 5, 25, 9, 30)

    def test_sunday_goes_to_monday(self) -> None:
        now = _bj(2026, 5, 24, 23, 59)  # 周日深夜
        nxt = self.eng._calculate_next_open_time(now)
        assert nxt == _bj(2026, 5, 25, 9, 30)


# ---------- battlefield 写节流 ----------

def _make_event(code: str = "000001") -> object:
    """构造 MarketDataEvent，避免 import 时强依赖（push_adapter 已被
    其他测试覆盖；这里只需 attribute access）。"""
    from radar.core.push_adapter import MarketDataEvent

    return MarketDataEvent(
        code=code,
        name="测试",
        price=10.0,
        volume=100,
        open=10.0,
        high=10.0,
        low=10.0,
        prev_close=10.0,
        change_pct=0.0,
        timestamp=time.time(),
    )


def test_battlefield_save_throttled_under_high_frequency_ticks() -> None:
    """5 个间隔 < 2s 的 tick 只应触发 1 次 save_all（节流生效）。"""
    eng = _StubEngine()
    eng.targets = [MagicMock(code="000001", name="测试")]

    event = _make_event()
    for _ in range(5):
        eng._on_market_data("market_data", event)
        time.sleep(0.05)

    assert eng._battlefield.save_all.call_count == 1, (
        f"高频 tick 下 save_all 被调用 {eng._battlefield.save_all.call_count} 次，"
        "节流失效将引发 SQLite 写风暴"
    )


def test_battlefield_save_recovers_after_interval() -> None:
    """间隔超过 BATTLEFIELD_EVENT_SAVE_MIN_INTERVAL_SEC 后应再次触发 save。"""
    eng = _StubEngine()
    eng.targets = [MagicMock(code="000001", name="测试")]
    # 缩短间隔，避免单测 sleep 过久
    eng.BATTLEFIELD_EVENT_SAVE_MIN_INTERVAL_SEC = 0.1

    event = _make_event()
    eng._on_market_data("market_data", event)
    assert eng._battlefield.save_all.call_count == 1

    eng._on_market_data("market_data", event)  # 间隔 < 0.1s
    assert eng._battlefield.save_all.call_count == 1, "节流应阻止第二次写"

    time.sleep(0.15)
    eng._on_market_data("market_data", event)
    assert eng._battlefield.save_all.call_count == 2, "间隔后应恢复写盘"


def test_battlefield_save_ignores_unknown_target() -> None:
    """未注册的 code 不应触发任何 save。"""
    eng = _StubEngine()
    eng.targets = [MagicMock(code="000001", name="测试")]

    event = _make_event(code="999999")
    eng._on_market_data("market_data", event)
    assert eng._battlefield.save_all.call_count == 0
    eng.process_target.assert_not_called()
