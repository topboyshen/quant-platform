"""Conservative validity and risk-gate counterfactual summaries."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, Iterator, List, Sequence, Tuple

VALIDITY_LABELS = ("样本不足", "有效性不确定", "初步有效", "未见有效")


def walk_forward_windows(
    ordered_dates: Sequence[str], *, train_sessions: int = 120, test_sessions: int = 20
) -> Iterator[Tuple[Tuple[str, ...], Tuple[str, ...]]]:
    """Yield strictly ordered train/test windows with no overlapping future data."""
    dates = tuple(sorted(dict.fromkeys(ordered_dates)))
    if train_sessions <= 0 or test_sessions <= 0:
        raise ValueError("walk-forward 窗口必须为正数")
    cursor = train_sessions
    while cursor < len(dates):
        train = dates[max(0, cursor - train_sessions) : cursor]
        test = dates[cursor : cursor + test_sessions]
        if not test:
            break
        if date.fromisoformat(train[-1]) >= date.fromisoformat(test[0]):
            raise ValueError("walk-forward 训练窗口污染测试窗口")
        yield train, test
        cursor += test_sessions


def _mean_ci(values: Iterable[float]) -> Dict[str, float | None]:
    cleaned = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not cleaned:
        return {"mean": None, "low": None, "high": None}
    mean = statistics.fmean(cleaned)
    if len(cleaned) < 2:
        return {"mean": mean, "low": None, "high": None}
    margin = 1.96 * statistics.stdev(cleaned) / math.sqrt(len(cleaned))
    return {"mean": mean, "low": mean - margin, "high": mean + margin}


def _wilson(successes: int, total: int) -> Dict[str, float | None]:
    if total <= 0:
        return {"rate": None, "low": None, "high": None}
    z = 1.96
    rate = successes / total
    denominator = 1 + z * z / total
    center = (rate + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / denominator
    return {"rate": rate, "low": max(0.0, center - margin), "high": min(1.0, center + margin)}


def _group_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    net = [float(row["net_return"]) for row in rows if row.get("net_return") is not None]
    excess = [float(row["excess_return"]) for row in rows if row.get("excess_return") is not None]
    mfe = [float(row["mfe"]) for row in rows if row.get("mfe") is not None]
    mae = [float(row["mae"]) for row in rows if row.get("mae") is not None]
    hits = sum(value > 0 for value in net)
    return {
        "sample_size": len(net),
        "hit_rate": _wilson(hits, len(net)),
        "net_return": _mean_ci(net),
        "benchmark_excess": _mean_ci(excess),
        "mfe": _mean_ci(mfe),
        "mae": _mean_ci(mae),
    }


@dataclass(frozen=True, slots=True)
class ValidityPolicy:
    min_samples: int = 30
    primary_horizon: int = 20


def build_validity_summary(
    rows: List[Dict[str, Any]], *, policy: ValidityPolicy | None = None
) -> Dict[str, Any]:
    """Return labels safe for UI plus full metrics only after sample maturity."""
    policy = policy or ValidityPolicy()
    oos_rows = [
        row
        for row in rows
        if row.get("validation_split") in {"live_oos", "walk_forward_oos"}
    ]
    raw_signal = [row for row in oos_rows if int(row.get("raw_trigger_count") or 0) > 0]
    allowed = [row for row in raw_signal if row.get("decision_state") == "allowed"]
    blocked = [row for row in raw_signal if row.get("decision_state") == "blocked"]
    allowed_metrics = _group_metrics(allowed)
    sample_size = int(allowed_metrics["sample_size"])
    net_ci = allowed_metrics["net_return"]
    excess_ci = allowed_metrics["benchmark_excess"]
    if sample_size < policy.min_samples:
        label = "样本不足"
    elif net_ci["low"] is not None and net_ci["low"] > 0 and excess_ci["low"] is not None and excess_ci["low"] > 0:
        label = "初步有效"
    elif (
        net_ci["high"] is not None
        and net_ci["high"] <= 0
    ) or (
        excess_ci["high"] is not None
        and excess_ci["high"] <= 0
    ):
        label = "未见有效"
    else:
        label = "有效性不确定"

    blocked_returns = [
        float(row["net_return"])
        for row in blocked
        if row.get("net_return") is not None
    ]
    risk_counterfactual = {
        "sample_size": len(blocked_returns),
        "prevented_loss": sum(max(-value, 0.0) for value in blocked_returns),
        "missed_upside": sum(max(value, 0.0) for value in blocked_returns),
        "mean_blocked_return": (
            statistics.fmean(blocked_returns) if blocked_returns else None
        ),
        "risk_value_return": (
            -statistics.fmean(blocked_returns) if blocked_returns else None
        ),
    }
    mature = sample_size >= policy.min_samples
    return {
        "label": label,
        "sample_size": sample_size,
        "minimum_samples": policy.min_samples,
        "primary_horizon_sessions": policy.primary_horizon,
        "method": "live_oos_or_walk_forward_oos",
        # UI/API must not display a premature exact win rate.  Full metrics are
        # unlocked only after the declared minimum sample size is reached.
        "metrics": {
            "raw_signal": _group_metrics(raw_signal),
            "allowed": allowed_metrics,
            "blocked_counterfactual": _group_metrics(blocked),
        } if mature else None,
        "risk_counterfactual": risk_counterfactual,
    }
