"""
MXDataClient 便捷函数与高级查询封装。

从 mx_client.py 拆分出来的高层 API 函数，保持与原模块完全相同的调用签名。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from radar.data.mx_feature_cache import mx_cached

from .client import MXDataClient
from .parsers import (
    extract_first_table_value,
    parse_history_data,
    parse_money_flow_table,
    parse_numeric_value,
    parse_sector_list,
)


def get_mx_client() -> MXDataClient:
    """获取默认客户端实例"""
    return MXDataClient()


def get_stock_history_mx(code: str, name: str = None, days: int = 150) -> Optional[pd.DataFrame]:
    """便捷函数：获取股票历史数据"""
    client = get_mx_client()
    return client.get_stock_history(code, name, days)


def get_stock_realtime_mx(code: str, name: str = None) -> Optional[Dict[str, Any]]:
    """便捷函数：获取股票实时数据"""
    client = get_mx_client()
    return client.get_stock_realtime(code, name)


def get_market_sentiment_mx() -> Optional[Dict[str, Any]]:
    """
    获取市场情绪指标：涨跌停家数、北向资金流向

    Returns:
        {
            'limit_up_count': int,      # 涨停家数
            'limit_down_count': int,    # 跌停家数
            'northbound_net': float,    # 北向资金净流入（亿元）
        }
    """
    client = get_mx_client()
    try:
        limit_up_count = extract_first_table_value(
            client.query("今日涨停家数"), value_type="int"
        )
        limit_down_count = extract_first_table_value(
            client.query("今日跌停家数"), value_type="int"
        )

        # 北向资金需特殊处理（沪深合计，单位转换）
        result_north = client.query("北向资金净流入")
        northbound_net = 0.0
        if result_north.get("status") == 0:
            data = result_north.get("data", {}).get("data", {})
            dto_list = data.get("searchDataResultDTO", {}).get("dataTableDTOList", [])
            if dto_list:
                table = dto_list[0].get("table", {})
                if "0" in table and table["0"] and len(table["0"]) >= 2:
                    sh_net = float(table["0"][0]) if table["0"][0] else 0
                    sz_net = float(table["0"][1]) if table["0"][1] else 0
                    northbound_net = (sh_net + sz_net) / 10000

        return {
            "limit_up_count": limit_up_count,
            "limit_down_count": limit_down_count,
            "northbound_net": northbound_net,
        }
    except Exception as e:
        print(f"获取市场情绪数据失败: {e}")
        return None


def get_money_flow_mx(code: str, name: str = None) -> Optional[Dict[str, float]]:
    """
    获取个股资金流向数据

    Returns:
        {
            'main_net': float,      # 主力净额（亿元）
            'large_net': float,     # 大单净额（亿元）
            'medium_net': float,    # 中单净额（亿元）
            'small_net': float,     # 小单净额（亿元）
            'ddx': float,           # 当日DDX
            'ddy': float,           # 当日DDY
        }
    """
    client = get_mx_client()
    try:
        full_code = code.zfill(6)
        stock_name = name or ""
        query = f"{stock_name}{full_code}主力资金净流入"
        result = client.query(query)
        if result.get("status") != 0:
            return None

        data = result.get("data", {}).get("data", {})
        dto_list = data.get("searchDataResultDTO", {}).get("dataTableDTOList", [])
        if not dto_list:
            return None

        dto = dto_list[0]
        table = dto.get("table", {})
        return parse_money_flow_table(table)
    except Exception as e:
        print(f"获取个股资金流失败 {code}: {e}")
        return None


def _get_stock_sector_mx_uncached(code: str, name: str = None) -> Optional[List[str]]:
    """内部实现：单次妙想 query，由 get_stock_sector_mx 套缓存。"""
    client = get_mx_client()
    try:
        full_code = code.zfill(6)
        stock_name = name or ""
        query = f"{stock_name}{full_code}所属行业"
        result = client.query(query)
        if result.get("status") != 0:
            return None

        data = result.get("data", {}).get("data", {})
        dto_list = data.get("searchDataResultDTO", {}).get("dataTableDTOList", [])
        if not dto_list:
            return None

        dto = dto_list[0]
        table = dto.get("table", {})
        return parse_sector_list(table)
    except Exception as e:
        print(f"获取股票所属行业失败 {code}: {e}")
        return None


def get_stock_sector_mx(code: str, name: str = None) -> Optional[List[str]]:
    """
    获取股票所属行业分类列表

    Returns:
        行业名称列表（从具体到一般），如 ['证券Ⅲ', '证券Ⅱ', '非银金融']
    """
    key_code = str(code).zfill(6)
    key_name = str(name or "")
    raw = mx_cached(
        "mx_sector",
        ("stock_sector", key_code, key_name),
        lambda: _get_stock_sector_mx_uncached(code, name),
    )
    if raw is None:
        return None
    return list(raw)


def _get_sector_change_mx_uncached(sector_name: str) -> Optional[float]:
    client = get_mx_client()
    try:
        query = f"{sector_name}行业今日涨幅"
        result = client.query(query)
        if result.get("status") != 0:
            return None

        data = result.get("data", {}).get("data", {})
        dto_list = data.get("searchDataResultDTO", {}).get("dataTableDTOList", [])
        if not dto_list:
            return None

        dto = dto_list[0]
        raw_table = dto.get("rawTable", {})
        if "f3" in raw_table and raw_table["f3"]:
            return float(raw_table["f3"][0])
        return None
    except Exception as e:
        print(f"获取行业涨幅失败 {sector_name}: {e}")
        return None


def get_sector_change_mx(sector_name: str) -> Optional[float]:
    """
    获取行业板块当日涨跌幅

    Returns:
        涨跌幅百分比，如 -0.44
    """
    if not sector_name:
        return None
    return mx_cached(
        "mx_sector",
        ("sector_chg", str(sector_name)),
        lambda: _get_sector_change_mx_uncached(sector_name),
    )


def get_index_history_mx(index_code: str = "000001.SH", days: int = 5) -> Optional[pd.DataFrame]:
    """
    获取指数历史涨跌幅

    Args:
        index_code: 指数代码，如 '000001.SH' (上证), '000300.SH' (沪深300)
        days: 获取天数

    Returns:
        DataFrame with columns: ['date', 'change_pct']
    """
    client = get_mx_client()
    try:
        name_map = {
            "000001.SH": "上证指数",
            "000300.SH": "沪深300",
            "399006.SZ": "创业板指",
        }
        name = name_map.get(index_code, index_code)
        query = f"{name}最近{days}日涨跌幅"

        result = client.query(query)
        if result.get("status") != 0:
            return None

        data = result.get("data", {}).get("data", {})
        dto_list = data.get("searchDataResultDTO", {}).get("dataTableDTOList", [])
        if not dto_list:
            return None

        dto = dto_list[0]
        raw_table = dto.get("rawTable", {})
        head_name = raw_table.get("headName", [])

        data_key = None
        for key in raw_table.keys():
            if key != "headName":
                data_key = key
                break

        if not data_key or not head_name:
            return None

        rows = []
        for i, date_str in enumerate(head_name):
            if i < len(raw_table[data_key]):
                rows.append({
                    "date": date_str,
                    "change_pct": float(raw_table[data_key][i]),
                })

        return pd.DataFrame(rows)
    except Exception as e:
        print(f"获取指数历史数据失败 {index_code}: {e}")
        return None


def get_margin_total_history_mx(days: int = 5) -> Optional[pd.DataFrame]:
    """
    获取两市融资融券余额历史数据

    Returns:
        DataFrame with columns: ['date', 'margin_total']
        margin_total unit: 亿元
    """
    client = get_mx_client()
    try:
        result = client.query("两市融资融券余额")
        if result.get("status") != 0:
            return None

        data = result.get("data", {}).get("data", {})
        dto_list = data.get("searchDataResultDTO", {}).get("dataTableDTOList", [])
        if not dto_list:
            return None

        dto = dto_list[0]
        raw_table = dto.get("rawTable", {})
        head_name = raw_table.get("headName", [])

        data_key = None
        for key in raw_table.keys():
            if key != "headName":
                data_key = key
                break

        if not data_key or not head_name:
            return None

        rows = []
        for i, date_str in enumerate(head_name):
            if i < len(raw_table[data_key]):
                val = float(raw_table[data_key][i])
                rows.append({
                    "date": date_str,
                    "margin_total": val / 1e8,
                })

        df = pd.DataFrame(rows)
        if len(df) >= 2:
            df["margin_change_pct"] = df["margin_total"].pct_change() * 100
        else:
            df["margin_change_pct"] = 0.0
        return df
    except Exception as e:
        print(f"获取两市融资融券余额历史失败: {e}")
        return None


def get_margin_stock_history_mx(code: str, name: str = None, days: int = 5) -> Optional[pd.DataFrame]:
    """
    获取个股融资融券余额历史数据

    Returns:
        DataFrame with columns: ['date', 'margin_stock']
        margin_stock unit: 亿元
    """
    client = get_mx_client()
    try:
        full_code = code.zfill(6)
        stock_name = name or ""
        query = f"{stock_name}{full_code}融资融券余额"

        result = client.query(query)
        if result.get("status") != 0:
            return None

        data = result.get("data", {}).get("data", {})
        dto_list = data.get("searchDataResultDTO", {}).get("dataTableDTOList", [])
        if not dto_list:
            return None

        dto = dto_list[0]
        raw_table = dto.get("rawTable", {})
        head_name = raw_table.get("headName", [])

        data_key = None
        for key in raw_table.keys():
            if key != "headName":
                data_key = key
                break

        if not data_key or not head_name:
            return None

        rows = []
        for i, date_str in enumerate(head_name):
            if i < len(raw_table[data_key]):
                val = float(raw_table[data_key][i])
                rows.append({
                    "date": date_str,
                    "margin_stock": val / 1e8,
                })

        return pd.DataFrame(rows)
    except Exception as e:
        print(f"获取个股融资融券余额历史失败 {code}: {e}")
        return None
