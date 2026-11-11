"""Regression coverage for ML training's unified config loading."""

from __future__ import annotations

import importlib
import json
import logging
import sys
from types import ModuleType


def _write_config_pair(tmp_path, *, base_target, local_target):
    base_config = {
        "telegram": {"bot_token": "base-token", "chat_id": "base-chat"},
        "radar": {
            "cooldown_minutes": 5,
            "scan_interval_seconds": 60,
            "cache_file": "cache.json",
            "log_file": "radar.log",
        },
        "market": {
            "morning_start": "09:30",
            "morning_end": "11:30",
            "afternoon_start": "13:00",
            "afternoon_end": "15:00",
        },
        "targets": [base_target],
    }
    (tmp_path / "config.json").write_text(
        json.dumps(base_config, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "config.local.json").write_text(
        json.dumps({"targets": [local_target]}, ensure_ascii=False), encoding="utf-8"
    )


def _target(name):
    return {
        "code": "000001",
        "name": name,
        "trigger_price": 10,
        "direction": "below",
    }


def _load_training_compat(monkeypatch):
    """Import the script while replacing optional runtime-only dependencies."""
    pandas = ModuleType("pandas")
    pandas.DataFrame = object
    requests = ModuleType("requests")
    requests.post = lambda *args, **kwargs: None
    mx_client = ModuleType("mx_client")
    mx_client.get_stock_history_mx = lambda code, name, days: None
    akshare = ModuleType("akshare")
    unified_history = ModuleType("radar.data.unified_history")
    unified_history.log_history_lineage = lambda *args, **kwargs: None
    unified_history.normalize_history_df = lambda frame, **kwargs: frame

    monkeypatch.setitem(sys.modules, "pandas", pandas)
    monkeypatch.setitem(sys.modules, "requests", requests)
    monkeypatch.setitem(sys.modules, "mx_client", mx_client)
    monkeypatch.setitem(sys.modules, "akshare", akshare)
    monkeypatch.setitem(
        sys.modules, "radar.data.unified_history", unified_history
    )
    monkeypatch.delitem(sys.modules, "radar.ml.training_compat", raising=False)
    monkeypatch.setattr(
        logging,
        "FileHandler",
        lambda *args, **kwargs: logging.NullHandler(),
    )
    return importlib.import_module("radar.ml.training_compat")


def test_history_fallback_resolves_stock_name_from_local_overlay(
    tmp_path, monkeypatch
):
    _write_config_pair(
        tmp_path,
        base_target=_target("tracked-name"),
        local_target=_target("overlay-name"),
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RADAR_LOCAL_CONFIG", raising=False)
    monkeypatch.setenv("RADAR_ML_HISTORY_FALLBACK", "1")
    monkeypatch.setenv("RADAR_KLINE_PARQUET_PREFER_READ", "0")
    monkeypatch.setenv("RADAR_KLINE_PARQUET_AUTO_WRITE", "0")

    market_data = ModuleType("radar.data.market_data")

    class FakeMarketDataFetcher:
        def __init__(self, logger, use_akshare):
            pass

        def get_historical_data(self, code, days):
            return None

    market_data.MarketDataFetcher = FakeMarketDataFetcher
    parquet_store = ModuleType("radar.data.ohlcv_parquet_store")
    parquet_store.max_age_days_from_env = lambda: None
    parquet_store.maybe_persist_fetched_kline = lambda *args, **kwargs: None
    parquet_store.prefer_read_enabled = lambda: False
    parquet_store.try_load_kline_parquet = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "radar.data.market_data", market_data)
    monkeypatch.setitem(
        sys.modules, "radar.data.ohlcv_parquet_store", parquet_store
    )

    training_compat = _load_training_compat(monkeypatch)

    mx_calls = []
    monkeypatch.setattr(
        training_compat,
        "get_stock_history_mx",
        lambda code, name, days: mx_calls.append((code, name, days)),
    )
    monkeypatch.setattr(training_compat, "AKSHARE_AVAILABLE", False)
    monkeypatch.setattr(
        training_compat, "_get_stock_history_baostock", lambda code, days: None
    )

    assert training_compat.get_stock_history_mkt("000001", days=42) is None
    assert mx_calls == [("000001", "overlay-name", 42)]


def test_train_all_models_uses_overlay_targets_and_merged_secrets(
    tmp_path, monkeypatch
):
    _write_config_pair(
        tmp_path,
        base_target=_target("tracked-name"),
        local_target=_target("overlay-name"),
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RADAR_LOCAL_CONFIG", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_FILE", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID_FILE", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "env-chat")

    training_compat = _load_training_compat(monkeypatch)

    predictor_calls = []
    predictor_module = ModuleType("radar.ml.predictor")

    class FakePredictor:
        def __init__(self, *, model_dir, logger):
            predictor_calls.append((model_dir, logger))

    predictor_module.MLPredictor = FakePredictor
    monkeypatch.setitem(sys.modules, "radar.ml.predictor", predictor_module)

    trained_targets = []

    def fake_train_single(predictor, target):
        trained_targets.append(target)
        return True, {
            "accuracy": 0.8,
            "precision": 0.7,
            "recall": 0.6,
            "samples": 25,
        }, None

    notifications = []
    monkeypatch.setattr(training_compat, "_train_single", fake_train_single)
    monkeypatch.setattr(training_compat.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        training_compat,
        "send_telegram_notification",
        lambda token, chat_id, message: notifications.append(
            (token, chat_id, message)
        ),
    )

    training_compat.train_all_models()

    assert predictor_calls == [("models", training_compat.logger)]
    assert [target["name"] for target in trained_targets] == ["overlay-name"]
    assert len(notifications) == 1
    token, chat_id, report = notifications[0]
    assert (token, chat_id) == ("env-token", "env-chat")
    assert "overlay-name" in report
    assert "tracked-name" not in report
