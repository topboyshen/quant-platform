"""Synthetic-only evidence tests; the frozen oracle predates diagnostics.

No providers, real databases, notifications or EOD execution are used here.
"""

import json
import random
from dataclasses import asdict
from types import SimpleNamespace

import pandas as pd
import pytest

from radar.swing.trend_strategy import TrendConfirmStrategy, TrendSignal


def frame(rows=175, *, end="2026-09-11", last_volume=150.0):
    result = pd.DataFrame({
        "date": pd.bdate_range(end=end, periods=rows),
        "close": [10.0] * (rows - 1) + [12.0],
        "volume": [100.0] * (rows - 1) + [last_volume],
    })
    return result


def strategy(data, **kwargs):
    fetcher = SimpleNamespace(get_historical_data=lambda *args, **options: data.copy())
    return TrendConfirmStrategy(fetcher, **kwargs)


def signal_bytes(signals):
    return json.dumps([asdict(item) for item in signals], ensure_ascii=False, sort_keys=True)


def legacy_signals(raw, *, required_date=None, ma_period=60, volume_threshold=1.5):
    """Frozen pre-change normalization and rules, independent of diagnostics code."""
    df = raw.copy()
    if df.empty:
        return []
    if "close" not in df and "price" in df:
        df["close"] = df["price"]
    if "date" not in df or "close" not in df:
        return []
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ("open", "high", "low", "close", "volume"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    df = df[df["close"] > 0].sort_values("date").drop_duplicates("date", keep="last")
    if len(df) < ma_period + 5:
        return []
    data_date = df["date"].iloc[-1].strftime("%Y-%m-%d")
    if required_date is not None and required_date != data_date:
        return []
    signals = []
    close = df["close"].astype(float)
    ma = close.rolling(ma_period).mean()
    volume = df["volume"].astype(float) if "volume" in df else None
    ratio = None
    volume_ok = False
    if volume is not None:
        avg = volume.iloc[-21:-1].mean()
        if pd.notna(avg) and avg > 0:
            ratio = round(float(volume.iloc[-1] / avg), 2)
            volume_ok = ratio >= volume_threshold
    if close.iloc[-2] <= ma.iloc[-2] and close.iloc[-1] > ma.iloc[-1] and volume_ok:
        signals.append(TrendSignal(
            "510300", "测试ETF", "volume_breakout_ma60", "D", f"测试ETF 放量突破 MA{ma_period}",
            {"data_date": data_date, "close": round(float(close.iloc[-1]), 4),
             f"ma{ma_period}": round(float(ma.iloc[-1]), 4), "volume_ratio": ratio,
             "volume_threshold": volume_threshold}, 0.55,
        ))
    # Only close is used by MACD; the old OHLC/volume aggregations do not affect it.
    weekly = df.set_index("date").resample("W-FRI", label="right", closed="right").agg({"close": "last"})
    weekly = weekly.dropna(subset=["close"])
    weekly = weekly[weekly.index.normalize() <= pd.Timestamp(df["date"].max()).normalize()]
    if len(weekly) >= 35:
        prices = weekly["close"].astype(float)
        dif = prices.ewm(span=12, adjust=False).mean() - prices.ewm(span=26, adjust=False).mean()
        dea = dif.ewm(span=9, adjust=False).mean()
        if dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1]:
            signals.append(TrendSignal(
                "510300", "测试ETF", "weekly_macd_golden_cross", "W", "测试ETF 周线 MACD 金叉",
                {"data_date": weekly.index[-1].strftime("%Y-%m-%d"),
                 "dif": round(float(dif.iloc[-1]), 4), "dea": round(float(dea.iloc[-1]), 4),
                 "weekly_close": round(float(weekly["close"].iloc[-1]), 4)}, 0.6,
            ))
    return signals


@pytest.mark.parametrize("rows", [20, 64, 65, 170, 175, 250])
@pytest.mark.parametrize("volume", [0.0, 149.4, 149.6, 150.0, 200.0])
def test_signal_bytes_equal_frozen_rules(rows, volume):
    data = frame(rows, last_volume=volume)
    actual = strategy(data).evaluate("510300", "测试ETF")
    assert signal_bytes(actual) == signal_bytes(legacy_signals(data))


@pytest.mark.parametrize("seed", range(12))
def test_randomized_signal_bytes_unchanged(seed):
    rng = random.Random(seed)
    data = frame(280)
    data["close"] = [max(1.0, 10 + rng.uniform(-2, 2)) for _ in range(len(data))]
    data["volume"] = [rng.uniform(10, 300) for _ in range(len(data))]
    assert signal_bytes(strategy(data).evaluate("510300", "测试ETF")) == signal_bytes(legacy_signals(data))


def test_exactly_35_complete_weeks_records_both_crosses():
    subject = strategy(frame())
    output = subject.evaluate("510300", "测试ETF")
    assert len(output) == 2
    diagnostic = subject.last_diagnostics
    assert diagnostic["weekly"]["completed_bars"] == 35
    assert diagnostic["weekly"]["evaluated"] is True
    assert diagnostic["weekly"]["golden_cross"] is True
    assert diagnostic["weekly"]["previous_week_date"] == "2026-09-04"
    assert diagnostic["weekly"]["week_date"] == "2026-09-11"
    assert diagnostic["weekly"]["previous_dif"] == diagnostic["weekly"]["previous_dea"] == 0
    assert diagnostic["daily"]["price_cross"] is True
    assert diagnostic["daily"]["volume_ok"] is True
    assert diagnostic["daily"]["triggered"] is True
    json.dumps(diagnostic, allow_nan=False)


def test_insufficient_weeks_not_claimed_as_evaluated_no_cross():
    subject = strategy(frame(170))
    subject.evaluate("510300", "测试ETF")
    weekly = subject.last_diagnostics["weekly"]
    assert subject.last_data_ok is True
    assert weekly["completed_bars"] == 34
    assert weekly["evaluated"] is False
    assert weekly["golden_cross"] is None
    assert weekly["reason_codes"] == ["insufficient_completed_weeks"]


def test_incomplete_week_does_not_create_weekly_signal():
    data = frame(175, end="2026-09-10")
    subject = strategy(data)
    assert signal_bytes(subject.evaluate("510300", "测试ETF")) == signal_bytes(legacy_signals(data))
    assert subject.last_diagnostics["weekly"]["week_date"] == "2026-09-04"
    assert subject.last_diagnostics["weekly"]["golden_cross"] is False


@pytest.mark.parametrize("amount,ratio,allowed", [(149.4, 1.49, False), (149.6, 1.5, True)])
def test_raw_and_rounded_ratio_preserve_threshold_boundary(amount, ratio, allowed):
    subject = strategy(frame(last_volume=amount))
    subject.evaluate("510300", "测试ETF")
    daily = subject.last_diagnostics["daily"]
    assert daily["volume_ratio_raw"] == pytest.approx(amount / 100)
    assert daily["volume_ratio"] == ratio
    assert daily["volume_threshold"] == 1.5
    assert daily["volume_ok"] is allowed
    assert ("volume_below_threshold" in daily["reason_codes"]) is (not allowed)


@pytest.mark.parametrize("column", ["date", "close", "volume"])
def test_missing_columns_have_explicit_diagnostics_without_rule_change(column):
    data = frame().drop(columns=[column])
    subject = strategy(data)
    assert signal_bytes(subject.evaluate("510300", "测试ETF")) == signal_bytes(legacy_signals(data))
    if column == "volume":
        assert subject.last_data_ok is True
        assert subject.last_diagnostics["daily"]["volume_ok"] is None
        assert "volume_missing" in subject.last_diagnostics["daily"]["reason_codes"]
    else:
        assert subject.last_diagnostics["data"]["reason_codes"] == ["missing_date_or_close"]


def test_old_data_stops_both_branches_and_preserves_original_result():
    data = frame()
    subject = strategy(data, required_data_date="2026-09-14")
    assert signal_bytes(subject.evaluate("510300", "测试ETF")) == signal_bytes(legacy_signals(data, required_date="2026-09-14"))
    assert subject.last_diagnostics["data"]["data_date"] == "2026-09-11"
    assert subject.last_diagnostics["data"]["reason_codes"] == ["data_date_mismatch"]
    assert subject.last_diagnostics["daily"]["evaluated"] is False
    assert subject.last_diagnostics["weekly"]["evaluated"] is False


def test_future_data_is_not_reported_as_current():
    subject = strategy(frame(end="2026-09-15"), required_data_date="2026-09-14")
    assert subject.evaluate("510300", "测试ETF") == []
    assert subject.last_data_ok is False
    assert subject.last_diagnostics["data"]["reason_codes"] == ["data_date_mismatch"]


def test_fetch_exception_preserves_empty_result_and_reason():
    def unavailable(*args, **kwargs):
        raise ConnectionError("synthetic provider failure")
    subject = TrendConfirmStrategy(SimpleNamespace(get_historical_data=unavailable))
    assert subject.evaluate("510300", "测试ETF") == []
    assert subject.last_data_ok is False
    assert subject.last_diagnostics["data"]["reason_codes"] == ["fetch_failed"]


@pytest.mark.parametrize("ma_period", [20, 40, 80])
def test_nondefault_existing_ma_rule_is_unchanged(ma_period):
    data = frame()
    subject = strategy(data, ma_periods=[20, ma_period], volume_breakout_ratio=1.8)
    assert signal_bytes(subject.evaluate("510300", "测试ETF")) == signal_bytes(legacy_signals(
        data, ma_period=ma_period, volume_threshold=1.8,
    ))
    assert subject.last_diagnostics["daily"]["ma_period"] == ma_period
    assert subject.last_diagnostics["daily"]["volume_threshold"] == 1.8


def test_consecutive_evaluations_reset_all_evidence():
    subject = strategy(frame())
    subject.evaluate("510300", "测试ETF")
    previous = subject.last_diagnostics
    subject.fetcher = SimpleNamespace(get_historical_data=lambda *a, **kw: pd.DataFrame())
    assert subject.evaluate("510500", "另ETF") == []
    assert previous["daily"]["triggered"] is True
    assert subject.last_diagnostics["code"] == "510500"
    assert subject.last_diagnostics["data"]["rows"] == 0
    assert subject.last_diagnostics["daily"]["close"] is None
    assert subject.last_diagnostics["weekly"]["completed_bars"] is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_diagnostic_numbers_are_json_null(value):
    subject = strategy(frame(last_volume=value))
    subject.evaluate("510300", "测试ETF")
    json.dumps(subject.last_diagnostics, allow_nan=False)
    assert subject.last_diagnostics["daily"]["volume"] is None


def test_diagnostic_fault_does_not_change_signal_outputs(monkeypatch):
    data = frame()
    subject = strategy(data)
    def broken(*args):
        raise RuntimeError("synthetic diagnostic-only failure")
    monkeypatch.setattr(subject, "_daily_diagnostics", broken)
    assert signal_bytes(subject.evaluate("510300", "测试ETF")) == signal_bytes(legacy_signals(data))
    assert subject.last_diagnostics["daily"]["reason_codes"] == ["diagnostic_error"]


def test_nonfinite_weekly_values_are_unavailable_not_a_false_no_cross(monkeypatch):
    from radar.swing import trend_strategy
    def invalid_macd(close):
        return pd.DataFrame({"dif": [float("nan")] * len(close), "dea": [0.0] * len(close)})
    monkeypatch.setattr(trend_strategy, "compute_macd", invalid_macd)
    subject = strategy(frame())
    subject.evaluate("510300", "测试ETF")
    assert subject.last_diagnostics["weekly"]["evaluated"] is False
    assert subject.last_diagnostics["weekly"]["golden_cross"] is None
    assert subject.last_diagnostics["weekly"]["reason_codes"] == ["weekly_values_unavailable"]
    json.dumps(subject.last_diagnostics, allow_nan=False)


def test_eod_capture_is_detached_bounded_and_best_effort():
    from radar_swing_eod import _capture_trigger_diagnostics
    subject = strategy(frame())
    subject.evaluate("510300", "测试ETF")
    saved = _capture_trigger_diagnostics(subject, "510300")
    subject.last_diagnostics["data"]["rows"] = 0
    assert saved["data"]["rows"] == 175
    assert _capture_trigger_diagnostics(subject, "510500") is None
    subject.last_diagnostics["data"]["rows"] = float("nan")
    assert _capture_trigger_diagnostics(subject, "510300") is None
    subject.last_diagnostics = {"code": "510300", "oversized": "x" * 12000}
    assert _capture_trigger_diagnostics(subject, "510300") is None


def test_real_synthetic_eod_persists_same_attempt_diagnostics(tmp_path, monkeypatch):
    from radar.swing.store import SwingStore
    from tests.test_swing_required_eod_audit import test_real_eod_persists_failure_not_false_no_signal
    test_real_eod_persists_failure_not_false_no_signal(tmp_path, monkeypatch, 10, "success")
    run = SwingStore(str(tmp_path / "swing.db")).get_latest_eod_run()
    diagnostic = run["details"]["trigger_diagnostics"]
    assert diagnostic["schema_version"] == "quant.swing-trigger-diagnostics.v1"
    assert diagnostic["run_id"] == run["run_id"]
    assert diagnostic["decision_date"] == run["decision_date"]
    assert len(diagnostic["targets"]) == 10
    assert all(item["data"]["ready"] for item in diagnostic["targets"])
    assert all(item["daily"]["triggered"] is False for item in diagnostic["targets"])
    json.dumps(diagnostic, allow_nan=False)
