from __future__ import annotations

import pandas as pd

from radar.ml.predictor_compat import MLPredictor


def test_ml_split_is_chronological_not_random(tmp_path):
    predictor = MLPredictor(model_dir=str(tmp_path))
    X = pd.DataFrame({"feature": list(range(100))})
    y = pd.Series([0] * 60 + [1] * 40)

    X_train, X_test, y_train, y_test = predictor._split_data(X, y)

    assert X_train["feature"].tolist() == list(range(80))
    assert X_test["feature"].tolist() == list(range(80, 100))
    assert y_train.index.tolist() == list(range(80))
    assert y_test.index.tolist() == list(range(80, 100))
