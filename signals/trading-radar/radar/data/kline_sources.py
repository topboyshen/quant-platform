"""历史 K 线数据源（东财 push2his / 腾讯 / AkShare 兜底）。

从 ``market_data_v2.MarketDataFetcher`` 拆出的纯函数集合：
负责单一数据源的拉取与归一化，由 fetcher 按降级链顺序调用。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import time
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

import pandas as pd
import requests

_SOURCE_NAMES = frozenset({
    "eastmoney_push2his", "tencent_kline", "akshare_kline", "parquet",
    "akshare_fund_etf_hist_em", "akshare_fund_etf_hist_sina", "akshare_stock_zh_a_hist",
})
_ROUTES = frozenset({"adapter", "session", "urllib", "direct", "proxy", "cache"})
_STATES = frozenset({"ready", "stale", "empty", "invalid", "error"})
_REJECTIONS = frozenset({
    "ready", "empty_payload", "http_error", "transport_error", "parse_error",
    "stale_date", "date_mismatch", "insufficient_rows", "invalid_frame",
    "normalization_rejected", "unavailable", "unknown",
})
_ERROR_NAMES = frozenset({
    "HTTPError", "URLError", "Timeout", "ReadTimeout", "ConnectTimeout",
    "ConnectionError", "ProxyError", "RemoteDisconnected", "TimeoutError",
    "JSONDecodeError", "ValueError", "TypeError", "KeyError", "OSError",
})
_ATTEMPT_LIMIT = 32
_ATTEMPT_SCHEMA = "quant.kline-source-attempts.v1"
_attempt_context: ContextVar = ContextVar("kline_source_attempts", default=None)


def _safe_day(value: Any) -> Optional[str]:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return value if datetime.date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def source_attempts_snapshot(value: Any, *, code: str, required_date: Optional[str]) -> Optional[dict]:
    """Allowlisted metadata only; never freeze URLs, exception text or prices."""
    if not isinstance(value, dict) or value.get("schema_version") != _ATTEMPT_SCHEMA:
        return None
    if value.get("code") != code or value.get("required_date") != required_date:
        return None
    if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdecimal():
        return None
    if _safe_day(required_date) is None or not isinstance(value.get("attempts"), list):
        return None
    attempts = []
    for item in value["attempts"][:_ATTEMPT_LIMIT]:
        if not isinstance(item, dict):
            continue
        source, route = item.get("source"), item.get("route")
        if not isinstance(source, str) or not isinstance(route, str) or source not in _SOURCE_NAMES or route not in _ROUTES:
            continue
        def bounded_int(raw, low, high):
            return raw if type(raw) is int and low <= raw <= high else None
        state, error_type, rejection = item.get("state"), item.get("error_type"), item.get("rejection")
        attempts.append({
            "source": source, "route": route,
            "state": state if isinstance(state, str) and state in _STATES else "invalid",
            "http_status": bounded_int(item.get("http_status"), 100, 599),
            "error_type": error_type if isinstance(error_type, str) and error_type in _ERROR_NAMES else ("OtherError" if error_type else ""),
            "elapsed_ms": bounded_int(item.get("elapsed_ms"), 0, 3_600_000),
            "rows": bounded_int(item.get("rows"), 0, 10_000_000),
            "latest_date": _safe_day(item.get("latest_date")),
            "rejection": rejection if isinstance(rejection, str) and rejection in _REJECTIONS else "unknown",
        })
    raw_dropped = value.get("dropped")
    dropped = max(0, min(raw_dropped, 1_000_000)) if type(raw_dropped) is int else 0
    dropped += max(0, len(value["attempts"]) - _ATTEMPT_LIMIT)
    dropped += len(value["attempts"][:_ATTEMPT_LIMIT]) - len(attempts)
    return {"schema_version": _ATTEMPT_SCHEMA, "code": code,
            "required_date": required_date, "attempts": attempts,
            "dropped": dropped, "route_semantics": "application_phase_not_network_attestation",
            "response_origin": "unknown"}


@contextmanager
def capture_source_attempts(code: str, required_date: str) -> Iterator[dict]:
    """Request-local evidence: no shared fetcher state or legacy API changes."""
    evidence = {"schema_version": _ATTEMPT_SCHEMA, "code": code,
                "required_date": required_date, "attempts": [], "dropped": 0}
    token = _attempt_context.set(evidence)
    try:
        yield evidence
    finally:
        _attempt_context.reset(token)


def record_source_attempt(source: str, route: str, frame=None, *, days: int = 0,
                          error=None, http_status=None, rejection=None, started=None) -> None:
    """Best-effort observer: this function cannot change a fetch decision."""
    evidence = _attempt_context.get()
    if evidence is None:
        return
    try:
        if len(evidence["attempts"]) >= _ATTEMPT_LIMIT:
            evidence["dropped"] += 1
            return
        error_name = type(error).__name__ if error is not None else ""
        if http_status is None and error is not None:
            http_status = getattr(error, "code", None)
            if not isinstance(http_status, int):
                http_status = getattr(getattr(error, "response", None), "status_code", None)
        observation = kline_observation(source, route, frame, days, evidence["required_date"],
                                        error_type=error_name)
        if type(http_status) is int and http_status >= 400:
            observation["state"] = "error"
        if rejection is None:
            rejection = {"ready": "ready", "stale": "stale_date", "empty": "unknown"}.get(observation["state"], "invalid_frame")
            if error is not None:
                rejection = "http_error" if isinstance(http_status, int) else "transport_error"
            elif observation["state"] == "invalid" and observation["rows"] < max(30, min(max(int(days), 1) // 2, 120)):
                rejection = "insufficient_rows"
            elif observation["state"] == "invalid" and observation["latest_date"]:
                rejection = "date_mismatch"
        elapsed = None if started is None else int(max(0, time.monotonic() - started) * 1000)
        item = {"source": source, "route": route, "state": observation["state"],
                "http_status": http_status, "error_type": error_name, "elapsed_ms": elapsed,
                "rows": observation["rows"], "latest_date": observation["latest_date"],
                "rejection": rejection}
        safe = source_attempts_snapshot({**evidence, "attempts": [item], "dropped": 0},
                                        code=evidence["code"], required_date=evidence["required_date"])
        if safe is not None and safe["attempts"]:
            evidence["attempts"].append(safe["attempts"][0])
    except Exception:
        # Diagnostics must never alter source order, retries or usable prices.
        pass


def resolve_secid(code: str) -> str:
    """将 6 位证券代码解析为东财 push2 接口的 secid（市场前缀 + 代码）。

    ``1.`` = 上交所，``0.`` = 深交所。

    注意：旧逻辑 ``"1." if startswith("6") else "0."`` 只对个股成立，会把
    上交所 ETF（``510300`` 等 5 字头）、可转债（``11`` 字头）误判为深市，导致
    K 线接口取不到数据。ETF 是波段化模块的核心标的，必须按市场归属正确解析。
    """
    full = str(code).strip().zfill(6)
    # 上交所：股票 6 字头、基金/ETF/LOF 5 字头、可转债 11 字头
    if full[0] in ("5", "6") or full.startswith("11"):
        return f"1.{full}"
    # 其余（0/1/2/3 字头个股、15x/16x 深市基金、12x 深市可转债）归深交所
    return f"0.{full}"


@contextmanager
def temporary_standard_proxy_from_radar() -> Iterator[None]:
    """Expose RADAR_* proxy vars to libraries that only read standard proxy envs."""
    radar_all_proxy = os.getenv("RADAR_ALL_PROXY")
    radar_http_proxy = os.getenv("RADAR_HTTP_PROXY") or radar_all_proxy
    radar_https_proxy = os.getenv("RADAR_HTTPS_PROXY") or radar_all_proxy
    proxy_values = {
        "HTTP_PROXY": radar_http_proxy,
        "http_proxy": radar_http_proxy,
        "HTTPS_PROXY": radar_https_proxy,
        "https_proxy": radar_https_proxy,
        "ALL_PROXY": radar_all_proxy,
        "all_proxy": radar_all_proxy,
    }
    originals = {key: os.environ.get(key) for key in proxy_values}
    try:
        for key, value in proxy_values.items():
            if value:
                os.environ[key] = value
        yield
    finally:
        for key, value in originals.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def temporary_without_standard_proxy() -> Iterator[None]:
    """让国内行情源直连，避免故障的境外代理拖垮所有 AkShare 兜底。"""
    keys = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
    originals = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in originals.items():
            if value is not None:
                os.environ[key] = value


def is_etf_like_code(code: str) -> bool:
    full = str(code).strip().zfill(6)
    return full.startswith(("51", "56", "58", "15", "16"))


def exchange_prefixed_code(code: str) -> str:
    """Return the sh/sz symbol format used by Tencent and Sina endpoints."""
    full = str(code).strip().zfill(6)
    market = "sh" if resolve_secid(full).startswith("1.") else "sz"
    return f"{market}{full}"


def _klines_to_frame(klines: list) -> Optional[pd.DataFrame]:
    """东财 push2his 返回的 "日期,开,收,高,低,量,..." 行转 DataFrame。"""
    rows = []
    for line in klines:
        parts = line.split(",")
        rows.append({
            "date": parts[0],
            "open": float(parts[1]),
            "close": float(parts[2]),
            "high": float(parts[3]),
            "low": float(parts[4]),
            "volume": int(float(parts[5])),
        })
    df = pd.DataFrame(rows)
    return df if len(df) >= 30 else None


def fetch_eastmoney_kline(
    session: requests.Session,
    logger: logging.Logger,
    code: str,
    days: int,
) -> Optional[pd.DataFrame]:
    """从东方财富 push2his 接口拉取 K 线数据。

    容器内 requests 可能被东财识别拦截，失败时降级到 urllib.request 备用路径。
    """
    full_code = code.zfill(6)
    secid = resolve_secid(full_code)
    url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": "1",
        "end": datetime.datetime.now().strftime("%Y%m%d"),
        "lmt": str(days + 30),
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "http://quote.eastmoney.com/",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }

    # 先尝试 requests（宿主机正常）
    for attempt in range(2):
        started = time.monotonic()
        status = None
        try:
            response = session.get(url, params=params, headers=headers, timeout=30)
            status = response.status_code

            if 500 <= response.status_code < 600:
                record_source_attempt("eastmoney_push2his", "session", http_status=status,
                                      rejection="http_error", started=started)
                if attempt == 0:
                    time.sleep(1)
                    continue
                return None

            response.raise_for_status()
            data = response.json()
            klines = data.get("data", {}).get("klines", [])
            if not klines:
                record_source_attempt("eastmoney_push2his", "session", http_status=status,
                                      rejection="empty_payload", started=started)
                return None
            frame = _klines_to_frame(klines)
            record_source_attempt("eastmoney_push2his", "session", frame, days=days,
                                  http_status=status, rejection="insufficient_rows" if frame is None else None, started=started)
            return frame
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            record_source_attempt("eastmoney_push2his", "session", error=e, started=started)
            if attempt == 0:
                time.sleep(1)
                continue
            # 失败时尝试 urllib.request 备用路径
            break
        except Exception as e:
            record_source_attempt("eastmoney_push2his", "session", error=e, http_status=status,
                                  rejection="parse_error" if isinstance(e, (ValueError, TypeError, KeyError)) else None, started=started)
            logger.error(f"获取历史数据异常 {code}: {e}")
            return None

    # 备用路径：urllib.request（容器内更可靠）
    started = time.monotonic()
    status = None
    try:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        req = urllib.request.Request(f"{url}?{query}", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = getattr(resp, "status", None)
            data = json.loads(resp.read().decode("utf-8"))
        klines = data.get("data", {}).get("klines", [])
        if not klines:
            record_source_attempt("eastmoney_push2his", "urllib", http_status=status,
                                  rejection="empty_payload", started=started)
            return None
        frame = _klines_to_frame(klines)
        record_source_attempt("eastmoney_push2his", "urllib", frame, days=days, http_status=status,
                              rejection="insufficient_rows" if frame is None else None, started=started)
        return frame
    except Exception as e:
        record_source_attempt("eastmoney_push2his", "urllib", error=e, http_status=status,
                              rejection="parse_error" if isinstance(e, (ValueError, TypeError, KeyError)) else None, started=started)
        logger.info(f"urllib 备用路径也失败 {code}: {e}")
        return None


def fetch_tencent_kline(logger: logging.Logger, code: str, days: int) -> Optional[pd.DataFrame]:
    """从腾讯财经接口拉取日K线数据（东财被拦截时的备用源）。

    腾讯接口: http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=[code],day,,,[count],qfq
    返回前复权日K数据，格式稳定，容器内可正常访问。
    """
    full_code = code.zfill(6)
    tencent_code = exchange_prefixed_code(full_code)
    # 多请求 30 天冗余数据
    count = days + 30

    url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tencent_code},day,,,{count},qfq"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "http://stockpage.10jqka.com.cn/",
    }

    started = time.monotonic()
    status = None
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = getattr(resp, "status", None)
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") != 0:
            record_source_attempt("tencent_kline", "urllib", http_status=status,
                                  rejection="normalization_rejected", started=started)
            return None
        entry = data.get("data", {}).get(tencent_code, {})
        # Some ETFs (for example 513180) have no corporate-action adjustment
        # series and Tencent returns ``day`` instead of ``qfqday``. Treating
        # that valid payload as empty caused the entire ETF decision to skip.
        series_name = "qfqday" if entry.get("qfqday") else "day"
        klines = entry.get(series_name, [])
        if not klines:
            record_source_attempt("tencent_kline", "urllib", http_status=status,
                                  rejection="empty_payload", started=started)
            return None

        rows = []
        for item in klines:
            if len(item) >= 6:
                rows.append({
                    "date": item[0],
                    "open": float(item[1]),
                    "close": float(item[2]),
                    "high": float(item[3]),
                    "low": float(item[4]),
                    "volume": int(float(item[5])),
                })
        df = pd.DataFrame(rows)
        if len(df) < 30:
            record_source_attempt("tencent_kline", "urllib", df, days=days, http_status=status,
                                  rejection="insufficient_rows", started=started)
            return None
        record_source_attempt("tencent_kline", "urllib", df, days=days, http_status=status, started=started)
        logger.info("腾讯 K线获取成功: %s rows=%d series=%s", full_code, len(df), series_name)
        return df
    except Exception as e:
        record_source_attempt("tencent_kline", "urllib", error=e, http_status=status,
                              rejection="parse_error" if isinstance(e, (ValueError, TypeError, KeyError)) else None, started=started)
        logger.info("腾讯 K线获取失败 %s: %s", full_code, e)
        return None


def normalize_akshare_kline(
    logger: logging.Logger, code: str, df: pd.DataFrame
) -> Optional[pd.DataFrame]:
    """Normalize AkShare ETF/A-share history columns to the internal OHLCV schema."""
    if df is None or df.empty:
        return None

    column_map = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "date": "date",
        "open": "open",
        "close": "close",
        "high": "high",
        "low": "low",
        "volume": "volume",
    }
    renamed = df.rename(columns={k: v for k, v in column_map.items() if k in df.columns}).copy()
    required = ["date", "open", "close", "high", "low", "volume"]
    if not all(col in renamed.columns for col in required):
        logger.debug("akshare 历史数据字段不完整 %s: %s", code, list(df.columns))
        return None

    out = renamed[required].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    for col in ["open", "close", "high", "low"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0).astype(int)
    out = out.dropna(subset=["date", "open", "close", "high", "low"])
    out = out.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    if len(out) < 30:
        return None
    return out.reset_index(drop=True)


def is_usable_kline(
    df: Optional[pd.DataFrame],
    days: int,
    *,
    now: Optional[pd.Timestamp] = None,
    required_date: Optional[str] = None,
) -> bool:
    """校验 K 线行数与内容日期，不能用“今天写入的旧数据”冒充新缓存。"""
    if df is None or df.empty or "date" not in df.columns:
        return False
    min_rows = max(30, min(max(int(days), 1) // 2, 120))
    if len(df) < min_rows:
        return False
    dates = pd.to_datetime(df["date"], errors="coerce").dropna()
    if dates.empty:
        return False
    if required_date is not None:
        # EOD requires the exact decision day, not the general cache-age SLA.
        required = datetime.date.fromisoformat(required_date)
        if required.isoformat() != required_date:
            raise ValueError("required_date must be canonical YYYY-MM-DD")
        return len(dates) == len(df) and dates.max().date() == required
    today = (now if now is not None else pd.Timestamp.now()).normalize()
    latest = dates.max().normalize()
    try:
        max_age = max(0, int(os.getenv("RADAR_KLINE_MAX_DATA_AGE_DAYS", "4")))
    except ValueError:
        max_age = 4
    age_days = int((today - latest).days)
    return -1 <= age_days <= max_age


def kline_observation(source: str, route: str, frame: Optional[pd.DataFrame],
                      days: int, required_date: Optional[str], *, error_type: str = "") -> dict:
    """Metadata only: rejected prices NEVER escape through this side channel."""
    latest = None
    rows = 0 if frame is None else len(frame)
    state = "error" if error_type else "empty"
    if frame is not None and not frame.empty:
        dates = pd.to_datetime(frame["date"], errors="coerce") if "date" in frame else pd.Series(dtype="datetime64[ns]")
        newest = dates.max()
        latest = None if pd.isna(newest) else newest.date().isoformat()
        state = "invalid"
        if is_usable_kline(frame, days, required_date=required_date):
            state = "ready"
        elif (required_date and latest and latest < required_date
              and is_usable_kline(frame, days, required_date=latest)):
            state = "stale"
    return {"source": source, "route": route, "state": state, "rows": rows,
            "latest_date": latest, "required_date": required_date, "error_type": error_type}


def fetch_akshare_kline(
    logger: logging.Logger,
    akshare_fetcher: Any,
    code: str,
    days: int,
    *,
    required_date: Optional[str] = None,
    observations: Optional[list[dict]] = None,
) -> Optional[pd.DataFrame]:
    """从 AkShare 拉取历史 K 线，作为东财 push2his 失败时的兜底源。"""
    if akshare_fetcher is None or not akshare_fetcher.is_available:
        record_source_attempt("akshare_kline", "adapter", rejection="unavailable")
        return None
    ak = akshare_fetcher._ak
    full_code = code.zfill(6)
    end_date = datetime.datetime.now().strftime("%Y%m%d")
    start_date = (datetime.datetime.now() - datetime.timedelta(days=max(days * 2, days + 120))).strftime("%Y%m%d")

    attempts = []
    if is_etf_like_code(full_code) and hasattr(ak, "fund_etf_hist_em"):
        attempts.append((
            "akshare_fund_etf_hist_em",
            lambda: ak.fund_etf_hist_em(
                symbol=full_code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
            ),
        ))
    if is_etf_like_code(full_code) and hasattr(ak, "fund_etf_hist_sina"):
        # fund_etf_hist_sina expects an exchange-qualified symbol (sh513180),
        # unlike fund_etf_hist_em which expects only the six-digit code.
        sina_symbol = exchange_prefixed_code(full_code)
        attempts.append((
            "akshare_fund_etf_hist_sina",
            lambda: ak.fund_etf_hist_sina(symbol=sina_symbol),
        ))
    if hasattr(ak, "stock_zh_a_hist"):
        attempts.append((
            "akshare_stock_zh_a_hist",
            lambda: ak.stock_zh_a_hist(
                symbol=full_code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
            ),
        ))

    # 国内接口先直连；只有全部直连失败时才尝试 RADAR_* 代理。
    phases = [("direct", temporary_without_standard_proxy)]
    if any(os.getenv(key) for key in ("RADAR_ALL_PROXY", "RADAR_HTTP_PROXY", "RADAR_HTTPS_PROXY")):
        phases.append(("proxy", temporary_standard_proxy_from_radar))
    for phase, connection_context in phases:
        for source, fetch in attempts:
            started = time.monotonic()
            try:
                with connection_context():
                    df = fetch()
                normalized = normalize_akshare_kline(logger, full_code, df)
                record_source_attempt(source, phase, normalized, days=days, started=started,
                                      rejection="empty_payload" if df is None or df.empty else ("normalization_rejected" if normalized is None else None))
                observation = kline_observation(source, phase, normalized, days, required_date)
                if observations is not None:
                    observations.append(observation)
                if required_date is not None:
                    logger.info("kline_observation %s", json.dumps(observation, sort_keys=True))
                if not is_usable_kline(normalized, days, required_date=required_date):
                    if normalized is not None:
                        latest = pd.to_datetime(normalized["date"], errors="coerce").max()
                        logger.info(
                            "%s 返回数据暂不可用: %s rows=%d latest=%s required_date=%s route=%s",
                            source, full_code, len(normalized), latest, required_date, phase,
                        )
                    continue
                logger.info(
                    "%s 获取历史数据成功: %s rows=%d route=%s",
                    source, full_code, len(normalized), phase,
                )
                return normalized.tail(days + 30).reset_index(drop=True)
            except Exception as e:
                record_source_attempt(source, phase, error=e, started=started,
                                      rejection="parse_error" if isinstance(e, (ValueError, TypeError, KeyError)) else None)
                observation = kline_observation(source, phase, None, days, required_date, error_type=type(e).__name__)
                if observations is not None:
                    observations.append(observation)
                if required_date is not None:
                    logger.info("kline_observation %s", json.dumps(observation, sort_keys=True))
                logger.info("%s 获取历史数据失败 %s route=%s: %s", source, full_code, phase, e)
    return None
