"""
实盘行情快照 Pydantic 校验（进入策略链前的「刚性护甲」）。

- 校验必需字段、类型与基本 OHLC 逻辑（high >= low）。
- 允许额外字段（东财等接口常带多余键），校验后会写回 model_dump。
- 环境变量 RADAR_SKIP_LIVE_QUOTE_PYDANTIC=1 时由 MonitorEngine 跳过（仅调试用）。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

from pydantic import BaseModel, Field

try:  # Pydantic v2
    from pydantic import ConfigDict, field_validator, model_validator

    _PYDANTIC_V2 = True
except ImportError:  # Pydantic v1 from Ubuntu apt
    from pydantic import root_validator, validator

    ConfigDict = dict
    _PYDANTIC_V2 = False

    def field_validator(*fields, mode="after"):
        pre = mode == "before"

        def decorator(fn):
            return validator(*fields, pre=pre, allow_reuse=True)(fn)

        return decorator

    def model_validator(mode="after"):
        pre = mode == "before"

        def decorator(fn):
            if pre:
                return root_validator(pre=True, allow_reuse=True)(fn)

            def wrapper(cls, values):
                if isinstance(values, cls):
                    return fn(values)
                high = values.get("high")
                low = values.get("low")
                if high is not None and low is not None and high < low:
                    values["high"], values["low"] = low, high
                return values

            return root_validator(pre=False, allow_reuse=True)(wrapper)

        return decorator


class LiveQuoteSnapshot(BaseModel):
    """与 PriceTrigger / ML / RL 共用的行情 dict 契约。"""

    if _PYDANTIC_V2:
        model_config = ConfigDict(extra="allow", str_strip_whitespace=True)
    else:
        class Config:
            extra = "allow"
            anystr_strip_whitespace = True

    code: str
    name: str = ""
    price: float = Field(..., gt=0, description="最新价/现价")
    volume: int = Field(..., ge=0)
    open: float = Field(..., ge=0)
    high: float = Field(..., ge=0)
    low: float = Field(..., ge=0)
    prev_close: float = Field(..., ge=0)
    change_pct: Optional[float] = Field(default=None, description="涨跌幅%，可缺省")

    @field_validator("code", mode="before")
    @classmethod
    def _norm_code(cls, v: Any) -> str:
        if v is None:
            raise ValueError("code 不能为空")
        s = str(v).strip()
        if not s:
            raise ValueError("code 不能为空")
        return s.zfill(6)

    @field_validator("price", "open", "high", "low", "prev_close", mode="before")
    @classmethod
    def _required_finite_float(cls, v: Any) -> float:
        if v is None:
            raise ValueError("OHLC/价字段不能为 None")
        f = float(v)
        if not math.isfinite(f):
            raise ValueError("须为有限浮点数")
        return f

    @field_validator("change_pct", mode="before")
    @classmethod
    def _optional_change_pct(cls, v: Any) -> Optional[float]:
        if v is None or v == "":
            return None
        f = float(v)
        if not math.isfinite(f):
            raise ValueError("涨跌幅须为有限浮点数")
        return f

    @field_validator("volume", mode="before")
    @classmethod
    def _int_volume(cls, v: Any) -> int:
        if v is None:
            return 0
        return int(float(v))

    @model_validator(mode="after")
    def _ohlc_band(self) -> LiveQuoteSnapshot:
        if self.high < self.low:
            self.high, self.low = self.low, self.high
        return self


def validate_live_quote_dict(data: Mapping[str, Any]) -> Dict[str, Any]:
    """
    校验并归一化行情 dict，返回可回灌策略链的 plain dict（含原额外字段）。
    :raises pydantic.ValidationError: 不满足契约时
    """
    if _PYDANTIC_V2:
        model = LiveQuoteSnapshot.model_validate(dict(data))
        return model.model_dump(mode="python")
    model = LiveQuoteSnapshot.parse_obj(dict(data))
    return model.dict()
