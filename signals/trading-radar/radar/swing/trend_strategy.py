"""TrendConfirmStrategy - 日线 / 周线技术趋势确认。

给 ML/RL 放假，降维启用技术分析（MA/MACD），并把周期从日内强制拉长到日线(D)
与周线(W)。系统不再高频焦虑，只在「大于 50% 确定性」的趋势大浪来临时苏醒：

- 周线 MACD 金叉（DIF 上穿 DEA）
- 放量突破 60 日均线（收盘上穿 MA60 且成交量 > N× 近 20 日均量）

数据经由 ``radar.data.market_data_v2.MarketDataFetcher.get_historical_data``（东财
push2his 日 K，已修复 ETF secid 解析），周线由 pandas ``resample('W')`` 得到。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd

from radar.data.kline_sources import source_attempts_snapshot


@dataclass(slots=True)
class TrendSignal:
    """一条趋势确认信号。"""

    code: str
    name: str
    signal_type: str       # weekly_macd_golden_cross / volume_breakout_ma60
    period: str            # "W" / "D"
    title: str             # 人类可读标题（用于 Telegram Fact）
    detail: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.5


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def compute_macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """返回包含 dif / dea / hist 三列的 DataFrame（与 close 同索引）。"""
    dif = _ema(close, fast) - _ema(close, slow)
    dea = _ema(dif, signal)
    hist = (dif - dea) * 2
    return pd.DataFrame({"dif": dif, "dea": dea, "hist": hist})


def _is_golden_cross(dif: pd.Series, dea: pd.Series) -> bool:
    """最近一根 K 上 DIF 上穿 DEA（前一根在下方/相等，当前在上方）。"""
    if len(dif) < 2 or len(dea) < 2:
        return False
    return bool(dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1])


class TrendConfirmStrategy:
    """日线 / 周线趋势确认器。"""

    def __init__(
        self,
        fetcher: Any,
        logger: Optional[logging.Logger] = None,
        *,
        ma_periods: Optional[List[int]] = None,
        volume_breakout_ratio: float = 1.5,
        history_days: int = 250,
        required_data_date: Optional[str] = None,
        daily_breakout_enabled: bool = True,
        volume_window: int = 20,
    ):
        self.fetcher = fetcher
        self.logger = logger or logging.getLogger(__name__)
        self.ma_periods = sorted(ma_periods or [20, 60])
        self.ma_long = max(self.ma_periods)
        self.volume_breakout_ratio = float(volume_breakout_ratio)
        # 日线放量突破分支可关闭（2026-10-17 复盘：开发段显著为负）；关闭后仍计算诊断但不出信号
        self.daily_breakout_enabled = bool(daily_breakout_enabled)
        self.volume_window = int(volume_window)
        if self.volume_window < 1:
            raise ValueError("volume_window must be >= 1")
        self.history_days = int(history_days)
        if required_data_date is not None and date.fromisoformat(required_data_date).isoformat() != required_data_date:
            raise ValueError("required_data_date must be canonical YYYY-MM-DD")
        self.required_data_date = required_data_date
        self.last_data_rows = 0
        self.last_data_ok = False
        self.last_data_date: Optional[str] = None
        self.last_close: Optional[float] = None
        self.last_data_source: str = ""
        self.last_diagnostics: Dict[str, Any] = {}

    def _reset_diagnostics(self, code: str) -> None:
        """Per-evaluation evidence only; never reused as a strategy input."""
        self.last_diagnostics = {
            "code": code,
            "data": {
                "ready": False, "rows": 0, "minimum_rows": self.ma_long + 5,
                "data_date": None, "required_date": self.required_data_date,
                "reason_codes": ["data_unavailable"],
            },
            "daily": {
                "evaluated": False, "reason_codes": ["data_not_ready"],
                "ma_period": self.ma_long, "volume_threshold": self.volume_breakout_ratio,
                **dict.fromkeys(("previous_close", "close", "previous_ma", "ma",
                                "previous_at_or_below_ma", "close_above_ma", "price_cross",
                                "volume", "average_volume20", "volume_ratio_raw",
                                "volume_ratio", "volume_ok", "triggered")),
            },
            "weekly": {
                "evaluated": False, "reason_codes": ["data_not_ready"],
                "minimum_bars": 35,
                **dict.fromkeys(("completed_bars", "previous_week_date", "week_date",
                                "previous_dif", "previous_dea", "dif", "dea",
                                "previous_at_or_below", "now_above", "golden_cross", "triggered")),
            },
        }
        self._diagnose("daily", lambda: {"volume_threshold": self.volume_breakout_ratio})

    def _diagnose(self, section: str, values) -> None:
        """Diagnostics are best-effort and cannot fail an otherwise valid decision."""
        try:
            update = values()
            for key, value in update.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    update[key] = value if math.isfinite(value) else None
            self.last_diagnostics[section].update(update)
        except Exception:
            # Even an unexpected diagnostic serialization defect is not a rule change.
            try:
                self.last_diagnostics[section]["reason_codes"] = ["diagnostic_error"]
            except Exception:
                pass

    def evaluate(self, code: str, name: str = "") -> List[TrendSignal]:
        """取日 K，计算日线/周线信号，返回触发的 TrendSignal 列表。"""
        self.last_data_rows = 0
        self.last_data_ok = False
        self.last_data_date = None
        self.last_close = None
        self.last_data_source = ""
        self._reset_diagnostics(code)
        df = self._load_daily(code)
        self.last_data_rows = 0 if df is None else len(df)
        self._diagnose("data", lambda: {
            "rows": self.last_data_rows,
            "data_date": None if df is None or df.empty else df["date"].iloc[-1].strftime("%Y-%m-%d"),
        })
        if df is None or len(df) < self.ma_long + 5:
            if df is not None:
                self._diagnose("data", lambda: {"reason_codes": ["insufficient_daily_rows"]})
            self.logger.warning("%s(%s) 日 K 数据不足，跳过趋势评估", name, code)
            return []
        self.last_data_date = df["date"].iloc[-1].strftime("%Y-%m-%d")
        self.last_close = float(df["close"].iloc[-1])
        self.last_data_source = str(df.attrs.get("source") or "")
        # Recheck after normalisation: even a custom/cache fetcher may ignore
        # the requirement, or its newest close may have been filtered out.
        if self.required_data_date is not None and self.last_data_date != self.required_data_date:
            self._diagnose("data", lambda: {"reason_codes": ["data_date_mismatch"]})
            self.logger.warning("%s 日K日期不匹配: required_date=%s actual_date=%s; 不评估信号", code, self.required_data_date, self.last_data_date)
            return []
        self.last_data_ok = True
        self._diagnose("data", lambda: {"ready": True, "reason_codes": []})

        signals: List[TrendSignal] = []
        self._eval_volume_breakout(df, code, name, signals)
        self._eval_weekly_golden_cross(df, code, name, signals)
        return signals

    # ------------------------------------------------------------------ 数据
    def _load_daily(self, code: str) -> Optional[pd.DataFrame]:
        try:
            kwargs = {"days": self.history_days}
            if self.required_data_date is not None:
                kwargs["required_date"] = self.required_data_date
            df = self.fetcher.get_historical_data(code, **kwargs)
        except Exception as e:
            self._diagnose("data", lambda: {"reason_codes": ["fetch_failed"]})
            self.logger.error("获取 %s 日 K 失败: %s", code, e)
            return None
        def source_evidence():
            evidence = source_attempts_snapshot(
                getattr(df, "attrs", {}).get("source_attempts"),
                code=code, required_date=self.required_data_date)
            return {"source_attempts": evidence} if evidence is not None else {}
        self._diagnose("data", source_evidence)
        if df is None or getattr(df, "empty", True):
            self._diagnose("data", lambda: {"reason_codes": ["empty_daily_data"]})
            return None
        df = df.copy()
        if "close" not in df.columns and "price" in df.columns:
            df["close"] = df["price"]
        if "date" not in df.columns or "close" not in df.columns:
            self._diagnose("data", lambda: {"reason_codes": ["missing_date_or_close"]})
            return None

        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["date", "close"])
        df = df[df["close"] > 0]
        if df.empty:
            self._diagnose("data", lambda: {"reason_codes": ["no_valid_daily_rows"]})
            return None
        df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
        return df.reset_index(drop=True)

    # ----------------------------------------------------------- 日线放量破位
    def _eval_volume_breakout(
        self, df: pd.DataFrame, code: str, name: str, signals: List[TrendSignal]
    ) -> None:
        close = df["close"].astype(float)
        ma_long = close.rolling(self.ma_long).mean()
        if ma_long.iloc[-1] != ma_long.iloc[-1] or ma_long.iloc[-2] != ma_long.iloc[-2]:
            self._diagnose("daily", lambda: {"reason_codes": ["ma_unavailable"]})
            return  # NaN（数据不足）

        prev_below = close.iloc[-2] <= ma_long.iloc[-2]
        now_above = close.iloc[-1] > ma_long.iloc[-1]

        volume = df["volume"].astype(float) if "volume" in df.columns else None
        vol_ratio = None
        volume_ok = False
        avg_vol = None
        if volume is not None:
            avg_vol = volume.iloc[-(self.volume_window + 1):-1].mean()  # 近 N 日（不含当日）均量，默认 20
            if pd.notna(avg_vol) and avg_vol > 0:
                vol_ratio = round(float(volume.iloc[-1] / avg_vol), 2)
                volume_ok = vol_ratio >= self.volume_breakout_ratio

        self._diagnose("daily", lambda: self._daily_diagnostics(
            close, ma_long, volume, avg_vol, vol_ratio, prev_below, now_above, volume_ok
        ))

        if self.daily_breakout_enabled and prev_below and now_above and volume_ok:
            signals.append(
                TrendSignal(
                    code=code,
                    name=name,
                    signal_type="volume_breakout_ma60",
                    period="D",
                    title=f"{name} 放量突破 MA{self.ma_long}",
                    detail={
                        "data_date": df["date"].iloc[-1].strftime("%Y-%m-%d"),
                        "close": round(float(close.iloc[-1]), 4),
                        f"ma{self.ma_long}": round(float(ma_long.iloc[-1]), 4),
                        "volume_ratio": vol_ratio,
                        "volume_threshold": self.volume_breakout_ratio,
                    },
                    confidence=0.55,
                )
            )

    # ----------------------------------------------------------- 周线金叉
    def _daily_diagnostics(self, close, ma, volume, avg_vol, ratio, below, above, volume_ok):
        ready = volume is not None and avg_vol is not None and math.isfinite(float(avg_vol)) and avg_vol > 0
        ready = ready and math.isfinite(float(volume.iloc[-1]))
        reasons = []
        if volume is None:
            reasons.append("volume_missing")
        elif not ready:
            reasons.append("volume_unavailable")
        if not bool(below):
            reasons.append("previous_close_above_ma")
        if not bool(above):
            reasons.append("close_not_above_ma")
        if ready and not bool(volume_ok):
            reasons.append("volume_below_threshold")
        return {
            "evaluated": bool(ready), "reason_codes": reasons,
            "previous_close": float(close.iloc[-2]), "close": float(close.iloc[-1]),
            "previous_ma": float(ma.iloc[-2]), "ma": float(ma.iloc[-1]),
            "previous_at_or_below_ma": bool(below), "close_above_ma": bool(above),
            "price_cross": bool(below and above),
            "volume": None if volume is None else float(volume.iloc[-1]),
            "average_volume20": None if avg_vol is None else float(avg_vol),
            "volume_ratio_raw": float(volume.iloc[-1] / avg_vol) if ready else None,
            "volume_ratio": ratio, "volume_ok": bool(volume_ok) if ready else None,
            "triggered": bool(below and above and volume_ok) if ready else None,
        }

    def _eval_weekly_golden_cross(
        self, df: pd.DataFrame, code: str, name: str, signals: List[TrendSignal]
    ) -> None:
        weekly = self._resample_completed_weekly(df)
        self._diagnose("weekly", lambda: {
            "completed_bars": 0 if weekly is None else len(weekly),
            "previous_week_date": weekly.index[-2].strftime("%Y-%m-%d") if weekly is not None and len(weekly) >= 2 else None,
            "week_date": weekly.index[-1].strftime("%Y-%m-%d") if weekly is not None and len(weekly) else None,
            "reason_codes": ["insufficient_completed_weeks"],
        })
        if weekly is None or len(weekly) < 35:
            return
        macd = compute_macd(weekly["close"].astype(float))
        golden_cross = _is_golden_cross(macd["dif"], macd["dea"])
        self._diagnose("weekly", lambda: self._weekly_diagnostics(macd, golden_cross))
        if golden_cross:
            signals.append(
                TrendSignal(
                    code=code,
                    name=name,
                    signal_type="weekly_macd_golden_cross",
                    period="W",
                    title=f"{name} 周线 MACD 金叉",
                    detail={
                        "data_date": weekly.index[-1].strftime("%Y-%m-%d"),
                        "dif": round(float(macd["dif"].iloc[-1]), 4),
                        "dea": round(float(macd["dea"].iloc[-1]), 4),
                        "weekly_close": round(float(weekly["close"].iloc[-1]), 4),
                    },
                    confidence=0.6,
                )
            )

    @staticmethod
    def _weekly_diagnostics(macd, golden_cross):
        values = {
            "previous_dif": float(macd["dif"].iloc[-2]), "previous_dea": float(macd["dea"].iloc[-2]),
            "dif": float(macd["dif"].iloc[-1]), "dea": float(macd["dea"].iloc[-1]),
        }
        ready = all(math.isfinite(value) for value in values.values())
        reasons = [] if golden_cross else ["no_weekly_golden_cross"]
        return {
            **values, "evaluated": ready,
            "reason_codes": reasons if ready else ["weekly_values_unavailable"],
            "previous_at_or_below": bool(values["previous_dif"] <= values["previous_dea"]) if ready else None,
            "now_above": bool(values["dif"] > values["dea"]) if ready else None,
            "golden_cross": golden_cross if ready else None,
            "triggered": golden_cross if ready else None,
        }

    @staticmethod
    def _resample_weekly(df: pd.DataFrame) -> Optional[pd.DataFrame]:
        agg = {"close": "last"}
        if "open" in df.columns:
            agg["open"] = "first"
        if "high" in df.columns:
            agg["high"] = "max"
        if "low" in df.columns:
            agg["low"] = "min"
        if "volume" in df.columns:
            agg["volume"] = "sum"
        weekly = df.set_index("date").resample("W-FRI", label="right", closed="right").agg(agg)
        weekly = weekly.dropna(subset=["close"])
        return weekly if not weekly.empty else None

    @classmethod
    def _resample_completed_weekly(cls, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        """只返回已经收盘的周 K，避免周一至周四用半根周 K 重复触发。"""
        weekly = cls._resample_weekly(df)
        if weekly is None:
            return None
        latest_daily = pd.Timestamp(df["date"].max()).normalize()
        weekly = weekly[weekly.index.normalize() <= latest_daily]
        return weekly if not weekly.empty else None
