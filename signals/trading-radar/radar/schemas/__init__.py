"""实盘与运行时结构化校验（Pydantic）。"""

from radar.schemas.live_quote import LiveQuoteSnapshot, validate_live_quote_dict

__all__ = ["LiveQuoteSnapshot", "validate_live_quote_dict"]
