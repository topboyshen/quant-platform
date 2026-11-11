"""Authenticated QuantDinger facade for Trading Radar decision context."""

from flask import Blueprint, jsonify

from app.services.radar_context import RadarContextError, fetch_market_context
from app.utils.auth import login_required


radar_bp = Blueprint("radar_context", __name__)


@radar_bp.get("/api/radar/market-context")
@login_required
def market_context():
    try:
        context = fetch_market_context()
    except RadarContextError as exc:
        return (
            jsonify(
                {
                    "code": 0,
                    "msg": exc.message,
                    "data": None,
                    "error": {
                        "type": exc.code,
                        "retriable": exc.retriable,
                    },
                }
            ),
            exc.http_status,
        )
    return jsonify({"code": 1, "msg": "success", "data": context})


def register(app) -> None:
    app.register_blueprint(radar_bp)
