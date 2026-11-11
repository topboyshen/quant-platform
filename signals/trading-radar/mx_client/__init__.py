"""
东方财富妙想 API 客户端

从 mx_client.py 拆分重构后的包结构：
- ``parsers`` : 纯函数响应解析工具
- ``client``  : MXDataClient 主类
- ``helpers`` : 便捷函数与高级查询封装

向后兼容：所有原 ``mx_client`` 模块的导出均保留。
"""

from __future__ import annotations

from .client import MXDataClient
from .helpers import (
    get_index_history_mx,
    get_margin_stock_history_mx,
    get_margin_total_history_mx,
    get_market_sentiment_mx,
    get_money_flow_mx,
    get_mx_client,
    get_sector_change_mx,
    get_stock_history_mx,
    get_stock_realtime_mx,
    get_stock_sector_mx,
)
from .parsers import (
    ensure_ohlcv_columns,
    extract_first_table_value,
    normalize_column_name,
    parse_history_data,
    parse_money_flow_table,
    parse_numeric_value,
    parse_realtime_data,
    parse_sector_list,
)

__all__ = [
    "MXDataClient",
    "get_mx_client",
    "get_stock_history_mx",
    "get_stock_realtime_mx",
    "get_market_sentiment_mx",
    "get_money_flow_mx",
    "get_stock_sector_mx",
    "get_sector_change_mx",
    "get_index_history_mx",
    "get_margin_total_history_mx",
    "get_margin_stock_history_mx",
    "parse_numeric_value",
    "normalize_column_name",
    "ensure_ohlcv_columns",
    "parse_history_data",
    "parse_realtime_data",
    "extract_first_table_value",
    "parse_money_flow_table",
    "parse_sector_list",
]
