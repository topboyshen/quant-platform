from __future__ import annotations

import logging
import time
from types import SimpleNamespace

from radar.core.adaptive_trigger import AdaptiveTriggerManager
from radar.core.strategy_engine import AsyncStrategyEngine, StrategyResult
from radar.risk.macro_risk_control import MacroRiskControl


class _Notifier:
    def __init__(self):
        self.sent = []

    def send_system_alert(self, title, message):
        self.sent.append((title, message))
        return True


def test_macro_risk_control_alert_cooldown_is_not_consumed_by_check():
    rc = MacroRiskControl(logger_=logging.getLogger("test"))

    state = rc.check(vix=18.0, us_10y=4.0, crude_oil=70.0)

    assert state.level == "critical"
    assert rc.should_alert_now(state) is True
    rc.mark_alert_sent(state)
    assert rc.should_alert_now(state) is False


def test_macro_risk_control_logs_persistent_risk_once(caplog):
    rc = MacroRiskControl(logger_=logging.getLogger("test.persistent-risk"))
    caplog.set_level(logging.INFO, logger="test.persistent-risk")

    rc.check(vix=16.0, us_10y=4.7, crude_oil=96.0)
    rc.check(vix=16.1, us_10y=4.8, crude_oil=97.0)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "GROWTH_BLOCK" in warnings[0].getMessage()


def test_macro_risk_control_deduplicates_persistent_alerts():
    rc = MacroRiskControl(logger_=logging.getLogger("test"))

    state = rc.check(vix=16.0, us_10y=4.7, crude_oil=96.0)
    assert rc.should_alert_now(state) is True
    rc.mark_alert_sent(state)

    rc._last_alert_time = time.time() - 3600
    same_state = rc.check(vix=16.0, us_10y=4.7, crude_oil=96.0)
    assert rc.should_alert_now(same_state) is False

    # 不同 level 才触发告警（message 变化但 level 相同不触发）
    changed_state = state.__class__(
        level="critical",  # 从 growth_blocked 变为 critical
        vix=20.0,
        us_10y=state.us_10y,
        crude_oil=state.crude_oil,
        message="CRITICAL: VIX spike",
        timestamp=time.time(),
    )
    assert rc.should_alert_now(changed_state) is True


def test_macro_risk_control_keeps_state_when_data_unavailable():
    """宏观数据全部缺失时不得清除已有风险状态（数据中断 ≠ 风险解除）。"""
    rc = MacroRiskControl(logger_=logging.getLogger("test"))

    blocked = rc.check(vix=16.0, us_10y=4.7, crude_oil=96.0)
    assert blocked.level == "growth_blocked"

    blind = rc.check(vix=None, us_10y=None, crude_oil=None)
    assert blind.level == "growth_blocked"
    assert blind.us_10y == blocked.us_10y
    assert set(blind.cached_fields) == {"vix", "us_10y", "crude_oil"}


def test_macro_risk_control_blind_start_reports_missing_data():
    """冷启动且数据全缺失时必须告警，不能伪装成 ok。"""
    rc = MacroRiskControl(logger_=logging.getLogger("test"))
    state = rc.check(vix=None, us_10y=None, crude_oil=None)
    assert state.level == "warning"
    assert "DATA_UNAVAILABLE" in state.message


def test_partial_fetch_failure_does_not_clear_recent_risk():
    rc = MacroRiskControl(logger_=logging.getLogger("test"))
    rc.check(vix=16.0, us_10y=4.7, crude_oil=70.0)

    state = rc.check(vix=16.1, us_10y=None, crude_oil=None)

    assert state.level == "growth_blocked"
    assert state.us_10y == 4.7
    assert set(state.cached_fields) == {"us_10y", "crude_oil"}


def test_expired_observations_become_data_warning():
    rc = MacroRiskControl(logger_=logging.getLogger("test"))
    rc._observation_ttl_sec = 10
    rc.check(vix=16.0, us_10y=4.7, crude_oil=70.0)
    rc._state.observed_at = {name: time.time() - 11 for name in ("vix", "us_10y", "crude_oil")}

    state = rc.check(vix=None, us_10y=None, crude_oil=None)

    assert state.level == "warning"
    assert state.missing_fields == ["vix", "us_10y", "crude_oil"]


def test_adaptive_trigger_first_break_still_reports_original_trigger(tmp_path):
    db = tmp_path / "battlefield_state.db"
    manager = AdaptiveTriggerManager(
        db_path=str(db),
        step_pct=0.05,
        cooldown_hours=24,
        logger_=logging.getLogger("test"),
    )
    cfg = SimpleNamespace(rl_mode="off", ml_model_dir="models", ml_threshold=0.7, rl_model_dir="rl_models")
    engine = AsyncStrategyEngine(cfg, logging.getLogger("test"), max_workers=1)
    engine._adaptive_trigger = manager
    target = SimpleNamespace(
        code="000001",
        name="测试",
        trigger_price=10.0,
        direction="below",
        ml_enabled=False,
        rl_enabled=False,
    )

    result = engine.check_price(target, {"price": 9.9})

    assert result.triggered is True
    assert result.signal_info["trigger_price"] == 10.0
    assert result.signal_info["next_trigger_price"] == 9.5
    assert manager.get_current_trigger("000001", 10.0) == 9.5
    engine.shutdown()


def test_risk_filter_does_not_block_sell_or_reduce_signals():
    cfg = SimpleNamespace(rl_mode="off", ml_model_dir="models", ml_threshold=0.7, rl_model_dir="rl_models")
    engine = AsyncStrategyEngine(cfg, logging.getLogger("test"), max_workers=1)
    target = SimpleNamespace(code="300001", name="成长股", direction="below", tags=["growth"])
    risk = {"level": "growth_blocked", "message": "估值天花板"}
    sell_results = [
        StrategyResult("price", True, {"direction": "below"}, 1.0),
        StrategyResult("rl", True, {"action": "reduce"}, 1.0),
    ]

    filtered = engine._apply_risk_filter(sell_results, target, risk)

    assert [r.triggered for r in filtered] == [True, True]
    assert [r.risk_blocked for r in filtered] == [False, False]
    engine.shutdown()


def test_risk_filter_blocks_growth_buy_signals():
    cfg = SimpleNamespace(rl_mode="off", ml_model_dir="models", ml_threshold=0.7, rl_model_dir="rl_models")
    engine = AsyncStrategyEngine(cfg, logging.getLogger("test"), max_workers=1)
    target = SimpleNamespace(code="300001", name="成长股", direction="above", tags=["growth"])
    risk = {"level": "growth_blocked", "message": "估值天花板"}
    buy_results = [
        StrategyResult("price", True, {"direction": "above"}, 1.0),
        StrategyResult("rl", True, {"action": "open"}, 1.0),
    ]

    filtered = engine._apply_risk_filter(buy_results, target, risk)

    assert [r.triggered for r in filtered] == [False, False]
    assert [r.risk_blocked for r in filtered] == [True, True]
    engine.shutdown()


def test_risk_filter_blocks_ai_hardware_chain_by_name():
    cfg = SimpleNamespace(rl_mode="off", ml_model_dir="models", ml_threshold=0.7, rl_model_dir="rl_models")
    engine = AsyncStrategyEngine(cfg, logging.getLogger("test"), max_workers=1)
    target = SimpleNamespace(code="300308", name="中际旭创", direction="above", tags=[])
    risk = {"level": "growth_blocked", "message": "盘前指令：严禁新增 AI 硬件链买入"}
    buy_results = [StrategyResult("price", True, {"direction": "above"}, 1.0)]

    filtered = engine._apply_risk_filter(buy_results, target, risk)

    assert filtered[0].triggered is False
    assert filtered[0].risk_blocked is True
    assert filtered[0].signal_info["risk_level"] == "growth_blocked"
    engine.shutdown()


def test_risk_filter_blocks_resource_under_commodity_crash():
    """大宗商品暴跌时，资源股买入信号应被拦截（2025-06-24 需求）"""
    cfg = SimpleNamespace(rl_mode="off", ml_model_dir="models", ml_threshold=0.7, rl_model_dir="rl_models")
    engine = AsyncStrategyEngine(cfg, logging.getLogger("test"), max_workers=1)
    target = SimpleNamespace(code="600938", name="中国海油", direction="above", tags=["resource", "dividend"])
    risk = {"level": "growth_blocked", "message": "资源/红利作为 UPS 蓄水池"}
    buy_results = [StrategyResult("price", True, {"direction": "above"}, 1.0)]

    filtered = engine._apply_risk_filter(buy_results, target, risk)

    # 资源股在大宗商品暴跌期间应被拦截
    assert filtered[0].triggered is False
    assert filtered[0].risk_blocked is True
    assert filtered[0].signal_info["risk_level"] == "growth_blocked"
    engine.shutdown()


def test_manual_risk_override_takes_precedence(tmp_path, monkeypatch):
    override = tmp_path / "risk_override.json"
    monkeypatch.setattr(MacroRiskControl, "_OVERRIDE_FILE", override)
    override.write_text(
        '{"expires_at": %s, "state": {"level": "growth_blocked", "vix": 16.2, "us_10y": 4.5, "crude_oil": 96.0, "message": "manual"}}'
        % (time.time() + 3600),
        encoding="utf-8",
    )

    rc = MacroRiskControl(logger_=logging.getLogger("test"))
    state = rc.check_with_fetch()

    assert state.level == "growth_blocked"
    assert state.crude_oil == 96.0
    assert MacroRiskControl.load_state_from_file()["message"] == "manual"


def test_check_with_fetch_throttles_external_requests(monkeypatch):
    rc = MacroRiskControl(logger_=logging.getLogger("test"))
    rc._fetch_interval_sec = 300
    calls = []
    monkeypatch.setattr(rc, "fetch_vix", lambda: calls.append("vix") or 16.0)
    monkeypatch.setattr(rc, "fetch_us_10y", lambda: calls.append("us_10y") or 4.0)
    monkeypatch.setattr(rc, "fetch_crude_oil", lambda: calls.append("oil") or 70.0)

    first = rc.check_with_fetch()
    second = rc.check_with_fetch()

    assert first is second
    assert calls == ["vix", "us_10y", "oil"]


def test_risk_control_restores_recent_persisted_state(tmp_path, monkeypatch):
    state_file = tmp_path / "risk_state.json"
    state_file.write_text(
        '{"level":"growth_blocked","vix":16.0,"us_10y":4.7,"crude_oil":70.0,'
        '"message":"persisted","timestamp":%s,"observed_at":{},"cached_fields":[],"missing_fields":[]}'
        % time.time(),
        encoding="utf-8",
    )
    monkeypatch.setattr(MacroRiskControl, "_STATE_FILE", state_file)

    rc = MacroRiskControl(logger_=logging.getLogger("test"))

    assert rc._state is not None
    assert rc._state.level == "growth_blocked"
    assert rc._state.message == "persisted"


def test_historical_risk_replay_rejects_post_decision_quote_and_cache(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(MacroRiskControl, "_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(MacroRiskControl, "_OVERRIDE_FILE", tmp_path / "override.json")
    monkeypatch.setattr("radar.risk.macro_risk_control.time.time", lambda: 2_000.0)
    control = MacroRiskControl(logger_=logging.getLogger("test.replay"))
    control.check(
        vix=16.0,
        us_10y=4.0,
        crude_oil=70.0,
        observation_metadata={
            field: {
                "source_timestamp": 1_900.0,
                "available_at": 1_900.0,
                "source": "current-cache",
            }
            for field in ("vix", "us_10y", "crude_oil")
        },
    )

    replay = control.check(
        vix=19.0,
        us_10y=None,
        crude_oil=None,
        observation_metadata={
            "vix": {
                "source_timestamp": 1_100.0,
                "available_at": 1_100.0,
                "source": "post-decision",
            }
        },
        decision_timestamp=1_000.0,
    )

    assert replay.level == "warning"
    assert replay.vix is None
    assert replay.cached_fields == []
    assert replay.missing_fields == ["vix", "us_10y", "crude_oil"]
