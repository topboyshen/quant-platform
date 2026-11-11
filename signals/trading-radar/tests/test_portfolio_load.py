"""MonitorEngine._load_real_portfolio：损坏或非 JSON 文件。"""

import logging

import pytest

from radar.core.monitor_engine import MonitorConfig, MonitorEngine


@pytest.fixture
def engine_min(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = logging.getLogger(f"test_portfolio.{id(tmp_path)}")
    log.handlers.clear()
    log.setLevel(logging.WARNING)
    log.propagate = True
    cfg = MonitorConfig(
        bot_token="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz1234",
        chat_id="1",
        cooldown_minutes=5,
        scan_interval=60,
        cache_file=str(tmp_path / "c.json"),
        log_file=str(tmp_path / "l.log"),
        market_hours={
            "morning_start": "09:30",
            "morning_end": "11:30",
            "afternoon_start": "13:00",
            "afternoon_end": "15:00",
        },
        health_port=0,
    )
    return MonitorEngine(cfg, log)


def test_load_portfolio_invalid_json_logs_warning(engine_min, tmp_path, caplog):
    p = tmp_path / "portfolio.json"
    p.write_text("{ not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=engine_min.logger.name):
        engine_min._load_real_portfolio(str(p))
    assert "加载 portfolio.json 失败" in caplog.text


def test_load_portfolio_missing_positions_key_no_crash(engine_min, tmp_path):
    p = tmp_path / "portfolio.json"
    p.write_text('{"account": {}}', encoding="utf-8")
    engine_min._load_real_portfolio(str(p))
    assert engine_min._portfolio.positions == {}


def test_load_portfolio_skips_bad_position_rows(engine_min, tmp_path, caplog):
    p = tmp_path / "portfolio.json"
    p.write_text(
        '{"positions": [{"code": null, "shares": 100}, {"code": "000001", "shares": 0}]}',
        encoding="utf-8",
    )
    engine_min._load_real_portfolio(str(p))
    assert "000001" not in engine_min._portfolio.positions
