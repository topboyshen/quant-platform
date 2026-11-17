"""Risk gate and run-audit parity with the legacy swing EOD semantics."""

from qp.signals.audit import audit_result
from qp.signals.risk import gate_action, is_growth_risk_target


def test_risk_gate_levels():
    assert gate_action("ok", "成长", "半导体ETF") == "allowed"
    for level in ("critical", "warning", "unavailable", "weird", None, ""):
        assert gate_action(level, "宽基", "沪深300ETF") == "blocked"


def test_growth_blocked_only_blocks_growth():
    assert gate_action("growth_blocked", "成长", "创业板ETF") == "blocked"
    assert gate_action("growth_blocked", "宽基", "半导体ETF") == "blocked"
    assert gate_action("growth_blocked", "宽基", "沪深300ETF") == "allowed"
    assert gate_action("growth_blocked", "防御", "黄金ETF") == "allowed"
    assert is_growth_risk_target("硬科技", "x") is True
    assert is_growth_risk_target("跨境", "纳斯达克科技ETF") is True
    assert is_growth_risk_target("周期", "有色ETF") is False


def test_audit_skipped():
    got = audit_result({"trend_targets": 40, "skipped": True})
    assert got["status"] == "skipped" and got["outcome"] == "skipped"
    assert got["reason_codes"] == ["non_trading_day"]
    assert got["evaluated_count"] == 0 and got["signal_count"] == 0


def test_audit_all_failed_is_data_unavailable():
    got = audit_result(
        {"trend_targets": 40, "trend_data_ok": 0, "trend_data_failed": [{"code": str(i)} for i in range(40)]}
    )
    assert got["status"] == "failed" and got["outcome"] == "data_unavailable"
    assert got["reason_codes"] == ["trend_data_unavailable"]
    assert got["failed_count"] == 40


def test_audit_no_signal_success():
    got = audit_result(
        {
            "trend_targets": 40,
            "trend_data_ok": 40,
            "trend_data_failed": [],
            "decision_signals": 0,
            "decision_risk_blocked_signals": 0,
        }
    )
    assert (got["status"], got["outcome"]) == ("success", "no_signal")
    assert got["allowed_signal_count"] == 0 and got["blocked_signal_count"] == 0


def test_audit_blocked_and_emitted():
    blocked = audit_result(
        {
            "trend_targets": 40,
            "trend_data_ok": 40,
            "trend_data_failed": [],
            "decision_signals": 2,
            "decision_risk_blocked_signals": 2,
        }
    )
    assert (blocked["status"], blocked["outcome"]) == ("success", "blocked")
    assert blocked["blocked_signal_count"] == 2 and blocked["allowed_signal_count"] == 0

    emitted = audit_result(
        {
            "trend_targets": 40,
            "trend_data_ok": 40,
            "trend_data_failed": [],
            "decision_signals": 2,
            "decision_risk_blocked_signals": 1,
        }
    )
    assert (emitted["status"], emitted["outcome"]) == ("success", "emitted")
    assert emitted["allowed_signal_count"] == 1


def test_audit_partial_on_failed_targets_or_reasons():
    partial = audit_result(
        {
            "trend_targets": 40,
            "trend_data_ok": 39,
            "trend_data_failed": [{"code": "510300"}],
            "decision_signals": 0,
            "decision_risk_blocked_signals": 0,
        }
    )
    assert (partial["status"], partial["outcome"]) == ("partial", "data_unavailable")
    assert partial["reason_codes"] == ["trend_data_partial"]

    macro = audit_result(
        {
            "trend_targets": 40,
            "trend_data_ok": 40,
            "trend_data_failed": [],
            "decision_signals": 0,
            "decision_risk_blocked_signals": 0,
            "collect_status": {"total_amount": {"available": False}},
        }
    )
    # Legacy: a partial run with zero signals reports data_unavailable, not no_signal.
    assert (macro["status"], macro["outcome"]) == ("partial", "data_unavailable")
    assert macro["reason_codes"] == ["macro_data_partial"]


def test_audit_reason_codes_dedup_and_cap():
    raw = [f"r{i}" for i in range(10)] + ["r0", "r1", ""]
    got = audit_result(
        {
            "trend_targets": 40,
            "trend_data_ok": 40,
            "trend_data_failed": [],
            "decision_signals": 0,
            "decision_risk_blocked_signals": 0,
            "audit_reason_codes": raw,
        }
    )
    assert got["reason_codes"] == [f"r{i}" for i in range(8)]
