"""实盘行情 Pydantic 契约。"""

import pytest
from pydantic import ValidationError

from radar.schemas.live_quote import LiveQuoteSnapshot, validate_live_quote_dict


def _valid_payload():
    return {
        "code": "1",
        "name": "测试",
        "price": 10.5,
        "volume": 1000,
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "prev_close": 10.2,
        "change_pct": 1.2,
        "extra_from_api": 123,
    }


def test_validate_live_quote_normalizes_code_and_keeps_extra():
    d = validate_live_quote_dict(_valid_payload())
    assert d["code"] == "000001"
    assert d["price"] == 10.5
    assert d["extra_from_api"] == 123


def test_high_less_than_low_auto_fixed():
    """high < low 时自动交换，而非报错（数据源偶发颠倒）"""
    p = _valid_payload()
    p["high"] = 5.0
    p["low"] = 10.0
    d = validate_live_quote_dict(p)
    assert d["high"] == 10.0
    assert d["low"] == 5.0


def test_price_non_positive_rejected():
    p = _valid_payload()
    p["price"] = 0.0
    with pytest.raises(ValidationError):
        LiveQuoteSnapshot.model_validate(p)


def test_change_pct_optional():
    p = _valid_payload()
    del p["change_pct"]
    d = validate_live_quote_dict(p)
    assert d.get("change_pct") is None
