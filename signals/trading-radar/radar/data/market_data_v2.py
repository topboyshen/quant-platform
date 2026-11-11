"""
增强型数据源降级链 - 重构 MarketDataFetcher
核心改进：
1. 主源(东财批量) → 备用1(腾讯单股) → 备用2(akshare) 的清晰降级链
2. 批量缓存独立 TTL 管理，避免每次扫描都重新拉取
3. 502 错误快速检测，避免 tenacity 在服务端错误上浪费重试
4. 单股接口使用独立 session，与批量接口隔离
5. 响应编码自动检测（gb18030 优先）
"""

import datetime
import json
import logging
import math
import os
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import pytz
import requests

from radar.data.kline_sources import (  # noqa: F401  (resolve_secid 为公开 re-export)
    capture_source_attempts,
    fetch_akshare_kline,
    fetch_eastmoney_kline,
    fetch_tencent_kline,
    is_usable_kline,
    kline_observation,
    record_source_attempt,
    resolve_secid,
    source_attempts_snapshot,
)
from radar.data.session_pool import create_pooled_session
from runtime_contracts.calendar import TZ, previous_session, read_object, sessions

# 统一数据 Schema 支持（DataNormalizer 是公开 re-export，保留供下游 import）
try:
    from radar.schemas.data_schema import ColumnNames, DataNormalizer, normalize_dataframe  # noqa: F401
    SCHEMA_AVAILABLE = True
except ImportError:
    SCHEMA_AVAILABLE = False

try:
    from radar.data.unified_history import attach_lineage
except ImportError:
    attach_lineage = None  # type: ignore


class MarketTimeChecker:
    """A股交易时间检查器 - 使用北京时间"""

    def __init__(self, config: Dict, logger: logging.Logger):
        self.logger = logger
        self.beijing_tz = pytz.timezone('Asia/Shanghai')
        self.morning_start = parse_market_time(config['morning_start'])
        self.morning_end = parse_market_time(config['morning_end'])
        self.afternoon_start = parse_market_time(config['afternoon_start'])
        self.afternoon_end = parse_market_time(config['afternoon_end'])

    def get_beijing_time(self) -> datetime.datetime:
        """获取当前北京时间"""
        utc_now = datetime.datetime.now(pytz.UTC)
        return utc_now.astimezone(self.beijing_tz)

    def is_open(self) -> bool:
        """判断当前是否为A股交易时间（北京时间）"""
        now = self.get_beijing_time()

        # 周末休市（周一=0, 周日=6）
        if now.weekday() >= 5:
            return False

        curr_time = now.time()
        morning = self.morning_start <= curr_time <= self.morning_end
        afternoon = self.afternoon_start <= curr_time <= self.afternoon_end

        return morning or afternoon

    def get_time_info(self) -> str:
        """获取当前时间信息字符串"""
        now = self.get_beijing_time()
        utc_now = datetime.datetime.now(pytz.UTC)
        return f"北京时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (UTC: {utc_now.strftime('%H:%M:%S')})"


@lru_cache(maxsize=32)
def parse_market_time(time_str: str) -> datetime.time:
    hour, minute = map(int, time_str.split(':'))
    return datetime.time(hour, minute)


class MarketDataFetcher:
    """
    增强型市场数据获取器

    数据源降级链：
    1. 东财批量接口 (clist/get) - 拉取全市场 5000 只
    2. 东财单股接口 (stock/get) - 单只精确查询
    3. 腾讯财经接口 (qt.gtimg.cn) - 高稳定性备用
    4. akshare 接口 - 本地库兜底

    关键优化：
    - 批量缓存 TTL 自适应（交易时段 3s，非交易时段 60s）
    - 502/503 错误不触发 tenacity 重试（服务端错误重试无效）
    - 单股查询优先从批量缓存命中，避免额外请求
    - 独立 session 隔离，避免 cookies 污染
    """

    # 缓存 TTL 配置（秒）
    CACHE_TTL_TRADING = 3.0      # 交易时段批量缓存 3 秒
    CACHE_TTL_NON_TRADING = 60.0  # 非交易时段批量缓存 60 秒

    def __init__(self, logger: logging.Logger, api_key: str = "", use_akshare: bool = True):
        self.logger = logger
        self.api_key = api_key

        # 批量接口 session
        self._session = create_pooled_session({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })

        # 单股接口独立 session（避免批量接口的 502 污染）
        self._single_session = create_pooled_session({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "http://quote.eastmoney.com/",
        })

        # 腾讯备用源独立 session
        self._backup_session = create_pooled_session({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

        # 批量数据缓存
        self._cache_data = None
        self._cache_time = 0.0
        self._cache_ttl = self.CACHE_TTL_TRADING
        self._fetch_all_lock = threading.Lock()
        self._last_fetch_all_attempt = 0.0
        self._failed_fetch_cooldown = 2.0
        self._eastmoney_state_lock = threading.Lock()
        self._eastmoney_disabled_until = 0.0
        self._eastmoney_backoff_sec = 30.0

        # 交易时段检测器（用于自适应 TTL）
        self._time_checker = None

        # 初始化 akshare 适配器
        self._akshare_fetcher = None
        if use_akshare:
            try:
                from radar.data.akshare_adapter import AKShareDataFetcher
                self._akshare_fetcher = AKShareDataFetcher(logger)
                if self._akshare_fetcher.is_available:
                    self.logger.info("akshare 数据源已启用，作为第三备选源")
                else:
                    self.logger.info("akshare 数据源未可用")
            except Exception as e:
                self.logger.warning(f"akshare 适配器初始化失败: {e}")

    def _get_cache_ttl(self) -> float:
        """根据交易时段返回合适的缓存 TTL"""
        if self._time_checker is not None:
            return self.CACHE_TTL_TRADING if self._time_checker.is_open() else self.CACHE_TTL_NON_TRADING
        return self.CACHE_TTL_TRADING

    def _is_server_error(self, status_code: int) -> bool:
        """判断是否为服务端错误（5xx），这类错误重试通常无效"""
        return 500 <= status_code < 600

    def _eastmoney_available(self) -> bool:
        with self._eastmoney_state_lock:
            return time.monotonic() >= self._eastmoney_disabled_until

    def _pause_eastmoney(self) -> None:
        with self._eastmoney_state_lock:
            self._eastmoney_disabled_until = max(
                self._eastmoney_disabled_until,
                time.monotonic() + self._eastmoney_backoff_sec,
            )

    def _safe_float(self, val: Any, default: float = 0.0) -> float:
        """安全转换为 float"""
        if val is None or val == '-' or val == '':
            return default
        try:
            value = float(val)
            return value if math.isfinite(value) else default
        except (ValueError, TypeError):
            return default

    def _safe_int(self, val: Any, default: int = 0) -> int:
        """安全转换为 int"""
        if val is None or val == '-' or val == '':
            return default
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return default

    # ───────────────────────────────────────────────
    # 批量接口
    # ───────────────────────────────────────────────

    def fetch_all(self) -> Optional[pd.DataFrame]:
        """获取全部 A 股实时行情（带缓存）"""
        now = time.time()
        ttl = self._get_cache_ttl()

        if self._cache_data is not None and (now - self._cache_time) < ttl:
            return self._cache_data

        # fetch_multiple 会并发查询标的；同一 fetcher 只能有一个全市场请求在途。
        # 失败也做 2 秒负缓存，避免等待锁的线程依次再打一次已知故障源。
        with self._fetch_all_lock:
            now = time.time()
            ttl = self._get_cache_ttl()
            if self._cache_data is not None and (now - self._cache_time) < ttl:
                return self._cache_data
            if now - self._last_fetch_all_attempt < self._failed_fetch_cooldown:
                return None
            self._last_fetch_all_attempt = now
            try:
                df = self._fetch_all_from_eastmoney()
                if df is not None and not df.empty:
                    self._cache_data = df
                    # Age starts when the response is complete, not when a
                    # potentially slow request began.
                    self._cache_time = time.time()
                    self.logger.debug(f"批量行情缓存已更新，TTL={ttl:.0f}s")
                return df
            except Exception as e:
                self.logger.error(f"获取行情失败: {e}")
                # Never return an expired DataFrame without an explicit stale
                # contract; downstream realtime consumers may otherwise stamp
                # it with the current time and trigger strategies.
                return None

    def _fetch_all_from_eastmoney(self) -> Optional[pd.DataFrame]:
        """从东方财富获取全市场行情（无 tenacity 重试，因为 502 重试无效）"""
        if not self._eastmoney_available():
            return None
        url = "http://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "5000", "po": "1", "np": "1",
            "fltt": "2", "invt": "2", "fid": "f12",
            "fs": "m:0+t:6,m:0+t:13,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f2,f3,f4,f5,f6,f17,f18,f15,f16,f8",
            "_": str(int(time.time() * 1000)),
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "http://quote.eastmoney.com/",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            response = self._session.get(url, params=params, headers=headers, timeout=10)

            # 502/503 直接返回 None，不浪费重试
            if self._is_server_error(response.status_code):
                self._pause_eastmoney()
                self.logger.warning(f"东财批量接口服务端错误 {response.status_code}，跳过重试")
                return None

            response.raise_for_status()
            data = response.json()

            if data.get('data') and data['data'].get('diff'):
                stocks = data['data']['diff']
                rows = []
                for stock in stocks:
                    rows.append({
                        ColumnNames.CODE: stock.get('f12', ''),
                        ColumnNames.NAME: stock.get('f14', ''),
                        ColumnNames.CLOSE: self._safe_float(stock.get('f2')),
                        ColumnNames.CHANGE_PCT: self._safe_float(stock.get('f3')),
                        ColumnNames.CHANGE: self._safe_float(stock.get('f4')),
                        ColumnNames.VOLUME: self._safe_int(stock.get('f5')),
                        ColumnNames.AMOUNT: self._safe_float(stock.get('f6')),
                        ColumnNames.OPEN: self._safe_float(stock.get('f17')),
                        'prev_close': self._safe_float(stock.get('f18')),
                        ColumnNames.HIGH: self._safe_float(stock.get('f15')),
                        ColumnNames.LOW: self._safe_float(stock.get('f16')),
                        ColumnNames.TURNOVER: self._safe_float(stock.get('f8'))
                    })

                df = pd.DataFrame(rows)

                # 自动标准化
                if SCHEMA_AVAILABLE:
                    try:
                        df = normalize_dataframe(df, source="eastmoney_push2")
                    except (ValueError, TypeError):
                        df[ColumnNames.PRICE] = df[ColumnNames.CLOSE]

                return df
        except requests.exceptions.Timeout:
            self.logger.warning("东财批量接口超时")
        except requests.exceptions.ConnectionError:
            self.logger.warning("东财批量接口连接错误")
        except Exception as e:
            self.logger.error(f"东财批量接口异常: {e}")

        return None

    # ───────────────────────────────────────────────
    # 单股接口（降级链核心）
    # ───────────────────────────────────────────────

    def get_stock_data(self, code: str, name: str = None) -> Optional[Dict]:
        """
        获取单只股票完整数据（统一 Schema 格式）
        降级链：批量缓存命中 → 东财单股 → 腾讯备用 → akshare
        """
        full_code = str(code).strip().zfill(6)
        if len(full_code) != 6 or not full_code.isdigit():
            return None

        # Step 1: 优先从批量缓存命中（零额外请求）
        cached = self._get_from_batch_cache(full_code)
        if cached:
            return cached

        # Step 2: 北交所股票用专用接口
        if full_code.startswith('920') or full_code.startswith('8') or full_code.startswith('9'):
            result = self._get_bse_realtime(full_code, name)
            if result:
                return result
            return self._get_backup_stock_data(full_code, name)

        # Step 3: 东财单股接口
        result = self._get_single_stock_realtime(full_code, name)
        if result:
            return result

        # Step 4: 腾讯备用源
        self.logger.debug(f"东财单股获取 {code} 失败，尝试腾讯备用源...")
        backup_result = self._get_backup_stock_data(full_code, name)
        if backup_result:
            return backup_result

        # Step 5: akshare 兜底
        if self._akshare_fetcher and self._akshare_fetcher.is_available:
            self.logger.debug(f"腾讯备用源获取 {code} 失败，尝试 akshare...")
            akshare_result = self._akshare_fetcher.get_stock_data(code, name)
            if akshare_result:
                akshare_result.setdefault('source', 'akshare')
                return akshare_result

        return None

    def _get_from_batch_cache(self, code: str) -> Optional[Dict]:
        """从批量缓存获取单只股票数据（零网络请求）"""
        if self._cache_data is None or self._cache_data.empty:
            return None
        cache_age = time.time() - self._cache_time
        if self._cache_time <= 0 or cache_age > self._get_cache_ttl():
            return None

        try:
            norm = self._cache_data[ColumnNames.CODE].astype(str).str.strip().str.zfill(6)
            matches = self._cache_data[norm == code.zfill(6)]
            if not matches.empty:
                row = matches.iloc[0]
                return {
                    'code': code,
                    'name': row[ColumnNames.NAME],
                    'price': float(row[ColumnNames.CLOSE]),
                    'volume': int(row.get(ColumnNames.VOLUME, 0)),
                    'open': float(row.get(ColumnNames.OPEN, 0)),
                    'high': float(row.get(ColumnNames.HIGH, 0)),
                    'low': float(row.get(ColumnNames.LOW, 0)),
                    'prev_close': float(row.get('prev_close', 0)),
                    'timestamp': self._cache_time,
                    'source': 'eastmoney',
                    'fresh': True,
                }
        except Exception as e:
            self.logger.debug(f"从批量缓存获取 {code} 失败: {e}")
        return None

    def _get_single_stock_realtime(self, code: str, name: str = None) -> Optional[Dict]:
        """东财单股接口（带重试，只对网络错误重试，不对 5xx 重试）"""
        if not self._eastmoney_available():
            return None
        if code.startswith('6'):
            secid = f'1.{code}'
        else:
            secid = f'0.{code}'

        url = 'http://push2.eastmoney.com/api/qt/stock/get'
        params = {
            'secid': secid,
            'fields': 'f43,f44,f45,f46,f47,f48,f60,f14,f57',
            '_': str(int(time.time() * 1000))
        }

        # 手动重试：只对网络错误重试，502 不重试
        for attempt in range(2):
            try:
                response = self._single_session.get(url, params=params, timeout=8)

                if self._is_server_error(response.status_code):
                    self._pause_eastmoney()
                    self.logger.debug(f"东财单股接口 {response.status_code}，进入短暂退避")
                    return None

                response.raise_for_status()
                data = response.json()

                if data.get('data'):
                    d = data['data']
                    response_code = str(d.get('f57', '')).strip().zfill(6)
                    if response_code != code:
                        return None
                    close_price = d.get('f43', 0) / 100
                    if not math.isfinite(close_price) or close_price <= 0:
                        return None
                    return {
                        'code': code,
                        'name': d.get('f14', name),
                        'close': close_price,
                        'price': close_price,
                        'open': d.get('f44', 0) / 100,
                        'high': d.get('f45', 0) / 100,
                        'low': d.get('f46', 0) / 100,
                        'volume': d.get('f47', 0),
                        'prev_close': d.get('f60', 0) / 100,
                        'source': 'eastmoney',
                    }
                return None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == 0:
                    time.sleep(1)
                    continue
                self.logger.warning(f"东财单股接口网络错误 [{code}]")
                return None
            except Exception as e:
                self.logger.debug(f"东财单股接口异常 [{code}]: {e}")
                return None

        return None

    def _get_bse_realtime(self, code: str, name: str = None) -> Optional[Dict]:
        """北交所股票实时数据"""
        if not self._eastmoney_available():
            return None
        url = 'http://push2.eastmoney.com/api/qt/stock/get'
        params = {
            'secid': f'0.{code}',
            'fields': 'f43,f44,f45,f46,f47,f48,f60,f14,f57',
            '_': str(int(time.time() * 1000))
        }

        for attempt in range(2):
            try:
                response = self._single_session.get(url, params=params, timeout=8)

                if self._is_server_error(response.status_code):
                    self._pause_eastmoney()
                    return None

                response.raise_for_status()
                data = response.json()

                if data.get('data'):
                    d = data['data']
                    response_code = str(d.get('f57', '')).strip().zfill(6)
                    if response_code != code:
                        return None
                    close_price = d.get('f43', 0) / 100
                    if not math.isfinite(close_price) or close_price <= 0:
                        return None
                    return {
                        'code': code,
                        'name': d.get('f14', name),
                        'close': close_price,
                        'price': close_price,
                        'open': d.get('f44', 0) / 100,
                        'high': d.get('f45', 0) / 100,
                        'low': d.get('f46', 0) / 100,
                        'volume': d.get('f47', 0),
                        'prev_close': d.get('f60', 0) / 100,
                        'source': 'eastmoney',
                    }
                return None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == 0:
                    time.sleep(1)
                    continue
                return None
            except Exception as e:
                self.logger.debug(f"北交所接口异常 [{code}]: {e}")
                return None

        return None

    def _get_backup_stock_data(self, code: str, name: str = None) -> Optional[Dict]:
        """
        腾讯财经备用接口
        编码：优先 gb18030（兼容 GBK 和 GB2312）
        """
        try:
            if code.startswith('6'):
                tencent_code = f"sh{code}"
            elif len(code) == 6 and (code.startswith('8') or code.startswith('9')):
                tencent_code = f"bj{code}"
            else:
                tencent_code = f"sz{code}"

            url = f"http://qt.gtimg.cn/q={tencent_code}"

            # 备用源超时更短，快速失败
            response = self._backup_session.get(url, timeout=5)
            if response.status_code != 200:
                return None

            # 尝试多种编码解码
            for encoding in ['gb18030', 'gbk', 'gb2312', 'utf-8']:
                try:
                    response.encoding = encoding
                    text = response.text
                    break
                except Exception:
                    continue
            else:
                text = response.text

            if not text or '~' not in text:
                return None

            # 解析腾讯格式: v_sh600000="1~浦发银行~600000~10.50~..."
            if '"' not in text:
                return None
            parts = text.split('"', 2)[1].split('~')
            if len(parts) < 45:
                return None

            response_code = str(parts[2]).strip().zfill(6)
            if response_code != str(code).strip().zfill(6):
                return None
            price = float(parts[3])
            if not math.isfinite(price) or price <= 0:
                return None

            return {
                'code': code,
                'name': parts[1] if parts[1] else name,
                'price': price,
                'open': float(parts[5]),
                'high': float(parts[33]),
                'low': float(parts[34]),
                'volume': int(float(parts[36])),
                'prev_close': float(parts[4]),
                'close': price,
                'source': 'tencent',
            }
        except Exception as e:
            self.logger.debug(f"腾讯备用源获取 {code} 失败: {e}")
            return None

    # ───────────────────────────────────────────────
    # 历史数据接口（保持不变）
    # ───────────────────────────────────────────────

    def _try_load_parquet(self, code: str, days: int) -> Optional[pd.DataFrame]:
        """优先从 Parquet 缓存加载 K 线数据。"""
        from radar.data.ohlcv_parquet_store import (
            max_age_days_from_env,
            prefer_read_enabled,
            try_load_kline_parquet,
        )
        if not prefer_read_enabled():
            return None
        cached = try_load_kline_parquet(
            code, min_rows=max(30, days // 2), max_age_days=max_age_days_from_env()
        )
        if cached is not None and not is_usable_kline(cached, days):
            latest = pd.to_datetime(cached.get("date"), errors="coerce").max()
            self.logger.warning("Parquet 内容过旧或不足，跳过: %s latest=%s rows=%d", code, latest, len(cached))
            return None
        return cached

    def _fetch_eastmoney_kline(self, code: str, days: int) -> Optional[pd.DataFrame]:
        """从东方财富 push2his 接口拉取 K 线数据（实现见 kline_sources）。"""
        return fetch_eastmoney_kline(self._session, self.logger, code, days)

    def _fetch_tencent_kline(self, code: str, days: int) -> Optional[pd.DataFrame]:
        """从腾讯财经接口拉取日K线数据（东财被拦截时的备用源，实现见 kline_sources）。"""
        return fetch_tencent_kline(self.logger, code, days)

    def _fetch_akshare_kline(self, code: str, days: int, *, required_date: Optional[str] = None,
                             observations: Optional[list[dict]] = None) -> Optional[pd.DataFrame]:
        """Rejected-frame metadata is separate from accepted price data."""
        return fetch_akshare_kline(self.logger, self._akshare_fetcher, code, days,
                                   required_date=required_date, observations=observations)

    def _normalize_and_persist(self, code: str, df: pd.DataFrame, *, source: str = "eastmoney_push2his", required_date: Optional[str] = None, history_days: int = 60) -> pd.DataFrame:
        """标准化 Schema、附加血缘并持久化到 Parquet。"""
        from radar.data.ohlcv_parquet_store import maybe_persist_fetched_kline

        schema_source = "eastmoney_kline" if source == "eastmoney_push2his" else source
        normalized = False
        if SCHEMA_AVAILABLE:
            try:
                df = normalize_dataframe(df, source=schema_source)
                if attach_lineage is not None:
                    df = attach_lineage(
                        df, source=source,
                        pipeline="MarketDataFetcher.get_historical_data",
                        adjustment="qfq",
                    )
                normalized = True
            except (ValueError, TypeError):
                if required_date is not None:
                    # The permissive legacy conversion is not an EOD repair:
                    # malformed current-day prices must fall through to a
                    # different provider, never bypass schema rejection.
                    raise
                pass

        if not normalized:
            df["price"] = df["close"].astype(float)
            df["volume"] = df["volume"].astype(int)
            df["date"] = pd.to_datetime(df["date"])
            if attach_lineage is not None:
                df = attach_lineage(
                    df, source=source,
                    pipeline="MarketDataFetcher.get_historical_data",
                    adjustment="qfq",
                )
        # Normalisation can discard the newest invalid OHLCV row. Never
        # publish that older frame to cache or to EOD as current-day data.
        if required_date is not None and not is_usable_kline(
            df, history_days, required_date=required_date
        ):
            raise ValueError("normalised kline does not meet required EOD date/rows")
        maybe_persist_fetched_kline(code, df, persist_reason=f"market_fetcher_{source}")
        return df

    def get_historical_data(self, code: str, days: int = 60, *, required_date: Optional[str] = None) -> pd.DataFrame:
        """获取历史K线数据（东财 push2his 优先，AkShare ETF/A股历史接口兜底）。"""
        if required_date is not None:
            with capture_source_attempts(code, required_date) as evidence:
                frame = self._get_historical_data_required(code, days, required_date=required_date)
                try:
                    frame.attrs["source_attempts"] = source_attempts_snapshot(
                        evidence, code=code, required_date=required_date)
                except Exception:
                    # Observability failure is not permission to alter prices or rules.
                    pass
                return frame
        cached = self._try_load_parquet(code, days)
        if cached is not None:
            return cached

        try:
            df = self._fetch_eastmoney_kline(code, days)
            if is_usable_kline(df, days):
                return self._normalize_and_persist(code, df, source="eastmoney_push2his")
            if df is not None:
                self.logger.warning("东财 K线未通过时效/行数校验: %s rows=%d", code, len(df))

            # 东财被拦截时尝试腾讯 K线
            tencent_df = self._fetch_tencent_kline(code, days)
            if is_usable_kline(tencent_df, days):
                return self._normalize_and_persist(code, tencent_df, source="tencent_kline")
            if tencent_df is not None:
                self.logger.warning("腾讯 K线未通过时效/行数校验: %s rows=%d", code, len(tencent_df))

            ak_df = self._fetch_akshare_kline(code, days)
            if is_usable_kline(ak_df, days):
                return self._normalize_and_persist(code, ak_df, source="akshare_kline")
            if ak_df is not None:
                self.logger.warning("AkShare K线未通过时效/行数校验: %s rows=%d", code, len(ak_df))

            self.logger.warning(f"获取历史数据失败: {code} (无数据)")
        except Exception as e:
            self.logger.error(f"获取历史数据失败 {code}: {e}")
            fallback = self._try_load_parquet(code, days)
            if fallback is not None:
                self.logger.warning(f"网络失败，降级使用本地 Parquet 缓存: {code}")
                return fallback
        return pd.DataFrame()

    def _stale_retry_seconds(self) -> int:
        try:
            seconds = int(os.getenv("RADAR_EOD_STALE_RETRY_SECONDS", "45"))
        except ValueError:
            seconds = 45
        return max(0, min(seconds, 1200))

    def _sleep(self, seconds: int) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def _latest_frame_date(self, frame: Optional[pd.DataFrame]) -> Optional[datetime.date]:
        if frame is None or getattr(frame, "empty", True) or "date" not in getattr(frame, "columns", []):
            return None
        latest = pd.to_datetime(frame["date"], errors="coerce").max()
        if pd.isna(latest):
            return None
        return latest.date()

    def _is_publication_lag(self, latest: Optional[datetime.date], required: datetime.date) -> bool:
        if latest is None:
            return False
        path = Path(os.getenv("RADAR_TRADING_CALENDAR_FILE") or
                    Path(__file__).resolve().parents[2] / "data/trading_calendar.json")
        try:
            dates = sessions(read_object(path), datetime.datetime.now(TZ))
            return latest == previous_session(dates, required)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            # Unknown calendar disables waiting, NOT exact-date data validation.
            return False

    def _usable_required_frame(self, frame, source, code: str, days: int, required_date: str) -> bool:
        ok = is_usable_kline(frame, days, required_date=required_date)
        if not ok and frame is not None and not getattr(frame, "empty", True):
            latest = self._latest_frame_date(frame)
            self.logger.info(
                "EOD日K日期/行数暂不合格: code=%s source=%s required_date=%s latest=%s rows=%d",
                code, source, required_date, latest, len(frame),
            )
        return ok

    def _fetch_required_network_sources(
        self, code: str, days: int, required_date: str
    ) -> Tuple[pd.DataFrame, bool]:
        observations: list[dict] = []
        attempts = (
            ("eastmoney_push2his", lambda: self._fetch_eastmoney_kline(code, days)),
            ("tencent_kline", lambda: self._fetch_tencent_kline(code, days)),
            ("akshare_kline", lambda: self._fetch_akshare_kline(
                code, days, required_date=required_date, observations=observations)),
        )
        required = datetime.date.fromisoformat(required_date)
        for source, fetch in attempts:
            started = time.monotonic()
            try:
                raw = fetch()
                record_source_attempt(source, "adapter", raw, days=days, started=started)
                observation = kline_observation(source, "adapter", raw, days, required_date)
                observations.append(observation)
                self.logger.info("kline_observation %s", json.dumps(observation, sort_keys=True))
                if not self._usable_required_frame(raw, source, code, days, required_date):
                    continue
                return self._normalize_and_persist(
                    code, raw, source=source, required_date=required_date, history_days=days
                ), False
            except Exception as exc:
                record_source_attempt(source, "adapter", error=exc, started=started)
                observation = kline_observation(
                    source, "adapter", None, days, required_date, error_type=type(exc).__name__
                )
                observations.append(observation)
                self.logger.info("kline_observation %s", json.dumps(observation, sort_keys=True))
                self.logger.info(
                    "EOD数据源暂未满足日期合同，继续下一源: code=%s source=%s required_date=%s error_type=%s",
                    code, source, required_date, type(exc).__name__,
                )
        # One confirmed previous-session response suffices, even if other sources
        # are offline. Stale observations never enter normalization/persistence.
        publication_lag = any(
            item["state"] == "stale" and self._is_publication_lag(
                datetime.date.fromisoformat(item["latest_date"]), required)
            for item in observations
        )
        return pd.DataFrame(), publication_lag

    def _get_historical_data_required(self, code: str, days: int, *, required_date: str) -> pd.DataFrame:
        """EOD-only exact-date fallback; legacy callers keep the prior policy."""
        required = datetime.date.fromisoformat(required_date)
        if required.isoformat() != required_date:
            raise ValueError("required_date must be canonical YYYY-MM-DD")

        started = time.monotonic()
        try:
            cached = self._try_load_parquet(code, days)
            record_source_attempt("parquet", "cache", cached, days=days, started=started)
            if self._usable_required_frame(cached, "parquet", code, days, required_date):
                # Apply the same schema/date guard to cached input, but do not
                # rewrite a cache simply because it was read.
                from radar.schemas.data_schema import normalize_dataframe as normalize_cache
                normalized_cache = normalize_cache(cached.copy(), source="cache")
                if self._usable_required_frame(
                    normalized_cache, "parquet_normalised", code, days, required_date
                ):
                    observation = kline_observation(
                        "parquet", "cache", normalized_cache, days, required_date
                    )
                    self.logger.info("kline_observation %s", json.dumps(observation, sort_keys=True))
                    return normalized_cache
        except Exception as exc:
            record_source_attempt("parquet", "cache", error=exc, started=started)
            self.logger.warning("EOD缓存不可用: code=%s error_type=%s", code, type(exc).__name__)

        waited_before = getattr(self, "_eod_stale_wait_date", None) == required_date
        frame, publication_lag = self._fetch_required_network_sources(code, days, required_date)
        if not frame.empty:
            return frame
        wait_seconds = self._stale_retry_seconds()
        if publication_lag and wait_seconds > 0 and not waited_before:
            self.logger.info(
                "EOD至少一源仅到上一交易日，等待 %ss 后重试一次: code=%s required_date=%s",
                wait_seconds, code, required_date,
            )
            self._eod_stale_wait_date = required_date
            self._sleep(wait_seconds)
            frame, _ignored = self._fetch_required_network_sources(code, days, required_date)
            if not frame.empty:
                return frame
        self.logger.error("EOD无合格日K: code=%s required_date=%s; 不使用旧行情代替无信号", code, required_date)
        return pd.DataFrame()
