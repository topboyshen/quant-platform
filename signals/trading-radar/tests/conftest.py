"""共享 fixtures：路径、配置片段、隔离日志。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

# 保证 `import radar_monitor_v3` 等与从项目根运行一致
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _isolate_battlefield_for_monitor_tests(tmp_path_factory, monkeypatch):
    """
    MonitorEngine 使用 BattlefieldStateManager 单例 + 默认 DB 时易在测试间串数据。
    为每个用例分配独立 BATTLEFIELD_STATE_DB 并清空单例；显式传入 db_path 的用例不受影响。
    """
    from radar.core.battlefield_state import BattlefieldStateManager
    from radar.risk.macro_risk_control import MacroRiskControl

    db = tmp_path_factory.mktemp("battlefield_iso") / "state.db"
    kline_dir = tmp_path_factory.mktemp("kline_iso")
    monkeypatch.setenv("BATTLEFIELD_STATE_DB", str(db))
    monkeypatch.setenv("RADAR_KLINE_PARQUET_DIR", str(kline_dir))
    monkeypatch.setattr(MacroRiskControl, "_STATE_FILE", db.parent / "risk_state.json")
    monkeypatch.setattr(MacroRiskControl, "_OVERRIDE_FILE", db.parent / "risk_override.json")
    BattlefieldStateManager._instance = None
    yield
    BattlefieldStateManager._instance = None


@pytest.fixture
def project_root() -> Path:
    return _ROOT


@pytest.fixture
def null_logger() -> logging.Logger:
    log = logging.getLogger(f"pytest.{id(object())}")
    log.handlers.clear()
    log.addHandler(logging.NullHandler())
    log.setLevel(logging.CRITICAL)
    return log


@pytest.fixture
def minimal_config_dict():
    """构造 MonitorConfig / load_monitor_bundle 所需的最小 JSON 结构。"""
    return {
        "telegram": {"bot_token": "t", "chat_id": "1"},
        "radar": {
            "cooldown_minutes": 5,
            "scan_interval_seconds": 60,
            "cache_file": "radar_cache.json",
            "log_file": "radar.log",
        },
        "market": {
            "morning_start": "09:30",
            "morning_end": "11:30",
            "afternoon_start": "13:00",
            "afternoon_end": "15:00",
        },
        "alerts": {
            "ml_prediction": {"probability_threshold": 0.7, "min_history_bars": 30},
        },
        "targets": [
            {
                "code": "000001",
                "name": "测试",
                "trigger_price": 10.0,
                "direction": "below",
            }
        ],
    }
