"""model_manifest：训练记录写入 models/manifest.json。"""

from __future__ import annotations

import pandas as pd
import pytest


def test_record_ml_training_run(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_MODEL_MANIFEST", str(tmp_path / "manifest.json"))

    from radar.model_manifest import load_manifest, record_ml_training_run

    df = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=5, freq="D"),
            "close": [10.0, 10.1, 10.2, 10.3, 10.4],
            "price": [10.0, 10.1, 10.2, 10.3, 10.4],
        }
    )
    record_ml_training_run(
        model_dir=str(tmp_path / "weights"),
        code="000001",
        direction="below",
        prediction_window=5,
        df=df,
        train_result={
            "success": True,
            "accuracy": 0.81,
            "precision": 0.72,
            "recall": 0.63,
            "samples": 100,
            "positive_samples": 12,
        },
    )

    m = load_manifest()
    assert "000001_below" in m["models"]
    row = m["models"]["000001_below"]
    assert row["metrics"]["accuracy"] == pytest.approx(0.81)
    assert row["data_range"]["rows"] == 5
    assert row["data_range"]["start"] is not None
    assert row["artifacts"]["model"].endswith("000001_below_model.pkl")


def test_record_skips_on_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_MODEL_MANIFEST", str(tmp_path / "manifest.json"))
    from radar.model_manifest import load_manifest, record_ml_training_run

    record_ml_training_run(
        model_dir=str(tmp_path / "w"),
        code="X",
        direction="above",
        prediction_window=3,
        df=pd.DataFrame(),
        train_result={"success": False},
    )
    m = load_manifest()
    assert m["models"] == {}
