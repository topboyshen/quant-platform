"""价格 / ML 告警在 MonitorEngine 内传递的结构（供类型检查与 IDE 提示）。"""

from __future__ import annotations

from typing import Literal, TypedDict


class PriceTriggerAlertDict(TypedDict):
    type: Literal["price_trigger"]
    price: float
    trigger_price: float
    direction: str
    distance_pct: float


class MLPredictionAlertDict(TypedDict):
    type: Literal["ml_prediction"]
    prediction: str
    probability: float
    trigger_price: float
    triggered: bool
    current_price: float
