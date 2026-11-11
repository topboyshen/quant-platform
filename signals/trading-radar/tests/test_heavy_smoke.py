"""全量环境（sklearn + stable-baselines3）下的导入冒烟；无依赖时 skip。"""

import pytest


def test_sklearn_ml_predictor_imports():
    pytest.importorskip("sklearn")
    from ml_predictor import FeatureEngineer, MLPredictor  # noqa: F401

    fe = FeatureEngineer
    assert fe.calculate_rsi([1.0, 1.1, 1.0] * 10) is not None


def test_stable_baselines3_import():
    pytest.importorskip("stable_baselines3")
    from stable_baselines3 import PPO  # noqa: F401

    assert PPO is not None
