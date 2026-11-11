#!/usr/bin/env python3
"""
数据源管理器 V3 - 统一数据源入口
职责：
1. 多源统一管理（主/备/本地/缓存）
2. 连接池复用，减少TCP开销
3. 502快速检测，智能降级
4. 数据新鲜度标记
"""

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Dict, List, Optional

import pandas as pd

from radar.data.api_protection import ProtectedMarketDataFetcher
from radar.data.market_data_v2 import MarketDataFetcher
from radar.data.session_pool import create_pooled_session
from radar.schemas.data_schema import ColumnNames


class DataSource:
    """数据源基类"""

    FAILURE_THRESHOLD = 3
    RECOVERY_TIMEOUT = 30.0

    def __init__(self, name: str, priority: int, logger: logging.Logger):
        self.name = name
        self.priority = priority
        self.logger = logger
        self._last_success = 0.0
        self._failure_count = 0
        self._healthy = True
        self._disabled_at = 0.0
        self._health_lock = threading.Lock()
        self._half_open_probe = False
        self._half_open_owner: Optional[int] = None

    @property
    def is_healthy(self) -> bool:
        with self._health_lock:
            return self._healthy or (
                not self._half_open_probe
                and time.monotonic() - self._disabled_at >= self.RECOVERY_TIMEOUT
            )

    def can_attempt(self) -> bool:
        """Reserve at most one half-open probe after the recovery timeout."""
        with self._health_lock:
            if self._healthy:
                return True
            if (
                not self._half_open_probe
                and time.monotonic() - self._disabled_at >= self.RECOVERY_TIMEOUT
            ):
                self._half_open_probe = True
                self._half_open_owner = threading.get_ident()
                return True
            return False

    def mark_success(self):
        """Record adapter success without releasing a reserved half-open probe."""
        with self._health_lock:
            self._last_success = time.time()
            if self._half_open_probe:
                # Adapters validate transport/protocol details, while the
                # manager still has to validate symbol, price and freshness.
                # Keep the probe reserved until that second validation passes.
                return
            self._failure_count = 0
            self._healthy = True
            self._disabled_at = 0.0
            self._half_open_owner = None

    def mark_validated_success(self, half_open_attempt: bool = False) -> None:
        """Commit success only after manager-level quote validation."""
        with self._health_lock:
            self._last_success = time.time()
            if self._half_open_probe:
                if not half_open_attempt or self._half_open_owner != threading.get_ident():
                    return
                self._half_open_probe = False
                self._half_open_owner = None
            self._failure_count = 0
            self._healthy = True
            self._disabled_at = 0.0

    def mark_failure(self):
        with self._health_lock:
            if self._half_open_probe:
                if self._half_open_owner != threading.get_ident():
                    # Do not let an older in-flight request steal another
                    # thread's reserved recovery probe.
                    return
                # A failed half-open probe immediately reopens the source.
                self._half_open_probe = False
                self._half_open_owner = None
                self._failure_count = self.FAILURE_THRESHOLD
                self._healthy = False
                self._disabled_at = time.monotonic()
                return
            self._failure_count += 1
            if self._failure_count >= self.FAILURE_THRESHOLD:
                self._healthy = False
                self._disabled_at = time.monotonic()

    def record_validation_failure(
        self,
        previous_count: int,
        half_open_attempt: bool,
    ) -> None:
        """Let manager validation be authoritative for an attempted quote."""
        with self._health_lock:
            if half_open_attempt:
                self._half_open_probe = False
                self._half_open_owner = None
                self._failure_count = self.FAILURE_THRESHOLD
                self._healthy = False
                self._disabled_at = time.monotonic()
                return
            elif self._failure_count <= previous_count:
                # An adapter may optimistically call mark_success() before the
                # manager rejects its payload.  Preserve the attempted
                # failure instead of letting that reset hide bad data.
                self._failure_count = previous_count + 1
                if self._failure_count >= self.FAILURE_THRESHOLD:
                    self._healthy = False
                    self._disabled_at = time.monotonic()

    def fetch_single(self, code: str, name: str) -> Optional[Dict]:
        raise NotImplementedError

    def fetch_batch(self) -> Optional[pd.DataFrame]:
        raise NotImplementedError


class EastMoneySource(DataSource):
    """东方财富数据源（腾讯、新浪失败后的降级源）"""

    def __init__(self, logger: logging.Logger, api_key: str = ""):
        super().__init__("eastmoney", 3, logger)
        base = MarketDataFetcher(logger, api_key, use_akshare=True)
        self._fetcher = ProtectedMarketDataFetcher(
            fetcher=base, logger=logger,
            failure_threshold=5, recovery_timeout=30.0
        )
        self._session = create_pooled_session()
        self._batch_cache: Optional[pd.DataFrame] = None
        self._batch_cache_time = 0.0
        self._batch_ttl = 3.0
        self._row_index: Optional[Dict[str, int]] = None
        self._batch_lock = threading.Lock()

    def _update_batch_cache(self) -> bool:
        """更新批量缓存"""
        now = time.time()
        base_fetcher = getattr(self._fetcher, 'fetcher', None)
        upstream_ttl_getter = getattr(base_fetcher, '_get_cache_ttl', None)
        current_ttl = (
            float(upstream_ttl_getter())
            if callable(upstream_ttl_getter)
            else self._batch_ttl
        )
        with self._batch_lock:
            if (
                self._batch_cache is not None
                and now - self._batch_cache_time <= current_ttl
            ):
                return True

        try:
            df = self._fetcher.fetch_all()
            if df is not None and not df.empty:
                upstream_time = float(
                    getattr(base_fetcher, '_cache_time', now) or 0.0
                )
                upstream_ttl = (
                    float(upstream_ttl_getter())
                    if callable(upstream_ttl_getter)
                    else self._batch_ttl
                )
                if upstream_time <= 0 or now - upstream_time > upstream_ttl:
                    self.logger.debug("东财批量缓存已过期，拒绝重新标记为实时行情")
                    return False
                new_index = self._build_index(df)
                with self._batch_lock:
                    # Publish the DataFrame and matching index as one state.
                    self._batch_cache = df
                    self._batch_cache_time = upstream_time
                    self._batch_ttl = upstream_ttl
                    self._row_index = new_index
                self.mark_success()
                return True
        except Exception as e:
            self.logger.debug(f"东财批量获取失败: {e}")
            self.mark_failure()

        return False

    @staticmethod
    def _build_index(dataframe: pd.DataFrame) -> Dict[str, int]:
        """Build an immutable-by-convention code-to-row index locally."""
        index: Dict[str, int] = {}
        col = ColumnNames.CODE
        if dataframe is None or col not in dataframe.columns:
            return index
        for i, raw in enumerate(dataframe[col].astype(str)):
            key = raw.strip().zfill(6)
            if key not in index:
                index[key] = i
        return index

    def _get_from_cache(self, code: str) -> Optional[Dict]:
        """从缓存获取"""
        try:
            expected = str(code).strip().zfill(6)
            with self._batch_lock:
                if self._batch_cache is None or self._row_index is None:
                    return None
                row_idx = self._row_index.get(expected)
                if row_idx is None:
                    return None
                row = self._batch_cache.iloc[row_idx].copy()
                cache_time = self._batch_cache_time
                batch_ttl = self._batch_ttl
            actual = str(row.get(ColumnNames.CODE, '')).strip().zfill(6)
            if actual != expected:
                self.logger.warning("东财批量索引代码不一致，拒绝行情: %s", expected)
                return None
            age = max(0.0, time.time() - cache_time)
            base_fetcher = getattr(self._fetcher, 'fetcher', None)
            upstream_ttl_getter = getattr(base_fetcher, '_get_cache_ttl', None)
            current_ttl = (
                float(upstream_ttl_getter())
                if callable(upstream_ttl_getter)
                else batch_ttl
            )
            fresh = age <= current_ttl
            return {
                'code': code,
                'name': row[ColumnNames.NAME],
                'price': float(row[ColumnNames.CLOSE]),
                'volume': int(row.get(ColumnNames.VOLUME, 0)),
                'open': float(row.get(ColumnNames.OPEN, 0)),
                'high': float(row.get(ColumnNames.HIGH, 0)),
                'low': float(row.get(ColumnNames.LOW, 0)),
                'prev_close': float(row.get('prev_close', 0)),
                'timestamp': cache_time,
                'source': self.name,
                'fresh': fresh,
                'stale': not fresh,
                'data_age_seconds': age,
            }
        except Exception as e:
            self.logger.debug(f"缓存解析失败 {code}: {e}")
            return None

    def fetch_single(self, code: str, name: str) -> Optional[Dict]:
        """单股获取（优先缓存）"""
        # 先尝试批量缓存
        if self._update_batch_cache():
            data = self._get_from_cache(code)
            if data:
                return data

        # 缓存缺失，直接单查
        try:
            data = self._fetcher.get_stock_data(code, name)
            if data:
                # MarketDataFetcher may itself fall back to Tencent/AkShare.
                # Preserve that real lineage and any stale marker it returns.
                data.setdefault('timestamp', time.time())
                data.setdefault('source', 'eastmoney_chain')
                data.setdefault('fresh', True)
                self.mark_success()
                return data
        except Exception as e:
            self.logger.debug(f"东财单查失败 {code}: {e}")
            self.mark_failure()

        return None

    def fetch_batch(self) -> Optional[pd.DataFrame]:
        """批量获取"""
        if self._update_batch_cache():
            return self._batch_cache
        return None


class TencentSource(DataSource):
    """腾讯数据源（实时行情主源）"""

    def __init__(self, logger: logging.Logger):
        super().__init__("tencent", 1, logger)
        self._session = create_pooled_session({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

    def fetch_single(self, code: str, name: str) -> Optional[Dict]:
        """腾讯单股接口。解析失败必须返回 None，禁止用 0 价占位。"""
        try:
            full_code = str(code).strip().zfill(6)
            if full_code.startswith('6'):
                tencent_code = f"sh{full_code}"
            elif full_code.startswith(('8', '9')):
                tencent_code = f"bj{full_code}"
            else:
                tencent_code = f"sz{full_code}"

            url = f"https://qt.gtimg.cn/q={tencent_code}"
            resp = self._session.get(url, timeout=5)
            if resp.status_code != 200:
                self.mark_failure()
                return None

            for encoding in ('gb18030', 'gbk', 'gb2312', 'utf-8'):
                try:
                    resp.encoding = encoding
                    text = resp.text
                    break
                except Exception:
                    continue
            else:
                text = resp.text

            if not text or '"' not in text or '~' not in text:
                self.mark_failure()
                return None

            parts = text.split('"', 2)[1].split('~')
            if len(parts) < 37:
                self.mark_failure()
                return None

            response_code = str(parts[2]).strip().zfill(6)
            if response_code != full_code:
                self.mark_failure()
                return None
            price = float(parts[3] or 0)
            if not math.isfinite(price) or price <= 0:
                self.mark_failure()
                return None

            self.mark_success()
            return {
                'code': full_code,
                'name': parts[1] or name,
                'price': price,
                'close': price,
                'open': float(parts[5] or 0),
                'high': float(parts[33] or 0),
                'low': float(parts[34] or 0),
                'prev_close': float(parts[4] or 0),
                'volume': int(float(parts[36] or 0)),
                'timestamp': time.time(),
                'source': self.name,
                'fresh': True,
            }
        except Exception as e:
            self.logger.debug(f"腾讯获取失败 {code}: {e}")
            self.mark_failure()
        return None

    def fetch_batch(self, codes: List[str] = None) -> Optional[pd.DataFrame]:
        """腾讯批量接口 - 一次请求获取多只股票"""
        if codes is None:
            return None
        try:
            # 构建腾讯代码格式
            tencent_codes = []
            requested_codes = set()
            for code in codes:
                code = str(code).strip().zfill(6)
                requested_codes.add(code)
                if code.startswith('6'):
                    tencent_codes.append(f"sh{code}")
                elif code.startswith(('8', '9')):
                    tencent_codes.append(f"bj{code}")
                else:
                    tencent_codes.append(f"sz{code}")

            url = f"http://qt.gtimg.cn/q={','.join(tencent_codes)}"
            response = self._session.get(url, timeout=8)

            if response.status_code != 200:
                return None

            # 解码
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

            # 解析多只股票数据
            rows = []
            for line in text.strip().split(';'):
                try:
                    if not line or '"' not in line:
                        continue
                    parts = line.split('"', 2)[1].split('~')
                    if len(parts) < 45:
                        continue

                    # A malformed row must not discard other valid symbols;
                    # the manager will retry only the missing symbols.
                    raw_code = str(parts[2]).strip().zfill(6)
                    price = float(parts[3])
                    if (
                        raw_code not in requested_codes
                        or not math.isfinite(price)
                        or price <= 0
                    ):
                        continue

                    rows.append({
                        ColumnNames.CODE: raw_code,
                        ColumnNames.NAME: parts[1],
                        ColumnNames.CLOSE: price,
                        ColumnNames.PRICE: price,
                        ColumnNames.OPEN: float(parts[5] or 0),
                        ColumnNames.HIGH: float(parts[33] or 0),
                        ColumnNames.LOW: float(parts[34] or 0),
                        ColumnNames.VOLUME: int(float(parts[36] or 0)),
                        'prev_close': float(parts[4] or 0),
                        ColumnNames.CHANGE_PCT: float(parts[32] or 0),
                    })
                except (IndexError, TypeError, ValueError, OverflowError):
                    continue

            if rows:
                df = pd.DataFrame(rows)
                self.mark_success()
                return df

        except Exception as e:
            self.logger.debug(f"腾讯批量获取失败: {e}")
            self.mark_failure()

        return None


class AkshareSource(DataSource):
    """akshare本地数据源"""

    def __init__(self, logger: logging.Logger):
        super().__init__("akshare", 4, logger)
        self._ak = None
        self._init_akshare()

    def _init_akshare(self):
        try:
            import akshare as ak
            self._ak = ak
            self.logger.info("akshare 数据源已加载")
        except ImportError:
            self.logger.warning("akshare 未安装")

    def fetch_single(self, code: str, name: str) -> Optional[Dict]:
        if self._ak is None:
            return None
        try:
            # 使用akshare获取实时行情
            df = self._ak.stock_zh_a_spot_em()
            row = df[df['代码'] == code]
            if not row.empty:
                self.mark_success()
                return {
                    'code': code,
                    'name': name,
                    'price': float(row['最新价'].iloc[0]),
                    'volume': int(row['成交量'].iloc[0]),
                    'timestamp': time.time(),
                    'source': self.name,
                    'fresh': True,
                }
        except Exception as e:
            self.logger.debug(f"akshare获取失败 {code}: {e}")
            self.mark_failure()
        return None

    def fetch_batch(self) -> Optional[pd.DataFrame]:
        if self._ak is None:
            return None
        try:
            df = self._ak.stock_zh_a_spot_em()
            self.mark_success()
            return df
        except Exception as e:
            self.logger.debug(f"akshare批量获取失败: {e}")
            self.mark_failure()
        return None


class SinaSource(DataSource):
    """新浪财经数据源（备用2）"""

    def __init__(self, logger: logging.Logger):
        super().__init__("sina", 2, logger)
        self._session = create_pooled_session({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://finance.sina.com.cn'
        })

    def fetch_single(self, code: str, name: str) -> Optional[Dict]:
        """新浪单股接口"""
        try:
            full_code = str(code).strip().zfill(6)
            if full_code.startswith('6'):
                sina_code = f"sh{full_code}"
            elif full_code.startswith(('8', '9')):
                sina_code = f"bj{full_code}"
            else:
                sina_code = f"sz{full_code}"

            url = f"https://hq.sinajs.cn/list={sina_code}"
            resp = self._session.get(url, timeout=5)
            if resp.status_code != 200:
                self.mark_failure()
                return None
            text = resp.text
            lhs, separator, _ = text.partition('=')
            if (
                not separator
                or not lhs.strip().endswith(f"hq_str_{sina_code}")
                or '"' not in text
            ):
                self.mark_failure()
                return None
            data_part = text.split('"', 2)[1]
            parts = data_part.split(',')
            if len(parts) < 9:
                self.mark_failure()
                return None
            price = float(parts[3]) if parts[3] else 0.0
            if not math.isfinite(price) or price <= 0:
                self.mark_failure()
                return None
            self.mark_success()
            return {
                'code': full_code,
                'name': parts[0] if parts[0] else name,
                'price': price,
                'open': float(parts[1]) if parts[1] else 0.0,
                'high': float(parts[4]) if parts[4] else 0.0,
                'low': float(parts[5]) if parts[5] else 0.0,
                'volume': int(float(parts[8])) if parts[8] else 0,
                'prev_close': float(parts[2]) if parts[2] else 0.0,
                'timestamp': time.time(),
                'source': self.name,
                'fresh': True,
            }
        except Exception as e:
            self.logger.debug(f"新浪获取失败 {code}: {e}")
            self.mark_failure()
        return None

    def fetch_batch(self) -> Optional[pd.DataFrame]:
        return None


class CacheSource(DataSource):
    """缓存数据源（最后防线）"""

    def __init__(self, logger: logging.Logger, max_age: float = 300.0):
        super().__init__("cache", 99, logger)
        self._cache: Dict[str, Dict] = {}
        self._max_age = max_age  # 5分钟过期

    def put(self, code: str, data: Dict):
        """存入缓存"""
        self._cache[code] = {
            **data,
            'upstream_source': data.get('upstream_source', data.get('source')),
            'cached_at': time.time(),
        }

    def fetch_single(self, code: str, name: str) -> Optional[Dict]:
        cached = self._cache.get(code)
        if cached is None:
            return None

        age = time.time() - cached.get('cached_at', 0)
        if age > self._max_age:
            return None

        return {
            **cached,
            'source': self.name,
            'fresh': False,
            'stale': True,
            'cache_age': age,
        }

    def fetch_batch(self) -> Optional[pd.DataFrame]:
        return None


class DataSourceManager:
    """
    数据源管理器 V3

    职责：
    1. 管理多个数据源优先级
    2. 自动降级切换
    3. 统一返回格式
    4. 缓存管理
    """

    def __init__(self, logger: logging.Logger, max_workers: int = 5):
        self.logger = logger
        self._sources: List[DataSource] = []
        self._cache = CacheSource(logger)
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ds_")
        # Bound a whole parallel scan, rather than waiting up to 10 seconds
        # for every future in submission order during an upstream outage.
        self._parallel_timeout = 30.0

        # 注册数据源
        self._register_default_sources()

    def _register_default_sources(self):
        """注册默认数据源 - 腾讯优先，东财降级为备选"""
        self._sources.append(TencentSource(self.logger))   # priority=1 (主源)
        self._sources.append(SinaSource(self.logger))      # priority=2
        self._sources.append(EastMoneySource(self.logger)) # priority=3 (降级)
        self._sources.append(AkshareSource(self.logger))   # priority=4
        # cache不参与优先级排序，作为最后防线

    @staticmethod
    def _normalize_code(code: object) -> str:
        raw = str(code or '').strip()
        if raw.isdigit() and len(raw) <= 6:
            return raw.zfill(6)
        return raw

    @classmethod
    def _validate_quote(
        cls,
        requested_code: str,
        data: object,
        *,
        source_name: str,
        allow_stale: bool = False,
    ) -> Optional[Dict]:
        """Normalize and validate a quote before it reaches cache/strategies."""
        if not isinstance(data, dict):
            return None

        expected = cls._normalize_code(requested_code)
        actual = cls._normalize_code(data.get('code'))
        if len(expected) != 6 or not expected.isdigit() or actual != expected:
            return None

        try:
            price = float(data.get('price'))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(price) or price <= 0:
            return None

        stale = bool(data.get('stale') or data.get('is_stale'))
        if not allow_stale and (stale or data.get('fresh') is False):
            return None

        validated = dict(data)
        validated['code'] = expected
        validated['price'] = price
        validated.setdefault('source', source_name)
        validated.setdefault('fresh', not stale)
        validated.setdefault('timestamp', time.time())
        return validated

    @staticmethod
    def _mark_failure_if_needed(
        source: object,
        previous_count: object,
        half_open_attempt: bool = False,
    ) -> None:
        """Count a failed/invalid response once even if an adapter forgot to."""
        recorder = getattr(source, 'record_validation_failure', None)
        if callable(recorder) and isinstance(previous_count, int):
            recorder(previous_count, half_open_attempt)
            return
        marker = getattr(source, 'mark_failure', None)
        if not callable(marker):
            return
        current_count = getattr(source, '_failure_count', None)
        if previous_count is None or current_count == previous_count:
            marker()

    @staticmethod
    def _source_can_attempt(source: object) -> bool:
        checker = getattr(source, 'can_attempt', None)
        if callable(checker):
            return bool(checker())
        return bool(getattr(source, 'is_healthy', False))

    @staticmethod
    def _mark_validated_success(
        source: object,
        half_open_attempt: bool = False,
    ) -> None:
        marker = getattr(source, 'mark_validated_success', None)
        if callable(marker):
            marker(half_open_attempt)
            return
        marker = getattr(source, 'mark_success', None)
        if callable(marker):
            marker()

    def fetch_single(self, code: str, name: str) -> Optional[Dict]:
        """
        获取单只股票数据
        按优先级尝试各数据源，成功则更新缓存
        """
        code = self._normalize_code(code)
        if len(code) != 6 or not code.isdigit():
            return None

        # 按优先级尝试各源
        for source in sorted(self._sources, key=lambda s: s.priority):
            if not self._source_can_attempt(source):
                continue

            previous_count = getattr(source, '_failure_count', None)
            half_open_attempt = bool(getattr(source, '_half_open_probe', False))
            try:
                data = source.fetch_single(code, name)
                validated = self._validate_quote(
                    code,
                    data,
                    source_name=source.name,
                )
                if validated is not None:
                    self._mark_validated_success(source, half_open_attempt)
                    self._cache.put(code, validated)
                    return validated
                self._mark_failure_if_needed(
                    source,
                    previous_count,
                    half_open_attempt,
                )
            except Exception as e:
                self.logger.debug(f"数据源 {source.name} 获取 {code} 失败: {e}")
                self._mark_failure_if_needed(
                    source,
                    previous_count,
                    half_open_attempt,
                )
                continue

        # 所有源都失败，返回仍在最大年龄内、但显式标记 stale 的缓存。
        # Network calls may take long enough for the previously-read cache to
        # expire, so re-read and revalidate its age at fallback time.
        cached = self._cache.fetch_single(code, name)
        cached = self._validate_quote(
            code,
            cached,
            source_name='cache',
            allow_stale=True,
        )
        if cached is not None:
            self.logger.warning(f"{code} 使用缓存数据（{cached.get('cache_age', 0):.0f}秒）")
            return cached

        return None

    def fetch_multiple(self, stocks: List[tuple]) -> Dict[str, Dict]:
        """
        批量获取多只股票
        智能策略：
        - n <= 15: 并行单股
        - n > 15: 批量优先+补充
        """
        if not stocks:
            return {}

        stocks = [(self._normalize_code(code), name) for code, name in stocks]

        n = len(stocks)

        if n <= 15:
            # 并行单股
            return self._fetch_parallel(stocks)
        else:
            # 批量优先
            return self._fetch_batch_first(stocks)

    def _fetch_parallel(self, stocks: List[tuple]) -> Dict[str, Dict]:
        """并行单股获取"""
        results = {}
        futures = {}

        for code, name in stocks:
            future = self._executor.submit(self.fetch_single, code, name)
            futures[future] = code

        completed, pending = wait(
            tuple(futures),
            timeout=self._parallel_timeout,
        )
        for future in pending:
            # Running network calls cannot be force-killed safely, but queued
            # calls are cancelled and the current scan returns on deadline.
            future.cancel()
        if pending:
            self.logger.warning(
                "并行行情获取达到 %.0f 秒总时限，跳过 %d 个未完成标的",
                self._parallel_timeout,
                len(pending),
            )

        # Preserve target insertion order for callers that serialize the dict.
        for future in futures:
            if future not in completed:
                continue
            code = futures[future]
            try:
                data = future.result()
                if data:
                    results[code] = data
            except Exception as e:
                self.logger.debug(f"并行获取 {code} 失败: {e}")

        return results

    def _fetch_batch_first(self, stocks: List[tuple]) -> Dict[str, Dict]:
        """批量优先获取 - 使用腾讯批量接口"""
        codes = [code for code, _ in stocks]

        # 尝试腾讯批量
        tencent = None
        for s in self._sources:
            if s.name == "tencent":
                tencent = s
                break

        if tencent and self._source_can_attempt(tencent):
            previous_count = getattr(tencent, '_failure_count', None)
            half_open_attempt = bool(getattr(tencent, '_half_open_probe', False))
            try:
                df = tencent.fetch_batch(codes)
                required = {ColumnNames.CODE, ColumnNames.NAME, ColumnNames.CLOSE}
                if df is None or df.empty or not required.issubset(df.columns):
                    raise ValueError("腾讯批量响应为空或缺少必需字段")

                results: Dict[str, Dict] = {}
                missing = []
                normalized_codes = (
                    df[ColumnNames.CODE].astype(str).str.strip().str.zfill(6)
                )
                for code, name in stocks:
                    try:
                        row = df[normalized_codes == code]
                        if row.empty:
                            missing.append((code, name))
                            continue

                        def first(column: str, default: object = 0) -> object:
                            return row[column].iloc[0] if column in row.columns else default

                        data = {
                            'code': code,
                            'name': first(ColumnNames.NAME, name),
                            'price': float(first(ColumnNames.CLOSE)),
                            'volume': int(float(first(ColumnNames.VOLUME))),
                            'open': float(first(ColumnNames.OPEN)),
                            'high': float(first(ColumnNames.HIGH)),
                            'low': float(first(ColumnNames.LOW)),
                            'prev_close': float(first('prev_close')),
                            'timestamp': time.time(),
                            'source': 'tencent',
                            'fresh': True,
                        }
                        validated = self._validate_quote(
                            code,
                            data,
                            source_name='tencent',
                        )
                        if validated is None:
                            missing.append((code, name))
                            continue
                        results[code] = validated
                        self._cache.put(code, validated)
                    except (KeyError, TypeError, ValueError, OverflowError):
                        missing.append((code, name))

                if results:
                    self._mark_validated_success(tencent, half_open_attempt)
                else:
                    self._mark_failure_if_needed(
                        tencent,
                        previous_count,
                        half_open_attempt,
                    )
                if missing:
                    extra = self._fetch_parallel(missing)
                    results.update(extra)
                return results
            except Exception as e:
                self.logger.debug(f"腾讯批量响应无效，降级到单股链路: {e}")
                self._mark_failure_if_needed(
                    tencent,
                    previous_count,
                    half_open_attempt,
                )

        # 批量失败，降级到并行单股
        return self._fetch_parallel(stocks)

    def get_source_status(self) -> List[Dict]:
        """获取各数据源状态"""
        return [
            {
                'name': s.name,
                'healthy': s.is_healthy,
                'priority': s.priority,
                'failures': s._failure_count,
            }
            for s in self._sources
        ]

    def shutdown(self):
        """关闭线程池"""
        self._executor.shutdown(wait=False)
        self.logger.info("数据源管理器已关闭")
