"""MacroSnapshotCollector - 收盘后宏观情绪切片采集。

数据源优先级沿用 a-stock-data：腾讯等低封禁风险源优先，东财仅用于
独有数据或末级兜底，并通过串行限流降低出口 IP 被风控的概率。

采集三因子作为「大盘环境过滤器」：
1. 沪深市场每日成交额（亿元）
2. 两融余额增速（杠杆资金情绪）
3. 全市场涨跌停家数比（绝对情绪冰点 / 沸点）

并基于历史分位数判定「情绪冰点 / 沸点」。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
import random
import re
import time as _time
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from radar.swing.store import NUMERIC_FACTORS, MacroSlice, SwingStore

logger = logging.getLogger(__name__)

# 统一用北京时间判断交易日/收盘时点，避免 UTC 容器里日期与时刻判断漂移
_BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def _beijing_now() -> _dt.datetime:
    return _dt.datetime.now(_BEIJING_TZ)

# 因子方向：True 表示「高分位 = 沸点 / 低分位 = 冰点」（成交额、两融增速、涨跌停比皆如此）
_FACTOR_LABELS = {
    "total_amount": "沪深市场成交额",
    "margin_growth_pct": "两融余额增速",
    "limit_ratio": "涨跌停家数比",
}


@dataclass(slots=True)
class MacroCollectStatus:
    """单个宏观因子采集状态，供日志、Dashboard 和 metrics 解释缺失原因。"""

    factor: str
    available: bool
    source: str = ""
    message: str = ""
    observed_date: Optional[str] = None
    stale: bool = False
    anomalous: bool = False


@dataclass(slots=True)
class MacroExtreme:
    """单因子极值判定结果。"""

    factor: str
    label: str
    value: float
    percentile: float       # 0-100，当前值在历史样本中的分位
    level: str              # "冰点" / "沸点" / "正常"
    sample_size: int


class MacroSnapshotCollector:
    """收盘后宏观情绪采集器。"""

    MIN_TOTAL_AMOUNT_SECURITIES = 1000
    SINA_TOTAL_AMOUNT_URL = "https://hq.sinajs.cn/list=sh000001,sz399001"
    SINA_TOTAL_AMOUNT_MAX_BYTES = 16 * 1024
    SINA_TOTAL_AMOUNT_CODES = frozenset({"sh000001", "sz399001"})
    TENCENT_TOTAL_AMOUNT_DATE_EVIDENCE = (
        "qfqday:sh000001+sz399001"
    )

    def __init__(
        self,
        store: SwingStore,
        logger_: Optional[logging.Logger] = None,
        *,
        extreme_low: float = 5.0,
        extreme_high: float = 95.0,
        min_history_days: int = 20,
    ):
        self.store = store
        self.logger = logger_ or logger
        self.extreme_low = float(extreme_low)
        self.extreme_high = float(extreme_high)
        self.min_history_days = int(min_history_days)
        self.collect_status: Dict[str, MacroCollectStatus] = {}
        self._session = None
        self._last_http_request_at = 0.0
        self._collection_date = ""
        self._total_amount_observed_date: Optional[str] = None
        self._total_amount_date_evidence: Optional[str] = None
        self._margin_growth_observed_date: Optional[str] = None
        self._limit_observed_date: Optional[str] = None
        self._limit_source = ""

    def _set_status(
        self,
        factor: str,
        available: bool,
        source: str = "",
        message: str = "",
        *,
        observed_date: Optional[str] = None,
        stale: bool = False,
        anomalous: bool = False,
    ) -> None:
        self.collect_status[factor] = MacroCollectStatus(
            factor,
            available,
            source,
            message,
            observed_date,
            stale,
            anomalous,
        )

    def get_collect_status(self) -> Dict[str, Dict[str, object]]:
        return {
            factor: {
                "available": status.available,
                "source": status.source,
                "message": status.message,
                "observed_date": status.observed_date,
                "stale": status.stale,
                "anomalous": status.anomalous,
            }
            for factor, status in self.collect_status.items()
        }

    @staticmethod
    def is_valid_factor_value(factor: str, value: object) -> bool:
        """Return whether a factor is finite and within a deliberately broad sanity range."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(number):
            return False
        ranges = {
            # 1000--80000 亿元覆盖正常 A 股量级，同时隔离已出现的 8.9/66.5 万亿元脏值。
            "total_amount": (1000.0, 80000.0),
            "margin_growth_pct": (-50.0, 50.0),
            "limit_ratio": (0.0, 1000.0),
        }
        low, high = ranges.get(factor, (-float("inf"), float("inf")))
        return low <= number <= high

    @staticmethod
    def _status_for_slice(slice_: MacroSlice, factor: str) -> Dict[str, object]:
        extra = slice_.extra if isinstance(slice_.extra, dict) else {}
        statuses = extra.get("collect_status", {})
        return statuses.get(factor, {}) if isinstance(statuses, dict) else {}

    @staticmethod
    def _observation_is_stale(
        factor: str,
        observed_date: Optional[str],
        slice_date: Optional[str],
    ) -> bool:
        observed = _normalize_date(observed_date)
        target = _normalize_date(slice_date)
        if observed is None or target is None:
            return True
        age_days = (_dt.date.fromisoformat(target) - _dt.date.fromisoformat(observed)).days
        if factor in {"margin_balance", "margin_growth_pct"}:
            # 两融按交易所发布节奏天然滞后一交易日；周末/短假允许最多 4 个自然日。
            return age_days < 0 or age_days > 4
        return age_days != 0

    @classmethod
    def _factor_is_usable(cls, slice_: MacroSlice, factor: str) -> bool:
        if not cls.is_valid_factor_value(factor, getattr(slice_, factor, None)):
            return False
        status = cls._status_for_slice(slice_, factor)
        return not (
            status.get("available") is False
            or bool(status.get("stale"))
            or bool(status.get("anomalous"))
        )

    def _http_session(self):
        import requests

        if self._session is not None:
            return self._session
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
            }
        )
        session.trust_env = False
        proxy = (os.getenv("RADAR_ALL_PROXY") or "").strip()
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        self._session = session
        return session

    def _get_json_with_retry(self, url: str, params: Dict, *, timeout: int = 8, attempts: int = 3) -> Optional[Dict]:
        session = self._http_session()
        last_error = None
        for attempt in range(attempts):
            try:
                # a-stock-data 的东财防封策略：串行、复用 session、请求间隔>=1s。
                wait = 1.1 + random.uniform(0.0, 0.35) - (
                    _time.monotonic() - self._last_http_request_at
                )
                if wait > 0:
                    _time.sleep(wait)
                resp = session.get(
                    url,
                    params=params,
                    headers={"Referer": "https://quote.eastmoney.com/"},
                    timeout=timeout,
                )
                self._last_http_request_at = _time.monotonic()
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                last_error = exc
                if attempt < attempts - 1:
                    _time.sleep(0.8 * (attempt + 1))
        self.logger.warning("HTTP JSON 请求失败 url=%s params=%s error=%s", url, params, last_error)
        return None

    # ------------------------------------------------------------------ 采集
    def collect(self, date: Optional[str] = None, *, persist: bool = True) -> MacroSlice:
        """采集当日实时切片；实时源禁止伪装成任意历史日期。"""
        today = SwingStore.today_str()
        date = _normalize_date(date or today)
        if date is None:
            raise ValueError("宏观切片日期必须是 YYYY-MM-DD")
        if date != today:
            raise ValueError(
                f"实时宏观数据只能标记为当天 {today}，拒绝写入历史日期 {date}"
            )
        self.collect_status.clear()
        self._collection_date = date
        self._total_amount_observed_date = None
        self._total_amount_date_evidence = None
        self._margin_growth_observed_date = None
        self._limit_observed_date = None
        self._limit_source = ""
        sources: List[str] = []

        total_amount = self._collect_total_amount(sources)
        margin_balance, margin_growth = self._collect_margin(sources)
        limit_up, limit_down = self._collect_limit_pool(sources)

        if not self.is_valid_factor_value("total_amount", total_amount):
            if total_amount is not None:
                self._set_status(
                    "total_amount",
                    False,
                    message=f"成交额越界或非有限值: {total_amount!r}",
                    observed_date=self._total_amount_observed_date,
                    anomalous=True,
                )
            total_amount = None
        if not _is_finite_in_range(margin_balance, minimum=0.0, strict_min=True):
            if margin_balance is not None:
                self._set_status(
                    "margin_balance",
                    False,
                    message=f"两融余额无效: {margin_balance!r}",
                    anomalous=True,
                )
            margin_balance = None
        if not self.is_valid_factor_value("margin_growth_pct", margin_growth):
            if margin_growth is not None:
                self._set_status(
                    "margin_growth_pct",
                    False,
                    message=f"两融增速越界或非有限值: {margin_growth!r}",
                    anomalous=True,
                )
            margin_growth = None
        limit_up = _valid_nonnegative_int(limit_up)
        limit_down = _valid_nonnegative_int(limit_down)

        limit_ratio = None
        if limit_up is not None and limit_down is not None:
            limit_ratio = round(limit_up / (limit_down + 1), 3)
            if self.is_valid_factor_value("limit_ratio", limit_ratio):
                observed_date = self._limit_observed_date or date
                self._set_status(
                    "limit_ratio",
                    True,
                    self._limit_source or "akshare/mx",
                    observed_date=observed_date,
                    stale=self._observation_is_stale("limit_ratio", observed_date, date),
                )
            else:
                self._set_status(
                    "limit_ratio",
                    False,
                    self._limit_source or "akshare/mx",
                    f"涨跌停平滑比无效: {limit_ratio!r}",
                    observed_date=self._limit_observed_date,
                    anomalous=True,
                )
                limit_ratio = None
        else:
            self._set_status(
                "limit_ratio",
                False,
                "akshare/mx",
                "缺少同一交易日的完整涨停/跌停家数，无法计算平滑比",
            )

        slice_ = MacroSlice(
            date=date,
            total_amount=total_amount,
            margin_balance=margin_balance,
            margin_growth_pct=margin_growth,
            limit_up=limit_up,
            limit_down=limit_down,
            limit_ratio=limit_ratio,
            source="+".join(dict.fromkeys(sources)) or "none",
            extra={"collect_status": self.get_collect_status()},
        )

        has_any_factor = any(
            v is not None
            for v in (total_amount, margin_balance, margin_growth, limit_up, limit_down)
        )
        if persist and not has_any_factor:
            # 全因子缺失的切片没有信息量，写库只会污染历史分位样本
            self.logger.warning("宏观切片 %s 所有因子均缺失，跳过持久化", date)
        elif persist:
            try:
                self.store.upsert_partial(slice_)
            except Exception as e:  # 持久化失败不应阻断主流程
                self.logger.warning("宏观切片持久化失败: %s", e)

        self.logger.info(
            "宏观切片 %s | 成交额=%s亿 两融增速=%s%% 涨停=%s 跌停=%s 涨跌停比=%s (来源:%s)",
            slice_.date,
            _fmt(slice_.total_amount),
            _fmt(slice_.margin_growth_pct),
            slice_.limit_up,
            slice_.limit_down,
            _fmt(slice_.limit_ratio),
            slice_.source,
        )
        return slice_

    def _collect_total_amount(self, sources: List[str]) -> Optional[float]:
        """沪深市场成交额（亿元）：腾讯主源，Sina/AkShare/东财依次降级。"""
        errors: List[str] = []
        tencent_total = self._tencent_total_amount()
        if self.is_valid_factor_value("total_amount", tencent_total):
            observed_date = self._total_amount_observed_date
            if observed_date is None:
                errors.append("腾讯成交额缺少真实交易日")
                tencent_total = None
            else:
                sources.append("tencent")
                self._set_status(
                    "total_amount",
                    True,
                    "tencent",
                    observed_date=observed_date,
                    stale=self._observation_is_stale(
                        "total_amount", observed_date, self._collection_date
                    ),
                )
                return round(float(tencent_total), 2)
        if tencent_total is not None:
            errors.append(f"腾讯成交额异常({tencent_total!r})")

        sina_total = self._sina_total_amount()
        if self.is_valid_factor_value("total_amount", sina_total):
            observed_date = self._total_amount_observed_date
            if observed_date is None:
                errors.append("Sina 成交额缺少真实交易日")
                sina_total = None
            else:
                sources.append("sina")
                self._set_status(
                    "total_amount",
                    True,
                    "sina",
                    observed_date=observed_date,
                    stale=self._observation_is_stale(
                        "total_amount", observed_date, self._collection_date
                    ),
                )
                return round(float(sina_total), 2)
        if sina_total is not None:
            errors.append(f"Sina 成交额异常({sina_total!r})")

        ak_error = ""
        try:
            import akshare as ak
        except Exception as e:
            ak_error = f"akshare 不可用: {e}"
            self.logger.warning(ak_error)
        else:
            for attempt in range(2):
                try:
                    df = ak.stock_zh_a_spot_em()
                    if (
                        df is not None
                        and not df.empty
                        and "成交额" in df.columns
                        and "代码" in df.columns
                    ):
                        frame = df.copy()
                        frame["代码"] = frame["代码"].astype(str).str.zfill(6)
                        frame = frame[frame["代码"].str.startswith(("0", "3", "6"))]
                        frame = frame.drop_duplicates(subset=["代码"], keep="first")
                        total_raw = 0.0
                        for raw in frame["成交额"].tolist():
                            if _is_finite_in_range(raw, minimum=0.0):
                                total_raw += float(raw)
                        total = total_raw / 1e8
                        if (
                            len(frame) >= self.MIN_TOTAL_AMOUNT_SECURITIES
                            and self.is_valid_factor_value("total_amount", total)
                        ):
                            sources.append("akshare")
                            self._set_status(
                                "total_amount",
                                True,
                                "akshare",
                                observed_date=self._collection_date,
                            )
                            return round(total, 2)
                        errors.append(f"AkShare 成交额异常({total!r})")
                    ak_error = "akshare A 股快照为空、字段缺失或成交额异常"
                    break
                except Exception as e:
                    ak_error = str(e)
                    if attempt == 0:
                        _time.sleep(1.5)
                        continue
                    self.logger.warning("AkShare 成交额采集失败: %s", e)

        total = self._eastmoney_total_amount()
        if self.is_valid_factor_value("total_amount", total):
            sources.append("eastmoney")
            self._set_status(
                "total_amount",
                True,
                "eastmoney",
                observed_date=self._collection_date,
            )
            return round(float(total), 2)
        if total is not None:
            errors.append(f"东财成交额异常({total!r})")
        message = "; ".join(errors) or ak_error or "成交额所有数据源均失败"
        self._set_status(
            "total_amount",
            False,
            "tencent/sina/akshare/eastmoney",
            message,
            anomalous=bool(errors),
        )
        return None

    def _tencent_fqkline_http_direct(self, code: str) -> dict:
        """Same unproxied HTTP path as ETF K-line; used only after HTTPS 501."""
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,3,qfq"
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload["data"][code]

    def _tencent_fqkline_payload(self, session, code: str) -> dict:
        """HTTPS via the shared session; HTTP direct only after 501 Not Implemented."""
        try:
            response = session.get(
                "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                params={"param": f"{code},day,,,3,qfq"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            response.raise_for_status()
            return response.json()["data"][code]
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status != 501:
                raise
            self.logger.info("腾讯成交额 HTTPS 501，改走直连 HTTP: %s", code)
            return self._tencent_fqkline_http_direct(code)

    def _tencent_total_amount(self) -> Optional[float]:
        """从腾讯上证/深证指数行情汇总两市成交额，单位亿元。

        腾讯 ``qt`` 数组第 37 项为成交额（万元），第 30 项为 14 位报价
        时间。每腿报价日期必须与同腿最后一根 qfqday（若无则 day）日期
        一致，且沪深两腿日期相同；只有完整证明这一点才发布日期证据。
        """
        try:
            self._total_amount_observed_date = None
            self._total_amount_date_evidence = None
            session = self._http_session()
            total_wan = 0.0
            observed_dates: List[str] = []
            for code in ("sh000001", "sz399001"):
                payload = self._tencent_fqkline_payload(session, code)
                quote = payload["qt"][code]
                raw_quote_time = quote[30]
                if (
                    not isinstance(raw_quote_time, str)
                    or re.fullmatch(r"\d{14}", raw_quote_time) is None
                ):
                    self.logger.warning("腾讯%s报价缺少规范 14 位时间", code)
                    return None
                try:
                    quote_time = _dt.datetime.strptime(
                        raw_quote_time, "%Y%m%d%H%M%S"
                    )
                except ValueError:
                    self.logger.warning("腾讯%s报价时间非法", code)
                    return None
                quote_date = quote_time.date().isoformat()
                amount_wan = float(quote[37])
                if not math.isfinite(amount_wan) or amount_wan <= 0:
                    return None
                total_wan += amount_wan
                bars = payload.get("qfqday") or payload.get("day") or []
                raw_observed = bars[-1][0] if bars and bars[-1] else None
                observed = _normalize_date(raw_observed)
                if (
                    observed is None
                    or not isinstance(raw_observed, str)
                    or raw_observed != observed
                ):
                    self.logger.warning("腾讯%s成交额缺少规范交易日", code)
                    return None
                if quote_date != observed:
                    self.logger.warning(
                        "腾讯%s报价日期与K线日期不一致 quote=%s bar=%s",
                        code,
                        quote_date,
                        observed,
                    )
                    return None
                observed_dates.append(observed)
            if len(observed_dates) != 2 or observed_dates[0] != observed_dates[1]:
                self.logger.warning("腾讯沪深成交额交易日不一致: %s", observed_dates)
                return None
            self._total_amount_observed_date = observed_dates[0]
            self._total_amount_date_evidence = (
                self.TENCENT_TOTAL_AMOUNT_DATE_EVIDENCE
            )
            return round(total_wan / 10000, 2)
        except Exception as exc:
            self.logger.warning("腾讯沪深成交额采集失败: %s", exc)
            return None

    def _sina_total_amount(self) -> Optional[float]:
        """从 Sina HTTPS 全量指数报价汇总沪深成交额，返回亿元。

        Sina 全量报价第 10 个字段是当日累计成交额，单位为人民币元；
        第 31 个字段是 ``YYYY-MM-DD`` 交易日。仅在沪深两个固定指数都
        存在、日期规范且与本轮采集日一致时才接受，避免混日、缺腿或
        单位误读进入历史分位。
        """
        response = None
        try:
            self._total_amount_observed_date = None
            self._total_amount_date_evidence = None
            response = self._http_session().get(
                self.SINA_TOTAL_AMOUNT_URL,
                headers={
                    "Accept": "application/javascript,text/plain;q=0.9",
                    "Referer": "https://finance.sina.com.cn/",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=8,
                allow_redirects=False,
                stream=True,
            )
            if response.is_redirect or response.is_permanent_redirect:
                raise ValueError("redirect rejected")
            response.raise_for_status()

            content_type = str(response.headers.get("Content-Type") or "")
            media_type = content_type.partition(";")[0].strip().casefold()
            if media_type not in {"application/javascript", "text/plain"}:
                raise ValueError("unexpected content type")

            raw_length = response.headers.get("Content-Length")
            if raw_length is not None:
                declared_length = int(raw_length)
                if declared_length < 0 or declared_length > self.SINA_TOTAL_AMOUNT_MAX_BYTES:
                    raise ValueError("response too large")

            raw = bytearray()
            for chunk in response.iter_content(chunk_size=4096):
                if not isinstance(chunk, bytes):
                    raise ValueError("non-bytes response chunk")
                raw.extend(chunk)
                if len(raw) > self.SINA_TOTAL_AMOUNT_MAX_BYTES:
                    raise ValueError("response too large")
            text = bytes(raw).decode("gb18030")

            components: Dict[str, tuple[str, float]] = {}
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                match = re.fullmatch(
                    r'var hq_str_(sh000001|sz399001)="([^"\r\n]*)";', line
                )
                if match is None or match.group(1) in components:
                    raise ValueError("unexpected or duplicate component")
                code, payload = match.groups()
                fields = payload.split(",")
                if len(fields) < 32 or not fields[0].strip():
                    raise ValueError("incomplete component")
                amount_yuan = float(fields[9])
                if not _is_finite_in_range(amount_yuan, minimum=0.0, strict_min=True):
                    raise ValueError("invalid component amount")
                raw_observed = fields[30].strip()
                observed = _normalize_date(raw_observed)
                if observed is None or raw_observed != observed:
                    raise ValueError("invalid component date")
                components[code] = (observed, amount_yuan)

            if set(components) != self.SINA_TOTAL_AMOUNT_CODES:
                raise ValueError("incomplete component set")
            observed_dates = {item[0] for item in components.values()}
            if len(observed_dates) != 1:
                raise ValueError("component dates differ")
            observed_date = observed_dates.pop()
            if self._collection_date and observed_date != self._collection_date:
                raise ValueError("component date does not match collection date")

            total_yi = sum(item[1] for item in components.values()) / 1e8
            if not self.is_valid_factor_value("total_amount", total_yi):
                raise ValueError("implausible total amount")
            self._total_amount_observed_date = observed_date
            return round(total_yi, 2)
        except Exception as exc:
            self.logger.warning(
                "Sina 沪深成交额采集失败 reason=%s", type(exc).__name__
            )
            return None
        finally:
            if response is not None:
                response.close()

    def _eastmoney_total_amount(self) -> Optional[float]:
        """用东方财富沪深京 A 股快照汇总成交额，返回亿元。"""
        try:
            import requests  # noqa: F401 - verify the optional transport exists
        except Exception as e:
            self.logger.debug("requests 不可用，东财成交额兜底跳过: %s", e)
            return None

        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": 1,
            "pz": 1,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f6",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81,m:1+t:1,m:0+t:7,m:1+t:3",
            "fields": "f12,f6",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        }
        data = self._get_json_with_retry(url, params)
        try:
            payload = (data or {}).get("data") or {}
            total_raw = payload.get("total")
            if total_raw is None:
                return None
            total_pages = max(1, (int(total_raw) + 4999) // 5000)
            total_amount = 0.0
            seen_codes = set()
            for page in range(1, total_pages + 1):
                page_params = dict(params, pn=page, pz=5000)
                page_data = self._get_json_with_retry(url, page_params)
                page_payload = (page_data or {}).get("data")
                if not isinstance(page_payload, dict):
                    self.logger.warning("东财成交额第 %d/%d 页缺失，拒绝不完整汇总", page, total_pages)
                    return None
                diff = page_payload.get("diff") or []
                if not diff:
                    self.logger.warning("东财成交额第 %d/%d 页为空，拒绝不完整汇总", page, total_pages)
                    return None
                for row in diff:
                    code = str(row.get("f12") or "").strip()
                    if (
                        not code
                        or not code.startswith(("0", "3", "6"))
                        or code in seen_codes
                    ):
                        continue
                    amount = row.get("f6")
                    if not _is_finite_in_range(amount, minimum=0.0):
                        continue
                    seen_codes.add(code)
                    total_amount += float(amount)
            total_yi = total_amount / 1e8
            return (
                round(total_yi, 2)
                if len(seen_codes) >= self.MIN_TOTAL_AMOUNT_SECURITIES
                and self.is_valid_factor_value("total_amount", total_yi)
                else None
            )
        except Exception as e:
            self.logger.warning("东财成交额兜底解析失败: %s", e)
            return None

    def _collect_margin(self, sources: List[str]) -> tuple[Optional[float], Optional[float]]:
        """两融余额（亿元）与环比增速（%）。"""
        daily = self._akshare_margin_daily_balances(days=21)
        if daily:
            observed_date, balance = daily[0]
            balance = round(float(balance), 2)
            sources.append("akshare")
            stale = self._observation_is_stale(
                "margin_balance", observed_date, self._collection_date
            )
            self._set_status(
                "margin_balance",
                True,
                "akshare",
                observed_date=observed_date,
                stale=stale,
            )
            growth = None
            if len(daily) >= 2 and daily[1][1] > 0:
                growth = round((balance - daily[1][1]) / daily[1][1] * 100, 3)
            if growth is None:
                growth = self._margin_growth_from_store(
                    balance,
                    slice_date=self._collection_date,
                    observed_date=observed_date,
                )
            growth_ok = self.is_valid_factor_value("margin_growth_pct", growth)
            self._set_status(
                "margin_growth_pct",
                growth_ok,
                "akshare",
                "" if growth_ok else "两融增速缺少最近两期不同交易日的完整沪深余额",
                observed_date=observed_date,
                stale=stale,
                anomalous=growth is not None and not growth_ok,
            )
            if not growth_ok:
                growth = None
            return balance, growth

        # 降级 mx_client：直接拿到增速序列
        growth = self._mx_margin_growth()
        if self.is_valid_factor_value("margin_growth_pct", growth):
            sources.append("mx")
            observed_date = self._margin_growth_observed_date
            stale = self._observation_is_stale(
                "margin_growth_pct", observed_date, self._collection_date
            )
            self._set_status(
                "margin_growth_pct",
                True,
                "mx",
                observed_date=observed_date,
                stale=stale,
            )
            return None, round(float(growth), 3)
        self._set_status(
            "margin_balance",
            False,
            "akshare",
            "没有最近完整沪深同日两融余额",
        )
        self._set_status(
            "margin_growth_pct",
            False,
            "akshare/mx",
            "两融余额和增速所有数据源均失败",
            anomalous=growth is not None,
        )
        return None, None

    @staticmethod
    def _margin_to_yi(value: float) -> float:
        """单位归一到亿元。SSE 接口返回元（量级 1e12），SZSE 返回亿元（量级 1e4）。"""
        return value / 1e8 if value > 1e6 else value

    def _akshare_margin_balance(self) -> Optional[float]:
        """沪深两市两融余额（亿元）。

        注意 akshare 的 ``stock_margin_sse``/``stock_margin_szse`` 默认日期参数
        是写死的历史日期（如 end_date='20230922'），不传日期只会拿到陈旧数据；
        且两个接口单位不同（SSE=元、SZSE=亿元），必须逐市场归一后再相加。
        """
        try:
            import akshare as ak  # noqa: F401 - dependency check before delegated fetch
        except Exception as e:
            self.logger.warning("akshare 不可用: %s", e)
            return None

        daily = self._akshare_margin_daily_balances(days=21)
        if not daily:
            self._set_status("margin_balance", False, "akshare", "没有最近完整沪深两融余额")
            return None
        observed_date = daily[0][0]
        self._set_status(
            "margin_balance",
            True,
            "akshare",
            observed_date=observed_date,
            stale=(
                self._observation_is_stale(
                    "margin_balance", observed_date, self._collection_date
                )
                if self._collection_date
                else False
            ),
        )
        return round(daily[0][1], 2)

    def _akshare_margin_daily_balances(self, days: int = 21) -> List[tuple[str, float]]:
        """返回最近若干天沪深两融完整合计余额，按日期倒序。"""
        try:
            import akshare as ak
        except Exception as e:
            self.logger.warning("akshare 不可用: %s", e)
            return []

        col_candidates = ("融资融券余额", "融资融券余额(元)")
        now = _beijing_now()
        sse_by_date: Dict[str, float] = {}
        szse_by_date: Dict[str, float] = {}

        try:
            sse = ak.stock_margin_sse(
                start_date=(now - _dt.timedelta(days=days)).strftime("%Y%m%d"),
                end_date=now.strftime("%Y%m%d"),
            )
            if sse is not None and not sse.empty and "信用交易日期" in sse.columns:
                col = next((c for c in col_candidates if c in sse.columns), None)
                if col is not None:
                    for _, row in sse.iterrows():
                        observed = _normalize_date(row["信用交易日期"])
                        if observed is None:
                            continue
                        date_key = observed.replace("-", "")
                        value = self._margin_to_yi(float(row[col]))
                        if _is_finite_in_range(value, minimum=0.0, strict_min=True):
                            sse_by_date[date_key] = value
        except Exception as e:
            self.logger.warning("AkShare 上交所两融余额采集失败: %s", e)

        # 深交所接口一次只接受一个日期。以上交所实际返回的交易日为准查询，
        # 避免在周末/节假日做无效请求，也避免 SSE 数据源稍有延迟时两边日期错位。
        for date_key in sorted(sse_by_date, reverse=True):
            try:
                szse = ak.stock_margin_szse(date=date_key)
            except Exception:
                continue
            if szse is None or szse.empty:
                continue
            col = next((c for c in col_candidates if c in szse.columns), None)
            if col is None:
                continue
            szse_by_date[date_key] = self._margin_to_yi(float(szse.iloc[0][col]))

        out = []
        for date_key in sorted(set(sse_by_date) & set(szse_by_date), reverse=True):
            total = sse_by_date[date_key] + szse_by_date[date_key]
            if _is_finite_in_range(total, minimum=0.0, strict_min=True):
                out.append((_normalize_date(date_key), round(total, 2)))
        if not out and (sse_by_date or szse_by_date):
            self.logger.warning(
                "两融余额没有完整沪深同日口径（SSE日期=%s SZSE日期=%s）",
                sorted(sse_by_date.keys(), reverse=True)[:3],
                sorted(szse_by_date.keys(), reverse=True)[:3],
            )
        return out

    def _margin_growth_from_store(
        self,
        balance: float,
        *,
        slice_date: Optional[str] = None,
        observed_date: Optional[str] = None,
    ) -> Optional[float]:
        """用最近一个不同观测日余额计算增速，排除同日旧值。"""
        slice_date = slice_date or self._collection_date or SwingStore.today_str()
        try:
            for s in reversed(self.store.get_recent(10)):
                if slice_date and s.date == slice_date:
                    continue
                status = self._status_for_slice(s, "margin_balance")
                previous_observed = _normalize_date(status.get("observed_date"))
                if observed_date and previous_observed == observed_date:
                    continue
                if _is_finite_in_range(s.margin_balance, minimum=0.0, strict_min=True):
                    prev = float(s.margin_balance)
                    growth = round((balance - prev) / prev * 100, 3)
                    return growth if self.is_valid_factor_value("margin_growth_pct", growth) else None
        except Exception as e:
            self.logger.debug("两融增速计算失败: %s", e)
        return None

    def _akshare_margin_growth_direct(self) -> Optional[float]:
        """直接用 AkShare 最近两期沪深两融余额计算环比增速。"""
        daily = self._akshare_margin_daily_balances(days=21)
        if len(daily) < 2:
            return None
        latest = daily[0][1]
        prev = daily[1][1]
        return round((latest - prev) / prev * 100, 3) if prev > 0 else None

    def _mx_margin_growth(self) -> Optional[float]:
        try:
            from radar.rl.external_feature_provider import ExternalFeatureProvider

            df = ExternalFeatureProvider.get_margin_total_history(days=2)
            if df is not None and len(df) >= 2 and "margin_change_pct" in df.columns:
                observed = None
                for column in ("data_date", "trade_date", "date"):
                    if column in df.columns:
                        observed = _normalize_date(df[column].iloc[0])
                        break
                if observed is None:
                    try:
                        observed = _normalize_date(df.index[0])
                    except Exception:
                        observed = None
                if observed is None:
                    self.logger.warning("MX 两融增速缺少真实观测日，拒绝标记为当日数据")
                    return None
                self._margin_growth_observed_date = observed
                return round(float(df["margin_change_pct"].iloc[0]), 3)
        except Exception as e:
            self.logger.debug("mx 两融增速降级失败: %s", e)
        return None

    def _collect_limit_pool(self, sources: List[str]) -> tuple[Optional[int], Optional[int]]:
        """涨停 / 跌停家数。

        akshare 涨停/跌停股池接口必须传 ``date=YYYYMMDD``（且只覆盖最近 ~30 个交易日，
        收盘后才齐全）。当日数据缺失时回退到最近一个交易日。
        """
        try:
            import akshare as ak
        except Exception as e:
            self.logger.warning("akshare 不可用: %s", e)
            ak = None

        up_fn = getattr(ak, "stock_zt_pool_em", None) if ak is not None else None
        dn_fn = getattr(ak, "stock_zt_pool_dtgc_em", None) if ak is not None else None

        def _count(fn, date_str: str) -> Optional[int]:
            if fn is None:
                return None
            try:
                df = fn(date=date_str)
                return _valid_nonnegative_int(len(df)) if df is not None else None
            except Exception:
                return None

        up = down = None
        now = _beijing_now()
        # 只接受同一日期的完整双边数据；单边成功继续回溯，避免拼接错日口径。
        for back in range(0, 11):
            d = (now - _dt.timedelta(days=back)).strftime("%Y%m%d")
            if d == now.strftime("%Y%m%d") and now.hour < 15:
                continue  # 收盘前当日股池尚不完整
            up_try = _count(up_fn, d)
            dn_try = _count(dn_fn, d)
            if up_try is not None and dn_try is not None and up_try + dn_try > 0:
                up, down = up_try, dn_try
                self._limit_observed_date = _normalize_date(d)
                self._limit_source = "akshare"
                break
        if up is not None and down is not None:
            sources.append("akshare")

        if up is None or down is None:
            sentiment = self._mx_sentiment()
            if sentiment is not None:
                mx_up = _valid_nonnegative_int(sentiment.get("limit_up_count"))
                mx_down = _valid_nonnegative_int(sentiment.get("limit_down_count"))
                if mx_up is not None and mx_down is not None and mx_up + mx_down > 0:
                    up, down = mx_up, mx_down
                    observed = (
                        sentiment.get("data_date")
                        or sentiment.get("trade_date")
                        or sentiment.get("date")
                    )
                    self._limit_observed_date = _normalize_date(observed)
                    if self._limit_observed_date is None:
                        self.logger.warning("MX 涨跌停情绪缺少真实观测日，拒绝使用")
                        up = down = None
                    else:
                        self._limit_source = "mx"
                        sources.append("mx")
        return up, down

    def _mx_sentiment(self) -> Optional[Dict]:
        try:
            from radar.rl.external_feature_provider import ExternalFeatureProvider

            return ExternalFeatureProvider.get_market_sentiment()
        except Exception as e:
            self.logger.debug("mx 情绪降级失败: %s", e)
            return None

    # ------------------------------------------------------------------ 极值
    def detect_extremes(
        self,
        slice_: MacroSlice,
        *,
        suppress_replayed: bool = True,
    ) -> List[MacroExtreme]:
        """判定冰点/沸点；EOD默认抑制同一观测日的重复告警。"""
        extremes: List[MacroExtreme] = []
        for factor in NUMERIC_FACTORS:
            value = getattr(slice_, factor)
            if not self._factor_is_usable(slice_, factor):
                continue
            # 排除当前切片自身（可能已落库），避免自包含导致分位偏置
            sample = []
            seen_observations = set()
            current_status = self._status_for_slice(slice_, factor)
            current_observed = (
                _normalize_date(current_status.get("observed_date")) or slice_.date
            )
            observation_replayed = False
            for previous in reversed(self.store.get_recent(250)):
                if previous.date == slice_.date or not self._factor_is_usable(previous, factor):
                    continue
                status = self._status_for_slice(previous, factor)
                observed = _normalize_date(status.get("observed_date")) or previous.date
                if observed == current_observed:
                    observation_replayed = True
                    continue
                if observed in seen_observations:
                    continue
                seen_observations.add(observed)
                sample.append(float(getattr(previous, factor)))
            if (
                suppress_replayed
                and observation_replayed
                and current_status.get("observed_date")
            ):
                self.logger.info(
                    "宏观因子 %s 观测日 %s 已处理过，本次不重复判极值",
                    factor,
                    current_observed,
                )
                continue
            if len(sample) < self.min_history_days:
                continue
            pct = _percentile_rank(sample, float(value))
            if pct <= self.extreme_low:
                level = "冰点"
            elif pct >= self.extreme_high:
                level = "沸点"
            else:
                level = "正常"
            if level != "正常":
                extremes.append(
                    MacroExtreme(
                        factor=factor,
                        label=_FACTOR_LABELS.get(factor, factor),
                        value=float(value),
                        percentile=round(pct, 1),
                        level=level,
                        sample_size=len(sample),
                    )
                )
        return extremes


def _percentile_rank(sample: List[float], value: float) -> float:
    """value 在 sample 中的百分位排名（0-100）。"""
    clean = []
    for item in sample:
        try:
            number = float(item)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            clean.append(number)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 50.0
    if not clean or not math.isfinite(value):
        return 50.0
    below = sum(1 for x in clean if x < value)
    equal = sum(1 for x in clean if x == value)
    return (below + 0.5 * equal) / len(clean) * 100.0


def _normalize_date(value: object) -> Optional[str]:
    """Normalize API date variants to YYYY-MM-DD without guessing malformed values."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    raw = str(value).strip()
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 8:
        candidate = digits[:8]
        try:
            return _dt.datetime.strptime(candidate, "%Y%m%d").date().isoformat()
        except ValueError:
            pass
    try:
        return _dt.date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return None


def _is_finite_in_range(
    value: object,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    strict_min: bool = False,
) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(number):
        return False
    if minimum is not None and (number <= minimum if strict_min else number < minimum):
        return False
    if maximum is not None and number > maximum:
        return False
    return True


def _valid_nonnegative_int(value: object) -> Optional[int]:
    if isinstance(value, bool) or not _is_finite_in_range(value, minimum=0.0):
        return None
    number = float(value)
    integer = int(number)
    return integer if number == integer else None


def _fmt(v) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else "N/A"
