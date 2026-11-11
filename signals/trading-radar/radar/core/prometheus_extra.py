"""
Prometheus 指标：Tick-to-Trade Latency + EventBus 拥堵 + API 熔断/限频。

未安装 prometheus_client 时所有 observe_* 为空操作，/metrics 仍返回手写指标。
安装后由 HealthHTTPServer 将 generate_latest 输出追加到 /metrics。
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_HAVE = False
_REGISTRY: Optional[object] = None
_PREV_STRATEGY_TAGS_OPEN_PNL: set = set()
_PREV_ALLOC_TAGS: set = set()

# ── PushAdapter 指标 ──
_PUSH_POLL: Optional[object] = None
_PUSH_EVENTS: Optional[object] = None
_FETCH_HIST: Optional[object] = None

# ── Tick-to-Trade Latency（端到端延迟）──
_TTT_LATENCY: Optional[object] = None  # Histogram，毫秒级桶

# ── EventBus 拥堵 ──
_EVENTBUS_QUEUE_SIZE: Optional[object] = None  # Gauge（内存模式队列堆积）
_EVENTBUS_REDIS_LAG: Optional[object] = None   # Gauge（Redis Pub/Sub 消费延迟）

# ── API 熔断/限频 ──
_API_RATE_LIMIT: Optional[object] = None       # Counter（HTTP 429）
_API_CIRCUIT_OPEN: Optional[object] = None   # Counter（熔断器打开次数）
_API_ERROR_5XX: Optional[object] = None       # Counter（HTTP 5xx）

# ── 战局统筹：阵地状态机 / 策略标签盈亏 / 滑点 ──
_BATTLEFIELD_LANE: Optional[object] = None     # Gauge lane ∈ {empty_wait,has_position,rl_takeprofit_cooldown}
_PORTFOLIO_EQUITY: Optional[object] = None
_PORTFOLIO_NET_PROFIT: Optional[object] = None
_PORTFOLIO_DRAWDOWN_RATIO: Optional[object] = None
_STRATEGY_TAG_OPEN_UNREALIZED: Optional[object] = None  # Gauge(strategy_tag)
_STRATEGY_TRADE_WINS: Optional[object] = None   # Counter(strategy_tag)
_STRATEGY_TRADE_LOSSES: Optional[object] = None  # Counter(strategy_tag)
_SLIPPAGE_BPS: Optional[object] = None        # Histogram(strategy_tag)
_DAILY_PNL: Optional[object] = None             # Gauge — 北京时间交易日当日盈亏（首个采样为基准）
_L0_DEPENDENCY_UP: Optional[object] = None      # Gauge(dependency) — QMT / DB 红绿灯
_ALPHA_CURVE_INDEX: Optional[object] = None     # Gauge(series=strategy|hs300)
_STRATEGY_TAG_ALLOCATION: Optional[object] = None # Gauge(strategy_tag) 仓位占权益比

try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

    _HAVE = True
    _REGISTRY = CollectorRegistry()

    # PushAdapter
    _PUSH_POLL = Counter(
        "radar_pushadapter_polls_total",
        "PushAdapter HTTP 轮询次数（事件模式）",
        registry=_REGISTRY,
    )
    _PUSH_EVENTS = Counter(
        "radar_pushadapter_market_events_emitted_total",
        "PushAdapter 向 EventBus 发出的 market_data 事件数（有变化的 tick）",
        registry=_REGISTRY,
    )
    _FETCH_HIST = Histogram(
        "radar_pushadapter_fetch_seconds",
        "单次 _fetch_and_emit 耗时（秒）",
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
        registry=_REGISTRY,
    )

    # Tick-to-Trade Latency（毫秒级直方图，P95/P99 告警阈值 50ms）
    _TTT_LATENCY = Histogram(
        "radar_tick_to_trade_latency_milliseconds",
        "从 PushAdapter 收到价格跳动到策略生成信号的端到端延迟（ms）",
        buckets=(1, 5, 10, 20, 30, 40, 50, 60, 80, 100, 150, 200, 300, 500, 1000),
        registry=_REGISTRY,
    )

    # EventBus 拥堵
    _EVENTBUS_QUEUE_SIZE = Gauge(
        "radar_eventbus_queue_size",
        "EventBus 异步处理器待处理队列长度（内存模式 ThreadPool 堆积）",
        registry=_REGISTRY,
    )
    _EVENTBUS_REDIS_LAG = Gauge(
        "radar_eventbus_redis_lag_seconds",
        "Redis Pub/Sub 消息从发布到消费的时间差（秒）",
        registry=_REGISTRY,
    )

    # API 熔断/限频
    _API_RATE_LIMIT = Counter(
        "radar_api_rate_limit_429_total",
        "外部数据源 HTTP 429 Too Many Requests 次数",
        registry=_REGISTRY,
    )
    _API_CIRCUIT_OPEN = Counter(
        "radar_api_circuit_open_total",
        "断路器熔断（OPEN）次数",
        registry=_REGISTRY,
    )
    _API_ERROR_5XX = Counter(
        "radar_api_error_5xx_total",
        "外部数据源 HTTP 5xx 服务端错误次数",
        registry=_REGISTRY,
    )

    _BATTLEFIELD_LANE = Gauge(
        "radar_battlefield_lane_symbols",
        "标的池中处于各阵地语义的数量（可重叠统计）：空仓等待 / 持仓 / RL 告警冷却期（止盈信号等多走 RL 合并告警时常对应 rl 冷却）",
        ["lane"],
        registry=_REGISTRY,
    )
    _PORTFOLIO_EQUITY = Gauge(
        "radar_portfolio_equity_cny",
        "模拟组合总市值（现金 + 持仓按昨收价估值）",
        registry=_REGISTRY,
    )
    _PORTFOLIO_NET_PROFIT = Gauge(
        "radar_portfolio_net_profit_cny",
        "相对初始资金的浮动盈亏（总市值 − initial_cash）",
        registry=_REGISTRY,
    )
    _PORTFOLIO_DRAWDOWN_RATIO = Gauge(
        "radar_portfolio_max_drawdown_ratio",
        "相对本轮观测峰值的最大回撤比例 (peak−equity)/peak，峰值进程内追踪",
        registry=_REGISTRY,
    )
    _STRATEGY_TAG_OPEN_UNREALIZED = Gauge(
        "radar_strategy_tag_open_unrealized_pnl_cny",
        "按 targets.strategy_tag 汇总的持仓浮动盈亏（仅仍有头寸部分）",
        ["strategy_tag"],
        registry=_REGISTRY,
    )
    _STRATEGY_TRADE_WINS = Counter(
        "radar_strategy_tag_trade_wins_total",
        "平仓计数（盈利），用于胜率；需在 execute_trade(close) 或实盘网关回填",
        ["strategy_tag"],
        registry=_REGISTRY,
    )
    _STRATEGY_TRADE_LOSSES = Counter(
        "radar_strategy_tag_trade_losses_total",
        "平仓计数（亏损），用于胜率",
        ["strategy_tag"],
        registry=_REGISTRY,
    )
    _SLIPPAGE_BPS = Histogram(
        "radar_order_slippage_bps",
        "相对预期价的滑点（基点），做多成交价高于预期多为不利（符号依赖 observe 约定）",
        ["strategy_tag"],
        buckets=(
            -500.0,
            -200.0,
            -100.0,
            -50.0,
            -25.0,
            -10.0,
            -5.0,
            0.0,
            5.0,
            10.0,
            25.0,
            50.0,
            100.0,
            200.0,
            500.0,
        ),
        registry=_REGISTRY,
    )

    _DAILY_PNL = Gauge(
        "radar_daily_pnl_cny",
        "今日盈亏（人民币）：相对北京时间当日进程首次采样权益的增量；午夜跨日自动换基准",
        registry=_REGISTRY,
    )
    _L0_DEPENDENCY_UP = Gauge(
        "radar_l0_dependency_up",
        "L0 红线绿灯依赖存活（1=正常 0=异常）；组件 qmt_adapter=实盘网关适配器 database=RDS 等数据库",
        ["dependency"],
        registry=_REGISTRY,
    )
    _ALPHA_CURVE_INDEX = Gauge(
        "radar_alpha_curve_index",
        "超额曲线归一化指数（北京时间交易日基准=100）：strategy=组合总市值相对当日首个采样；hs300=沪深300指数点位相对当日首个成功抓取",
        ["series"],
        registry=_REGISTRY,
    )
    _STRATEGY_TAG_ALLOCATION = Gauge(
        "radar_strategy_tag_capital_allocation_ratio",
        "策略标签持仓市值占组合总市值（equity）的比例；现金隐含为 1−Σ(tags)",
        ["strategy_tag"],
        registry=_REGISTRY,
    )
except ImportError:
    pass


def _parse_env_bool(raw: str) -> Optional[bool]:
    s = raw.strip().lower()
    if s in ("1", "true", "yes", "up", "ok", "green"):
        return True
    if s in ("0", "false", "no", "down", "fail", "red"):
        return False
    return None


def observe_alpha_curve_indices(strategy_idx: Optional[float], hs300_idx: Optional[float]) -> None:
    """Alpha 曲线：策略与沪深300在同尺度下的归一化走势（基准日首个采样=100）。"""
    if not _HAVE or _ALPHA_CURVE_INDEX is None:
        return
    if strategy_idx is not None:
        _ALPHA_CURVE_INDEX.labels(series="strategy").set(float(strategy_idx))
    if hs300_idx is not None:
        _ALPHA_CURVE_INDEX.labels(series="hs300").set(float(hs300_idx))


def observe_strategy_tag_allocation_ratios(tag_ratios: Dict[str, float]) -> None:
    """各 strategy_tag 持仓市值 / equity；缺失标签归零。"""
    global _PREV_ALLOC_TAGS
    if not _HAVE or _STRATEGY_TAG_ALLOCATION is None:
        return
    for tag in _PREV_ALLOC_TAGS:
        if tag not in tag_ratios:
            _STRATEGY_TAG_ALLOCATION.labels(strategy_tag=tag).set(0.0)
    _PREV_ALLOC_TAGS = set(tag_ratios.keys())
    for tag, ratio in tag_ratios.items():
        _STRATEGY_TAG_ALLOCATION.labels(strategy_tag=str(tag)).set(max(0.0, min(1.0, float(ratio))))


def observe_daily_pnl_cny(delta_cny: float) -> None:
    """刷新当天盈亏 Gauge（由 trading_observability 根据日内基准权益计算）。"""
    if not _HAVE or _DAILY_PNL is None:
        return
    _DAILY_PNL.set(float(delta_cny))


_L0_DEP_ALIASES = {
    "qmt": "qmt_adapter",
    "qmt_adapter": "qmt_adapter",
    "database": "database",
    "db": "database",
    "rds": "database",
    "postgres": "database",
    "postgresql": "database",
    "aurora": "database",
}


def observe_l0_dependency_up(dependency: str, up: bool) -> None:
    """程序化刷新依赖红绿灯；dependency 可为 qmt_adapter / database 或别名 qmt、db、rds 等。"""
    if not _HAVE or _L0_DEPENDENCY_UP is None:
        return
    key = dependency.strip().lower().replace("-", "_")
    dep = _L0_DEP_ALIASES.get(key)
    if dep is None:
        logger.debug("observe_l0_dependency_up: unknown dependency %r", dependency)
        return
    _L0_DEPENDENCY_UP.labels(dependency=dep).set(1.0 if up else 0.0)


def refresh_l0_dependency_gauges_from_env() -> None:
    """从环境变量刷新 QMT / 数据库存活状态（便于 K8s 注入探针结果）。"""
    if not _HAVE or _L0_DEPENDENCY_UP is None:
        return
    raw_qmt = os.getenv("RADAR_L0_QMT_ADAPTER_UP")
    if raw_qmt is not None and str(raw_qmt).strip() != "":
        b = _parse_env_bool(str(raw_qmt))
        if b is not None:
            _L0_DEPENDENCY_UP.labels(dependency="qmt_adapter").set(1.0 if b else 0.0)

    raw_db = os.getenv("RADAR_L0_DATABASE_UP") or os.getenv("RADAR_L0_AWS_DATABASE_UP")
    if raw_db is not None and str(raw_db).strip() != "":
        b = _parse_env_bool(str(raw_db))
        if b is not None:
            _L0_DEPENDENCY_UP.labels(dependency="database").set(1.0 if b else 0.0)


def observe_pushadapter_poll(duration_sec: float, events_emitted: int) -> None:
    if not _HAVE or _PUSH_POLL is None:
        return
    _PUSH_POLL.inc()
    _PUSH_EVENTS.inc(max(0, int(events_emitted)))
    _FETCH_HIST.observe(max(0.0, float(duration_sec)))


def observe_tick_to_trade_latency(duration_ms: float) -> None:
    """记录端到端延迟（毫秒）。"""
    if not _HAVE or _TTT_LATENCY is None:
        return
    _TTT_LATENCY.observe(max(0.0, float(duration_ms)))


def observe_eventbus_queue_size(size: int) -> None:
    """记录 EventBus 队列堆积量。"""
    if not _HAVE or _EVENTBUS_QUEUE_SIZE is None:
        return
    _EVENTBUS_QUEUE_SIZE.set(max(0, int(size)))


def observe_eventbus_redis_lag(lag_sec: float) -> None:
    """记录 Redis Pub/Sub 消费延迟。"""
    if not _HAVE or _EVENTBUS_REDIS_LAG is None:
        return
    _EVENTBUS_REDIS_LAG.set(max(0.0, float(lag_sec)))


def observe_api_rate_limit_429() -> None:
    """记录 HTTP 429 限频错误。"""
    if not _HAVE or _API_RATE_LIMIT is None:
        return
    _API_RATE_LIMIT.inc()


def observe_api_circuit_open() -> None:
    """记录断路器熔断。"""
    if not _HAVE or _API_CIRCUIT_OPEN is None:
        return
    _API_CIRCUIT_OPEN.inc()


def observe_api_error_5xx() -> None:
    """记录 HTTP 5xx 服务端错误。"""
    if not _HAVE or _API_ERROR_5XX is None:
        return
    _API_ERROR_5XX.inc()


def observe_battlefield_lane_counts(
    empty_wait: int,
    has_position: int,
    rl_takeprofit_cooldown: int,
) -> None:
    """阵地状态机：三类并行计数（同一标的可同时计入持仓与 RL 冷却）。"""
    if not _HAVE or _BATTLEFIELD_LANE is None:
        return
    _BATTLEFIELD_LANE.labels(lane="empty_wait").set(max(0, int(empty_wait)))
    _BATTLEFIELD_LANE.labels(lane="has_position").set(max(0, int(has_position)))
    _BATTLEFIELD_LANE.labels(lane="rl_takeprofit_cooldown").set(
        max(0, int(rl_takeprofit_cooldown))
    )


def observe_portfolio_performance(
    equity_cny: float,
    net_profit_cny: float,
    drawdown_ratio: float,
) -> None:
    """账户权益、净利、最大回撤（相对进程内峰值）。"""
    if not _HAVE:
        return
    if _PORTFOLIO_EQUITY is not None:
        _PORTFOLIO_EQUITY.set(float(equity_cny))
    if _PORTFOLIO_NET_PROFIT is not None:
        _PORTFOLIO_NET_PROFIT.set(float(net_profit_cny))
    if _PORTFOLIO_DRAWDOWN_RATIO is not None:
        _PORTFOLIO_DRAWDOWN_RATIO.set(max(0.0, float(drawdown_ratio)))


def observe_strategy_tag_open_unrealized_pnl(tag_values: Dict[str, float]) -> None:
    """
    刷新各策略标签下的浮动盈亏 Gauge；缺失的标签归零。
    tag_values: strategy_tag -> 浮动盈亏金额（人民币）
    """
    global _PREV_STRATEGY_TAGS_OPEN_PNL
    if not _HAVE or _STRATEGY_TAG_OPEN_UNREALIZED is None:
        return
    for tag in _PREV_STRATEGY_TAGS_OPEN_PNL:
        if tag not in tag_values:
            _STRATEGY_TAG_OPEN_UNREALIZED.labels(strategy_tag=tag).set(0.0)
    _PREV_STRATEGY_TAGS_OPEN_PNL = set(tag_values.keys())
    for tag, val in tag_values.items():
        _STRATEGY_TAG_OPEN_UNREALIZED.labels(strategy_tag=str(tag)).set(float(val))


def observe_strategy_trade_outcome(strategy_tag: str, win: bool) -> None:
    """平仓结果计数（胜率分子分母）；strategy_tag 空时用 untagged。"""
    if not _HAVE:
        return
    tag = strategy_tag.strip() or "untagged"
    if win:
        if _STRATEGY_TRADE_WINS is not None:
            _STRATEGY_TRADE_WINS.labels(strategy_tag=tag).inc()
    else:
        if _STRATEGY_TRADE_LOSSES is not None:
            _STRATEGY_TRADE_LOSSES.labels(strategy_tag=tag).inc()


def observe_order_slippage_bps(strategy_tag: str, slippage_bps: float) -> None:
    """
    记录成交价相对预期价的滑点（基点）。
    约定：buy/open 时 (fill−expected)/expected*10000；sell 可对符号单独约定。
    """
    if not _HAVE or _SLIPPAGE_BPS is None:
        return
    tag = strategy_tag.strip() or "untagged"
    _SLIPPAGE_BPS.labels(strategy_tag=tag).observe(float(slippage_bps))


def metrics_exposition_suffix() -> str:
    """
    返回需追加到 /metrics 的 Prometheus 文本（仅含本模块注册的指标）。
    未安装库时返回空字符串。
    """
    if not _HAVE:
        return ""
    try:
        refresh_l0_dependency_gauges_from_env()
        from prometheus_client import generate_latest

        if _REGISTRY is None:
            return ""
        return generate_latest(_REGISTRY).decode("utf-8", errors="replace")
    except Exception as e:
        logger.debug("prometheus generate_latest 失败: %s", e)
        return ""
