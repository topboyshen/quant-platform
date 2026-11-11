"""
数据层 - 行情获取模块
职责：实时行情、历史数据获取，不依赖ML/RL

注意：本模块返回的数据已统一为 Standard Schema（英文列名）
兼容层会自动处理中文列名到英文列名的映射
"""

import datetime
import logging
import time
from typing import Any, Dict, Optional

import pandas as pd
import pytz
import requests
from tenacity import before_sleep_log, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

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
        self.morning_start = self._parse_time(config['morning_start'])
        self.morning_end = self._parse_time(config['morning_end'])
        self.afternoon_start = self._parse_time(config['afternoon_start'])
        self.afternoon_end = self._parse_time(config['afternoon_end'])

    @staticmethod
    def _parse_time(time_str: str) -> datetime.time:
        hour, minute = map(int, time_str.split(':'))
        return datetime.time(hour, minute)

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


class MarketDataFetcher:
    """市场数据获取器 - 仅获取数据，无ML/RL依赖"""

    def __init__(self, logger: logging.Logger, api_key: str = "", use_akshare: bool = True):
        self.logger = logger
        self.api_key = api_key
        self._cache_data = None
        self._cache_time = 0
        self._cache_ttl = 5
        self._session = requests.Session()
        self._backup_session = requests.Session()  # 独立 session，避免被东方财富 502 污染

        # 初始化 akshare 适配器（第三级数据源）
        self._akshare_fetcher = None
        if use_akshare:
            try:
                from radar.data.akshare_adapter import AKShareDataFetcher
                self._akshare_fetcher = AKShareDataFetcher(logger)
                if self._akshare_fetcher.is_available:
                    self.logger.info("akshare 数据源已启用，作为第三备选源")
                else:
                    self.logger.info("akshare 数据源未可用（未安装或加载失败）")
            except Exception as e:
                self.logger.warning(f"akshare 适配器初始化失败: {e}")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type((
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        )),
        before_sleep=before_sleep_log(logging.getLogger('tenacity'), logging.WARNING),
        reraise=False
    )
    def _fetch_all_with_retry(self) -> Optional[pd.DataFrame]:
        """获取全部 A 股实时行情（带重试）"""
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

        response = self._session.get(url, params=params, headers=headers, timeout=10)
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
                    # 降级：手动保证基础字段  # 安全忽略
                    df[ColumnNames.PRICE] = df[ColumnNames.CLOSE]

            return df

        return None

    def fetch_all(self) -> Optional[pd.DataFrame]:
        """获取全部 A 股实时行情"""
        now = time.time()
        if self._cache_data is not None and (now - self._cache_time) < self._cache_ttl:
            return self._cache_data

        try:
            df = self._fetch_all_with_retry()
            if df is not None and not df.empty:
                self._cache_data = df
                self._cache_time = now
            return df
        except Exception as e:
            self.logger.error(f"获取行情失败(重试后仍异常): {e}")
            return None

    def get_stock_data(self, code: str, name: str = None) -> Optional[Dict]:
        """
获取单只股票完整数据（统一 Schema 格式）
优化：主源失败时自动回退到腾讯备用源
        """
        full_code = str(code).strip().zfill(6)

        # 首先尝试批量接口（按 6 位代码精确匹配，避免子串误命中）
        df = self.fetch_all()
        if df is not None:
            norm = df[ColumnNames.CODE].astype(str).str.strip().str.zfill(6)
            matches = df[norm == full_code]
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
                    'prev_close': float(row.get('prev_close', 0))
                }

        # 北交所股票用单股接口
        if full_code.startswith('920'):
            result = self._get_bse_realtime(full_code, name)
            if result:
                return result
            return self._get_backup_stock_data(full_code, name)

        # 首先尝试东方财富单股接口
        result = self._get_single_stock_realtime(full_code, name)
        if result:
            return result

        # 主源失败，回退到腾讯备用源
        self.logger.warning(f"东方财富获取 {code} 失败，尝试备用源...")
        backup_result = self._get_backup_stock_data(full_code, name)
        if backup_result:
            return backup_result

        # 第三级数据源：akshare 买卖盘接口
        if self._akshare_fetcher and self._akshare_fetcher.is_available:
            self.logger.warning(f"腾讯备用源获取 {code} 失败，尝试 akshare...")
            akshare_result = self._akshare_fetcher.get_stock_data(code, name)
            if akshare_result:
                return akshare_result

        return None

    def _get_backup_stock_data(self, code: str, name: str = None) -> Optional[Dict]:
        """
备用数据源：腾讯财经接口
优点：稳定性高，响应快
        """
        try:
            # 构建腾讯股票代码
            if code.startswith('6'):
                tencent_code = f"sh{code}"
            # 北交所: 8/9 开头的 6 位代码
            elif len(code) == 6 and (code.startswith('8') or code.startswith('9')):
                tencent_code = f"bj{code}"
            else:
                tencent_code = f"sz{code}"

            url = f"https://qt.gtimg.cn/q={tencent_code}"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }

            # 备用源超时更短，快速失败
            response = self._backup_session.get(url, headers=headers, timeout=5)
            response.encoding = 'gbk'
            text = response.text

            if not text or '~' not in text:
                return None

            # 解析腾讯格式: v_sh600000="1~浦发银行~600000~10.50~..."
            parts = text.split('"')[1].split('~')
            if len(parts) < 45:
                return None

            return {
                'code': code,
                'name': parts[1] if parts[1] else name,
                'price': float(parts[3]),
                'open': float(parts[5]),
                'high': float(parts[33]),
                'low': float(parts[34]),
                'volume': int(float(parts[36])),
                'prev_close': float(parts[4]),
                'close': float(parts[3]),  # 兼容字段
            }
        except Exception as e:
            self.logger.warning(f"备用源获取 {code} 失败: {e}")
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type((
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        )),
        before_sleep=before_sleep_log(logging.getLogger('tenacity'), logging.WARNING),
        reraise=False
    )
    def _get_bse_realtime_with_retry(self, code: str, name: str) -> Optional[Dict]:
        """获取北交所股票实时数据（带重试）"""
        url = 'http://push2.eastmoney.com/api/qt/stock/get'
        params = {
            'secid': f'0.{code}',
            'fields': 'f43,f44,f45,f46,f47,f48,f60,f14',
            '_': str(int(time.time() * 1000))
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'http://quote.eastmoney.com/',
        }

        response = self._session.get(url, params=params, headers=headers, timeout=10)
        data = response.json()

        if data.get('data'):
            d = data['data']
            close_price = d.get('f43', 0) / 100
            return {
                'code': code,
                'name': d.get('f14', name),
                'close': close_price,
                'price': close_price,  # 兼容旧代码
                'open': d.get('f44', 0) / 100,
                'high': d.get('f45', 0) / 100,
                'low': d.get('f46', 0) / 100,
                'volume': d.get('f47', 0),
                'prev_close': d.get('f60', 0) / 100,
            }
        return None

    def _get_bse_realtime(self, code: str, name: str) -> Optional[Dict]:
        """获取北交所股票实时数据"""
        try:
            return self._get_bse_realtime_with_retry(code, name)
        except Exception as e:
            self.logger.warning(f"获取北交所股票 {code} 失败(重试后): {e}")
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type((
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        )),
        before_sleep=before_sleep_log(logging.getLogger('tenacity'), logging.WARNING),
        reraise=False
    )
    def _get_single_stock_realtime_with_retry(self, code: str, name: str) -> Optional[Dict]:
        """获取单只股票实时数据（带重试）"""
        if code.startswith('6'):
            secid = f'1.{code}'
        else:
            secid = f'0.{code}'

        url = 'http://push2.eastmoney.com/api/qt/stock/get'
        params = {
            'secid': secid,
            'fields': 'f43,f44,f45,f46,f47,f48,f60,f14',
            '_': str(int(time.time() * 1000))
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'http://quote.eastmoney.com/',
        }

        response = self._session.get(url, params=params, headers=headers, timeout=10)
        data = response.json()

        if data.get('data'):
            d = data['data']
            close_price = d.get('f43', 0) / 100
            return {
                'code': code,
                'name': d.get('f14', name),
                'close': close_price,
                'price': close_price,  # 兼容旧代码
                'open': d.get('f44', 0) / 100,
                'high': d.get('f45', 0) / 100,
                'low': d.get('f46', 0) / 100,
                'volume': d.get('f47', 0),
                'prev_close': d.get('f60', 0) / 100,
            }
        return None

    def _get_single_stock_realtime(self, code: str, name: str) -> Optional[Dict]:
        """获取单只股票实时数据（通用）"""
        try:
            return self._get_single_stock_realtime_with_retry(code, name)
        except Exception as e:
            self.logger.warning(f"获取股票 {code} 失败(重试后): {e}")
            return None

    def _try_load_parquet(self, code: str, days: int) -> Optional[pd.DataFrame]:
        """优先从 Parquet 缓存加载 K 线数据。"""
        from radar.data.ohlcv_parquet_store import (
            max_age_days_from_env,
            prefer_read_enabled,
            try_load_kline_parquet,
        )
        if not prefer_read_enabled():
            return None
        return try_load_kline_parquet(
            code, min_rows=max(30, days // 2), max_age_days=max_age_days_from_env()
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type((
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        )),
        before_sleep=before_sleep_log(logging.getLogger('tenacity'), logging.WARNING),
        reraise=False
    )
    def _fetch_eastmoney_kline(self, code: str, days: int) -> Optional[pd.DataFrame]:
        """从东方财富 push2his 接口拉取 K 线数据。"""
        full_code = code.zfill(6)
        secid = f"1.{full_code}" if full_code.startswith("6") else f"0.{full_code}"
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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "http://quote.eastmoney.com/",
        }
        response = self._session.get(url, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        klines = data.get("data", {}).get("klines", [])
        if not klines:
            return None
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

    def _normalize_and_persist(self, code: str, df: pd.DataFrame) -> pd.DataFrame:
        """标准化 Schema、附加血缘并持久化到 Parquet。"""
        from radar.data.ohlcv_parquet_store import maybe_persist_fetched_kline

        if SCHEMA_AVAILABLE:
            try:
                df = normalize_dataframe(df, source="eastmoney_kline")
                if attach_lineage is not None:
                    df = attach_lineage(
                        df,
                        source="eastmoney_push2his",
                        pipeline="MarketDataFetcher.get_historical_data",
                        adjustment="qfq",
                    )
                maybe_persist_fetched_kline(code, df, persist_reason="market_fetcher_eastmoney")
                return df
            except (ValueError, TypeError):
                # Schema 标准化失败降级到手动处理  # 安全忽略
                pass

        # 降级：手动标准化
        df["price"] = df["close"].astype(float)
        df["volume"] = df["volume"].astype(int)
        df["date"] = pd.to_datetime(df["date"])
        if attach_lineage is not None:
            df = attach_lineage(
                df,
                source="eastmoney_push2his",
                pipeline="MarketDataFetcher.get_historical_data",
                adjustment="qfq",
            )
        maybe_persist_fetched_kline(code, df, persist_reason="market_fetcher_eastmoney")
        return df

    def get_historical_data(self, code: str, days: int = 60) -> pd.DataFrame:
        """获取历史K线数据（东财 push2his）。可选优先读 Parquet，见 ohlcv_parquet_store。"""
        cached = self._try_load_parquet(code, days)
        if cached is not None:
            return cached

        try:
            df = self._fetch_eastmoney_kline(code, days)
            if df is not None:
                return self._normalize_and_persist(code, df)
            self.logger.warning(f"获取历史数据失败: {code} (无数据)")
        except Exception as e:
            self.logger.error(f"获取历史数据失败 {code}: {e}")
            # 网络失败时，若本地 Parquet 存在则作为最终降级返回
            fallback = self._try_load_parquet(code, days)
            if fallback is not None:
                self.logger.warning(f"网络失败，降级使用本地 Parquet 缓存: {code}")
                return fallback
        return pd.DataFrame()
    @staticmethod
    def _safe_float(val: Any, default: float = 0.0) -> float:
        if val is None or val == '' or val == '-':
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _safe_int(val: Any, default: int = 0) -> int:
        if val is None or val == '' or val == '-':
            return default
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return default
