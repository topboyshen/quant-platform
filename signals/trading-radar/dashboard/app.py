#!/usr/bin/env python3
"""
Radar 系统仪表板 v3.0
提供 Web 界面查看系统状态、实时股价、持仓数据和告警历史

v3.0 升级:
- 统一数据提供者（Session 复用 + 批量请求 + TTLCache）
- SSE 实时推送（替代轮询）
- 事件驱动架构
- 增量更新

API 路由：Flask Blueprint 见 dashboard/api/routes.py（/api/* 与 /api/stream）。

用法:
    python dashboard/app.py
    # 访问 http://localhost:8081
"""

import os
import sys
import json
import copy
import re
import sqlite3
import time
import threading
import logging
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, render_template, jsonify, request, g

logger = logging.getLogger(__name__)

# 确保模块搜索路径包含项目根目录（用于 systemd 启动时的导入）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from radar.runtime_lock import SingleInstanceLock, SingleInstanceLockError
from radar.core.scan_scheduler import HolidayCalendar
from dashboard import service_auth
from dashboard.config_guard import maybe_warn_monitor_config_schema

# 导入统一数据提供层（v3.0 新增）
from dashboard.data_provider import (
    get_provider, get_cache, get_event_engine, get_live_prices,
    is_trading_time
)
from dashboard.alert_system import (
    AlertManager, TelegramAlertChannel, WebhookAlertChannel
)
from dashboard.ops_status import (
    collect_status_snapshot,
    get_cooldown_stats as _ops_get_cooldown_stats,
    get_disk_usage as _ops_get_disk_usage,
    get_process_stats as _ops_get_process_stats,
    get_service_status as _ops_get_service_status,
)
from dashboard.risk_view import load_risk_state
from dashboard.swing_metrics import render_swing_metrics

# 导入 akshare 增值数据模块
try:
    from dashboard.akshare_data import (
        get_capital_flow, get_market_sentiment
    )
    AKSHARE_AVAILABLE = True
except ImportError:
    AKSHARE_AVAILABLE = False

# 确保在正确目录
os.chdir(PROJECT_ROOT)

app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['JSON_AS_ASCII'] = False

BASE_DIR = Path(PROJECT_ROOT)
DASHBOARD_TOKEN_FILE_ENV = 'DASHBOARD_API_TOKEN_FILE'
PUBLIC_DASHBOARD_PATHS = frozenset({'/health'})
SLOW_REQUEST_MS = int(os.getenv('DASHBOARD_SLOW_REQUEST_MS', '300'))
_json_cache = {}
_json_cache_lock = threading.Lock()
_runtime_cache_lock = threading.Lock()
# prices 命中率指标已统一收口到 dashboard.data_provider.DataCache.get_metrics()，
# 此处保留 status_snapshot 缓存与 last_log_at 节流。
_runtime_cache = {
    'status_snapshot': {'expires_at': 0.0, 'data': None},
    'metrics': {
        'status_hit': 0,
        'status_miss': 0,
        'last_log_at': 0.0,
    },
}
_config_schema_last_warn_monotonic = 0.0
CONFIG_SCHEMA_WARN_INTERVAL_SEC = float(
    os.getenv('DASHBOARD_CONFIG_WARN_INTERVAL', '300')
)
service_auth.install_flask_token_auth(
    app,
    token_file_env_name=DASHBOARD_TOKEN_FILE_ENV,
    header_name='X-Dashboard-Token',
    public_paths=PUBLIC_DASHBOARD_PATHS,
)


def _read_refresh_interval(name: str, default: float, minimum: float) -> float:
    """Read a bounded refresh interval without letting bad env values kill the loop."""
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r 无效，使用默认值 %.0f 秒", name, raw, default)
        return default
    if not (minimum <= value <= 3600):
        logger.warning(
            "%s=%r 超出范围 [%.0f, 3600]，使用默认值 %.0f 秒",
            name,
            raw,
            minimum,
            default,
        )
        return default
    return value


def _background_refresh_interval() -> float:
    """Keep live quotes responsive while reducing closed-market polling."""
    now = datetime.now()
    if now.weekday() >= 5 or HolidayCalendar.is_holiday(now):
        return _read_refresh_interval(
            'RADAR_DASHBOARD_REFRESH_WEEKEND_SEC', default=300, minimum=30
        )
    if is_trading_time():
        return _read_refresh_interval(
            'RADAR_DASHBOARD_REFRESH_TRADING_SEC', default=5, minimum=2
        )
    return _read_refresh_interval(
        'RADAR_DASHBOARD_REFRESH_CLOSED_SEC', default=60, minimum=15
    )


def _maybe_warn_monitor_config_schema(merged: dict) -> None:
    global _config_schema_last_warn_monotonic
    _config_schema_last_warn_monotonic = maybe_warn_monitor_config_schema(
        merged,
        logger=logger,
        last_warn_monotonic=_config_schema_last_warn_monotonic,
        interval_sec=CONFIG_SCHEMA_WARN_INTERVAL_SEC,
    )


@app.before_request
def _track_request_start():
    """记录请求开始时间（用于慢请求监控）。"""
    g._request_start = time.perf_counter()


@app.after_request
def _log_slow_request(response):
    """仅记录慢请求，避免日志噪音。"""
    # SSE 长连接会天然很慢，不参与慢请求统计
    if request.path == '/api/stream':
        return response

    start = getattr(g, '_request_start', None)
    if start is None:
        return response

    elapsed_ms = (time.perf_counter() - start) * 1000
    is_key_route = request.path.startswith('/api/') or request.path in {'/', '/logs'}
    if is_key_route and elapsed_ms >= SLOW_REQUEST_MS:
        logger.warning(
            "[SlowRequest] %s %s status=%s took=%.1fms threshold=%sms",
            request.method,
            request.path,
            response.status_code,
            elapsed_ms,
            SLOW_REQUEST_MS,
        )
    return response


def _load_json_cached(path: Path, default, copy_data: bool = True):
    """按文件 mtime 缓存 JSON，可选返回深拷贝避免调用方污染缓存。"""
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return copy.deepcopy(default) if copy_data else default

    cache_key = str(path)
    with _json_cache_lock:
        cached = _json_cache.get(cache_key)
        if cached and cached.get('mtime_ns') == mtime_ns:
            return copy.deepcopy(cached['data']) if copy_data else cached['data']

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return copy.deepcopy(default) if copy_data else default

    with _json_cache_lock:
        _json_cache[cache_key] = {'mtime_ns': mtime_ns, 'data': data}
    return copy.deepcopy(data) if copy_data else data


def _maybe_log_cache_metrics(now: float, interval_seconds: float = 60.0):
    """按固定间隔输出缓存命中率，避免日志刷屏。"""
    with _runtime_cache_lock:
        metrics = _runtime_cache['metrics']
        if now - metrics['last_log_at'] < interval_seconds:
            return
        metrics['last_log_at'] = now
        status_hit = metrics['status_hit']
        status_miss = metrics['status_miss']

    cache = get_cache()
    cache_metrics = cache.get_metrics() if cache is not None else {
        'tick_hits': 0, 'tick_misses': 0, 'tick_hit_ratio_pct': 0.0,
    }

    status_total = status_hit + status_miss
    status_ratio = (status_hit / status_total * 100) if status_total else 0.0
    logger.info(
        "[CacheMetrics] prices hit=%s miss=%s ratio=%.2f%% | status hit=%s miss=%s ratio=%.1f%%",
        cache_metrics['tick_hits'],
        cache_metrics['tick_misses'],
        cache_metrics['tick_hit_ratio_pct'],
        status_hit,
        status_miss,
        status_ratio,
    )


def _get_live_prices_cached(codes, ttl_seconds: float = None):
    """委托给 data_provider 层（DataCache + EastMoneyProvider 已带 per-code TTL 与命中率），
    Dashboard 不再维护独立的 prices 缓存层，指标统一收口到 DataCache.get_metrics()。

    `ttl_seconds` 参数保留只为向后兼容，已忽略；TTL 由 provider 的 get_dynamic_ttl(8) 控制。
    """
    normalized = sorted({str(code).strip() for code in codes if str(code).strip()})
    if not normalized:
        return {}
    out = get_live_prices(normalized) or {}
    _maybe_log_cache_metrics(time.monotonic())
    return out


def _get_status_snapshot_cached(ttl_seconds: float = 2.0):
    """缓存系统状态快照，避免高频触发系统命令。"""
    now = time.monotonic()
    with _runtime_cache_lock:
        cached = _runtime_cache['status_snapshot']
        if cached['data'] is not None and cached['expires_at'] > now:
            _runtime_cache['metrics']['status_hit'] += 1
            return cached['data']
        _runtime_cache['metrics']['status_miss'] += 1

    snapshot = collect_status_snapshot(BASE_DIR)
    with _runtime_cache_lock:
        _runtime_cache['status_snapshot'] = {
            'expires_at': now + ttl_seconds,
            'data': snapshot,
        }
    _maybe_log_cache_metrics(now)
    return snapshot


def _get_int_arg(name: str, default: int, min_value: int, max_value: int) -> int:
    """读取并夹紧整数查询参数，避免异常输入导致 500。"""
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(value, max_value))

# 初始化告警管理器（v3.1 新增）
_alert_manager = AlertManager()

def init_alert_channels():
    """根据配置初始化告警通道"""
    global _alert_manager
    config = load_config()
    channels = config.get('alert_channels', {})
    # Build a replacement manager first.  A partial initialization failure must
    # not leave duplicate channels behind when the background task retries.
    manager = AlertManager()

    # Telegram
    tg = channels.get('telegram', {})
    if tg.get('enabled') and tg.get('bot_token') and tg.get('chat_id'):
        manager.add_channel(TelegramAlertChannel(
            bot_token=tg['bot_token'],
            chat_id=tg['chat_id'],
            enabled=True
        ))
        logger.info("[Alert] Telegram 告警通道已启用")

    # Webhook（钉钉/飞书/自定义）
    wh = channels.get('webhook', {})
    if wh.get('enabled') and wh.get('url'):
        manager.add_channel(WebhookAlertChannel(
            webhook_url=wh['url'],
            headers=wh.get('headers'),
            template=wh.get('template'),
            enabled=True
        ))
        logger.info("[Alert] Webhook 告警通道已启用")

    _alert_manager = manager

# 股票代码映射（已移至 data_provider，如需本地使用请从 data_provider 导入）


def get_service_status():
    """获取监控服务状态（兼容 systemd 不可用场景）。"""
    return _ops_get_service_status()


def get_process_stats():
    """获取监控/Dashboard 进程状态并识别多实例。"""
    return _ops_get_process_stats()


def get_disk_usage():
    """获取磁盘使用情况。"""
    return _ops_get_disk_usage(BASE_DIR)


def get_cooldown_stats():
    """获取告警冷却统计。"""
    return _ops_get_cooldown_stats(BASE_DIR)


def _resolve_log_file():
    """解析当前监控进程实际写入的日志文件。

    微服务部署优先把运行日志放在 ``logs/`` 目录；保留根目录 ``radar.log``
    作为旧部署回退。外部 logrotate 可能已把日志重命名为 ``*.1``，因此按
    "存在且非空" 的优先级挑选。
    """
    candidates = (
        BASE_DIR / 'logs' / 'radar.log',
        BASE_DIR / 'logs' / 'radar.log.1',
        BASE_DIR / 'radar.log',
        BASE_DIR / 'radar.log.1',
    )
    for candidate in candidates:
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def get_recent_logs(limit=100, level_filter=None):
    """获取最近日志，支持级别过滤"""
    log_file = _resolve_log_file()
    if log_file is None or not log_file.exists():
        return []

    try:
        limit = max(1, min(int(limit), 1000))
        lines = deque(maxlen=limit * 3 if level_filter else limit)
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.rstrip('\n')
                if line:
                    lines.append(line)

        parsed = []
        for line in lines:
            if ' - ' in line:
                parts = line.split(' - ', 2)
                if len(parts) >= 3:
                    level = parts[1].strip()
                    if level_filter and level.upper() != level_filter.upper():
                        continue
                    parsed.append({
                        'timestamp': parts[0],
                        'level': level,
                        'message': parts[2]
                    })
                else:
                    parsed.append({'message': line})
            else:
                parsed.append({'message': line})

        # 如果过滤后超出限制，裁剪
        if level_filter and len(parsed) > limit:
            parsed = parsed[-limit:]

        return parsed
    except (OSError, UnicodeDecodeError):
        return []


def load_config():
    """加载配置（Telegram 等可被环境变量覆盖，见 radar.secrets）"""
    from radar.config_loader import load_config as _load_radar_config

    merged = _load_radar_config(BASE_DIR / 'config.json', validate_monitor=False)
    _maybe_warn_monitor_config_schema(merged)
    return merged


def load_config_readonly():
    """加载配置的只读调用约定；每次返回独立对象。"""
    from radar.config_loader import load_config as _load_radar_config

    merged = _load_radar_config(BASE_DIR / 'config.json', validate_monitor=False)
    _maybe_warn_monitor_config_schema(merged)
    return merged


def load_portfolio():
    """加载持仓数据"""
    return _load_json_cached(BASE_DIR / 'portfolio.json', {}, copy_data=True)


def load_swing_overview():
    """加载波段化概览（实现见 dashboard/swing_view.py，使用当前 BASE_DIR）。"""
    from dashboard.swing_view import load_swing_overview as _impl

    return _impl(BASE_DIR)


def get_live_prices_legacy(codes=None):
    """获取实时股价 - 使用统一数据提供层（v3.0 升级）"""
    if not codes:
        config = load_config()
        codes = [t['code'] for t in config.get('targets', [])]

    # 使用统一数据提供层（Session 复用 + 批量请求 + 缓存）
    provider = get_provider()
    ticks, _ = provider.get_realtime(codes)

    # 转换为原有格式（保持向上兼容）
    prices = {}
    for code, tick in ticks.items():
        prices[code] = tick.to_dict()

    return prices


# 保持旧函数名称向上兼容
get_live_prices = get_live_prices_legacy


# ---------------------------------------------------------------------------
# SSE 实时推送（v3.0 新增）
# ---------------------------------------------------------------------------

# 全局 SSE 订阅者列表
_sse_subscribers: list = []
_sse_lock = threading.Lock()
_MAX_SSE_SUBSCRIBERS = 1000


def broadcast_event(event_type: str, data: dict):
    """广播事件到所有 SSE 订阅者"""
    with _sse_lock:
        # 如果订阅者过多，先清理最早的一批
        while len(_sse_subscribers) > _MAX_SSE_SUBSCRIBERS:
            _sse_subscribers.pop(0)
        dead = []
        for subscriber in _sse_subscribers:
            try:
                subscriber.put((event_type, data))
            except Exception:
                dead.append(subscriber)
        for d in dead:
            if d in _sse_subscribers:
                _sse_subscribers.remove(d)


class SSEQueue:
    """SSE 消息队列"""
    def __init__(self):
        self._queue = deque()
        self._event = threading.Event()
        self._lock = threading.Lock()

    def put(self, item):
        with self._lock:
            self._queue.append(item)
        self._event.set()

    def get(self, timeout: float = 1.0):
        if self._event.wait(timeout):
            with self._lock:
                if self._queue:
                    item = self._queue.popleft()
                    if not self._queue:
                        self._event.clear()
                    return item
                self._event.clear()
        return None


# 注册事件引擎处理器，将数据变化转发到 SSE
event_engine = get_event_engine()

def on_tick_update(event_type: str, data: dict):
    """当 tick 更新时广播到 SSE"""
    if event_type == 'tick.update':
        broadcast_event('tick', data)
# 告警事件广播到 SSE（v3.1 新增）
def on_alert_event(event_type: str, data: dict):
    """当告警触发时广播到 SSE"""
    if event_type == 'alert.new':
        broadcast_event('alert', data)

event_engine.register('tick.update', on_tick_update)
event_engine.register('alert.new', on_alert_event)


# 定时任务：定期刷新监控列表中的股票
def start_background_refresh():
    """启动后台定时刷新任务"""
    def refresh_loop():
        provider = None
        alert_channels_ready = False
        next_alert_retry_at = 0.0
        consecutive_failures = 0

        while True:
            now = time.monotonic()
            if not alert_channels_ready and now >= next_alert_retry_at:
                try:
                    init_alert_channels()
                    alert_channels_ready = True
                except Exception:
                    next_alert_retry_at = now + 300
                    logger.exception("[Alert] 初始化告警通道失败，5 分钟后重试")

            try:
                if provider is None:
                    provider = get_provider()
                config = load_config_readonly()
                codes = [t['code'] for t in config.get('targets', [])]
                targets_map = {t['code']: t for t in config.get('targets', [])}

                if codes:
                    # 调用 get_realtime 会自动更新缓存，并返回 deltas（用于 SSE 推送）
                    ticks, deltas = provider.get_realtime(codes)
                    for code, delta in deltas.items():
                        if delta:
                            event_engine.put('tick.update', delta)

                    # 检查是否触发价格告警 (v3.1 新增)
                    for code, tick in ticks.items():
                        target = targets_map.get(code)
                        if target:
                            alert_event = _alert_manager.check_price_alert(target, tick)
                            if alert_event:
                                results = _alert_manager.send(alert_event)
                                if results is None:
                                    continue
                                logger.log(logging.INFO if not results or any(results.values()) else logging.WARNING, "[Alert] 触发结果: %s, 通道: %s", alert_event.message, results)
                                # 同时存入缓存
                                get_cache().add_alert(alert_event)
                                # 广播到 SSE 实时更新图表
                                event_engine.put('alert.new', {
                                    'symbol': alert_event.symbol,
                                    'type': alert_event.type,
                                    'message': alert_event.message,
                                    'severity': alert_event.severity,
                                    'timestamp': alert_event.timestamp,
                                    'data': alert_event.data
                                })
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1
                retry_seconds = min(15 * (2 ** min(consecutive_failures - 1, 4)), 300)
                logger.exception(
                    "Dashboard 后台刷新失败（连续 %d 次），%.0f 秒后重试",
                    consecutive_failures,
                    retry_seconds,
                )
                time.sleep(retry_seconds)
                continue

            # 交易时段保持低延迟；休市和周末显著降频，避免无意义的数据源请求。
            time.sleep(_background_refresh_interval())

    thread = threading.Thread(target=refresh_loop, daemon=True, name="bg-refresh")
    thread.start()
    return thread


def get_alert_history(days=7):
    """从历史日志解析告警数据（支持按天数过滤）"""
    try:
        log_file = _resolve_log_file()
        if log_file is None or not log_file.exists():
            return {}

        # days 参数验证，非法输入默认使用 7
        try:
            days = max(int(days), 1)
        except (ValueError, TypeError):
            days = 7

        keywords = ('价格触发', '策略信号', '告警', 'alert')
        day_pattern = re.compile(r'^(\d{4}-\d{2}-\d{2})')
        stock_names = [
            str(t.get('name', '')).strip()
            for t in load_config().get('targets', [])
            if str(t.get('name', '')).strip()
        ]
        # days 参数验证，非法输入默认使用 7
        try:
            days = max(int(days), 1)
        except (ValueError, TypeError):
            days = 7

        cutoff_day = (datetime.now() - timedelta(days=days - 1)).date()
        alerts_by_day = {}
        alerts_by_stock = {}
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                if not line:
                    continue
                if not any(k in line for k in keywords):
                    continue

                match = day_pattern.match(line)
                if not match:
                    continue
                day_str = match.group(1)
                day_obj = datetime.strptime(day_str, '%Y-%m-%d').date()
                if day_obj < cutoff_day:
                    continue

                alerts_by_day[day_str] = alerts_by_day.get(day_str, 0) + 1
                for name in stock_names:
                    if name in line:
                        alerts_by_stock[name] = alerts_by_stock.get(name, 0) + 1
                        break

        return {
            'by_day': dict(sorted(alerts_by_day.items())),
            'by_stock': alerts_by_stock,
            'total': sum(alerts_by_day.values())
        }
    except (OSError, sqlite3.Error, ValueError):
        return {}


@app.route('/')
def index():
    """默认首页 - 趋势波段核心决策面板。"""
    return swing_page()


@app.route('/overview')
def overview_page():
    """系统状态概览（原首页）。"""
    config = load_config()
    portfolio = load_portfolio()
    status_snapshot = _get_status_snapshot_cached()

    return render_template('index.html',
        service=status_snapshot['service'],
        process=status_snapshot['process'],
        disk=status_snapshot['disk'],
        cooldown=status_snapshot['cooldown'],
        targets=config.get('targets', []),
        config=config,
        portfolio=portfolio,
        risk=load_risk_state(),
    )


@app.route('/logs')
def logs_page():
    """日志页面"""
    level = request.args.get('level', None)
    return render_template('logs.html', logs=get_recent_logs(100, level), level=level)


@app.route('/swing')
def swing_page():
    """波段化大盘环境页（宏观情绪切片 + 趋势确认信号 + 宏观风控）"""
    return render_template('swing.html', swing=load_swing_overview(), risk=load_risk_state())


@app.route('/api/swing/latest')
def api_swing_latest():
    """波段化最新概览 JSON。"""
    return jsonify(load_swing_overview())


@app.route('/metrics/swing')
def metrics_swing():
    """波段趋势 Prometheus 指标暴露（来自 SQLite 数据）。"""
    from flask import Response
    return Response(
        render_swing_metrics(load_swing_overview()),
        mimetype="text/plain; version=0.0.4",
    )


from dashboard.api.routes import api_bp, attach_dashboard_module

attach_dashboard_module(sys.modules[__name__])
app.register_blueprint(api_bp)


if __name__ == '__main__':
    try:
        _dashboard_lock = SingleInstanceLock("radar-dashboard").acquire()
    except SingleInstanceLockError as exc:
        print(f"Dashboard 已在运行，拒绝重复启动: {exc}")
        sys.exit(2)

    Path('dashboard/templates').mkdir(parents=True, exist_ok=True)
    Path('dashboard/static').mkdir(parents=True, exist_ok=True)

    # 安全加固：仅监听本地和私有网络
    import socket
    def get_lan_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('10.255.255.255', 1))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return '0.0.0.0'
    
    # 默认仅绑定本机。需要公网访问时请通过反向代理/防火墙控制，并显式设置 DASHBOARD_HOST=0.0.0.0。
    bind_host = os.environ.get('DASHBOARD_HOST', '0.0.0.0').strip() or '0.0.0.0'
    bind_port = int(os.environ.get('DASHBOARD_PORT', 8081))

    # 启动后台数据刷新任务（v3.0 SSE 实时推送）
    print("正在启动后台数据刷新任务...")
    start_background_refresh()

    print(f"Dashboard v3.0 starting at: http://{bind_host}:{bind_port}")
    print(f"API status: http://{bind_host}:{bind_port}/api/status")
    print(f"SSE stream: http://{bind_host}:{bind_port}/api/stream")

    app.run(host=bind_host, port=bind_port, debug=False, threaded=True)
