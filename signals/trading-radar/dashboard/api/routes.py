"""
Dashboard API 路由（Flask Blueprint）。

由 dashboard.app 在加载末尾调用 attach_dashboard_module(sys.modules[__name__])
完成绑定，以便视图访问 app 模块上的缓存与工具函数。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from flask import Blueprint, Response, jsonify, request, stream_with_context

from dashboard import service_auth

api_bp = Blueprint("dashboard_api", __name__)

_DASHBOARD_TOKEN_FILE_ENV = "DASHBOARD_API_TOKEN_FILE"

SENSITIVE_API_PREFIXES = (
    "/api/logs",
    "/api/portfolio",
    "/api/battlefield",
    "/api/monitor_config",
    "/api/stream",
    "/api/prices",
    "/api/targets",
    "/api/history",
)


def _dashboard_token() -> str:
    return service_auth.read_token_file(_DASHBOARD_TOKEN_FILE_ENV)


def _dashboard_auth_error(status: int) -> Response:
    response = jsonify(
        {"error": "service_unavailable" if status == 503 else "unauthorized"}
    )
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@api_bp.before_request
def require_dashboard_token():
    if not request.path.startswith(SENSITIVE_API_PREFIXES):
        return None

    expected = _dashboard_token()
    if not expected:
        return _dashboard_auth_error(503)

    # Header-only by design: query-string secrets leak into access logs,
    # browser history, referrers, screenshots and monitoring traces.
    supplied = request.headers.get("X-Dashboard-Token", "")
    if not service_auth.constant_time_matches(supplied, expected):
        return _dashboard_auth_error(401)
    return None


_dash_mod = None


def attach_dashboard_module(mod) -> None:
    """由 app.py 传入自身模块，供 API 视图访问内部实现。"""
    global _dash_mod
    _dash_mod = mod


def _d() -> Any:
    if _dash_mod is None:
        raise RuntimeError("Dashboard API 未绑定：app 加载时应调用 attach_dashboard_module")
    return _dash_mod


@api_bp.route("/api/stream")
def api_stream() -> Response:
    """SSE 实时数据流"""
    m = _d()
    with m._sse_lock:
        if len(m._sse_subscribers) >= m._MAX_SSE_SUBSCRIBERS:
            return jsonify(
                {"error": "SSE subscribers limit reached", "limit": m._MAX_SSE_SUBSCRIBERS}
            ), 503
        subscriber = m.SSEQueue()
        m._sse_subscribers.append(subscriber)

    def generate():
        try:
            cache = m.get_cache()
            ticks = cache.get_all_ticks()
            if ticks:
                yield f"event: init\ndata: {json.dumps({'ticks': {k: v.to_dict() for k, v in ticks.items()}})}\n\n"

            while True:
                item = subscriber.get(timeout=5.0)
                if item:
                    event_type, data = item
                    yield f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
                else:
                    yield f"event: heartbeat\ndata: {json.dumps({'time': datetime.now().isoformat()})}\n\n"
        finally:
            with m._sse_lock:
                if subscriber in m._sse_subscribers:
                    m._sse_subscribers.remove(subscriber)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@api_bp.route("/api/status")
def api_status() -> Response:
    """API: 系统状态 JSON"""
    m = _d()
    snapshot = m._get_status_snapshot_cached()
    return jsonify(
        {
            "service": snapshot.get("service"),
            "dashboard_service": snapshot.get("dashboard_service"),
            "swing_service": snapshot.get("swing_service"),
            "swing_timer": snapshot.get("swing_timer"),
            "process": snapshot.get("process"),
            "disk": snapshot.get("disk"),
            "cooldown": snapshot.get("cooldown"),
            "monitor_health": snapshot.get("monitor_health"),
            "generated_at": snapshot.get("generated_at"),
            "partial_failure": snapshot.get("partial_failure", False),
            "errors": snapshot.get("errors", {}),
            "timestamp": datetime.now().isoformat(),
        }
    )


@api_bp.route("/api/health")
def api_health_aggregate() -> Response:
    """API: 聚合健康检查（dashboard 自身 + 后端 monitor 端点）

    /health 是 dashboard 进程的轻量探活；
    /api/health 额外透传 monitor (8080) 的健康状态，
    便于外部探针/脚本一次性拿到完整状态。
    """
    import urllib.error
    import urllib.request

    snapshot = _d()._get_status_snapshot_cached()
    monitor_status: dict[str, Any] = {"reachable": False}

    # Compose 中 dashboard 与 monitor 是独立容器，地址必须可配置。
    monitor_url = os.getenv(
        "RADAR_MONITOR_HEALTH_URL", "http://127.0.0.1:8080/health"
    ).strip()
    try:
        with urllib.request.urlopen(monitor_url, timeout=1.5) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            monitor_status = {
                "reachable": True,
                "http_status": resp.status,
                "body": json.loads(body) if body else {},
            }
    except (urllib.error.URLError, OSError, ValueError) as exc:
        monitor_status = {"reachable": False, "error": str(exc)}

    payload = {
        "status": "ok",
        "service": "stock-radar-dashboard",
        "process": snapshot.get("process"),
        "monitor": monitor_status,
        "timestamp": datetime.now().isoformat(),
    }
    return jsonify(payload)


@api_bp.route("/api/prices")
def api_prices() -> Response:
    """API: 实时股价"""
    m = _d()
    config = m.load_config_readonly()
    codes = [t["code"] for t in config.get("targets", [])]
    prices = m._get_live_prices_cached(codes)
    return jsonify({"prices": prices, "timestamp": datetime.now().isoformat()})


@api_bp.route("/api/portfolio")
def api_portfolio() -> Response:
    """API: 持仓信息（优先 battlefield_state.db，fallback portfolio.json）"""
    m = _d()
    try:
        from radar.core.battlefield_state import BattlefieldStateManager

        bsm = BattlefieldStateManager()
        snapshot = bsm.get_position_snapshot()
        if snapshot and snapshot.get("positions"):
            positions = []
            for pos in snapshot["positions"]:
                positions.append(
                    {
                        "code": pos["code"],
                        "shares": pos["shares"],
                        "avg_cost": pos["cost"],
                        "current_price": pos["last_price"],
                        "pnl_pct": round(pos.get("pnl_pct", 0), 2),
                    }
                )
            portfolio = {
                "cash": snapshot["cash"],
                "initial_capital": snapshot["initial_cash"],
                "positions": positions,
                "source": "battlefield_state",
            }
            if positions:
                prices = m._get_live_prices_cached([p["code"] for p in positions])
                for pos in positions:
                    code = pos["code"]
                    if (
                        code in prices
                        and prices[code].get("source") != "error"
                        and prices[code].get("price", 0) > 0
                    ):
                        pos["current_price"] = prices[code]["price"]
                        pos["change_pct"] = prices[code]["change_pct"]
                        avg_cost = float(pos.get("avg_cost", 0) or 0)
                        shares = float(pos.get("shares", 0) or 0)
                        pos["pnl"] = round((prices[code]["price"] - avg_cost) * shares, 2)
                        pos["pnl_pct"] = (
                            round((prices[code]["price"] - avg_cost) / avg_cost * 100, 2)
                            if avg_cost > 0
                            else 0.0
                        )
            return jsonify(portfolio)
    except Exception as e:
        m.logger.debug(f"[Dashboard] 读取 battlefield_state 失败: {e}")

    portfolio = m.load_portfolio()

    if portfolio.get("positions"):
        prices = m._get_live_prices_cached([p["code"] for p in portfolio["positions"]])
        for pos in portfolio["positions"]:
            code = pos["code"]
            if (
                code in prices
                and prices[code].get("source") != "error"
                and prices[code].get("price", 0) > 0
            ):
                pos["current_price"] = prices[code]["price"]
                pos["change_pct"] = prices[code]["change_pct"]
                avg_cost = float(pos.get("avg_cost", 0) or 0)
                shares = float(pos.get("shares", 0) or 0)
                pos["pnl"] = round((prices[code]["price"] - avg_cost) * shares, 2)
                pos["pnl_pct"] = (
                    round((prices[code]["price"] - avg_cost) / avg_cost * 100, 2)
                    if avg_cost > 0
                    else 0.0
                )

    return jsonify(portfolio)


@api_bp.route("/api/logs")
def api_logs() -> Response:
    """API: 日志"""
    m = _d()
    limit = m._get_int_arg("limit", 100, 1, 1000)
    level = request.args.get("level", None)
    return jsonify(m.get_recent_logs(limit, level))


@api_bp.route("/api/targets")
def api_targets() -> Response:
    """API: 监控目标（含实时价格）"""
    m = _d()
    config = m.load_config()
    targets = config.get("targets", [])

    codes = [t["code"] for t in targets]
    prices = m._get_live_prices_cached(codes)

    for t in targets:
        code = t["code"]
        if code in prices and "error" not in prices[code]:
            price_info = prices[code]
            current = price_info.get("price", 0)
            t["current_price"] = current
            t["change_pct"] = price_info.get("change_pct", 0)
            t["volume"] = price_info.get("volume", 0)
            try:
                trigger = float(t.get("trigger_price", 0) or 0)
            except (TypeError, ValueError):
                trigger = 0.0
            if trigger > 0:
                if t.get("direction") == "above":
                    t["distance_pct"] = round((current - trigger) / trigger * 100, 2)
                else:
                    t["distance_pct"] = round((trigger - current) / trigger * 100, 2)

    return jsonify(targets)


@api_bp.route("/api/monitor_config")
def api_monitor_config() -> Response:
    """API: 监控配置"""
    m = _d()
    config_path = m.BASE_DIR / "dashboard" / "config" / "monitors.json"
    try:
        return jsonify(m._load_json_cached(config_path, {}))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_bp.route("/api/history")
def api_history() -> Response:
    """API: 历史统计数据"""
    m = _d()
    days = m._get_int_arg("days", 7, 1, 365)
    return jsonify(m.get_alert_history(days))


@api_bp.route("/api/battlefield")
def api_battlefield() -> Response:
    """API: 阵地状态"""
    m = _d()
    try:
        from radar.core.battlefield_state import BattlefieldStateManager

        bsm = BattlefieldStateManager()
        limit = m._get_int_arg("alert_limit", 50, 1, 500)
        code = request.args.get("code", None)
        return jsonify(
            {
                "stats": bsm.get_stats(),
                "position_snapshot": bsm.get_position_snapshot(),
                "alert_history": bsm.get_alert_history(code=code, limit=limit),
                "timestamp": datetime.now().isoformat(),
            }
        )
    except Exception as e:
        m.logger.warning(f"[Dashboard] /api/battlefield 失败: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.route("/api/capital_flow")
def api_capital_flow() -> Response:
    """API: 个股资金流向"""
    m = _d()
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "Missing code parameter"}), 400
    if not m.AKSHARE_AVAILABLE:
        return jsonify({"error": "Akshare not available"}), 503
    return jsonify(m.get_capital_flow(code))


@api_bp.route("/api/market_sentiment")
def api_market_sentiment() -> Response:
    """API: 市场情绪指标"""
    m = _d()
    if not m.AKSHARE_AVAILABLE:
        return jsonify({"error": "Akshare not available"}), 503
    return jsonify(m.get_market_sentiment())


@api_bp.route("/api/cache_metrics")
def api_cache_metrics() -> Response:
    """API: 缓存命中统计

    prices 指标统一来自 dashboard.data_provider.DataCache（per-code TTL 缓存层）。
    status_snapshot 仍是 dashboard 自身的进程命令快照缓存，独立维护。
    """
    m = _d()
    cache = m.get_cache()
    cache_metrics = cache.get_metrics() if cache is not None else {
        "tick_hits": 0,
        "tick_misses": 0,
        "tick_hit_ratio_pct": 0.0,
        "tick_cache_size": 0,
    }

    with m._runtime_cache_lock:
        status_metrics = dict(m._runtime_cache["metrics"])

    status_total = status_metrics["status_hit"] + status_metrics["status_miss"]
    status_ratio = (
        round((status_metrics["status_hit"] / status_total * 100), 2)
        if status_total
        else 0.0
    )

    return jsonify(
        {
            "prices": {
                "hit": cache_metrics["tick_hits"],
                "miss": cache_metrics["tick_misses"],
                "hit_ratio_pct": cache_metrics["tick_hit_ratio_pct"],
                "cache_size": cache_metrics["tick_cache_size"],
            },
            "status_snapshot": {
                "hit": status_metrics["status_hit"],
                "miss": status_metrics["status_miss"],
                "hit_ratio_pct": status_ratio,
            },
            "timestamp": datetime.now().isoformat(),
        }
    )


@api_bp.route("/api/tools_status")
def api_tools_status() -> Response:
    """API: 工具集运行状态"""
    m = _d()
    tools_dir = m.BASE_DIR / "tools"

    tools = {
        "radarctl": {"exists": (tools_dir / "radarctl.py").exists(), "desc": "系统管理控制台"},
        "mx_query": {"exists": (tools_dir / "mx_query.py").exists(), "desc": "妙想数据查询"},
        "dfcf_spider": {"exists": (tools_dir / "dfcf_spider.py").exists(), "desc": "东方财富爬虫"},
        "web_screenshot": {
            "exists": (tools_dir / "web_screenshot.py").exists(),
            "desc": "网页截图",
        },
        "auto_monitor": {"exists": (tools_dir / "auto_monitor.py").exists(), "desc": "自动监控"},
        "daily_report": {"exists": (tools_dir / "daily_report.py").exists(), "desc": "每日报告"},
    }

    return jsonify(tools)


@api_bp.route("/health")
def api_health() -> Response:
    """健康检查端点（Docker/K8s 用）"""
    return jsonify({"status": "ok"}), 200
