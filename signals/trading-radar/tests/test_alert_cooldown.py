"""告警冷却 SQLite 行为。"""

import time

from radar.alert.cooldown import AlertCooldownManager


def test_record_and_cooldown(tmp_path, null_logger):
    cache = tmp_path / "radar_cache.json"
    m = AlertCooldownManager(str(cache), null_logger)
    assert m.is_in_cooldown("000001", "price", cooldown_minutes=60) is False
    m.record_alert("000001", "price")
    assert m.is_in_cooldown("000001", "price", cooldown_minutes=60) is True
    assert m.is_in_cooldown("000001", "ml", cooldown_minutes=60) is False


def test_cooldown_expires(monkeypatch, tmp_path, null_logger):
    cache = tmp_path / "radar_cache.json"
    m = AlertCooldownManager(str(cache), null_logger)
    t0 = 1_000_000.0
    monkeypatch.setattr(time, "time", lambda: t0)
    m.record_alert("000001", "price")
    monkeypatch.setattr(time, "time", lambda: t0 + 60 * 60 + 1)
    assert m.is_in_cooldown("000001", "price", cooldown_minutes=60) is False
