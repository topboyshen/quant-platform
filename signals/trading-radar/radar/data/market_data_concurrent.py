"""
并发行情获取器 - 使用 ThreadPoolExecutor 并行获取
职责：解决串行扫描单点阻塞问题
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from radar.data.api_protection import ProtectedMarketDataFetcher
from radar.data.market_data import MarketDataFetcher
from radar.schemas.data_schema import ColumnNames


class ConcurrentMarketDataFetcher:
    """
    智能并发行情获取器
    - 根据股票数量自动选择最优策略
    - 小批量(<20): 并行单股接口，避免拉取全市场
    - 大批量(>100): 批量接口，减少连接数
    - 支持单股超时控制，失败自动降级
    """

    # 策略切换阈值
    BATCH_THRESHOLD_SMALL = 20   # 小于此值用单股接口
    BATCH_THRESHOLD_LARGE = 100  # 大于此值必须用批量接口

    def __init__(self, logger: logging.Logger, api_key: str = "",
                 max_workers: int = 5, timeout: float = 15.0,
                 on_circuit_open: callable = None, on_circuit_close: callable = None):
        self.logger = logger
        self.api_key = api_key
        self.timeout = timeout

        # 基础获取器 + 保护包装（重试 + 断路器）
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

        # 批量数据缓存（仅用于大批量场景）
        self._batch_cache = None
        self._batch_cache_time = 0
        self._batch_cache_ttl = 5
        # 6 位代码 -> 行号，避免对每个标的在全表上做 str.contains
        self._batch_row_index: Optional[Dict[str, int]] = None

    def _get_single_stock_direct(self, code: str, name: str) -> Optional[Dict]:
        """直接调用单股接口（不检查批量缓存）"""
        try:
            return self._fetcher.get_stock_data(code, name)
        except Exception as e:
            self.logger.warning(f"获取 {code} 数据失败: {e}")
            return None

    @staticmethod
    def _normalize_listed_code(code: str) -> str:
        """与行情表 f12 对齐的 6 位代码（用于字典查找）"""
        return str(code).strip().zfill(6)

    def _rebuild_batch_row_index(self) -> None:
        """批量缓存更新后构建 code -> 首行行号（重复代码保留第一次）"""
        self._batch_row_index = None
        if self._batch_cache is None or self._batch_cache.empty:
            return
        col = ColumnNames.CODE
        if col not in self._batch_cache.columns:
            return
        index: Dict[str, int] = {}
        for i, raw in enumerate(self._batch_cache[col].astype(str)):
            key = self._normalize_listed_code(raw)
            if key not in index:
                index[key] = i
        self._batch_row_index = index

    def _get_single_stock_from_cache(self, code: str, name: str) -> Optional[Dict]:
        """从批量缓存获取单只股票"""
        try:
            if self._batch_cache is not None and self._batch_row_index:
                row_idx = self._batch_row_index.get(self._normalize_listed_code(code))
                if row_idx is not None:
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
            return None
        except Exception as e:
            self.logger.warning(f"从缓存获取 {code} 失败: {e}")
            return None

    def _update_batch_cache(self) -> bool:
        """更新批量缓存，返回是否成功"""
        now = time.time()
        if self._batch_cache is None or (now - self._batch_cache_time) > self._batch_cache_ttl:
            try:
                self._batch_cache = self._fetcher.fetch_all()
                self._batch_cache_time = now
                self._rebuild_batch_row_index()
                self.logger.debug("批量行情缓存已更新")
                return True
            except Exception as e:
                self.logger.warning(f"批量行情获取失败: {e}")
                self._batch_cache = None
                self._batch_row_index = None
                return False
        return True  # 缓存仍有效

    def fetch_multiple(self, stocks: List[tuple]) -> Dict[str, Dict]:
        """
        智能并发获取多只股票数据
        根据股票数量自动选择最优策略
        :param stocks: [(code, name), ...]
        :return: {code: data_dict, ...}
        """
        if not stocks:
            return {}

        n_stocks = len(stocks)

        # 修复：小批量(<=15)强制使用批量接口，避免并发超时问题
        # 批量接口一次拉取全市场，比并行单股接口更稳定
        if n_stocks <= 15:
            self.logger.debug(f"股票数量 {n_stocks} <= 15，使用批量接口（更稳定）")
            return self._fetch_large_batch(stocks)
        elif n_stocks > self.BATCH_THRESHOLD_LARGE:
            self.logger.debug(f"股票数量 {n_stocks} > {self.BATCH_THRESHOLD_LARGE}，使用批量接口")
            return self._fetch_large_batch(stocks)
        else:
            self.logger.debug(f"股票数量 {n_stocks}，使用批量接口")
            return self._fetch_medium_batch(stocks)

    def _fetch_small_batch(self, stocks: List[tuple]) -> Dict[str, Dict]:
        """
        小批量获取策略：直接并行单股接口
        避免拉取全市场 5000 只股票的 overhead
        修复：as_completed 超时异常导致主循环中断
        """
        results = {}
        futures = {}

        # 提交所有单股任务
        for code, name in stocks:
            future = self._executor.submit(self._get_single_stock_direct, code, name)
            futures[future] = (code, name)

        # 修复：收集结果，超时异常静默处理
        try:
            for future in as_completed(futures, timeout=self.timeout * 2):
                code, name = futures[future]
                try:
                    data = future.result(timeout=self.timeout)
                    if data:
                        results[code] = data
                    else:
                        self.logger.warning(f"{name}({code}) 数据获取失败")
                except Exception as e:
                    self.logger.warning(f"{name}({code}) 获取超时或异常: {e}")
        except Exception as e:
            # 修复关键点：as_completed 整体超时不抛异常，只记录日志
            self.logger.warning(f"批次获取部分超时: {e}")
        finally:
            # 取消未完成的 future，避免泄漏
            for future in futures:
                if not future.done():
                    future.cancel()

        return results

    def _fetch_large_batch(self, stocks: List[tuple]) -> Dict[str, Dict]:
        """
        大批量获取策略：使用批量接口 + 缓存
        减少连接数和传输开销
        """
        # 1. 更新批量缓存
        if not self._update_batch_cache():
            # 批量接口失败，降级到小批量策略
            self.logger.warning("批量接口失败，降级到单股接口")
            return self._fetch_small_batch(stocks)

        # 2. 从缓存提取所需股票
        results = {}
        missing_stocks = []

        for code, name in stocks:
            data = self._get_single_stock_from_cache(code, name)
            if data:
                results[code] = data
            else:
                missing_stocks.append((code, name))

        # 3. 对缺失的股票并行单查（可能是北交所等特殊股票）
        if missing_stocks:
            self.logger.debug(f"{len(missing_stocks)} 只股票从批量缓存缺失，使用单股接口补充")
            extra_results = self._fetch_small_batch(missing_stocks)
            results.update(extra_results)

        return results

    def _fetch_medium_batch(self, stocks: List[tuple]) -> Dict[str, Dict]:
        """
        中批量获取策略：优先批量接口，失败降级
        """
        # 尝试批量接口
        if self._update_batch_cache():
            results = {}
            missing = []

            for code, name in stocks:
                data = self._get_single_stock_from_cache(code, name)
                if data:
                    results[code] = data
                else:
                    missing.append((code, name))

            # 如果大部分都在缓存中，补充缺失的
            if len(results) >= len(stocks) * 0.7:
                if missing:
                    extra_results = self._fetch_small_batch(missing)
                    results.update(extra_results)
                return results

        # 批量接口不可用或效率低，降级到小批量策略
        self.logger.debug("批量接口效率低，降级到并行单股接口")
        return self._fetch_small_batch(stocks)

    def fetch_single(self, code: str, name: str = None) -> Optional[Dict]:
        """获取单只股票（兼容旧接口）"""
        results = self.fetch_multiple([(code, name or code)])
        return results.get(code)

    def get_batch_data(self) -> Optional[object]:
        """获取批量行情数据"""
        now = time.time()
        if self._batch_cache is None or (now - self._batch_cache_time) > self._batch_cache_ttl:
            self._batch_cache = self._fetcher.fetch_all()
            self._batch_cache_time = now
            self._rebuild_batch_row_index()
        return self._batch_cache

    def shutdown(self) -> None:
        """关闭线程池（幂等，避免 run/finally 与 stop 重复调用出错）"""
        if self._executor_shutdown:
            return
        self._executor.shutdown(wait=False)
        self._executor_shutdown = True
        self.logger.info("并发行情获取器已关闭")


class AsyncMarketMonitor:
    """
    异步监控模式（更激进的优化）
    使用 asyncio + aiohttp 实现真正的异步 I/O

    注意：此模式需要 aiohttp 依赖
    pip install aiohttp
    """

    def __init__(self, logger: logging.Logger, timeout: float = 5.0):
        self.logger = logger
        self.timeout = timeout
        self._session = None

    async def _init_session(self):
        """初始化 aiohttp session"""
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }
            )

    async def fetch_single_async(self, code: str, name: str) -> Optional[Dict]:
        """异步获取单只股票"""
        await self._init_session()

        try:

            # 判断市场
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

            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

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

        except Exception as e:
            self.logger.warning(f"异步获取 {code} 失败: {e}")
            return None

    async def fetch_multiple_async(self, stocks: List[tuple]) -> Dict[str, Dict]:
        """并发获取多只股票（asyncio 模式）"""
        import asyncio

        tasks = [
            self.fetch_single_async(code, name)
            for code, name in stocks
        ]

        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        results = {}
        for (code, name), result in zip(stocks, results_list):
            if isinstance(result, dict):
                results[code] = result
            elif isinstance(result, Exception):
                self.logger.warning(f"{code} 异步获取异常: {result}")

        return results

    async def close(self):
        """关闭 session"""
        if self._session:
            await self._session.close()
            self._session = None
