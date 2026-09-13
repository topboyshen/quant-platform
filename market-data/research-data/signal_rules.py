"""Shared causal close-of-day signal definitions; no loading or execution.

The five existing rule thresholds are preserved. Volume baselines use the
complete 20 observations preceding the signal day, never the signal day itself.
"""
import numpy as np
import pandas as pd


def _values(frame, name):
    values = pd.to_numeric(frame[name], errors="raise").astype(float)
    valid = np.isfinite(values)
    valid &= values >= 0 if name in {"volume", "amount"} else values > 0
    return values.where(valid)


def _boolean(series):
    return series.fillna(False).astype(bool)


def sig_turtle(frame):
    close, high = _values(frame, "close"), _values(frame, "high")
    return _boolean((close > high.shift(1).rolling(20, min_periods=20).max())
                    & (_values(frame, "amount") > 1e5)
                    & (close > _values(frame, "open")) & (close > close.shift(1)))


def sig_ma_volume(frame):
    close, volume = _values(frame, "close"), _values(frame, "volume")
    ma5 = close.rolling(5, min_periods=5).mean()
    ma20 = close.rolling(20, min_periods=20).mean()
    previous_volume20 = volume.shift(1).rolling(20, min_periods=20).mean()
    return _boolean((ma5.shift(1) < ma20.shift(1)) & (ma5 > ma20)
                    & (volume > previous_volume20 * 1.5))


def sig_high_tight_flag(frame):
    high, low, volume = (_values(frame, key) for key in ("high", "low", "volume"))
    high40 = high.rolling(40, min_periods=40).max()
    low40 = low.rolling(40, min_periods=40).min()
    high10 = high.rolling(10, min_periods=10).max()
    low10 = low.rolling(10, min_periods=10).min()
    previous_volume20 = volume.shift(1).rolling(20, min_periods=20).mean()
    return _boolean((high40 / low40 > 1.6) & (high10 / low10 < 1.15)
                    & (low10 >= high40 * 0.8) & (volume < previous_volume20 * 0.6))


def sig_limit_up_shakeout(frame):
    close, volume = _values(frame, "close"), _values(frame, "volume")
    return _boolean((close.shift(1) >= close.shift(2) * 1.095)
                    & (close < _values(frame, "open"))
                    & (volume > volume.shift(1) * 2.0)
                    & (_values(frame, "low") >= close.shift(1)))


def sig_uptrend_limit_down(frame):
    close, volume = _values(frame, "close"), _values(frame, "volume")
    previous_close = close.shift(1)
    ma20 = previous_close.rolling(20, min_periods=20).mean()
    ma60 = previous_close.rolling(60, min_periods=60).mean()
    previous_volume20 = volume.shift(1).rolling(20, min_periods=20).mean()
    return _boolean((ma20 > ma60) & (close <= previous_close * 0.905)
                    & (volume > previous_volume20 * 2.0))


RULES = {
    "turtle_trade": (sig_turtle, 21, ("close", "high", "amount", "open")),
    "ma_volume": (sig_ma_volume, 21, ("close", "volume")),
    "high_tight_flag": (sig_high_tight_flag, 40, ("high", "low", "volume")),
    "limit_up_shakeout": (sig_limit_up_shakeout, 3, ("close", "open", "low", "volume")),
    "uptrend_limit_down": (sig_uptrend_limit_down, 61, ("close", "volume")),
}


def latest_signal(name, data):
    """Evaluate the scanner's last observation using the exact shared rule.

Only the required complete prefix tail is needed; no future observation or data
loading occurs. Malformed/misaligned fields are not silently filled with zeros.
"""
    function, minimum, fields = RULES[name]
    lengths = [len(data[field]) for field in fields]
    if len(set(lengths)) != 1:
        raise ValueError("signal fields must have equal lengths")
    if lengths[0] < minimum:
        return False
    frame = pd.DataFrame({field: np.asarray(data[field])[-minimum:] for field in fields})
    return bool(function(frame).iloc[-1])
