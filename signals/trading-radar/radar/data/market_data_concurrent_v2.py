"""
增强型并发行情获取器 - 重构 ConcurrentMarketDataFetcher
核心改进：
1. 批量缓存行级索引（code -> row_idx），O(1) 查找
2. 小批量场景直接并行单股，避免全市场拉取 overhead
3. 线程池优雅关闭（wait=False，避免阻塞）
4. fetch_multiple 统一入口，自动选择最优策略
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from radar.data.api_protection import ProtectedMarketDataFetcher
from radar.data.market_data_v2 import MarketDataFetcher
from radar.schemas.data_schema import ColumnNames


class ConcurrentMarketDataFetcher:
    """
    智能并发行情获取器 V2

    策略选择：
    - n <= 15:  并行单股接口（避免全市场 5000 只 overhead）
    - n > 100:  批量接口 + 缓存索引
    - 15 < n <= 100: 批量接口优先，缓存缺失的补充单查

    性能优化：
    - 批量缓存行级索引：code -> row_idx，O(1) 查找
    - 线程池线程名前缀，便于调试
    - 超时 future 自动取消，避免泄漏
    """

    # 策略切换阈值
    BATCH_THRESHOLD_SMALL = 15   # 小于此值用并行单股
    BATCH_THRESHOLD_LARGE = 100    # 大于此值用批量接口

    # 批量缓存 TTL（秒）
    BATCH_CACHE_TTL = 3.0

    def __init__(self, logger: logging.Logger, api_key: str = "",
                 max_workers: int = 5, timeout: float = 15.0,
                 on_circuit_open: callable = None, on_circuit_close: callable = None):
        self.logger = logger
        self.api_key = api_key
        self.timeout = timeout

        # 基础获取器 + 保护包装
        base_fetcher = MarketDataFetcher(logger, api_key, use_akshare=True)
        self._fetcher = ProtectedMarketDataFetcher(
            fetcher=base_fetcher,
            logger=logger,
            failure_threshold=5,
            recovery_timeout=30.0
        )

        # 设置数据源异常告警回调
        if on_circuit_open or on_circuit_close:
            self._fetcher.set_alert_callbacks(on_circuit_open, on_circuit_close)

        # 线程池
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="market_fetch_"
        )
        self._executor_shutdown = False

        # 批量数据缓存
        self._batch_cache = None
        self._batch_cache_time = 0.0

        # 6 位代码 -> 行号索引（O(1) 查找）
        self._batch_row_index: Optional[Dict[str, int]] = None

    def _normalize_code(self, code: str) -> str:
        """标准化为 6 位代码"""
        return str(code).strip().zfill(6)

    def _rebuild_batch_index(self) -> None:
        """重建批量缓存索引"""
        self._batch_row_index = None
        if self._batch_cache is None or self._batch_cache.empty:
            return

        col = ColumnNames.CODE
        if col not in self._batch_cache.columns:
            return

        index: Dict[str, int] = {}
        for i, raw in enumerate(self._batch_cache[col].astype(str)):
            key = self._normalize_code(raw)
            if key not in index:
                index[key] = i
        self._batch_row_index = index

    def _update_batch_cache(self) -> bool:
        """更新批量缓存，返回是否成功"""
        now = time.time()
        if self._batch_cache is None or (now - self._batch_cache_time) > self.BATCH_CACHE_TTL:
            try:
                self._batch_cache = self._fetcher.fetch_all()
                self._batch_cache_time = now
                self._rebuild_batch_index()
                self.logger.debug("批量行情缓存已更新")
                return True
            except Exception as e:
                self.logger.warning(f"批量行情获取失败: {e}")
                self._batch_cache = None
                self._batch_row_index = None
                return False
        return True

    def _get_from_batch_cache(self, code: str) -> Optional[Dict]:
        """从批量缓存获取单只股票（O(1)）"""
        if self._batch_cache is None or self._batch_row_index is None:
            return None

        row_idx = self._batch_row_index.get(self._normalize_code(code))
        if row_idx is None:
            return None

        try:
            row = self._batch_cache.iloc[row_idx]
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
        except Exception as e:
            self.logger.debug(f"从缓存获取 {code} 失败: {e}")
            return None

    def _fetch_single_direct(self, code: str, name: str) -> Optional[Dict]:
        """直接调用单股接口"""
        try:
            return self._fetcher.get_stock_data(code, name)
        except Exception as e:
            self.logger.debug(f"获取 {code} 数据失败: {e}")
            return None

    def _fetch_parallel_single(self, stocks: List[tuple]) -> Dict[str, Dict]:
        """
        并行单股获取策略
        适用于小批量（<=15），避免全市场拉取 overhead
        """
        results = {}
        futures = {}

        for code, name in stocks:
            future = self._executor.submit(self._fetch_single_direct, code, name)
            futures[future] = (code, name)

        try:
            for future in as_completed(futures, timeout=self.timeout * 2):
                code, name = futures[future]
                try:
                    data = future.result(timeout=self.timeout)
                    if data:
                        results[code] = data
                except Exception as e:
                    self.logger.debug(f"{name}({code}) 获取异常: {e}")
        except Exception as e:
            self.logger.debug(f"批次获取部分超时: {e}")
        finally:
            for future in futures:
                if not future.done():
                    future.cancel()

        return results

    def _fetch_batch_first(self, stocks: List[tuple]) -> Dict[str, Dict]:
        """
        批量优先策略：先拉全市场缓存，再补充缺失
        适用于大批量（>15）
        """
        if not self._update_batch_cache():
            # 批量接口失败，降级到并行单股
            self.logger.debug("批量接口失败，降级到并行单股")
            return self._fetch_parallel_single(stocks)

        results = {}
        missing = []

        for code, name in stocks:
            data = self._get_from_batch_cache(code)
            if data:
                results[code] = data
            else:
                missing.append((code, name))

        # 补充缺失的股票
        if missing:
            self.logger.debug(f"{len(missing)} 只从批量缓存缺失，补充单查")
            extra = self._fetch_parallel_single(missing)
            results.update(extra)

        return results

    def fetch_multiple(self, stocks: List[tuple]) -> Dict[str, Dict]:
        """
        智能并发获取多只股票数据
        自动选择最优策略
        """
        if not stocks:
            return {}

        n_stocks = len(stocks)

        if n_stocks <= self.BATCH_THRESHOLD_SMALL:
            self.logger.debug(f"股票数量 {n_stocks} <= {self.BATCH_THRESHOLD_SMALL}，并行单股")
            return self._fetch_parallel_single(stocks)
        else:
            self.logger.debug(f"股票数量 {n_stocks}，批量优先")
            return self._fetch_batch_first(stocks)

    def fetch_single(self, code: str, name: str = None) -> Optional[Dict]:
        """获取单只股票"""
        results = self.fetch_multiple([(code, name or code)])
        return results.get(code)

    def get_batch_data(self) -> Optional[object]:
        """获取批量行情数据"""
        self._update_batch_cache()
        return self._batch_cache

    def shutdown(self) -> None:
        """关闭线程池（幂等）"""
        if self._executor_shutdown:
            return
        self._executor.shutdown(wait=False)
        self._executor_shutdown = True
        self.logger.info("并发行情获取器已关闭")


class AsyncMarketMonitor:
    """异步监控模式占位（保留兼容）"""

    def __init__(self, logger: logging.Logger, timeout: float = 5.0):
        self.logger = logger
        self.timeout = timeout
        self._session = None
