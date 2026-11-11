#!/usr/bin/env python3
"""Fixed, read-only Kronos market-state forecast bridge.

The bridge accepts normalized OHLCV bars on stdin and emits one JSON object on
stdout.  Model, tokenizer, source tree and calendar paths are fixed: callers
cannot supply paths, Python code, commands or model identifiers.

Kronos output is model inference, not a fact or trading signal.  The response
therefore labels it as an experimental scenario and never reports confidence
or an action recommendation.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import date, datetime
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Sequence


KRONOS_SOURCE = Path("/opt/ai/research/kronos")
MODEL_PATH = Path("/opt/ai/models/kronos/mini")
TOKENIZER_PATH = Path("/opt/ai/models/kronos/tokenizer-2k")
QLIB_CALENDAR = Path("/opt/ai/data/market/qlib/cn_data/calendars/day.txt")
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_SERIES = 3
MIN_LOOKBACK = 60
MAX_LOOKBACK = 512
MAX_PREDICTION_DAYS = 20
SYMBOL_PATTERN = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PRICE_FIELDS = ("open", "high", "low", "close")
FORECAST_FIELDS = (*PRICE_FIELDS, "volume", "amount")


def _iso_date(value: Any, field: str) -> str:
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} is not a valid date")
    return value


def _finite_number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = "positive " if positive else "finite "
        raise ValueError(f"{field} must be a {qualifier}number")
    return number


def normalize_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    allowed = {"schemaVersion", "task", "series", "lookback", "predictionDays"}
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise ValueError(f"request contains unsupported fields: {', '.join(unexpected)}")
    if value.get("schemaVersion") != "1.0" or value.get("task") != "kronos_market_state":
        raise ValueError("only schemaVersion=1.0 task=kronos_market_state is supported")

    lookback = value.get("lookback", 100)
    prediction_days = value.get("predictionDays", 5)
    if not isinstance(lookback, int) or isinstance(lookback, bool) or not MIN_LOOKBACK <= lookback <= MAX_LOOKBACK:
        raise ValueError(f"lookback must be an integer from {MIN_LOOKBACK} to {MAX_LOOKBACK}")
    if (
        not isinstance(prediction_days, int)
        or isinstance(prediction_days, bool)
        or not 1 <= prediction_days <= MAX_PREDICTION_DAYS
    ):
        raise ValueError(f"predictionDays must be an integer from 1 to {MAX_PREDICTION_DAYS}")

    raw_series = value.get("series")
    if not isinstance(raw_series, list) or not 1 <= len(raw_series) <= MAX_SERIES:
        raise ValueError(f"series must contain 1 to {MAX_SERIES} entries")

    normalized_series: list[dict[str, Any]] = []
    seen: set[str] = set()
    for series_index, item in enumerate(raw_series):
        if not isinstance(item, dict) or set(item) - {"symbol", "bars"}:
            raise ValueError(f"series[{series_index}] must contain only symbol and bars")
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError(f"series[{series_index}].symbol is invalid")
        if symbol in seen:
            raise ValueError("series symbols must be unique")
        seen.add(symbol)

        raw_bars = item.get("bars")
        if not isinstance(raw_bars, list) or len(raw_bars) < MIN_LOOKBACK:
            raise ValueError(f"{symbol} requires at least {MIN_LOOKBACK} bars")
        bars: list[dict[str, Any]] = []
        dates: set[str] = set()
        for bar_index, raw_bar in enumerate(raw_bars[-lookback:]):
            if not isinstance(raw_bar, dict):
                raise ValueError(f"{symbol}.bars[{bar_index}] must be an object")
            allowed_bar = {"date", "open", "high", "low", "close", "volume", "amount"}
            if set(raw_bar) - allowed_bar:
                raise ValueError(f"{symbol}.bars[{bar_index}] contains unsupported fields")
            day = _iso_date(raw_bar.get("date"), f"{symbol}.bars[{bar_index}].date")
            if day in dates:
                raise ValueError(f"{symbol} contains duplicate bar dates")
            dates.add(day)
            bar = {
                "date": day,
                "open": _finite_number(raw_bar.get("open"), "open", positive=True),
                "high": _finite_number(raw_bar.get("high"), "high", positive=True),
                "low": _finite_number(raw_bar.get("low"), "low", positive=True),
                "close": _finite_number(raw_bar.get("close"), "close", positive=True),
                "volume": _finite_number(raw_bar.get("volume", 0), "volume"),
                "amount": _finite_number(raw_bar.get("amount", 0), "amount"),
            }
            if bar["high"] < max(bar["open"], bar["close"], bar["low"]):
                raise ValueError(f"{symbol} contains a bar with high below open, close or low")
            if bar["low"] > min(bar["open"], bar["close"], bar["high"]):
                raise ValueError(f"{symbol} contains a bar with low above open, close or high")
            if bar["volume"] < 0 or bar["amount"] < 0:
                raise ValueError(f"{symbol} contains a bar with negative volume or amount")
            bars.append(bar)
        bars.sort(key=lambda bar: bar["date"])
        if len(bars) < MIN_LOOKBACK:
            raise ValueError(f"{symbol} requires at least {MIN_LOOKBACK} unique bars")
        normalized_series.append({"symbol": symbol, "bars": bars})

    # Batch inference requires equal context lengths.  Trim all series to the
    # shortest validated tail without inventing or forward-filling observations.
    common_length = min(len(item["bars"]) for item in normalized_series)
    normalized_series = [
        {"symbol": item["symbol"], "bars": item["bars"][-common_length:]}
        for item in normalized_series
    ]
    return {
        "schemaVersion": "1.0",
        "task": "kronos_market_state",
        "lookback": common_length,
        "predictionDays": prediction_days,
        "series": normalized_series,
    }


def _future_sessions(last_day: str, count: int) -> tuple[list[str], str]:
    after = date.fromisoformat(last_day)
    if QLIB_CALENDAR.is_file() and QLIB_CALENDAR.stat().st_size <= 4 * 1024 * 1024:
        values: list[str] = []
        with QLIB_CALENDAR.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()[:10]
                if DATE_PATTERN.fullmatch(text) and date.fromisoformat(text) > after:
                    values.append(text)
                    if len(values) == count:
                        return values, "Qlib CN trading calendar"

    # Calendar fallback is explicitly labelled because weekdays are not a
    # complete Chinese exchange calendar.
    import pandas as pd

    values = [item.date().isoformat() for item in pd.bdate_range(after, periods=count + 1) if item.date() > after]
    return values[:count], "weekday fallback (exchange holidays not verified)"


def _pct_change(values: Sequence[float]) -> list[float]:
    output: list[float] = []
    for previous, current in zip(values, values[1:]):
        if previous != 0:
            output.append(current / previous - 1.0)
    return output


def _std(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((item - mean) ** 2 for item in values) / (len(values) - 1))


def _forecast_bar_issues(bar: dict[str, Any]) -> list[str]:
    """Return safety violations without attempting to repair model output."""
    values: dict[str, float] = {}
    issues: list[str] = []
    for field in FORECAST_FIELDS:
        try:
            number = float(bar[field])
        except (KeyError, TypeError, ValueError):
            issues.append(f"{field}_not_finite")
            continue
        if not math.isfinite(number):
            issues.append(f"{field}_not_finite")
            continue
        values[field] = number

    if all(field in values for field in PRICE_FIELDS):
        if any(values[field] <= 0 for field in PRICE_FIELDS):
            issues.append("non_positive_price")
        if values["high"] < max(values["open"], values["close"], values["low"]):
            issues.append("high_below_open_close_or_low")
        if values["low"] > min(values["open"], values["close"], values["high"]):
            issues.append("low_above_open_close_or_high")
    if "volume" in values and values["volume"] < 0:
        issues.append("negative_volume")
    if "amount" in values and values["amount"] < 0:
        issues.append("negative_amount")
    return issues


def summarize_state(actual_bars: Sequence[dict[str, Any]], predicted: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not predicted or any(_forecast_bar_issues(item) for item in predicted):
        return {"state": "unavailable", "reason": "forecast contains invalid OHLCV bars"}
    try:
        actual_close = [float(item["close"]) for item in actual_bars]
        predicted_close = [float(item["close"]) for item in predicted]
    except (KeyError, TypeError, ValueError):
        return {"state": "unavailable", "reason": "forecast contains invalid close values"}
    if not predicted_close or any(not math.isfinite(value) or value <= 0 for value in predicted_close):
        return {"state": "unavailable", "reason": "forecast contains invalid close values"}

    horizon_return = (predicted_close[-1] / actual_close[-1] - 1.0) * 100
    threshold = 2.0
    if horizon_return >= threshold:
        state = "uptrend_candidate"
    elif horizon_return <= -threshold:
        state = "downtrend_candidate"
    else:
        state = "range_candidate"

    history_volatility = _std(_pct_change(actual_close[-21:]))
    forecast_volatility = _std(_pct_change([actual_close[-1], *predicted_close]))
    try:
        actual_volumes = [float(item.get("volume", 0) or 0) for item in actual_bars[-20:]]
        predicted_volumes = [float(item.get("volume", 0) or 0) for item in predicted]
    except (TypeError, ValueError):
        actual_volumes = []
        predicted_volumes = []
    average_actual_volume = sum(actual_volumes) / len(actual_volumes) if actual_volumes else 0
    average_predicted_volume = sum(predicted_volumes) / len(predicted_volumes) if predicted_volumes else 0
    return {
        "state": state,
        "horizonReturnPercent": round(horizon_return, 4),
        "historicalDailyVolatilityPercent": (
            round(history_volatility * 100, 4) if history_volatility is not None else None
        ),
        "forecastDailyVolatilityPercent": (
            round(forecast_volatility * 100, 4) if forecast_volatility is not None else None
        ),
        "forecastToRecentVolumeRatio": (
            round(average_predicted_volume / average_actual_volume, 4)
            if average_actual_volume > 0
            else None
        ),
    }


def _load_predictor() -> Any:
    for required in (KRONOS_SOURCE / "model" / "__init__.py", MODEL_PATH / "model.safetensors", TOKENIZER_PATH / "model.safetensors"):
        if not required.is_file():
            raise RuntimeError("fixed Kronos source or model artifact is unavailable")

    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    if str(KRONOS_SOURCE) not in sys.path:
        sys.path.insert(0, str(KRONOS_SOURCE))
    import numpy as np
    import torch
    from model import Kronos, KronosPredictor, KronosTokenizer

    np.random.seed(0)
    torch.manual_seed(0)
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    tokenizer = KronosTokenizer.from_pretrained(str(TOKENIZER_PATH))
    model = Kronos.from_pretrained(str(MODEL_PATH))
    return KronosPredictor(model, tokenizer, device="cpu", max_context=MAX_LOOKBACK)


def build_forecast(
    value: Any,
    *,
    predictor_factory: Callable[[], Any] | None = None,
    future_session_loader: Callable[[str, int], tuple[list[str], str]] | None = None,
) -> dict[str, Any]:
    request = normalize_request(value)
    import pandas as pd

    future_loader = future_session_loader or _future_sessions
    frames = []
    x_timestamps = []
    y_timestamps = []
    calendar_sources: set[str] = set()
    for item in request["series"]:
        bars = item["bars"]
        frame = pd.DataFrame(bars).set_index("date")
        frames.append(frame[["open", "high", "low", "close", "volume", "amount"]])
        x_timestamps.append(pd.Series(pd.to_datetime(frame.index)))
        future_days, calendar_source = future_loader(bars[-1]["date"], request["predictionDays"])
        if len(future_days) != request["predictionDays"]:
            raise RuntimeError("future trading calendar does not cover the requested horizon")
        calendar_sources.add(calendar_source)
        y_timestamps.append(pd.Series(pd.to_datetime(future_days)))

    # Kronos' local weight loader prints diagnostics to stdout.  stdout is the
    # JSON-RPC/data protocol channel, so keep the entire model boundary on
    # stderr even when an upstream library ignores ``verbose=False``.
    with redirect_stdout(sys.stderr):
        predictor = (predictor_factory or _load_predictor)()
        predictions = predictor.predict_batch(
            df_list=frames,
            x_timestamp_list=x_timestamps,
            y_timestamp_list=y_timestamps,
            pred_len=request["predictionDays"],
            T=1.0,
            top_k=0,
            top_p=0.9,
            sample_count=1,
            verbose=False,
        )
    if len(predictions) != len(request["series"]):
        raise RuntimeError("Kronos returned an unexpected batch size")

    results = []
    for item, prediction, future_dates in zip(request["series"], predictions, y_timestamps):
        if len(prediction) != request["predictionDays"]:
            raise RuntimeError("Kronos returned an unexpected forecast length")
        required_columns = {"open", "high", "low", "close", "volume", "amount"}
        if not required_columns.issubset(prediction.columns):
            raise RuntimeError("Kronos forecast is missing required columns")
        forecast_bars: list[dict[str, Any]] = []
        invalid_bar_count = 0
        invalid_dates: list[str] = []
        validation_issues: set[str] = set()
        for day, (_, row) in zip(future_dates, prediction.iterrows()):
            bar = {"date": day.date().isoformat()}
            for field in FORECAST_FIELDS:
                try:
                    number = float(row[field])
                except (TypeError, ValueError):
                    number = math.nan
                bar[field] = round(number, 6) if math.isfinite(number) else None
            issues = _forecast_bar_issues(bar)
            if issues:
                invalid_bar_count += 1
                invalid_dates.append(bar["date"])
                validation_issues.update(issues)
                continue
            forecast_bars.append(bar)

        if invalid_bar_count:
            result_status = "unavailable" if not forecast_bars else "partial"
            state_summary = {
                "state": "unavailable",
                "reason": "forecast horizon contains invalid OHLCV bars",
            }
        else:
            result_status = "experimental"
            state_summary = summarize_state(item["bars"], forecast_bars)
        results.append(
            {
                "symbol": item["symbol"],
                "status": result_status,
                "dataAsOf": item["bars"][-1]["date"],
                "forecastThrough": forecast_bars[-1]["date"] if forecast_bars else None,
                "requestedForecastThrough": future_dates.iloc[-1].date().isoformat(),
                "lookbackBars": len(item["bars"]),
                "predictedBars": forecast_bars,
                "validPredictedBars": len(forecast_bars),
                "invalidPredictedBars": invalid_bar_count,
                "invalidPredictedDates": invalid_dates,
                "validationIssues": sorted(validation_issues),
                "stateSummary": state_summary,
            }
        )

    warnings = [
        "Kronos 输出属于实验性模型推断，不是行情事实、概率置信度或交易信号。",
        "未来状态必须与 Qlib 横截面评分、实时证据和人工规则交叉验证。",
    ]
    if any(source.startswith("weekday fallback") for source in calendar_sources):
        warnings.append("Qlib 交易日历不足，预测日期使用工作日回退，可能包含休市日。")
    for result in results:
        if result["invalidPredictedBars"]:
            warnings.append(
                f"{result['symbol']} 有 {result['invalidPredictedBars']} 根预测柱未通过 OHLCV 安全校验；"
                "异常柱已排除，方向性状态已降级为不可用。"
            )
    return {
        "schemaVersion": "1.0",
        "task": "kronos_market_state",
        "status": "experimental",
        "provider": "Kronos-mini local model",
        "model": {
            "name": "Kronos-mini",
            "tokenizer": "Kronos-Tokenizer-2k",
            "device": "cpu",
            "sampling": {"temperature": 1.0, "topP": 0.9, "sampleCount": 1, "seed": 0},
        },
        "calendarSources": sorted(calendar_sources),
        "results": results,
        "warnings": warnings,
    }


def _read_stdin() -> Any:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("request exceeds 4 MiB")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("stdin must be UTF-8 JSON") from exc


def main() -> int:
    try:
        response = build_forecast(_read_stdin())
        json.dump(response, sys.stdout, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        print(f"Kronos bridge failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
