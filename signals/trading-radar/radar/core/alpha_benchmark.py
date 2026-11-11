"""
L1 主战区指标：超额曲线（策略 vs 沪深300）与策略仓位占比。

沪深300点位优先使用 akshare.stock_zh_index_spot_em（可选依赖）；失败时可关闭抓取以免刷屏。
抓取间隔默认 60s，可用 RADAR_HS300_FETCH_INTERVAL_SEC 覆盖。
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from radar.core.monitor_engine import MonitorEngine

logger = logging.getLogger(__name__)

_FETCH_INTERVAL_SEC = float(os.getenv("RADAR_HS300_FETCH_INTERVAL_SEC", "60"))

_cn_day: Optional[str] = None
_strategy_anchor_equity: float = 0.0
_hs300_anchor_price: float = 0.0

_last_hs300_fetch_ts: float = 0.0
_cached_hs300_price: Optional[float] = None


def reset_alpha_benchmark_state_for_tests() -> None:
    """测试用：重置北京时间锚点与 HS300 缓存。"""
    global _cn_day, _strategy_anchor_equity, _hs300_anchor_price
    global _last_hs300_fetch_ts, _cached_hs300_price
    _cn_day = None
    _strategy_anchor_equity = 0.0
    _hs300_anchor_price = 0.0
    _last_hs300_fetch_ts = 0.0
    _cached_hs300_price = None


def _beijing_calendar_date_str() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).date().isoformat()


def _fetch_hs300_price_best_effort() -> Optional[float]:
    """返回沪深300指数点位（近似）。优先 akshare。"""
    try:
        import akshare as ak

        df = ak.stock_zh_index_spot_em()
        if df is None or df.empty:
            return None
        name_col = "名称" if "名称" in df.columns else None
        price_col = "最新价" if "最新价" in df.columns else None
        if not name_col or not price_col:
            return None
        sub = df[df[name_col].astype(str).str.contains("沪深300", na=False)]
        if sub.empty:
            return None
        return float(sub.iloc[0][price_col])
    except Exception as e:
        logger.debug("[Alpha] akshare HS300: %s", e)

    try:
        import requests

        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {"secid": "1.000300", "fields": "f43,f170"}
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
        raw = data.get("data") or {}
        f43 = raw.get("f43")
        if f43 is None:
            return None
        # Eastmoney f43：常为整数报价×10000，兼容小数直接返回
        v = float(f43)
        if v > 100000:
            return v / 10000.0
        if v > 10000:
            return v / 1000.0
        return v
    except Exception as e:
        logger.debug("[Alpha] eastmoney HS300: %s", e)
        return None


def _hs300_price_throttled() -> Optional[float]:
    global _last_hs300_fetch_ts, _cached_hs300_price

    now = time.time()
    if now - _last_hs300_fetch_ts >= _FETCH_INTERVAL_SEC:
        _last_hs300_fetch_ts = now
        px = _fetch_hs300_price_best_effort()
        if px is not None and px > 0:
            _cached_hs300_price = px
    return _cached_hs300_price


def refresh_alpha_and_allocation_metrics(engine: MonitorEngine) -> None:
    """刷新 Alpha 曲线 Gauge 与策略仓位占比。"""
    global _cn_day, _strategy_anchor_equity, _hs300_anchor_price

    try:
        from radar.core import prometheus_extra as pe

        if not getattr(pe, "_HAVE", False):
            return
        from radar.core.prometheus_extra import (
            observe_alpha_curve_indices,
            observe_strategy_tag_allocation_ratios,
        )
    except (ImportError, AttributeError):
        # Prometheus指标导入失败，安全退出  # 安全忽略
        return

    portfolio = engine._portfolio
    eq = float(portfolio.get_portfolio_value())

    today = _beijing_calendar_date_str()
    if _cn_day != today:
        _cn_day = today
        _strategy_anchor_equity = eq
        _hs300_anchor_price = 0.0

    strat_idx = (eq / max(_strategy_anchor_equity, 1e-9)) * 100.0

    hs300_px = _hs300_price_throttled()
    hs300_idx: Optional[float] = None
    if hs300_px is not None and hs300_px > 0:
        if _hs300_anchor_price <= 0:
            _hs300_anchor_price = hs300_px
        hs300_idx = (hs300_px / _hs300_anchor_price) * 100.0

    observe_alpha_curve_indices(strat_idx, hs300_idx)

    code_to_tag: Dict[str, str] = {}
    for t in engine.targets:
        raw = getattr(t, "strategy_tag", "") or ""
        code_to_tag[t.code] = raw.strip() or "untagged"

    tag_mv: Dict[str, float] = defaultdict(float)
    for code, pos in portfolio.positions.items():
        shares = int(pos.get("shares", 0) or 0)
        if shares <= 0:
            continue
        lp = float(pos.get("last_price", 0.0) or pos.get("cost", 0.0) or 0.0)
        tag = code_to_tag.get(code, "untagged")
        tag_mv[tag] += shares * lp

    denom = eq if eq > 1e-9 else 1.0
    ratios = {tag: mv / denom for tag, mv in tag_mv.items()}
    observe_strategy_tag_allocation_ratios(ratios)
