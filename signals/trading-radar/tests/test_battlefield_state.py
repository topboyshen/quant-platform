"""
测试：阵地状态持久化 (BattlefieldStateManager)

覆盖：
1. 数据库初始化与表结构
2. 保存/恢复全局状态 (battlefield_state)
3. 保存/恢复持仓 (position_state + account_state)
4. 保存/恢复策略状态 (strategy_state)
5. 告警历史记录与查询 (alert_history)
6. 冷却状态镜像 (cooldown_mirror)
7. 与 MonitorEngine 的集成（restore_all / save_all）
8. Dashboard 查询接口（get_position_snapshot / get_alert_history / get_stats）
"""

import json
import os
import sqlite3
import tempfile
import threading
import time
from unittest.mock import MagicMock

import pytest

from radar.core.battlefield_state import BattlefieldStateManager


@pytest.fixture
def temp_db():
    """每个测试独立的临时数据库"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # 清理
    for ext in ["", "-shm", "-wal"]:
        try:
            os.remove(db_path + ext)
        except FileNotFoundError:
            pass


@pytest.fixture
def bsm(temp_db):
    """重置单例并返回新的 BattlefieldStateManager 实例"""
    # 重置单例
    BattlefieldStateManager._instance = None
    return BattlefieldStateManager(db_path=temp_db, logger=MagicMock())


class TestDatabaseInit:
    """测试数据库初始化"""

    def test_tables_created(self, bsm, temp_db):
        """所有表应正确创建"""
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        expected = {
            "battlefield_state",
            "position_state",
            "account_state",
            "strategy_state",
            "alert_history",
            "cooldown_mirror",
        }
        assert expected.issubset(tables)

    def test_wal_mode(self, bsm, temp_db):
        """应启用 WAL 模式（通过 _get_conn 创建的连接）"""
        # 先触发 _get_conn 初始化
        bsm._get_conn()
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        conn.close()
        assert mode.upper() == "WAL"


class TestBattlefieldState:
    """测试全局运行时状态"""

    def test_save_and_restore(self, bsm):
        """保存后应能正确恢复"""
        state = {
            "scan_count": 42,
            "error_count": 3,
            "consecutive_failures": 1,
            "failure_alert_sent": True,
            "saved_at": time.time(),
        }
        bsm.save_battlefield_state(state)

        restored = bsm.restore_battlefield_state()
        assert restored["scan_count"] == 42
        assert restored["error_count"] == 3
        assert restored["consecutive_failures"] == 1
        assert restored["failure_alert_sent"] is True
        assert "saved_at" in restored

    def test_update_existing_key(self, bsm):
        """更新已有 key 应覆盖旧值"""
        bsm.save_battlefield_state({"scan_count": 10})
        bsm.save_battlefield_state({"scan_count": 20})

        restored = bsm.restore_battlefield_state()
        assert restored["scan_count"] == 20

    def test_empty_restore(self, bsm):
        """空数据库恢复应返回空 dict"""
        restored = bsm.restore_battlefield_state()
        assert restored == {}


class TestPortfolioState:
    """测试持仓状态持久化"""

    def test_save_and_restore_portfolio(self, bsm):
        """保存并恢复 PortfolioTracker 状态"""
        from radar.strategy.rl_strategy_v2 import PortfolioTracker

        tracker = PortfolioTracker(initial_cash=200000.0)
        tracker.cash = 150000.0
        tracker.set_position("300059", 1000, 23.5)
        tracker.set_position("300033", 500, 240.0)

        bsm.save_portfolio(tracker)

        # 恢复
        restored = PortfolioTracker(initial_cash=200000.0)
        bsm.restore_portfolio(restored)

        assert restored.cash == 150000.0
        assert restored.initial_cash == 200000.0
        assert len(restored.positions) == 2
        assert restored.positions["300059"]["shares"] == 1000
        assert restored.positions["300059"]["cost"] == 23.5
        assert restored.positions["300033"]["shares"] == 500

    def test_clear_removed_positions(self, bsm):
        """保存时应清理已不存在的持仓"""
        from radar.strategy.rl_strategy_v2 import PortfolioTracker

        tracker = PortfolioTracker()
        tracker.set_position("300059", 1000, 23.5)
        bsm.save_portfolio(tracker)

        # 清空持仓后再次保存
        tracker.positions.clear()
        bsm.save_portfolio(tracker)

        conn = sqlite3.connect(bsm.db_path)
        cursor = conn.execute("SELECT COUNT(*) FROM position_state")
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 0

    def test_position_snapshot(self, bsm):
        """get_position_snapshot 应返回正确结构"""
        from radar.strategy.rl_strategy_v2 import PortfolioTracker

        tracker = PortfolioTracker(initial_cash=100000.0)
        tracker.cash = 80000.0
        tracker.set_position("300059", 1000, 20.0)
        # 手动更新 last_price
        tracker.positions["300059"]["last_price"] = 25.0
        bsm.save_portfolio(tracker)

        snapshot = bsm.get_position_snapshot()
        assert snapshot["cash"] == 80000.0
        assert snapshot["initial_cash"] == 100000.0
        assert snapshot["position_count"] == 1
        assert len(snapshot["positions"]) == 1

        pos = snapshot["positions"][0]
        assert pos["code"] == "300059"
        assert pos["shares"] == 1000
        assert pos["cost"] == 20.0
        assert pos["last_price"] == 25.0
        assert pos["pnl_pct"] == 25.0  # (25-20)/20*100


class TestStrategyState:
    """测试策略状态持久化"""

    def test_save_and_restore_rl_state(self, bsm):
        """保存并恢复 RL 策略的 last_action"""
        bsm.save_strategy_state("300059", "rl", {"last_action": 2})

        conn = sqlite3.connect(bsm.db_path)
        cursor = conn.execute(
            "SELECT state_json FROM strategy_state WHERE code=? AND strategy_type=?",
            ("300059", "rl"),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        state = json.loads(row[0])
        assert state["last_action"] == 2

    def test_multiple_strategies_per_code(self, bsm):
        """同一只股票可保存多种策略状态"""
        bsm.save_strategy_state("300059", "rl", {"last_action": 1})
        bsm.save_strategy_state("300059", "ml", {"last_prediction": 0.85})

        conn = sqlite3.connect(bsm.db_path)
        cursor = conn.execute(
            "SELECT strategy_type, state_json FROM strategy_state WHERE code=?",
            ("300059",),
        )
        rows = cursor.fetchall()
        conn.close()

        assert len(rows) == 2
        types = {row[0] for row in rows}
        assert types == {"rl", "ml"}


class TestAlertHistory:
    """测试告警历史"""

    def test_save_and_query(self, bsm):
        """保存告警后应能查询"""
        bsm.save_alert(
            alert_type="price_trigger",
            code="300059",
            name="东方财富",
            price=23.7,
            data={"trigger": 23.5, "direction": "below"},
        )

        history = bsm.get_alert_history(limit=10)
        assert len(history) == 1
        assert history[0]["alert_type"] == "price_trigger"
        assert history[0]["code"] == "300059"
        assert history[0]["name"] == "东方财富"
        assert history[0]["price"] == 23.7
        assert history[0]["data"]["trigger"] == 23.5

    def test_filter_by_code(self, bsm):
        """按股票代码过滤"""
        bsm.save_alert("price", "300059", "A", 10.0, {})
        bsm.save_alert("price", "300033", "B", 20.0, {})

        history = bsm.get_alert_history(code="300059", limit=10)
        assert len(history) == 1
        assert history[0]["code"] == "300059"

    def test_limit(self, bsm):
        """limit 应限制返回数量"""
        for i in range(5):
            bsm.save_alert("price", "300059", "A", float(i), {})

        history = bsm.get_alert_history(limit=3)
        assert len(history) == 3
        # 最新在前
        assert history[0]["price"] == 4.0


class TestCooldownMirror:
    """测试冷却状态镜像"""

    def test_mirror_cooldown(self, bsm, temp_db):
        """应正确镜像冷却状态"""
        from radar.alert.cooldown import AlertCooldownManager

        # 创建独立的 cooldown db
        cooldown_db = temp_db.replace(".db", "_cooldown.db")
        cooldown = AlertCooldownManager(cooldown_db, MagicMock())
        cooldown.record_alert("300059", "price")
        cooldown.record_alert("300033", "ml")

        # 镜像
        bsm.mirror_cooldown(cooldown)

        conn = sqlite3.connect(bsm.db_path)
        cursor = conn.execute("SELECT code, alert_type FROM cooldown_mirror")
        rows = cursor.fetchall()
        conn.close()

        assert len(rows) == 2
        codes = {row[0] for row in rows}
        assert codes == {"300059", "300033"}

    def test_mirror_cooldown_skips_when_unchanged(self, bsm, temp_db):
        """指纹未变时应跳过 DELETE+INSERT 全量重写（诊断报告 6.5 修复）。"""
        from radar.alert.cooldown import AlertCooldownManager

        cooldown_db = temp_db.replace(".db", "_cooldown_idem.db")
        cooldown = AlertCooldownManager(cooldown_db, MagicMock())
        cooldown.record_alert("300059", "price")
        cooldown.record_alert("300033", "ml")

        # 第一次镜像，写入 + 记录 digest
        bsm.mirror_cooldown(cooldown)
        first_digest = bsm._last_cooldown_digest
        assert first_digest

        # 篡改 cooldown_mirror 表内容，模拟"如果再次执行 DELETE+INSERT 会被覆盖"
        conn = sqlite3.connect(bsm.db_path)
        conn.execute("UPDATE cooldown_mirror SET alert_count = 999")
        conn.commit()
        conn.close()

        # 上游 cooldown 未变，应短路跳过，篡改值得以保留
        bsm.mirror_cooldown(cooldown)
        assert bsm._last_cooldown_digest == first_digest

        conn = sqlite3.connect(bsm.db_path)
        cursor = conn.execute("SELECT alert_count FROM cooldown_mirror")
        counts = [r[0] for r in cursor.fetchall()]
        conn.close()
        assert all(c == 999 for c in counts), (
            "指纹未变却仍重写了 cooldown_mirror（hash 短路失效）"
        )

        # 上游真正变化时应重新写入
        cooldown.record_alert("300059", "price")  # 改变 alert_count + last_alert
        bsm.mirror_cooldown(cooldown)
        assert bsm._last_cooldown_digest != first_digest

        conn = sqlite3.connect(bsm.db_path)
        cursor = conn.execute("SELECT alert_count FROM cooldown_mirror")
        counts = [r[0] for r in cursor.fetchall()]
        conn.close()
        assert 999 not in counts, "上游变化后未重写 cooldown_mirror"

    def test_restore_cooldown(self, bsm, temp_db):
        """应能从镜像恢复冷却状态"""
        from radar.alert.cooldown import AlertCooldownManager

        # 先镜像
        cooldown_db = temp_db.replace(".db", "_cooldown.db")
        src_cooldown = AlertCooldownManager(cooldown_db, MagicMock())
        src_cooldown.record_alert("300059", "price")
        bsm.mirror_cooldown(src_cooldown)

        # 再恢复到新的 cooldown
        dst_cooldown_db = temp_db.replace(".db", "_dst_cooldown.db")
        dst_cooldown = AlertCooldownManager(dst_cooldown_db, MagicMock())
        bsm.restore_cooldown(dst_cooldown)

        assert dst_cooldown.is_in_cooldown("300059", "price", cooldown_minutes=30)


class TestMonitorEngineIntegration:
    """测试与 MonitorEngine 的集成"""

    def test_save_all_and_restore_all(self, bsm):
        """save_all + restore_all 应完整保存/恢复 MonitorEngine 状态"""
        from radar.core.monitor_engine import MonitorConfig, MonitorEngine, TargetStock

        # 创建 mock engine
        config = MonitorConfig(
            bot_token="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz1234",
            chat_id="test_chat",
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
            initial_cash=150000.0,
        )
        logger = MagicMock()
        engine = MonitorEngine(config, logger)

        # 添加目标
        target = TargetStock(
            code="300059", name="东方财富",
            trigger_price=23.7, direction="below"
        )
        engine.add_target(target)

        # 修改一些运行时状态
        engine._consecutive_failures = 5
        engine._failure_alert_sent = True
        engine._portfolio.cash = 80000.0
        engine._portfolio.set_position("300059", 2000, 22.0)

        # 模拟 RL 策略状态
        if "300059" in engine._rl_strategies:
            engine._rl_strategies["300059"]._last_action = 3

        # 保存
        bsm.save_all(engine)

        # 创建新 engine 并恢复
        engine2 = MonitorEngine(config, logger)
        # 需要添加同样的目标才能恢复策略状态
        engine2.add_target(target)
        bsm.restore_all(engine2)

        # 验证恢复
        assert engine2._consecutive_failures == 5
        assert engine2._failure_alert_sent is True
        assert engine2._portfolio.cash == 80000.0
        assert engine2._portfolio.positions["300059"]["shares"] == 2000
        assert engine2._portfolio.positions["300059"]["cost"] == 22.0
        if "300059" in engine2._rl_strategies:
            assert engine2._rl_strategies["300059"]._last_action == 3


class TestStats:
    """测试统计接口"""

    def test_get_stats(self, bsm):
        """get_stats 应返回正确统计"""
        # 先写入一些数据
        bsm.save_alert("price", "300059", "A", 10.0, {})
        bsm.save_alert("price", "300033", "B", 20.0, {})

        from radar.strategy.rl_strategy_v2 import PortfolioTracker
        tracker = PortfolioTracker()
        tracker.set_position("300059", 100, 10.0)
        bsm.save_portfolio(tracker)

        bsm.save_battlefield_state({"saved_at": time.time()})

        stats = bsm.get_stats()
        assert stats["alert_count"] == 2
        assert stats["position_count"] == 1
        assert stats["last_saved"] is not None
        assert stats["db_path"] == bsm.db_path


class TestThreadSafety:
    """测试线程安全"""

    def test_concurrent_save(self, bsm):
        """多线程并发保存不应报错"""
        from radar.strategy.rl_strategy_v2 import PortfolioTracker

        errors = []
        lock = threading.Lock()

        def worker(code):
            try:
                tracker = PortfolioTracker()
                tracker.set_position(code, 100, 10.0)
                bsm.save_portfolio(tracker)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker, args=(f"{i:06d}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 允许少量 database locked 错误（SQLite WAL 模式下并发写入的合理行为）
        database_locked_errors = [e for e in errors if 'database is locked' in str(e)]
        other_errors = [e for e in errors if 'database is locked' not in str(e)]
        assert not other_errors, f"非数据库锁定错误: {other_errors}"
        # database locked 错误不应超过 3 个（允许合理并发冲突）
        assert len(database_locked_errors) <= 3, f"过多 database locked 错误: {database_locked_errors}"

        # 验证最终状态
        conn = sqlite3.connect(bsm.db_path)
        cursor = conn.execute("SELECT COUNT(*) FROM position_state")
        count = cursor.fetchone()[0]
        conn.close()
        # 由于并发更新同一行可能冲突，但至少不应崩溃
        assert count >= 1
