"""
Radar 统一数据提供层 v3.0
参考 vnpy OmsEngine + akshare-one 缓存策略 + howtrader 事件驱动

核心特性:
- Session 复用（避免重复 TCP 握手）
- 批量请求（一次 HTTP 获取多只股票）
- TTLCache 内存缓存（按数据类型设置不同 TTL）
- 指数退避重试
- 动态 TTL（交易时段 vs 非交易时段）
- 增量更新（只推送变化字段）
"""

from __future__ import annotations

import math
import time
import logging
import os
import threading
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable, Tuple
from collections import defaultdict

import requests
from cachetools import TTLCache
from dashboard.alert_system import AlertEvent
from radar.data.kline_sources import exchange_prefixed_code, resolve_secid
from radar.data.session_pool import create_pooled_session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _safe_float(val: Any) -> float:
    """安全转换为 float，处理 None/空字符串/数字"""
    if val is None or val == '' or val == '-':
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0

def is_trading_time() -> bool:
    """判断是否为 A 股交易时间"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周末
        return False
    hour, minute = now.hour, now.minute
    # 上午 9:30-11:30，下午 13:00-15:00
    return ((9, 30) <= (hour, minute) <= (11, 30) or
            (13, 0) <= (hour, minute) <= (15, 0))


def get_dynamic_ttl(base_ttl: int) -> int:
    """根据是否交易时段动态调整 TTL"""
    if is_trading_time():
        return base_ttl
    # 非交易时段缓存稍长，但不能死（原 300s 太长）
    return base_ttl * 2


def get_secid(code: str) -> str:
    """获取东方财富 secid"""
    return resolve_secid(str(code).strip())


# ---------------------------------------------------------------------------
# 标准化数据对象（参考 vnpy BaseData）
# ---------------------------------------------------------------------------

@dataclass
class TickData:
    """标准化股票行情数据"""
    symbol: str
    name: str = ""
    price: float = 0.0
    change_pct: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    prev_close: float = 0.0
    bid_price: float = 0.0
    ask_price: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    source: str = "eastmoney"
    is_stale: bool = False
    data_age_seconds: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def diff(self, other: Optional[TickData]) -> Dict[str, Any]:
        """计算与另一个 TickData 的差异，返回变化字段
        只返回常用字段（price, change_pct, volume, amount, high, low），
        若无变化则返回空 dict
        """
        if other is None:
            return self.to_dict()

        delta: Dict[str, Any] = {}
        watch_fields = ['price', 'change_pct', 'volume', 'amount', 'high', 'low', 'bid_price', 'ask_price']

        for k in watch_fields:
            v = getattr(self, k)
            ov = getattr(other, k, None)
            if v != ov:
                delta[k] = v

        if delta:
            delta['symbol'] = self.symbol

        return delta


# ---------------------------------------------------------------------------
# 内存缓存（参考 vnpy OmsEngine + akshare-one TTLCache）
# ---------------------------------------------------------------------------

class DataCache:
    """中央内存缓存 - 维护最新数据快照"""

    def __init__(self):
        # 按数据类型分层设置 TTL
        self._ticks: Dict[str, TickData] = {}
        self._alerts: List[AlertEvent] = []
        self._lock = threading.RLock()
        # tick 命中/未命中计数（per-code 粒度，由 provider 在决策点更新）
        self._metrics = {'tick_hits': 0, 'tick_misses': 0}

        # cachetools TTLCache 用于 API 响应缓存
        self._api_cache = {
            'realtime': TTLCache(maxsize=1000, ttl=30),
            'bid_ask': TTLCache(maxsize=500, ttl=15),
            'capital_flow': TTLCache(maxsize=200, ttl=60),
            'sentiment': TTLCache(maxsize=10, ttl=60),
            'hist': TTLCache(maxsize=500, ttl=3600),
        }

    # --- Tick 缓存 ---
    def update_tick(self, tick: TickData) -> Optional[Dict[str, Any]]:
        """更新 tick 缓存，返回增量变化"""
        with self._lock:
            old = self._ticks.get(tick.symbol)
            self._ticks[tick.symbol] = tick
            return tick.diff(old)

    def get_tick(self, symbol: str) -> Optional[TickData]:
        with self._lock:
            return self._ticks.get(symbol)

    def get_all_ticks(self) -> Dict[str, TickData]:
        with self._lock:
            return dict(self._ticks)

    def record_tick_hit(self, n: int = 1) -> None:
        with self._lock:
            self._metrics['tick_hits'] += n

    def record_tick_miss(self, n: int = 1) -> None:
        with self._lock:
            self._metrics['tick_misses'] += n

    def get_metrics(self) -> Dict[str, Any]:
        """返回缓存指标快照（拷贝），调用方安全。"""
        with self._lock:
            hits = self._metrics['tick_hits']
            misses = self._metrics['tick_misses']
            cache_size = len(self._ticks)
        total = hits + misses
        return {
            'tick_hits': hits,
            'tick_misses': misses,
            'tick_hit_ratio_pct': round(hits / total * 100, 2) if total else 0.0,
            'tick_cache_size': cache_size,
        }

    # --- API 响应缓存 ---
    def get_api_cache(self, cache_type: str, key: str) -> Any:
        cache = self._api_cache.get(cache_type)
        if cache is None:
            return None
        return cache.get(key)

    def set_api_cache(self, cache_type: str, key: str, value: Any) -> None:
        cache = self._api_cache.get(cache_type)
        if cache is None:
            return
        cache[key] = value

    # --- 告警缓存 ---
    def add_alert(self, alert: AlertEvent) -> None:
        with self._lock:
            self._alerts.append(alert)
            # 只保留最近 1000 条
            if len(self._alerts) > 1000:
                self._alerts = self._alerts[-1000:]

    def get_recent_alerts(self, limit: int = 100) -> List[AlertEvent]:
        with self._lock:
            return self._alerts[-limit:]


# ---------------------------------------------------------------------------
# 事件引擎（简化版，参考 vnpy EventEngine）
# ---------------------------------------------------------------------------

class EventEngine:
    """轻量级事件引擎 - 用于解耦数据生产与消费"""

    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = defaultdict(list)
        self._general_handlers: List[Callable] = []
        self._lock = threading.Lock()

    def register(self, event_type: str, handler: Callable) -> None:
        """注册特定事件类型的处理器"""
        with self._lock:
            if handler not in self._handlers[event_type]:
                self._handlers[event_type].append(handler)

    def unregister(self, event_type: str, handler: Callable) -> None:
        with self._lock:
            if handler in self._handlers[event_type]:
                self._handlers[event_type].remove(handler)

    def register_general(self, handler: Callable) -> None:
        """注册通用处理器（监听所有事件）"""
        with self._lock:
            if handler not in self._general_handlers:
                self._general_handlers.append(handler)

    def put(self, event_type: str, data: Any) -> None:
        """发布事件"""
        # 分发给特定类型处理器
        handlers = []
        with self._lock:
            handlers = list(self._handlers.get(event_type, []))
            handlers.extend(self._general_handlers)

        for handler in handlers:
            try:
                handler(event_type, data)
            except Exception as e:
                logger.error(f"Event handler error for {event_type}: {e}")


# ---------------------------------------------------------------------------
# 数据提供者（参考 akshare-one EastMoneyClient）
# ---------------------------------------------------------------------------

class EastMoneyProvider:
    """东方财富数据提供者"""

    # 单只接口字段映射 (stock/get)
    FIELD_MAP_SINGLE = {
        'f43': 'price', 'f44': 'high', 'f45': 'low', 'f46': 'open',
        'f47': 'volume', 'f48': 'amount', 'f57': 'symbol', 'f58': 'name',
        'f60': 'prev_close', 'f62': 'outer_volume', 'f63': 'inner_volume',
        'f64': 'bid_volume', 'f66': 'ask_volume', 'f69': 'pe_ttm',
        'f70': 'bid_price', 'f71': 'ask_price', 'f72': 'pb', 'f170': 'change_pct',
    }

    # 批量接口字段映射 (ulist) - 字段含义完全不同！
    FIELD_MAP_ULIST = {
        'f2': 'price',        # 当前价（不是 f43!）
        'f3': 'change_pct',   # 涨跌幅%（不是 f170!）
        'f4': 'change_amount',# 涨跌额
        'f5': 'volume',       # 成交量（不是 f47!）
        'f6': 'amount',       # 成交额（不是 f48!）
        'f12': 'symbol',      # 代码（不是 f57!）
        'f13': 'market',      # 市场类型 0/1
        'f14': 'name',        # 名称（不是 f58!）
        'f15': 'high',        # 最高（不是 f44!）
        'f16': 'low',         # 最低（不是 f45!）
        'f17': 'open',        # 今开（不是 f46!）
        'f18': 'prev_close',  # 昨收（不是 f60!）
        'f31': 'bid_price',   # 买一价
        'f32': 'ask_price',   # 卖一价
    }

    # 批量接口请求字段列表
    ULIST_FIELDS = 'f2,f3,f4,f5,f6,f12,f13,f14,f15,f16,f17,f18,f31,f32'

    def __init__(self, cache: DataCache):
        self.cache = cache
        self.session = create_pooled_session({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        # 备用腾讯接口 session
        self._tencent_session = create_pooled_session({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://stock.finance.qq.com'
        })
        self._eastmoney_lock = threading.Lock()
        self._eastmoney_disabled_until = 0.0
        self._eastmoney_backoff_sec = float(
            os.getenv("RADAR_DASHBOARD_EASTMONEY_BACKOFF_SEC", "300")
        )

    def _fetch_tencent(self, codes: List[str]) -> Dict[str, TickData]:
        """备用数据源：腾讯财经接口
        东财接口 502 时自动切换
        """
        result: Dict[str, TickData] = {}
        if not codes:
            return result

        # 构造腾讯股票代码
        def _tencent_code(c: str) -> str:
            c = str(c).strip()
            # 北交所: 8/9 开头的 6 位代码
            if len(c) == 6 and (c.startswith('8') or c.startswith('9')):
                return f'bj{c}'
            return exchange_prefixed_code(c)

        requested_codes = {str(code).strip() for code in codes}
        tencent_codes = ','.join([_tencent_code(c) for c in codes])
        url = f'https://qt.gtimg.cn/q={tencent_codes}'

        try:
            resp = self._tencent_session.get(url, timeout=8)
            resp.raise_for_status()
            resp.encoding = 'gb18030'
            text = resp.text
            if not text:
                return result

            for line in text.strip().split(';'):
                line = line.strip()
                if not line.startswith('v_'):
                    continue
                # 解析: v_sz300059="1~东方财富~300059~19.63~..."
                try:
                    inner = line.split('="')[1].rstrip('"')
                    parts = inner.split('~')
                    if len(parts) < 38:
                        continue
                    code = parts[2]
                    price = _safe_float(parts[3])
                    if code not in requested_codes:
                        logger.warning("[Tencent] 忽略非请求代码: %s", code)
                        continue
                    if not math.isfinite(price) or price <= 0:
                        logger.warning("[Tencent] 忽略无效价格: %s=%r", code, parts[3])
                        continue
                    tick = TickData(
                        symbol=code,
                        name=parts[1],
                        price=price,
                        prev_close=_safe_float(parts[4]),
                        open=_safe_float(parts[5]),
                        volume=_safe_float(parts[6]),
                        high=_safe_float(parts[33]),
                        low=_safe_float(parts[34]),
                        change_pct=_safe_float(parts[32]),
                        amount=_safe_float(parts[37]),
                        timestamp=datetime.now().isoformat(),
                        source='tencent'
                    )
                    result[code] = tick
                except Exception as e:
                    logger.warning(f"[Tencent] 解析失败: {e}")
                    continue
        except Exception as e:
            logger.warning(f"[Tencent] 请求失败: {e}")

        return result

    def _request_with_retry(self, url: str, params: Dict, max_retries: int = 2) -> Optional[Dict]:
        """带指数退避重试的请求
        5xx 服务器错误不重试（重试无效），429 限流等一会
        """
        retry_delay = 1.0
        last_error = None

        with self._eastmoney_lock:
            if time.monotonic() < self._eastmoney_disabled_until:
                return None
            for attempt in range(max_retries):
                try:
                    resp = self.session.get(url, params=params, timeout=6)
                    if resp.status_code == 200:
                        return resp.json()
                    elif resp.status_code == 429:
                        last_error = "Rate limited (429)"
                        logger.warning(f"[{self.__class__.__name__}] Rate limited, retrying in {retry_delay}s...")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                    elif 500 <= resp.status_code < 600:
                        last_error = f"HTTP {resp.status_code} Server Error"
                        self._eastmoney_disabled_until = time.monotonic() + self._eastmoney_backoff_sec
                        logger.warning(
                            f"[{self.__class__.__name__}] {last_error}, "
                            f"pause {self._eastmoney_backoff_sec:.0f}s and fallback to tencent"
                        )
                        return None
                    else:
                        last_error = f"HTTP {resp.status_code}"
                        logger.warning(f"[{self.__class__.__name__}] {last_error}, retrying...")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                except requests.exceptions.Timeout:
                    last_error = "Timeout"
                    logger.warning(f"[{self.__class__.__name__}] Request timeout, retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                except Exception as e:
                    last_error = str(e)
                    logger.warning(f"[{self.__class__.__name__}] Request error: {e}, retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay *= 2

        logger.error(f"[{self.__class__.__name__}] Failed after {max_retries} retries: {last_error}")
        return None

    def _parse_tick(self, code: str, raw_data: Dict) -> TickData:
        """解析单只接口东方财富原始数据为 TickData (stock/get)"""
        d = raw_data.get('data', {})
        return TickData(
            symbol=code,
            name=d.get('f58', ''),
            price=_safe_float(d.get('f43')),
            change_pct=_safe_float(d.get('f170')),
            open=_safe_float(d.get('f46')),
            high=_safe_float(d.get('f44')),
            low=_safe_float(d.get('f45')),
            volume=_safe_float(d.get('f47')),
            amount=_safe_float(d.get('f48')),
            prev_close=_safe_float(d.get('f60')),
            bid_price=_safe_float(d.get('f70')),
            ask_price=_safe_float(d.get('f71')),
            timestamp=datetime.now().isoformat(),
            source='eastmoney'
        )

    def _parse_tick_ulist(self, item: Dict) -> TickData:
        """解析批量接口 ulist 数据为 TickData
        ulist 字段含义与 stock/get 完全不同！"""
        return TickData(
            symbol=str(item.get('f12', '')),
            name=item.get('f14', ''),
            price=_safe_float(item.get('f2')),
            change_pct=_safe_float(item.get('f3')),
            open=_safe_float(item.get('f17')),
            high=_safe_float(item.get('f15')),
            low=_safe_float(item.get('f16')),
            volume=_safe_float(item.get('f5')),
            amount=_safe_float(item.get('f6')),
            prev_close=_safe_float(item.get('f18')),
            bid_price=_safe_float(item.get('f31')),
            ask_price=_safe_float(item.get('f32')),
            timestamp=datetime.now().isoformat(),
            source='eastmoney'
        )

    def get_realtime(self, codes: List[str]) -> Tuple[Dict[str, TickData], Dict[str, Dict[str, Any]]]:
        """获取实时行情

        返回: (ticks_dict, deltas_dict)
        - ticks: 股票代码 -> TickData 映射
        - deltas: 发生变化的股票代码 -> 变化字段映射（用于 SSE 推送）
        """
        codes = [str(c).strip() for c in codes]
        result: Dict[str, TickData] = {}
        deltas: Dict[str, Dict[str, Any]] = {}
        missing_codes: List[str] = []

        # 检查缓存：Flask 请求先返回缓存（即使过期也用旧数据底）
        # 只有缓存不存在时才请求 API，避免前端被 API 故障阻塞
        for code in codes:
            cached = self.cache.get_tick(code)
            if cached:
                result[code] = cached  # 始终返回缓存，不管是否过期
                cache_time = datetime.fromisoformat(cached.timestamp)
                ttl = get_dynamic_ttl(8)
                if (datetime.now() - cache_time).total_seconds() < ttl:
                    self.cache.record_tick_hit()
                    continue
            missing_codes.append(code)
        if missing_codes:
            self.cache.record_tick_miss(len(missing_codes))

        if not missing_codes:
            return result, deltas

        # 批量请求：多只时用 ulist 批量接口
        batch_unavailable = False
        if len(missing_codes) > 1:
            try:
                secids = ','.join([get_secid(c) for c in missing_codes])
                url = 'http://push2.eastmoney.com/api/qt/ulist.np/get'
                params = {
                    'ut': 'bd1d9ddb04089700cf9c27f6f7426281',
                    'fltt': 2,
                    'invt': 2,
                    'fields': self.ULIST_FIELDS,
                    'secids': secids,
                }
                data = self._request_with_retry(url, params)
                if data and data.get('data') and data['data'].get('diff'):
                    for item in data['data']['diff']:
                        tick = self._parse_tick_ulist(item)
                        if (
                            tick.symbol in missing_codes
                            and math.isfinite(tick.price)
                            and tick.price > 0
                        ):
                            delta = self.cache.update_tick(tick)
                            if delta:
                                deltas[tick.symbol] = delta
                            result[tick.symbol] = tick
                            # 从 missing_codes 中移除已成功获取的
                            if tick.symbol in missing_codes:
                                missing_codes.remove(tick.symbol)
                else:
                    batch_unavailable = True
            except Exception as e:
                logger.error(f"ulist batch request failed: {e}, fallback to single requests")

        # 单只请求：仍然使用 stock/get 接口
        if missing_codes and not batch_unavailable:
            fields = ','.join(self.FIELD_MAP_SINGLE.keys())
            for code in list(missing_codes):
                try:
                    secid = get_secid(code)
                    url = 'http://push2.eastmoney.com/api/qt/stock/get'
                    params = {
                        'ut': 'bd1d9ddb04089700cf9c27f6f7426281',
                        'fltt': 2,
                        'invt': 2,
                        'secid': secid,
                        'fields': fields,
                    }
                    data = self._request_with_retry(url, params)
                    if data and data.get('data'):
                        tick = self._parse_tick(code, data)
                        if not math.isfinite(tick.price) or tick.price <= 0:
                            logger.warning(
                                "EastMoney single returned invalid price for %s: %r",
                                code,
                                tick.price,
                            )
                            continue
                        delta = self.cache.update_tick(tick)
                        if delta:
                            deltas[tick.symbol] = delta
                        result[code] = tick
                        missing_codes.remove(code)
                except Exception as e:
                    logger.warning(f"EastMoney single failed for {code}: {e}")

        # 备用数据源：东财全部失败时，切换腾讯接口
        if missing_codes:
            tencent_result = self._fetch_tencent(missing_codes)
            for code in missing_codes:
                if code in tencent_result:
                    tick = tencent_result[code]
                    delta = self.cache.update_tick(tick)
                    if delta:
                        deltas[tick.symbol] = delta
                    result[code] = tick
                else:
                    # A stale real tick is safer than replacing it with a
                    # fabricated zero-price error row when every source fails.
                    cached_tick = result.get(code)
                    if cached_tick is not None and cached_tick.source != "error":
                        try:
                            cached_at = datetime.fromisoformat(cached_tick.timestamp)
                            age_seconds = max(0.0, (datetime.now() - cached_at).total_seconds())
                        except (TypeError, ValueError):
                            age_seconds = None
                        result[code] = replace(
                            cached_tick,
                            is_stale=True,
                            data_age_seconds=round(age_seconds, 1) if age_seconds is not None else None,
                        )
                    else:
                        result[code] = TickData(
                            symbol=code,
                            name=f"未知-{code}",
                            source="error",
                            is_stale=True,
                        )

        return result, deltas

    def get_realtime_single(self, code: str) -> TickData:
        """获取单只股票实时行情（向后兼容）"""
        result, _ = self.get_realtime([code])
        return result.get(code, TickData(symbol=code, source="error"))


# ---------------------------------------------------------------------------
# 全局实例（单例模式）
# ---------------------------------------------------------------------------

_data_cache = DataCache()
_event_engine = EventEngine()
_eastmoney_provider: Optional[EastMoneyProvider] = None


def get_provider() -> EastMoneyProvider:
    """获取数据提供者实例"""
    global _eastmoney_provider
    if _eastmoney_provider is None:
        _eastmoney_provider = EastMoneyProvider(_data_cache)
    return _eastmoney_provider


def get_cache() -> DataCache:
    """获取缓存实例"""
    return _data_cache


def get_event_engine() -> EventEngine:
    """获取事件引擎实例"""
    return _event_engine


# ---------------------------------------------------------------------------
# 向上兼容接口（保持与原 get_live_prices 等价）
# ---------------------------------------------------------------------------

def get_live_prices(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """向上兼容接口 - 返回字典格式（与原代码保持兼容）"""
    provider = get_provider()
    ticks, _ = provider.get_realtime(codes)
    return {code: tick.to_dict() for code, tick in ticks.items()}


def get_cached_tick(symbol: str) -> Optional[Dict[str, Any]]:
    """获取缓存中的 tick 数据"""
    tick = _data_cache.get_tick(symbol)
    return tick.to_dict() if tick else None
