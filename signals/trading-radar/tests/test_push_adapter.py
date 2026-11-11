"""
测试：事件驱动行情推送适配器 (PushAdapter + EventBus)

覆盖：
1. EventBus 注册/注销/发布
2. MarketDataEvent 构建与变化检测
3. PushAdapter 数据获取与事件推送
4. 自适应频率调整
5. 与 MonitorEngine 的集成（事件模式）
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

from radar.core.push_adapter import (
    EventBus,
    MarketDataEvent,
    PushAdapter,
    create_push_adapter,
)


@pytest.fixture
def mock_fetcher():
    """模拟返回固定数据的 fetcher"""
    fetcher = MagicMock()

    # 模拟 fetch_all 返回 DataFrame
    import pandas as pd
    df = pd.DataFrame({
        '代码': ['300059', '300033'],
        '名称': ['东方财富', '同花顺'],
        '最新价': [23.5, 245.0],
        '涨跌幅': [1.2, -0.5],
        '成交量': [100000, 50000],
        '今开': [23.0, 246.0],
        '最高': [24.0, 247.0],
        '最低': [22.5, 244.0],
        '昨收': [23.2, 246.2],
    })
    fetcher.fetch_all.return_value = df
    return fetcher


@pytest.fixture
def event_bus():
    return EventBus(logger=MagicMock())


class TestEventBus:
    """测试事件总线"""

    def test_register_and_emit(self, event_bus):
        """注册处理器后发布事件应被接收"""
        received = []

        def handler(event_type, data):
            received.append((event_type, data))

        event_bus.register("test_event", handler)
        event_bus.emit("test_event", {"msg": "hello"})

        assert len(received) == 1
        assert received[0] == ("test_event", {"msg": "hello"})

    def test_unregister(self, event_bus):
        """注销后不应再接收事件"""
        received = []

        def handler(event_type, data):
            received.append(data)

        event_bus.register("test", handler)
        event_bus.emit("test", 1)
        event_bus.unregister("test", handler)
        event_bus.emit("test", 2)

        assert len(received) == 1
        assert received[0] == 1

    def test_async_handler(self, event_bus):
        """异步处理器应在后台线程执行"""
        received = []
        thread_ids = []

        def handler(event_type, data):
            received.append(data)
            thread_ids.append(threading.current_thread().ident)

        main_thread = threading.current_thread().ident
        event_bus.register("test", handler, async_mode=True)
        event_bus.emit("test", 42)

        # 等待异步执行
        time.sleep(0.2)

        assert len(received) == 1
        assert received[0] == 42
        assert thread_ids[0] != main_thread  # 在后台线程执行

    def test_multiple_handlers(self, event_bus):
        """多个处理器应都被调用"""
        results = []

        def h1(event_type, data):
            results.append("h1")

        def h2(event_type, data):
            results.append("h2")

        event_bus.register("test", h1)
        event_bus.register("test", h2)
        event_bus.emit("test", None)

        assert sorted(results) == ["h1", "h2"]

    def test_handler_exception_not_propagate(self, event_bus):
        """处理器异常不应传播到发布者"""
        def bad_handler(event_type, data):
            raise ValueError("boom")

        event_bus.register("test", bad_handler)

        # 不应抛出异常
        event_bus.emit("test", None)

    def test_shutdown(self, event_bus):
        """shutdown 不应报错"""
        event_bus.shutdown()


class TestMarketDataEvent:
    """测试行情事件"""

    def test_build_event(self):
        """构建事件"""
        e = MarketDataEvent(
            code="300059", name="东方财富", price=23.5,
            volume=10000, change_pct=1.2,
            open=23.0, high=24.0, low=22.5, prev_close=23.2,
            timestamp=time.time(),
        )
        assert e.code == "300059"
        assert e.price == 23.5

    def test_significant_change_price(self):
        """价格变化超过阈值应视为显著变化"""
        e1 = MarketDataEvent("300059", "A", 100.0, 1000, 0, 99, 101, 98, 99, time.time())
        e2 = MarketDataEvent("300059", "A", 100.5, 1000, 0, 99, 101, 98, 99, time.time())

        # 价格变化 0.5%，阈值 0.1%，应视为变化
        assert e2.is_significant_change(e1, price_threshold=0.001)

    def test_no_significant_change(self):
        """价格变化很小不应视为显著变化"""
        e1 = MarketDataEvent("300059", "A", 100.0, 1000, 0, 99, 101, 98, 99, time.time())
        e2 = MarketDataEvent("300059", "A", 100.01, 1000, 0, 99, 101, 98, 99, time.time())

        # 价格变化 0.01%，阈值 0.1%，不应视为变化
        assert not e2.is_significant_change(e1, price_threshold=0.001)

    def test_first_data_is_significant(self):
        """首次数据（price=0）应视为变化"""
        e1 = MarketDataEvent("300059", "A", 0, 0, 0, 0, 0, 0, 0, time.time())
        e2 = MarketDataEvent("300059", "A", 100.0, 1000, 0, 99, 101, 98, 99, time.time())

        assert e2.is_significant_change(e1)

    def test_volume_change(self):
        """成交量变化超过 5% 应视为显著变化"""
        e1 = MarketDataEvent("300059", "A", 100.0, 1000, 0, 99, 101, 98, 99, time.time())
        e2 = MarketDataEvent("300059", "A", 100.0, 2000, 0, 99, 101, 98, 99, time.time())

        # 成交量变化 100%，超过 5% 阈值
        assert e2.is_significant_change(e1)


class TestPushAdapter:
    """测试推送适配器"""

    def test_fetch_and_emit(self, mock_fetcher, event_bus):
        """获取数据并推送事件"""
        received = []

        def handler(event_type, data):
            if isinstance(data, MarketDataEvent):
                received.append(data)

        event_bus.register("market_data", handler)

        adapter = PushAdapter(
            fetcher=mock_fetcher,
            event_bus=event_bus,
            logger=MagicMock(),
            target_codes={"300059", "300033"},
        )

        # 手动调用一次获取
        emitted, monitored = adapter._fetch_and_emit()

        assert emitted == 2  # 两只股票都有数据
        assert monitored == 2
        assert len(received) == 2
        assert received[0].code in {"300059", "300033"}

    def test_skip_unchanged(self, mock_fetcher, event_bus):
        """无变化的数据应跳过"""
        adapter = PushAdapter(
            fetcher=mock_fetcher,
            event_bus=event_bus,
            logger=MagicMock(),
            target_codes={"300059"},
        )

        # 第一次获取
        emitted1, _ = adapter._fetch_and_emit()
        assert emitted1 == 1

        # 第二次获取（数据相同）
        emitted2, _ = adapter._fetch_and_emit()
        assert emitted2 == 0  # 无变化，跳过

        assert adapter._skip_count > 0

    def test_set_targets(self, mock_fetcher, event_bus):
        """动态修改监控目标"""
        adapter = PushAdapter(
            fetcher=mock_fetcher,
            event_bus=event_bus,
            logger=MagicMock(),
            target_codes={"300059"},
        )

        emitted, monitored = adapter._fetch_and_emit()
        assert monitored == 1  # 只有 300059

        # 增加目标
        adapter.set_targets(["300059", "300033"])
        emitted, monitored = adapter._fetch_and_emit()
        assert monitored == 2  # 现在两只

    def test_last_events_pruned_when_targets_drop_silently(
        self, mock_fetcher, event_bus
    ):
        """诊断报告 5.1 修复：_fetch_and_emit 应主动 prune _last_events 中
        不在 target_codes 里的旧 key，避免 set_targets 永远不被调用时无界增长。"""
        adapter = PushAdapter(
            fetcher=mock_fetcher,
            event_bus=event_bus,
            logger=MagicMock(),
            target_codes={"300059", "300033"},
        )

        adapter._fetch_and_emit()
        assert set(adapter._last_events.keys()) == {"300059", "300033"}

        # 模拟 target_codes 在外部直接被改窄（绕过 set_targets，重现报告里的场景）
        adapter.target_codes = {"300059"}
        adapter._fetch_and_emit()

        assert "300033" not in adapter._last_events, (
            "drop 掉的 target 仍残留在 _last_events，会无界增长"
        )
        assert "300059" in adapter._last_events

    def test_adjust_interval_high_activity(self, mock_fetcher, event_bus):
        """高活跃度应缩短间隔"""
        adapter = PushAdapter(
            fetcher=mock_fetcher,
            event_bus=event_bus,
            logger=MagicMock(),
            target_codes={"300059", "300033"},
        )

        initial = adapter._current_interval

        # 模拟高活跃（100% 变化）
        adapter._adjust_interval(emitted=2, monitored=2)

        assert adapter._current_interval < initial  # 间隔缩短

    def test_adjust_interval_low_activity(self, mock_fetcher, event_bus):
        """低活跃度应延长间隔"""
        adapter = PushAdapter(
            fetcher=mock_fetcher,
            event_bus=event_bus,
            logger=MagicMock(),
            target_codes={"300059", "300033"},
        )

        initial = adapter._current_interval

        # 模拟低活跃（0% 变化）
        adapter._adjust_interval(emitted=0, monitored=2)

        assert adapter._current_interval > initial  # 间隔延长

    def test_stats(self, mock_fetcher, event_bus):
        """统计信息应正确（_fetch_and_emit 内部不增加 poll_count）"""
        adapter = PushAdapter(
            fetcher=mock_fetcher,
            event_bus=event_bus,
            logger=MagicMock(),
            target_codes={"300059"},
        )

        adapter._fetch_and_emit()
        # poll_count 在 _run_loop 中增加，手动调用不增加
        # 但 event_count 应增加
        stats = adapter.get_stats()
        assert stats["event_count"] == 1
        assert stats["target_count"] == 1

    def test_start_stop(self, mock_fetcher, event_bus):
        """启动和停止应正常工作"""
        adapter = PushAdapter(
            fetcher=mock_fetcher,
            event_bus=event_bus,
            logger=MagicMock(),
            target_codes={"300059"},
        )

        adapter.start()
        assert adapter._running is True
        assert adapter._thread is not None
        assert adapter._thread.is_alive()

        # 等待一轮轮询
        time.sleep(0.5)

        adapter.stop()
        assert adapter._running is False
        # 线程应已结束或即将结束
        time.sleep(0.3)


class TestCreatePushAdapter:
    """测试工厂函数"""

    def test_factory(self, mock_fetcher):
        """工厂函数应返回正确类型"""
        adapter, bus = create_push_adapter(
            fetcher=mock_fetcher,
            logger=MagicMock(),
            target_codes=["300059", "300033"],
        )

        assert isinstance(adapter, PushAdapter)
        assert isinstance(bus, EventBus)
        assert adapter.target_codes == {"300059", "300033"}


class TestIntegrationWithMonitorEngine:
    """测试与 MonitorEngine 的集成"""

    def test_enable_event_mode(self):
        """MonitorEngine 应能启用事件模式"""
        from radar.core.monitor_engine import MonitorConfig, MonitorEngine, TargetStock

        config = MonitorConfig(
            bot_token="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz1234",
            chat_id="test",
            cooldown_minutes=30,
            scan_interval=60,
            cache_file="test_cache.json",
            log_file="test.log",
            market_hours={
                "morning_start": "09:30",
                "morning_end": "11:30",
                "afternoon_start": "13:00",
                "afternoon_end": "15:00",
            },
        )
        logger = MagicMock()
        engine = MonitorEngine(config, logger)

        # 添加目标
        engine.add_target(TargetStock(
            code="300059", name="东方财富",
            trigger_price=23.7, direction="below"
        ))

        # 启用事件模式
        engine.enable_event_mode()

        assert engine._event_mode is True
        assert engine._push_adapter is not None
        assert engine._event_bus is not None

    def test_on_market_data(self):
        """_on_market_data 应正确处理行情事件"""
        from radar.core.monitor_engine import MonitorConfig, MonitorEngine, TargetStock

        config = MonitorConfig(
            bot_token="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz1234",
            chat_id="test",
            cooldown_minutes=30,
            scan_interval=60,
            cache_file="test_cache.json",
            log_file="test.log",
            market_hours={
                "morning_start": "09:30",
                "morning_end": "11:30",
                "afternoon_start": "13:00",
                "afternoon_end": "15:00",
            },
        )
        logger = MagicMock()
        engine = MonitorEngine(config, logger)

        engine.add_target(TargetStock(
            code="300059", name="东方财富",
            trigger_price=25.0, direction="below"  # 触发价 25，当前价 23.5 应触发
        ))

        event = MarketDataEvent(
            code="300059", name="东方财富", price=23.5,
            volume=10000, change_pct=-2.0,
            open=24.0, high=24.5, low=23.0, prev_close=24.0,
            timestamp=time.time(),
        )

        # 手动调用事件处理器
        engine._on_market_data("market_data", event)

        # 验证 portfolio 价格已更新（update_price 会创建条目，即使之前无持仓）
        # PortfolioTracker.update_price 会创建 {shares:0, cost:0, last_price:price} 条目
        assert "300059" in engine._portfolio.positions
        assert engine._portfolio.positions["300059"]["last_price"] == 23.5
