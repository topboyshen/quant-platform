"""
MXDataClient 响应解析工具集。

从 mx_client.py 拆分出来的纯函数解析模块，无外部依赖（除 pandas），
便于单元测试和复用。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd


# 单位转换规则：(单位后缀, 乘数)
_UNIT_MULTIPLIERS = (
    ("亿股", 100_000_000),
    ("万股", 10_000),
    ("亿", 100_000_000),
    ("万手", 1_000_000),  # 10000 * 100
    ("万", 10_000),
)

# 实时数据字段映射：响应列名 -> 标准字段
_REALTIME_FIELD_MAP = {
    "最新价": "price",
    "f2": "price",
    "成交量": "volume",
    "今开": "open",
    "开盘价": "open",
    "最高": "high",
    "最高价": "high",
    "最低": "low",
    "最低价": "low",
    "昨收": "prev_close",
    "昨收价": "prev_close",
}

# 列名标准化映射
_COLUMN_NORMALIZE_MAP = {
    "开盘价": "open",
    "收盘价": "close",
    "最高价": "high",
    "最低价": "low",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "change_pct",
    "涨跌额": "change",
}

# OHLCV 缺失列的 fallback 策略
_OHLCV_FALLBACKS = {
    "close": ["收盘价", "最新价", "price"],
    "open": ["开盘价"],
    "high": ["最高价"],
    "low": ["最低价"],
    "volume": ["成交量"],
}


def parse_numeric_value(value: Any) -> Any:
    """解析带单位的数值，如 '2.309亿' -> 230900000"""
    if not value or not isinstance(value, str):
        return value

    val = value.replace("元", "").replace(",", "").replace("%", "").strip()

    for unit, multiplier in _UNIT_MULTIPLIERS:
        if unit in val:
            try:
                return float(val.replace(unit, "").strip()) * multiplier
            except (ValueError, TypeError):
                return value

    try:
        return float(val)
    except (ValueError, TypeError):
        return value


def normalize_column_name(col_name: str) -> str:
    """标准化列名（中文 -> 英文）"""
    return _COLUMN_NORMALIZE_MAP.get(col_name, col_name)


def ensure_ohlcv_columns(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """确保 DataFrame 包含必要的 OHLCV 列，缺失时按优先级填充"""
    df = df.copy()
    for target, sources in _OHLCV_FALLBACKS.items():
        if target not in df.columns:
            for src in sources:
                if src in df.columns:
                    df[target] = df[src]
                    break
            else:
                if target == "volume":
                    df[target] = 0
                elif target == "close":
                    return None
                else:
                    df[target] = df.get("close", 0)

    if "price" not in df.columns and "close" in df.columns:
        df["price"] = df["close"]

    return df


def parse_history_data(result: Dict[str, Any]) -> Optional[pd.DataFrame]:
    """
    解析历史数据响应。

    注意：妙想 API 的"日K数据"查询返回的是区间统计数据（涨跌幅、换手率、成交量）
    而非每日的 OHLCV 价格序列。此方法检测到这种情况时会直接返回 None，
    让调用方使用备用数据源（如 baostock）。
    """
    try:
        if result.get("status") != 0:
            return None

        data = result.get("data", {}).get("data", {})
        search_result = data.get("searchDataResultDTO", {})
        dto_list = search_result.get("dataTableDTOList", [])

        if not dto_list:
            return None

        dto = dto_list[0]
        table = dto.get("table", {})
        name_map = dto.get("nameMap", {})

        # 检测：如果返回的是区间统计数据（无收盘价/开盘价等），直接返回 None
        col_names = list(name_map.values())
        ohlc_keywords = ["收盘价", "开盘价", "最高价", "最低价", "close", "open", "high", "low"]
        has_ohlc = any(kw in col_names for kw in ohlc_keywords)

        if not has_ohlc:
            return None

        dates = table.get("headName", [])
        if not dates:
            return None

        rows = []
        for i, date in enumerate(dates):
            row = {"date": date.split("(")[0] if "(" in date else date}

            for key, values in table.items():
                if key == "headName":
                    continue

                col_name = name_map.get(key, key)
                value = values[i] if i < len(values) else None
                col_name = normalize_column_name(col_name)
                row[col_name] = parse_numeric_value(value)

            rows.append(row)

        df = pd.DataFrame(rows)
        df = df.sort_values("date").reset_index(drop=True)
        df = ensure_ohlcv_columns(df)
        return df

    except Exception as e:
        print(f"解析历史数据失败: {e}")
        return None


def parse_realtime_data(result: Dict[str, Any], code: str, name: str) -> Optional[Dict[str, Any]]:
    """解析实时数据响应"""
    try:
        if result.get("status") != 0:
            return None

        data = result.get("data", {}).get("data", {})
        search_result = data.get("searchDataResultDTO", {})
        dto_list = search_result.get("dataTableDTOList", [])

        if not dto_list:
            return None

        dto = dto_list[0]
        table = dto.get("table", {})
        name_map = dto.get("nameMap", {})

        entity_name = dto.get("entityName", "")
        stock_name = entity_name.split("(")[0] if "(" in entity_name else name

        result_data = {
            "code": code,
            "name": stock_name,
            "price": 0.0,
            "volume": 0,
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "prev_close": 0.0,
        }

        for key, values in table.items():
            if key == "headName" or not values:
                continue

            col_name = name_map.get(key, key)
            value = values[0] if isinstance(values, list) else values
            value = parse_numeric_value(value)

            field = _REALTIME_FIELD_MAP.get(col_name)
            if field:
                if field == "volume":
                    result_data[field] = int(float(value)) if value else 0
                else:
                    result_data[field] = float(value) if value else 0.0

        return result_data

    except Exception as e:
        print(f"解析实时数据失败: {e}")
        return None


def extract_first_table_value(result: dict, value_type: str = "int") -> Any:
    """从 mx_client query 结果中提取第一个非 headName 列的数据值"""
    if result.get("status") != 0:
        return 0
    data = result.get("data", {}).get("data", {})
    dto_list = data.get("searchDataResultDTO", {}).get("dataTableDTOList", [])
    if not dto_list:
        return 0
    table = dto_list[0].get("table", {})
    for key, values in table.items():
        if key != "headName" and values:
            raw = values[0]
            if value_type == "int":
                return int(raw) if raw else 0
            elif value_type == "float":
                return float(raw) if raw else 0.0
            else:
                return raw
    return 0 if value_type in ("int", "float") else None


def parse_money_flow_table(table: Dict[str, Any]) -> Dict[str, float]:
    """解析个股资金流向表格数据"""

    def _parse_value(values):
        if not values or not values[0]:
            return 0.0
        val_str = str(values[0]).replace("亿元", "").replace("万元", "").replace(",", "").strip()
        try:
            num = float(val_str)
            if "万元" in str(values[0]):
                num = num / 10000
            return num
        except Exception:
            return 0.0

    return {
        "main_net": _parse_value(table.get("f62")),
        "large_net": _parse_value(table.get("f72")),
        "medium_net": _parse_value(table.get("f78")),
        "small_net": _parse_value(table.get("f84")),
        "ddx": float(table.get("f88", [0])[0]) if table.get("f88") else 0.0,
        "ddy": float(table.get("f89", [0])[0]) if table.get("f89") else 0.0,
    }


def parse_sector_list(table: Dict[str, Any]) -> Optional[List[str]]:
    """从表格数据解析行业分类列表"""
    sectors = set()
    for key, values in table.items():
        if key == "headName" or not values:
            continue
        chain = str(values[0])
        for part in chain.split("-"):
            part = part.strip()
            if part and part not in ("行业名称", "所属行业名称"):
                sectors.add(part)
    return sorted(sectors, key=len, reverse=True) if sectors else None
