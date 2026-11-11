"""MLStrategy：伪造 Predictor，不加载 sklearn / pkl。"""

from unittest.mock import MagicMock

from radar.strategy.ml_strategy import ML_FEATURE_ENGINE_MIN_BARS, MLStrategy


def test_should_alert_respects_threshold(null_logger):
    strat = MLStrategy(
        "000001",
        "below",
        10.0,
        threshold=0.8,
        min_history_bars=ML_FEATURE_ENGINE_MIN_BARS,
        logger=null_logger,
    )
    strat._price_history = [10.0 + i * 0.01 for i in range(ML_FEATURE_ENGINE_MIN_BARS)]
    strat._volume_history = [1000.0] * ML_FEATURE_ENGINE_MIN_BARS

    mock = MagicMock()
    mock.predict.return_value = {
        "probability": 0.5,
        "will_trigger": False,
        "confidence": "low",
    }
    strat._predictor = mock

    assert strat.should_alert({"price": 10.5, "volume": 2000}) is None
    mock.predict.assert_called_once()


def test_should_alert_fires_when_above_threshold(null_logger):
    strat = MLStrategy(
        "000001",
        "below",
        10.0,
        threshold=0.3,
        min_history_bars=30,
        logger=null_logger,
    )
    strat._price_history = [10.0] * 30
    strat._volume_history = [100.0] * 30
    mock = MagicMock()
    mock.predict.return_value = {
        "probability": 0.9,
        "will_trigger": True,
        "confidence": "high",
    }
    strat._predictor = mock
    out = strat.should_alert({"price": 10.0, "volume": 100})
    assert out is not None
    assert out["probability"] == 0.9
    assert "下破" in out["prediction"]
