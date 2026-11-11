"""MacroRiskControl - 宏观风险控制系统

基于 VIX、美债收益率、原油价格等外部宏观指标，
实施全局仓位控制与策略拦截。

集成点：
- 在 MonitorEngineV3.scan_once() 之前调用 check()
- 在 AsyncStrategyEngine.evaluate_all() 之前调用 filter_signals()
- 结果通过 AlertManager 发送系统级告警
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RiskState:
    """风险控制系统当前状态"""
    level: str          # "ok" / "warning" / "critical" / "growth_blocked"
    vix: Optional[float] = None
    us_10y: Optional[float] = None
    crude_oil: Optional[float] = None
    message: str = ""
    timestamp: float = 0.0
    observed_at: Dict[str, float] = field(default_factory=dict)
    source_timestamps: Dict[str, float] = field(default_factory=dict)
    available_at: Dict[str, float] = field(default_factory=dict)
    sources: Dict[str, str] = field(default_factory=dict)
    cached_fields: List[str] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "level": self.level,
            "vix": self.vix,
            "us_10y": self.us_10y,
            "crude_oil": self.crude_oil,
            "message": self.message,
            "timestamp": self.timestamp,
            "observed_at": self.observed_at,
            "source_timestamps": self.source_timestamps,
            "available_at": self.available_at,
            "sources": self.sources,
            "cached_fields": self.cached_fields,
            "missing_fields": self.missing_fields,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RiskState":
        return cls(
            level=str(data.get("level", "ok")),
            vix=data.get("vix"),
            us_10y=data.get("us_10y"),
            crude_oil=data.get("crude_oil"),
            message=str(data.get("message", "")),
            timestamp=float(data.get("timestamp") or time.time()),
            observed_at={
                str(key): float(value)
                for key, value in (data.get("observed_at") or {}).items()
            },
            source_timestamps={
                str(key): float(value)
                for key, value in (data.get("source_timestamps") or {}).items()
            },
            available_at={
                str(key): float(value)
                for key, value in (data.get("available_at") or {}).items()
            },
            sources={
                str(key): str(value)
                for key, value in (data.get("sources") or {}).items()
            },
            cached_fields=list(data.get("cached_fields") or []),
            missing_fields=list(data.get("missing_fields") or []),
        )


class MacroRiskControl:
    """宏观风险控制器

    阈值配置（可通过 config.json 覆盖）：
    - vix_critical: VIX 触发梯度爆炸警报阈值 (默认 17.5)
    - us_10y_block: 美债收益率触发估值天花板阈值 (默认 4.55)
    - crude_oil_block: 原油价格触发约束阈值 (默认 88.5)
    """

    _FETCH_METADATA: Dict[str, Dict[str, Any]] = {}
    _FETCH_METADATA_LOCK = threading.Lock()

    def __init__(
        self,
        logger_: Optional[logging.Logger] = None,
        *,
        vix_critical: float = 17.5,
        us_10y_block: float = 4.55,
        crude_oil_block: float = 88.5,
    ):
        self.logger = logger_ or logger
        self.vix_critical = float(vix_critical)
        self.us_10y_block = float(us_10y_block)
        self.crude_oil_block = float(crude_oil_block)

        # 当前状态缓存（避免重复告警）
        persisted = self.load_state_from_file()
        self._state: Optional[RiskState] = (
            RiskState.from_dict(persisted) if persisted is not None else None
        )
        self._last_alert_time: float = 0.0
        self._last_alert_fingerprint: Optional[str] = None
        self._alert_cooldown_sec: float = 3600.0  # 同一风险等级最小告警间隔 1 小时
        self._observation_ttl_sec = float(
            os.environ.get("RADAR_RISK_OBSERVATION_TTL_SEC", "900")
        )
        self._fetch_interval_sec = float(
            os.environ.get("RADAR_RISK_FETCH_INTERVAL_SEC", "300")
        )
        self._last_fetch_attempt = 0.0

    # ------------------------------------------------------------------ 核心判断
    def check(
        self,
        vix: Optional[float] = None,
        us_10y: Optional[float] = None,
        crude_oil: Optional[float] = None,
        observation_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
        decision_timestamp: Optional[float] = None,
    ) -> RiskState:
        """执行风险检查，返回当前风险状态。

        ``decision_timestamp`` 是历史回放的可见性边界；实时调用留空。源行情、
        可用时间以及既有缓存都不得晚于这个边界。
        """

        wall_now = time.time()
        now = float(decision_timestamp) if decision_timestamp is not None else wall_now
        if not math.isfinite(now) or now <= 0 or now > wall_now + 5:
            raise ValueError("decision_timestamp 非法或晚于当前时间")
        values = {"vix": vix, "us_10y": us_10y, "crude_oil": crude_oil}
        observed_at = dict(self._state.observed_at) if self._state else {}
        source_timestamps = dict(self._state.source_timestamps) if self._state else {}
        available_at = dict(self._state.available_at) if self._state else {}
        sources = dict(self._state.sources) if self._state else {}
        observation_metadata = observation_metadata or {}
        cached_fields: List[str] = []
        missing_fields: List[str] = []
        for name, value in values.items():
            if value is not None:
                item_metadata = observation_metadata.get(name, {})
                item_available = float(item_metadata.get("available_at") or now)
                item_source_time = float(
                    item_metadata.get("source_timestamp") or item_available
                )
                # A future-dated provider quote is not data that was visible at
                # the decision point.  Reject it instead of letting clock or
                # daily-bar errors introduce look-ahead bias.
                if item_available > now + 5 or item_source_time > now + 300:
                    values[name] = None
                    missing_fields.append(name)
                    continue
                observed_at[name] = now
                source_timestamps[name] = item_source_time
                available_at[name] = item_available
                sources[name] = str(item_metadata.get("source") or "direct")[:64]
                continue
            previous = getattr(self._state, name, None) if self._state else None
            observed = observed_at.get(name, 0.0)
            age = now - observed
            if previous is not None and 0 <= age <= self._observation_ttl_sec:
                values[name] = previous
                cached_fields.append(name)
            else:
                missing_fields.append(name)
        vix, us_10y, crude_oil = (
            values["vix"], values["us_10y"], values["crude_oil"]
        )
        metadata = {
            "observed_at": observed_at,
            "source_timestamps": source_timestamps,
            "available_at": available_at,
            "sources": sources,
            "cached_fields": cached_fields,
            "missing_fields": missing_fields,
        }

        # 1. 触发梯度爆炸警报（VIX 突变）
        if vix is not None and vix > self.vix_critical:
            state = RiskState(
                level="critical",
                vix=vix,
                us_10y=us_10y,
                crude_oil=crude_oil,
                message=(
                    f"CRITICAL_WARNING: 波动率低位反弹，非线性风险来袭\n"
                    f"VIX={vix:.2f} > 阈值 {self.vix_critical}\n"
                    f"建议：降低整体仓位上限（TTL限制），暂停新增买入"
                ),
                timestamp=now,
                **metadata,
            )
            self._maybe_alert(state)
            self._state = state
            return state

        # 1.5 数据全部缺失：不得伪装成 SYSTEM_OK。
        if vix is None and us_10y is None and crude_oil is None:
            state = RiskState(
                level="warning",
                message=(
                    "DATA_UNAVAILABLE: VIX/美债10Y/原油均未获取到，"
                    "风控处于盲区，请检查外网代理连通性"
                ),
                timestamp=now,
                **metadata,
            )
            self._state = state
            return state

        # 2. 触发估值天花板拦截（美债压测 + 原油约束）
        us_10y_triggered = us_10y is not None and us_10y >= self.us_10y_block
        oil_triggered = crude_oil is not None and crude_oil > self.crude_oil_block

        if us_10y_triggered or oil_triggered:
            triggers = []
            if us_10y_triggered:
                triggers.append(f"美债10Y={us_10y:.2f}% >= {self.us_10y_block}%")
            if oil_triggered:
                triggers.append(f"原油={crude_oil:.2f} > {self.crude_oil_block}")

            state = RiskState(
                level="growth_blocked",
                vix=vix,
                us_10y=us_10y,
                crude_oil=crude_oil,
                message=(
                    f"GROWTH_BLOCK: 核心约束收紧\n"
                    f"触发条件: {' | '.join(triggers)}\n"
                    f"建议：无条件挂起（Suspend）所有高估值成长股买入指令，"
                    f"资金向红利/低波动核心资产（沪深300稳定器）收敛"
                ),
                timestamp=now,
                **metadata,
            )
            self._maybe_alert(state)
            self._state = state
            return state

        # 3. 没有触发阈值，但数据不完整时只能判定为数据告警。
        if missing_fields:
            state = RiskState(
                level="warning",
                vix=vix,
                us_10y=us_10y,
                crude_oil=crude_oil,
                message="DATA_PARTIAL: 宏观指标不完整，暂未发现已知阈值超标",
                timestamp=now,
                **metadata,
            )
            self._state = state
            return state

        # 4. 系统平稳运行
        state = RiskState(
            level="ok",
            vix=vix,
            us_10y=us_10y,
            crude_oil=crude_oil,
            message="SYSTEM_OK: 宏观环境平稳，维持现有防御型配置",
            timestamp=now,
            **metadata,
        )
        # OK 状态只在从非OK恢复时通知一次
        if self._state is not None and self._state.level != "ok":
            self._maybe_alert(state)
        self._state = state
        return state

    # ------------------------------------------------------------------ 策略拦截
    def filter_signals(
        self,
        results: List,
        target: Optional[Any] = None,
    ) -> List:
        """根据当前风险状态过滤策略信号

        - critical: 拦截所有新增买入信号（price/ml/rl）
        - growth_blocked: 拦截高估值成长股买入，保留红利/低波动标的
        - ok: 放行所有信号
        """
        if self._state is None or self._state.level == "ok":
            return results

        if self._state.level == "critical":
            # 拦截所有触发信号
            filtered = []
            for r in results:
                if getattr(r, "triggered", False):
                    self.logger.warning(
                        "[RiskControl] 拦截 %s 信号（VIX 危机模式）: %s",
                        getattr(r, "strategy_type", "?"),
                        getattr(target, "code", "?"),
                    )
                    # 将 triggered 改为 False，保留 info 供记录
                    try:
                        r.triggered = False
                        r.signal_info["risk_blocked"] = self._state.message
                    except Exception:
                        pass
                filtered.append(r)
            return filtered

        if self._state.level == "growth_blocked":
            # 拦截高估值成长股，保留红利/低波动
            # 通过 strategy_tag 或行业分类判断（简化：默认拦截 ml/rl，保留 price）
            filtered = []
            for r in results:
                stype = getattr(r, "strategy_type", "")
                if getattr(r, "triggered", False) and stype in ("ml", "rl"):
                    self.logger.warning(
                        "[RiskControl] 拦截 %s 信号（估值天花板）: %s",
                        stype,
                        getattr(target, "code", "?"),
                    )
                    try:
                        r.triggered = False
                        r.signal_info["risk_blocked"] = self._state.message
                    except Exception:
                        pass
                filtered.append(r)
            return filtered

        return results

    # ------------------------------------------------------------------ 仓位建议
    def position_limit(self, base_position_pct: float = 1.0) -> float:
        """返回当前允许的最大仓位比例（0.0-1.0）"""
        if self._state is None:
            return base_position_pct
        if self._state.level == "critical":
            return 0.3  # 危机模式：最高30%仓位
        if self._state.level == "growth_blocked":
            return 0.6  # 约束模式：最高60%仓位
        return base_position_pct

    # ------------------------------------------------------------------ 告警
    def _maybe_alert(self, state: RiskState) -> None:
        """记录状态变化；持续风险不重复写 WARNING，真实通知由调用方控制。"""
        previous_level = self._state.level if self._state is not None else None
        log = self.logger.info if previous_level == state.level else self.logger.warning
        log("[RiskControl] %s", state.message)

    @staticmethod
    def alert_fingerprint(state: RiskState) -> str:
        """Return a stable identity for deduplicating persistent risk alerts.

        使用风险等级作为指纹（忽略 VIX 具体数值的微小波动），
        避免同一风险状态下因数值小幅变化导致重复告警。
        """
        return f"level:{state.level}"

    def should_alert_now(self, state: Optional[RiskState] = None) -> bool:
        """Return True only for a new/changed risk alert after cooldown.

        Persistent risk states (for example a manual GROWTH_BLOCK held all day)
        should not page Telegram every scan cycle.  Re-alert only when the risk
        fingerprint changes, and still respect a short cooldown for rapid flips.
        """
        if state is None:
            return time.time() - self._last_alert_time >= self._alert_cooldown_sec

        fingerprint = self.alert_fingerprint(state)
        if fingerprint == self._last_alert_fingerprint:
            return False
        return time.time() - self._last_alert_time >= self._alert_cooldown_sec

    def mark_alert_sent(self, state: Optional[RiskState] = None) -> None:
        """外部通知成功后更新冷却时间和已发送风险指纹。"""
        self._last_alert_time = time.time()
        if state is not None:
            self._last_alert_fingerprint = self.alert_fingerprint(state)

    # ------------------------------------------------------------------ 数据获取（Yahoo Finance）
    @classmethod
    def _remember_fetch_metadata(
        cls,
        symbol: str,
        *,
        source_timestamp: float,
        source: str,
        available_at: Optional[float] = None,
    ) -> None:
        with cls._FETCH_METADATA_LOCK:
            cls._FETCH_METADATA[symbol] = {
                "source_timestamp": float(source_timestamp),
                "available_at": float(available_at or time.time()),
                "source": str(source),
                "symbol": symbol,
            }

    @classmethod
    def _fetch_yahoo_api(cls, symbol: str) -> Optional[float]:
        """通过 Yahoo Finance API 直接获取最新价格（带代理支持）"""
        import json
        import urllib.request

        # 容器环境只注入 RADAR_* 代理变量（避免污染国内源），此处一并识别
        proxy = (
            os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("RADAR_HTTP_PROXY")
            or os.environ.get("RADAR_HTTPS_PROXY")
            or os.environ.get("RADAR_ALL_PROXY")
        )
        opener = None
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )

        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?interval=1d&range=1d"
        )
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        try:
            if opener:
                with opener.open(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            else:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            result = data["chart"]["result"][0]
            price = result["meta"]["regularMarketPrice"]
            if price is not None:
                source_timestamp = float(
                    result.get("meta", {}).get("regularMarketTime") or time.time()
                )
                cls._remember_fetch_metadata(
                    symbol,
                    source_timestamp=source_timestamp,
                    source="yahoo_chart_regular_market",
                )
                return float(price)
            # fallback to last close
            closes = result["indicators"]["quote"][0]["close"]
            timestamps = result.get("timestamp") or []
            for reverse_index, c in enumerate(reversed(closes)):
                if c is not None:
                    source_timestamp = (
                        float(timestamps[-(reverse_index + 1)])
                        if reverse_index < len(timestamps)
                        else time.time()
                    )
                    cls._remember_fetch_metadata(
                        symbol,
                        source_timestamp=source_timestamp,
                        source="yahoo_chart_daily_close",
                    )
                    return float(c)
        except Exception as exc:
            logger.warning(
                "[RiskControl] fetch failed provider=yahoo symbol=%s reason=%s",
                symbol,
                type(exc).__name__,
            )
        return None

    @classmethod
    def _fetch_fred_api(cls, symbol: str, series_id: str) -> Optional[float]:
        """Fetch a recent official daily observation from FRED without a proxy.

        Yahoo remains the primary near-real-time source.  FRED is deliberately a
        daily, fail-closed fallback for proxy outages; observations older than
        ``RADAR_RISK_FRED_MAX_AGE_SEC`` are rejected instead of being presented
        as current market data.
        """
        import csv
        import datetime as dt
        import io
        import urllib.parse

        now = time.time()
        try:
            max_age = float(os.environ.get("RADAR_RISK_FRED_MAX_AGE_SEC", str(5 * 86400)))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(max_age) or max_age <= 0:
            return None

        lookback_days = max(14, int(math.ceil(max_age / 86400)) + 7)
        start_date = dt.datetime.fromtimestamp(now - lookback_days * 86400, tz=dt.timezone.utc).date()
        query = urllib.parse.urlencode({"id": series_id, "cosd": start_date.isoformat()})
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?{query}"

        try:
            body = cls._request_direct_bytes(
                url,
                allowed_host="fred.stlouisfed.org",
                allowed_path="/graph/fredgraph.csv",
                accepted_content_types=("application/csv", "text/csv"),
            )
            if body is None:
                return None
            text = body.decode("utf-8")

            latest: tuple[float, float] | None = None
            for row in csv.DictReader(io.StringIO(text)):
                date_text = str(row.get("observation_date") or row.get("DATE") or "").strip()
                value_text = str(row.get(series_id) or "").strip()
                if not date_text or not value_text or value_text == ".":
                    continue
                value = float(value_text)
                upper_bound = {
                    "VIXCLS": 200.0,
                    "DGS10": 25.0,
                    "DCOILWTICO": 500.0,
                }.get(series_id, 1_000_000.0)
                if not math.isfinite(value) or value <= 0 or value >= upper_bound:
                    continue
                source_date = dt.datetime.strptime(date_text, "%Y-%m-%d").date()
                source_timestamp = dt.datetime.combine(source_date, dt.time.min, tzinfo=dt.timezone.utc).timestamp()
                if source_timestamp > now + 300:
                    continue
                if latest is None or source_timestamp > latest[0]:
                    latest = (source_timestamp, value)

            if latest is None or now - latest[0] > max_age:
                return None
            source_timestamp, value = latest
            cls._remember_fetch_metadata(
                symbol,
                source_timestamp=source_timestamp,
                available_at=now,
                source=(
                    "fred_dcoilwtico_spot_daily" if series_id == "DCOILWTICO" else f"fred_{series_id.lower()}_daily"
                ),
            )
            return value
        except Exception as exc:
            logger.warning(
                "[RiskControl] fetch failed provider=fred series=%s reason=%s",
                series_id,
                type(exc).__name__,
            )
            return None

    @staticmethod
    def _request_direct_bytes(
        url: str,
        *,
        allowed_host: str,
        allowed_path: str,
        accepted_content_types: tuple[str, ...],
        timeout: int = 8,
        max_bytes: int = 65536,
    ) -> bytes | None:
        """Read a small fixed-host HTTPS response outside the failed proxy path."""
        import urllib.parse
        import urllib.request

        class _RejectRedirects(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None

        initial = urllib.parse.urlparse(url)
        if (
            initial.scheme.lower() != "https"
            or (initial.hostname or "").lower() != allowed_host
            or initial.path != allowed_path
        ):
            return None

        request = urllib.request.Request(
            url,
            headers={
                "Accept": ", ".join(accepted_content_types),
                "Accept-Encoding": "identity",
                "User-Agent": "TradingRadar-MacroRisk/1.0",
            },
        )
        try:
            # The fallback must remain independent from the Yahoo proxy path.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())
            with opener.open(request, timeout=timeout) as response:
                if int(getattr(response, "status", 200)) != 200:
                    return None
                final = urllib.parse.urlparse(response.geturl())
                if (
                    final.scheme.lower() != "https"
                    or (final.hostname or "").lower() != allowed_host
                    or final.path != allowed_path
                ):
                    return None
                content_type = str(response.headers.get("Content-Type") or "").lower()
                if not any(item in content_type for item in accepted_content_types):
                    return None
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > max_bytes:
                    return None
                body = response.read(max_bytes + 1)
                return body if len(body) <= max_bytes else None
        except Exception as exc:
            logger.warning(
                "[RiskControl] direct fetch failed host=%s path=%s reason=%s",
                allowed_host,
                allowed_path,
                type(exc).__name__,
            )
            return None

    @classmethod
    def _fetch_cboe_vix(cls) -> Optional[float]:
        """Fetch Cboe's delayed VIX quote as an independent direct fallback."""
        import datetime as dt
        import json

        now = time.time()
        body = cls._request_direct_bytes(
            "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json",
            allowed_host="cdn.cboe.com",
            allowed_path="/api/global/delayed_quotes/quotes/_VIX.json",
            accepted_content_types=("application/json",),
        )
        if body is None:
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
            data = payload["data"]
            value = float(data["current_price"])
            source_timestamp = (
                dt.datetime.strptime(str(payload["timestamp"]), "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=dt.timezone.utc)
                .timestamp()
            )
            max_age = float(os.environ.get("RADAR_RISK_CBOE_MAX_AGE_SEC", str(4 * 86400)))
            age = now - source_timestamp
            if (
                not math.isfinite(value)
                or not 0 < value < 200
                or not math.isfinite(max_age)
                or max_age <= 0
                or age < -300
                or age > max_age
            ):
                return None
            cls._remember_fetch_metadata(
                "^VIX",
                source_timestamp=source_timestamp,
                available_at=now,
                source="cboe_vix_delayed",
            )
            return value
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "[RiskControl] fetch failed provider=cboe symbol=^VIX reason=%s",
                type(exc).__name__,
            )
            return None

    @classmethod
    def _fetch_yfinance_daily_close(cls, symbol: str) -> Optional[float]:
        """Last-resort daily close via yfinance; kept from the host branch."""
        try:
            import yfinance as yf

            data = yf.Ticker(symbol).history(period="1d")
            if not data.empty:
                cls._remember_fetch_metadata(
                    symbol,
                    source_timestamp=float(data.index[-1].timestamp()),
                    source="yfinance_daily_close",
                )
                return float(data["Close"].iloc[-1])
        except Exception:
            pass
        return None

    @classmethod
    def fetch_vix(cls) -> Optional[float]:
        """获取 VIX 指数"""
        # Yahoo is near-real-time; Cboe/FRED are independent official fallbacks.
        price = cls._fetch_yahoo_api("^VIX")
        if price is not None:
            return price
        price = cls._fetch_cboe_vix()
        if price is not None:
            return price
        price = cls._fetch_fred_api("^VIX", "VIXCLS")
        if price is not None:
            return price
        return cls._fetch_yfinance_daily_close("^VIX")

    @classmethod
    def fetch_us_10y(cls) -> Optional[float]:
        """获取美国10年期国债收益率（^TNX）"""
        price = cls._fetch_yahoo_api("^TNX")
        if price is not None:
            return price
        price = cls._fetch_fred_api("^TNX", "DGS10")
        if price is not None:
            return price
        return cls._fetch_yfinance_daily_close("^TNX")

    @classmethod
    def fetch_crude_oil(cls) -> Optional[float]:
        """获取 WTI 原油价格；Yahoo期货失败时使用有明确来源标识的官方现货日值。"""
        price = cls._fetch_yahoo_api("CL=F")
        if price is not None:
            return price
        allow_spot = os.environ.get("RADAR_RISK_ALLOW_WTI_SPOT_FALLBACK", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if allow_spot:
            price = cls._fetch_fred_api("CL=F", "DCOILWTICO")
            if price is not None:
                return price
        else:
            logger.warning("[RiskControl] WTI spot fallback disabled after CL=F failure")
        return cls._fetch_yfinance_daily_close("CL=F")

    def check_with_fetch(self) -> RiskState:
        """自动获取数据并检查（完整流程）。"""
        override = self.load_override_state()
        if override is not None:
            self._state = override
            return override

        # 宏观指标是低频数据，禁止随每轮行情扫描（约 60 秒）重复请求三次 Yahoo。
        # 失败时同样短暂缓存 warning 状态，避免故障代理/429 形成请求风暴。
        now = time.time()
        if self._state is not None and now - self._last_fetch_attempt < self._fetch_interval_sec:
            return self._state
        self._last_fetch_attempt = now

        fetch_started = time.time()
        values = {
            "vix": self.fetch_vix(),
            "us_10y": self.fetch_us_10y(),
            "crude_oil": self.fetch_crude_oil(),
        }
        symbol_by_field = {"vix": "^VIX", "us_10y": "^TNX", "crude_oil": "CL=F"}
        with self._FETCH_METADATA_LOCK:
            metadata = {
                field: dict(self._FETCH_METADATA[symbol])
                for field, symbol in symbol_by_field.items()
                if symbol in self._FETCH_METADATA
                and float(self._FETCH_METADATA[symbol].get("available_at") or 0)
                >= fetch_started - 1
            }
        return self.check(**values, observation_metadata=metadata)

    # ------------------------------------------------------------------ 当日风控覆盖
    # 路径可用环境变量覆盖：容器化部署时指向共享卷（如 /app/data/...），
    # 使 monitor 写入的状态能被 dashboard 容器读取。
    _OVERRIDE_FILE = Path(
        os.environ.get("RADAR_RISK_OVERRIDE_FILE", "/tmp/radar_risk_override.json")
    )

    @classmethod
    def load_override_state(cls) -> Optional[RiskState]:
        """读取人工/盘前风控覆盖；过期后自动失效。"""
        try:
            if not cls._OVERRIDE_FILE.exists():
                return None
            data = json.loads(cls._OVERRIDE_FILE.read_text(encoding="utf-8"))
            expires_at = float(data.get("expires_at") or 0)
            if expires_at and time.time() > expires_at:
                return None
            state = data.get("state") if isinstance(data.get("state"), dict) else data
            return RiskState.from_dict(state)
        except Exception:
            return None

    # ------------------------------------------------------------------ 状态持久化
    _STATE_FILE = Path(
        os.environ.get("RADAR_RISK_STATE_FILE", "/tmp/radar_risk_state.json")
    )
    _STATE_LOCK = threading.Lock()

    def save_state(self, state: RiskState) -> None:
        """原子发布共享风控状态，避免跨进程读到半截 JSON。"""
        temp_path = self._STATE_FILE.with_name(
            f".{self._STATE_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            with self._STATE_LOCK:
                self._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                payload = json.dumps(state.to_dict(), ensure_ascii=False, default=str)
                with open(temp_path, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self._STATE_FILE)
        except Exception as e:
            self.logger.debug("[RiskControl] 保存状态失败: %s", e)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except (OSError, TypeError):
                # Python 3.7 compatibility for Path.unlink(missing_ok=...).
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass

    @classmethod
    def load_state_from_file(cls) -> Optional[Dict]:
        """从共享文件读取风控状态（Dashboard/策略引擎调用）。"""
        override = cls.load_override_state()
        if override is not None:
            return override.to_dict()

        try:
            if cls._STATE_FILE.exists():
                with cls._STATE_LOCK:
                    data = json.loads(cls._STATE_FILE.read_text(encoding="utf-8"))
                # 验证数据新鲜度（5分钟）
                if time.time() - data.get("timestamp", 0) < 300:
                    return data
        except Exception:
            pass
        return None
