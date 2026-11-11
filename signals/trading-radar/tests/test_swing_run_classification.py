from radar_swing_eod import _audit_result


def _result(**overrides):
    value = {
        "trend_targets": 10,
        "trend_data_ok": 10,
        "trend_data_failed": [],
        "generated_signals": 0,
        "risk_blocked_signals": 0,
        "decision_signals": 0,
        "decision_risk_blocked_signals": 0,
        "collect_status": {
            "total_amount": {"available": True},
            "margin_growth_pct": {"available": True},
            "limit_ratio": {"available": True},
        },
        "audit_reason_codes": [],
    }
    value.update(overrides)
    return value


def test_complete_zero_signal_run_is_audited_as_no_signal():
    audit = _audit_result(_result())
    assert audit["status"] == "success"
    assert audit["outcome"] == "no_signal"
    assert audit["evaluated_count"] == 10


def test_all_generated_signals_blocked_is_not_no_signal():
    audit = _audit_result(
        _result(
            generated_signals=2,
            risk_blocked_signals=2,
            decision_signals=2,
            decision_risk_blocked_signals=2,
        )
    )
    assert audit["status"] == "success"
    assert audit["outcome"] == "blocked"
    assert audit["signal_count"] == 2
    assert audit["allowed_signal_count"] == 0
    assert audit["blocked_signal_count"] == 2


def test_partial_target_coverage_never_claims_no_signal():
    audit = _audit_result(
        _result(
            trend_data_ok=8,
            trend_data_failed=["a", "b"],
        )
    )
    assert audit["status"] == "partial"
    assert audit["outcome"] == "data_unavailable"
    assert "trend_data_partial" in audit["reason_codes"]


def test_all_target_data_failure_is_failed():
    audit = _audit_result(
        _result(
            trend_data_ok=0,
            trend_data_failed=[str(index) for index in range(10)],
        )
    )
    assert audit["status"] == "failed"
    assert audit["outcome"] == "data_unavailable"
    assert "trend_data_unavailable" in audit["reason_codes"]


def test_cross_day_duplicate_detection_is_not_a_new_daily_signal():
    audit = _audit_result(
        _result(
            generated_signals=1,
            decision_signals=0,
        )
    )
    assert audit["status"] == "success"
    assert audit["outcome"] == "no_signal"
    assert audit["signal_count"] == 0


def test_unexpected_inner_nontrading_guard_is_recorded_as_skipped():
    audit = _audit_result({"skipped": True, "reason": "non_trading_day"})
    assert audit["status"] == "skipped"
    assert audit["outcome"] == "skipped"
    assert audit["reason_codes"] == ["non_trading_day"]
