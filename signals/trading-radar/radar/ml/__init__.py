"""Machine learning helpers used by strategies, training, and serving."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .predictor import FeatureEngineer, MLPredictor


def __getattr__(name: str):
    if name in {"FeatureEngineer", "MLPredictor"}:
        from .predictor import FeatureEngineer, MLPredictor

        return {"FeatureEngineer": FeatureEngineer, "MLPredictor": MLPredictor}[name]
    raise AttributeError(name)

__all__ = ["FeatureEngineer", "MLPredictor"]
