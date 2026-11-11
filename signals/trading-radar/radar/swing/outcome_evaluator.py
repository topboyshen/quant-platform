"""Forward-only outcome collection for immutable Swing decisions."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, Optional

import pandas as pd

HORIZONS = (1, 3, 5, 10, 20)


@dataclass(frozen=True, slots=True)
class OutcomeSettings:
    benchmark_code: str = "510300"
    notional: float = 100_000.0
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    slippage_rate: float = 0.0005
    stamp_duty_rate: float = 0.0  # ETF 默认免印花税
    limit_up_unfilled_ratio: float = 0.095


def _normalise_daily(frame: Any, *, as_of: str) -> pd.DataFrame:
    if frame is None or getattr(frame, "empty", True):
        return pd.DataFrame()
    data = frame.copy()
    required = {"date", "open", "high", "low", "close"}
    if not required.issubset(data.columns):
        return pd.DataFrame()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.date
    for column in ("open", "high", "low", "close"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=list(required))
    data = data[(data["open"] > 0) & (data["close"] > 0)]
    data = data[data["date"] <= date.fromisoformat(as_of)]
    return data.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


class SwingOutcomeEvaluator:
    """Collect only outcomes that are observable as of the evaluation date."""

    def __init__(
        self,
        store: Any,
        fetcher: Any,
        *,
        settings: Optional[OutcomeSettings] = None,
        logger: Optional[logging.Logger] = None,
        evidence_recorder: Any = None,
    ):
        self.store = store
        self.fetcher = fetcher
        self.settings = settings or OutcomeSettings()
        self.logger = logger or logging.getLogger(__name__)
        self._frames: Dict[str, pd.DataFrame] = {}
        self.evidence_recorder = evidence_recorder

    def _frame(self, code: str, *, as_of: str) -> pd.DataFrame:
        key = f"{code}:{as_of}"
        if key not in self._frames:
            raw = self.fetcher.get_historical_data(code, days=420)
            self._frames[key] = _normalise_daily(raw, as_of=as_of)
        return self._frames[key]

    def evaluate_pending(self, *, as_of: Optional[str] = None, limit: int = 500) -> Dict[str, int]:
        as_of = as_of or date.today().isoformat()
        date.fromisoformat(as_of)
        decisions = self.store.list_decisions_for_outcome(limit=limit)
        benchmark = self._frame(self.settings.benchmark_code, as_of=as_of)
        summary = {"checked": 0, "inserted": 0, "not_mature": 0, "unfilled": 0, "failed": 0}
        if self.evidence_recorder is not None:
            summary.update({"evidence_" + key: 0 for key in (
                "written", "existing", "conflict", "failed", "unavailable", "skipped_pre_cutover", "basis_diff", "provenance_unknown")})
        if benchmark.empty:
            summary["failed"] = len(decisions)
            return summary

        for decision in decisions:
            summary["checked"] += 1
            try:
                outcomes = self._evaluate_one(decision, benchmark=benchmark, as_of=as_of)
                if not outcomes:
                    summary["not_mature"] += 1
                for outcome in outcomes:
                    if self.store.record_decision_outcome(outcome):
                        summary["inserted"] += 1
                        if outcome["fill_status"] == "unfilled":
                            summary["unfilled"] += 1
                        # Observe the exact frames already used; no new fetch,
                        # DB lookup, historical sweep, or business-result change.
                        if self.evidence_recorder is not None:
                            try:
                                evidence = self.evidence_recorder.capture(
                                    decision=decision, outcome=outcome,
                                    target_frame=self._frames.get(f"{decision['code']}:{as_of}"),
                                    benchmark=benchmark, settings=self.settings, as_of=as_of,
                                )
                                status = evidence["status"]
                                if status not in {"written", "existing", "conflict", "failed", "unavailable", "skipped_pre_cutover"}:
                                    raise ValueError("unknown evidence result")
                                summary["evidence_" + status] += 1
                                summary["evidence_basis_diff"] += int(bool(evidence.get("basis_diff")))
                                summary["evidence_provenance_unknown"] += int(bool(evidence.get("provenance_unknown")))
                                if status in {"failed", "conflict", "unavailable"} or evidence.get("basis_diff") or evidence.get("provenance_unknown"):
                                    self.logger.warning("outcome evidence status=%s decision_id=%s horizon=%s; business outcome unchanged",
                                                        status, decision.get("decision_id"), outcome["horizon_sessions"])
                            except Exception as exc:
                                summary["evidence_failed"] += 1
                                self.logger.warning("outcome evidence failed decision_id=%s horizon=%s error_type=%s; business outcome unchanged",
                                                    decision.get("decision_id"), outcome["horizon_sessions"], type(exc).__name__)
            except Exception as exc:
                summary["failed"] += 1
                self.logger.warning(
                    "结果采集失败 decision_id=%s code=%s: %s",
                    decision.get("decision_id"),
                    decision.get("code"),
                    exc,
                )
        return summary

    def _evaluate_one(
        self, decision: Dict[str, Any], *, benchmark: pd.DataFrame, as_of: str
    ) -> Iterable[Dict[str, Any]]:
        decision_date = date.fromisoformat(str(decision["decision_date"]))
        sessions = benchmark[benchmark["date"] > decision_date]["date"].tolist()
        completed = set(decision.get("completed_horizons") or set())
        if not sessions:
            return []
        code_frame = self._frame(str(decision["code"]), as_of=as_of)
        code_by_date = {row.date: row for row in code_frame.itertuples(index=False)}
        benchmark_by_date = {row.date: row for row in benchmark.itertuples(index=False)}
        entry_date = sessions[0]
        entry_bar = code_by_date.get(entry_date)
        prior_rows = code_frame[code_frame["date"] <= decision_date]
        prior_close = (
            float(decision["reference_close"])
            if decision.get("reference_close") is not None
            else (float(prior_rows.iloc[-1]["close"]) if not prior_rows.empty else None)
        )

        terminal_unfilled_reason = ""
        if entry_bar is None:
            terminal_unfilled_reason = "suspended_or_no_next_session_bar"
        elif prior_close is None or prior_close <= 0:
            terminal_unfilled_reason = "decision_reference_close_missing"
        elif (
            float(entry_bar.open) / prior_close - 1 >= self.settings.limit_up_unfilled_ratio
            and float(entry_bar.low) >= float(entry_bar.open) * 0.999
        ):
            terminal_unfilled_reason = "limit_up_no_executable_liquidity"

        results = []
        for horizon in HORIZONS:
            if horizon in completed or len(sessions) < horizon:
                continue
            exit_date = sessions[horizon - 1]
            if terminal_unfilled_reason:
                results.append(
                    self._unfilled(
                        decision,
                        horizon=horizon,
                        as_of=as_of,
                        entry_date=entry_date,
                        reason=terminal_unfilled_reason,
                    )
                )
                continue
            exit_bar = code_by_date.get(exit_date)
            benchmark_entry = benchmark_by_date.get(entry_date)
            benchmark_exit = benchmark_by_date.get(exit_date)
            if exit_bar is None:
                results.append(
                    self._unfilled(
                        decision,
                        horizon=horizon,
                        as_of=as_of,
                        entry_date=entry_date,
                        reason="exit_bar_missing_or_suspended",
                    )
                )
                continue
            if benchmark_entry is None or benchmark_exit is None:
                continue
            results.append(
                self._filled(
                    decision,
                    code_frame=code_frame,
                    entry_bar=entry_bar,
                    exit_bar=exit_bar,
                    benchmark_entry=benchmark_entry,
                    benchmark_exit=benchmark_exit,
                    prior_close=float(prior_close),
                    horizon=horizon,
                    as_of=as_of,
                )
            )
        return results

    def _filled(
        self,
        decision: Dict[str, Any],
        *,
        code_frame: pd.DataFrame,
        entry_bar: Any,
        exit_bar: Any,
        benchmark_entry: Any,
        benchmark_exit: Any,
        prior_close: float,
        horizon: int,
        as_of: str,
    ) -> Dict[str, Any]:
        settings = self.settings
        entry_open = float(entry_bar.open)
        exit_close = float(exit_bar.close)
        entry_fill = entry_open * (1 + settings.slippage_rate)
        exit_fill = exit_close * (1 - settings.slippage_rate)
        shares = math.floor(settings.notional / entry_fill / 100) * 100
        if shares <= 0:
            return self._unfilled(
                decision,
                horizon=horizon,
                as_of=as_of,
                entry_date=entry_bar.date,
                reason="notional_below_one_board_lot",
            )
        buy_amount = entry_fill * shares
        sell_amount = exit_fill * shares
        buy_fee = max(buy_amount * settings.commission_rate, settings.min_commission)
        sell_fee = (
            max(sell_amount * settings.commission_rate, settings.min_commission)
            + sell_amount * settings.stamp_duty_rate
        )
        cash_in = buy_amount + buy_fee
        cash_out = sell_amount - sell_fee
        gross_return = exit_close / entry_open - 1
        net_return = cash_out / cash_in - 1
        benchmark_return = float(benchmark_exit.close) / float(benchmark_entry.open) - 1
        window = code_frame[
            (code_frame["date"] >= entry_bar.date)
            & (code_frame["date"] <= exit_bar.date)
        ]
        mfe = float(window["high"].max()) / entry_fill - 1
        mae = float(window["low"].min()) / entry_fill - 1
        fee_return = (buy_fee + sell_fee) / cash_in
        slippage_return = max(0.0, gross_return - net_return - fee_return)
        return {
            "decision_id": decision["decision_id"],
            "horizon_sessions": horizon,
            "evaluation_as_of": as_of,
            "entry_date": entry_bar.date.isoformat(),
            "entry_open": entry_open,
            "entry_fill": entry_fill,
            "exit_date": exit_bar.date.isoformat(),
            "exit_close": exit_close,
            "exit_fill": exit_fill,
            "benchmark_code": settings.benchmark_code,
            "benchmark_return": benchmark_return,
            "gap_return": entry_open / prior_close - 1,
            "gross_return": gross_return,
            "net_return": net_return,
            "excess_return": net_return - benchmark_return,
            "mfe": mfe,
            "mae": mae,
            "fees_return": fee_return,
            "slippage_return": slippage_return,
            "fill_status": "filled",
            "fill_reason": "next_session_open",
        }

    def _unfilled(
        self,
        decision: Dict[str, Any],
        *,
        horizon: int,
        as_of: str,
        entry_date: Any,
        reason: str,
    ) -> Dict[str, Any]:
        return {
            "decision_id": decision["decision_id"],
            "horizon_sessions": horizon,
            "evaluation_as_of": as_of,
            "entry_date": entry_date.isoformat() if hasattr(entry_date, "isoformat") else str(entry_date),
            "benchmark_code": self.settings.benchmark_code,
            "fill_status": "unfilled",
            "fill_reason": reason,
        }
